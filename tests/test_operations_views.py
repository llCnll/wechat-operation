from datetime import timedelta
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from config.settings import runtime
from config.game_circle_ipc import GameCircleIpcError
from jobs.models import BackgroundJob
from posts.game_catalog import set_active_game
from posts.views import _author_mute_presentation
from posts.models import (
    POST_STATUS_BLOCKED,
    POST_STATUS_DELETED,
    POST_STATUS_SELF_VISIBLE,
    POST_STATUS_SUNK,
    AuthorMuteStatus,
    CandidatePost,
    GameCircleLoginRuntimeState,
    InteractionSnapshot,
    ManagedGame,
    OriginalPost,
    OriginType,
)
from reviews.adapters import AssessmentOutput, ReplyOutput, ReplyStyleOutput, RiskOutput
from reviews.models import (
    AiOperationReview,
    AiOperationReviewStatus,
    OperationAudit,
    OperationExecutionSource,
    RecommendedAction,
    ReviewStatus,
    ReviewTask,
    VideoEvidence,
    VideoEvidenceKind,
    VideoExtraction,
)
from reviews.reply_faq import CITY_LORD_APPID
from reviews.services import AI_EVALUATE_TASK, OCR_EXTRACT_TASK, persist_assessment
from reviews.video import media_fingerprint
from reviews.video_tasks import VIDEO_EXTRACTOR_VERSION


def _activate_game(appid: str) -> None:
    game, _ = ManagedGame.objects.get_or_create(appid=appid, defaults={"name": f"测试-{appid}"})
    set_active_game(appid=game.appid)


@pytest.mark.django_db
def test_operations_dashboard_requires_login():
    response = Client().get("/")
    assert response.status_code == 302
    assert "/admin/login/" in response.url


@pytest.mark.django_db
def test_original_post_admin_url_requires_authenticated_trace_access():
    response = Client().get("/admin/posts/originalpost/")
    assert response.status_code == 302
    assert response.url.startswith("/admin/login/")


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
    assert 'class="cdp-mode">会话</span>' in response.content.decode()
    assert "data-cdp-login-status" in response.content.decode()
    assert 'class="cdp-mode">Frame</span>' in response.content.decode()
    assert "data-cdp-frame-status" in response.content.decode()
    assert "status.can_open_login" in response.content.decode()
    assert "status.frame_valid" in response.content.decode()
    assert "status.frame_can_recover" in response.content.decode()
    assert 'fetch("/auth/status"' in response.content.decode()
    assert 'action="/auth/open-login/"' in response.content.decode()
    assert 'action="/admin/runtime/current-game-frame/"' in response.content.decode()
    assert 'data-operation="SKIP"' in response.content.decode()
    assert 'data-operation="UNMUTE"' in response.content.decode()
    assert f"/posts/{post.topic_id}/refresh/" in response.content.decode()


@pytest.mark.django_db
def test_browser_login_status_only_allows_opening_login_page_when_scan_is_required():
    state = GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING,
        frame_state=GameCircleLoginRuntimeState.FrameState.UNREADY,
        available_actions=[GameCircleLoginRuntimeState.Action.OPEN_LOGIN],
            error_category=GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED,
            checked_at=timezone.now(),
            outer_verified_at=timezone.now(),
    )

    scan_required = Client().get("/auth/status").json()

    assert scan_required["valid"] is False
    assert scan_required["can_open_login"] is True
    assert scan_required["state"] == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING
    assert scan_required["frame_valid"] is False

    state.outer_state = GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE
    state.available_actions = []
    state.error_category = GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE
    state.save(update_fields=["outer_state", "available_actions", "error_category", "updated_at"])

    unavailable = Client().get("/auth/status").json()

    assert unavailable["can_open_login"] is False
    assert unavailable["state"] == GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE


@pytest.mark.django_db
def test_browser_login_status_keeps_a_fresh_login_required_state_visible_without_success_timestamp():
    GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED,
        frame_state=GameCircleLoginRuntimeState.FrameState.UNREADY,
        available_actions=[GameCircleLoginRuntimeState.Action.OPEN_LOGIN],
        error_category=GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED,
        checked_at=timezone.now(),
        outer_verified_at=None,
    )

    status = Client().get("/auth/status").json()

    assert status["state"] == GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED
    assert status["can_open_login"] is True
    assert status["reason"] == "微信登录态已失效，请重新扫码"


@pytest.mark.django_db
def test_browser_login_status_reports_outer_session_and_current_game_frame_separately():
    GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.OUTER_READY,
        frame_state=GameCircleLoginRuntimeState.FrameState.READY,
            error_category=GameCircleLoginRuntimeState.ErrorCategory.NONE,
            checked_at=timezone.now(),
            outer_verified_at=timezone.now(),
    )
    status = Client().get("/auth/status").json()

    assert status["valid"] is True
    assert status["frame_valid"] is True
    assert status["frame_state"] == "READY"
    assert status["frame_can_recover"] is False


@pytest.mark.django_db
def test_dashboard_explicitly_marks_missing_author_mute_observation_as_unknown():
    user = get_user_model().objects.create_user(username="mute-unknown", password="test-password")
    OriginalPost.objects.create(topic_id=98, content_id="mute-unknown", content_text="待审核")
    client = Client()
    client.force_login(user)

    response = client.get("/?topic=98")

    assert response.status_code == 200
    assert "作者禁言状态：状态未知（尚未同步）" in response.content.decode()
    assert "author-mute-status is-unknown" in response.content.decode()


@pytest.mark.django_db
def test_dashboard_displays_only_the_local_author_nickname_projection():
    user = get_user_model().objects.create_user(username="author-display", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=981,
        content_id="author-display",
        source_appid="wx6d2454a8afcd9706",
        content_text="待审核",
        author_nick_name="展示作者",
    )
    client = Client()
    client.force_login(user)

    response = client.get(f"/?topic={post.topic_id}")

    assert response.status_code == 200
    assert response.content.decode().count("展示作者") >= 2
    assert "作者未知" not in response.content.decode()


@pytest.mark.django_db
def test_dashboard_marks_active_author_mute_with_a_distinct_indicator():
    user = get_user_model().objects.create_user(username="mute-active", password="test-password")
    now = timezone.now()
    post = OriginalPost.objects.create(
        topic_id=95,
        content_id="mute-active",
        source_appid="wx6d2454a8afcd9706",
        author_openid="active-author",
    )
    AuthorMuteStatus.objects.create(
        openid="active-author",
        source_appid="wx6d2454a8afcd9706",
        local_muted_until=now + timedelta(days=1),
        last_synced_at=now,
        source_requested_at=now,
    )
    client = Client()
    client.force_login(user)

    response = client.get(f"/?topic={post.topic_id}")

    assert "author-mute-status is-muted" in response.content.decode()


@pytest.mark.django_db
def test_mute_management_filters_by_game_and_links_author_history():
    user = get_user_model().objects.create_user(username="mute-list", password="test-password")
    now = timezone.now()
    active = OriginalPost.objects.create(
        topic_id=96,
        content_id="author-active",
        source_appid="wx6d2454a8afcd9706",
        author_openid="author-a",
    )
    hidden = OriginalPost.objects.create(
        topic_id=97, content_id="author-other", source_appid="other-game", author_openid="author-a"
    )
    status = AuthorMuteStatus.objects.create(
        openid="author-a",
        source_appid="wx6d2454a8afcd9706",
        nick_name="可见作者",
        local_muted_until=now + timedelta(days=1),
        last_synced_at=now,
        source_requested_at=now,
        last_source_post=active,
    )
    AuthorMuteStatus.objects.create(
        openid="author-a",
        source_appid="other-game",
        nick_name="不应出现",
        local_muted_until=now + timedelta(days=1),
        last_synced_at=now,
        source_requested_at=now,
        last_source_post=hidden,
    )
    client = Client()
    client.force_login(user)

    response = client.get("/mute-management/?appid=wx6d2454a8afcd9706&q=可见")
    assert response.status_code == 200
    body = response.content.decode()
    assert "可见作者" in body
    assert "不应出现" not in body
    assert f"/mute-management/{status.pk}/posts/" in body

    history = client.get(f"/mute-management/{status.pk}/posts/?appid=wx6d2454a8afcd9706")
    assert history.status_code == 200
    assert "#96" in history.content.decode()
    assert "#97" not in history.content.decode()


@pytest.mark.django_db
def test_mute_management_filters_scope_and_imminent_expiry():
    user = get_user_model().objects.create_user(username="mute-scope", password="test-password")
    now = timezone.now()
    AuthorMuteStatus.objects.create(
        openid="local-author",
        source_appid="wx6d2454a8afcd9706",
        nick_name="本地禁言",
        local_muted_until=now + timedelta(days=2),
        last_synced_at=now,
        source_requested_at=now,
    )
    AuthorMuteStatus.objects.create(
        openid="global-author",
        source_appid="wx6d2454a8afcd9706",
        nick_name="全局禁言",
        global_muted_until=now + timedelta(days=10),
        last_synced_at=now,
        source_requested_at=now,
    )
    client = Client()
    client.force_login(user)

    local = client.get("/mute-management/?scope=LOCAL&expires=SOON")
    global_mutes = client.get("/mute-management/?scope=GLOBAL")

    assert [status.nick_name for status in local.context["statuses"]] == ["本地禁言"]
    assert [status.nick_name for status in global_mutes.context["statuses"]] == ["全局禁言"]


@pytest.mark.django_db
def test_mute_management_imminent_expiry_uses_the_presented_effective_deadline():
    appid = "mute-expiry-game"
    _activate_game(appid)
    user = get_user_model().objects.create_user(username="mute-expiry", password="test-password")
    now = timezone.now()
    AuthorMuteStatus.objects.create(
        openid="both-but-global-later",
        source_appid=appid,
        nick_name="全局仍有效",
        local_muted_until=now + timedelta(days=2),
        global_muted_until=now + timedelta(days=10),
        last_synced_at=now,
        source_requested_at=now,
    )
    AuthorMuteStatus.objects.create(
        openid="both-imminent",
        source_appid=appid,
        nick_name="都将到期",
        local_muted_until=now + timedelta(days=2),
        global_muted_until=now + timedelta(days=3),
        last_synced_at=now,
        source_requested_at=now,
    )
    client = Client()
    client.force_login(user)

    response = client.get(f"/mute-management/?appid={appid}&expires=SOON")

    assert [status.nick_name for status in response.context["statuses"]] == ["都将到期"]


@pytest.mark.parametrize(
    ("local_offset", "global_offset", "expected_offset"),
    [(30, 1, 30), (1, 30, 30)],
)
def test_author_mute_presentation_uses_the_later_deadline_when_both_scopes_are_active(
    local_offset, global_offset, expected_offset
):
    now = timezone.now()

    presentation = _author_mute_presentation(
        SimpleNamespace(
            local_muted_until=now + timedelta(days=local_offset),
            global_muted_until=now + timedelta(days=global_offset),
        ),
        now=now,
    )

    assert presentation["scope"] == "当前游戏及全局"
    assert presentation["until"] == now + timedelta(days=expected_offset)


@pytest.mark.django_db
@patch("posts.views.GameCircleIpcClient.call")
def test_invalid_cdp_login_control_only_opens_controlled_chrome(mock_call):
    user = get_user_model().objects.create_user(username="cdp-login", password="test-password")
    client = Client()
    client.force_login(user)

    response = client.post("/auth/open-login/", {"return_to": "/reports/"})

    assert response.status_code == 302
    assert response.url == "/reports/"
    assert mock_call.call_args_list == [call("OPEN_CONTROLLED_LOGIN")]


@pytest.mark.django_db
@patch("posts.views.GameCircleIpcClient.call", return_value={"status": "CONTROLLED_SESSION_LOGIN_PENDING"})
def test_login_control_reports_login_wait_without_claiming_sync_validation(mock_call):
    user = get_user_model().objects.create_user(username="cdp-login-ready", password="test-password")
    client = Client()
    client.force_login(user)

    response = client.post("/auth/open-login/", {"return_to": "/admin/"}, follow=True)

    assert response.status_code == 200
    assert "正在等待扫码登录" in response.content.decode()
    assert mock_call.call_args_list == [call("OPEN_CONTROLLED_LOGIN")]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "method", "status", "expected_message"),
    (
        (
            "/admin/runtime/session-check/",
            "CHECK_LOGIN",
            "LOGIN_PENDING",
            "正在等待扫码登录；未执行外层检查。",
        ),
        (
            "/admin/runtime/current-game-frame/",
            "BUILD_CURRENT_GAME_FRAME",
            "LOGIN_PENDING",
            "正在等待扫码登录；未执行当前游戏 Frame 建立。",
        ),
        (
            "/admin/runtime/session-check/",
            "CHECK_LOGIN",
            "CONTROL_NOT_COMPLETED",
            "外层会话当前未就绪，检查未完成；请根据会话状态处理。",
        ),
        (
            "/admin/runtime/current-game-frame/",
            "BUILD_CURRENT_GAME_FRAME",
            "CONTROL_NOT_COMPLETED",
            "当前游戏 Frame 尚未建立；请根据会话状态处理。",
        ),
    ),
)
def test_session_control_reports_uncompleted_outcomes_without_claiming_completion(
    monkeypatch, url, method, status, expected_message
):
    user = get_user_model().objects.create_user(
        username=f"pending-{method}", password="test-password", is_staff=True
    )
    client = Client()
    client.force_login(user)
    calls = []
    monkeypatch.setattr(
        "config.supervisor_views.GameCircleIpcClient.call",
        lambda _self, request_method: calls.append(request_method) or {"status": status},
    )

    response = client.post(url, {"return_to": "/admin/"}, follow=True)

    assert response.status_code == 200
    assert expected_message in response.content.decode()
    assert calls == [method]


@pytest.mark.django_db
def test_operations_dashboard_shows_existing_local_reply_draft_without_publish_controls():
    user = get_user_model().objects.create_user(
        username="reply-draft-operator", password="test-password"
    )
    post = OriginalPost.objects.create(
        topic_id=1099,
        content_id="reply-draft-content",
        content_text="希望增加编队预设",
        source_appid=CITY_LORD_APPID,
    )
    persist_assessment(
        post,
        AssessmentOutput(
            content_category="SUGGESTION",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.91,
            reply_suggestion=ReplyOutput(
                user_intent_summary="希望减少重复配队操作",
                faq_rule_id="feature-request",
                style_variants=[
                    ReplyStyleOutput(style="WARM", draft="感谢建议，我们已记录编队预设需求。"),
                    ReplyStyleOutput(style="CONCISE", draft="已记录编队预设需求。"),
                    ReplyStyleOutput(style="FORMAL", draft="您的编队预设需求已登记参考。"),
                ],
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
    assert "功能/内容期望 · feature-request" in page
    assert "仅供运营准备回复，不会发送或发布到原平台。" in page
    assert "标记本地已采纳" in page
    assert "data-copy-reply-draft" in page
    assert f"/posts/{post.topic_id}/reply-draft/" in page
    assert "/publish/" not in page


@pytest.mark.django_db
def test_operations_dashboard_labels_a_generic_unmatched_reply_draft():
    user = get_user_model().objects.create_user(
        username="generic-reply-draft-operator", password="test-password"
    )
    post = OriginalPost.objects.create(
        topic_id=1102,
        content_id="generic-reply-draft-content",
        content_text="就问你还有多少和我一样的",
        source_appid=CITY_LORD_APPID,
    )
    persist_assessment(
        post,
        AssessmentOutput(
            content_category="SOCIAL",
            recommended_action=RecommendedAction.KEEP,
            overall_confidence=0.9,
            reply_suggestion=ReplyOutput(
                user_intent_summary="社区互动",
                style_variants=[
                    ReplyStyleOutput(style="WARM", draft="感谢分享，欢迎聊聊你的体验。"),
                    ReplyStyleOutput(style="CONCISE", draft="感谢分享，欢迎交流。"),
                    ReplyStyleOutput(style="FORMAL", draft="感谢分享，欢迎在评论区交流体验。"),
                ],
            ),
        ),
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    assert "未命中 FAQ 规则（通用草案）" in page
    assert "参考来源" not in page
    assert "待补充信息" not in page


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
    assert "['BLOCK','MUTE','BLOCK_AND_MUTE'].includes(action)&&input.value===\"1\"" in page


@pytest.mark.django_db
def test_dashboard_renders_quick_filter_for_normal_pending_recommendations():
    user = get_user_model().objects.create_user(username="quick-filter", password="test-password")
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
    page = response.content.decode()
    assert 'id="post-workspace"' in page
    assert 'const postWorkspaceAnchor="#post-workspace"' in page
    assert repaired.status_code == 302
    assert (
        repaired.url == f"/?appid=wx6d2454a8afcd9706&sort=publish_time_desc&topic={first.topic_id}"
    )


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
def test_dashboard_aggregates_current_video_evidence_and_hides_history_by_default():
    user = get_user_model().objects.create_user(username="video-evidence", password="test-password")
    post = OriginalPost.objects.create(topic_id=101, content_id="video-evidence-content")
    media = post.media.create(url="https://example.test/video.mp4", media_type="VIDEO")
    current = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    historical = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version="v1",
    )
    for sequence, text in enumerate(("扫码联系客服", "扫码联系客服！", "搜索群号领取礼包")):
        VideoEvidence.objects.create(
            extraction=current,
            kind=VideoEvidenceKind.FRAME_OCR,
            start_ms=sequence * 250,
            sequence=sequence,
            text=text,
            confidence=0.9,
        )
    VideoEvidence.objects.create(
        extraction=historical,
        kind=VideoEvidenceKind.FRAME_OCR,
        start_ms=0,
        text="历史逐帧证据",
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    assert "视频帧文字汇总" in page
    assert "查看原始 3 帧 OCR" in page
    assert "另有 1 个历史提取记录，未用于当前 AI 审核。" in page
    assert "历史逐帧证据" not in page


@pytest.mark.django_db
def test_dashboard_explains_duration_limited_video_with_historical_snapshot():
    user = get_user_model().objects.create_user(username="duration-limit", password="test-password")
    post = OriginalPost.objects.create(topic_id=1011, content_id="duration-limited-video")
    media = post.media.create(url="https://example.test/too-long.mp4", media_type="VIDEO")
    VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        status="SKIPPED_LIMIT",
        error_summary="video_duration_limit_exceeded",
        duration_ms=31_000,
        duration_limit_seconds=30,
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    assert "视频时长 31 秒，超过提取时资源与采样时长上限 30 秒，未进行提取。" in page
    assert "未生成可用视频证据；请人工核验。" not in page


@pytest.mark.django_db
def test_dashboard_explains_distributed_video_sampling_with_historical_budget():
    user = get_user_model().objects.create_user(
        username="distributed-sampling", password="test-password"
    )
    post = OriginalPost.objects.create(topic_id=1012, content_id="distributed-video")
    media = post.media.create(url="https://example.test/distributed.mp4", media_type="VIDEO")
    VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        status="PARTIAL",
        error_summary="video_duration_distributed_sampling;frame_decode_failed",
        duration_ms=90_000,
        duration_limit_seconds=60,
        sampled_intervals_ms=[[0, 20_000], [35_000, 55_000], [70_000, 90_000]],
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    explanation = (
        "视频时长 90 秒，提取时采样预算 60 秒；已覆盖 "
        "0:00–0:20、0:35–0:55、1:10–1:30，未覆盖其他时间，未执行 ASR。"
    )
    assert explanation in page
    assert "video_duration_distributed_sampling" not in page
    assert "frame_decode_failed" in page


@pytest.mark.django_db
def test_dashboard_keeps_distributed_sampling_code_when_its_range_is_missing():
    user = get_user_model().objects.create_user(
        username="missing-sampling-range", password="test-password"
    )
    post = OriginalPost.objects.create(topic_id=1013, content_id="missing-sampling-range")
    media = post.media.create(url="https://example.test/missing-range.mp4", media_type="VIDEO")
    VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        status="PARTIAL",
        error_summary="video_duration_distributed_sampling",
        duration_ms=90_000,
        duration_limit_seconds=60,
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"topic": post.topic_id}).content.decode()

    assert "video_duration_distributed_sampling" in page
    assert "未覆盖其他时间，未执行 ASR。" not in page


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
    user = get_user_model().objects.create_user(
        username="post-status-filter", password="test-password"
    )
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
def test_dashboard_displays_and_filters_self_visible_posts():
    user = get_user_model().objects.create_user(username="self-visible", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=1009,
        content_id="status-self-visible",
        content_text="仅自己可见状态",
        status=POST_STATUS_SELF_VISIBLE,
    )
    client = Client()
    client.force_login(user)

    page = client.get("/", {"post_status": ["SELF_VISIBLE"]}).content.decode()

    assert post.content_text in page
    assert "当前状态：仅自己可见" in page
    assert 'name="post_status" value="SELF_VISIBLE" checked' in page


@pytest.mark.django_db
def test_dashboard_rejects_upstream_operation_for_self_visible_post(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_block_writes", True)
    user = get_user_model().objects.create_user(
        username="self-visible-action", password="test-password"
    )
    post = OriginalPost.objects.create(
        topic_id=1010,
        content_id="self-visible-action-content",
        status=POST_STATUS_SELF_VISIBLE,
        source_appid="wx-game",
    )

    class Source:
        def __init__(self, **kwargs):
            raise AssertionError("仅自己可见帖子不能建立上游处置会话")

    monkeypatch.setattr("posts.views.GameCirclePostClient", Source)
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/BLOCK/",
        {"reason": "广告", "block_reason": "8", "confirmed": "yes"},
    )

    assert response.status_code == 302
    assert not ReviewTask.objects.filter(post=post).exists()


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
    user = get_user_model().objects.create_user(username="deleted-action", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=1002,
        content_id="deleted-action-content",
        status=POST_STATUS_DELETED | POST_STATUS_SUNK,
        source_appid="wx-game",
    )

    class Source:
        def __init__(self, **kwargs):
            raise AssertionError("业务删除帖子不能建立上游处置会话")

    monkeypatch.setattr("posts.views.GameCirclePostClient", Source)
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/BLOCK/",
        {"reason": "广告", "block_reason": "8", "confirmed": "yes"},
    )

    assert response.status_code == 302
    assert not ReviewTask.objects.filter(post=post).exists()


@pytest.mark.django_db
def test_dashboard_allows_deleted_post_author_unmute_without_post_read(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_unmute_writes", True)
    user = get_user_model().objects.create_user(username="deleted-author-action")
    post = OriginalPost.objects.create(
        topic_id=1003,
        content_id="deleted-author-action-content",
        status=POST_STATUS_DELETED,
        source_appid="wx-game",
        author_openid="deleted-author",
    )
    source = object()
    executed = []

    @contextmanager
    def source_session(_post):
        yield source

    def execute(*args, **kwargs):
        executed.append(kwargs)
        assert kwargs["action"] == RecommendedAction.UNMUTE
        assert kwargs["source"] is source
        assert kwargs["upstream_payload"] == {"author_openid": "deleted-author"}
        return SimpleNamespace(result_code="OK")

    monkeypatch.setattr("posts.views._source_for_post", source_session)
    monkeypatch.setattr("posts.views.execute_confirmed_operation", execute)
    client = Client()
    client.force_login(user)

    page = client.get(f"/?appid={post.source_appid}&topic={post.topic_id}").content.decode()
    assert 'data-operation="MUTE"' in page
    assert 'data-operation="UNMUTE"' in page

    response = client.post(
        f"/posts/{post.topic_id}/operate/UNMUTE/",
        {"reason": "作者状态核验后解除禁言", "confirmation_key": "deleted-author-unmute"},
    )

    assert response.status_code == 302
    assert len(executed) == 1


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
    _activate_game(post.source_appid)
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

    monkeypatch.setattr("posts.views.GameCirclePostClient", Source)

    @contextmanager
    def source_session(_appid):
        yield Source()

    monkeypatch.setattr("posts.views._source_for_active_game", source_session)
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
def test_dashboard_operation_reports_session_recovery_without_completing_review(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    user = get_user_model().objects.create_user(username="session-recovery-operator")
    post = OriginalPost.objects.create(
        topic_id=1051,
        content_id="session-recovery-content",
        source_appid="wx-session-recovery",
        status=2,
    )
    _activate_game(post.source_appid)
    CandidatePost.objects.create(
        topic_id=post.topic_id,
        content_id=post.content_id,
        source_appid=post.source_appid,
        origin_type=OriginType.ORIGINAL,
    )

    class Source:
        def __init__(self, **_kwargs):
            pass

        def get_post_metrics(self, _topic_id):
            raise GameCircleIpcError("SESSION_RECOVERING", "游戏圈会话正在恢复，请稍后重新确认")

        def close(self):
            pass

    monkeypatch.setattr("posts.views.GameCirclePostClient", Source)
    client = Client()
    client.force_login(user)

    response = client.post(
        f"/posts/{post.topic_id}/operate/SINK/",
        {"reason": "无价值灌水", "confirmed": "yes", "confirmation_key": "session-recovery"},
        follow=True,
    )

    assert "游戏圈会话正在恢复，请稍后重新确认。" in response.content.decode()
    task = post.review_task
    assert task.status == ReviewStatus.PENDING
    assert task.operation_audits.get().result_code == "SESSION_RECOVERING"


@pytest.mark.django_db
def test_escalated_ai_operation_can_restore_and_apply_final_action_in_one_flow(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_restore_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    user = get_user_model().objects.create_user(username="escalation-correction")
    post = OriginalPost.objects.create(
        topic_id=110,
        content_id="escalation-correction-content",
        source_appid="wx-escalation-correction",
        status=POST_STATUS_BLOCKED,
    )
    _activate_game(post.source_appid)
    CandidatePost.objects.create(
        topic_id=post.topic_id,
        content_id=post.content_id,
        source_appid=post.source_appid,
        origin_type=OriginType.ORIGINAL,
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER",
            recommended_action=RecommendedAction.BLOCK,
            overall_confidence=0.99,
        ),
    )
    task.status = ReviewStatus.AUTO_REVIEW_ESCALATED
    task.save(update_fields=["status", "updated_at"])
    ai_audit = OperationAudit.objects.create(
        task=task,
        operation_id="ai-escalation-original",
        idempotency_key="ai-escalation-original",
        action=RecommendedAction.BLOCK,
        reason="AI 自动屏蔽",
        result_code="OK",
        execution_source=OperationExecutionSource.AI_AUTOMATED,
    )
    ai_review = AiOperationReview.objects.create(
        audit=ai_audit,
        status=AiOperationReviewStatus.ESCALATED,
        reviewer=user,
    )

    class Source:
        def __init__(self, **_kwargs):
            self.status = POST_STATUS_BLOCKED
            self.operations = []

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": self.status}}}

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            assert topic_id == post.topic_id
            self.operations.append((action, payload))
            self.status = 2 if action == RecommendedAction.RESTORE else POST_STATUS_SUNK
            return {"errcode": 0}

        def close(self):
            pass

    source = Source()
    monkeypatch.setattr("posts.views.GameCirclePostClient", lambda **_kwargs: source)

    @contextmanager
    def source_session(_appid):
        yield source

    monkeypatch.setattr("posts.views._source_for_active_game", source_session)
    client = Client()
    client.force_login(user)

    page = client.get(f"/?appid={post.source_appid}&topic={post.topic_id}")
    body = page.content.decode()
    assert "纠正 AI 操作" in body
    assert "恢复后沉底" in body

    response = client.post(
        f"/posts/{post.topic_id}/operate/SINK/",
        {"reason": "应降级处理", "confirmation_key": "escalation-correction-sink"},
    )

    assert response.status_code == 302
    assert source.operations == [(RecommendedAction.RESTORE, {}), (RecommendedAction.SINK, {})]
    task.refresh_from_db()
    ai_review.refresh_from_db()
    post.refresh_from_db()
    assert task.status == ReviewStatus.REVIEWED
    assert task.final_action == RecommendedAction.SINK
    assert ai_review.status == AiOperationReviewStatus.RESTORED
    assert post.status == POST_STATUS_SUNK
    assert list(task.operation_audits.exclude(pk=ai_audit.pk).values_list("action", flat=True)) == [
        RecommendedAction.RESTORE,
        RecommendedAction.SINK,
    ]


@pytest.mark.django_db
def test_dashboard_partial_combined_operation_keeps_human_review_pending():
    user = get_user_model().objects.create_user(
        username="combined-pending", password="test-password"
    )
    post = OriginalPost.objects.create(
        topic_id=108, content_id="combined-pending-content", status=2
    )
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
    user = get_user_model().objects.create_user(
        username="deleted-requeue", password="test-password"
    )
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
        110,
        "wx-filter-b",
        "多选命中 B",
        "FEEDBACK",
        "TRAFFIC_DIVERSION",
        "KEEP",
        ReviewStatus.REVIEWED,
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
    _activate_game("wx-filter-a")
    default_page = client.get("/").content.decode()
    assert "多选命中 A" in default_page
    assert "多选命中 B" not in default_page
    assert 'id="active-game"' in default_page
    assert 'name="appid" value="wx-filter-a"' in default_page

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
def test_dashboard_refresh_updates_only_post_status_and_metrics(monkeypatch):
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
    _activate_game(post.source_appid)
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
            raise AssertionError("轻量刷新不得拉取内容卡")

        def get_post_metrics(self, topic_id):
            assert topic_id == 104
            return {
                "data": {
                    "topic": {
                        "view_count": 12,
                        "reply_count": 5,
                        "love_count": 4,
                        "share_count": 3,
                        "status": 2,
                    }
                }
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr("posts.views.GameCirclePostClient", Source)

    @contextmanager
    def source_session(_appid):
        yield Source()

    monkeypatch.setattr("posts.views._source_for_active_game", source_session)
    client = Client()
    client.force_login(user)

    response = client.post(f"/posts/{post.topic_id}/refresh/")

    assert response.status_code == 302
    post.refresh_from_db()
    snapshot = post.interaction_snapshots.order_by("-captured_at").first()
    assert post.content_text == "旧正文"
    assert post.status == 2
    assert snapshot.view_count == 12
    assert snapshot.love_count == 4
    assert snapshot.reply_count == 5
    assert snapshot.share_count == 3
    assert not BackgroundJob.objects.filter(task_type=AI_EVALUATE_TASK).exists()


@pytest.mark.django_db
def test_dashboard_batch_executes_only_eligible_ai_sink_and_block_recommendations(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_sink_writes", True)
    monkeypatch.setattr(runtime, "enable_block_writes", True)
    user = get_user_model().objects.create_user(username="batch-operator", password="test-password")
    appid = "wx6d2454a8afcd9706"
    _activate_game(appid)

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
            [
                RiskOutput(
                    label="TRAFFIC_DIVERSION", confidence=0.91, evidence=[f"联系方式 {topic_id}"]
                )
            ]
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
    already_blocked_post, already_blocked_task = create_post(1207, RecommendedAction.BLOCK)

    class Source:
        operations = []

        def __init__(self, **kwargs):
            self.statuses = {
                sink_post.topic_id: 2,
                block_post.topic_id: 2,
                already_blocked_post.topic_id: 4,
            }

        def get_post_metrics(self, topic_id):
            return {"data": {"topic": {"topic_id": topic_id, "status": self.statuses[topic_id]}}}

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            self.operations.append((topic_id, action, payload))
            self.statuses[topic_id] = 8 if action == RecommendedAction.SINK else 4
            return {"errcode": 0, "errmsg": "ok"}

        def close(self):
            pass

    monkeypatch.setattr("posts.views.GameCirclePostClient", Source)

    @contextmanager
    def source_session(_appid):
        yield Source()

    monkeypatch.setattr("posts.views._source_for_active_game", source_session)
    client = Client()
    client.force_login(user)
    preview = client.get("/", {"appid": appid}).content.decode()
    assert 'id="batch-operation-modal"' in preview
    assert "确认执行 AI 建议" in preview
    assert "本次处置预览" in preview
    assert f"#{sink_post.topic_id}" in preview
    assert f"#{block_post.topic_id}" in preview
    assert "引流/外部联系方式：联系方式 1201" in preview
    assert "data-batch-open" in preview
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
                already_blocked_post.topic_id,
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
    already_blocked_task.refresh_from_db()
    assert sink_task.status == ReviewStatus.REVIEWED
    assert block_task.status == ReviewStatus.REVIEWED
    assert already_blocked_task.status == ReviewStatus.REVIEWED
    assert "AI 风险依据：引流/外部联系方式：联系方式 1201；执行沉底。" == sink_task.reason
    assert "AI 风险依据：引流/外部联系方式：联系方式 1202；执行屏蔽。" == block_task.reason
    for task in (mute_task, human_task, no_evidence_task, non_normal_task):
        task.refresh_from_db()
        assert task.status == ReviewStatus.PENDING
        assert not task.operation_audits.exists()
