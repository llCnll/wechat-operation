"""Portable runtime state primitives shared by the supervisor and workers."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
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
            lock_file.seek(0)
            lock_file.write(b"\\0")
            lock_file.flush()
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
        for directory in (self.root, self.services_dir, self.heartbeats_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def lock_path(self) -> Path:
        return self.root / "supervisor.lock"

    @property
    def token_path(self) -> Path:
        return self.root / "control.token"

    @property
    def endpoint_path(self) -> Path:
        return self.root / "endpoint.json"

    def service_path(self, service: str) -> Path:
        return self.services_dir / f"{service}.json"

    def heartbeat_path(self, service: str) -> Path:
        return self.heartbeats_dir / f"{service}.json"

    def log_path(self, service: str) -> Path:
        return self.logs_dir / f"{service}.log"

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
        try:
            return self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            self.token_path.write_text(token, encoding="utf-8")
            try:
                self.token_path.chmod(0o600)
            except OSError:  # pragma: no cover - permissions vary on Windows.
                pass
            return token

    def write_heartbeat(self, service: str) -> None:
        self.write_json(self.heartbeat_path(service), {"service": service, "at": utc_now()})


def write_worker_heartbeat() -> None:
    """Update the optional heartbeat only for processes started by Supervisor."""
    service = os.getenv("OPERATION_SUPERVISOR_SERVICE", "")
    runtime_dir = os.getenv("OPERATION_RUNTIME_DIR", "")
    if service not in {"scheduler", "ingestion", "backfill_ingestion", "review", "video_extraction"} or not runtime_dir:
        return
    RuntimeStore(Path(runtime_dir)).write_heartbeat(service)


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
