"""Whitelist editor for runtime/.env without exposing saved secrets."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from config.settings import RUNTIME_DIR

SECRET_KEYS = frozenset(("AI_API_KEY", "OCR_API_KEY", "ASR_API_KEY"))
EDITABLE_KEYS = (
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
    "AI_TIMEOUT_SECONDS",
    "PUBLIC_OPINION_CONCLUSION_ENABLED",
    "OCR_BASE_URL",
    "OCR_API_KEY",
    "OCR_MODEL",
    "ASR_BASE_URL",
    "ASR_API_KEY",
    "ASR_MODEL",
    "ASR_TIMEOUT_SECONDS",
    "ASR_ENABLED",
    "VIDEO_ENABLED",
    "VIDEO_PREFLIGHT_VERIFIED",
    "VIDEO_MAX_BYTES",
    "VIDEO_MAX_DURATION_SECONDS",
    "VIDEO_MAX_PER_POST",
    "VIDEO_MAX_FRAME_EDGE",
    "VIDEO_DHASH_THRESHOLD",
    "VIDEO_OCR_FRAME_BUDGET",
    "VIDEO_CONNECT_TIMEOUT_SECONDS",
    "VIDEO_READ_TIMEOUT_SECONDS",
    "VIDEO_PROCESSING_TIMEOUT_SECONDS",
    "ENABLE_UPSTREAM_WRITES",
    "ENABLE_SINK_WRITES",
    "ENABLE_BLOCK_WRITES",
    "ENABLE_MUTE_WRITES",
    "ENABLE_UNMUTE_WRITES",
    "ENABLE_RESTORE_WRITES",
    "ENABLE_AI_AUTOMATED_MODERATION",
    "ENABLE_AI_SINK_WRITES",
    "ENABLE_AI_BLOCK_WRITES",
    "SESSION_REPAIR_ENABLED",
)
BOOLEAN_KEYS = frozenset(key for key in EDITABLE_KEYS if key.startswith("ENABLE_")) | {
    "ASR_ENABLED",
    "VIDEO_ENABLED",
    "VIDEO_PREFLIGHT_VERIFIED",
    "SESSION_REPAIR_ENABLED",
    "PUBLIC_OPINION_CONCLUSION_ENABLED",
}
SESSION_MAINTENANCE_KEYS = frozenset(("SESSION_REPAIR_ENABLED",))
VIDEO_DEFAULTS = {
    "ASR_ENABLED": "false",
    "ASR_TIMEOUT_SECONDS": "60",
    "VIDEO_MAX_BYTES": str(50 * 1024 * 1024),
    "VIDEO_MAX_DURATION_SECONDS": "30",
    "VIDEO_MAX_PER_POST": "3",
    "VIDEO_MAX_FRAME_EDGE": "960",
    "VIDEO_DHASH_THRESHOLD": "24",
    "VIDEO_OCR_FRAME_BUDGET": "60",
    "VIDEO_CONNECT_TIMEOUT_SECONDS": "10",
    "VIDEO_READ_TIMEOUT_SECONDS": "30",
    "VIDEO_PROCESSING_TIMEOUT_SECONDS": "120",
}
RUNTIME_DEFAULTS = {
    **VIDEO_DEFAULTS,
    **{key: "false" for key in BOOLEAN_KEYS},
}
SERVICE_DISPLAY_NAMES = {
    "web": "Web 服务",
    "scheduler": "自动调度器",
    "game_circle_session": "游戏圈会话服务",
    "ingestion": "实时采集 Worker",
    "backfill_ingestion": "人工补采 Worker",
    "metric_sync": "指标同步 Worker",
    "public_opinion_reporting": "舆情报告 Worker",
    "review": "AI 审核 Worker",
    "video_extraction": "视频审核 Worker",
    "ai_moderation": "AI 自动处置 Worker",
}
NUMERIC_BOUNDS = {
    "ASR_TIMEOUT_SECONDS": (float, 1, 300),
    "VIDEO_MAX_BYTES": (int, 1_048_576, 200 * 1024 * 1024),
    "VIDEO_MAX_DURATION_SECONDS": (int, 1, 60),
    "VIDEO_MAX_PER_POST": (int, 1, 10),
    "VIDEO_MAX_FRAME_EDGE": (int, 240, 1920),
    "VIDEO_DHASH_THRESHOLD": (int, 0, 32),
    "VIDEO_OCR_FRAME_BUDGET": (int, 1, 120),
    "VIDEO_CONNECT_TIMEOUT_SECONDS": (float, 1, 60),
    "VIDEO_READ_TIMEOUT_SECONDS": (float, 1, 180),
    "VIDEO_PROCESSING_TIMEOUT_SECONDS": (float, 10, 600),
}


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _validated_numeric_value(key: str, value: str) -> str:
    numeric_type, minimum, maximum = NUMERIC_BOUNDS[key]
    try:
        parsed = numeric_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是数字") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{key} 必须在 {minimum} 至 {maximum} 范围内")
    return str(parsed)


def public_runtime_config(path: Path = RUNTIME_DIR / ".env") -> dict[str, object]:
    values = _values(_read_lines(path))
    result: dict[str, object] = {
        key.lower(): values.get(key) or RUNTIME_DEFAULTS.get(key, "")
        for key in EDITABLE_KEYS
        if key not in SECRET_KEYS
    }
    result["ai_api_key_configured"] = bool(values.get("AI_API_KEY"))
    result["ocr_api_key_configured"] = bool(values.get("OCR_API_KEY"))
    result["asr_api_key_configured"] = bool(values.get("ASR_API_KEY"))
    return result


def update_runtime_config(data: dict[str, str], path: Path = RUNTIME_DIR / ".env") -> set[str]:
    """Persist only approved keys; blank secret input deliberately keeps its value."""
    lines = _read_lines(path)
    values = _values(lines)
    changed: set[str] = set()
    for key in EDITABLE_KEYS:
        if key not in data:
            continue
        value = data[key]
        if key in SECRET_KEYS and not value:
            continue
        if key in VIDEO_DEFAULTS and not value.strip():
            value = VIDEO_DEFAULTS[key]
        if key in BOOLEAN_KEYS:
            value = "true" if value.lower() in {"1", "true", "on", "yes"} else "false"
        elif key in NUMERIC_BOUNDS:
            value = _validated_numeric_value(key, value.strip())
        if values.get(key) != value:
            values[key] = value
            changed.add(key)

    if not changed:
        return changed
    emitted: set[str] = set()
    output: list[str] = []
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in changed:
                output.append(f"{key}={values[key]}")
                emitted.add(key)
                continue
        output.append(line)
    output.extend(f"{key}={values[key]}" for key in sorted(changed - emitted))
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write("\n".join(output).rstrip() + "\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
    return changed


def affected_services(changed: set[str]) -> set[str]:
    services: set[str] = set()
    review_model_keys = {
        "AI_BASE_URL",
        "AI_API_KEY",
        "AI_MODEL",
        "AI_TIMEOUT_SECONDS",
        "OCR_BASE_URL",
        "OCR_API_KEY",
        "OCR_MODEL",
    }
    video_keys = {
        "OCR_BASE_URL",
        "OCR_API_KEY",
        "OCR_MODEL",
        "ASR_BASE_URL",
        "ASR_API_KEY",
        "ASR_MODEL",
        "ASR_ENABLED",
        "ASR_TIMEOUT_SECONDS",
        "VIDEO_ENABLED",
        "VIDEO_PREFLIGHT_VERIFIED",
        "VIDEO_MAX_BYTES",
        "VIDEO_MAX_DURATION_SECONDS",
        "VIDEO_MAX_PER_POST",
        "VIDEO_MAX_FRAME_EDGE",
        "VIDEO_DHASH_THRESHOLD",
        "VIDEO_OCR_FRAME_BUDGET",
        "VIDEO_CONNECT_TIMEOUT_SECONDS",
        "VIDEO_READ_TIMEOUT_SECONDS",
        "VIDEO_PROCESSING_TIMEOUT_SECONDS",
    }
    if changed & review_model_keys:
        services.add("review")
    if changed & (review_model_keys | {"PUBLIC_OPINION_CONCLUSION_ENABLED"}):
        services.add("public_opinion_reporting")
    if changed & video_keys:
        services.add("video_extraction")
    if changed & (BOOLEAN_KEYS - SESSION_MAINTENANCE_KEYS):
        services.update(("web", "ai_moderation"))
    if changed & SESSION_MAINTENANCE_KEYS:
        services.update(("game_circle_session", "scheduler"))
    return services


def display_service_names(services: set[str]) -> list[str]:
    """Return console-facing service labels without leaking internal service keys."""
    return [SERVICE_DISPLAY_NAMES.get(service, service) for service in sorted(services)]
