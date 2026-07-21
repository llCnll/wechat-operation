import time
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from posts.ingestion_jobs import (
    SHANGHAI,
    enqueue_scheduled_jobs,
    scheduled_ingestion_window,
)
from reporting.metric_jobs import enqueue_due_metric_jobs
from service_supervisor.runtime import write_worker_heartbeat


class Command(BaseCommand):
    help = "为城主别慌张持续创建自动状态巡检和帖子采集任务。"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="仅执行一次调度。")
        parser.add_argument("--loop", action="store_true", help="持续检查并调度任务。")
        parser.add_argument("--poll-seconds", type=float, default=30, help="循环模式的检查间隔。")

    def handle(self, *args, **options):
        if options["once"] == options["loop"]:
            raise CommandError("必须且只能传入 --once 或 --loop")
        if options["poll_seconds"] <= 0:
            raise CommandError("--poll-seconds 必须为正数")
        while True:
            write_worker_heartbeat()
            self._schedule_once()
            if options["once"]:
                return
            time.sleep(options["poll_seconds"])

    def _schedule_once(self) -> None:
        now = timezone.now()
        try:
            window = scheduled_ingestion_window(now)
            scheduled = enqueue_scheduled_jobs(now)
            metrics = enqueue_due_metric_jobs(now)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        start_at = datetime.fromtimestamp(window.start_time, tz=SHANGHAI)
        end_at = datetime.fromtimestamp(window.end_time, tz=SHANGHAI)
        outcome = "已创建" if scheduled.ingestion.created else "已存在"
        self.stdout.write(
            "自动采集任务"
            f" {outcome}：appid=wx6d2454a8afcd9706，"
            f"窗口={start_at.isoformat()} 至 {end_at.isoformat()}，"
            f"任务={scheduled.ingestion.job.pk}。"
        )
        status_outcome = "已创建" if scheduled.status_sync.created else "已存在"
        self.stdout.write(
            f"待审核帖子状态巡检 {status_outcome}："
            f"appid=wx6d2454a8afcd9706，批量=20，任务={scheduled.status_sync.job.pk}。"
        )
        if metrics.daily:
            created = sum(result.created for result in metrics.daily)
            self.stdout.write(
                f"每日指标同步：{created} 个已创建，{len(metrics.daily) - created} 个已存在。"
            )
        if metrics.weekly:
            created = sum(result.created for result in metrics.weekly)
            self.stdout.write(
                f"周度运营效果同步：{created} 个已创建，"
                f"{len(metrics.weekly) - created} 个已存在。"
            )
