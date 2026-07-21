"""Build a release source archive from an immutable Git tag."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path


class SourceArchiveError(RuntimeError):
    """Raised when a release source archive cannot be safely produced."""


FORBIDDEN_TOP_LEVEL_PATHS = frozenset({"runtime", ".git", ".venv"})


@dataclass(frozen=True)
class SourceArchive:
    version: str
    tag: str
    commit: str
    archive_path: Path
    checksum_path: Path


def _run(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        message = (error.stderr or error.stdout or b"Git command failed").decode(
            "utf-8", errors="replace"
        ).strip()
        raise SourceArchiveError(message) from error
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _git(root: Path, *args: str) -> str:
    return _run(["git", "-C", str(root), *args])


def _gh(*args: str) -> str:
    return _run(["gh", *args])


def _version_at_tag(root: Path, tag: str) -> str:
    try:
        document = tomllib.loads(_git(root, "show", f"{tag}:pyproject.toml"))
        return document["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError) as error:
        raise SourceArchiveError(f"{tag} 中的 pyproject.toml 缺少有效项目版本") from error


def _validate_archive(archive_path: Path, prefix: str) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as error:
        raise SourceArchiveError(f"源码归档无效：{archive_path}") from error

    if not names or any(not name.startswith(prefix) for name in names):
        raise SourceArchiveError("源码归档必须只包含一个版本化顶层目录")

    for name in names:
        relative_name = name.removeprefix(prefix).rstrip("/")
        if not relative_name:
            continue
        if relative_name.split("/", 1)[0] in FORBIDDEN_TOP_LEVEL_PATHS:
            raise SourceArchiveError(f"源码归档包含禁止路径：{name}")


def _write_sha256(archive_path: Path) -> Path:
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = archive_path.with_suffix(f"{archive_path.suffix}.sha256")
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return checksum_path


def build_source_archive(root: Path, tag: str, output_dir: Path) -> SourceArchive:
    root = root.resolve()
    if not tag.startswith("v") or "/" in tag or "\\" in tag:
        raise SourceArchiveError(f"发布 tag 无效：{tag}")
    _git(root, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}")
    commit = _git(root, "rev-parse", f"{tag}^{{commit}}")
    version = _version_at_tag(root, tag)
    if tag != f"v{version}":
        raise SourceArchiveError(f"tag {tag} 与项目版本 {version} 不匹配")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"wechat-content-operations-{version}-src.zip"
    prefix = f"wechat-content-operations-{version}/"
    try:
        _git(
            root,
            "archive",
            "--format=zip",
            f"--prefix={prefix}",
            f"--output={archive_path}",
            tag,
        )
        _validate_archive(archive_path, prefix)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    return SourceArchive(
        version=version,
        tag=tag,
        commit=commit,
        archive_path=archive_path,
        checksum_path=_write_sha256(archive_path),
    )


def _release_asset_names(tag: str) -> set[str]:
    response = _gh("release", "view", tag, "--json", "assets")
    try:
        return {asset["name"] for asset in json.loads(response)["assets"]}
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SourceArchiveError(f"无法读取 Release {tag} 的附件列表") from error


def _verify_existing_release_asset(tag: str, artifact_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="wechat-release-asset-") as directory:
        download_dir = Path(directory)
        _gh("release", "download", tag, "--pattern", artifact_path.name, "--dir", str(download_dir))
        downloaded_path = download_dir / artifact_path.name
        same_content = downloaded_path.is_file() and (
            downloaded_path.read_bytes() == artifact_path.read_bytes()
        )
        if not same_content:
            raise SourceArchiveError(
                f"已有 Release 附件与 {tag} 构建结果不一致：{artifact_path.name}"
            )


def publish_source_archive(artifact: SourceArchive) -> None:
    try:
        _gh(
            "release",
            "create",
            artifact.tag,
            "--verify-tag",
            "--title",
            f"Release {artifact.tag}",
            "--generate-notes",
        )
        existing_assets: set[str] = set()
    except SourceArchiveError:
        try:
            existing_assets = _release_asset_names(artifact.tag)
        except SourceArchiveError as view_error:
            raise SourceArchiveError(f"无法创建或读取 Release {artifact.tag}") from view_error

    for artifact_path in (artifact.archive_path, artifact.checksum_path):
        if artifact_path.name in existing_assets:
            _verify_existing_release_asset(artifact.tag, artifact_path)
        else:
            _gh("release", "upload", artifact.tag, str(artifact_path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="与 pyproject.toml 版本匹配的 v<version> tag")
    parser.add_argument("--output-dir", required=True, type=Path, help="归档输出目录")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Git 仓库根目录")
    parser.add_argument(
        "--publish-release", action="store_true", help="发布或核验 GitHub Release 附件"
    )
    arguments = parser.parse_args()
    artifact = build_source_archive(arguments.root, arguments.tag, arguments.output_dir)
    if arguments.publish_release:
        publish_source_archive(artifact)
    print(artifact.archive_path)
    print(artifact.checksum_path)


if __name__ == "__main__":
    main()
