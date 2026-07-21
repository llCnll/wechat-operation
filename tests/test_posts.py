from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.adapters import SourceBusinessDeletedError
from posts.models import (
    POST_STATUS_BLOCKED,
    POST_STATUS_DELETED,
    POST_STATUS_NORMAL,
    POST_STATUS_SELF_VISIBLE,
    POST_STATUS_SUNK,
    AuthorMuteStatus,
    CandidatePost,
    InteractionSnapshot,
    MediaType,
    OriginalPost,
    OriginType,
    PostMedia,
    is_post_deleted,
    is_post_self_visible,
    mark_post_deleted,
    normalize_post_status,
)
from posts.services import (
    classify_content_card,
    refresh_post_status_and_metrics,
    store_post_metrics,
    sync_author_mute_status,
)


def _author_detail(
    *, appid="wx-game", openid="author-1", nick_name="作者", local_end=0, global_end=0
):
    return {
        "data": {
            "topic": {
                "appid": appid,
                "creater": {
                    "openid": openid,
                    "nick_name": nick_name,
                    "block_info": {"appid": appid, "block_end_time": local_end},
                    "global_block_end_time": global_end,
                },
            }
        }
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (None, POST_STATUS_DELETED),
        (POST_STATUS_NORMAL, POST_STATUS_DELETED),
        (POST_STATUS_BLOCKED, POST_STATUS_BLOCKED | POST_STATUS_DELETED),
        (POST_STATUS_SUNK, POST_STATUS_SUNK | POST_STATUS_DELETED),
        (
            POST_STATUS_BLOCKED | POST_STATUS_SUNK,
            POST_STATUS_BLOCKED | POST_STATUS_SUNK | POST_STATUS_DELETED,
        ),
    ),
)
def test_business_deletion_preserves_only_moderation_status_bits(status, expected):
    assert mark_post_deleted(status) == expected


@pytest.mark.parametrize(("value", "expected"), ((None, 2), ("", 2), ("8", 8), (16, 16)))
def test_missing_or_invalid_upstream_status_uses_normal_base(value, expected):
    assert normalize_post_status(value) == expected


def test_self_visible_status_is_preserved_as_an_upstream_visibility_state():
    assert normalize_post_status(POST_STATUS_SELF_VISIBLE) == POST_STATUS_SELF_VISIBLE
    assert is_post_self_visible(POST_STATUS_SELF_VISIBLE)
    assert not is_post_self_visible(POST_STATUS_NORMAL)


@pytest.mark.django_db
def test_forward_card_is_excluded_without_original_post():
    candidate = CandidatePost.objects.create(topic_id=1, content_id="forward")
    assert classify_content_card(candidate, {"forward_topic_info": {"source_topic_id": 9}}) is None
    candidate.refresh_from_db()
    assert candidate.origin_type == OriginType.FORWARD
    assert not OriginalPost.objects.exists()


@pytest.mark.django_db
def test_original_card_is_normalized():
    candidate = CandidatePost.objects.create(
        topic_id=2, content_id="original", source_appid="wx-game"
    )
    post = classify_content_card(
        candidate,
        {
            "base_content": {
                "title": "标题",
                "content": "<p>正文<br>话题</p>",
                "image_list": [{"url": "https://example.test/a.png", "width": 10, "height": 20}],
            },
            "reply_info": {"total_reply_count": 2},
            "love_info": {"total_love_count": 3},
            "share_info": {"total_share_count": 4},
        },
    )
    assert post is not None
    assert post.content_text == "正文\n话题"
    assert post.status == POST_STATUS_NORMAL
    candidate.refresh_from_db()
    assert candidate.origin_type == OriginType.ORIGINAL
    assert PostMedia.objects.filter(post=post).count() == 1
    assert InteractionSnapshot.objects.get(post=post).love_count == 3


@pytest.mark.django_db
def test_video_attachment_is_projected_as_allowed_media():
    candidate = CandidatePost.objects.create(topic_id=3, content_id="video", source_appid="wx-game")
    post = classify_content_card(
        candidate,
        {
            "base_content": {
                "content": "视频帖子",
                "video_info": [
                    {"url": "https://example.test/post.mp4", "width": 1280, "height": 720}
                ],
            }
        },
    )
    assert post is not None
    media = PostMedia.objects.get(post=post)
    assert media.media_type == MediaType.VIDEO


@pytest.mark.django_db
def test_ballot_title_and_option_text_are_normalized_for_review():
    candidate = CandidatePost.objects.create(
        topic_id=4, content_id="ballot", source_appid="wx-game"
    )
    post = classify_content_card(
        candidate,
        {
            "base_content": {
                "content": "普通正文",
                "ballot_info_list": [
                    {
                        "content_id": "poll-id-must-not-be-saved",
                        "title": "穿梭（竖着看第一个字）",
                        "option_item_list": [
                            {"content_id": "option-id-1", "text": "卫 护山河岁月长"},
                            {"content_id": "option-id-2", "text": "收 藏万象入华章"},
                        ],
                    }
                ],
            }
        },
    )

    assert post is not None
    assert post.ballot_text == (
        "投票 1：穿梭（竖着看第一个字）\n选项 1：卫 护山河岁月长\n选项 2：收 藏万象入华章"
    )
    assert "poll-id-must-not-be-saved" not in post.ballot_text
    assert "option-id-1" not in post.ballot_text


@pytest.mark.django_db
def test_base_info_card_is_normalized():
    candidate = CandidatePost.objects.create(
        topic_id=10, content_id="content-10", source_appid="game"
    )
    post = classify_content_card(
        candidate,
        {
            "base_info": {
                "publish_time": 1,
                "base_content": {"title": "标题", "content": "<p>正文</p>"},
                "reply_info": {"total_reply_count": 2},
            }
        },
    )
    candidate.refresh_from_db()
    assert post is not None
    assert post.content_text == "正文"
    assert post.publish_time is not None
    assert candidate.origin_type == OriginType.ORIGINAL


@pytest.mark.django_db
def test_metrics_snapshot_discards_reply_payloads():
    post = OriginalPost.objects.create(topic_id=11, content_id="content-11")
    snapshot = store_post_metrics(
        post,
        {
            "data": {
                "topic": {"view_count": 12, "reply_count": 3, "love_count": 4},
                "reply_list": [{"content": "不应保存"}],
                "child_reply_list": [{"content": "不应保存"}],
            }
        },
    )
    assert snapshot.view_count == 12
    assert snapshot.reply_count == 3
    assert snapshot.love_count == 4


@pytest.mark.django_db
def test_status_and_metrics_refresh_marks_deleted_without_changing_cached_content():
    post = OriginalPost.objects.create(
        topic_id=12,
        content_id="content-12",
        content_text="历史缓存内容",
        source_appid="wx-game",
        status=POST_STATUS_SUNK,
    )

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == 12
            raise SourceBusinessDeletedError("原平台帖子已从管理队列删除")

        def get_content_card(self, _content_id):
            raise AssertionError("轻量刷新不得拉取内容卡")

    refreshed = refresh_post_status_and_metrics(Source(), post)

    assert refreshed.status == POST_STATUS_SUNK | POST_STATUS_DELETED
    assert is_post_deleted(refreshed.status)
    assert refreshed.content_text == "历史缓存内容"


@pytest.mark.django_db
def test_management_detail_recovery_clears_local_deletion_bit():
    post = OriginalPost.objects.create(
        topic_id=13,
        content_id="content-13",
        source_appid="wx-game",
        status=POST_STATUS_SUNK | POST_STATUS_DELETED,
    )

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == 13
            return {"data": {"topic": {"view_count": 2, "status": POST_STATUS_BLOCKED}}}

        def get_content_card(self, _content_id):
            raise AssertionError("轻量刷新不得拉取内容卡")

    refreshed = refresh_post_status_and_metrics(Source(), post)

    assert refreshed.status == POST_STATUS_BLOCKED
    assert not is_post_deleted(refreshed.status)


@pytest.mark.django_db
def test_author_mute_sync_normalizes_and_clears_expired_statuses():
    post = OriginalPost.objects.create(
        topic_id=14, content_id="author-status", source_appid="wx-game"
    )
    future = int((timezone.now() + timedelta(days=1)).timestamp())
    state = sync_author_mute_status(
        post,
        _author_detail(local_end=future, global_end=future),
        source_requested_at=timezone.now(),
    )
    assert state.local_muted_until is not None
    assert state.global_muted_until is not None
    assert post.author_openid == "author-1"

    state = sync_author_mute_status(
        post, _author_detail(local_end=0, global_end=0), source_requested_at=timezone.now()
    )
    assert state.local_muted_until is None
    assert state.global_muted_until is None
    assert state.first_detected_at is not None


@pytest.mark.django_db
def test_author_mute_sync_creates_unmuted_observation_and_updates_nickname():
    first = OriginalPost.objects.create(
        topic_id=15, content_id="author-first", source_appid="wx-game"
    )
    second = OriginalPost.objects.create(
        topic_id=16, content_id="author-second", source_appid="wx-game"
    )
    sync_author_mute_status(
        first, _author_detail(nick_name="旧昵称", local_end=0), source_requested_at=timezone.now()
    )
    state = sync_author_mute_status(
        second, _author_detail(nick_name="新昵称", local_end=0), source_requested_at=timezone.now()
    )
    assert AuthorMuteStatus.objects.count() == 1
    assert state.nick_name == "新昵称"
    assert state.last_source_post == second
    assert first.author_openid == second.author_openid == "author-1"
    assert state.local_muted_until is None
    assert state.global_muted_until is None


@pytest.mark.django_db
def test_author_mute_sync_isolated_by_game_and_ignores_out_of_order_detail():
    first = OriginalPost.objects.create(
        topic_id=17, content_id="author-game-a", source_appid="game-a"
    )
    second = OriginalPost.objects.create(
        topic_id=18, content_id="author-game-b", source_appid="game-b"
    )
    now = timezone.now()
    later = now + timedelta(seconds=10)
    local_end = int((now + timedelta(days=1)).timestamp())
    sync_author_mute_status(
        first, _author_detail(appid="game-a", local_end=local_end), source_requested_at=later
    )
    stale = sync_author_mute_status(
        first, _author_detail(appid="game-a", local_end=0), source_requested_at=now
    )
    sync_author_mute_status(
        second, _author_detail(appid="game-b", local_end=0), source_requested_at=later
    )
    assert stale.local_muted_until is not None
    assert AuthorMuteStatus.objects.filter(openid="author-1").count() == 2


@pytest.mark.django_db
def test_author_mute_sync_keeps_the_post_game_scope_when_detail_appid_differs():
    post = OriginalPost.objects.create(
        topic_id=181, content_id="author-appid-mismatch", source_appid="game-a"
    )
    future = int((timezone.now() + timedelta(days=1)).timestamp())

    state = sync_author_mute_status(
        post,
        _author_detail(appid="game-b", local_end=future),
        source_requested_at=timezone.now(),
    )

    assert state.source_appid == "game-a"
    assert state.local_muted_until is None
    assert not AuthorMuteStatus.objects.filter(openid="author-1", source_appid="game-b").exists()


@pytest.mark.django_db
def test_author_mute_sync_does_not_create_observation_without_openid():
    post = OriginalPost.objects.create(
        topic_id=19, content_id="author-missing", source_appid="wx-game"
    )
    payload = _author_detail()
    del payload["data"]["topic"]["creater"]["openid"]
    assert sync_author_mute_status(post, payload) is None
    assert not AuthorMuteStatus.objects.exists()
