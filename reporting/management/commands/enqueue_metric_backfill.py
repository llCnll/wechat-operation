from datetime import date

from django.core.management.base import BaseCommand, CommandError

from reporting.metric_jobs import enqueue_metric_backfill
from reporting.models import MetricSource


class Command(BaseCommand):
    help = "创建或复用指定游戏圈指标来源和日期范围的人工回填任务。"

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=[source.value for source in MetricSource])
        parser.add_argument("--start-date", required=True)
        parser.add_argument("--end-date", required=True)

    def handle(self, *args, **options):
        try:
            start_day = date.fromisoformat(options["start_date"])
            end_day = date.fromisoformat(options["end_date"])
            result = enqueue_metric_backfill(
                source=options["source"],
                start_day=start_day,
                end_day=end_day,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        outcome = "已创建" if result.created else "已存在"
        self.stdout.write(f"指标回填任务{outcome}：#{result.job.pk}。")
