"""Enqueue one checkpointed, read-only original-post ingestion window."""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from posts.ingestion_jobs import SHANGHAI, enqueue_ingestion_window


class Command(BaseCommand):
    help = "创建只读原创帖子采集任务；Worker 将按页保存检查点。"

    def add_arguments(self, parser):
        parser.add_argument("--appid", required=True, help="目标小游戏 appid")
        duration = parser.add_mutually_exclusive_group()
        duration.add_argument("--hours", type=int, default=12, help="采集最近 N 小时，默认 12")
        duration.add_argument("--days", type=int, help="采集最近 N 天")
        parser.add_argument("--page-size", type=int, default=20, help="每页条数，范围 1 至 100")

    def handle(self, *args, **options):
        appid = options["appid"].strip()
        page_size = options["page_size"]
        hours = options["days"] * 24 if options.get("days") else options["hours"]
        if not appid:
            raise CommandError("appid 不能为空")
        if hours <= 0:
            raise CommandError("采集时长必须为正数")
        if not 1 <= page_size <= 100:
            raise CommandError("page-size 必须在 1 至 100 之间")

        end_at = timezone.now().astimezone(SHANGHAI).replace(second=0, microsecond=0)
        start_at = end_at - timedelta(hours=hours)
        start_time = int(start_at.timestamp())
        end_time = int(end_at.timestamp())
        try:
            result = enqueue_ingestion_window(
                appid=appid,
                start_time=start_time,
                end_time=end_time,
                page_size=page_size,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        outcome = "已创建" if result.created else "已存在"
        self.stdout.write(
            f"采集任务 {result.job.pk} {outcome}：{start_at.isoformat()} 至 {end_at.isoformat()}。"
        )
