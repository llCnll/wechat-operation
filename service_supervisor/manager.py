"""Supervisor process lifecycle and loopback-only control protocol."""

from __future__ import annotations

import hmac
import json
import os
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

from .runtime import RuntimeStore, SupervisorLockedError, utc_now

SERVICE_NAMES = ("web", "scheduler", "ingestion", "backfill_ingestion", "review")
WORKER_NAMES = ("scheduler", "ingestion", "backfill_ingestion", "review")
CONTROL_VERSION = "v1"
WEB_PORT = 8000
MAX_LOG_BYTES = 1_000_000


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
        self._stopping = threading.Event()
        self._log_handles: dict[str, Any] = {}

    def definitions(self) -> dict[str, ServiceDefinition]:
        python = sys.executable
        manage = str(self.project_root / "manage.py")
        return {
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
            "review": ServiceDefinition(
                "review",
                (python, manage, "run_review_worker", "--loop"),
                frozenset(("start", "stop", "restart", "status", "logs")),
            ),
        }

    def acquire(self) -> None:
        from .runtime import ExclusiveFileLock

        self.store.ensure()
        self._lock = ExclusiveFileLock(self.store.lock_path)
        self._lock.acquire()

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
            commands = (("migrate",), ("collectstatic", "--noinput"), ("ensure_default_policy",))
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
            self.start_controller()
            self.preflight()
            web_state = self._read_state("web")
            web_state["desired"] = True
            self._write_state("web", web_state)
            self.reconcile()
            if not self._web_healthy(timeout_seconds=20) or not self._is_process_current(
                "web", self._read_state("web")
            ):
                raise SupervisorError("Web 服务未在 20 秒内通过健康检查")
        except Exception:
            state = self._read_state("web")
            state["desired"] = False
            self._terminate("web", state)
            self._write_state("web", state)
            self.release()
            raise

    def run_forever(self) -> None:
        while not self._stopping.wait(2):
            self.reconcile()

    def shutdown(self) -> None:
        self._stopping.set()
        self.release()

    def reconcile(self) -> None:
        with self._state_lock:
            for service in SERVICE_NAMES:
                state = self._read_state(service)
                if state.get("desired") and not self._is_process_current(service, state):
                    self._terminate(service, state)
                    self._launch(service, state)
                if service == "web" and self._is_process_current(
                    service, self._read_state(service)
                ):
                    self.store.write_heartbeat(service)

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
        if service == "web" and action == "restart":
            threading.Timer(0.7, self.restart, args=(service,)).start()
            return {"service": service, "state": "restarting"}
        if action == "start":
            self.start(service)
        elif action == "stop":
            self.stop(service)
        elif action == "restart":
            self.restart(service)
        return {"service": service, "status": self.status()[service]}

    def start(self, service: str) -> None:
        if service not in WORKER_NAMES:
            raise SupervisorError("Web 只能通过重启操作恢复")
        with self._state_lock:
            state = self._read_state(service)
            state["desired"] = True
            self._write_state(service, state)
            if not self._is_process_current(service, state):
                self._launch(service, state)

    def stop(self, service: str) -> None:
        if service not in WORKER_NAMES:
            raise SupervisorError("Web 不能通过页面停止")
        with self._state_lock:
            state = self._read_state(service)
            state["desired"] = False
            self._terminate(service, state)
            self._write_state(service, state)

    def restart(self, service: str) -> None:
        with self._state_lock:
            state = self._read_state(service)
            state["desired"] = True
            self._terminate(service, state)
            self._write_state(service, state)
            self._launch(service, state)

    def logs(self, service: str, lines: int) -> list[str]:
        if service not in SERVICE_NAMES:
            raise SupervisorError("未知服务")
        safe_lines = max(1, min(lines, 200))
        try:
            content = self.store.log_path(service).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
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
            }
        )
        self._write_state(service, state)

    def _terminate(self, service: str, state: dict) -> None:
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
        if not self._is_recorded_process(state):
            return False
        expected = self.definitions()[service].command
        try:
            commandline = " ".join(psutil.Process(state["pid"]).cmdline())
        except (psutil.Error, KeyError, TypeError, ValueError):
            return False
        return all(part in commandline for part in expected[1:3])

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
    return 0
