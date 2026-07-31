"""Regression coverage for pytest runtime and HTTP safety boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import httpx
import pytest

from config import settings, test_settings
from tests.conftest import allowed_local_http_hosts


def test_test_settings_isolate_runtime_and_override_development_configuration():
    """A fresh settings import must not inherit a developer's model configuration."""

    script = """
import json
import config.test_settings
from config.settings import IS_PYTEST_RUNTIME, RUNTIME_DIR, RuntimeSettings, runtime
print(json.dumps({
    "is_pytest_runtime": IS_PYTEST_RUNTIME,
    "runtime_dir": str(RUNTIME_DIR),
    "database_path": str(runtime.database_path),
    "ai_base_url": runtime.ai_base_url,
    "ai_api_key": runtime.ai_api_key,
    "ai_model": runtime.ai_model,
    "conclusion_enabled": runtime.public_opinion_conclusion_enabled,
    "env_file": RuntimeSettings.model_config["env_file"],
}))
"""
    environment = os.environ | {
        "AI_BASE_URL": "https://development.invalid",
        "AI_API_KEY": "development-only-secret",
        "AI_MODEL": "development-model",
        "PUBLIC_OPINION_CONCLUSION_ENABLED": "true",
        "PYTEST_XDIST_WORKER": "settingsprobe",
    }

    expected_runtime = settings.BASE_DIR / ".pytest-runtime" / "settingsprobe"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=settings.BASE_DIR,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        configured = json.loads(completed.stdout)

        assert configured == {
            "is_pytest_runtime": True,
            "runtime_dir": str(expected_runtime),
            "database_path": str(expected_runtime / "operations.sqlite3"),
            "ai_base_url": "https://test.invalid",
            "ai_api_key": "test-only-key",
            "ai_model": "test-model",
            "conclusion_enabled": False,
            "env_file": None,
        }
    finally:
        runtime_root = (settings.BASE_DIR / ".pytest-runtime").resolve()
        assert expected_runtime.resolve().parent == runtime_root
        shutil.rmtree(expected_runtime, ignore_errors=True)


def test_runtime_paths_are_scoped_to_the_current_pytest_worker():
    worker_id = os.getenv("PYTEST_XDIST_WORKER", "master")

    assert settings.RUNTIME_DIR == settings.PYTEST_RUNTIME_ROOT / worker_id
    assert settings.runtime.database_path == settings.RUNTIME_DIR / "operations.sqlite3"
    assert settings.runtime.database_path.parent / "worker-locks" == (
        settings.RUNTIME_DIR / "worker-locks"
    )


@pytest.mark.parametrize(
    "unsafe_runtime_dir",
    [
        settings.PYTEST_RUNTIME_ROOT,
        settings.BASE_DIR,
        settings.BASE_DIR / "runtime",
    ],
)
def test_pytest_runtime_path_rejects_a_directory_outside_the_owned_root(
    monkeypatch, unsafe_runtime_dir
):
    monkeypatch.setenv(settings.PYTEST_MODE_ENV, settings.PYTEST_MODE_VALUE)
    monkeypatch.setenv(settings.PYTEST_RUNTIME_DIR_ENV, str(unsafe_runtime_dir))

    with pytest.raises(RuntimeError, match=".pytest-runtime"):
        settings._pytest_runtime_dir()


def test_volatile_runtime_cleanup_is_limited_to_the_current_worker(monkeypatch, tmp_path):
    runtime_root = tmp_path / ".pytest-runtime"
    runtime_dir = runtime_root / "worker"
    protected_runtime = tmp_path / "runtime"
    for name in test_settings.VOLATILE_RUNTIME_DIRECTORIES:
        target = runtime_dir / name
        target.mkdir(parents=True)
        (target / "state.txt").write_text("volatile", encoding="utf-8")
    protected_runtime.mkdir()
    protected_file = protected_runtime / "state.txt"
    protected_file.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(test_settings, "_runtime_root", lambda: runtime_root.resolve())
    monkeypatch.setattr(test_settings, "_worker_id", lambda: "worker")

    test_settings._clear_volatile_state(runtime_dir)

    assert not any(
        (runtime_dir / name).exists()
        for name in test_settings.VOLATILE_RUNTIME_DIRECTORIES
    )
    with pytest.raises(RuntimeError, match="清理目标"):
        test_settings._clear_volatile_state(protected_runtime)
    assert protected_file.read_text(encoding="utf-8") == "keep"


def test_volatile_runtime_cleanup_retries_windows_file_occupancy(monkeypatch, tmp_path):
    target = tmp_path / "worker" / "supervisor"
    target.mkdir(parents=True)
    attempts = []

    def remove_after_two_contentions(path):
        attempts.append(path)
        if len(attempts) < 3:
            raise PermissionError("locked")

    monkeypatch.setattr(test_settings.shutil, "rmtree", remove_after_two_contentions)
    monkeypatch.setattr(test_settings.time, "sleep", lambda _seconds: None)

    test_settings._remove_volatile_directory(target)

    assert attempts == [target, target, target]


def test_worker_lock_uses_the_isolated_runtime_not_a_development_lock(monkeypatch, tmp_path):
    from jobs import worker

    development_database = tmp_path / "runtime" / "operations.sqlite3"
    test_database = settings.runtime.database_path
    monkeypatch.setattr(worker.runtime, "database_path", development_database)

    with worker.worker_lock(("SCHEDULER",)) as development_lock:
        assert development_lock is True
        monkeypatch.setattr(worker.runtime, "database_path", test_database)
        with worker.worker_lock(("SCHEDULER",)) as test_lock:
            assert test_lock is True

    assert test_database.parent == settings.RUNTIME_DIR
    assert settings.RUNTIME_DIR.parent == settings.PYTEST_RUNTIME_ROOT


def test_http_guard_only_allows_loopback_marker_hosts():
    assert allowed_local_http_hosts(("localhost", "127.0.0.2", "::1")) == frozenset(
        {"localhost", "127.0.0.2", "::1"}
    )

    with pytest.raises(pytest.UsageError, match="loopback"):
        allowed_local_http_hosts(("model.example.test",))


def test_http_guard_fails_before_a_non_mock_transport_can_connect():
    class UnexpectedTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("HTTP 护栏不应把请求交给真实 transport")

    with httpx.Client(transport=UnexpectedTransport()) as client:
        with pytest.raises(pytest.fail.Exception) as exc_info:
            client.get("https://model.example.test/v1/chat?api_key=secret")

    message = str(exc_info.value)
    assert "GET model.example.test" in message
    assert "/v1/chat" not in message
    assert "api_key=secret" not in message


def test_http_guard_allows_httpx_mock_transport():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
    )

    assert client.get("https://model.example.test/v1/chat").status_code == 204
