import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils import timezone

from config.context_processors import admin_console_overview
from jobs.models import BackgroundJob, JobStatus
from posts.models import ManagedGame, OriginalPost
from reviews.adapters import AssessmentOutput
from reviews.models import PolicyRule, PolicyVersion, RecommendedAction
from reviews.services import persist_assessment


@pytest.fixture
def admin_client():
    user = get_user_model().objects.create_superuser(
        "admin-console", "admin-console@test.invalid", "password"
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_admin_home_is_configuration_and_traceability_console(admin_client):
    response = admin_client.get("/admin/")

    assert response.status_code == 200
    page = response.content.decode()
    assert "系统配置与追溯台" in page
    assert "内容管理" in page
    assert "运营分析" in page
    assert "核心配置" in page
    assert "数据追溯" in page
    assert "仅此区域允许编辑" in page
    assert "未启用策略" in page


@pytest.mark.django_db
def test_admin_home_shows_local_configuration_and_runtime_overview(admin_client):
    policy = PolicyVersion.objects.create(name="默认审核策略", version="v2026.07", is_active=True)
    PolicyRule.objects.create(
        policy_version=policy,
        risk_label="ADVERTISEMENT_SPAM",
        min_confidence=0.8,
        recommended_action=RecommendedAction.BLOCK,
        priority=80,
    )
    ManagedGame.objects.create(
        appid="wx-admin-overview", name="概览游戏", last_synced_at=timezone.now()
    )
    BackgroundJob.objects.create(
        task_type="INGEST_WINDOW",
        idempotency_key="admin-overview-pending",
        status=JobStatus.PENDING,
    )
    BackgroundJob.objects.create(
        task_type="AI_EVALUATE",
        idempotency_key="admin-overview-failed",
        status=JobStatus.FAILED,
    )
    BackgroundJob.objects.create(
        task_type="OCR_EXTRACT",
        idempotency_key="admin-overview-running",
        status=JobStatus.RUNNING,
    )

    page = admin_client.get("/admin/").content.decode()

    assert "默认审核策略 · v2026.07" in page
    assert PolicyVersion.objects.filter(is_active=True).count() == 1
    assert PolicyRule.objects.filter(policy_version=policy, enabled=True).count() == 1
    assert "当前启用策略包含 1 条规则。" in page
    assert "当前生效规则" in page
    assert "查看规则" in page
    assert "广告/垃圾内容" in page
    assert "最低置信度" in page
    assert "80%" in page
    assert "屏蔽" in page
    assert "运行健康" in page
    assert "待执行任务" in page
    assert "执行中任务" in page
    assert "请查看脱敏错误摘要" in page
    assert "帖子与互动" in page
    assert "审核与模型" in page


@pytest.mark.django_db
def test_admin_home_warns_when_multiple_policies_are_active(admin_client):
    PolicyVersion.objects.create(name="策略 A", version="v1", is_active=True)
    PolicyVersion.objects.create(name="策略 B", version="v2", is_active=True)

    page = admin_client.get("/admin/").content.decode()

    assert "多个策略同时启用" in page
    assert "检测到 2 个启用版本，请保留唯一生效版本。" in page


@pytest.mark.django_db
def test_admin_console_overview_does_not_query_on_admin_subpages():
    request = RequestFactory().get("/admin/posts/originalpost/")
    request.resolver_match = resolve(request.path)

    with CaptureQueriesContext(connection) as queries:
        context = admin_console_overview(request)

    assert context == {}
    assert len(queries) == 0


@pytest.mark.django_db
def test_original_posts_are_read_only_and_link_to_operations_dashboard(admin_client):
    post = OriginalPost.objects.create(topic_id=911, content_id="admin-trace-post")

    changelist = admin_client.get("/admin/posts/originalpost/")
    detail = admin_client.get(f"/admin/posts/originalpost/{post.pk}/change/")
    add = admin_client.get("/admin/posts/originalpost/add/")
    change = admin_client.post(
        f"/admin/posts/originalpost/{post.pk}/change/",
        {"content_text": "不应写入", "_save": "保存"},
    )

    assert changelist.status_code == 200
    assert 'href="/admin/posts/originalpost/add/"' not in changelist.content.decode()
    assert detail.status_code == 200
    assert f"/?topic={post.topic_id}" in detail.content.decode()
    assert add.status_code == 403
    assert change.status_code == 403
    post.refresh_from_db()
    assert post.content_text == ""


@pytest.mark.django_db
def test_managed_game_only_exposes_controlled_sync_action(admin_client):
    game = ManagedGame.objects.create(appid="wx-admin", name="仅同步目录")

    page = admin_client.get("/admin/posts/managedgame/")

    assert page.status_code == 200
    assert "从受控微信同步可管理游戏" in page.content.decode()
    assert admin_client.get("/admin/posts/managedgame/add/").status_code == 403
    assert admin_client.post(
        f"/admin/posts/managedgame/{game.pk}/change/", {"name": "不应写入", "_save": "保存"}
    ).status_code == 403
    game.refresh_from_db()
    assert game.name == "仅同步目录"


@pytest.mark.django_db
def test_policy_configuration_remains_editable(admin_client):
    version_response = admin_client.post(
        "/admin/reviews/policyversion/add/",
        {"name": "默认策略", "version": "v1", "is_active": "on", "_save": "保存"},
    )
    version = PolicyVersion.objects.get(name="默认策略")
    rule_response = admin_client.post(
        "/admin/reviews/policyrule/add/",
        {
            "policy_version": str(version.pk),
            "risk_label": "ADVERTISEMENT_SPAM",
            "min_confidence": "0.8",
            "recommended_action": RecommendedAction.BLOCK,
            "priority": "80",
            "exceptions": "[]",
            "enabled": "on",
            "_save": "保存",
        },
    )

    assert version_response.status_code == 302
    assert rule_response.status_code == 302
    assert PolicyRule.objects.filter(
        policy_version=version, risk_label="ADVERTISEMENT_SPAM"
    ).exists()
    rule_list = admin_client.get("/admin/reviews/policyrule/").content.decode()
    assert "风险类型（中文）" in rule_list
    assert "广告/垃圾内容" in rule_list


@pytest.mark.django_db
def test_review_task_is_trace_only_and_legacy_operation_urls_are_gone(admin_client):
    post = OriginalPost.objects.create(topic_id=912, content_id="admin-review-post")
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="KEEP", overall_confidence=0.5
        ),
    )

    detail = admin_client.get(f"/admin/reviews/reviewtask/{task.pk}/change/")
    legacy = admin_client.post(
        f"/admin/reviews/reviewtask/{task.pk}/confirm-block/",
        {"reason": "不应执行", "block_reason": "8"},
    )

    page = detail.content.decode()
    assert detail.status_code == 200
    assert f"/?topic={post.topic_id}" in page
    assert "确认屏蔽" not in page
    assert "确认沉底" not in page
    assert "确认禁言" not in page
    assert "恢复正常" not in page
    assert legacy.status_code == 410
    assert not task.operation_audits.exists()
