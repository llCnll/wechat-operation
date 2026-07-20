"""Strict, local-only helpers for versioned deployment release protocols."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from jobs.models import BackgroundJob, JobStatus

SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
STATE_SCHEMA_VERSION = 1
RESTART_MODES = frozenset(("none", "services", "supervisor"))


class ReleaseProtocolError(ValueError):
    """A release file or local state file violates the upgrade contract."""


@dataclass(frozen=True)
class Release:
    version: str
    previous: str | None
    document: dict[str, Any]
    path: Path


def _version_key(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not SEMVER_PATTERN.fullmatch(value):
        raise ReleaseProtocolError(f"版本必须为三段 SemVer：{value!r}")
    return tuple(int(part) for part in value.split("."))


def current_project_version(project_root: Path) -> str:
    with (project_root / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source).get("project", {})
    version = project.get("version")
    _version_key(version)
    return version


def _require_mapping(value: object, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseProtocolError(f"{context} 必须为对象")
    return value


def _require_string_list(value: object, *, context: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ReleaseProtocolError(f"{context} 必须为非空字符串列表")
    return value


def _validate_once_job(value: object, *, context: str) -> None:
    job = _require_mapping(value, context=context)
    for key in ("id", "enqueue", "required"):
        if key not in job:
            raise ReleaseProtocolError(f"{context} 缺少 {key}")
    if not isinstance(job["id"], str) or not job["id"]:
        raise ReleaseProtocolError(f"{context}.id 必须为非空字符串")
    if not isinstance(job["enqueue"], str) or not job["enqueue"]:
        raise ReleaseProtocolError(f"{context}.enqueue 必须为非空字符串")
    if not isinstance(job["required"], bool):
        raise ReleaseProtocolError(f"{context}.required 必须为布尔值")
    completion = _require_mapping(job.get("completion"), context=f"{context}.completion")
    for key in ("task_type", "idempotency_key", "checkpoint_complete"):
        if key not in completion:
            raise ReleaseProtocolError(f"{context}.completion 缺少 {key}")
    if not all(
        isinstance(completion[key], str) and completion[key]
        for key in ("task_type", "idempotency_key")
    ):
        raise ReleaseProtocolError(f"{context}.completion 任务标识必须为非空字符串")
    if not isinstance(completion["checkpoint_complete"], bool):
        raise ReleaseProtocolError(f"{context}.completion.checkpoint_complete 必须为布尔值")


def _validate_document(document: dict[str, Any], *, path: Path) -> Release:
    required = {"version", "previous", "changes", "upgrade", "verification"}
    missing = required - document.keys()
    if missing:
        raise ReleaseProtocolError(f"{path.name} 缺少字段：{', '.join(sorted(missing))}")
    version = document["version"]
    _version_key(version)
    if path.stem != version:
        raise ReleaseProtocolError(f"{path.name} 必须与声明版本 {version} 一致")
    previous = document["previous"]
    if previous is not None:
        _version_key(previous)
    _require_string_list(document["changes"], context=f"{path.name}.changes")
    _require_string_list(document["verification"], context=f"{path.name}.verification")
    upgrade = _require_mapping(document["upgrade"], context=f"{path.name}.upgrade")
    expected_upgrade_fields = {
        "dependencies",
        "database",
        "static",
        "restart",
        "once_jobs",
        "manual",
    }
    if set(upgrade) != expected_upgrade_fields:
        raise ReleaseProtocolError(
            f"{path.name}.upgrade 字段必须为 {sorted(expected_upgrade_fields)}"
        )
    if upgrade["dependencies"] not in ("none", "uv-sync"):
        raise ReleaseProtocolError(f"{path.name}.upgrade.dependencies 无效")
    if upgrade["database"] not in ("none", "migrate"):
        raise ReleaseProtocolError(f"{path.name}.upgrade.database 无效")
    if upgrade["static"] not in ("none", "collectstatic"):
        raise ReleaseProtocolError(f"{path.name}.upgrade.static 无效")
    restart = _require_mapping(upgrade["restart"], context=f"{path.name}.upgrade.restart")
    if set(restart) != {"mode", "services"} or restart["mode"] not in RESTART_MODES:
        raise ReleaseProtocolError(f"{path.name}.upgrade.restart 无效")
    if not isinstance(restart["services"], list) or not all(
        isinstance(service, str) and service for service in restart["services"]
    ):
        raise ReleaseProtocolError(f"{path.name}.upgrade.restart.services 必须为字符串列表")
    if restart["mode"] != "services" and restart["services"]:
        raise ReleaseProtocolError(f"{path.name}.upgrade.restart 仅 services 模式可声明服务")
    if not isinstance(upgrade["once_jobs"], list):
        raise ReleaseProtocolError(f"{path.name}.upgrade.once_jobs 必须为列表")
    for index, job in enumerate(upgrade["once_jobs"]):
        _validate_once_job(job, context=f"{path.name}.upgrade.once_jobs[{index}]")
    if not isinstance(upgrade["manual"], list) or not all(
        isinstance(item, str) and item for item in upgrade["manual"]
    ):
        raise ReleaseProtocolError(f"{path.name}.upgrade.manual 必须为字符串列表")
    return Release(version=version, previous=previous, document=document, path=path)


def load_releases(project_root: Path) -> list[Release]:
    release_dir = project_root / "releases"
    if not release_dir.is_dir():
        raise ReleaseProtocolError("缺少 releases 目录")
    releases: list[Release] = []
    for path in sorted(release_dir.glob("*.yaml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ReleaseProtocolError(f"{path.name} 不是有效 YAML") from exc
        releases.append(_validate_document(_require_mapping(loaded, context=path.name), path=path))
    if not releases:
        raise ReleaseProtocolError("至少需要一份发布清单")
    releases.sort(key=lambda release: _version_key(release.version))
    for index, release in enumerate(releases):
        expected_previous = None if index == 0 else releases[index - 1].version
        if release.previous != expected_previous:
            raise ReleaseProtocolError(
                f"{release.path.name} 的 previous 必须为 {expected_previous!r}"
            )
    return releases


def validate_releases(project_root: Path) -> list[Release]:
    releases = load_releases(project_root)
    if releases[-1].version != current_project_version(project_root):
        raise ReleaseProtocolError("pyproject.toml 版本必须等于最新发布清单版本")
    seen_job_ids: set[str] = set()
    for release in releases:
        for job in release.document["upgrade"]["once_jobs"]:
            job_id = job["id"]
            if job_id in seen_job_ids:
                raise ReleaseProtocolError(f"一次性 Job 发布标识重复：{job_id}")
            seen_job_ids.add(job_id)
    return releases


def release_for_version(releases: list[Release], version: str) -> Release:
    for release in releases:
        if release.version == version:
            return release
    raise ReleaseProtocolError(f"不存在发布版本：{version}")


def build_upgrade_plan(
    releases: list[Release], *, from_version: str, to_version: str
) -> dict[str, Any]:
    from_index = next(
        (index for index, item in enumerate(releases) if item.version == from_version), None
    )
    to_index = next(
        (index for index, item in enumerate(releases) if item.version == to_version), None
    )
    if from_index is None or to_index is None:
        raise ReleaseProtocolError("本机版本或目标版本不在发布链中")
    if from_index > to_index:
        raise ReleaseProtocolError("本机版本不能高于当前代码版本")
    applicable = releases[from_index + 1 : to_index + 1]
    restart_mode = "none"
    restart_services: list[str] = []
    once_jobs: list[dict[str, Any]] = []
    result = {
        "from_version": from_version,
        "to_version": to_version,
        "releases": [release.version for release in applicable],
        "dependencies": "uv-sync" if any(
            release.document["upgrade"]["dependencies"] == "uv-sync" for release in applicable
        ) else "none",
        "database": "migrate" if any(
            release.document["upgrade"]["database"] == "migrate" for release in applicable
        ) else "none",
        "static": "collectstatic" if any(
            release.document["upgrade"]["static"] == "collectstatic" for release in applicable
        ) else "none",
        "once_jobs": once_jobs,
        "manual": [],
        "verification": [],
    }
    for release in applicable:
        upgrade = release.document["upgrade"]
        restart = upgrade["restart"]
        if restart["mode"] == "supervisor":
            restart_mode = "supervisor"
            restart_services = []
        elif restart["mode"] == "services" and restart_mode != "supervisor":
            restart_mode = "services"
            restart_services.extend(restart["services"])
        once_jobs.extend(upgrade["once_jobs"])
        result["manual"].extend(upgrade["manual"])
        result["verification"].extend(release.document["verification"])
    result["restart"] = {"mode": restart_mode, "services": sorted(set(restart_services))}
    return result


def release_state_path(project_root: Path) -> Path:
    return project_root / "runtime" / "release-state.json"


def read_release_state(project_root: Path) -> dict[str, str] | None:
    path = release_state_path(project_root)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseProtocolError("本机发布状态文件无效") from exc
    if not isinstance(state, dict) or set(state) != {
        "schema_version",
        "applied_version",
        "applied_commit",
        "applied_at",
    }:
        raise ReleaseProtocolError("本机发布状态字段无效")
    if state["schema_version"] != STATE_SCHEMA_VERSION:
        raise ReleaseProtocolError("本机发布状态版本不受支持")
    _version_key(state["applied_version"])
    if not isinstance(state["applied_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", state["applied_commit"]
    ):
        raise ReleaseProtocolError("本机发布状态提交无效")
    if not isinstance(state["applied_at"], str) or not state["applied_at"].endswith("Z"):
        raise ReleaseProtocolError("本机发布状态时间必须为 UTC ISO-8601")
    return state


def write_release_state(project_root: Path, state: dict[str, object]) -> None:
    path = release_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".release-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
            json.dump(state, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def tagged_commit(project_root: Path, version: str) -> str:
    tag = f"v{version}"
    try:
        result = subprocess.run(
            ("git", "rev-list", "-n", "1", tag),
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseProtocolError(f"缺少发布 tag：{tag}") from exc
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseProtocolError(f"发布 tag 无法解析提交：{tag}")
    return commit


def validate_local_release_state(
    project_root: Path, releases: list[Release], state: dict[str, str]
) -> None:
    release_for_version(releases, state["applied_version"])
    expected_commit = tagged_commit(project_root, state["applied_version"])
    if state["applied_commit"] != expected_commit:
        raise ReleaseProtocolError("本机发布状态提交与其版本 tag 不一致")


def pending_migrations() -> list[str]:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


def once_job_status(job: dict[str, Any]) -> dict[str, Any]:
    completion = job["completion"]
    background_job = BackgroundJob.objects.filter(
        idempotency_key=completion["idempotency_key"]
    ).first()
    if background_job is None:
        return {"id": job["id"], "complete": False, "reason": "任务不存在"}
    complete = (
        background_job.task_type == completion["task_type"]
        and background_job.status == JobStatus.SUCCEEDED
        and (
            not completion["checkpoint_complete"]
            or bool((background_job.checkpoint or {}).get("complete"))
        )
    )
    return {
        "id": job["id"],
        "complete": complete,
        "job_id": background_job.pk,
        "status": background_job.status,
        "checkpoint_complete": bool((background_job.checkpoint or {}).get("complete")),
        "reason": "" if complete else "任务状态、类型或检查点不符合发布要求",
    }
