from types import SimpleNamespace
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils import timezone

from config.context_processors import MANAGED_SERVICE_NAMES, _service_reason, admin_console_overview
from config.supervisor import SupervisorUnavailableError
from jobs.models import (
    BackgroundJob,
    JobStatus,
    ScheduledProducerConfig,
    ScheduledProducerRuleConfig,
)
from jobs.scheduled_producers import delete_rule_configuration
from posts.game_catalog import set_active_game
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
    assert "运行概览" in page
    assert "按运行流程" in page
    assert "按服务管理" in page
    assert "console-lucide" in page
    assert 'data-operations-theme-toggle' in page
    assert 'aria-label="切换至深色配色"' in page
    assert "console-hero-mark" not in page
    assert "当前范围" in page
    assert "受控 Chrome" in page
    assert "游戏圈会话服务" in page
    assert "外层会话" in page
    assert "保活当前游戏 Frame" in page
    assert 'class="console-session-maintenance"' in page
    assert "自动修复当前游戏 Frame" in page
    assert "检查外层会话" in page
    assert "更换操作游戏" in page
    assert "刷新可管理游戏目录" in page
    assert 'action="/admin/runtime/game-catalog/sync/"' in page
    assert "受控同步" not in page
    active_start = page.index(">当前操作游戏<")
    active_end = page.index('id="service-foundation"', active_start)
    active_node = page[active_start:active_end]
    assert 'action="/admin/runtime/active-game/"' in active_node
    assert 'action="/admin/runtime/game-catalog/sync/"' in active_node
    assert 'action="/admin/runtime/session-keepalive/"' not in active_node
    assert 'action="/admin/runtime/session-check/"' not in active_node
    assert "会话自动维护" not in active_node
    assert "自动调度器" in page
    assert "帖子采集 Worker" in page
    assert "指标快照 Worker" in page
    assert "AI 审核 Worker" in page
    assert "配置 AI 审核" in page
    assert "配置 OCR" in page
    assert "配置视频审核" in page
    assert (
        "/admin/runtime/services/video_extraction/start/" in page
        or "/admin/runtime/services/video_extraction/restart/" in page
    )
    assert "人工处置授权" in page
    assert 'data-config-dialog="ai"' in page
    assert 'data-config-dialog-panel="ai"' in page
    assert 'data-config-dialog-panel="ocr"' in page
    assert 'data-config-dialog-panel="writes"' in page
    assert "配置 AI、OCR 与人工处置授权" not in page
    assert "生命周期" in page
    assert "业务条件" in page
    assert "条件缺失时" in page
    assert "当前状态" in page
    assert "下一步：" in page
    assert 'data-runtime-service-link' in page
    assert 'class="console-subservice-log-button"' in page
    assert "自动化任务" in page
    assert "新增规则" in page
    assert 'class="console-producer-add-card"' in page
    assert "任务类型" in page
    assert "执行频率" in page
    assert "未来三次触发" in page
    assert "下次触发" in page
    assert 'aria-label="查看下次触发"' not in page
    assert 'name="timezone_name"' not in page
    assert "AUTOMATED PRODUCTION" not in page
    assert 'class="console-section-surface console-producer-grid"' in page
    assert 'class="console-section-surface console-configuration-surface"' in page
    assert 'class="console-section-surface console-health-grid"' in page
    assert 'class="console-section-surface console-trace-grid"' in page


def test_runtime_console_feedback_bounds_its_icon_size():
    stylesheet = Path("posts/static/posts/admin_console.css").read_text(encoding="utf-8")

    assert ".console-feedback-icon svg" in stylesheet
    assert ".console-feedback-icon { display:grid; flex:none; width:20px; height:20px;" in stylesheet
    assert ".console-subservice-log-button { display:inline-flex;" in stylesheet
    assert "The lower console is one continuous system" in stylesheet
    assert 'data-console-theme="dark"] .console-producer-card' in stylesheet
    assert 'data-console-theme="dark"] .console-runtime-tabs' in stylesheet
    assert 'data-console-theme="dark"] :is(.console-config-dialog,.console-log-dialog)::backdrop' in stylesheet
    assert ".console-session-maintenance { flex-basis:100%;" in stylesheet
    assert ".console-section-surface { box-sizing:border-box;" in stylesheet
    assert 'data-console-theme="dark"] .console-section-surface' in stylesheet
    assert ".configuration-dashboard .console-section { margin-top:28px;" in stylesheet
    assert 'data-console-theme="dark"] .console-section {' in stylesheet


def test_runtime_flow_service_link_switches_to_the_service_view():
    base_template = Path("templates/admin/base_site.html").read_text(encoding="utf-8")

    assert "[data-runtime-service-link]" in base_template
    assert 'document.getElementById(`runtime-${view}-tab`)' in base_template
    assert "target.scrollIntoView" in base_template
    assert 'operations-console-runtime-view' in base_template
    assert 'activateRuntimeView("services", true)' in base_template
    assert 'operations-console-runtime-scroll' in base_template
    assert 'window.scrollTo(0, scrollTop)' in base_template


@pytest.mark.django_db
def test_admin_can_add_a_time_point_rule_instance(admin_client):
    producer = ScheduledProducerConfig.objects.get(producer_key="metric_weekly")

    response = admin_client.post(
        f"/admin/scheduled-producers/{producer.pk}/rules/",
        {
            "return_to": "/admin/",
            "rule_type": "afternoon-recheck",
            "schedule_time": "18:00",
            "enabled": "true",
        },
        follow=True,
    )

    assert response.status_code == 200
    assert "已新增周指标·每周二 18:00" in response.content.decode()
    assert ScheduledProducerRuleConfig.objects.filter(
        producer=producer,
        rule_type="afternoon-recheck",
        schedule_expression="0 18 * * 2",
    ).exists()


@pytest.mark.django_db
def test_admin_hides_archived_rule_instances(admin_client):
    rule = ScheduledProducerRuleConfig.objects.get(producer__producer_key="metric_daily")

    delete_rule_configuration(rule_id=rule.pk)
    page = admin_client.get("/admin/").content.decode()

    assert "暂无启用规则" in page
    assert 'action="/admin/scheduled-producer-rules/%s/delete/"' % rule.pk not in page


@pytest.mark.django_db
def test_runtime_console_flow_follows_the_operational_dependency_order(admin_client):
    response = admin_client.get("/admin/")

    flow = admin_console_overview(response.wsgi_request)["admin_console"]["runtime_console"]["flow"]

    assert [node["key"] for node in flow] == [
        "console",
        "game-circle-connection",
        "active-game-scope",
        "current-game-frame",
        "audit-configuration",
        "core-workers",
    ]
    assert sum(node["recommended"] for node in flow) == 1


@pytest.mark.django_db
def test_admin_pipeline_does_not_misreport_chrome_login_when_session_service_is_stopped(
    admin_client, monkeypatch
):
    def stopped_session_service(_self):
        return {
            "web": {"running": True, "desired": True},
            "controlled_chrome": {"running": True, "desired": True},
            "game_circle_session": {"running": False, "desired": False},
        }

    monkeypatch.setattr(
        "config.context_processors.SupervisorClient.status", stopped_session_service
    )

    page = admin_client.get("/admin/").content.decode()

    assert "游戏圈会话服务未运行，状态暂不可用" in page
    assert "打开登录页" not in page


@pytest.mark.django_db
def test_runtime_overview_requires_a_fresh_frame_for_the_current_scope(admin_client, monkeypatch):
    from posts.game_catalog import set_active_game
    from posts.models import GameCircleLoginRuntimeState, ManagedGame

    game = ManagedGame.objects.create(appid="wx-runtime-current", name="当前游戏")
    scope = set_active_game(appid=game.appid)
    monkeypatch.setattr(
        "config.context_processors.SupervisorClient.status",
        lambda _self: {name: {"running": True, "desired": True} for name in MANAGED_SERVICE_NAMES},
    )
    state = GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.OUTER_READY,
        frame_state=GameCircleLoginRuntimeState.FrameState.READY,
        frame_scope_version=scope.scope_version,
        checked_at=timezone.now(),
    )

    overview = admin_console_overview(admin_client.get("/admin/").wsgi_request)["admin_console"]
    assert overview["runtime_console"]["readiness"]["source_ready"] is True

    other = ManagedGame.objects.create(appid="wx-runtime-next", name="下一游戏")
    set_active_game(appid=other.appid)
    overview = admin_console_overview(admin_client.get("/admin/").wsgi_request)["admin_console"]
    assert overview["runtime_console"]["readiness"]["source_ready"] is False

    state.frame_scope_version = 2
    state.checked_at = timezone.now() - timedelta(minutes=11)
    state.save(update_fields=["frame_scope_version", "checked_at"])
    overview = admin_console_overview(admin_client.get("/admin/").wsgi_request)["admin_console"]
    assert overview["runtime_console"]["readiness"]["source_ready"] is False


@pytest.mark.django_db
def test_runtime_console_groups_fixed_services_and_keeps_lifecycle_separate_from_conditions(
    admin_client, monkeypatch
):
    ScheduledProducerRuleConfig.objects.update(enabled=False)
    monkeypatch.setattr(
        "config.context_processors.SupervisorClient.status",
        lambda _self: {name: {"running": True, "desired": True} for name in MANAGED_SERVICE_NAMES},
    )

    console = admin_console_overview(admin_client.get("/admin/").wsgi_request)["admin_console"][
        "runtime_console"
    ]
    groups = {group["key"]: group for group in console["service_groups"]}

    assert list(groups) == [
        "foundation",
        "game-circle-access",
        "core-workers",
        "optional-capabilities",
    ]
    assert [node["key"] for node in groups["game-circle-access"]["nodes"]] == [
        "game-circle-session"
    ]
    scheduler = next(node for node in groups["core-workers"]["nodes"] if node["key"] == "scheduler")
    mute = next(node for node in groups["core-workers"]["nodes"] if node["key"] == "mute-maintenance")
    assert scheduler["lifecycle"].startswith("运行中")
    assert scheduler["business_ready"] is False
    assert "无规则时服务仍运行" in scheduler["missing_behavior"]
    assert "只读状态刷新可直接消费" in mute["business_condition"]


@pytest.mark.django_db
def test_runtime_console_only_renders_fixed_csrf_protected_controls(admin_client):
    page = admin_client.get("/admin/").content.decode()

    assert 'id="runtime-flow-tab"' in page
    assert 'id="runtime-services-tab"' in page
    assert 'action="/admin/runtime/services/controlled_chrome/' not in page
    assert 'action="/admin/runtime/session-check/"' in page
    assert 'action="/admin/runtime/current-game-frame/"' in page
    assert 'name="csrfmiddlewaretoken"' in page
    assert "target_id" not in page
    assert "source_token" not in page


def test_system_configuration_uses_local_lucide_icons_without_emoji_assets():
    icon_template = Path("posts/templates/posts/includes/lucide_icon.html").read_text(
        encoding="utf-8"
    )
    console_template = Path("templates/admin/index.html").read_text(encoding="utf-8")

    assert "<svg" in icon_template
    assert "lucide_icon.html" in console_template
    assert "https://" not in icon_template
    assert "😀" not in console_template


@pytest.mark.django_db
def test_admin_home_degrades_when_supervisor_is_unavailable(admin_client, monkeypatch):
    def unavailable(_self):
        raise SupervisorUnavailableError("Supervisor 未启动或运行态文件不可用")

    monkeypatch.setattr("config.context_processors.SupervisorClient.status", unavailable)

    page = admin_client.get("/admin/").content.decode()

    assert "Supervisor 未启动或运行态文件不可用" in page
    assert "查看基础托管" in page


def test_service_reason_formats_heartbeat_in_local_time():
    reason = _service_reason({"running": True, "heartbeat_at": "2026-07-19T18:12:43.074687+00:00"})

    assert reason == "运行中；最近心跳 2026-07-20 02:12:43"


@pytest.mark.django_db
def test_admin_home_only_offers_restart_and_stop_for_running_worker(admin_client, monkeypatch):
    def running_services(_self):
        return {
            "web": {"running": True, "desired": True},
            "scheduler": {"running": True, "desired": True},
            "ingestion": {"running": False, "desired": False},
            "backfill_ingestion": {"running": False, "desired": False},
            "review": {"running": False, "desired": False},
        }

    monkeypatch.setattr("config.context_processors.SupervisorClient.status", running_services)

    page = admin_client.get("/admin/").content.decode()

    scheduler_start = "/admin/runtime/services/scheduler/start/"
    scheduler_restart = "/admin/runtime/services/scheduler/restart/"
    assert scheduler_start not in page
    assert scheduler_restart in page
    assert "/admin/runtime/services/scheduler/stop/" in page
    assert "/admin/runtime/ingestion/start/" in page
    assert "启动两个 Worker" in page


@pytest.mark.django_db
def test_runtime_control_is_staff_only_and_uses_fixed_proxy(admin_client, monkeypatch):
    calls = []

    def record(_self, service, action):
        calls.append((service, action))
        return {"service": service}

    monkeypatch.setattr("config.supervisor_views.SupervisorClient.control", record)
    anonymous = Client()

    denied = anonymous.post("/admin/runtime/services/review/start/")
    response = admin_client.post("/admin/runtime/services/review/start/", {"return_to": "/admin/"})

    assert denied.status_code == 302
    assert response.status_code == 302
    assert calls == [("review", "start")]


@pytest.mark.django_db
def test_runtime_control_feedback_is_rendered_as_a_dismissible_console_toast(
    admin_client, monkeypatch
):
    monkeypatch.setattr(
        "config.supervisor_views.SupervisorClient.control", lambda _self, _service, _action: {}
    )

    response = admin_client.post(
        "/admin/runtime/services/review/start/", {"return_to": "/admin/"}, follow=True
    )

    assert response.status_code == 200
    page = response.content.decode()
    assert 'class="console-feedback"' in page
    assert "已提交 review 的 start 操作。" in page
    assert "data-console-feedback-dismiss" in page


@pytest.mark.django_db
def test_runtime_log_route_uses_the_read_only_log_proxy(admin_client, monkeypatch):
    monkeypatch.setattr(
        "config.supervisor_views.SupervisorClient.logs",
        lambda _self, service: [f"{service} 日志"],
    )

    response = admin_client.get("/admin/runtime/services/backfill_ingestion/logs/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "backfill_ingestion",
        "lines": ["backfill_ingestion 日志"],
    }


@pytest.mark.django_db
def test_ingestion_control_starts_both_fixed_workers_and_reports_partial_failure(
    admin_client, monkeypatch
):
    calls = []

    def record(_self, service, action):
        calls.append((service, action))
        if service == "backfill_ingestion":
            raise SupervisorUnavailableError("控制通道不可用")
        return {"service": service}

    monkeypatch.setattr("config.supervisor_views.SupervisorClient.control", record)

    response = admin_client.post("/admin/runtime/ingestion/start/", {"return_to": "/admin/"})

    assert response.status_code == 302
    assert calls == [("ingestion", "start"), ("backfill_ingestion", "start")]
    response_messages = " ".join(str(message) for message in get_messages(response.wsgi_request))
    assert "backfill_ingestion" in response_messages


@pytest.mark.django_db
def test_admin_can_enqueue_bounded_manual_ingestion_for_a_managed_game(admin_client, monkeypatch):
    game = ManagedGame.objects.create(appid="wx-manual-ingestion", name="人工补采游戏")
    set_active_game(appid=game.appid)
    captured = {}

    def enqueue(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(created=True, job=SimpleNamespace(pk=123))

    monkeypatch.setattr("config.supervisor_views.enqueue_manual_ingestion_window", enqueue)

    response = admin_client.post(
        "/admin/runtime/ingestion/enqueue/",
        {"return_to": "/admin/", "appid": game.appid, "hours": "12"},
    )

    assert response.status_code == 302
    assert captured["appid"] == game.appid
    assert captured["page_size"] == 20
    assert captured["end_time"] - captured["start_time"] == 12 * 60 * 60
    response_messages = " ".join(str(message) for message in get_messages(response.wsgi_request))
    assert "已创建" in response_messages
    assert resolve("/admin/runtime/ingestion/enqueue/").func.__name__ == "enqueue_manual_ingestion"


@pytest.mark.django_db
def test_admin_rejects_manual_ingestion_outside_the_bounded_hour_range(admin_client):
    response = admin_client.post(
        "/admin/runtime/ingestion/enqueue/",
        {"return_to": "/admin/", "appid": "wx6d2454a8afcd9706", "hours": "745"},
    )

    assert response.status_code == 302
    response_messages = " ".join(str(message) for message in get_messages(response.wsgi_request))
    assert "1 至 744 小时" in response_messages


@pytest.mark.django_db
def test_admin_home_offers_manual_ingestion_with_the_current_game_selected(admin_client):
    game = ManagedGame.objects.create(appid="wx-other-game", name="其他游戏")
    set_active_game(appid=game.appid)

    page = admin_client.get("/admin/").content.decode()

    assert "创建历史补采" in page
    assert 'action="/admin/runtime/ingestion/enqueue/"' in page
    assert 'name="hours" type="number" min="1" max="744" value="12"' in page
    assert 'value="wx-other-game" selected' in page


@pytest.mark.django_db
def test_runtime_configuration_view_turns_missing_write_switches_off(admin_client, monkeypatch):
    saved = {}

    def record(values):
        saved.update(values)
        return {"ENABLE_BLOCK_WRITES"}

    monkeypatch.setattr("config.supervisor_views.update_runtime_config", record)
    monkeypatch.setattr("config.supervisor_views.affected_services", lambda _changed: {"web"})

    response = admin_client.post(
        "/admin/runtime/configuration/",
        {"config_scope": "writes", "AI_MODEL": "test-model", "return_to": "/admin/"},
    )

    assert response.status_code == 302
    assert saved["AI_MODEL"] == "test-model"
    assert saved["ENABLE_BLOCK_WRITES"] == "false"
    assert saved["ENABLE_MUTE_WRITES"] == "false"
    response_messages = " ".join(str(message) for message in get_messages(response.wsgi_request))
    assert "Web 服务" in response_messages
    assert "web" not in response_messages


@pytest.mark.django_db
def test_ai_configuration_does_not_change_write_switches(admin_client, monkeypatch):
    saved = {}

    def record(values):
        saved.update(values)
        return {"AI_MODEL"}

    monkeypatch.setattr("config.supervisor_views.update_runtime_config", record)
    monkeypatch.setattr("config.supervisor_views.affected_services", lambda _changed: {"review"})

    response = admin_client.post(
        "/admin/runtime/configuration/",
        {"config_scope": "ai", "AI_MODEL": "test-model", "return_to": "/admin/"},
    )

    assert response.status_code == 302
    assert saved == {"config_scope": "ai", "AI_MODEL": "test-model", "return_to": "/admin/"}


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
    assert (
        admin_client.post(
            f"/admin/posts/managedgame/{game.pk}/change/", {"name": "不应写入", "_save": "保存"}
        ).status_code
        == 403
    )
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


def test_dark_console_policy_summary_uses_a_dark_surface():
    stylesheet = Path("posts/static/posts/admin_console.css").read_text(encoding="utf-8")

    assert 'body.configuration-dashboard[data-console-theme="dark"] .console-policy-rules-summary { background:linear-gradient(105deg,#162922,#10151d); }' in stylesheet
