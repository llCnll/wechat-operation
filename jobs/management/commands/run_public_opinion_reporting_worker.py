from django.core.management.base import BaseCommand

from jobs.worker import consume_jobs
from reporting.public_opinion_jobs import PUBLIC_OPINION_REPORT_HANDLERS


class Command(BaseCommand):
    help = "仅认领并执行本地舆情报告任务。"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="持续消费舆情报告任务。")
        parser.add_argument("--poll-seconds", type=float, default=2, help="空队列轮询间隔。")

    def handle(self, *args, **options):
        consume_jobs(
            self,
            PUBLIC_OPINION_REPORT_HANDLERS,
            loop=options["loop"],
            poll_seconds=options["poll_seconds"],
        )
