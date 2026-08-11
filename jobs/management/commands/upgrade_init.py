"""Explicitly initialize a verified legacy deployment's release baseline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.release_protocol import (
    ReleaseProtocolError,
    current_project_version,
    read_release_state,
    release_commit,
    release_for_version,
    validate_releases,
    write_release_state,
)


class Command(BaseCommand):
    help = "在完成迁移和运行态核验后，显式初始化旧端点的发布基线。"

    def add_arguments(self, parser):
        parser.add_argument("version", help="已人工核验的历史发布版本。")
        parser.add_argument("--confirmed", action="store_true", help="确认已人工核验旧端点。")

    def handle(self, *args, **options):
        if not options["confirmed"]:
            raise CommandError("初始化基线必须显式传入 --confirmed")
        project_root = Path(settings.BASE_DIR)
        try:
            releases = validate_releases(project_root)
            if read_release_state(project_root) is not None:
                raise ReleaseProtocolError("本机发布状态已存在；不得用初始化命令覆盖")
            release = release_for_version(releases, options["version"])
            current_release = release_for_version(releases, current_project_version(project_root))
            if releases.index(release) > releases.index(current_release):
                raise ReleaseProtocolError("初始化版本不能高于当前代码版本")
            write_release_state(
                project_root,
                {
                    "schema_version": 1,
                    "applied_version": release.version,
                    "applied_commit": release_commit(project_root, releases, release.version),
                    "applied_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
        except ReleaseProtocolError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"已初始化本机发布基线：{release.version}。"))
