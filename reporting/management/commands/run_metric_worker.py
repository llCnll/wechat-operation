from django.core.management.base import BaseCommand

from jobs.worker import consume_jobs
from reporting.metric_tasks import METRIC_WORKER_HANDLERS


class Command(BaseCommand):
    help = "消费游戏圈每日、每周和历史指标快照任务。"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="仅处理一个待执行任务。")
        parser.add_argument("--loop", action="store_true", help="持续处理指标任务。")
        parser.add_argument("--poll-seconds", type=float, default=30)

    def handle(self, *args, **options):
        if options["once"] == options["loop"]:
            self.stderr.write("必须且只能传入 --once 或 --loop")
            return
        if options["poll_seconds"] <= 0:
            self.stderr.write("--poll-seconds 必须为正数")
            return
        consume_jobs(
            self,
            METRIC_WORKER_HANDLERS,
            loop=options["loop"],
            poll_seconds=options["poll_seconds"],
        )
