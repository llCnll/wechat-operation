"""Small, read-only context used by the system configuration homepage."""

from dataclasses import dataclass
from datetime import timedelta
from importlib.util import find_spec

from django.db.models import Max, Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from config.runtime_config import public_runtime_config
from config.supervisor import SupervisorClient, SupervisorUnavailableError
from reviews.video import media_tools_available

MANAGED_SERVICE_NAMES = (
    "controlled_chrome",
    "web",
    "scheduler",
    "game_circle_session",
    "ingestion",
    "backfill_ingestion",
    "metric_sync",
    "public_opinion_reporting",
    "review",
    "video_extraction",
    "ai_moderation",
    "mute_maintenance",
)
SCHEDULED_PRODUCER_RESULT_LABELS = {
    "NOT_DUE": "尚未到触发时间",
    "MISFIRE_SKIPPED": "错过时点，已跳过",
    "OVERLAP_SKIPPED": "已有任务执行中，已跳过",
    "OVERLAP_HELD": "已有任务执行中，等待合并",
    "NO_ACTIVE_GAME": "未选择当前操作游戏",
    "CONFIG_INVALID": "配置无效",
    "ENQUEUE_FAILED": "自动入队失败",
}


RUNTIME_CONSOLE_GROUPS = (
    ("foundation", "基础托管"),
    ("game-circle-access", "游戏圈访问"),
    ("core-workers", "调度与业务 Worker"),
    ("optional-capabilities", "可选能力"),
)


@dataclass(frozen=True)
class RuntimeConsoleSnapshot:
    """A single, deliberately de-identified view of operational state for one request."""

    checked_at: object
    supervisor_available: bool
    services: dict[str, dict]
    active_scope_exists: bool
    active_scope_name: str
    active_scope_version: int | None
    chrome_running: bool
    session_service_running: bool
    session_fresh: bool
    outer_session_ready: bool
    frame_ready: bool
    ai_ready: bool
    policy_ready: bool
    ocr_ready: bool
    video_ready: bool
    manual_writes_enabled: bool
    ai_moderation_authorized: bool


def _runtime_node(
    key: str,
    label: str,
    *,
    ready: bool,
    reason: str,
    action_label: str,
    anchor: str,
    checked_at,
    purpose: str,
    lifecycle: str = "",
    business_condition: str = "",
    missing_behavior: str = "",
    optional: bool = False,
    service: str = "",
    running: bool = False,
    **extra,
) -> dict:
    return {
        "key": key,
        "label": label,
        "ready": ready,
        "reason": reason,
        "action_label": action_label,
        "anchor": anchor,
        "checked_at": checked_at,
        "purpose": purpose,
        "lifecycle": lifecycle,
        "business_condition": business_condition,
        "missing_behavior": missing_behavior,
        "optional": optional,
        "service": service,
        "running": running,
        **extra,
    }


def _session_reason(snapshot: RuntimeConsoleSnapshot) -> str:
    if not snapshot.chrome_running:
        return "受控 Chrome 未运行"
    if not snapshot.session_service_running:
        return "游戏圈会话服务未运行，状态暂不可用"
    if not snapshot.session_fresh:
        return "会话状态超过十分钟未确认"
    if snapshot.outer_session_ready:
        return "外层会话有效"
    return "外层会话未确认，请检查登录状态"


def _frame_reason(snapshot: RuntimeConsoleSnapshot) -> str:
    if not snapshot.outer_session_ready:
        return "需先恢复受控游戏圈连接"
    if not snapshot.active_scope_exists:
        return "需先选择当前操作游戏"
    if snapshot.frame_ready:
        return "当前范围的来源 Frame 有效"
    return "当前范围尚无已验证的来源 Frame"


def _service_card(
    snapshot: RuntimeConsoleSnapshot,
    key: str,
    label: str,
    purpose: str,
    *,
    service: str = "",
    ready: bool | None = None,
    reason: str = "",
    business_condition: str = "",
    missing_behavior: str = "",
    optional: bool = False,
    **extra,
) -> dict:
    service_state = snapshot.services.get(service, {}) if service else {}
    running = bool(service_state.get("running"))
    lifecycle = _service_reason(service_state) if service else "不适用"
    return _runtime_node(
        key,
        label,
        ready=running if ready is None else ready,
        reason=reason or lifecycle,
        action_label="查看服务管理",
        anchor=f"service-{key}",
        checked_at=snapshot.checked_at,
        purpose=purpose,
        lifecycle=lifecycle,
        business_condition=business_condition,
        missing_behavior=missing_behavior,
        optional=optional,
        service=service,
        running=running,
        **extra,
    )


def _build_runtime_console(
    *,
    services: dict[str, dict],
    supervisor_error: str,
    active_scope,
    login_state,
    ai_ready: bool,
    policy_ready: bool,
    policy_status: dict,
    ocr_ready: bool,
    video_ready: bool,
    manual_writes_enabled: bool,
    ai_moderation_authorized: bool,
    ingestion_subservices: tuple[dict, ...],
    scheduled_rule_ready: bool,
) -> dict:
    """Collect one safe runtime snapshot and derive all console-facing projections from it."""
    from posts.models import GameCircleLoginRuntimeState

    checked_at = timezone.now()
    # A recent negative observation (for example LOGIN_REQUIRED) is still
    # fresh state.  Successful verification time is intentionally separate
    # and must not make the console conceal a confirmed scan requirement.
    session_fresh = bool(
        login_state
        and login_state.checked_at
        and login_state.checked_at >= checked_at - timedelta(minutes=10)
    )
    chrome_running = bool(services["controlled_chrome"].get("running"))
    session_service_running = bool(services["game_circle_session"].get("running"))
    outer_session_ready = bool(
        chrome_running
        and session_service_running
        and session_fresh
        and login_state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    )
    active_scope_version = active_scope.scope_version if active_scope else None
    frame_ready = bool(
        outer_session_ready
        and active_scope_version is not None
        and login_state.frame_state == GameCircleLoginRuntimeState.FrameState.READY
        and login_state.frame_scope_version == active_scope_version
    )
    snapshot = RuntimeConsoleSnapshot(
        checked_at=checked_at,
        supervisor_available=not supervisor_error,
        services=services,
        active_scope_exists=active_scope is not None,
        active_scope_name=active_scope.active_game.name if active_scope else "",
        active_scope_version=active_scope_version,
        chrome_running=chrome_running,
        session_service_running=session_service_running,
        session_fresh=session_fresh,
        outer_session_ready=outer_session_ready,
        frame_ready=frame_ready,
        ai_ready=ai_ready,
        policy_ready=policy_ready,
        ocr_ready=ocr_ready,
        video_ready=video_ready,
        manual_writes_enabled=manual_writes_enabled,
        ai_moderation_authorized=ai_moderation_authorized,
    )

    console_ready = bool(snapshot.supervisor_available and services["web"].get("running"))
    audit_configuration_ready = bool(snapshot.ai_ready and snapshot.policy_ready)
    ingestion_running = all(item["running"] for item in ingestion_subservices)
    core_workers_ready = bool(
        services["scheduler"].get("running")
        and ingestion_running
        and services["review"].get("running")
    )
    source_ready = snapshot.frame_ready
    core_review_ready = bool(source_ready and audit_configuration_ready and core_workers_ready)

    flow = (
        _runtime_node(
            "console",
            "控制台与 Supervisor",
            ready=console_ready,
            reason="控制台与固定控制通道可用" if console_ready else (supervisor_error or "Web 服务未运行"),
            action_label="查看基础托管",
            anchor="service-foundation",
            checked_at=checked_at,
            purpose="提供本地运行状态、固定服务控制与只读诊断。",
        ),
        _runtime_node(
            "game-circle-connection",
            "受控游戏圈连接",
            ready=snapshot.outer_session_ready,
            reason=_session_reason(snapshot),
            action_label="处理游戏圈连接",
            anchor="service-game-circle-access",
            checked_at=checked_at,
            purpose="确认受控 Chrome、会话服务与微信外层会话。",
        ),
        _runtime_node(
            "active-game-scope",
            "当前操作游戏",
            ready=snapshot.active_scope_exists,
            reason=(f"当前：{snapshot.active_scope_name}" if snapshot.active_scope_exists else "尚未选择当前操作游戏"),
            action_label="选择操作游戏",
            anchor="work-scope-title",
            checked_at=checked_at,
            purpose="确定采集、指标和人工操作所使用的业务范围。",
        ),
        _runtime_node(
            "current-game-frame",
            "当前游戏 Frame",
            ready=snapshot.frame_ready,
            reason=_frame_reason(snapshot),
            action_label="建立当前游戏 Frame",
            anchor="service-game-circle-access",
            checked_at=checked_at,
            purpose="提供当前范围的来源读取身份。",
        ),
        _runtime_node(
            "audit-configuration",
            "审核能力配置",
            ready=audit_configuration_ready,
            reason=("AI 配置和启用策略均有效" if audit_configuration_ready else policy_status["description"] if snapshot.ai_ready else "AI 配置尚不完整"),
            action_label="配置审核能力",
            anchor="service-review",
            checked_at=checked_at,
            purpose="提供 AI 评估所需模型连接和唯一启用策略。",
        ),
        _runtime_node(
            "core-workers",
            "核心后台服务",
            ready=core_workers_ready,
            reason=("调度、采集和 AI 审核服务均运行" if core_workers_ready else "存在未运行的核心后台服务"),
            action_label="查看核心服务",
            anchor="service-core-workers",
            checked_at=checked_at,
            purpose="调度并处理采集与 AI 审核任务。",
        ),
    )
    first_blocked = next((node for node in flow if not node["ready"]), None)
    flow = tuple({**node, "recommended": node is first_blocked} for node in flow)

    game_circle_card = _service_card(
        snapshot,
        "game-circle-session",
        "游戏圈会话服务",
        "唯一持有受控 Chrome、外层会话和系统自建 Frame；其他服务仅通过固定本机 IPC 使用它。",
        service="game_circle_session",
        ready=snapshot.outer_session_ready,
        reason=_session_reason(snapshot),
        business_condition="Chrome、会话服务和十分钟内确认的外层会话均有效；当前 Frame 另行绑定工作范围。",
        missing_behavior="会话服务可继续运行；来源任务按既有 Job 语义延后或失败并保留摘要。",
        connection_layers=(
            {"label": "受控 Chrome", "ready": snapshot.chrome_running, "reason": _service_reason(services["controlled_chrome"])},
            {"label": "外层会话", "ready": snapshot.outer_session_ready, "reason": _session_reason(snapshot)},
            {"label": "当前游戏 Frame", "ready": snapshot.frame_ready, "reason": _frame_reason(snapshot)},
        ),
        session_actions=tuple(login_state.available_actions or []) if login_state else (),
    )
    scheduler_card = _service_card(
        snapshot,
        "scheduler",
        "自动调度器",
        "按已启用的生产者规则创建未来任务；不读取微信，也不调用 AI。",
        service="scheduler",
        business_condition="至少存在一条启用的自动任务规则。",
        missing_behavior="无规则时服务仍运行并等待；不会影响手动创建的任务。",
        rule_ready=False,  # Filled below from the existing rule projection.
    )
    ingestion_card = _runtime_node(
        "ingestion",
        "帖子采集 Worker",
        ready=ingestion_running,
        reason="；".join(f"{item['label']}：{item['reason']}" for item in ingestion_subservices),
        action_label="管理采集服务",
        anchor="service-ingestion",
        checked_at=checked_at,
        purpose="实时采集处理自动任务，人工补采处理历史补采、互动刷新与旧采集任务。",
        lifecycle="两个采集 Worker 均运行" if ingestion_running else "存在未运行的采集 Worker",
        business_condition="当前操作游戏和已验证的当前游戏 Frame 可用后，才能完成来源读取。",
        missing_behavior="Worker 仍可运行；来源任务按既有语义延后或失败并保留摘要。",
        running=ingestion_running,
        subservices=ingestion_subservices,
    )
    metric_card = _service_card(
        snapshot,
        "metric-sync",
        "指标快照 Worker",
        "读取每日粉丝和周度运营效果，仅保存聚合指标快照。",
        service="metric_sync",
        business_condition="当前操作游戏和已验证的当前游戏 Frame 可用后，才能完成来源读取。",
        missing_behavior="Worker 仍可运行；指标任务按既有语义延后或失败并保留摘要。",
    )
    public_opinion_card = _service_card(
        snapshot,
        "public-opinion-reporting",
        "舆情报告 Worker",
        "仅读取本地锁定的帖子、AI 快照与互动快照，生成可追溯的舆情报告。",
        service="public_opinion_reporting",
        business_condition="无需 Chrome、游戏圈会话、AI 模型或上游访问。",
        missing_behavior="创建的报告任务保持等待，直到 Worker 启动。",
    )
    review_card = _service_card(
        snapshot,
        "review",
        "AI 审核 Worker",
        "消费 OCR、视频证据和 AI 评估任务；不创建任务，也不直接读取微信。",
        service="review",
        business_condition="AI 配置完整且存在唯一启用、含规则的审核策略。",
        missing_behavior="Worker 仍可运行；待评估任务按既有语义延后或失败并保留摘要。",
        ai_ready=snapshot.ai_ready,
        policy_ready=snapshot.policy_ready,
    )
    mute_card = _service_card(
        snapshot,
        "mute-maintenance",
        "禁言维护 Worker",
        "维护已明确标记的永久禁言，并消费作者禁言状态刷新任务。",
        service="mute_maintenance",
        business_condition="只读状态刷新可直接消费；仅永久禁言续期 Job 另需对应写入授权。",
        missing_behavior="未授权时仅续期 Job 不执行写入；只读刷新不受影响。",
        allow_enqueue_once=True,
    )
    ocr_card = _runtime_node(
        "ocr",
        "OCR 配置",
        ready=snapshot.ocr_ready,
        reason="OCR 服务地址、模型和密钥均已配置" if snapshot.ocr_ready else "未配置时跳过图片文字识别",
        action_label="配置 OCR",
        anchor="service-optional-capabilities",
        checked_at=checked_at,
        purpose="从图片中提取文字并纳入 AI 审核上下文。",
        lifecycle="不适用",
        business_condition="OCR 地址、模型和密钥均已配置。",
        missing_behavior="图片审核继续使用已有正文和其他证据。",
        optional=True,
    )
    video_card = _service_card(
        snapshot,
        "video-extraction",
        "视频审核 Worker",
        "本机抽帧并仅向 OCR/ASR 传帧或音频字节；不传视频 URL、Cookie 或二维码目标。",
        service="video_extraction",
        ready=snapshot.video_ready and bool(services["video_extraction"].get("running")),
        reason="视频提取 Worker 已运行" if snapshot.video_ready and services["video_extraction"].get("running") else "默认关闭；需媒体工具与已确认的受控样本预检",
        business_condition="ffmpeg、ffprobe、OpenCV、已确认预检和视频功能开关均就绪。",
        missing_behavior="视频审核任务按既有降级证据策略处理。",
        optional=True,
    )
    writes_card = _runtime_node(
        "writes",
        "人工处置授权",
        ready=snapshot.manual_writes_enabled,
        reason="人工处置总开关已开启" if snapshot.manual_writes_enabled else "保持关闭，不会向上游执行处置",
        action_label="管理人工授权",
        anchor="service-optional-capabilities",
        checked_at=checked_at,
        purpose="控制人工确认上游写入与 AI 自动处置的前置授权。",
        lifecycle="不适用",
        business_condition="运营人员显式开启对应人工写入开关。",
        missing_behavior="人工审核记录继续保留，不向上游执行写入。",
        optional=True,
    )
    ai_moderation_card = _service_card(
        snapshot,
        "ai-moderation",
        "AI 自动处置 Worker",
        "受 Supervisor 独立托管；未授权结论继续保留人工审核。",
        service="ai_moderation",
        ready=snapshot.ai_moderation_authorized and bool(services["ai_moderation"].get("running")),
        reason="已运行；固定上限为 100 条/24 小时" if snapshot.ai_moderation_authorized and services["ai_moderation"].get("running") else "默认关闭；需 AI、人工动作和规则级授权",
        business_condition="AI 自动处置、对应人工写入和规则级授权均已完成。",
        missing_behavior="建议保留人工审核，不自动写入上游。",
        optional=True,
    )
    scheduler_card = {
        **scheduler_card,
        "business_ready": scheduled_rule_ready,
        "reason": scheduler_card["lifecycle"],
    }
    groups_by_key = {
        "foundation": (_service_card(
            snapshot,
            "web",
            "Web 服务",
            "提供工作台、系统配置和本地固定控制代理。",
            service="web",
            business_condition="Supervisor 控制通道可达。",
            missing_behavior="页面仅显示固定脱敏错误和终端恢复方向。",
        ),),
        "game-circle-access": (game_circle_card,),
        "core-workers": (
            scheduler_card,
            ingestion_card,
            metric_card,
            public_opinion_card,
            review_card,
            mute_card,
        ),
        "optional-capabilities": (ocr_card, video_card, writes_card, ai_moderation_card),
    }
    service_groups = tuple(
        {"key": key, "label": label, "nodes": groups_by_key[key]}
        for key, label in RUNTIME_CONSOLE_GROUPS
    )
    return {
        "snapshot": snapshot,
        "flow": flow,
        "service_groups": service_groups,
        "readiness": {
            "console_ready": console_ready,
            "source_ready": source_ready,
            "core_review_ready": core_review_ready,
        },
        "work_scope": {
            "configured": snapshot.active_scope_exists,
            "name": snapshot.active_scope_name,
            "description": (
                "采集、指标与人工操作将使用此游戏。"
                if snapshot.active_scope_exists
                else "尚未选择；来源任务不会执行。"
            ),
        },
        "first_blocked": first_blocked,
        "checked_at": checked_at,
    }

def admin_console_overview(request):
    """Avoid loading operational summaries outside the configuration console."""
    if (
        request.resolver_match is None
        or request.resolver_match.namespace != "admin"
        or request.resolver_match.url_name != "index"
    ):
        return {}

    from jobs.models import (
        BackgroundJob,
        JobStatus,
        ScheduledProducerConfig,
        ScheduledProducerRuleConfig,
    )
    from jobs.scheduled_producers import (
        DEFINITION_BY_KEY,
        definition_for_rule,
        next_due_for_display,
        next_due_times_for_display,
        schedule_editor_presentation_for_rule,
        schedule_editor_presentation_for_template,
        schedule_description_for_rule,
        schedule_value_for_rule,
    )
    from posts.models import ActiveGameScope, ManagedGame
    from reviews.models import PolicyVersion, risk_label_display

    active_policies = list(PolicyVersion.objects.filter(is_active=True).order_by("-created_at"))
    active_policy = active_policies[0] if len(active_policies) == 1 else None
    active_policy_rules = []
    if active_policy:
        active_policy_rules = [
            {
                "risk_label": risk_label_display(rule.risk_label),
                "min_confidence": f"{rule.min_confidence:.0%}",
                "recommended_action": rule.get_recommended_action_display(),
                "action_code": rule.recommended_action.lower(),
                "priority": rule.priority,
                "auto_execution_enabled": rule.auto_execution_enabled,
            }
            for rule in active_policy.rules.filter(enabled=True).order_by("-priority", "risk_label")
        ]
    enabled_rule_count = len(active_policy_rules)
    if not active_policies:
        policy_status = {
            "label": "未启用策略",
            "description": "AI 评估不会有可追溯的启用策略版本。",
            "tone": "is-alert",
        }
    elif len(active_policies) > 1:
        policy_status = {
            "label": "多个策略同时启用",
            "description": f"检测到 {len(active_policies)} 个启用版本，请保留唯一生效版本。",
            "tone": "is-alert",
        }
    elif not enabled_rule_count:
        policy_status = {
            "label": "启用策略没有规则",
            "description": "请新增并启用至少一条规则后再依赖策略建议。",
            "tone": "is-warning",
        }
    else:
        policy_status = {
            "label": "策略有效",
            "description": f"当前启用策略包含 {enabled_rule_count} 条规则。",
            "tone": "is-healthy",
        }
    managed_games = list(ManagedGame.objects.order_by("name", "appid"))
    active_scope = ActiveGameScope.objects.select_related("active_game").filter(pk=1).first()
    game_summary = ManagedGame.objects.aggregate(last_synced_at=Max("last_synced_at"))
    runtime_config = public_runtime_config()
    scheduled_producers = []
    active_rules = ScheduledProducerRuleConfig.objects.filter(archived_at__isnull=True).order_by(
        "pk"
    )
    for producer in ScheduledProducerConfig.objects.prefetch_related(
        Prefetch("rules", queryset=active_rules, to_attr="active_rules")
    ).order_by("pk"):
        rules = tuple(producer.active_rules)
        rule_items = [
            {
                "rule": rule,
                "label": schedule_description_for_rule(rule),
                "template_label": definition_for_rule(rule).label,
                "schedule_editor": schedule_editor_presentation_for_rule(rule),
                "schedule_value": schedule_value_for_rule(rule),
                "is_interval": rule.schedule_kind
                == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL,
                "next_due_at": next_due_for_display(rule),
                "next_due_times": next_due_times_for_display(rule),
                "last_result": SCHEDULED_PRODUCER_RESULT_LABELS.get(
                    rule.last_error_category, rule.last_error_category
                ),
            }
            for rule in rules
        ]
        enabled_rule_items = [item for item in rule_items if item["rule"].enabled]
        recent_rule = max(
            rule_items,
            key=lambda item: (
                item["rule"].last_checked_at.timestamp()
                if item["rule"].last_checked_at is not None
                else 0
            ),
            default=None,
        )
        upcoming_triggers = sorted(
            (
                {
                    "at": due_at,
                    "label": item["template_label"],
                }
                for item in enabled_rule_items
                for due_at in item["next_due_times"]
            ),
            key=lambda item: (item["at"], item["label"]),
        )[:3]
        scheduled_producers.append(
            {
                "producer": producer,
                "label": DEFINITION_BY_KEY[producer.producer_key].label,
                "new_rule_templates": tuple(
                    {"key": definition.key, "label": definition.label}
                    for definition in DEFINITION_BY_KEY[producer.producer_key].templates
                ),
                "rules": rule_items,
                "upcoming_triggers": upcoming_triggers,
                "recent_rule": recent_rule,
                "new_rule_is_interval": (
                    DEFINITION_BY_KEY[producer.producer_key].templates[0].schedule_kind
                    == ScheduledProducerRuleConfig.ScheduleKind.INTERVAL
                ),
                "new_rule_schedule_editor": schedule_editor_presentation_for_template(
                    producer_key=producer.producer_key,
                    template=DEFINITION_BY_KEY[producer.producer_key].templates[0],
                ),
            }
        )
    try:
        services = SupervisorClient().status()
        supervisor_error = ""
    except SupervisorUnavailableError as exc:
        services = {}
        supervisor_error = str(exc)
    for name in MANAGED_SERVICE_NAMES:
        services.setdefault(name, {"running": False, "desired": False})
    from posts.models import GameCircleLoginRuntimeState

    login_state = GameCircleLoginRuntimeState.objects.filter(pk=1).first()
    ai_ready = bool(
        runtime_config["ai_base_url"]
        and runtime_config["ai_api_key_configured"]
        and runtime_config["ai_model"]
    )
    ocr_ready = bool(
        runtime_config["ocr_base_url"]
        and runtime_config["ocr_api_key_configured"]
        and runtime_config["ocr_model"]
    )
    video_tools_ready = bool(media_tools_available() and find_spec("cv2"))
    video_ready = (
        runtime_config["video_enabled"] == "true"
        and runtime_config["video_preflight_verified"] == "true"
        and video_tools_ready
    )
    write_switches = tuple(
        (key, {"name": label, "enabled": runtime_config[key.lower()] == "true"})
        for key, label in (
            ("ENABLE_UPSTREAM_WRITES", "启用人工处置总开关"),
            ("ENABLE_SINK_WRITES", "允许沉底"),
            ("ENABLE_BLOCK_WRITES", "允许屏蔽"),
            ("ENABLE_MUTE_WRITES", "允许禁言"),
            ("ENABLE_UNMUTE_WRITES", "允许解除禁言"),
            ("ENABLE_RESTORE_WRITES", "允许恢复正常"),
            ("ENABLE_AI_AUTOMATED_MODERATION", "启用 AI 自动处置总开关"),
            ("ENABLE_AI_SINK_WRITES", "允许 AI 自动沉底"),
            ("ENABLE_AI_BLOCK_WRITES", "允许 AI 自动屏蔽"),
        )
    )
    ai_moderation_authorized = runtime_config["enable_ai_automated_moderation"] == "true" and (
        (
            runtime_config["enable_ai_sink_writes"] == "true"
            and runtime_config["enable_upstream_writes"] == "true"
            and runtime_config["enable_sink_writes"] == "true"
        )
        or (
            runtime_config["enable_ai_block_writes"] == "true"
            and runtime_config["enable_upstream_writes"] == "true"
            and runtime_config["enable_block_writes"] == "true"
        )
    )
    ingestion_subservices = tuple(
        {
            "service": service,
            "label": label,
            "running": bool(services[service].get("running")),
            "reason": _service_reason(services[service]),
        }
        for service, label in (
            ("ingestion", "实时采集"),
            ("backfill_ingestion", "人工补采"),
        )
    )
    runtime_console = _build_runtime_console(
        services=services,
        supervisor_error=supervisor_error,
        active_scope=active_scope,
        login_state=login_state,
        ai_ready=ai_ready,
        policy_ready=bool(enabled_rule_count),
        policy_status=policy_status,
        ocr_ready=ocr_ready,
        video_ready=video_ready,
        manual_writes_enabled=runtime_config["enable_upstream_writes"] == "true",
        ai_moderation_authorized=ai_moderation_authorized,
        ingestion_subservices=ingestion_subservices,
        scheduled_rule_ready=any(
            rule["rule"].enabled
            for producer in scheduled_producers
            for rule in producer["rules"]
        ),
    )
    return {
        "admin_console": {
            "active_policy": active_policy,
            "active_policy_rules": active_policy_rules,
            "policy_status": policy_status,
            "managed_game_count": ManagedGame.objects.count(),
            "active_game_scope": active_scope,
            "manual_ingestion_default_appid": active_scope.active_appid if active_scope else "",
            "manual_ingestion_games": managed_games,
            "last_game_sync_at": game_summary["last_synced_at"],
            "pending_job_count": BackgroundJob.objects.filter(status=JobStatus.PENDING).count(),
            "running_job_count": BackgroundJob.objects.filter(status=JobStatus.RUNNING).count(),
            "failed_job_count": BackgroundJob.objects.filter(status=JobStatus.FAILED).count(),
            "runtime_config": runtime_config,
            "scheduled_producers": scheduled_producers,
            "services": services,
            "supervisor_error": supervisor_error,
            "runtime_console": runtime_console,
            "write_switches": write_switches,
        }
    }

def _service_reason(service: dict) -> str:
    if service.get("running"):
        heartbeat = service.get("heartbeat_at")
        state = (
            f"运行中；最近心跳 {_display_heartbeat(heartbeat)}"
            if heartbeat
            else "运行中；尚未写入心跳"
        )
    elif service.get("failure_reason") == "CHROME_START_TIMEOUT":
        state = "受控 Chrome 未在限定时间内就绪；Supervisor 将继续重试"
    elif service.get("exit_code") is not None:
        state = f"未运行；最近退出码 {service['exit_code']}"
    else:
        state = "未运行；尚未启用"
    return state


def _display_heartbeat(value: object) -> str:
    """Present Supervisor's UTC timestamp in the operator's local time."""
    if not isinstance(value, str):
        return str(value)
    parsed = parse_datetime(value)
    if parsed is None:
        return value
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return timezone.localtime(parsed).strftime("%Y-%m-%d %H:%M:%S")
