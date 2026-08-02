"""Portable runtime state primitives shared by the supervisor and workers."""

from __future__ import annotations

import json
import os
import random
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
SUPERVISOR_PROCESS_REGISTRY_VERSION = 1
SUPERVISOR_EVENT_BUFFER_LIMIT = 80
SESSION_TARGET_EVENT_BUFFER_LIMIT = 80
ATOMIC_WRITE_RETRY_SECONDS = 1.0
ATOMIC_WRITE_INITIAL_BACKOFF_SECONDS = 0.02
ATOMIC_WRITE_MAX_BACKOFF_SECONDS = 0.2
WINDOWS_RETRYABLE_REPLACE_ERRORS = frozenset({5, 32})

LIFECYCLE_SERVICES = frozenset(
    {
        "supervisor",
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
)
LIFECYCLE_EVENTS = frozenset(
    {"STARTED", "STOPPED", "LAUNCH_FAILED", "LEASE_REVOKE", "RECOVERY_SKIP", "REPLACED"}
)
LIFECYCLE_REASONS = frozenset(
    {
        "BOOTSTRAP",
        "DESIRED_START",
        "OPERATOR_RESTART",
        "TAKEOVER_RECLAIM",
        "GRACEFUL_SHUTDOWN",
        "LEASE_REVOKED",
        "LEASE_REVOKE_WRITE_FAILED",
        "TERMINATION_TIMEOUT",
        "LAUNCH_STATE_PUBLISH_FAILED",
        "CHROME_CDP_UNAVAILABLE",
        "CHROME_GENERATION_CHANGED",
        "SESSION_OWNER_REPLACEMENT",
        "IDENTITY_MISMATCH",
    }
)
LIFECYCLE_OUTCOMES = frozenset({"COMPLETED", "FAILED", "GRACEFUL", "FORCED", "SKIPPED"})
LIFECYCLE_TRIGGERS = frozenset(
    {"BOOTSTRAP", "RECONCILE", "OPERATOR", "TAKEOVER", "SESSION_OWNER"}
)
LIFECYCLE_DETECTIONS = frozenset(
    {"CDP_UNAVAILABLE", "GENERATION_MISMATCH", "SESSION_OWNER_REPLACEMENT"}
)
SESSION_TARGET_KINDS = frozenset({"HOME", "PLUGIN"})
SESSION_TARGET_ACTIONS = frozenset({"CREATED", "REUSED", "CLOSED", "MISSING", "ACTIVATED"})
SESSION_TARGET_INTENTS = frozenset({"BOOTSTRAP", "REPAIR", "KEEPALIVE", "OPERATOR", "RECLAIM"})
SESSION_TARGET_OUTCOMES = frozenset({"COMPLETED", "FAILED", "SKIPPED"})


class SupervisorLockedError(RuntimeError):
    """Raised when a second supervisor attempts to own the same runtime."""


class SupervisorLeaseOwnershipError(RuntimeError):
    """Raised when an instance tries to revoke another Supervisor's lease."""


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

    @property
    def process_registry_path(self) -> Path:
        return self.root / "managed-processes.json"

    @property
    def lifecycle_events_path(self) -> Path:
        return self.root / "lifecycle-events.json"

    @property
    def session_target_events_path(self) -> Path:
        return self.root / "session-target-events.json"

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
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(value, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
            self._replace_json_file(temporary_path, path)
        except BaseException:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    @staticmethod
    def _replace_json_file(temporary_path: Path, path: Path) -> None:
        deadline = time.monotonic() + ATOMIC_WRITE_RETRY_SECONDS
        delay = ATOMIC_WRITE_INITIAL_BACKOFF_SECONDS
        attempts = 0
        while True:
            attempts += 1
            try:
                os.replace(temporary_path, path)
                return
            except PermissionError as exc:
                if not RuntimeStore._is_retryable_replace_error(exc):
                    RuntimeStore._record_write_attempts(exc, attempts)
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    RuntimeStore._record_write_attempts(exc, attempts)
                    raise
                sleep_seconds = min(
                    delay * random.uniform(0.8, 1.2),
                    remaining,
                )
                time.sleep(sleep_seconds)
                delay = min(delay * 2, ATOMIC_WRITE_MAX_BACKOFF_SECONDS)

    @staticmethod
    def _is_retryable_replace_error(exc: PermissionError) -> bool:
        return (
            os.name == "nt"
            and getattr(exc, "winerror", None) in WINDOWS_RETRYABLE_REPLACE_ERRORS
        )

    @staticmethod
    def _record_write_attempts(exc: OSError, attempts: int) -> None:
        exc.runtime_write_attempts = attempts

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

    def revoke_instance_lease(self, instance_id: str) -> None:
        """Atomically expire the lease only when it still belongs to this owner."""
        lease = self.read_instance_lease()
        if not instance_id or lease.get("instance_id") != instance_id:
            raise SupervisorLeaseOwnershipError("Supervisor 租约不属于当前实例")
        self.write_json(
            self.instance_lease_path,
            {
                "version": SUPERVISOR_LEASE_VERSION,
                "instance_id": instance_id,
                "renewed_at": utc_now(),
                "expires_at_epoch": time.time(),
            },
        )

    def read_instance_lease(self) -> dict:
        value = self.read_json(self.instance_lease_path, {})
        return value if isinstance(value, dict) else {}

    def read_process_registry(self) -> list[dict]:
        value = self.read_json(self.process_registry_path, {})
        if (
            not isinstance(value, dict)
            or value.get("version") != SUPERVISOR_PROCESS_REGISTRY_VERSION
        ):
            return []
        records = value.get("records")
        if not isinstance(records, list):
            return []
        return [record for record in records if self._is_process_record(record)]

    def write_process_registry(self, records: list[dict]) -> None:
        if not all(self._is_process_record(record) for record in records):
            raise ValueError("受管进程登记格式无效")
        self.write_json(
            self.process_registry_path,
            {"version": SUPERVISOR_PROCESS_REGISTRY_VERSION, "records": records},
        )

    def append_lifecycle_event(
        self,
        *,
        service: str,
        event: str,
        reason: str,
        generation: int,
        outcome: str,
        trigger: str | None = None,
        detection: str | None = None,
    ) -> None:
        if (
            service not in LIFECYCLE_SERVICES
            or event not in LIFECYCLE_EVENTS
            or reason not in LIFECYCLE_REASONS
            or outcome not in LIFECYCLE_OUTCOMES
            or not isinstance(generation, int)
            or generation < 0
            or (trigger is not None and trigger not in LIFECYCLE_TRIGGERS)
            or (detection is not None and detection not in LIFECYCLE_DETECTIONS)
        ):
            raise ValueError("Supervisor 生命周期事件格式无效")
        payload = {
            "at": utc_now(),
            "service": service,
            "event": event,
            "reason": reason,
            "generation": generation,
            "outcome": outcome,
        }
        if trigger is not None:
            payload["trigger"] = trigger
        if detection is not None:
            payload["detection"] = detection
        self._append_bounded_event(
            self.lifecycle_events_path, payload, limit=SUPERVISOR_EVENT_BUFFER_LIMIT
        )

    def read_lifecycle_events(self) -> list[dict]:
        return [
            event
            for event in self._read_bounded_events(self.lifecycle_events_path)
            if self._is_lifecycle_event(event)
        ]

    def append_session_target_event(
        self, *, target_kind: str, action: str, intent: str, outcome: str
    ) -> None:
        if (
            target_kind not in SESSION_TARGET_KINDS
            or action not in SESSION_TARGET_ACTIONS
            or intent not in SESSION_TARGET_INTENTS
            or outcome not in SESSION_TARGET_OUTCOMES
        ):
            raise ValueError("会话 target 动作摘要格式无效")
        self._append_bounded_event(
            self.session_target_events_path,
            {
                "at": utc_now(),
                "target_kind": target_kind,
                "action": action,
                "intent": intent,
                "outcome": outcome,
            },
            limit=SESSION_TARGET_EVENT_BUFFER_LIMIT,
        )

    def read_session_target_events(self) -> list[dict]:
        return [
            event
            for event in self._read_bounded_events(self.session_target_events_path)
            if self._is_session_target_event(event)
        ]

    def _append_bounded_event(self, path: Path, event: dict, *, limit: int) -> None:
        value = self.read_json(path, {})
        existing = value.get("events") if isinstance(value, dict) else None
        if isinstance(existing, list):
            events = [item for item in existing if isinstance(item, dict)]
        else:
            events = []
        events.append(event)
        self.write_json(path, {"events": events[-limit:]})

    def _read_bounded_events(self, path: Path) -> list[dict]:
        value = self.read_json(path, {})
        events = value.get("events") if isinstance(value, dict) else None
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]

    @staticmethod
    def _is_process_record(record: object) -> bool:
        if not isinstance(record, dict):
            return False
        return (
            record.get("service") in LIFECYCLE_SERVICES - {"supervisor", "controlled_chrome"}
            and isinstance(record.get("pid"), int)
            and record["pid"] > 0
            and isinstance(record.get("created_at"), int | float)
            and isinstance(record.get("instance_id"), str)
            and bool(record["instance_id"])
            and isinstance(record.get("generation"), int)
            and record["generation"] > 0
            and set(record) == {"service", "pid", "created_at", "instance_id", "generation"}
        )

    @staticmethod
    def _is_event_timestamp(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
        return True

    @classmethod
    def _is_lifecycle_event(cls, event: object) -> bool:
        if not isinstance(event, dict):
            return False
        allowed = {
            "at",
            "service",
            "event",
            "reason",
            "generation",
            "outcome",
            "trigger",
            "detection",
        }
        if not set(event).issubset(allowed):
            return False
        return (
            cls._is_event_timestamp(event.get("at"))
            and event.get("service") in LIFECYCLE_SERVICES
            and event.get("event") in LIFECYCLE_EVENTS
            and event.get("reason") in LIFECYCLE_REASONS
            and isinstance(event.get("generation"), int)
            and event["generation"] >= 0
            and event.get("outcome") in LIFECYCLE_OUTCOMES
            and ("trigger" not in event or event["trigger"] in LIFECYCLE_TRIGGERS)
            and ("detection" not in event or event["detection"] in LIFECYCLE_DETECTIONS)
        )

    @classmethod
    def _is_session_target_event(cls, event: object) -> bool:
        if not isinstance(event, dict):
            return False
        return set(event) == {"at", "target_kind", "action", "intent", "outcome"} and (
            cls._is_event_timestamp(event.get("at"))
            and event.get("target_kind") in SESSION_TARGET_KINDS
            and event.get("action") in SESSION_TARGET_ACTIONS
            and event.get("intent") in SESSION_TARGET_INTENTS
            and event.get("outcome") in SESSION_TARGET_OUTCOMES
        )

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
