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


def _write_release_manifest(
    root: Path, version: str, previous: str | None, changes: list[str]
) -> None:
    releases = root / "releases"
    releases.mkdir(exist_ok=True)
    change_lines = (
        ["changes: []"]
        if not changes
        else ["changes:", *[f"  - {change}" for change in changes]]
    )
    (releases / f"{version}.yaml").write_text(
        "\n".join(
            [
                f"version: {version}",
                f"previous: {previous if previous else 'null'}",
                *change_lines,
                "upgrade: {}",
                "verification: []",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _release_repository(
    tmp_path: Path,
    *,
    version="1.2.3",
    tag=None,
    runtime=False,
    previous_version: str | None = None,
    maintenance_subjects: tuple[str, ...] = (),
    platform_changes: list[str] | None = None,
) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Release test")
    _git(root, "config", "user.email", "release-test@example.invalid")
    (root / "README.md").write_text("release source\n", encoding="utf-8")
    if previous_version:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "test-project"\nversion = "{previous_version}"\n',
            encoding="utf-8",
        )
        _write_release_manifest(root, previous_version, None, ["上一版本的平台功能"])
        _git(root, "add", ".")
        _git(root, "commit", "-m", f"release: prepare v{previous_version}")
        _git(root, "tag", "-a", f"v{previous_version}", "-m", "previous release")
        for index, subject in enumerate(maintenance_subjects):
            (root / "README.md").write_text(f"maintenance {index}\n", encoding="utf-8")
            _git(root, "add", "README.md")
            _git(root, "commit", "-m", subject)

    (root / "pyproject.toml").write_text(
        (
            f'[project]\nname = "test-project"\nversion = "{version}"\n'
            'description = "中文发布测试"\n'
        ),
        encoding="utf-8",
    )
    _write_release_manifest(
        root,
        version,
        previous_version,
        ["当前版本的平台功能"] if platform_changes is None else platform_changes,
    )
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
            "wechat-content-operations-1.2.3/releases/",
            "wechat-content-operations-1.2.3/releases/1.2.3.yaml",
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
    root = _release_repository(tmp_path)
    artifact = builder.build_source_archive(root, "v1.2.3", tmp_path / "dist")
    commands = []

    notes = []

    def fake_gh(*arguments):
        commands.append(arguments)
        if arguments[:2] == ("release", "create"):
            notes_path = Path(arguments[arguments.index("--notes-file") + 1])
            notes.append(notes_path.read_text(encoding="utf-8"))
        return ""

    monkeypatch.setattr(builder, "_gh", fake_gh)

    builder.publish_source_archive(root, artifact)

    assert commands == [
        (
            "release",
            "create",
            "v1.2.3",
            "--verify-tag",
            "--title",
            "Release v1.2.3",
            "--notes-file",
            commands[0][-1],
        ),
        ("release", "upload", "v1.2.3", str(artifact.archive_path)),
        ("release", "upload", "v1.2.3", str(artifact.checksum_path)),
    ]
    assert notes == [
        "## 平台功能更新\n"
        "- 当前版本的平台功能\n\n"
        "## 工程与文档维护\n"
        "- 本版本没有工程或文档维护项。\n"
    ]


def test_publish_source_archive_verifies_existing_assets_without_uploading(tmp_path, monkeypatch):
    root = _release_repository(tmp_path)
    artifact = builder.build_source_archive(root, "v1.2.3", tmp_path / "dist")
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

    builder.publish_source_archive(root, artifact)

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


def test_release_notes_use_manifest_for_platform_and_commits_for_maintenance(tmp_path, monkeypatch):
    root = _release_repository(
        tmp_path,
        previous_version="1.2.2",
        maintenance_subjects=(
            "docs: update deployment guide",
            "ci: update release runtime",
            "feat: omit me",
        ),
    )
    artifact = builder.build_source_archive(root, "v1.2.3", tmp_path / "dist")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")

    assert builder.render_release_notes(root, artifact) == (
        "## 平台功能更新\n"
        "- 当前版本的平台功能\n\n"
        "## 工程与文档维护\n"
        "- docs: update deployment guide\n"
        "- ci: update release runtime\n\n"
        "## 完整提交记录\n"
        "[查看完整提交记录](https://github.com/example/project/compare/v1.2.2...v1.2.3)\n"
    )


def test_release_notes_identify_a_maintenance_only_release(tmp_path):
    root = _release_repository(tmp_path, platform_changes=[])
    artifact = builder.build_source_archive(root, "v1.2.3", tmp_path / "dist")

    assert builder.render_release_notes(root, artifact) == (
        "## 平台功能更新\n"
        "- 本版本没有平台功能更新。\n\n"
        "## 工程与文档维护\n"
        "- 本版本没有工程或文档维护项。\n"
    )


def _git_commit(root: Path, reference: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", f"{reference}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
