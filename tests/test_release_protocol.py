import json
from pathlib import Path

import pytest
import yaml

from jobs.models import BackgroundJob, JobStatus
from jobs.release_protocol import (
    ReleaseProtocolError,
    build_upgrade_plan,
    load_releases,
    once_job_status,
    read_release_state,
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


def test_release_catalog_validates_the_current_release_chain(tmp_path):
    first = _document()
    second = _document("1.0.1", "1.0.0")
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

    with pytest.raises(ReleaseProtocolError, match="tag 不一致"):
        validate_local_release_state(tmp_path, releases, state)


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
