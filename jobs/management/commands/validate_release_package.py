"""Validate the immutable provenance embedded in an extracted Release ZIP."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.release_protocol import (
    ReleaseProtocolError,
    read_release_provenance,
    validate_releases,
)


class Command(BaseCommand):
    help = "校验已解压 Release ZIP 的 provenance；执行前必须先校验 ZIP SHA-256。"

    def handle(self, *args, **options):
        project_root = Path(settings.BASE_DIR)
        try:
            releases = validate_releases(project_root)
            provenance = read_release_provenance(project_root, releases)
        except ReleaseProtocolError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "发布包 provenance 有效："
                f"{provenance.artifact_tag} -> {provenance.artifact_commit}。"
            )
        )
