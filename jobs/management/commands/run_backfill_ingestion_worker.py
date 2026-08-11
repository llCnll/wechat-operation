"""Run the dedicated worker for manual historical ingestion work."""

from django.core.management.base import BaseCommand

from jobs.worker import consume_jobs
from posts.tasks import BACKFILL_INGESTION_WORKER_HANDLERS


class Command(BaseCommand):
    help = "仅认领并执行人工补采（含指定帖子）、范围互动刷新和旧采集任务。"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="持续消费人工补采任务。")
        parser.add_argument("--poll-seconds", type=float, default=2, help="空队列轮询间隔。")

    def handle(self, *args, **options):
        consume_jobs(
            self,
            BACKFILL_INGESTION_WORKER_HANDLERS,
            loop=options["loop"],
            poll_seconds=options["poll_seconds"],
        )
