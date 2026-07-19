"""Small, read-only context used by the system configuration homepage."""

from importlib.util import find_spec

from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from config.cdp import CdpUnavailableError, get_browser_login_status
from config.runtime_config import public_runtime_config
from config.supervisor import SupervisorClient, SupervisorUnavailableError
from reviews.video import media_tools_available

MANAGED_SERVICE_NAMES = (
    "web",
    "scheduler",
    "ingestion",
    "backfill_ingestion",
    "review",
    "video_extraction",
    "ai_moderation",
)


def _pipeline_node(
    key: str,
    label: str,
    ready: bool,
    next_step: str,
    reason: str,
    checked_at,
    **extra,
) -> dict:
    return {
        "key": key,
        "label": label,
        "ready": ready,
        "next_step": next_step,
        "reason": reason,
        "checked_at": checked_at,
        **extra,
    }


def admin_console_overview(request):
    """Avoid loading operational summaries outside the configuration console."""
    if (
        request.resolver_match is None
        or request.resolver_match.namespace != "admin"
        or request.resolver_match.url_name != "index"
    ):
        return {}

    from jobs.models import BackgroundJob, JobStatus
    from posts.game_catalog import DEFAULT_GAME_APPID, DEFAULT_GAME_NAME, GameOption
    from posts.models import ManagedGame
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
    if not any(game.appid == DEFAULT_GAME_APPID for game in managed_games):
        managed_games.insert(0, GameOption(DEFAULT_GAME_APPID, DEFAULT_GAME_NAME))
    game_summary = ManagedGame.objects.aggregate(last_synced_at=Max("last_synced_at"))
    runtime_config = public_runtime_config()
    try:
        services = SupervisorClient().status()
        supervisor_error = ""
    except SupervisorUnavailableError as exc:
        services = {}
        supervisor_error = str(exc)
    for name in MANAGED_SERVICE_NAMES:
        services.setdefault(name, {"running": False, "desired": False})
    try:
        cdp_status = get_browser_login_status()
        cdp_valid = cdp_status.valid
        cdp_reason = cdp_status.reason
    except CdpUnavailableError:
        cdp_valid = False
        cdp_reason = "受控 Chrome 未启动"
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
    checked_at = timezone.now()
    # This is intentionally ordered by operational data flow. Configuration nodes sit
    # immediately before the worker that consumes them, rather than by implementation
    # module, so the next setup action is understandable from the console.
    pipeline = [
        _pipeline_node(
            "web",
            "Web 服务",
            not supervisor_error and services["web"]["running"],
            "启动 Supervisor",
            supervisor_error
            or ("Web 服务运行正常" if services["web"]["running"] else "Web 服务未运行"),
            checked_at,
            service="web",
            description="由本机 Supervisor 托管，提供工作台、系统配置和本地控制代理。",
        ),
        _pipeline_node(
            "cdp",
            "CDP 登录态",
            cdp_valid,
            cdp_reason or "打开受控 Chrome 登录页",
            "登录态有效" if cdp_valid else cdp_reason,
            checked_at,
            description="检查受控 Chrome 的微信插件会话；系统不保存 Cookie、令牌或二维码内容。",
        ),
        _pipeline_node(
            "scheduler",
            "自动调度器",
            bool(services["scheduler"]["running"]),
            "启动自动调度器",
            _service_reason(services["scheduler"]),
            checked_at,
            service="scheduler",
            running=bool(services["scheduler"]["running"]),
            description="每 15 分钟创建采集和状态巡检任务；不读取微信，也不调用 AI。",
        ),
    ]
    ingestion_services = (
        (
            "ingestion",
            "实时采集",
            "消费自动采集与状态巡检任务，保障每 15 分钟任务及时处理。",
        ),
        (
            "backfill_ingestion",
            "人工补采",
            "消费历史补采、范围互动刷新和旧采集任务。",
        ),
    )
    ingestion_subservices = tuple(
        {
            "service": service,
            "label": label,
            "running": bool(services[service].get("running")),
            "reason": _service_reason(services[service]),
        }
        for service, label, description in ingestion_services
    )
    ingestion_ready = all(item["running"] for item in ingestion_subservices)
    pipeline.append(
        _pipeline_node(
            "ingestion",
            "帖子采集 Worker",
            ingestion_ready,
            "启动帖子采集 Worker（同时启动实时采集和人工补采）",
            "；".join(f"{item['label']}：{item['reason']}" for item in ingestion_subservices),
            checked_at,
            running=ingestion_ready,
            subservices=ingestion_subservices,
            description=(
                "实时采集 Worker 处理自动采集和状态巡检；"
                "人工补采 Worker 处理历史补采、互动刷新和旧任务。"
            ),
        ),
    )
    pipeline.extend(
        (
            _pipeline_node(
                "ocr",
                "OCR 配置（可选）",
                ocr_ready,
                "填写 OCR 服务配置",
                "OCR 服务地址、模型和密钥均已配置" if ocr_ready else "未配置时跳过图片文字识别",
                checked_at,
                optional=True,
                description="从图片中提取文字并纳入 AI 审核上下文；未配置不影响纯文本审核。",
            ),
            _pipeline_node(
                "video_extraction",
                "视频审核 Worker（可选）",
                video_ready and bool(services["video_extraction"]["running"]),
                "安装媒体工具、完成无凭据读取预检后启用",
                (
                    "视频提取 Worker 已运行"
                    if video_ready and services["video_extraction"]["running"]
                    else "默认关闭；需 ffmpeg、ffprobe、OpenCV 与已确认的受控样本预检"
                ),
                checked_at,
                optional=True,
                service="video_extraction",
                running=bool(services["video_extraction"]["running"]),
                description=(
                    "本机抽帧并仅向 OCR/ASR 传帧或音频字节；"
                    "不传视频 URL、Cookie 或二维码目标。"
                ),
            ),
            _pipeline_node(
                "ai",
                "AI 审核配置",
                ai_ready,
                "填写 AI 服务配置",
                "AI 服务地址、模型和密钥均已配置" if ai_ready else "AI 配置尚不完整",
                checked_at,
                description="用于帖子内容分类、风险识别和人工审核建议。",
            ),
            _pipeline_node(
                "policy",
                "默认审核策略",
                bool(enabled_rule_count),
                "初始化或启用审核策略",
                policy_status["description"],
                checked_at,
                description="为 AI 审核建议提供唯一启用策略和规则阈值。",
            ),
            _pipeline_node(
                "review",
                "AI 审核 Worker",
                bool(services["review"]["running"]),
                "启动AI 审核 Worker",
                _service_reason(services["review"]),
                checked_at,
                service="review",
                running=bool(services["review"]["running"]),
                description="消费图片 OCR、视频证据和 AI 评估任务；不创建任务，也不直接读取微信。",
            ),
            _pipeline_node(
                "writes",
                "人工处置授权（可选）",
                runtime_config["enable_upstream_writes"] == "true",
                "按需开启人工处置开关",
                "人工处置总开关已开启"
                if runtime_config["enable_upstream_writes"] == "true"
                else "保持关闭，不会向上游执行处置",
                checked_at,
                optional=True,
                description="人工确认写入与默认关闭的 AI 自动沉底/屏蔽授权。",
            ),
            _pipeline_node(
                "ai_moderation",
                "AI 自动处置 Worker（可选）",
                (
                    ai_moderation_authorized
                    and bool(services["ai_moderation"]["running"])
                ),
                "完成 AI 自动处置授权后启动 Worker",
                (
                    "AI 自动处置 Worker 已运行；固定上限为 100 条/24 小时"
                    if ai_moderation_authorized
                    and services["ai_moderation"]["running"]
                    else "默认关闭；需同时完成 AI、人工动作和规则级授权"
                ),
                checked_at,
                optional=True,
                service="ai_moderation",
                running=bool(services["ai_moderation"]["running"]),
                description="独立受 Supervisor 托管；未授权结论直接保留人工审核。",
            ),
        )
    )
    core_keys = {"web", "policy", "ai", "cdp", "scheduler", "ingestion", "review"}
    first_blocked = next(
        (node for node in pipeline if node["key"] in core_keys and not node["ready"]), None
    )
    return {
        "admin_console": {
            "active_policy": active_policy,
            "active_policy_rules": active_policy_rules,
            "policy_status": policy_status,
            "managed_game_count": ManagedGame.objects.count(),
            "manual_ingestion_default_appid": DEFAULT_GAME_APPID,
            "manual_ingestion_games": managed_games,
            "last_game_sync_at": game_summary["last_synced_at"],
            "pending_job_count": BackgroundJob.objects.filter(status=JobStatus.PENDING).count(),
            "running_job_count": BackgroundJob.objects.filter(status=JobStatus.RUNNING).count(),
            "failed_job_count": BackgroundJob.objects.filter(status=JobStatus.FAILED).count(),
            "runtime_config": runtime_config,
            "services": services,
            "supervisor_error": supervisor_error,
            "pipeline": pipeline,
            "pipeline_ready": first_blocked is None,
            "first_blocked_pipeline_node": first_blocked,
            "pipeline_checked_at": checked_at,
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
