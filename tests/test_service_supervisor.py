from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from service_supervisor.manager import Supervisor, SupervisorError
from service_supervisor.runtime import SupervisorLockedError


def test_supervisor_allows_one_owner_per_runtime(tmp_path):
    first = Supervisor(Path.cwd(), tmp_path)
    second = Supervisor(Path.cwd(), tmp_path)
    first.acquire()
    try:
        with pytest.raises(SupervisorLockedError):
            second.acquire()
    finally:
        first.release()


def test_supervisor_control_protocol_rejects_web_start_and_invalid_action(tmp_path):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor.acquire()
    try:
        with pytest.raises(SupervisorError, match="Web 只能"):
            supervisor.start("web")
        with pytest.raises(SupervisorError, match="不支持"):
            supervisor.control("web", "stop")
        with pytest.raises(SupervisorError, match="不支持"):
            supervisor.control("unknown", "start")
    finally:
        supervisor.release()


def test_supervisor_control_server_requires_token_and_reports_status(tmp_path):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor.acquire()
    try:
        supervisor.start_controller()
        endpoint = supervisor.store.read_json(supervisor.store.endpoint_path)
        url = f"http://{endpoint['host']}:{endpoint['port']}/v1/status"
        with httpx.Client(trust_env=False) as client:
            assert client.get(url).status_code == 401
        token = supervisor.store.token()
        with httpx.Client(trust_env=False) as client:
            response = client.get(url, headers={"X-Operations-Supervisor-Token": token})
        assert response.status_code == 200
        assert set(response.json()["services"]) == {
            "controlled_chrome",
            "web",
            "scheduler",
            "game_circle_session",
            "ingestion",
            "backfill_ingestion",
            "metric_sync",
            "review",
            "video_extraction",
            "ai_moderation",
        }
    finally:
        supervisor.release()


def test_web_health_check_retries_connection_refusals(tmp_path):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    with patch("service_supervisor.manager.time.monotonic", side_effect=(0, 0, 2)):
        with patch(
            "service_supervisor.manager.httpx.Client.get", side_effect=httpx.ConnectError("down")
        ):
            assert supervisor._web_healthy(timeout_seconds=1) is False


def test_reconcile_replaces_a_recorded_process_when_its_fixed_command_changes(
    tmp_path, monkeypatch
):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    state = {"desired": True, "pid": 123, "created_at": 1}
    calls = []

    monkeypatch.setattr(supervisor, "_read_state", lambda _service: state)
    monkeypatch.setattr(supervisor, "_is_process_current", lambda _service, _state: False)
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda service, received: calls.append(("terminate", service, received)),
    )
    monkeypatch.setattr(
        supervisor, "_launch", lambda service, received: calls.append(("launch", service, received))
    )

    supervisor.reconcile()

    assert ("terminate", "ingestion", state) in calls
    assert ("launch", "ingestion", state) in calls


def test_controlled_chrome_uses_fixed_loopback_debugging_and_dedicated_profile(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.write_text("", encoding="utf-8")
    monkeypatch.setattr("service_supervisor.manager.CONTROLLED_CHROME_BIN", str(chrome))
    monkeypatch.setattr("service_supervisor.manager.CONTROLLED_CHROME_USER_DATA_DIR", tmp_path / "profile")

    command = Supervisor(Path.cwd(), tmp_path).definitions()["controlled_chrome"].command

    assert command[0] == str(chrome)
    assert "--remote-debugging-address=127.0.0.1" in command
    assert any(part.startswith("--remote-debugging-port=") for part in command)
    assert any(part.startswith("--user-data-dir=") for part in command)


def test_controlled_chrome_is_started_as_a_default_supervisor_dependency(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    calls = []
    monkeypatch.setattr(supervisor, "acquire", lambda: None)
    monkeypatch.setattr(supervisor, "start_controller", lambda: None)
    monkeypatch.setattr(supervisor, "preflight", lambda: None)
    monkeypatch.setattr(supervisor, "reconcile", lambda: calls.append("reconcile"))
    monkeypatch.setattr(supervisor, "_web_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(supervisor, "_is_process_current", lambda *_args: True)

    supervisor.bootstrap()

    assert supervisor._read_state("controlled_chrome")["desired"] is True
    assert calls == ["reconcile"]
