from django.core.management.base import BaseCommand

from jobs.worker import consume_jobs
from reviews.video_tasks import WORKER_HANDLERS


class Command(BaseCommand):
    help = "运行独立的视频提取 Worker。"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=1.0)

    def handle(self, *args, **options):
        consume_jobs(
            self, WORKER_HANDLERS, loop=options["loop"], poll_seconds=options["poll_seconds"]
        )
