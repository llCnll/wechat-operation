"""Display the deterministic upgrade work between local and checked-out releases."""

from __future__ import annotations

import json
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
    validate_local_release_state,
    validate_releases,
)


class Command(BaseCommand):
    help = "只读输出本机版本到当前代码版本的升级计划。"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于 Agent 解析。")

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR)
        try:
            releases = validate_releases(project_root)
            state = read_release_state(project_root)
            target_version = current_project_version(project_root)
            if state is None:
                payload = {
                    "status": "BASELINE_REQUIRED",
                    "target_version": target_version,
                    "available_versions": [release.version for release in releases],
                    "message": "缺少 runtime/release-state.json；请先完成核验后执行 upgrade_init。",
                }
            else:
                validate_local_release_state(project_root, releases, state)
                payload = build_upgrade_plan(
                    releases, from_version=state["applied_version"], to_version=target_version
                )
                required_jobs = [job for job in payload["once_jobs"] if job["required"]]
                payload["once_job_status"] = [once_job_status(job) for job in required_jobs]
                payload["pending_migrations"] = (
                    pending_migrations() if payload["database"] == "migrate" else []
                )
                payload["status"] = "READY" if not payload["releases"] else "UPGRADE_REQUIRED"
        except ReleaseProtocolError as exc:
            raise CommandError(str(exc)) from exc
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        self.stdout.write(rendered if options["json"] else rendered)
