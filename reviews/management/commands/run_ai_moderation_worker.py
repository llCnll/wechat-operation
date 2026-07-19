from django.core.management.base import BaseCommand

from jobs.worker import consume_jobs
from reviews.automation_tasks import WORKER_HANDLERS


class Command(BaseCommand):
    help = "仅消费已授权 AI 结论创建的自动处置任务。"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="持续消费自动处置任务。")
        parser.add_argument("--poll-seconds", type=float, default=2, help="空队列轮询间隔。")

    def handle(self, *args, **options):
        consume_jobs(
            self, WORKER_HANDLERS, loop=options["loop"], poll_seconds=options["poll_seconds"]
        )
