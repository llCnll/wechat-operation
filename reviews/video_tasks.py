"""Background task handling for locally extracted video evidence."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from config.settings import runtime
from jobs.models import BackgroundJob
from jobs.task_types import VIDEO_EXTRACT_TASK
from posts.models import MediaType

from .adapters import OpenAiCompatibleOcrClient, SpeechTranscriptionClient
from .models import VideoEvidence, VideoEvidenceKind, VideoExtraction, VideoExtractionStatus
from .video import (
    VideoExtractionError,
    VideoLimitError,
    VideoMediaReader,
    candidate_times,
    dhash,
    extract_audio,
    extract_frame,
    frame_data_url,
    probe_video,
    qr_present,
    safe_error,
    video_workdir,
)

VIDEO_EXTRACTOR_VERSION = "v3"
QR_CONTIGUOUS_GAP_MS = 1_000
OCR_TEXT_SIMILARITY_THRESHOLD = 0.92


@dataclass
class QrSpan:
    start_ms: int
    end_ms: int


def merge_qr_timestamp(spans: list[QrSpan], timestamp_ms: int) -> bool:
    """Merge consecutive QR detections and return whether this starts a new span."""
    if spans and timestamp_ms - spans[-1].end_ms <= QR_CONTIGUOUS_GAP_MS:
        spans[-1].end_ms = timestamp_ms
        return False
    spans.append(QrSpan(start_ms=timestamp_ms, end_ms=timestamp_ms))
    return True


def normalized_ocr_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", "", value)


def is_duplicate_ocr_text(text: str, accepted: list[str]) -> bool:
    """Suppress near-identical OCR output from visually similar retained frames."""
    normalized = normalized_ocr_text(text)
    if not normalized:
        return True
    return any(
        normalized == previous
        or SequenceMatcher(a=normalized, b=previous, autojunk=False).ratio()
        >= OCR_TEXT_SIMILARITY_THRESHOLD
        for previous in accepted
    )


def _set_status(extraction: VideoExtraction, status: str, error: str = "", frame_count: int = 0):
    extraction.status = status
    extraction.error_summary = error[:500]
    extraction.frame_count = frame_count
    extraction.save(update_fields=("status", "error_summary", "frame_count", "updated_at"))


def _store_qr(extraction: VideoExtraction, span: QrSpan, sequence: int) -> None:
    VideoEvidence.objects.update_or_create(
        extraction=extraction,
        kind=VideoEvidenceKind.QR_CODE,
        sequence=sequence,
        defaults={
            "start_ms": span.start_ms,
            "end_ms": span.end_ms if span.end_ms > span.start_ms else None,
            "text": "检测到二维码",
            "provider": "opencv-detector",
        },
    )


def extract_video_handler(job: BackgroundJob) -> None:
    extraction = VideoExtraction.objects.select_related("media", "post").get(
        pk=int(job.parameter_summary["video_extraction_id"])
    )
    media = extraction.media
    if media.media_type != MediaType.VIDEO:
        _set_status(extraction, VideoExtractionStatus.FAILED, "video_media_invalid")
        return
    _set_status(extraction, VideoExtractionStatus.RUNNING)
    VideoEvidence.objects.filter(extraction=extraction).delete()
    partial: list[str] = []
    frame_count = 0
    try:
        with video_workdir() as directory:
            video = directory / "input.bin"
            VideoMediaReader().download(media.url, video)
            probe = probe_video(video)
            if extraction.duration_limit_seconds is None:
                extraction.duration_limit_seconds = runtime.video_max_duration_seconds
            extraction.duration_ms = probe.duration_ms
            extraction.save(
                update_fields=("duration_ms", "duration_limit_seconds", "updated_at")
            )
            if probe.duration_ms > extraction.duration_limit_seconds * 1000:
                _set_status(
                    extraction,
                    VideoExtractionStatus.SKIPPED_LIMIT,
                    "video_duration_limit_exceeded",
                )
                return
            hashes: list[int] = []
            selected: list[tuple[int, Path, bool]] = []
            qr_spans: list[QrSpan] = []
            for timestamp in candidate_times(probe.duration_ms):
                frame = directory / f"frame-{timestamp}.jpg"
                try:
                    extract_frame(video, timestamp, frame)
                except VideoExtractionError as exc:
                    if str(exc) != "frame_decode_failed":
                        raise
                    partial.append(safe_error(exc))
                    continue
                try:
                    has_qr = qr_present(frame)
                except VideoExtractionError as exc:
                    partial.append(safe_error(exc))
                    has_qr = False
                if has_qr:
                    if merge_qr_timestamp(qr_spans, timestamp):
                        selected.append((timestamp, frame, True))
                    continue
                try:
                    value = dhash(frame)
                except VideoExtractionError as exc:
                    partial.append(safe_error(exc))
                    continue
                if any(
                    (value ^ previous).bit_count() <= runtime.video_dhash_threshold
                    for previous in hashes
                ):
                    continue
                hashes.append(value)
                selected.append((timestamp, frame, False))
            for sequence, span in enumerate(qr_spans):
                _store_qr(extraction, span, sequence)
            selected.sort(key=lambda item: (not item[2], item[0]))
            selected = selected[: runtime.video_ocr_frame_budget]
            ocr_sequence = 0
            accepted_ocr_text: list[str] = []
            try:
                ocr = OpenAiCompatibleOcrClient()
            except Exception:
                partial.append("frame_ocr_failed")
            else:
                for timestamp, frame, _has_qr in selected:
                    try:
                        text, confidence = ocr.extract_text(frame_data_url(frame))
                    except Exception:
                        partial.append("frame_ocr_failed")
                        ocr_sequence += 1
                        continue
                    if text and not is_duplicate_ocr_text(text, accepted_ocr_text):
                        VideoEvidence.objects.create(
                            extraction=extraction,
                            kind=VideoEvidenceKind.FRAME_OCR,
                            start_ms=timestamp,
                            sequence=ocr_sequence,
                            text=text,
                            confidence=confidence,
                            provider="openai-compatible",
                        )
                        accepted_ocr_text.append(normalized_ocr_text(text))
                    ocr_sequence += 1
            if probe.has_audio:
                try:
                    audio = directory / "audio.wav"
                    extract_audio(video, audio)
                    asr = SpeechTranscriptionClient()
                    segments = asr.transcribe(audio.read_bytes())
                    for sequence, (start, end, text, confidence) in enumerate(segments):
                        VideoEvidence.objects.create(
                            extraction=extraction,
                            kind=VideoEvidenceKind.AUDIO_TRANSCRIPT,
                            start_ms=start,
                            end_ms=end or None,
                            sequence=sequence,
                            text=text,
                            confidence=confidence,
                            provider="openai-compatible-asr",
                        )
                except Exception:
                    partial.append("audio_transcript_unavailable")
            elif probe.has_audio is False:
                partial.append("audio_track_unavailable")
            frame_count = len(selected)
    except VideoLimitError as exc:
        _set_status(extraction, VideoExtractionStatus.SKIPPED_LIMIT, safe_error(exc), frame_count)
        return
    except Exception as exc:
        _set_status(extraction, VideoExtractionStatus.FAILED, safe_error(exc), frame_count)
        return
    _set_status(
        extraction,
        VideoExtractionStatus.PARTIAL if partial else VideoExtractionStatus.SUCCEEDED,
        ";".join(sorted(set(partial))),
        frame_count,
    )


WORKER_HANDLERS = {VIDEO_EXTRACT_TASK: extract_video_handler}
