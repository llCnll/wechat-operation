"""Enqueue the local-only one-time original-post topic backfill."""

from django.core.management.base import BaseCommand

from jobs.once.backfill_post_topics import enqueue_post_topic_backfill


class Command(BaseCommand):
    help = "入队一次性原创帖尾部话题回填任务（不访问上游）。"

    def handle(self, *args, **options):
        job = enqueue_post_topic_backfill()
        self.stdout.write(f"已入队或复用任务 {job.pk}（{job.status}）。")
