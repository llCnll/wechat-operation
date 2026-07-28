"""Read-only presentation state for public-opinion conclusion configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from config.runtime_config import public_runtime_config

from .models import PublicOpinionReportRun
from .public_opinion_conclusion import conclusion_failure_label

NON_FAILURE_CODES = frozenset({"", "MODEL_CONCLUSION_DISABLED", "MODEL_NOT_CONFIGURED"})


@dataclass(frozen=True)
class PublicOpinionConclusionConfiguration:
    code: str
    label: str
    description: str
    enabled: bool
    ai_base_url: str
    ai_model: str
    ai_api_key_configured: bool
    recent_failure: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def public_opinion_conclusion_configuration(
    *, appid: str, runtime_config: dict[str, object] | None = None
) -> dict[str, object]:
    """Return current config separately from immutable historical run outcomes."""
    runtime_config = runtime_config or public_runtime_config()
    enabled = runtime_config["public_opinion_conclusion_enabled"] == "true"
    ai_base_url = str(runtime_config["ai_base_url"])
    ai_model = str(runtime_config["ai_model"])
    ai_api_key_configured = bool(runtime_config["ai_api_key_configured"])

    if not enabled:
        state = PublicOpinionConclusionConfiguration(
            code="DISABLED",
            label="未启用",
            description="模型结论已关闭；报告仍会生成确定性统计和可导出的证据。",
            enabled=False,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            ai_api_key_configured=ai_api_key_configured,
        )
    elif not (ai_base_url and ai_model and ai_api_key_configured):
        state = PublicOpinionConclusionConfiguration(
            code="MODEL_NOT_CONFIGURED",
            label="模型连接未配置",
            description="请先在系统配置的 AI 审核 Worker 中完善服务地址、模型名称和访问密钥。",
            enabled=True,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            ai_api_key_configured=ai_api_key_configured,
        )
    else:
        state = PublicOpinionConclusionConfiguration(
            code="READY",
            label="已就绪",
            description="新建报告或重跑固定来源时，可生成一次受约束的模型结论。",
            enabled=True,
            ai_base_url=ai_base_url,
            ai_model=ai_model,
            ai_api_key_configured=ai_api_key_configured,
        )

    recent = (
        PublicOpinionReportRun.objects.filter(
            report__source_appid=appid,
            conclusion_status="UNAVAILABLE",
        )
        .exclude(conclusion_error_code__in=NON_FAILURE_CODES)
        .order_by("-finished_at", "-pk")
        .first()
    )
    if recent is None:
        return state.as_dict()
    return {
        **state.as_dict(),
        "recent_failure": conclusion_failure_label(recent.conclusion_error_code),
    }
