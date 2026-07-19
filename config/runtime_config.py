"""Whitelist editor for runtime/.env without exposing saved secrets."""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from config.settings import RUNTIME_DIR

SECRET_KEYS = frozenset(("AI_API_KEY", "OCR_API_KEY"))
EDITABLE_KEYS = (
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
    "AI_TIMEOUT_SECONDS",
    "OCR_BASE_URL",
    "OCR_API_KEY",
    "OCR_MODEL",
    "ENABLE_UPSTREAM_WRITES",
    "ENABLE_SINK_WRITES",
    "ENABLE_BLOCK_WRITES",
    "ENABLE_MUTE_WRITES",
    "ENABLE_UNMUTE_WRITES",
    "ENABLE_RESTORE_WRITES",
)
BOOLEAN_KEYS = frozenset(key for key in EDITABLE_KEYS if key.startswith("ENABLE_"))


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


def public_runtime_config(path: Path = RUNTIME_DIR / ".env") -> dict[str, object]:
    values = _values(_read_lines(path))
    result: dict[str, object] = {
        key.lower(): values.get(key, "") for key in EDITABLE_KEYS if key not in SECRET_KEYS
    }
    result["ai_api_key_configured"] = bool(values.get("AI_API_KEY"))
    result["ocr_api_key_configured"] = bool(values.get("OCR_API_KEY"))
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
        if key in BOOLEAN_KEYS:
            value = "true" if value.lower() in {"1", "true", "on", "yes"} else "false"
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
    model_keys = {
        "AI_BASE_URL",
        "AI_API_KEY",
        "AI_MODEL",
        "AI_TIMEOUT_SECONDS",
        "OCR_BASE_URL",
        "OCR_API_KEY",
        "OCR_MODEL",
    }
    if changed & model_keys:
        services.add("review")
    if changed & BOOLEAN_KEYS:
        services.add("web")
    return services
