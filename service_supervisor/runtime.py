"""Portable runtime state primitives shared by the supervisor and workers."""

from __future__ import annotations

import json
import os
import secrets
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only.
    fcntl = None
    import msvcrt
else:
    msvcrt = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


SUPERVISOR_LEASE_VERSION = 1
SUPERVISOR_LEASE_SECONDS = 30.0
SUPERVISOR_LEASE_POLL_SECONDS = 5.0
SUPERVISOR_LEASE_READ_FAILURE_LIMIT = 2


class SupervisorLockedError(RuntimeError):
    """Raised when a second supervisor attempts to own the same runtime."""


class ExclusiveFileLock:
    """Hold a cross-platform, process-scoped file lock for one Supervisor."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        if fcntl is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock_file.close()
                raise SupervisorLockedError("已有 Supervisor 正在运行") from exc
        else:  # pragma: no cover - Windows only.
            lock_file.seek(0, 2)
            if lock_file.tell() == 0:
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                lock_file.close()
                raise SupervisorLockedError("已有 Supervisor 正在运行") from exc
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows only.
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None


class RuntimeStore:
    """Keep supervisor files outside the database so Web restarts remain harmless."""

    def __init__(self, runtime_dir: Path):
        self.root = runtime_dir / "supervisor"
        self.services_dir = self.root / "services"
        self.heartbeats_dir = self.root / "heartbeats"
        self.logs_dir = self.root / "logs"

    def ensure(self) -> None:
        for directory in (
            self.root,
            self.services_dir,
            self.heartbeats_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def lock_path(self) -> Path:
        return self.root / "supervisor.lock"

    @property
    def token_path(self) -> Path:
        return self.root / "control.token"

    @property
    def game_circle_ipc_token_path(self) -> Path:
        return self.root / "game-circle-ipc.token"

    @property
    def endpoint_path(self) -> Path:
        return self.root / "endpoint.json"

    @property
    def instance_lease_path(self) -> Path:
        return self.root / "instance-lease.json"

    def service_path(self, service: str) -> Path:
        return self.services_dir / f"{service}.json"

    def heartbeat_path(self, service: str) -> Path:
        return self.heartbeats_dir / f"{service}.json"

    def log_path(self, service: str) -> Path:
        return self.logs_dir / f"{service}.log"

    @property
    def game_circle_session_path(self) -> Path:
        return self.root / "game-circle-session.json"

    def read_json(self, path: Path, default=None):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            json.dump(value, temporary, ensure_ascii=False, sort_keys=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)

    def token(self) -> str:
        return self._token_at(self.token_path)

    def game_circle_ipc_token(self) -> str:
        """Return the capability dedicated to game-circle service calls."""
        return self._token_at(self.game_circle_ipc_token_path)

    @staticmethod
    def _token_at(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            path.write_text(token, encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:  # pragma: no cover - permissions vary on Windows.
                pass
            return token

    def write_instance_lease(
        self, instance_id: str, *, ttl_seconds: float = SUPERVISOR_LEASE_SECONDS
    ) -> None:
        if not instance_id or ttl_seconds <= 0:
            raise ValueError("Supervisor 租约参数无效")
        now = time.time()
        self.write_json(
            self.instance_lease_path,
            {
                "version": SUPERVISOR_LEASE_VERSION,
                "instance_id": instance_id,
                "renewed_at": utc_now(),
                "expires_at_epoch": now + ttl_seconds,
            },
        )

    def read_instance_lease(self) -> dict:
        value = self.read_json(self.instance_lease_path, {})
        return value if isinstance(value, dict) else {}

    def write_heartbeat(self, service: str, *, instance_id: str | None = None) -> None:
        payload = {"service": service, "at": utc_now()}
        if instance_id:
            payload["instance_id"] = instance_id
        self.write_json(self.heartbeat_path(service), payload)


@dataclass
class SupervisorLeaseGuard:
    """Cooperatively stop a Supervisor-managed Python process after lease loss."""

    store: RuntimeStore
    service: str
    instance_id: str
    consecutive_read_failures: int = 0

    @classmethod
    def from_environment(cls) -> SupervisorLeaseGuard | None:
        service = os.getenv("OPERATION_SUPERVISOR_SERVICE", "")
        runtime_dir = os.getenv("OPERATION_RUNTIME_DIR", "")
        instance_id = os.getenv("OPERATION_SUPERVISOR_INSTANCE", "")
        if not service or not runtime_dir or not instance_id:
            return None
        return cls(RuntimeStore(Path(runtime_dir)), service, instance_id)

    def check_and_heartbeat(self) -> bool:
        try:
            lease = self.store.read_instance_lease()
        except OSError:
            self.consecutive_read_failures += 1
            return self.consecutive_read_failures < SUPERVISOR_LEASE_READ_FAILURE_LIMIT

        expires_at = lease.get("expires_at_epoch")
        if (
            lease.get("version") == SUPERVISOR_LEASE_VERSION
            and isinstance(expires_at, int | float)
        ):
            if lease.get("instance_id") != self.instance_id or float(expires_at) <= time.time():
                return False
            try:
                self.store.write_heartbeat(self.service, instance_id=self.instance_id)
            except OSError:
                self.consecutive_read_failures += 1
                return self.consecutive_read_failures < SUPERVISOR_LEASE_READ_FAILURE_LIMIT
            self.consecutive_read_failures = 0
            return True

        self.consecutive_read_failures += 1
        return self.consecutive_read_failures < SUPERVISOR_LEASE_READ_FAILURE_LIMIT


_worker_lease_guard: SupervisorLeaseGuard | None = None
_worker_lease_guard_key: tuple[str, str, str] | None = None


def check_supervisor_lease_and_heartbeat() -> bool:
    """Return whether a managed Worker may begin another loop iteration."""
    global _worker_lease_guard, _worker_lease_guard_key
    key = (
        os.getenv("OPERATION_SUPERVISOR_SERVICE", ""),
        os.getenv("OPERATION_RUNTIME_DIR", ""),
        os.getenv("OPERATION_SUPERVISOR_INSTANCE", ""),
    )
    if _worker_lease_guard is None or _worker_lease_guard_key != key:
        _worker_lease_guard = SupervisorLeaseGuard.from_environment()
        _worker_lease_guard_key = key
    return _worker_lease_guard is None or _worker_lease_guard.check_and_heartbeat()


def write_worker_heartbeat() -> bool:
    """Backward-compatible Worker heartbeat entry point with lease checking."""
    return check_supervisor_lease_and_heartbeat()


def start_web_lease_guard() -> None:
    """Stop the Supervisor-managed WSGI process when its lease becomes invalid."""
    guard = SupervisorLeaseGuard.from_environment()
    if guard is None or guard.service != "web":
        return

    def monitor() -> None:
        while guard.check_and_heartbeat():
            time.sleep(SUPERVISOR_LEASE_POLL_SECONDS)
        if os.name == "nt":  # Windows cannot deliver SIGTERM as a graceful self-signal.
            os._exit(0)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=monitor, name="supervisor-web-lease", daemon=True).start()


@contextmanager
def supervisor_lock(runtime_dir: Path):
    store = RuntimeStore(runtime_dir)
    store.ensure()
    lock = ExclusiveFileLock(store.lock_path)
    lock.acquire()
    try:
        yield store
    finally:
        lock.release()
