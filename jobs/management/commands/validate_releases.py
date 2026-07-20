"""Validate release manifests before merge or after release tagging."""

from __future__ import annotations

import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.release_protocol import (
    ReleaseProtocolError,
    current_project_version,
    tagged_commit,
    validate_releases,
)


class Command(BaseCommand):
    help = "校验发布清单、当前项目版本和可选的当前发布 tag。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--require-current-tag", action="store_true", help="要求当前 tag 指向 HEAD。"
        )

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR)
        try:
            releases = validate_releases(project_root)
            version = current_project_version(project_root)
            if options["require_current_tag"]:
                head = subprocess.run(
                    ("git", "rev-parse", "HEAD"),
                    cwd=project_root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if tagged_commit(project_root, version) != head:
                    raise ReleaseProtocolError(f"v{version} 必须指向当前 HEAD")
        except (ReleaseProtocolError, OSError, subprocess.CalledProcessError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"发布清单有效：{len(releases)} 个版本，当前 {version}。")
        )
