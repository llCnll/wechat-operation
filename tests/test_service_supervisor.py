from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest

import service_supervisor.manager as supervisor_manager
import service_supervisor.runtime as supervisor_runtime
from service_supervisor.manager import Supervisor, SupervisorError
from service_supervisor.runtime import (
    RuntimeStore,
    SupervisorLeaseGuard,
    SupervisorLeaseOwnershipError,
    SupervisorLockedError,
)


def windows_replace_error(winerror: int, path: Path) -> PermissionError:
    error = PermissionError(13, "sharing conflict", str(path))
    error.winerror = winerror
    return error


def test_supervisor_allows_one_owner_per_runtime(tmp_path):
    first = Supervisor(Path.cwd(), tmp_path)
    second = Supervisor(Path.cwd(), tmp_path)
    first.acquire()
    try:
        with pytest.raises(SupervisorLockedError):
            second.acquire()
    finally:
        first.release()


def test_lock_conflict_then_shutdown_does_not_touch_the_running_supervisor(tmp_path, monkeypatch):
    first = Supervisor(Path.cwd(), tmp_path)
    second = Supervisor(Path.cwd(), tmp_path)
    calls = []
    first.acquire()
    monkeypatch.setattr(second, "_terminate", lambda service, _state: calls.append(service))
    try:
        with pytest.raises(SupervisorLockedError):
            second.acquire()
        second.shutdown()
    finally:
        first.release()

    assert calls == []


def test_shutdown_without_the_supervisor_lock_does_not_touch_existing_services(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    calls = []
    monkeypatch.setattr(supervisor, "_terminate", lambda service, _state: calls.append(service))

    supervisor.shutdown()

    assert calls == []


def test_sigterm_runs_the_supervisor_shutdown_path(tmp_path, monkeypatch):
    events = []
    registered_handlers = []

    class SignalDrivenSupervisor:
        def __init__(self, *_args):
            return None

        def bootstrap(self):
            events.append("bootstrap")

        def run_forever(self):
            registered_handlers[0](supervisor_manager.signal.SIGTERM, None)

        def shutdown(self):
            events.append("shutdown")

    monkeypatch.setattr(supervisor_manager, "Supervisor", SignalDrivenSupervisor)
    monkeypatch.setattr(supervisor_manager.signal, "getsignal", lambda _signal: "previous")
    monkeypatch.setattr(
        supervisor_manager.signal,
        "signal",
        lambda _signal, handler: registered_handlers.append(handler),
    )

    assert supervisor_manager.run_supervisor(Path.cwd(), tmp_path) == 0
    assert events == ["bootstrap", "shutdown"]
    assert callable(registered_handlers[0])
    assert registered_handlers[-1] == "previous"


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


@pytest.mark.allow_http_hosts("127.0.0.1")
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
                "public_opinion_reporting",
                "review",
            "video_extraction",
            "ai_moderation",
            "mute_maintenance",
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


@pytest.mark.skipif(supervisor_runtime.os.name != "nt", reason="Windows 共享锁重试")
@pytest.mark.parametrize("winerror", (5, 32))
def test_write_json_retries_windows_sharing_errors(tmp_path, monkeypatch, winerror):
    store = RuntimeStore(tmp_path)
    store.ensure()
    path = store.instance_lease_path
    original_replace = supervisor_runtime.os.replace
    failures = 0

    def flaky_replace(source, destination):
        nonlocal failures
        failures += 1
        if failures <= 2:
            raise windows_replace_error(winerror, Path(destination))
        return original_replace(source, destination)

    monkeypatch.setattr(supervisor_runtime.os, "replace", flaky_replace)
    monkeypatch.setattr(supervisor_runtime.time, "sleep", lambda _seconds: None)

    store.write_json(path, {"version": 1})

    assert failures == 3
    assert store.read_json(path) == {"version": 1}
    assert list(path.parent.glob("tmp*")) == []


@pytest.mark.skipif(supervisor_runtime.os.name != "nt", reason="Windows 共享锁重试")
def test_write_json_cleans_temporary_file_after_exhausted_windows_retry(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    store.ensure()
    path = store.instance_lease_path
    failure = windows_replace_error(5, path)
    clock_values = iter((0.0, 0.0, supervisor_runtime.ATOMIC_WRITE_RETRY_SECONDS))

    monkeypatch.setattr(
        supervisor_runtime.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(supervisor_runtime.time, "monotonic", lambda: next(clock_values))
    monkeypatch.setattr(supervisor_runtime.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError) as raised:
        store.write_json(path, {"version": 1})

    assert raised.value is failure
    assert failure.runtime_write_attempts == 2
    assert list(path.parent.glob("tmp*")) == []


def test_write_json_cleans_temporary_file_after_nonretryable_replace_failure(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    store.ensure()
    path = store.instance_lease_path
    failure = OSError("runtime unavailable")

    monkeypatch.setattr(
        supervisor_runtime.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(OSError) as raised:
        store.write_json(path, {"version": 1})

    assert raised.value is failure
    assert list(path.parent.glob("tmp*")) == []


def test_supervisor_lease_guard_stops_after_instance_replacement(tmp_path):
    store = RuntimeStore(tmp_path)
    store.ensure()
    store.write_instance_lease("current")
    guard = SupervisorLeaseGuard(store, "ingestion", "current")

    assert guard.check_and_heartbeat() is True
    assert store.read_json(store.heartbeat_path("ingestion"))["instance_id"] == "current"

    store.write_instance_lease("replacement")

    assert guard.check_and_heartbeat() is False


def test_revoke_instance_lease_immediately_expires_only_the_current_owner(tmp_path):
    store = RuntimeStore(tmp_path)
    store.ensure()
    store.write_instance_lease("current")

    with pytest.raises(SupervisorLeaseOwnershipError):
        store.revoke_instance_lease("other")

    store.revoke_instance_lease("current")
    lease = store.read_instance_lease()
    assert lease["instance_id"] == "current"
    assert lease["expires_at_epoch"] <= supervisor_runtime.time.time()


def test_runtime_store_keeps_only_valid_bounded_process_and_event_records(tmp_path):
    store = RuntimeStore(tmp_path)
    store.ensure()
    record = {
        "service": "ingestion",
        "pid": 123,
        "created_at": 1.0,
        "instance_id": "instance",
        "generation": 1,
    }
    store.write_process_registry([record])
    assert store.read_process_registry() == [record]

    with pytest.raises(ValueError):
        store.write_process_registry([{**record, "command": "sensitive"}])
    with pytest.raises(ValueError):
        store.append_lifecycle_event(
            service="ingestion",
            event="STARTED",
            reason="not-a-fixed-code",
            generation=1,
            outcome="COMPLETED",
        )

    for generation in range(supervisor_runtime.SUPERVISOR_EVENT_BUFFER_LIMIT + 1):
        store.append_lifecycle_event(
            service="ingestion",
            event="STARTED",
            reason="DESIRED_START",
            generation=generation,
            outcome="COMPLETED",
        )
    events = store.read_lifecycle_events()
    assert len(events) == supervisor_runtime.SUPERVISOR_EVENT_BUFFER_LIMIT
    assert events[0]["generation"] == 1
    assert {key for event in events for key in event}.isdisjoint(
        {"pid", "instance_id", "token", "cookie", "command", "url"}
    )


def test_process_recovery_requires_exact_argv_and_project_working_directory(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    command = supervisor.definitions()["ingestion"].command

    class Process:
        def __init__(self, _pid):
            return None

        def is_running(self):
            return True

        def create_time(self):
            return 1.0

        def cmdline(self):
            return list(command)

        def cwd(self):
            return str(Path.cwd())

    record = {
        "service": "ingestion",
        "pid": 123,
        "created_at": 1.0,
        "instance_id": "instance",
        "generation": 1,
    }
    monkeypatch.setattr(supervisor_manager.psutil, "Process", Process)
    assert supervisor._registered_process_matches(record) is True

    monkeypatch.setattr(
        Process,
        "cmdline",
        lambda _self: [*command[:-1], "--not-the-fixed-loop"],
    )
    assert supervisor._registered_process_matches(record) is False
    monkeypatch.setattr(Process, "cmdline", lambda _self: list(command))
    monkeypatch.setattr(Process, "cwd", lambda _self: str(tmp_path))
    assert supervisor._registered_process_matches(record) is False


def test_shutdown_reclaims_every_registered_process_for_the_current_instance(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor._instance_id = "current"
    supervisor._lock = object()
    records = [
        {
            "service": "ingestion",
            "pid": 101,
            "created_at": 1.0,
            "instance_id": "current",
            "generation": 1,
        },
        {
            "service": "ingestion",
            "pid": 102,
            "created_at": 2.0,
            "instance_id": "current",
            "generation": 2,
        },
    ]
    reclaimed = []
    monkeypatch.setattr(supervisor.store, "read_process_registry", lambda: records)
    monkeypatch.setattr(
        supervisor,
        "_terminate_registered_process",
        lambda record, *, reason: reclaimed.append((record["generation"], reason)),
    )
    monkeypatch.setattr(supervisor, "_revoke_current_lease", lambda: None)
    monkeypatch.setattr(supervisor, "_read_state", lambda _service: {"desired": False})
    monkeypatch.setattr(supervisor, "release", lambda: None)

    supervisor.shutdown()

    assert reclaimed == [(1, "GRACEFUL_SHUTDOWN"), (2, "GRACEFUL_SHUTDOWN")]


def test_supervisor_lease_guard_stops_after_repeated_heartbeat_write_failures(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    store.ensure()
    store.write_instance_lease("current")
    guard = SupervisorLeaseGuard(store, "ingestion", "current")
    monkeypatch.setattr(
        store,
        "write_heartbeat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("runtime unavailable")),
    )

    assert guard.check_and_heartbeat() is True
    assert guard.check_and_heartbeat() is False


def test_run_forever_recovers_after_a_transient_lease_renewal_failure(
    tmp_path, monkeypatch, caplog
):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor._instance_id = "current"
    renew = Mock(
        side_effect=(OSError("runtime unavailable"), OSError("runtime unavailable"), None)
    )
    reconcile = Mock()
    shutdown = Mock()

    class StopAfterRecovery:
        def __init__(self):
            self.results = iter((False, False, False, True))

        def wait(self, _seconds):
            return next(self.results)

    supervisor._stopping = StopAfterRecovery()
    monkeypatch.setattr(supervisor, "_renew_lease", renew)
    monkeypatch.setattr(supervisor, "reconcile", reconcile)
    monkeypatch.setattr(supervisor, "shutdown", shutdown)

    supervisor.run_forever()

    assert renew.call_count == 3
    assert reconcile.call_count == 1
    assert shutdown.call_count == 0
    assert "Supervisor 租约续租失败" in caplog.text
    assert "Supervisor 租约续租已恢复" in caplog.text
    assert "failure_count=2" in caplog.text


def test_run_forever_rate_limits_persistent_lease_renewal_failure_logs(
    tmp_path, monkeypatch
):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor._instance_id = "current"
    renew = Mock(side_effect=OSError("runtime unavailable"))
    reconcile = Mock()
    logged = Mock()

    class StopAfterFailures:
        def __init__(self):
            self.results = iter((False, False, False, True))

        def wait(self, _seconds):
            return next(self.results)

    supervisor._stopping = StopAfterFailures()
    monkeypatch.setattr(supervisor, "_renew_lease", renew)
    monkeypatch.setattr(supervisor, "reconcile", reconcile)
    monkeypatch.setattr(supervisor_manager.logger, "warning", logged)
    monkeypatch.setattr(
        supervisor_manager.time,
        "monotonic",
        Mock(side_effect=(0.0, 30.0, 60.0)),
    )

    supervisor.run_forever()

    assert renew.call_count == 3
    assert reconcile.call_count == 0
    assert logged.call_count == 2
    assert logged.call_args_list[0].args[-1] == 1
    assert logged.call_args_list[1].args[-1] == 3


def test_run_forever_preserves_the_existing_lease_during_persistent_failures(
    tmp_path, monkeypatch
):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor.store.ensure()
    supervisor._instance_id = "current"
    supervisor.store.write_instance_lease("current")
    lease_before = supervisor.store.read_instance_lease()
    reconcile = Mock()

    class StopAfterFailures:
        def __init__(self):
            self.results = iter((False, False, True))

        def wait(self, _seconds):
            return next(self.results)

    supervisor._stopping = StopAfterFailures()
    monkeypatch.setattr(
        supervisor,
        "_renew_lease",
        lambda: (_ for _ in ()).throw(OSError("runtime unavailable")),
    )
    monkeypatch.setattr(supervisor, "reconcile", reconcile)

    supervisor.run_forever()

    assert supervisor.store.read_instance_lease() == lease_before
    assert reconcile.call_count == 0
    supervisor.store.write_json(
        supervisor.store.instance_lease_path,
        {"version": 1, "instance_id": "current", "expires_at_epoch": 0},
    )
    guard = SupervisorLeaseGuard(supervisor.store, "ingestion", "current")
    assert guard.check_and_heartbeat() is False


def test_bootstrap_terminates_identifiable_legacy_services_before_preflight(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    supervisor._instance_id = "current"
    states = {
        service: {"desired": False}
        for service in (
            "controlled_chrome", "web", "scheduler", "game_circle_session", "ingestion",
                "backfill_ingestion", "metric_sync", "public_opinion_reporting", "review", "video_extraction", "ai_moderation",
            "mute_maintenance",
        )
    }
    states["web"] = {"desired": True, "pid": 100, "created_at": 1}
    states["ingestion"] = {
        "desired": True,
        "pid": 101,
        "created_at": 1,
        "instance_id": "previous",
    }
    calls = []

    monkeypatch.setattr(supervisor, "_read_state", lambda service: states[service])
    monkeypatch.setattr(supervisor, "_is_recorded_process", lambda state: state.get("pid") is not None)
    monkeypatch.setattr(
        supervisor,
        "_terminate",
        lambda service, state: (calls.append(("terminate", service)), state.update(pid=None)),
    )
    monkeypatch.setattr(supervisor, "_write_state", lambda service, _state: calls.append(("write", service)))

    supervisor._wait_for_previous_services()

    assert calls == [
        ("terminate", "ingestion"),
        ("write", "ingestion"),
        ("terminate", "web"),
        ("write", "web"),
    ]


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
    monkeypatch.setattr(supervisor, "_wait_for_chrome_generation", lambda: "test-generation")

    supervisor.reconcile()

    assert ("terminate", "ingestion", state) in calls
    assert ("launch", "ingestion", state) in calls


def test_reconcile_replaces_session_owner_when_controlled_chrome_is_lost(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    states = {
        service: {"desired": False}
        for service in (
            "controlled_chrome", "web", "scheduler", "game_circle_session", "ingestion",
                "backfill_ingestion", "metric_sync", "public_opinion_reporting", "review", "video_extraction", "ai_moderation",
            "mute_maintenance",
        )
    }
    states["controlled_chrome"] = {"desired": True, "pid": 101, "created_at": 1}
    states["game_circle_session"] = {"desired": True, "pid": 102, "created_at": 1}
    current = {"controlled_chrome": False, "game_circle_session": True}
    calls = []

    monkeypatch.setattr(supervisor, "_read_state", lambda service: states[service])
    monkeypatch.setattr(supervisor, "_is_process_current", lambda service, _state: current.get(service, False))

    def replace(state):
        calls.append(state)
        current["controlled_chrome"] = True
        current["game_circle_session"] = True

    monkeypatch.setattr(supervisor, "_replace_game_circle_session", replace)

    supervisor.reconcile()

    assert calls == [states["game_circle_session"]]


def test_chrome_generation_keeps_service_healthy_after_launcher_pid_handoff(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    state = {"browser_generation": "stable", "pid": 1, "created_at": 1}
    monkeypatch.setattr("service_supervisor.manager.CdpBrowser.browser_generation", lambda _self: "stable")
    monkeypatch.setattr(supervisor, "_is_recorded_process", lambda _state: False)

    assert supervisor._is_process_current("controlled_chrome", state) is True


def test_chrome_port_cleanup_accepts_browser_close_connection_drop(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    calls = []
    listening = iter((True, False))
    monkeypatch.setattr("service_supervisor.manager.CdpBrowser.close_browser", lambda _self: calls.append("close"))
    monkeypatch.setattr(supervisor, "_chrome_port_listening", lambda: next(listening))

    supervisor._cleanup_chrome_port()

    assert calls == ["close"]


def test_controlled_chrome_restart_replaces_session_owner_when_session_is_desired(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    states = {
        "controlled_chrome": {"desired": True, "pid": 101, "created_at": 1},
        "game_circle_session": {"desired": True, "pid": 102, "created_at": 1},
    }
    calls = []
    monkeypatch.setattr(supervisor, "_read_state", lambda service: states[service])
    monkeypatch.setattr(supervisor, "_replace_game_circle_session", lambda state: calls.append(state))

    supervisor.restart("controlled_chrome")

    assert calls == [states["game_circle_session"]]


def test_session_restart_is_submitted_asynchronously(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    calls = []
    monkeypatch.setattr(supervisor, "_schedule_restart", lambda service: calls.append(service))

    result = supervisor.control("game_circle_session", "restart")

    assert result == {"service": "game_circle_session", "state": "restarting"}
    assert calls == ["game_circle_session"]


def test_starting_session_service_uses_the_paired_owner_replacement(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    state = {"desired": False}
    calls = []
    monkeypatch.setattr(supervisor, "_read_state", lambda _service: state)
    monkeypatch.setattr(supervisor, "_write_state", lambda service, received: calls.append(("write", service, received["desired"])))
    monkeypatch.setattr(supervisor, "_replace_game_circle_session", lambda received: calls.append(("replace", received["desired"])))

    supervisor.start("game_circle_session")

    assert calls == [("write", "game_circle_session", True), ("replace", True)]


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


def test_default_supervisor_services_are_started_on_bootstrap(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    calls = []
    monkeypatch.setattr(supervisor, "acquire", lambda: None)
    monkeypatch.setattr(supervisor, "start_controller", lambda: None)
    monkeypatch.setattr(supervisor, "preflight", lambda: None)
    monkeypatch.setattr(supervisor, "_cleanup_chrome_port", lambda: None)
    monkeypatch.setattr(supervisor, "reconcile", lambda: calls.append("reconcile"))
    monkeypatch.setattr(supervisor, "_web_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(supervisor, "_is_process_current", lambda *_args: True)

    supervisor.bootstrap()

    assert supervisor._read_state("controlled_chrome")["desired"] is True
    assert supervisor._read_state("web")["desired"] is True
    assert supervisor._read_state("mute_maintenance")["desired"] is True
    assert calls == ["reconcile"]


def test_replacing_game_circle_session_retires_chrome_generation_first(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    states = {
        "game_circle_session": {"desired": True, "pid": 101, "created_at": 1},
        "controlled_chrome": {"desired": True, "pid": 102, "created_at": 1},
    }
    calls = []

    monkeypatch.setattr(supervisor, "_read_state", lambda service: states[service])
    monkeypatch.setattr(supervisor, "_write_state", lambda service, state: calls.append(("write", service)))
    monkeypatch.setattr(supervisor, "_terminate", lambda service, state: calls.append(("terminate", service)))
    monkeypatch.setattr(supervisor, "_launch", lambda service, state: calls.append(("launch", service)))
    monkeypatch.setattr(supervisor, "_wait_for_chrome_generation", lambda: "new-generation")

    supervisor._replace_game_circle_session(states["game_circle_session"])

    assert calls == [
        ("terminate", "game_circle_session"),
        ("write", "game_circle_session"),
        ("terminate", "controlled_chrome"),
        ("write", "controlled_chrome"),
        ("launch", "controlled_chrome"),
        ("write", "game_circle_session"),
        ("write", "controlled_chrome"),
        ("launch", "game_circle_session"),
    ]


def test_chrome_start_timeout_keeps_the_replacement_generation_for_the_next_reconcile(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    states = {
        "game_circle_session": {"desired": True, "pid": 101, "created_at": 1},
        "controlled_chrome": {"desired": True, "pid": 102, "created_at": 1},
    }
    calls = []
    readiness = iter((SupervisorError("not ready"), "generation"))

    monkeypatch.setattr(supervisor, "_read_state", lambda service: states[service])
    monkeypatch.setattr(supervisor, "_write_state", lambda service, state: calls.append(("write", service)))
    monkeypatch.setattr(supervisor, "_terminate", lambda service, state: calls.append(("terminate", service)))
    monkeypatch.setattr(supervisor, "_launch", lambda service, state: calls.append(("launch", service)))
    def wait_for_chrome_generation():
        outcome = next(readiness)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(supervisor, "_wait_for_chrome_generation", wait_for_chrome_generation)
    monkeypatch.setattr(supervisor, "_is_process_current", lambda service, _state: service == "controlled_chrome")

    assert supervisor._replace_game_circle_session(states["game_circle_session"]) is False
    assert supervisor._replace_game_circle_session(states["game_circle_session"]) is True

    assert calls.count(("terminate", "controlled_chrome")) == 1
    assert calls.count(("launch", "controlled_chrome")) == 1
    assert calls.count(("launch", "game_circle_session")) == 1


def test_chrome_recovery_circuit_opens_after_three_failures(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    states = {"game_circle_session": {"desired": True}, "controlled_chrome": {"desired": True}}
    monkeypatch.setattr(supervisor, "_read_state", lambda service: states[service])
    monkeypatch.setattr(supervisor, "_write_state", lambda _service, _state: None)
    monkeypatch.setattr(supervisor, "_terminate", lambda _service, _state: None)
    monkeypatch.setattr(supervisor, "_launch", lambda _service, _state: None)
    monkeypatch.setattr(supervisor, "_is_process_current", lambda _service, _state: True)
    monkeypatch.setattr(
        supervisor,
        "_wait_for_chrome_generation",
        lambda: (_ for _ in ()).throw(SupervisorError("not ready")),
    )

    assert supervisor._replace_game_circle_session(states["game_circle_session"]) is False
    assert supervisor._replace_game_circle_session(states["game_circle_session"]) is False
    assert supervisor._replace_game_circle_session(states["game_circle_session"]) is False

    assert states["game_circle_session"]["replacement_phase"] == "CIRCUIT_OPEN"
    assert states["controlled_chrome"]["recovery_failures"] == 3


def test_shutdown_stops_services_in_dependency_order_without_clearing_desired(tmp_path, monkeypatch):
    supervisor = Supervisor(Path.cwd(), tmp_path)
    calls = []
    supervisor._instance_id = "current"
    supervisor._lock = object()
    monkeypatch.setattr(
        supervisor, "_read_state", lambda _service: {"desired": True, "instance_id": "current"}
    )
    monkeypatch.setattr(supervisor, "_terminate", lambda service, _state: calls.append(service))
    monkeypatch.setattr(supervisor, "_write_state", lambda _service, _state: None)
    monkeypatch.setattr(supervisor, "release", lambda: None)

    supervisor.shutdown()

    assert calls == [
        "ai_moderation", "video_extraction", "review", "metric_sync", "public_opinion_reporting", "backfill_ingestion",
        "ingestion", "mute_maintenance", "scheduler", "game_circle_session",
        "controlled_chrome", "web",
    ]
