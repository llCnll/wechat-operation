import json
from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command
from django.test import override_settings

from jobs.models import BackgroundJob, JobStatus
from jobs.release_protocol import (
    RELEASE_PROVENANCE_FILENAME,
    ReleaseProtocolError,
    build_upgrade_plan,
    load_releases,
    once_job_status,
    read_release_provenance,
    read_release_state,
    release_commit,
    validate_local_release_state,
    validate_releases,
    write_release_state,
)


def _document(version="1.0.0", previous=None):
    return {
        "version": version,
        "previous": previous,
        "changes": ["测试发布"],
        "upgrade": {
            "dependencies": "none",
            "database": "none",
            "static": "none",
            "restart": {"mode": "none", "services": []},
            "once_jobs": [],
            "manual": [],
        },
        "verification": ["验证"],
    }


def _project(root: Path, documents: list[dict]):
    (root / "releases").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "test-project"\nversion = "{documents[-1]["version"]}"\n',
        encoding="utf-8",
    )
    for document in documents:
        (root / "releases" / f"{document['version']}.yaml").write_text(
            yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
        )


def _write_provenance(root: Path, documents: list[dict], commits: dict[str, str]) -> None:
    version = documents[-1]["version"]
    (root / RELEASE_PROVENANCE_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_version": version,
                "artifact_tag": f"v{version}",
                "artifact_commit": commits[version],
                "release_commits": commits,
            }
        ),
        encoding="utf-8",
    )


def test_release_catalog_validates_the_current_release_chain(tmp_path):
    first = _document()
    second = _document("1.0.1", "1.0.0")
    _project(tmp_path, [first, second])

    assert [release.version for release in validate_releases(tmp_path)] == ["1.0.0", "1.0.1"]


def test_release_catalog_allows_a_maintenance_release_without_platform_changes(tmp_path):
    first = _document()
    second = _document("1.0.1", "1.0.0")
    second["changes"] = []
    _project(tmp_path, [first, second])

    assert [release.version for release in validate_releases(tmp_path)] == ["1.0.0", "1.0.1"]


def test_release_catalog_rejects_a_once_job_without_exact_idempotency_key(tmp_path):
    document = _document()
    document["upgrade"]["once_jobs"] = [
        {
            "id": "backfill-v1",
            "enqueue": "manage.py enqueue",
            "required": True,
            "completion": {"task_type": "BACKFILL", "checkpoint_complete": True},
        }
    ]
    _project(tmp_path, [document])

    with pytest.raises(ReleaseProtocolError, match="idempotency_key"):
        load_releases(tmp_path)


def test_release_state_is_written_and_read_atomically_from_its_fixed_schema(tmp_path):
    state = {
        "schema_version": 1,
        "applied_version": "1.0.0",
        "applied_commit": "a" * 40,
        "applied_at": "2026-07-20T00:00:00Z",
    }

    write_release_state(tmp_path, state)

    assert read_release_state(tmp_path) == state
    assert json.loads((tmp_path / "runtime" / "release-state.json").read_text()) == state


def test_state_validation_rejects_a_commit_that_does_not_match_its_release_tag(
    monkeypatch, tmp_path
):
    document = _document()
    _project(tmp_path, [document])
    releases = load_releases(tmp_path)
    state = {
        "schema_version": 1,
        "applied_version": "1.0.0",
        "applied_commit": "a" * 40,
        "applied_at": "2026-07-20T00:00:00Z",
    }
    monkeypatch.setattr("jobs.release_protocol.tagged_commit", lambda *_: "b" * 40)

    with pytest.raises(ReleaseProtocolError, match="发布证据不一致"):
        validate_local_release_state(tmp_path, releases, state)


def test_zip_provenance_validates_release_state_without_a_git_directory(tmp_path):
    first = _document()
    second = _document("1.0.1", "1.0.0")
    commits = {"1.0.0": "a" * 40, "1.0.1": "b" * 40}
    _project(tmp_path, [first, second])
    _write_provenance(tmp_path, [first, second], commits)
    releases = validate_releases(tmp_path)
    state = {
        "schema_version": 1,
        "applied_version": "1.0.0",
        "applied_commit": commits["1.0.0"],
        "applied_at": "2026-08-11T00:00:00Z",
    }

    assert not (tmp_path / ".git").exists()
    assert read_release_provenance(tmp_path, releases).release_commits == commits
    assert release_commit(tmp_path, releases, "1.0.1") == commits["1.0.1"]
    validate_local_release_state(tmp_path, releases, state)


def test_zip_provenance_rejects_a_missing_historical_release_commit(tmp_path):
    first = _document()
    second = _document("1.0.1", "1.0.0")
    _project(tmp_path, [first, second])
    _write_provenance(tmp_path, [first, second], {"1.0.1": "b" * 40})
    releases = validate_releases(tmp_path)

    with pytest.raises(ReleaseProtocolError, match="完整发布版本映射"):
        read_release_provenance(tmp_path, releases)


def test_zip_upgrade_commands_plan_and_ack_without_git_metadata(tmp_path):
    first = _document()
    second = _document("1.0.1", "1.0.0")
    commits = {"1.0.0": "a" * 40, "1.0.1": "b" * 40}
    _project(tmp_path, [first, second])
    _write_provenance(tmp_path, [first, second], commits)
    write_release_state(
        tmp_path,
        {
            "schema_version": 1,
            "applied_version": "1.0.0",
            "applied_commit": commits["1.0.0"],
            "applied_at": "2026-08-11T00:00:00Z",
        },
    )
    package_output = StringIO()
    plan_output = StringIO()

    with override_settings(BASE_DIR=tmp_path):
        call_command("validate_release_package", stdout=package_output)
        call_command("upgrade_plan", "--json", stdout=plan_output)
        call_command("upgrade_ack", "1.0.1", "--confirm-verification")

    assert "v1.0.1" in package_output.getvalue()
    assert json.loads(plan_output.getvalue())["releases"] == ["1.0.1"]
    assert read_release_state(tmp_path)["applied_commit"] == commits["1.0.1"]


@pytest.mark.django_db
def test_upgrade_plan_and_once_job_status_use_the_exact_idempotency_key(tmp_path):
    release = _document()
    document = _document("1.0.1", "1.0.0")
    document["upgrade"]["once_jobs"] = [
        {
            "id": "backfill-v1",
            "enqueue": "manage.py enqueue",
            "required": True,
            "completion": {
                "task_type": "BACKFILL",
                "idempotency_key": "release:backfill:v1",
                "checkpoint_complete": True,
            },
        }
    ]
    releases = load_releases_for_plan(tmp_path, [release, document])
    plan = build_upgrade_plan(releases, from_version="1.0.0", to_version="1.0.1")
    job = plan["once_jobs"][0]
    BackgroundJob.objects.create(
        task_type="BACKFILL",
        idempotency_key="another:backfill:v1",
        status=JobStatus.SUCCEEDED,
        checkpoint={"complete": True},
    )
    assert once_job_status(job)["complete"] is False
    BackgroundJob.objects.create(
        task_type="BACKFILL",
        idempotency_key="release:backfill:v1",
        status=JobStatus.SUCCEEDED,
        checkpoint={"complete": True},
    )
    assert once_job_status(job)["complete"] is True


def load_releases_for_plan(root: Path, documents: list[dict]):
    _project(root, documents)
    return load_releases(root)
