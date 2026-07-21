import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def _load_builder():
    path = Path(__file__).parents[1] / "scripts" / "build_release_source_archive.py"
    spec = importlib.util.spec_from_file_location("build_release_source_archive", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _git(root: Path, *arguments: str):
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _release_repository(tmp_path: Path, *, version="1.2.3", tag=None, runtime=False) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Release test")
    _git(root, "config", "user.email", "release-test@example.invalid")
    (root / "pyproject.toml").write_text(
        (
            f'[project]\nname = "test-project"\nversion = "{version}"\n'
            'description = "中文发布测试"\n'
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("release source\n", encoding="utf-8")
    if runtime:
        (root / "runtime").mkdir()
        (root / "runtime" / "operations.sqlite3").write_text(
            "not a real database", encoding="utf-8"
        )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "release")
    _git(root, "tag", "-a", tag or f"v{version}", "-m", "release")
    return root


def test_build_source_archive_uses_tagged_files_and_writes_sha256(tmp_path):
    root = _release_repository(tmp_path)

    artifact = builder.build_source_archive(root, "v1.2.3", tmp_path / "dist")

    assert artifact.commit == _git_commit(root, "v1.2.3")
    assert artifact.checksum_path.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(artifact.archive_path.read_bytes()).hexdigest()}  "
        "wechat-content-operations-1.2.3-src.zip\n"
    )
    with zipfile.ZipFile(artifact.archive_path) as archive:
        assert archive.namelist() == [
            "wechat-content-operations-1.2.3/",
            "wechat-content-operations-1.2.3/README.md",
            "wechat-content-operations-1.2.3/pyproject.toml",
        ]


def test_build_source_archive_rejects_tag_and_project_version_mismatch(tmp_path):
    root = _release_repository(tmp_path, tag="v1.2.4")

    with pytest.raises(builder.SourceArchiveError, match="不匹配"):
        builder.build_source_archive(root, "v1.2.4", tmp_path / "dist")


def test_build_source_archive_rejects_tracked_runtime_directory(tmp_path):
    root = _release_repository(tmp_path, runtime=True)
    output_dir = tmp_path / "dist"

    with pytest.raises(builder.SourceArchiveError, match="禁止路径"):
        builder.build_source_archive(root, "v1.2.3", output_dir)

    assert not list(output_dir.glob("*.zip"))


def test_publish_source_archive_creates_release_and_uploads_both_assets(tmp_path, monkeypatch):
    artifact = builder.build_source_archive(
        _release_repository(tmp_path), "v1.2.3", tmp_path / "dist"
    )
    commands = []

    monkeypatch.setattr(builder, "_gh", lambda *arguments: commands.append(arguments) or "")

    builder.publish_source_archive(artifact)

    assert commands == [
        (
            "release",
            "create",
            "v1.2.3",
            "--verify-tag",
            "--title",
            "Release v1.2.3",
            "--generate-notes",
        ),
        ("release", "upload", "v1.2.3", str(artifact.archive_path)),
        ("release", "upload", "v1.2.3", str(artifact.checksum_path)),
    ]


def test_publish_source_archive_verifies_existing_assets_without_uploading(tmp_path, monkeypatch):
    artifact = builder.build_source_archive(
        _release_repository(tmp_path), "v1.2.3", tmp_path / "dist"
    )
    verified = []
    commands = []

    def fake_gh(*arguments):
        commands.append(arguments)
        if arguments[:2] == ("release", "create"):
            raise builder.SourceArchiveError("release already exists")
        if arguments[:2] == ("release", "view"):
            return json.dumps(
                {
                    "assets": [
                        {"name": artifact.archive_path.name},
                        {"name": artifact.checksum_path.name},
                    ]
                }
            )
        raise AssertionError(f"unexpected GitHub CLI command: {arguments}")

    monkeypatch.setattr(builder, "_gh", fake_gh)
    monkeypatch.setattr(
        builder,
        "_verify_existing_release_asset",
        lambda tag, path: verified.append((tag, path.name)),
    )

    builder.publish_source_archive(artifact)

    assert verified == [
        ("v1.2.3", artifact.archive_path.name),
        ("v1.2.3", artifact.checksum_path.name),
    ]
    assert not any(command[:2] == ("release", "upload") for command in commands)


def test_verify_existing_release_asset_rejects_different_bytes(tmp_path, monkeypatch):
    artifact = builder.build_source_archive(
        _release_repository(tmp_path), "v1.2.3", tmp_path / "dist"
    )

    def fake_gh(*arguments):
        download_dir = Path(arguments[arguments.index("--dir") + 1])
        (download_dir / artifact.archive_path.name).write_bytes(b"different archive")
        return ""

    monkeypatch.setattr(builder, "_gh", fake_gh)

    with pytest.raises(builder.SourceArchiveError, match="不一致"):
        builder._verify_existing_release_asset("v1.2.3", artifact.archive_path)


def _git_commit(root: Path, reference: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{reference}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
