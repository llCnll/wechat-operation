"""Supervisor process lifecycle and loopback-only control protocol."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import psutil

from config.cdp import CdpBrowser, CdpUnavailableError
from config.settings import (
    CONTROLLED_CHROME_BIN,
    CONTROLLED_CHROME_DEBUG_PORT,
    CONTROLLED_CHROME_USER_DATA_DIR,
)

from .runtime import (
    RuntimeStore,
    SupervisorLockedError,
    utc_now,
)

SERVICE_NAMES = (
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
WORKER_NAMES = (
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
CONTROLLABLE_SERVICE_NAMES = ("controlled_chrome", *WORKER_NAMES)
CONTROL_VERSION = "v1"
WEB_PORT = 8000
MAX_LOG_BYTES = 1_000_000
SESSION_REPLACEMENT_WAITING_FOR_CHROME = "WAITING_FOR_CHROME"
SESSION_REPLACEMENT_CIRCUIT_OPEN = "CIRCUIT_OPEN"
CHROME_RECOVERY_FAILURE_LIMIT = 3
PREVIOUS_SERVICE_WAIT_SECONDS = 10
SERVICE_SHUTDOWN_ORDER = (
    "ai_moderation",
    "video_extraction",
    "review",
    "metric_sync",
    "public_opinion_reporting",
    "backfill_ingestion",
    "ingestion",
    "mute_maintenance",
    "scheduler",
    "game_circle_session",
    "controlled_chrome",
    "web",
)


class SupervisorError(RuntimeError):
    """A safe-to-display Supervisor failure."""


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    command: tuple[str, ...]
    allowed_actions: frozenset[str]


def redact(value: str) -> str:
    lowered = value.lower()
    if any(term in lowered for term in ("token=", "cookie", "authorization", "openid", "api_key")):
        return "[已脱敏的日志消息]"
    return value


class Supervisor:
    """Own a fixed set of child processes and expose a small local protocol."""

    def __init__(self, project_root: Path, runtime_dir: Path, *, web_port: int = WEB_PORT):
        self.project_root = project_root
        self.runtime_dir = runtime_dir
        self.web_port = web_port
        self.store = RuntimeStore(runtime_dir)
        self._lock = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._scheduled_restarts: set[str] = set()
        self._stopping = threading.Event()
        self._log_handles: dict[str, Any] = {}
        self._instance_id = ""

    def definitions(self) -> dict[str, ServiceDefinition]:
        python = sys.executable
        manage = str(self.project_root / "manage.py")
        return {
            "controlled_chrome": ServiceDefinition(
                "controlled_chrome",
                self._controlled_chrome_command(),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "web": ServiceDefinition(
                "web",
                (python, manage, "runserver", f"127.0.0.1:{self.web_port}", "--noreload"),
                frozenset(("status", "logs", "restart")),
            ),
            "scheduler": ServiceDefinition(
                "scheduler",
                (python, manage, "run_scheduler", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "game_circle_session": ServiceDefinition(
                "game_circle_session",
                (python, manage, "run_game_circle_session_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "ingestion": ServiceDefinition(
                "ingestion",
                (python, manage, "run_realtime_ingestion_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "backfill_ingestion": ServiceDefinition(
                "backfill_ingestion",
                (python, manage, "run_backfill_ingestion_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "metric_sync": ServiceDefinition(
                "metric_sync",
                (python, manage, "run_metric_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "public_opinion_reporting": ServiceDefinition(
                "public_opinion_reporting",
                (python, manage, "run_public_opinion_reporting_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "review": ServiceDefinition(
                "review",
                (python, manage, "run_review_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "video_extraction": ServiceDefinition(
                "video_extraction",
                (python, manage, "run_video_extraction_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "ai_moderation": ServiceDefinition(
                "ai_moderation",
                (python, manage, "run_ai_moderation_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
            "mute_maintenance": ServiceDefinition(
                "mute_maintenance",
                (python, manage, "run_mute_maintenance_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
        }

    def _controlled_chrome_command(self) -> tuple[str, ...]:
        """Build the fixed cross-platform command for the Supervisor-owned browser."""
        executable = self._find_controlled_chrome()
        profile = CONTROLLED_CHROME_USER_DATA_DIR
        profile.mkdir(parents=True, exist_ok=True)
        return (
            executable,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={CONTROLLED_CHROME_DEBUG_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        )

    @staticmethod
    def _find_controlled_chrome() -> str:
        """Resolve an explicit executable first, then a small platform-specific safe list."""
        configured = CONTROLLED_CHROME_BIN.strip()
        if configured:
            candidate = Path(configured)
            if candidate.is_file():
                return str(candidate)
            resolved = shutil.which(configured)
            if resolved:
                return resolved
            raise SupervisorError("受控 Chrome 可执行文件不存在；请检查 CONTROLLED_CHROME_BIN")

        candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        for name in ("google-chrome", "chromium", "chromium-browser"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
        raise SupervisorError("未找到受控 Chrome；请在 runtime/.env 配置 CONTROLLED_CHROME_BIN")

    def acquire(self) -> None:
        from .runtime import ExclusiveFileLock

        self.store.ensure()
        lock = ExclusiveFileLock(self.store.lock_path)
        lock.acquire()
        self._lock = lock
        self._instance_id = secrets.token_urlsafe(24)
        self.store.write_instance_lease(self._instance_id)

    def release(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for handle in self._log_handles.values():
            handle.close()
        self._log_handles.clear()
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def preflight(self) -> None:
        self.store.ensure()
        log_path = self.store.log_path("preflight")
        self._rotate_log(log_path)
        with log_path.open("a", encoding="utf-8") as output:
            commands = (
                ("migrate",),
                ("collectstatic", "--noinput"),
                ("ensure_default_policy",),
            )
            for command in commands:
                result = subprocess.run(
                    [sys.executable, str(self.project_root / "manage.py"), *command],
                    cwd=self.project_root,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=False,
                    text=True,
                )
                if result.returncode:
                    raise SupervisorError(f"启动预检失败：{' '.join(command)}，请查看预检日志")

    def start_controller(self) -> None:
        supervisor = self
        # Generate the file before Django attempts its first authenticated status request.
        self.store.token()
        self.store.game_circle_ipc_token()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):  # pragma: no cover - deliberately quiet.
                return

            def _authorized(self) -> bool:
                presented = self.headers.get("X-Operations-Supervisor-Token", "")
                return hmac.compare_digest(presented, supervisor.store.token())

            def _json(self, status: HTTPStatus, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "未授权"})
                    return
                parsed = urlparse(self.path)
                if parsed.path == f"/{CONTROL_VERSION}/status":
                    self._json(HTTPStatus.OK, {"services": supervisor.status()})
                    return
                if parsed.path.startswith(f"/{CONTROL_VERSION}/logs/"):
                    service = parsed.path.rsplit("/", 1)[-1]
                    lines = int(parse_qs(parsed.query).get("lines", ["80"])[0])
                    self._json(
                        HTTPStatus.OK,
                        {"service": service, "lines": supervisor.logs(service, lines)},
                    )
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "未知接口"})

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "未授权"})
                    return
                parts = urlparse(self.path).path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != [CONTROL_VERSION, "services"]:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "未知接口"})
                    return
                try:
                    result = supervisor.control(parts[2], parts[3])
                except SupervisorError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._json(HTTPStatus.ACCEPTED, result)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address[:2]
        self.store.write_json(
            self.store.endpoint_path,
            {"host": host, "port": port, "version": CONTROL_VERSION},
        )
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

    def bootstrap(self) -> None:
        self.acquire()
        try:
            self._wait_for_previous_services()
            self.start_controller()
            self.preflight()
            self._cleanup_chrome_port()
            for service in (
                "controlled_chrome",
                "web",
                "mute_maintenance",
                "public_opinion_reporting",
            ):
                state = self._read_state(service)
                state["desired"] = True
                self._write_state(service, state)
            self.reconcile()
            if not self._web_healthy(timeout_seconds=20) or not self._is_process_current(
                "web", self._read_state("web")
            ):
                raise SupervisorError("Web 服务未在 20 秒内通过健康检查")
        except Exception:
            state = self._read_state("web")
            self._terminate("web", state)
            self._write_state("web", state)
            self.release()
            raise

    def run_forever(self) -> None:
        while not self._stopping.wait(2):
            self._renew_lease()
            self.reconcile()

    def shutdown(self) -> None:
        self._stopping.set()
        if self._lock is not None:
            with self._state_lock:
                for service in SERVICE_SHUTDOWN_ORDER:
                    state = self._read_state(service)
                    if state.get("instance_id") != self._instance_id:
                        continue
                    self._terminate(service, state)
                    self._write_state(service, state)
        self.release()

    def reconcile(self) -> None:
        with self._state_lock:
            session_owner_reconciled = False
            for service in SERVICE_NAMES:
                state = self._read_state(service)
                running = self._is_process_current(service, state)
                if state.get("desired") and not running:
                    if service == "controlled_chrome" and self._read_state(
                        "game_circle_session"
                    ).get("desired"):
                        # A new browser generation invalidates every in-memory lease
                        # owned by the still-running session Worker.  Replacing only
                        # Chrome would leave that owner unable to publish a new Frame.
                        self._replace_game_circle_session(self._read_state("game_circle_session"))
                        session_owner_reconciled = True
                    elif service == "game_circle_session" and not session_owner_reconciled:
                        self._replace_game_circle_session(state)
                    else:
                        self._terminate(service, state)
                        self._launch(service, state)

    def status(self) -> dict[str, dict]:
        with self._state_lock:
            status = {}
            for service in SERVICE_NAMES:
                state = self._read_state(service)
                running = self._is_process_current(service, state)
                heartbeat = self.store.read_json(self.store.heartbeat_path(service), {})
                status[service] = {
                    "running": running,
                    "desired": bool(state.get("desired")),
                    "started_at": state.get("started_at"),
                    "exit_code": state.get("exit_code"),
                    "restart_count": state.get("restart_count", 0),
                    "failure_reason": state.get("failure_reason"),
                    "heartbeat_at": heartbeat.get("at"),
                    "log_path": str(self.store.log_path(service)),
                }
            return status

    def control(self, service: str, action: str) -> dict:
        definitions = self.definitions()
        if service not in definitions or action not in definitions[service].allowed_actions:
            raise SupervisorError("不支持该服务操作")
        if action == "status":
            return {"service": service, "status": self.status()[service]}
        if action == "logs":
            return {"service": service, "lines": self.logs(service, 80)}
        if service in {"web", "game_circle_session"} and action == "restart":
            self._schedule_restart(service)
            return {"service": service, "state": "restarting"}
        if action == "start":
            self.start(service)
        elif action == "stop":
            self.stop(service)
        elif action == "restart":
            self.restart(service)
        return {"service": service, "status": self.status()[service]}

    def start(self, service: str) -> None:
        if service not in CONTROLLABLE_SERVICE_NAMES:
            raise SupervisorError("Web 只能通过重启操作恢复")
        with self._state_lock:
            if service == "game_circle_session":
                # Starting a session service establishes a new paired owner, not
                # an unverified Worker on whichever Chrome happens to be running.
                state = self._read_state(service)
                state["desired"] = True
                self._write_state(service, state)
                self._replace_game_circle_session(state)
                return
            state = self._read_state(service)
            state["desired"] = True
            self._write_state(service, state)
            if not self._is_process_current(service, state):
                self._launch(service, state)

    def stop(self, service: str) -> None:
        if service not in CONTROLLABLE_SERVICE_NAMES:
            raise SupervisorError("Web 不能通过页面停止")
        with self._state_lock:
            state = self._read_state(service)
            state["desired"] = False
            self._terminate(service, state)
            self._write_state(service, state)
            if service == "game_circle_session":
                chrome_state = self._read_state("controlled_chrome")
                chrome_state["desired"] = False
                self._terminate("controlled_chrome", chrome_state)
                self._write_state("controlled_chrome", chrome_state)

    def restart(self, service: str) -> None:
        with self._state_lock:
            state = self._read_state(service)
            state["desired"] = True
            if service == "game_circle_session" or (
                service == "controlled_chrome"
                and self._read_state("game_circle_session").get("desired")
            ):
                session_state = (
                    state
                    if service == "game_circle_session"
                    else self._read_state("game_circle_session")
                )
                session_state.pop("replacement_phase", None)
                session_state.pop("failure_reason", None)
                chrome_state = self._read_state("controlled_chrome")
                chrome_state["recovery_failures"] = 0
                self._write_state("controlled_chrome", chrome_state)
                self._replace_game_circle_session(session_state)
            else:
                self._terminate(service, state)
                self._write_state(service, state)
                self._launch(service, state)

    def _schedule_restart(self, service: str) -> None:
        """Accept a long restart promptly and collapse duplicate control clicks."""
        with self._state_lock:
            if service in self._scheduled_restarts:
                return
            self._scheduled_restarts.add(service)

        def run() -> None:
            try:
                self.restart(service)
            finally:
                with self._state_lock:
                    self._scheduled_restarts.discard(service)

        threading.Thread(target=run, name=f"supervisor-restart-{service}", daemon=True).start()

    def _replace_game_circle_session(self, session_state: dict) -> bool:
        """Advance the paired Chrome/session-owner replacement state machine."""
        session_state["desired"] = True
        chrome_state = self._read_state("controlled_chrome")
        chrome_state["desired"] = True
        if session_state.get("replacement_phase") == SESSION_REPLACEMENT_CIRCUIT_OPEN:
            return False
        if session_state.get("replacement_phase") != SESSION_REPLACEMENT_WAITING_FOR_CHROME:
            # A new owner must never inherit a Chrome generation.  This is the
            # one destructive transition; later retries only wait for the same
            # replacement generation rather than repeatedly closing it.
            self._terminate("game_circle_session", session_state)
            self._write_state("game_circle_session", session_state)
            self._terminate("controlled_chrome", chrome_state)
            self._write_state("controlled_chrome", chrome_state)
            self._launch("controlled_chrome", chrome_state)
            session_state["replacement_phase"] = SESSION_REPLACEMENT_WAITING_FOR_CHROME
            self._write_state("game_circle_session", session_state)
        elif not self._is_process_current("controlled_chrome", chrome_state):
            self._launch("controlled_chrome", chrome_state)
        try:
            generation = self._wait_for_chrome_generation()
        except SupervisorError:
            failures = int(chrome_state.get("recovery_failures", 0)) + 1
            chrome_state["recovery_failures"] = failures
            self._write_state("controlled_chrome", chrome_state)
            if failures >= CHROME_RECOVERY_FAILURE_LIMIT:
                session_state["replacement_phase"] = SESSION_REPLACEMENT_CIRCUIT_OPEN
                session_state["failure_reason"] = "CHROME_RECOVERY_CIRCUIT_OPEN"
            else:
                session_state["failure_reason"] = "CHROME_START_TIMEOUT"
            self._write_state("game_circle_session", session_state)
            return False

        chrome_state["browser_generation"] = generation
        chrome_state["recovery_failures"] = 0
        self._write_state("controlled_chrome", chrome_state)
        session_state.pop("replacement_phase", None)
        session_state.pop("failure_reason", None)
        self._launch("game_circle_session", session_state)
        return True

    @staticmethod
    def _wait_for_chrome_generation(timeout_seconds: float = 15) -> str:
        """Wait for the replacement browser's CDP endpoint before assigning a session owner."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                return CdpBrowser(timeout=1).browser_generation()
            except CdpUnavailableError:
                time.sleep(0.2)
        raise SupervisorError("受控 Chrome 未在限定时间内就绪")

    def logs(self, service: str, lines: int) -> list[str]:
        if service not in SERVICE_NAMES:
            raise SupervisorError("未知服务")
        safe_lines = max(1, min(lines, 200))
        try:
            content = (
                self.store.log_path(service)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except FileNotFoundError:
            return []
        return [redact(line) for line in content[-safe_lines:]]

    def _read_state(self, service: str) -> dict:
        return self.store.read_json(self.store.service_path(service), {}) or {}

    def _write_state(self, service: str, state: dict) -> None:
        state["updated_at"] = utc_now()
        self.store.write_json(self.store.service_path(service), state)

    def _launch(self, service: str, state: dict) -> None:
        definition = self.definitions()[service]
        log_path = self.store.log_path(service)
        self._rotate_log(log_path)
        handle = log_path.open("a", encoding="utf-8")
        previous_handle = self._log_handles.pop(service, None)
        if previous_handle is not None:
            previous_handle.close()
        self._log_handles[service] = handle
        environment = {
            **os.environ,
            "OPERATION_SUPERVISOR_SERVICE": service,
            "OPERATION_RUNTIME_DIR": str(self.runtime_dir),
            "OPERATION_SUPERVISOR_INSTANCE": self._instance_id,
            "OPERATION_GAME_CIRCLE_IPC_CAPABILITY": self.store.game_circle_ipc_token(),
        }
        process = subprocess.Popen(
            definition.command,
            cwd=self.project_root,
            env=environment,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process_info = psutil.Process(process.pid)
        state.update(
            {
                "desired": True,
                "pid": process.pid,
                "created_at": process_info.create_time(),
                "started_at": utc_now(),
                "command": list(definition.command),
                "exit_code": None,
                "restart_count": int(state.get("restart_count", 0)) + 1,
                "instance_id": self._instance_id,
            }
        )
        state.pop("failure_reason", None)
        if service == "controlled_chrome":
            state.pop("browser_generation", None)
            try:
                state["browser_generation"] = self._wait_for_chrome_generation()
            except SupervisorError:
                state["failure_reason"] = "CHROME_START_TIMEOUT"
        self._write_state(service, state)

    def _terminate(self, service: str, state: dict) -> None:
        if service == "controlled_chrome":
            self._cleanup_chrome_port()
            state.pop("browser_generation", None)
        if not self._is_recorded_process(state):
            return
        process = psutil.Process(state["pid"])
        process.terminate()
        try:
            exit_code = process.wait(timeout=8)
        except psutil.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=4)
        state["exit_code"] = exit_code
        state["pid"] = None
        state["created_at"] = None

    def _is_process_current(self, service: str, state: dict) -> bool:
        if service == "controlled_chrome":
            return self._is_chrome_current(state)
        if not self._is_recorded_process(state):
            return False
        expected = self.definitions()[service].command
        try:
            commandline = " ".join(psutil.Process(state["pid"]).cmdline())
        except (psutil.Error, KeyError, TypeError, ValueError):
            return False
        return all(part in commandline for part in expected[1:3])

    def _is_chrome_current(self, state: dict) -> bool:
        generation = state.get("browser_generation")
        if not isinstance(generation, str) or not generation:
            return self._is_recorded_process(state)
        try:
            return CdpBrowser(timeout=1).browser_generation() == generation
        except CdpUnavailableError:
            return False

    def _renew_lease(self) -> None:
        if self._instance_id:
            self.store.write_instance_lease(self._instance_id)

    def _wait_for_previous_services(self) -> None:
        self._terminate_previous_services()
        deadline = time.monotonic() + PREVIOUS_SERVICE_WAIT_SECONDS
        while True:
            pending = []
            for service in SERVICE_NAMES:
                if service == "controlled_chrome":
                    continue
                state = self._read_state(service)
                if (
                    state.get("instance_id") != self._instance_id
                    and self._is_recorded_process(state)
                ):
                    pending.append(service)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise SupervisorError("上一实例服务未在限定时间内退出")
            time.sleep(0.2)

    def _terminate_previous_services(self) -> None:
        """Stop every identifiable prior Web/Worker before bootstrap preflight."""
        for service in SERVICE_SHUTDOWN_ORDER:
            if service == "controlled_chrome":
                continue
            state = self._read_state(service)
            if (
                state.get("instance_id") == self._instance_id
                or not self._is_recorded_process(state)
            ):
                continue
            self._terminate(service, state)
            self._write_state(service, state)

    def _chrome_listener_pid(self) -> int | None:
        try:
            connections = psutil.net_connections(kind="tcp")
        except psutil.Error as exc:
            raise SupervisorError("无法定位受控 Chrome 监听进程") from exc
        for connection in connections:
            address = connection.laddr
            if not address or address.port != CONTROLLED_CHROME_DEBUG_PORT:
                continue
            if address.ip not in {"127.0.0.1", "::1"}:
                continue
            if connection.pid is None:
                raise SupervisorError("无法定位受控 Chrome 监听进程")
            return connection.pid
        return None

    @staticmethod
    def _chrome_port_listening() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", CONTROLLED_CHROME_DEBUG_PORT), timeout=0.2):
                return True
        except OSError:
            return False

    def _cleanup_chrome_port(self) -> None:
        try:
            CdpBrowser(timeout=1).close_browser()
        except CdpUnavailableError:
            pass
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if not self._chrome_port_listening():
                return
            time.sleep(0.1)
        if not self._chrome_port_listening():
            return
        pid = self._chrome_listener_pid()
        if pid is None:
            raise SupervisorError("无法定位受控 Chrome 监听进程")
        try:
            process = psutil.Process(pid)
            process.terminate()
            process.wait(timeout=4)
        except (psutil.Error, psutil.TimeoutExpired) as exc:
            raise SupervisorError("受控 Chrome 端口未能释放") from exc
        if self._chrome_port_listening():
            raise SupervisorError("受控 Chrome 端口未能释放")

    @staticmethod
    def _is_recorded_process(state: dict) -> bool:
        pid = state.get("pid")
        created_at = state.get("created_at")
        if not isinstance(pid, int) or created_at is None:
            return False
        try:
            process = psutil.Process(pid)
            if not process.is_running() or abs(process.create_time() - float(created_at)) > 1:
                return False
            return True
        except (psutil.Error, KeyError, TypeError, ValueError):
            return False

    def _web_healthy(self, timeout_seconds: int) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                with httpx.Client(trust_env=False, timeout=1) as client:
                    response = client.get(f"http://127.0.0.1:{self.web_port}/healthz")
                    if response.status_code == HTTPStatus.OK:
                        return True
            except httpx.HTTPError:
                time.sleep(0.5)
        return False

    @staticmethod
    def _rotate_log(path: Path) -> None:
        try:
            if path.stat().st_size >= MAX_LOG_BYTES:
                backup = path.with_suffix(".log.1")
                backup.unlink(missing_ok=True)
                path.replace(backup)
        except FileNotFoundError:
            pass


def run_supervisor(project_root: Path, runtime_dir: Path) -> int:
    supervisor = Supervisor(project_root, runtime_dir)
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

    def request_graceful_shutdown(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_graceful_shutdown)
    try:
        supervisor.bootstrap()
        supervisor.run_forever()
    except SupervisorLockedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SupervisorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        supervisor.shutdown()
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
    return 0
