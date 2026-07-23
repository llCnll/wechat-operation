from django.core.management.base import BaseCommand

from jobs.permanent_mute import WORKER_HANDLERS
from jobs.worker import consume_jobs


class Command(BaseCommand):
    help = "仅认领并执行永久禁言续期任务。"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="持续消费永久禁言任务。")
        parser.add_argument("--poll-seconds", type=float, default=2, help="空队列轮询间隔。")

    def handle(self, *args, **options):
        consume_jobs(
            self, WORKER_HANDLERS, loop=options["loop"], poll_seconds=options["poll_seconds"]
        )
