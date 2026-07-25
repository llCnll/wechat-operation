"""Shared loop for workers that own an explicit set of background task types."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from django.db import OperationalError

from config.settings import runtime
from jobs.models import BackgroundJob
from jobs.services import execute_next_job, recover_interrupted_jobs
from service_supervisor.runtime import write_worker_heartbeat

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    fcntl = None
    import msvcrt
else:
    msvcrt = None


def _try_lock(lock_file) -> bool:
    """Use the platform file-lock primitive without adding a deployment dependency."""
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    # Windows locks a byte range, so ensure the first byte exists before locking it.
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.seek(0)
        lock_file.write("\0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def worker_lock(task_types: tuple[str, ...]):
    """Hold one process-wide lock for a specialized SQLite worker on every platform."""
    lock_dir = Path(runtime.database_path).parent / "worker-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_name = sha256(",".join(sorted(task_types)).encode()).hexdigest()[:16]
    lock_file = (lock_dir / f"{lock_name}.lock").open("a+")
    acquired = False
    try:
        acquired = _try_lock(lock_file)
        yield acquired
    finally:
        if acquired:
            _unlock(lock_file)
        lock_file.close()


def consume_jobs(
    command,
    handlers: dict[str, Callable[[BackgroundJob], None]],
    *,
    loop: bool,
    poll_seconds: float,
) -> None:
    """Consume only the supplied task types so specialized workers cannot steal work."""
    task_types = tuple(handlers)
    with worker_lock(task_types) as acquired:
        if not acquired:
            command.stderr.write("同类 Worker 已在运行，本实例不会执行或恢复任务。")
            return
        recovery_complete = False
        while True:
            if not write_worker_heartbeat():
                command.stdout.write("Supervisor 租约已失效，Worker 将退出。")
                return
            try:
                if not recovery_complete:
                    recovered = recover_interrupted_jobs(task_types=task_types)
                    recovery_complete = True
                    if recovered:
                        command.stdout.write(f"已恢复 {recovered} 个中断任务。")
                job = execute_next_job(handlers, task_types=task_types)
            except OperationalError as exc:
                command.stderr.write(f"数据库暂时繁忙，Worker 将重试：{exc}")
                if not loop:
                    return
                time.sleep(max(poll_seconds, 0.1))
                continue
            if job is not None:
                command.stdout.write(f"已处理任务 {job.pk}（{job.task_type}）：{job.status}。")
                if not loop:
                    return
                continue
            if not loop:
                command.stdout.write("没有待执行任务。")
                return
            time.sleep(max(poll_seconds, 0.1))
