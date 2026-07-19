from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from config.settings import runtime
from jobs.models import BackgroundJob
from posts.models import (
    POST_STATUS_BLOCKED,
    POST_STATUS_DELETED,
    POST_STATUS_SUNK,
    CandidatePost,
    InteractionSnapshot,
    ManagedGame,
    OriginalPost,
    OriginType,
)
from reviews.adapters import AssessmentOutput, ReplyOutput, RiskOutput
from reviews.models import RecommendedAction, ReviewStatus, ReviewTask
from reviews.services import AI_EVALUATE_TASK, OCR_EXTRACT_TASK, persist_assessment


@pytest.mark.django_db
def test_operations_dashboard_requires_login():
    response = Client().get("/")
    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_legacy_original_post_admin_url_redirects_to_operations_dashboard():
    response = Client().get("/admin/posts/originalpost/")
    assert response.status_code == 302
    assert response.url == "/"


@pytest.mark.django_db
def test_operations_dashboard_renders_original_post_metrics():
    user = get_user_model().objects.create_user(username="operator", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=99, content_id="content-99", content_text="运营测试帖子正文"
    )
    InteractionSnapshot.objects.create(
        post=post,
        view_count=12,
        love_count=3,
        reply_count=2,
        share_count=0,
        source="topicsearchgetdetail",
    )
    client = Client()
    client.force_login(user)

    response = client.get("/")

    assert response.status_code == 200
    assert "内容管理" in response.content.decode()
    assert "运营测试帖子正文" in response.content.decode()
    assert "浏览 12" in response.content.decode()
    assert "审核处置" in response.content.decode()
    assert 'class="cdp-mode">CDP</span>' in response.content.decode()
    assert 'data-cdp-login-status' in response.content.decode()
    assert 'fetch("/auth/status"' in response.content.decode()
    assert 'action="/auth/open-login/"' in response.content.decode()
    assert 'data-operation="SKIP"' in response.content.decode()
    assert 'data-operation="UNMUTE"' in response.content.decode()
    assert f"/posts/{post.topic_id}/refresh/" in response.content.decode()


@pytest.mark.django_db
@patch("posts.views.open_controlled_login_page")
def test_invalid_cdp_login_control_only_opens_controlled_chrome(mock_open_login):
    user = get_user_model().objects.create_user(username="cdp-login", password="test-password")
    client = Client()
    client.force_login(user)

    response = client.post("/auth/open-login/", {"return_to": "/reports/"})

    assert response.status_code == 302
    assert response.url == "/reports/"
    mock_open_login.assert_called_once_with()


@pytest.mark.django_db
def test_operations_dashboard_shows_existing_local_reply_draft_without_publish_controls():
    user = get_user_model().objects.create_user(username="reply-draft-operator", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=1099, content_id="reply-draft-content", content_text="希望增加编队预设"
    )
    persist_assessment(
        post,
        AssessmentOutput(
            content_category="SUGGESTION",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.91,
            reply_suggestion=ReplyOutput(
                user_intent_summary="希望减少重复配队操作",
                reply_draft="感谢建议，我们已记录编队预设需求。",
                missing_information=["常用阵容数量"],
                knowledge_sources=["已发布功能说明"],
                risk_notice="不要承诺具体上线时间。",
            ),
        ),
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    assert "有回复建议" in page
    assert "回复建议" in page
    assert "希望减少重复配队操作" in page
    assert "仅供运营准备回复，不会发送或发布到原平台。" in page
    assert "标记本地已采纳" in page
    assert "data-copy-reply-draft" in page
    assert f"/posts/{post.topic_id}/reply-draft/" in page
    assert "/publish/" not in page


@pytest.mark.django_db
def test_operations_dashboard_hides_reply_draft_card_when_no_local_draft_exists():
    user = get_user_model().objects.create_user(username="no-reply-draft", password="test-password")
    post = OriginalPost.objects.create(topic_id=1100, content_id="no-reply-draft-content")
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    assert "有回复建议" not in page
    assert 'id="reply-draft-text"' not in page


@pytest.mark.django_db
def test_dashboard_requires_one_block_reason_without_requiring_every_reason():
    user = get_user_model().objects.create_user(
        username="block-reason-ui", password="test-password"
    )
    OriginalPost.objects.create(topic_id=98, content_id="block-reason-content")
    client = Client()
    client.force_login(user)

    page = client.get("/").content.decode()

    assert "请选择至少一个违规原因。" in page
    assert 'input.required=(!operationReasons.hidden&&input.name==="block_reason")' not in page
    assert 'name="confirmed"' not in page
    assert "data-reason-suggestion" in page
    assert 'input.name==="block_reason"&&action==="BLOCK"&&input.value==="1"' in page


@pytest.mark.django_db
def test_dashboard_renders_quick_filter_for_normal_pending_recommendations():
    user = get_user_model().objects.create_user(
        username="quick-filter", password="test-password"
    )
    client = Client()
    client.force_login(user)

    page = client.get("/").content.decode()

    assert "快速筛选" in page
    assert "待处理推荐" in page
    assert "suggested_action=SINK" in page
    assert "suggested_action=BLOCK" in page
    assert "suggested_action=MUTE" in page
    assert "suggested_action=HUMAN_REVIEW" in page
    assert "review_status=PENDING" in page
    assert "post_status=NORMAL" in page
    assert "批量执行 AI 建议" in page
    assert "当前没有可执行的 AI 沉底或屏蔽建议" in page


@pytest.mark.django_db
def test_operations_dashboard_can_sort_by_upstream_post_creation_time():
    user = get_user_model().objects.create_user(username="time-sort", password="test-password")
    now = timezone.now()
    OriginalPost.objects.create(
        topic_id=9101,
        content_id="oldest-post",
        content_text="最早创建的帖子",
        publish_time=now - timedelta(hours=2),
    )
    OriginalPost.objects.create(
        topic_id=9102,
        content_id="newest-post",
        content_text="最新创建的帖子",
        publish_time=now,
    )
    client = Client()
    client.force_login(user)

    newest_first = client.get("/", {"sort": "publish_time_desc"}).content.decode()
    oldest_first = client.get("/", {"sort": "publish_time_asc"}).content.decode()

    assert newest_first.find("最新创建的帖子") < newest_first.find("最早创建的帖子")
    assert oldest_first.find("最早创建的帖子") < oldest_first.find("最新创建的帖子")
    assert 'value="publish_time_desc" class="is-selected"' in newest_first
    assert 'type="hidden" name="sort" value="publish_time_desc"' in newest_first


@pytest.mark.django_db
def test_dashboard_keyboard_navigation_uses_real_query_separators_and_repairs_legacy_urls():
    user = get_user_model().objects.create_user(username="keyboard-query", password="test-password")
    first = OriginalPost.objects.create(topic_id=9011, content_id="keyboard-first")
    OriginalPost.objects.create(topic_id=9012, content_id="keyboard-second")
    client = Client()
    client.force_login(user)

    response = client.get("/", {"topic": first.topic_id, "sort": "publish_time_desc"})
    repaired = client.get(
        f"/?appid=wx6d2454a8afcd9706&amp%3Bsort=publish_time_desc&amp%3Btopic={first.topic_id}"
    )

    assert response.status_code == 200
    assert repaired.status_code == 302
    assert repaired.url == f"/?appid=wx6d2454a8afcd9706&sort=publish_time_desc&topic={first.topic_id}"


@pytest.mark.django_db
def test_operations_dashboard_renders_post_media():
    user = get_user_model().objects.create_user(username="operator", password="test-password")
    post = OriginalPost.objects.create(topic_id=100, content_id="content-100")
    post.media.create(url="https://example.test/post-image.png")
    client = Client()
    client.force_login(user)

    response = client.get("/")

    assert response.status_code == 200
    assert 'src="https://example.test/post-image.png"' in response.content.decode()


@pytest.mark.django_db
def test_operations_dashboard_shows_sink_status_and_only_restore_transition():
    user = get_user_model().objects.create_user(username="sink-status", password="test-password")
    OriginalPost.objects.create(topic_id=1003, content_id="sunk-content", status=8)
    client = Client()
    client.force_login(user)

    response = client.get("/")

    page = response.content.decode()
    assert "当前状态：已沉底" in page
    assert "上游 已沉底" in page
    assert 'title="帖子当前已沉底">已沉底</button>' in page
    assert 'data-operation="SINK"' not in page
    assert 'data-operation="RESTORE"' in page


@pytest.mark.django_db
def test_dashboard_filters_combined_upstream_post_statuses():
    user = get_user_model().objects.create_user(username="post-status-filter", password="test-password")
    normal = OriginalPost.objects.create(
        topic_id=1004, content_id="status-normal", content_text="状态正常", status=2
    )
    blocked = OriginalPost.objects.create(
        topic_id=1005, content_id="status-blocked", content_text="状态屏蔽", status=4
    )
    sunk = OriginalPost.objects.create(
        topic_id=1006, content_id="status-sunk", content_text="状态沉底", status=8
    )
    deleted_blocked = OriginalPost.objects.create(
        topic_id=1007,
        content_id="status-deleted-blocked",
        content_text="状态删除屏蔽",
        status=POST_STATUS_DELETED | POST_STATUS_BLOCKED,
    )
    OriginalPost.objects.create(
        topic_id=1008, content_id="status-unsynced", content_text="状态未同步", status=None
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"post_status": ["BLOCKED", "SUNK"]}).content.decode()

    assert blocked.content_text in page
    assert sunk.content_text in page
    assert deleted_blocked.content_text in page
    assert normal.content_text not in page
    assert "状态未同步" not in page
    assert 'name="post_status" value="BLOCKED" checked' in page


@pytest.mark.django_db
def test_operations_dashboard_renders_ballot_title_and_options():
    user = get_user_model().objects.create_user(
        username="ballot-operator", password="test-password"
    )
    OriginalPost.objects.create(
        topic_id=1000,
        content_id="ballot-content",
        ballot_text="投票 1：穿梭（竖着看第一个字）\n选项 1：卫 护山河岁月长",
    )
    client = Client()
    client.force_login(user)

    response = client.get("/")

    page = response.content.decode()
    assert "投票内容" in page
    assert "穿梭（竖着看第一个字）" in page
    assert "卫 护山河岁月长" in page


@pytest.mark.django_db
def test_operations_dashboard_keeps_posts_deleted_from_management_queue_as_status():
    user = get_user_model().objects.create_user(
        username="deleted-operator", password="test-password"
    )
    OriginalPost.objects.create(
        topic_id=1001,
        content_id="deleted-content",
        content_text="仍应在默认队列展示",
        status=POST_STATUS_DELETED | POST_STATUS_BLOCKED,
    )
    client = Client()
    client.force_login(user)

    response = client.get("/")

    assert response.status_code == 200
    page = response.content.decode()
    assert "仍应在默认队列展示" in page
    assert "原平台已删除" in page
    assert "当前状态：原平台已删除、已屏蔽" in page
    assert 'data-operation="BLOCK"' not in page


@pytest.mark.django_db
def test_dashboard_rejects_operation_for_post_deleted_from_management_queue(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_block_writes", True)
    user = get_user_model().objects.create_user(
        username="deleted-action", password="test-password"
    )
    post = OriginalPost.objects.create(
        topic_id=1002,
        content_id="deleted-action-content",
        status=POST_STATUS_DELETED | POST_STATUS_SUNK,
        source_appid="wx-game",
    )

    class Source:
        def __init__(self, **kwargs):
            raise AssertionError("业务删除帖子不能建立上游处置会话")

    monkeypatch.setattr("posts.views.CdpGameCircleClient", Source)
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/BLOCK/",
        {"reason": "广告", "block_reason": "8", "confirmed": "yes"},
    )

    assert response.status_code == 302
    assert not ReviewTask.objects.filter(post=post).exists()


@pytest.mark.django_db
def test_dashboard_skip_completes_review_without_upstream_request():
    user = get_user_model().objects.create_user(username="skip-operator", password="test-password")
    post = OriginalPost.objects.create(topic_id=101, content_id="content-101")
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/SKIP/",
        {
            "reason": "人工确认无须处置",
            "return_query": "appid=wx-test&sort=publish_time_desc&category=GUIDE",
        },
    )

    assert response.status_code == 302
    assert response.url == "/?appid=wx-test&sort=publish_time_desc&category=GUIDE&topic=101"
    assert post.review_task.status == ReviewStatus.REVIEWED
    assert post.review_task.final_action == RecommendedAction.KEEP
    assert not post.review_task.operation_audits.exists()


@pytest.mark.django_db
def test_dashboard_block_creates_disabled_audit_when_capability_is_off(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", False)
    monkeypatch.setattr(runtime, "enable_block_writes", False)
    user = get_user_model().objects.create_user(username="block-operator", password="test-password")
    post = OriginalPost.objects.create(topic_id=102, content_id="content-102")
    CandidatePost.objects.create(
        topic_id=102, content_id="content-102", origin_type=OriginType.ORIGINAL
    )
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/BLOCK/",
        {"reason": "广告欺诈", "block_reason": "8", "confirmed": "yes"},
    )

    assert response.status_code == 302
    task = post.review_task
    assert task.operation_audits.get().result_code == "CAPABILITY_DISABLED"
    assert task.status == ReviewStatus.PENDING
    assert task.final_action == ""


@pytest.mark.django_db
def test_dashboard_successful_single_operation_completes_human_review(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    user = get_user_model().objects.create_user(username="sink-review", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=105,
        content_id="sink-review-content",
        source_appid="wx-sink-review",
        status=2,
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id,
        content_id=post.content_id,
        source_appid=post.source_appid,
        origin_type=OriginType.ORIGINAL,
    )

    class Source:
        def __init__(self, **kwargs):
            self.status = 2

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": self.status}}}

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            assert topic_id == post.topic_id
            assert action == RecommendedAction.SINK
            assert payload == {}
            self.status = 8
            return {"errcode": 0, "errmsg": "ok"}

        def close(self):
            pass

    monkeypatch.setattr("posts.views.CdpGameCircleClient", Source)
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/SINK/",
        {"reason": "无价值灌水", "confirmed": "yes", "confirmation_key": "sink-success"},
    )

    assert response.status_code == 302
    task = post.review_task
    assert task.operation_audits.get().result_code == "OK"
    assert task.status == ReviewStatus.REVIEWED
    assert task.final_action == RecommendedAction.SINK
    assert task.reason == "无价值灌水"


@pytest.mark.django_db
def test_dashboard_partial_combined_operation_keeps_human_review_pending():
    user = get_user_model().objects.create_user(username="combined-pending", password="test-password")
    post = OriginalPost.objects.create(topic_id=108, content_id="combined-pending-content", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/BLOCK_AND_MUTE/",
        {
            "reason": "疑似广告欺诈",
            "block_reason": "8",
            "mute_days": "1",
            "confirmed": "yes",
            "confirmation_key": "combined-pending",
        },
    )

    assert response.status_code == 302
    task = post.review_task
    assert task.status == ReviewStatus.PENDING
    assert task.final_action == ""
    result_codes = set(task.operation_audits.values_list("result_code", flat=True))
    assert task.operation_audits.count() == 2
    assert "OK" not in result_codes


@pytest.mark.django_db
def test_dashboard_shows_ai_suggestion_and_human_final_action_without_feedback_ui():
    user = get_user_model().objects.create_user(username="result-display", password="test-password")
    post = OriginalPost.objects.create(topic_id=106, content_id="result-display-content", status=2)
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.SINK,
            overall_confidence=0.8,
        ),
    )
    task.status = ReviewStatus.REVIEWED
    task.final_action = RecommendedAction.KEEP
    task.reason = "人工保留"
    task.save(update_fields=["status", "final_action", "reason"])
    client = Client()
    client.force_login(user)

    response = client.get(f"/?topic={post.topic_id}")

    page = response.content.decode()
    assert "AI 沉底" in page
    assert "人工 保留" in page
    assert "人工结果：保留" in page
    assert 'class="sink is-recommended"' in page
    assert "回复草案" not in page
    assert "AI 判断正确" not in page


@pytest.mark.django_db
def test_dashboard_allows_deleted_post_to_be_requeued_for_local_evaluation():
    user = get_user_model().objects.create_user(username="deleted-requeue", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=107,
        content_id="deleted-requeue-content",
        status=POST_STATUS_DELETED,
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    client = Client()
    client.force_login(user)

    response = client.post(f"/posts/{post.topic_id}/requeue/", {"force": "yes"})

    assert response.status_code == 302
    jobs = BackgroundJob.objects.filter(parameter_summary__topic_id=post.topic_id)
    assert jobs.filter(task_type=OCR_EXTRACT_TASK).count() == 1
    assert jobs.filter(task_type=AI_EVALUATE_TASK).count() == 1


@pytest.mark.django_db
def test_dashboard_uses_one_selected_game_and_other_filters_remain_multi_value():
    user = get_user_model().objects.create_user(username="multi-filter", password="test-password")

    def create_assessed_post(topic_id, appid, content, category, risk, action, status):
        post = OriginalPost.objects.create(
            topic_id=topic_id,
            content_id=f"multi-filter-{topic_id}",
            source_appid=appid,
            content_text=content,
            status=2,
        )
        CandidatePost.objects.create(
            topic_id=topic_id,
            content_id=post.content_id,
            source_appid=appid,
            origin_type=OriginType.ORIGINAL,
        )
        _, task = persist_assessment(
            post,
            AssessmentOutput(
                content_category=category,
                recommended_action=action,
                overall_confidence=0.8,
                risk_labels=[RiskOutput(label=risk, confidence=0.8, evidence=[content])],
            ),
        )
        task.status = status
        task.save(update_fields=["status"])
        return post

    first = create_assessed_post(
        109, "wx-filter-a", "多选命中 A", "GUIDE", "CHEAT", "SINK", ReviewStatus.PENDING
    )
    second = create_assessed_post(
        110, "wx-filter-b", "多选命中 B", "FEEDBACK", "TRAFFIC_DIVERSION", "KEEP", ReviewStatus.REVIEWED
    )
    create_assessed_post(
        111, "wx-filter-c", "不应命中", "SOCIAL", "OFF_TOPIC", "HUMAN_REVIEW", ReviewStatus.PENDING
    )
    client = Client()
    client.force_login(user)

    ManagedGame.objects.bulk_create(
        [
            ManagedGame(appid="wx-filter-a", name="筛选游戏 A"),
            ManagedGame(appid="wx-filter-b", name="筛选游戏 B"),
        ]
    )
    default_page = client.get("/").content.decode()
    assert "多选命中 A" not in default_page
    assert "多选命中 B" not in default_page
    assert 'id="active-game"' in default_page
    assert 'name="appid" value="wx-filter-a"' not in default_page

    response = client.get(
        "/",
        {
            "appid": "wx-filter-a",
            "category": ["GUIDE", "FEEDBACK"],
            "risk": ["CHEAT", "TRAFFIC_DIVERSION"],
            "suggested_action": ["SINK", "KEEP"],
            "review_status": [ReviewStatus.PENDING, ReviewStatus.REVIEWED],
        },
    )

    page = response.content.decode()
    assert first.content_text in page
    assert second.content_text not in page
    assert "不应命中" not in page
    assert 'class="is-selected">筛选游戏 A</a>' in page

    invalid_page = client.get("/?appid=not-managed").content.decode()
    assert "多选命中 A" not in invalid_page
    assert "多选命中 B" not in invalid_page


@pytest.mark.django_db
def test_dashboard_rejects_unknown_block_reason(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", False)
    user = get_user_model().objects.create_user(
        username="reason-operator", password="test-password"
    )
    post = OriginalPost.objects.create(topic_id=103, content_id="content-103")
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/BLOCK/",
        {"reason": "未知原因", "block_reason": "32", "confirmed": "yes"},
    )

    assert response.status_code == 302
    assert not post.review_task.operation_audits.exists()


@pytest.mark.django_db
def test_dashboard_refresh_updates_post_content_status_and_metrics(monkeypatch):
    user = get_user_model().objects.create_user(
        username="refresh-operator", password="test-password"
    )
    post = OriginalPost.objects.create(
        topic_id=104,
        content_id="content-104",
        content_text="旧正文",
        source_appid="wx-refresh",
        status=8,
    )
    CandidatePost.objects.create(
        topic_id=104,
        content_id="content-104",
        source_appid="wx-refresh",
        origin_type=OriginType.ORIGINAL,
    )

    class Source:
        def __init__(self, **kwargs):
            self.closed = False

        def get_content_card(self, content_id):
            assert content_id == "content-104"
            return {
                "data": {
                    "content_card": {
                        "base_content": {"content": "<p>新正文</p>", "status": 2},
                        "reply_info": {"total_reply_count": 5},
                        "love_info": {"total_love_count": 4},
                        "share_info": {"total_share_count": 3},
                    }
                }
            }

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"view_count": 12, "reply_count": 5, "love_count": 4}}}

        def close(self):
            self.closed = True

    monkeypatch.setattr("posts.views.CdpGameCircleClient", Source)
    client = Client()
    client.force_login(user)

    response = client.post(f"/posts/{post.topic_id}/refresh/")

    assert response.status_code == 302
    post.refresh_from_db()
    snapshot = post.interaction_snapshots.order_by("-captured_at").first()
    assert post.content_text == "新正文"
    assert post.status == 2
    assert snapshot.view_count == 12
    assert snapshot.love_count == 4
    assert snapshot.reply_count == 5


@pytest.mark.django_db
def test_dashboard_batch_executes_only_eligible_ai_sink_and_block_recommendations(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    monkeypatch.setattr(runtime, "enable_block_writes", True)
    user = get_user_model().objects.create_user(
        username="batch-operator", password="test-password"
    )
    appid = "wx6d2454a8afcd9706"

    def create_post(topic_id, action, *, status=2, with_evidence=True):
        post = OriginalPost.objects.create(
            topic_id=topic_id,
            content_id=f"batch-{topic_id}",
            source_appid=appid,
            content_text=f"帖子 {topic_id}",
            status=status,
        )
        CandidatePost.objects.create(
            topic_id=topic_id,
            content_id=post.content_id,
            source_appid=appid,
            origin_type=OriginType.ORIGINAL,
        )
        risks = (
            [RiskOutput(label="TRAFFIC_DIVERSION", confidence=0.91, evidence=[f"联系方式 {topic_id}"])]
            if with_evidence
            else []
        )
        _, task = persist_assessment(
            post,
            AssessmentOutput(
                content_category="OTHER",
                recommended_action=action,
                overall_confidence=0.91,
                risk_labels=risks,
            ),
        )
        return post, task

    sink_post, sink_task = create_post(1201, RecommendedAction.SINK)
    block_post, block_task = create_post(1202, RecommendedAction.BLOCK)
    mute_post, mute_task = create_post(1203, RecommendedAction.MUTE)
    human_post, human_task = create_post(1204, RecommendedAction.HUMAN_REVIEW)
    no_evidence_post, no_evidence_task = create_post(
        1205, RecommendedAction.BLOCK, with_evidence=False
    )
    non_normal_post, non_normal_task = create_post(
        1206, RecommendedAction.SINK, status=POST_STATUS_SUNK
    )

    class Source:
        operations = []

        def __init__(self, **kwargs):
            self.statuses = {
                sink_post.topic_id: 2,
                block_post.topic_id: 2,
            }

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": self.statuses[topic_id]}}}

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            self.operations.append((topic_id, action, payload))
            self.statuses[topic_id] = 8 if action == RecommendedAction.SINK else 4
            return {"errcode": 0, "errmsg": "ok"}

        def close(self):
            pass

    monkeypatch.setattr("posts.views.CdpGameCircleClient", Source)
    client = Client()
    client.force_login(user)
    preview = client.get("/", {"appid": appid}).content.decode()
    assert 'id="batch-operation-modal"' in preview
    assert "确认执行 AI 建议" in preview
    assert "本次处置预览" in preview
    assert f"#{sink_post.topic_id}" in preview
    assert f"#{block_post.topic_id}" in preview
    assert "引流/外部联系方式：联系方式 1201" in preview
    assert 'data-batch-open' in preview
    response = client.post(
        "/posts/batch-ai-recommendations/",
        {
            "appid": appid,
            "topic_id": [
                sink_post.topic_id,
                block_post.topic_id,
                mute_post.topic_id,
                human_post.topic_id,
                no_evidence_post.topic_id,
                non_normal_post.topic_id,
            ],
            "return_query": f"appid={appid}&sort=priority_desc",
            "selected_topic_id": sink_post.topic_id,
            "batch_key": "batch-20260719",
        },
    )

    assert response.status_code == 302
    assert response.url == f"/?appid={appid}&sort=priority_desc&topic={sink_post.topic_id}"
    assert Source.operations == [
        (sink_post.topic_id, RecommendedAction.SINK, {}),
        (block_post.topic_id, RecommendedAction.BLOCK, {"block_reason_code": 1}),
    ]
    sink_task.refresh_from_db()
    block_task.refresh_from_db()
    assert sink_task.status == ReviewStatus.REVIEWED
    assert block_task.status == ReviewStatus.REVIEWED
    assert "AI 风险依据：引流/外部联系方式：联系方式 1201；执行沉底。" == sink_task.reason
    assert "AI 风险依据：引流/外部联系方式：联系方式 1202；执行屏蔽。" == block_task.reason
    for task in (mute_task, human_task, no_evidence_task, non_normal_task):
        task.refresh_from_db()
        assert task.status == ReviewStatus.PENDING
        assert not task.operation_audits.exists()
