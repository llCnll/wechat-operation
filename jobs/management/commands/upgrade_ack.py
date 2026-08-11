"""Acknowledge a completed upgrade only after verifiable release checks pass."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.release_protocol import (
    ReleaseProtocolError,
    build_upgrade_plan,
    current_project_version,
    once_job_status,
    pending_migrations,
    read_release_state,
    release_commit,
    validate_local_release_state,
    validate_releases,
    write_release_state,
)


class Command(BaseCommand):
    help = "验证可检查的升级条件，并在人工确认剩余操作后写入本机发布版本。"

    def add_arguments(self, parser):
        parser.add_argument("version", help="要确认的目标发布版本。")
        for name in ("dependencies", "static", "restart", "manual", "verification"):
            parser.add_argument(
                f"--confirm-{name}", action="store_true", help=f"确认已完成 {name} 操作。"
            )

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR)
        try:
            releases = validate_releases(project_root)
            if options["version"] != current_project_version(project_root):
                raise ReleaseProtocolError("只能确认当前代码版本")
            state = read_release_state(project_root)
            if state is None:
                raise ReleaseProtocolError("缺少本机基线；请先执行 upgrade_init")
            validate_local_release_state(project_root, releases, state)
            plan = build_upgrade_plan(
                releases,
                from_version=state["applied_version"],
                to_version=options["version"],
            )
            confirmations = {
                "dependencies": plan["dependencies"] != "none",
                "static": plan["static"] != "none",
                "restart": plan["restart"]["mode"] != "none",
                "manual": bool(plan["manual"]),
                "verification": bool(plan["verification"]),
            }
            missing = [
                name
                for name, required in confirmations.items()
                if required and not options[f"confirm_{name}"]
            ]
            if missing:
                raise ReleaseProtocolError(f"仍需显式确认：{', '.join(missing)}")
            if plan["database"] == "migrate":
                pending = pending_migrations()
                if pending:
                    raise ReleaseProtocolError(f"仍有未应用迁移：{', '.join(pending)}")
            incomplete = [
                status["id"]
                for job in plan["once_jobs"]
                if job["required"]
                for status in [once_job_status(job)]
                if not status["complete"]
            ]
            if incomplete:
                raise ReleaseProtocolError(f"一次性 Job 尚未完成：{', '.join(incomplete)}")
            write_release_state(
                project_root,
                {
                    "schema_version": 1,
                    "applied_version": options["version"],
                    "applied_commit": release_commit(project_root, releases, options["version"]),
                    "applied_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
        except ReleaseProtocolError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"已确认本机完成升级：{options['version']}。"))
