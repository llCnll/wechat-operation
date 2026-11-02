"""Pytest-only settings bootstrap that isolates all runtime-derived state."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

PYTEST_MODE_ENV = "WECHAT_OPERATION_PYTEST_MODE"
PYTEST_RUNTIME_DIR_ENV = "WECHAT_OPERATION_PYTEST_RUNTIME_DIR"
PYTEST_MODE_VALUE = "isolated"
VOLATILE_RUNTIME_DIRECTORIES = frozenset({"supervisor", "worker-locks", "static"})
VOLATILE_RUNTIME_REMOVE_ATTEMPTS = 3
TEST_EXTERNAL_ENVIRONMENT = {
    "DJANGO_SECRET_KEY": "pytest-only-secret-key-not-used-outside-isolated-tests",
    "AI_BASE_URL": "https://test.invalid",
    "AI_API_KEY": "test-only-key",
    "AI_MODEL": "test-model",
    "PUBLIC_OPINION_CONCLUSION_ENABLED": "false",
    "OCR_BASE_URL": "",
    "OCR_API_KEY": "",
    "OCR_MODEL": "",
    "ASR_BASE_URL": "",
    "ASR_API_KEY": "",
    "ASR_MODEL": "",
    "ASR_ENABLED": "false",
    "VIDEO_ENABLED": "false",
    "VIDEO_PREFLIGHT_VERIFIED": "false",
    "ENABLE_UPSTREAM_WRITES": "false",
    "ENABLE_SINK_WRITES": "false",
    "ENABLE_BLOCK_WRITES": "false",
    "ENABLE_MUTE_WRITES": "false",
    "ENABLE_UNMUTE_WRITES": "false",
    "ENABLE_RESTORE_WRITES": "false",
    "ENABLE_AI_AUTOMATED_MODERATION": "false",
    "ENABLE_AI_SINK_WRITES": "false",
    "ENABLE_AI_BLOCK_WRITES": "false",
}


def _worker_id() -> str:
    value = os.getenv("PYTEST_XDIST_WORKER", "master")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        raise RuntimeError("pytest worker 标识无效")
    return value


def _runtime_root() -> Path:
    project_root = Path(__file__).resolve().parent.parent
    return (project_root / ".pytest-runtime").resolve(strict=False)


def _runtime_dir() -> Path:
    runtime_root = _runtime_root()
    candidate = (runtime_root / _worker_id()).resolve(strict=False)
    if candidate.parent != runtime_root:
        raise RuntimeError("pytest 运行态目录无效")
    return candidate


def _clear_volatile_state(runtime_dir: Path) -> None:
    resolved_runtime = runtime_dir.resolve(strict=False)
    if (
        resolved_runtime.parent != _runtime_root()
        or resolved_runtime.name != _worker_id()
    ):
        raise RuntimeError("pytest 运行态清理目标无效")
    for name in VOLATILE_RUNTIME_DIRECTORIES:
        target = (resolved_runtime / name).resolve(strict=False)
        if target.parent != resolved_runtime:
            raise RuntimeError("pytest 运行态清理目标无效")
        if target.exists():
            _remove_volatile_directory(target)


def _remove_volatile_directory(target: Path) -> None:
    for attempt in range(VOLATILE_RUNTIME_REMOVE_ATTEMPTS):
        try:
            shutil.rmtree(target)
            return
        except PermissionError:
            if attempt == VOLATILE_RUNTIME_REMOVE_ATTEMPTS - 1:
                raise
            time.sleep(0.1)


def _configure_test_runtime() -> None:
    runtime_dir = _runtime_dir()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _clear_volatile_state(runtime_dir)
    (runtime_dir / "static").mkdir(exist_ok=True)
    os.environ[PYTEST_MODE_ENV] = PYTEST_MODE_VALUE
    os.environ[PYTEST_RUNTIME_DIR_ENV] = str(runtime_dir)
    os.environ.update(TEST_EXTERNAL_ENVIRONMENT)


_configure_test_runtime()

from config.settings import *  # noqa: F403,E402
