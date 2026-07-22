"""Checkpointed metric task state machine with explicit retry classification."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from contextlib import contextmanager

from jobs.adapters import SourceContractError, SourceTransportError
from jobs.models import BackgroundJob
from jobs.services import JobDeferred, JobTerminalError
from posts.game_catalog import checkpoint_with_execution_scope, resolve_execution_scope
from config.game_circle_ipc import (
    GameCircleIpcError,
    GameCircleMetricClient,
    is_session_recovering_error,
)

from .metric_jobs import METRIC_BACKFILL_TASK, METRIC_DAILY_SYNC_TASK, METRIC_WEEKLY_SYNC_TASK
from .metric_sources import read_metric_page
from .metric_sync import MetricBusinessRetryableError, MetricPermissionError, MetricRecord, upsert_metric_snapshot
from .models import MetricSource

METRIC_DATE_CHUNK_DAYS = 90


def _date_code(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


@contextmanager
def _metric_client(scope):
    client = GameCircleMetricClient(scope=scope)
    try:
        yield client
    finally:
        client.close()


def _parse_date_code(value: object, *, field: str) -> date:
    try:
        return datetime.strptime(str(int(value)), "%Y%m%d").date()
    except (TypeError, ValueError) as exc:
        raise JobTerminalError(f"指标任务参数无效：{field}") from exc


def _task_source(job: BackgroundJob) -> MetricSource:
    try:
        source = MetricSource(job.parameter_summary["source"])
    except (KeyError, ValueError) as exc:
        raise JobTerminalError("指标任务来源无效") from exc
    return source


def _save_checkpoint(
    job: BackgroundJob,
    *,
    source: MetricSource,
    chunk_start: date,
    chunk_end: date,
    next_page: int,
    saved: int,
    last_period: date | None,
    complete: bool = False,
) -> None:
    checkpoint: dict[str, object] = {
        "source": source,
        "chunk_start_ds": _date_code(chunk_start),
        "chunk_end_ds": _date_code(chunk_end),
        "next_page": next_page,
        "saved": saved,
        "complete": complete,
    }
    if last_period is not None:
        checkpoint["last_period"] = last_period.isoformat()
    job.checkpoint = checkpoint_with_execution_scope(job, checkpoint)
    job.save(update_fields=["checkpoint", "updated_at"])


def _load_cursor(
    job: BackgroundJob,
    *,
    source: MetricSource,
    start_day: date,
) -> tuple[date, int, int]:
    checkpoint = job.checkpoint or {}
    if checkpoint.get("complete"):
        return start_day, 0, int(checkpoint.get("saved", 0))
    if checkpoint and checkpoint.get("source") not in (None, source):
        raise JobTerminalError("指标任务检查点来源不匹配")
    try:
        chunk_start = _parse_date_code(
            checkpoint.get("chunk_start_ds", _date_code(start_day)),
            field="checkpoint.chunk_start_ds",
        )
        next_page = int(checkpoint.get("next_page", 0))
        saved = int(checkpoint.get("saved", 0))
    except (TypeError, ValueError) as exc:
        raise JobTerminalError("指标任务检查点无效") from exc
    if next_page < 0 or saved < 0:
        raise JobTerminalError("指标任务检查点无效")
    return chunk_start, next_page, saved


def _persist_records(
    job: BackgroundJob,
    *,
    source: MetricSource,
    records: tuple[MetricRecord, ...],
) -> int:
    saved = int((job.checkpoint or {}).get("saved", 0))
    for record in records:
        upsert_metric_snapshot(
            appid=str(job.parameter_summary["appid"]),
            source=source,
            record=record,
            job=job,
        )
        saved += 1
    return saved


def _run_sync(
    job: BackgroundJob,
    *,
    client: GameCircleMetricClient,
    source: MetricSource,
) -> None:
    parameters = job.parameter_summary
    start_day = _parse_date_code(parameters.get("start_ds"), field="start_ds")
    end_day = _parse_date_code(parameters.get("end_ds"), field="end_ds")
    if start_day > end_day:
        raise JobTerminalError("指标任务日期范围无效")
    if (job.checkpoint or {}).get("complete"):
        return

    chunk_start, page, saved = _load_cursor(job, source=source, start_day=start_day)
    if chunk_start < start_day or chunk_start > end_day + timedelta(days=1):
        raise JobTerminalError("指标任务检查点日期范围无效")

    while chunk_start <= end_day:
        chunk_end = min(chunk_start + timedelta(days=METRIC_DATE_CHUNK_DAYS - 1), end_day)
        while True:
            metric_page = read_metric_page(
                client,
                source=source,
                start_ds=_date_code(chunk_start),
                end_ds=_date_code(chunk_end),
                page=page,
            )
            saved = _persist_records(job, source=source, records=metric_page.records)
            if metric_page.has_more:
                page += 1
                _save_checkpoint(
                    job,
                    source=source,
                    chunk_start=chunk_start,
                    chunk_end=chunk_end,
                    next_page=page,
                    saved=saved,
                    last_period=metric_page.records[-1].period_start
                    if metric_page.records
                    else None,
                )
                continue
            break
        next_chunk = chunk_end + timedelta(days=1)
        _save_checkpoint(
            job,
            source=source,
            chunk_start=next_chunk,
            chunk_end=chunk_end,
            next_page=0,
            saved=saved,
            last_period=metric_page.records[-1].period_start if metric_page.records else None,
        )
        chunk_start, page = next_chunk, 0
    job.checkpoint = checkpoint_with_execution_scope(
        job, {"source": source, "saved": saved, "complete": True}
    )
    job.save(update_fields=["checkpoint", "updated_at"])


def metric_sync_handler(job: BackgroundJob) -> None:
    try:
        scope = resolve_execution_scope(job)
        appid = scope.target_appid
    except ValueError as exc:
        raise JobTerminalError(str(exc)) from exc
    source = _task_source(job)
    try:
        with _metric_client(scope) as client:
            _run_sync(job, client=client, source=source)
    except MetricBusinessRetryableError:
        raise
    except GameCircleIpcError as exc:
        if is_session_recovering_error(exc):
            raise JobDeferred("等待游戏圈会话恢复", delay_seconds=5) from exc
        raise
    except SourceTransportError:
        raise
    except (MetricPermissionError, SourceContractError) as exc:
        raise JobTerminalError(str(exc)) from exc


METRIC_WORKER_HANDLERS = {
    METRIC_DAILY_SYNC_TASK: metric_sync_handler,
    METRIC_WEEKLY_SYNC_TASK: metric_sync_handler,
    METRIC_BACKFILL_TASK: metric_sync_handler,
}
