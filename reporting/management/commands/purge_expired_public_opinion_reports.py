from django.core.management.base import BaseCommand
from django.utils import timezone

from reporting.public_opinion_jobs import purge_expired_public_opinion_reports


class Command(BaseCommand):
    help = "删除已过来源保留期的完整舆情报告。"

    def handle(self, *args, **options):
        deleted = purge_expired_public_opinion_reports(now=timezone.now())
        self.stdout.write(f"已清理 {deleted} 条过期舆情报告记录。")
