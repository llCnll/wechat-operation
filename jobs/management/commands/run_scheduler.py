"""Run the single local producer reconciler; it never consumes jobs or touches CDP."""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError
from django.utils import timezone

from jobs.scheduled_producers import reconcile_scheduled_producers
from jobs.worker import worker_lock
from service_supervisor.runtime import write_worker_heartbeat


class Command(BaseCommand):
    help = "按受控生产者规则持续创建后台任务，不读取来源或执行任务。"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="仅执行一次调和。")
        parser.add_argument("--loop", action="store_true", help="持续检查并调和任务。")
        parser.add_argument("--poll-seconds", type=float, default=30, help="循环模式的检查间隔。")

    def handle(self, *args, **options):
        if options["once"] == options["loop"]:
            raise CommandError("必须且只能传入 --once 或 --loop")
        if options["poll_seconds"] <= 0:
            raise CommandError("--poll-seconds 必须为正数")
        with worker_lock(("scheduled-producer-reconciler",)) as acquired:
            if not acquired:
                raise CommandError("自动调度器已在当前 runtime 运行")
            while True:
                if not write_worker_heartbeat():
                    self.stdout.write("Supervisor 租约已失效，自动调度器将退出。")
                    return
                try:
                    outcomes = reconcile_scheduled_producers(timezone.now())
                except OperationalError as exc:
                    self.stderr.write(f"数据库暂时繁忙，自动调度器将重试：{exc}")
                    if options["once"]:
                        return
                else:
                    summary = "；".join(f"{key}={value}" for key, value in outcomes.items())
                    self.stdout.write(f"自动生产者调和：{summary or '暂无规则'}。")
                    if options["once"]:
                        return
                time.sleep(options["poll_seconds"])
