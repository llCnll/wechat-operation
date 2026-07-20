"""Enqueue the one-time author mute-status backfill without opening a source session."""

from django.core.management.base import BaseCommand

from jobs.once.author_mute_status_backfill import enqueue_author_mute_status_backfill


class Command(BaseCommand):
    help = "入队一次性全量原创帖作者禁言状态回填任务。"

    def handle(self, *args, **options):
        job = enqueue_author_mute_status_backfill()
        self.stdout.write(f"已入队或复用任务 {job.pk}（{job.status}）。")
