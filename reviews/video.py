"""Local, bounded video evidence extraction. No upstream session material is accepted here."""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlparse

import httpx

from config.settings import BASE_DIR, RUNTIME_DIR, runtime


class VideoExtractionError(RuntimeError):
    """A classified, deliberately URL-free failure that is safe to persist."""


class VideoLimitError(VideoExtractionError):
    pass


_LOCAL_MEDIA_TOOLS = {
    "ffmpeg": RUNTIME_DIR / "video-tools/node_modules/@ffmpeg-installer/darwin-x64/ffmpeg",
    "ffprobe": RUNTIME_DIR / "video-tools/node_modules/ffprobe-static/bin/darwin/x64/ffprobe",
}


def _configured_media_tools_dir() -> Path | None:
    """Resolve an explicit tool directory only when it stays under runtime/."""
    configured = runtime.video_media_tools_dir.strip()
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(RUNTIME_DIR.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return resolved


def _tool_in_directory(directory: Path, name: str) -> str | None:
    names = (f"{name}.exe", name) if os.name == "nt" else (name,)
    for filename in names:
        candidate = directory / filename
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def media_tool_path(name: str) -> str | None:
    """Find a configured local media tool, a system tool, or the macOS fallback."""
    if runtime.video_media_tools_dir.strip():
        configured = _configured_media_tools_dir()
        return _tool_in_directory(configured, name) if configured else None
    if path := which(name):
        return path
    fallback = _LOCAL_MEDIA_TOOLS.get(name)
    if fallback and fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    return None


def media_tools_available() -> bool:
    return bool(media_tool_path("ffmpeg") and media_tool_path("ffprobe"))


def safe_error(exc: Exception) -> str:
    """Return only stable, URL-free error codes for persistent task state."""
    if isinstance(exc, VideoExtractionError):
        return str(exc)[:500]
    return exc.__class__.__name__


def _public_addresses(host: str) -> list[str]:
    try:
        addresses = {
            entry[4][0] for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise VideoExtractionError("video_dns_lookup_failed") from exc
    if not addresses:
        raise VideoExtractionError("video_dns_lookup_failed")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise VideoExtractionError("video_non_public_address")
    return sorted(addresses)


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise VideoExtractionError("video_url_not_allowed")
    _public_addresses(parsed.hostname)


def _connected_address(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    try:
        value = stream.get_extra_info("server_addr")
    except (AttributeError, OSError):
        return None
    return value[0] if isinstance(value, tuple) and value else None


class VideoMediaReader:
    """Download an HTTPS video without credentials, proxies, redirects or URL persistence."""

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=runtime.video_connect_timeout_seconds,
                read=runtime.video_read_timeout_seconds,
                write=runtime.video_read_timeout_seconds,
                pool=runtime.video_connect_timeout_seconds,
            ),
        )

    def download(self, url: str, destination: Path) -> None:
        current = url
        for _redirect in range(4):
            _validate_url(current)
            try:
                with self.client.stream(
                    "GET",
                    current,
                    headers={"Accept": "video/*"},
                ) as response:
                    connected = _connected_address(response)
                    if connected is None:
                        raise VideoExtractionError("video_peer_address_unavailable")
                    if not ipaddress.ip_address(connected).is_global:
                        raise VideoExtractionError("video_connected_to_non_public_address")
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoExtractionError("video_redirect_missing_location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length and int(length) > runtime.video_max_bytes:
                        raise VideoLimitError("video_byte_limit_exceeded")
                    written = 0
                    with destination.open("wb") as output:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if written > runtime.video_max_bytes:
                                raise VideoLimitError("video_byte_limit_exceeded")
                            output.write(chunk)
                    return
            except ValueError as exc:
                raise VideoExtractionError("video_invalid_response") from exc
            except httpx.HTTPError as exc:
                raise VideoExtractionError("video_uncredentialed_read_failed") from exc
        raise VideoExtractionError("video_redirect_limit_exceeded")


@contextmanager
def video_workdir():
    """A service-owned directory that is deleted on every normal error path."""
    with TemporaryDirectory(prefix="wechat-video-") as temporary:
        yield Path(temporary)


@dataclass(frozen=True)
class VideoProbe:
    duration_ms: int
    has_audio: bool


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise VideoExtractionError("video_tool_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoLimitError("video_processing_timeout") from exc
    except subprocess.CalledProcessError as exc:
        raise VideoExtractionError("video_media_not_supported") from exc


def probe_video(path: Path) -> VideoProbe:
    ffprobe = media_tool_path("ffprobe")
    if ffprobe is None:
        raise VideoExtractionError("video_tool_unavailable")
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        timeout=runtime.video_processing_timeout_seconds,
    )
    import json

    try:
        payload = json.loads(result.stdout)
        duration_ms = int(float(payload["format"]["duration"]) * 1000)
        has_audio = any(
            stream.get("codec_type") == "audio" for stream in payload.get("streams", [])
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoExtractionError("video_probe_invalid") from exc
    if duration_ms <= 0:
        raise VideoExtractionError("video_duration_invalid")
    return VideoProbe(duration_ms=duration_ms, has_audio=has_audio)


def candidate_times(duration_ms: int) -> list[int]:
    """Return left-closed, right-open candidate positions in milliseconds."""
    duration = duration_ms / 1000
    if duration <= 6:
        return list(range(0, duration_ms, 250))
    start = list(range(0, min(3000, duration_ms), 250))
    middle_end = max(3000, duration_ms - 3000)
    middle = list(range(3000, middle_end, 1000))
    end = list(range(max(3000, duration_ms - 3000), duration_ms, 250))
    return sorted(set(start + middle + end))


def extract_frame(video: Path, timestamp_ms: int, destination: Path) -> None:
    ffmpeg = media_tool_path("ffmpeg")
    if ffmpeg is None:
        raise VideoExtractionError("video_tool_unavailable")
    _run(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{timestamp_ms / 1000:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({runtime.video_max_frame_edge},iw)':-2",
            str(destination),
        ],
        timeout=runtime.video_processing_timeout_seconds,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise VideoExtractionError("frame_decode_failed")


def extract_audio(video: Path, destination: Path) -> None:
    ffmpeg = media_tool_path("ffmpeg")
    if ffmpeg is None:
        raise VideoExtractionError("video_tool_unavailable")
    _run(
        [ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(destination)],
        timeout=runtime.video_processing_timeout_seconds,
    )


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise VideoExtractionError("qr_detector_unavailable") from exc
    return cv2


def qr_present(frame: Path) -> bool:
    cv2 = _cv2()
    image = cv2.imread(str(frame))
    if image is None:
        raise VideoExtractionError("qr_detection_failed")
    try:
        found, _points = cv2.QRCodeDetector().detect(image)
    except Exception as exc:
        raise VideoExtractionError("qr_detection_failed") from exc
    return bool(found)


def dhash(frame: Path) -> int:
    cv2 = _cv2()
    image = cv2.imread(str(frame), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise VideoExtractionError("frame_decode_failed")
    resized = cv2.resize(image, (9, 8))
    bits = resized[:, 1:] > resized[:, :-1]
    return int("".join("1" if value else "0" for value in bits.flat), 2)


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def frame_data_url(frame: Path) -> str:
    encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def media_fingerprint(url: str) -> str:
    return sha256(url.encode()).hexdigest()
