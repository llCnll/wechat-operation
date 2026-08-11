import pytest
from django.contrib.auth import get_user_model

from config.game_circle_ipc import GameCirclePostClient
from jobs.adapters import SourceContractError
from jobs.models import BackgroundJob, JobStatus
from jobs.services import execute_next_job
from jobs.task_types import COMMENT_EXPORT_V1_TASK
from posts.comment_exports import (
    CommentExportSnapshot,
    comment_export_handler,
    create_or_reuse_run,
    format_source_time,
    issue_confirmation_token,
    match_comment,
    normalize_keyword,
    read_confirmation_snapshot,
)
from posts.game_catalog import ExecutionScope
from posts.models import ActiveGameScope, CommentExportRun, ManagedGame, OriginalPost
from posts.source_client import CdpGameCircleClient


def test_comment_export_rule_is_literal_nfc_and_ascii_digits_only():
    assert normalize_keyword("  甄嬛传  ") == "甄嬛传"
    assert normalize_keyword(" ") == ""
    assert match_comment("甄嬛传 123 与 456", "甄嬛传") == "123"
    assert match_comment("甄嬛传１２３", "甄嬛传") is None
    assert match_comment("甄嬛传 123", "甄嬛") == "123"
    assert match_comment("甄嬛传 123", "甄嬛傳") is None


def test_comment_export_rule_rejects_oversized_keyword_and_invalid_timestamp():
    with pytest.raises(ValueError):
        normalize_keyword("x" * 101)
    with pytest.raises(SourceContractError):
        format_source_time(None)


def test_comment_export_reply_page_is_fixed_at_one_hundred():
    client = GameCirclePostClient(scope=ExecutionScope("game-a", 1), ipc=object())
    with pytest.raises(SourceContractError, match="必须为 100"):
        client.list_topic_replies(topic_id=1, topic_openid="author", page=0, limit=99)


def test_comment_export_source_page_rejects_mismatched_topic_identity(monkeypatch):
    source = CdpGameCircleClient(appid="game-a", frame=object(), identity_token="identity")
    monkeypatch.setattr(
        source,
        "_get",
        lambda *_args, **_kwargs: {
            "data": {
                "topic": {"topic_id": 9, "appid": "game-a", "creater": {"openid": "author"}},
                "reply_list": [],
                "has_next": False,
            }
        },
    )

    with pytest.raises(SourceContractError, match="评论响应结构无效"):
        source.list_topic_replies(topic_id=1, topic_openid="author", page=0, limit=100)


@pytest.mark.django_db
def test_confirmation_snapshot_is_read_only_and_uses_source_content(monkeypatch):
    game = ManagedGame.objects.create(appid="game-a", name="测试游戏")
    ActiveGameScope.objects.create(active_game=game)
    post = OriginalPost.objects.create(
        topic_id=9,
        content_id="content-9",
        source_appid="game-a",
        author_openid="author",
        author_nick_name="本地昵称",
        title="旧标题",
        content_text="旧正文",
    )

    class Source:
        def __init__(self, **_kwargs):
            pass

        def get_post_metrics(self, _topic_id):
            return {
                "data": {
                    "topic": {
                        "topic_id": 9,
                        "appid": "game-a",
                        "creater": {"openid": "author", "nick_name": "来源昵称"},
                        "view_count": 3,
                        "love_count": 2,
                        "reply_count": 1,
                    }
                }
            }

        def get_content_card(self, _content_id):
            return {
                "data": {
                    "content_card": {
                        "base_content": {"title": "新标题", "content": "新正文<br>#尾部"}
                    }
                }
            }

        def close(self):
            return None

    monkeypatch.setattr("posts.comment_exports.GameCirclePostClient", Source)
    snapshot = read_confirmation_snapshot(topic_id=9)
    post.refresh_from_db()
    assert snapshot.title == "新标题" and snapshot.content_text == "新正文"
    assert snapshot.author_nick_name == "来源昵称"
    assert post.title == "旧标题" and post.content_text == "旧正文"


@pytest.mark.django_db
def test_comment_export_worker_saves_only_matching_results(monkeypatch):
    user = get_user_model().objects.create_user("comment-exporter")
    game = ManagedGame.objects.create(appid="game-a", name="测试游戏")
    ActiveGameScope.objects.create(active_game=game)
    job = BackgroundJob.objects.create(
        task_type=COMMENT_EXPORT_V1_TASK,
        idempotency_key="comment-export:test",
        checkpoint={"execution_scope": {"target_appid": "game-a", "scope_version": 1}},
    )
    run = CommentExportRun.objects.create(
        job=job,
        created_by=user,
        source_appid="game-a",
        scope_version=1,
        topic_id=1,
        title_snapshot="测试帖",
        author_openid="author",
        rule_version="keyword-first-digit-v1",
        keyword="组队",
        page_size=100,
    )

    class Source:
        def __init__(self, **_kwargs):
            pass

        def list_topic_replies(self, **_kwargs):
            return {
                "items": [
                    {
                        "reply_id": "hit",
                        "openid": "u1",
                        "nick_name": "甲",
                        "create_time": 1,
                        "content": "组队 123",
                    },
                    {
                        "reply_id": "miss",
                        "openid": "u2",
                        "nick_name": "乙",
                        "create_time": 1,
                        "content": "无数字",
                    },
                ],
                "has_next": False,
                "total_count": 2,
            }

        def close(self):
            return None

    monkeypatch.setattr("posts.comment_exports.GameCirclePostClient", Source)
    completed = execute_next_job({COMMENT_EXPORT_V1_TASK: comment_export_handler})
    assert completed is not None
    job.refresh_from_db()
    run.refresh_from_db()
    assert job.status == JobStatus.SUCCEEDED
    assert run.processed_count == 2 and run.matched_count == 1
    assert list(run.results.values_list("reply_id", flat=True)) == ["hit"]


@pytest.mark.django_db
def test_comment_export_worker_saves_every_top_level_comment_when_rule_is_empty(monkeypatch):
    user = get_user_model().objects.create_user("full-comment-exporter")
    game = ManagedGame.objects.create(appid="game-a", name="测试游戏")
    ActiveGameScope.objects.create(active_game=game)
    job = BackgroundJob.objects.create(
        task_type=COMMENT_EXPORT_V1_TASK,
        idempotency_key="comment-export:full",
        checkpoint={"execution_scope": {"target_appid": "game-a", "scope_version": 1}},
    )
    run = CommentExportRun.objects.create(
        job=job,
        created_by=user,
        source_appid="game-a",
        scope_version=1,
        topic_id=1,
        title_snapshot="测试帖",
        author_openid="author",
        rule_version="keyword-first-digit-v1",
        keyword="",
        page_size=100,
    )

    class Source:
        def __init__(self, **_kwargs):
            pass

        def list_topic_replies(self, **_kwargs):
            return {
                "items": [
                    {
                        "reply_id": "first",
                        "openid": "u1",
                        "nick_name": "甲",
                        "create_time": 1,
                        "content": "没有数字的评论",
                    },
                    {
                        "reply_id": "second",
                        "openid": "u2",
                        "nick_name": "乙",
                        "create_time": 2,
                        "content": "任意评论 456",
                    },
                ],
                "has_next": False,
                "total_count": 2,
            }

        def close(self):
            return None

    monkeypatch.setattr("posts.comment_exports.GameCirclePostClient", Source)
    execute_next_job({COMMENT_EXPORT_V1_TASK: comment_export_handler})

    run.refresh_from_db()
    assert run.processed_count == 2 and run.matched_count == 2
    assert list(run.results.order_by("reply_id").values_list("reply_id", "game_id")) == [
        ("first", ""),
        ("second", ""),
    ]


@pytest.mark.django_db
def test_full_export_and_keyword_export_use_separate_active_jobs(monkeypatch):
    user = get_user_model().objects.create_user("export-key-user")
    snapshot = CommentExportSnapshot(
        topic_id=1,
        source_appid="game-a",
        scope_version=1,
        author_openid="author",
        author_nick_name="作者",
        title="测试帖",
        content_text="正文",
        view_count=0,
        love_count=0,
        reply_count=0,
        fingerprint="fingerprint",
    )
    token = issue_confirmation_token(snapshot=snapshot, user_id=user.pk)
    monkeypatch.setattr("posts.comment_exports.read_confirmation_snapshot", lambda **_kwargs: snapshot)

    full, full_created = create_or_reuse_run(user_id=user.pk, token=token, keyword_value="")
    repeated_full, repeated_created = create_or_reuse_run(
        user_id=user.pk, token=token, keyword_value=" "
    )
    filtered, filtered_created = create_or_reuse_run(
        user_id=user.pk, token=token, keyword_value="组队"
    )

    assert full_created is True
    assert repeated_created is False
    assert repeated_full.pk == full.pk
    assert filtered_created is True
    assert filtered.pk != full.pk


@pytest.mark.django_db
def test_invalid_configured_keyword_stays_on_the_rule_step(client, monkeypatch):
    user = get_user_model().objects.create_user("rule-step-user")
    client.force_login(user)
    snapshot = CommentExportSnapshot(
        topic_id=1,
        source_appid="game-a",
        scope_version=1,
        author_openid="author",
        author_nick_name="作者",
        title="测试帖",
        content_text="正文",
        view_count=0,
        love_count=0,
        reply_count=0,
        fingerprint="fingerprint",
    )
    monkeypatch.setattr("posts.views.read_confirmation_snapshot", lambda **_kwargs: snapshot)

    response = client.post(
        "/comment-export/",
        {
            "action": "configure",
            "confirmation_token": issue_confirmation_token(snapshot=snapshot, user_id=user.pk),
            "keyword": "x" * 101,
        },
    )

    assert response.status_code == 200
    assert response.context["step"] == 2
    assert "关键词长度" in response.context["form_error"]


@pytest.mark.django_db
def test_empty_rule_moves_to_creation_step_for_a_full_export(client, monkeypatch):
    user = get_user_model().objects.create_user("full-export-step-user")
    client.force_login(user)
    snapshot = CommentExportSnapshot(
        topic_id=1,
        source_appid="game-a",
        scope_version=1,
        author_openid="author",
        author_nick_name="作者",
        title="测试帖",
        content_text="正文",
        view_count=0,
        love_count=0,
        reply_count=0,
        fingerprint="fingerprint",
    )
    monkeypatch.setattr("posts.views.read_confirmation_snapshot", lambda **_kwargs: snapshot)

    response = client.post(
        "/comment-export/",
        {
            "action": "configure",
            "confirmation_token": issue_confirmation_token(snapshot=snapshot, user_id=user.pk),
            "keyword": " ",
        },
    )

    assert response.status_code == 200
    assert response.context["step"] == 3
    assert response.context["keyword"] == ""
    assert "全部顶层评论" in response.content.decode()
