"""Release compatibility utility for retired session Probe and Refresh jobs.

Run only after the old session Worker has stopped.  Taking the exact former worker
lock makes that ordering machine-checkable and keeps active records auditable.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.worker import worker_lock


# Keep these historical identifiers only inside the short-lived release
# compatibility command. They are not registered runtime task types.
LEGACY_TASK_TYPES = ("GAME_CIRCLE_LOGIN_PROBE", "GAME_CIRCLE_SESSION_REFRESH")
RELEASE_REASON = "RELEASED_LEGACY_SESSION_JOB"


class Command(BaseCommand):
    help = "终态化已废弃的游戏圈 Probe/Refresh 任务，保留全部审计数据。"

    def handle(self, *args, **options):
        with worker_lock(LEGACY_TASK_TYPES) as acquired:
            if not acquired:
                raise CommandError("旧游戏圈会话 Worker 仍持有任务锁；请先停止旧 Worker")
            changed = BackgroundJob.objects.filter(
                task_type__in=LEGACY_TASK_TYPES,
                status__in=(JobStatus.PENDING, JobStatus.RUNNING),
            ).update(
                status=JobStatus.FAILED,
                error_summary=RELEASE_REASON,
                finished_at=timezone.now(),
            )
        self.stdout.write(f"已终态化 {changed} 个废弃游戏圈会话任务。")
