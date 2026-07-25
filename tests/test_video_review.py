from __future__ import annotations

import logging
import os
import subprocess

import httpx
import pytest
from django.contrib import admin
from django.db import IntegrityError
from django.test import RequestFactory

from config.settings import SensitiveDataFilter
from jobs.models import BackgroundJob, JobStatus
from jobs.services import execute_next_job, recover_interrupted_jobs
from jobs.task_types import VIDEO_EXTRACT_TASK
from posts.models import CandidatePost, MediaType, OriginalPost, OriginType, PostMedia
from posts.services import classify_content_card
from reviews.adapters import SourceTransportError, SpeechTranscriptionClient
from reviews.models import (
    VideoEvidence,
    VideoEvidenceKind,
    VideoExtraction,
    VideoExtractionStatus,
)
from reviews.services import build_evaluation_input, enqueue_evaluation_pipeline
from reviews.tasks import WORKER_HANDLERS as REVIEW_WORKER_HANDLERS
from reviews.video import (
    VideoExtractionError,
    VideoLimitError,
    VideoMediaReader,
    VideoProbe,
    VideoToolCommandError,
    candidate_times,
    extract_frame,
    media_fingerprint,
    media_tool_path,
    safe_error,
    video_workdir,
)
from reviews.video_tasks import (
    VIDEO_EXTRACTOR_VERSION,
    QrSpan,
    _store_qr,
    extract_video_handler,
    is_duplicate_ocr_text,
    merge_qr_timestamp,
    normalized_ocr_text,
)


def _card(*, image_url: str = "", video_url: str = "") -> dict:
    return {
        "base_info": {
            "base_content": {
                "title": "视频样本",
                "content": "正文",
                "image_list": [{"url": image_url}] if image_url else [],
                "video_info": [{"url": video_url}] if video_url else [],
            }
        }
    }


def test_asr_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr("reviews.adapters.runtime.asr_enabled", False)

    with pytest.raises(SourceTransportError, match="ASR 未启用"):
        SpeechTranscriptionClient()


@pytest.mark.django_db
def test_media_projection_preserves_unchanged_video_row():
    candidate = CandidatePost.objects.create(topic_id=301, content_id="video-301")
    post = classify_content_card(candidate, _card(video_url="https://media.example/video.mp4"))
    media = post.media.get()
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version="v1",
    )
    refreshed = classify_content_card(candidate, _card(video_url="https://media.example/video.mp4"))
    assert refreshed.media.get().pk == media.pk
    assert VideoExtraction.objects.get(pk=extraction.pk).media_id == media.pk


@pytest.mark.django_db
def test_media_projection_adds_replaces_and_removes_video_rows_without_recreating_unchanged_media():
    candidate = CandidatePost.objects.create(topic_id=310, content_id="video-310")
    post = classify_content_card(candidate, _card(video_url="https://media.example/first.mp4"))
    first = post.media.get()
    extraction = VideoExtraction.objects.create(
        post=post,
        media=first,
        media_fingerprint=media_fingerprint(first.url),
        extractor_version="v1",
    )
    post = classify_content_card(
        candidate,
        {
            "base_info": {
                "base_content": {
                    "title": "视频样本",
                    "content": "正文",
                    "image_list": [],
                    "video_info": [
                        {"url": "https://media.example/first.mp4"},
                        {"url": "https://media.example/second.mp4"},
                    ],
                }
            }
        },
    )
    assert post.media.get(url="https://media.example/first.mp4").pk == first.pk
    assert post.media.filter(media_type=MediaType.VIDEO).count() == 2

    classify_content_card(candidate, _card(video_url="https://media.example/second.mp4"))
    assert not PostMedia.objects.filter(pk=first.pk).exists()
    assert not VideoExtraction.objects.filter(pk=extraction.pk).exists()
    assert post.media.get(url="https://media.example/second.mp4").media_type == MediaType.VIDEO


class _NetworkStream:
    def __init__(self, address: str):
        self.address = address

    def get_extra_info(self, name: str):
        return (self.address, 443) if name == "server_addr" else None


def test_video_reader_uses_uncredentialed_no_proxy_bounded_streaming(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            content=b"video-bytes",
            extensions={"network_stream": _NetworkStream("8.8.8.8")},
        )

    monkeypatch.setattr("reviews.video._public_addresses", lambda _host: ["8.8.8.8"])
    reader = VideoMediaReader(
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    )

    reader.download("https://media.example/video.mp4", tmp_path / "video.bin")

    assert (tmp_path / "video.bin").read_bytes() == b"video-bytes"
    assert reader.client._trust_env is False
    assert requests[0].headers["accept"] == "video/*"
    assert "cookie" not in requests[0].headers
    assert "authorization" not in requests[0].headers


def test_video_reader_rejects_non_public_peer_and_limits_download(monkeypatch, tmp_path):
    monkeypatch.setattr("reviews.video._public_addresses", lambda _host: ["8.8.8.8"])
    private_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"video-bytes",
                extensions={"network_stream": _NetworkStream("127.0.0.1")},
            )
        ),
        trust_env=False,
    )
    with pytest.raises(VideoExtractionError, match="video_connected_to_non_public_address"):
        VideoMediaReader(client=private_client).download(
            "https://media.example/video.mp4", tmp_path / "video.bin"
        )

    oversized_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-length": "999"},
                extensions={"network_stream": _NetworkStream("8.8.8.8")},
            )
        ),
        trust_env=False,
    )
    monkeypatch.setattr("reviews.video.runtime.video_max_bytes", 16)
    with pytest.raises(VideoLimitError, match="video_byte_limit_exceeded"):
        VideoMediaReader(client=oversized_client).download(
            "https://media.example/video.mp4", tmp_path / "video.bin"
        )


def test_video_reader_revalidates_redirects_and_rejects_unsafe_addresses(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example/video.mp4"},
                extensions={"network_stream": _NetworkStream("8.8.8.8")},
            )
        return httpx.Response(
            200,
            content=b"video-bytes",
            extensions={"network_stream": _NetworkStream("8.8.8.8")},
        )

    monkeypatch.setattr("reviews.video._public_addresses", lambda _host: ["8.8.8.8"])
    VideoMediaReader(
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    ).download("https://media.example/video.mp4", tmp_path / "video.bin")
    assert calls == ["https://media.example/video.mp4", "https://cdn.example/video.mp4"]

    with pytest.raises(VideoExtractionError, match="video_url_not_allowed"):
        VideoMediaReader().download("http://media.example/video.mp4", tmp_path / "video.bin")

    monkeypatch.setattr(
        "reviews.video._public_addresses",
        lambda _host: (_ for _ in ()).throw(VideoExtractionError("video_non_public_address")),
    )
    with pytest.raises(VideoExtractionError, match="video_non_public_address"):
        VideoMediaReader().download("https://media.example/video.mp4", tmp_path / "video.bin")


def test_video_workdir_cleans_up_on_success_and_error():
    with video_workdir() as directory:
        marker = directory / "marker"
        marker.write_text("temporary")
    assert not directory.exists()

    with pytest.raises(RuntimeError):
        with video_workdir() as directory:
            raise RuntimeError("boom")
    assert not directory.exists()


def test_video_errors_do_not_persist_unexpected_exception_messages():
    assert safe_error(VideoExtractionError("video_dns_lookup_failed")) == "video_dns_lookup_failed"
    assert safe_error(ValueError("https://private.example/signed-token")) == "ValueError"


def test_explicit_media_tools_dir_resolves_relative_to_project_runtime(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    runtime_dir = project_root / "runtime"
    tools_dir = runtime_dir / "video-tools" / "bin"
    tools_dir.mkdir(parents=True)
    ffmpeg = tools_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    ffprobe = tools_dir / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffmpeg.touch()
    ffprobe.touch()
    if os.name != "nt":
        ffmpeg.chmod(0o755)
        ffprobe.chmod(0o755)

    monkeypatch.setattr("reviews.video.BASE_DIR", project_root)
    monkeypatch.setattr("reviews.video.RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr("reviews.video.runtime.video_media_tools_dir", "runtime/video-tools/bin")

    assert media_tool_path("ffmpeg") == str(ffmpeg)
    assert media_tool_path("ffprobe") == str(ffprobe)


def test_explicit_media_tools_dir_rejects_paths_outside_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr("reviews.video.BASE_DIR", tmp_path)
    monkeypatch.setattr("reviews.video.RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr("reviews.video.runtime.video_media_tools_dir", "outside/bin")
    monkeypatch.setattr("reviews.video.which", lambda _name: "/system/ffmpeg")

    assert media_tool_path("ffmpeg") is None


def test_http_logs_redact_media_urls_even_without_query_parameters():
    record = logging.LogRecord(
        "httpx", 20, __file__, 0, "GET https://media.example/video.mp4 completed", (), None
    )

    assert SensitiveDataFilter().filter(record) is True
    assert record.getMessage() == "[已脱敏的日志消息]"


def test_dense_candidate_times_have_explicit_boundaries():
    assert len(candidate_times(30_000)) == 48
    assert len(candidate_times(60_000)) == 78
    assert candidate_times(1_000) == [0, 250, 500, 750]


def test_extract_frame_requires_a_nonempty_output_file(monkeypatch, tmp_path):
    monkeypatch.setattr("reviews.video.media_tool_path", lambda _name: "ffmpeg")
    monkeypatch.setattr("reviews.video._run", lambda *_args, **_kwargs: None)

    with pytest.raises(VideoExtractionError, match="frame_decode_failed"):
        extract_frame(tmp_path / "input.bin", 5_000, tmp_path / "frame.jpg")


def test_extract_frame_classifies_ffmpeg_command_failure_by_stage(monkeypatch, tmp_path):
    monkeypatch.setattr("reviews.video.media_tool_path", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        "reviews.video.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(9, ["ffmpeg"])
        ),
    )

    with pytest.raises(VideoToolCommandError) as captured:
        extract_frame(tmp_path / "input.bin", 0, tmp_path / "frame.jpg")

    assert str(captured.value) == "video_frame_extract_command_failed:exit_9"
    assert captured.value.stage == "frame_extract"
    assert captured.value.return_code == 9


@pytest.mark.django_db
def test_video_worker_only_recovers_its_own_interrupted_jobs():
    video_job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="recover-video-only",
        parameter_summary={"topic_id": 311},
        status=JobStatus.RUNNING,
    )
    ocr_job = BackgroundJob.objects.create(
        task_type="OCR_EXTRACT",
        idempotency_key="recover-ocr-not-video",
        parameter_summary={"topic_id": 311},
        status=JobStatus.RUNNING,
    )

    assert recover_interrupted_jobs(task_types=("VIDEO_EXTRACT",)) == 1
    video_job.refresh_from_db()
    ocr_job.refresh_from_db()
    assert video_job.status == JobStatus.PENDING
    assert ocr_job.status == JobStatus.RUNNING


def test_continuous_qr_frames_are_merged_but_separate_appearances_remain_distinct():
    spans: list[QrSpan] = []

    assert merge_qr_timestamp(spans, 4_250) is True
    assert merge_qr_timestamp(spans, 4_500) is False
    assert merge_qr_timestamp(spans, 5_250) is False
    assert merge_qr_timestamp(spans, 7_000) is True
    assert [(span.start_ms, span.end_ms) for span in spans] == [(4_250, 5_250), (7_000, 7_000)]


def test_ocr_text_similarity_deduplicates_only_near_identical_output():
    accepted = [normalized_ocr_text("微信扫码咨询客服")]

    assert is_duplicate_ocr_text("微信 扫码咨询客服！", accepted)
    assert not is_duplicate_ocr_text("获得 10 万奖励，搜索群号领取", accepted)


@pytest.mark.django_db
def test_video_worker_keeps_qr_when_detector_and_ocr_are_partially_unavailable(monkeypatch):
    post = OriginalPost.objects.create(topic_id=313, content_id="video-313")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="video-313",
        parameter_summary={"video_extraction_id": extraction.pk},
    )
    monkeypatch.setattr("reviews.video_tasks.VideoMediaReader.download", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.probe_video", lambda _path: VideoProbe(500, False))
    monkeypatch.setattr("reviews.video_tasks.candidate_times", lambda _duration: [0, 250])
    monkeypatch.setattr("reviews.video_tasks.extract_frame", lambda *_args: None)
    monkeypatch.setattr(
        "reviews.video_tasks.qr_present",
        lambda _frame: (_ for _ in ()).throw(VideoExtractionError("qr_detector_unavailable")),
    )
    monkeypatch.setattr("reviews.video_tasks.dhash", lambda _frame: 1)

    extract_video_handler(job)
    extraction.refresh_from_db()

    assert extraction.status == VideoExtractionStatus.PARTIAL
    assert "qr_detector_unavailable" in extraction.error_summary
    assert "audio_track_unavailable" in extraction.error_summary


@pytest.mark.django_db
def test_failed_video_extraction_marks_its_job_failed_and_logs_safe_stage(monkeypatch, caplog):
    post = OriginalPost.objects.create(topic_id=3131, content_id="video-failure")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    job = BackgroundJob.objects.create(
        task_type=VIDEO_EXTRACT_TASK,
        idempotency_key="video-failure",
        parameter_summary={"video_extraction_id": extraction.pk},
    )
    monkeypatch.setattr("reviews.video_tasks.VideoMediaReader.download", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.probe_video", lambda _path: VideoProbe(500, False))
    monkeypatch.setattr("reviews.video_tasks.candidate_times", lambda _duration: [0])
    monkeypatch.setattr(
        "reviews.video_tasks.extract_frame",
        lambda *_args: (_ for _ in ()).throw(
            VideoToolCommandError(stage="frame_extract", return_code=9)
        ),
    )
    caplog.set_level(logging.WARNING, logger="reviews.video_tasks")

    processed = execute_next_job({VIDEO_EXTRACT_TASK: extract_video_handler})

    assert processed is not None
    extraction.refresh_from_db()
    job.refresh_from_db()
    assert extraction.status == VideoExtractionStatus.FAILED
    assert extraction.error_summary == "video_frame_extract_command_failed:exit_9"
    assert job.status == JobStatus.FAILED
    assert job.error_summary == extraction.error_summary
    record = next(record for record in caplog.records if record.message == "视频提取失败")
    assert record.topic_id == post.topic_id
    assert record.result_code == extraction.error_summary
    assert record.video_stage == "frame_extract"
    assert record.process_exit_code == 9


@pytest.mark.django_db
def test_video_worker_prioritizes_qr_frames_before_the_ocr_budget(monkeypatch):
    post = OriginalPost.objects.create(topic_id=314, content_id="video-314")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="video-314",
        parameter_summary={"video_extraction_id": extraction.pk},
    )
    monkeypatch.setattr("reviews.video_tasks.VideoMediaReader.download", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.probe_video", lambda _path: VideoProbe(750, False))
    monkeypatch.setattr("reviews.video_tasks.candidate_times", lambda _duration: [0, 250, 500])
    monkeypatch.setattr("reviews.video_tasks.extract_frame", lambda *_args: None)
    monkeypatch.setattr(
        "reviews.video_tasks.qr_present",
        lambda frame: "frame-0" in str(frame) or "frame-250" in str(frame),
    )
    monkeypatch.setattr("reviews.video_tasks.dhash", lambda _frame: 9)
    monkeypatch.setattr("reviews.video_tasks.runtime.video_ocr_frame_budget", 1)

    class Ocr:
        def extract_text(self, _frame):
            return "二维码旁的联系方式", 0.9

    monkeypatch.setattr("reviews.video_tasks.OpenAiCompatibleOcrClient", Ocr)
    monkeypatch.setattr("reviews.video_tasks.frame_data_url", lambda frame: str(frame))
    extract_video_handler(job)
    extraction.refresh_from_db()

    assert extraction.evidence.filter(kind=VideoEvidenceKind.QR_CODE).count() == 1
    assert extraction.evidence.get(kind=VideoEvidenceKind.FRAME_OCR).start_ms == 0


@pytest.mark.django_db
def test_video_worker_continues_after_a_single_frame_ocr_failure(monkeypatch):
    post = OriginalPost.objects.create(topic_id=315, content_id="video-315")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="video-315",
        parameter_summary={"video_extraction_id": extraction.pk},
    )
    monkeypatch.setattr("reviews.video_tasks.VideoMediaReader.download", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.probe_video", lambda _path: VideoProbe(500, False))
    monkeypatch.setattr("reviews.video_tasks.candidate_times", lambda _duration: [0, 250])
    monkeypatch.setattr("reviews.video_tasks.extract_frame", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.qr_present", lambda _frame: False)
    monkeypatch.setattr(
        "reviews.video_tasks.dhash",
        lambda frame: 1 if "frame-0" in str(frame) else (1 << 64) - 1,
    )
    monkeypatch.setattr("reviews.video_tasks.frame_data_url", lambda frame: str(frame))

    class Ocr:
        calls = 0

        def extract_text(self, frame):
            type(self).calls += 1
            if self.calls == 1:
                raise SourceTransportError("frame failed")
            return "后续帧仍被识别", 0.9

    monkeypatch.setattr("reviews.video_tasks.OpenAiCompatibleOcrClient", Ocr)
    extract_video_handler(job)
    extraction.refresh_from_db()

    assert extraction.status == VideoExtractionStatus.PARTIAL
    assert "frame_ocr_failed" in extraction.error_summary
    assert extraction.evidence.get(kind=VideoEvidenceKind.FRAME_OCR).text == "后续帧仍被识别"


@pytest.mark.django_db
def test_video_worker_skips_a_single_unavailable_frame_and_keeps_prior_evidence(monkeypatch):
    post = OriginalPost.objects.create(topic_id=316, content_id="video-316")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="video-316",
        parameter_summary={"video_extraction_id": extraction.pk},
    )
    monkeypatch.setattr("reviews.video_tasks.VideoMediaReader.download", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.probe_video", lambda _path: VideoProbe(500, False))
    monkeypatch.setattr("reviews.video_tasks.candidate_times", lambda _duration: [0, 250])

    def extract_with_missing_tail(_video, timestamp, _destination):
        if timestamp == 250:
            raise VideoExtractionError("frame_decode_failed")

    monkeypatch.setattr("reviews.video_tasks.extract_frame", extract_with_missing_tail)
    monkeypatch.setattr("reviews.video_tasks.qr_present", lambda _frame: False)
    monkeypatch.setattr("reviews.video_tasks.dhash", lambda _frame: 1)
    monkeypatch.setattr("reviews.video_tasks.frame_data_url", lambda frame: str(frame))

    class Ocr:
        def extract_text(self, _frame):
            return "首帧证据仍可保留", 0.9

    monkeypatch.setattr("reviews.video_tasks.OpenAiCompatibleOcrClient", Ocr)
    extract_video_handler(job)
    extraction.refresh_from_db()

    assert extraction.status == VideoExtractionStatus.PARTIAL
    assert extraction.frame_count == 1
    assert "frame_decode_failed" in extraction.error_summary
    assert extraction.evidence.get(kind=VideoEvidenceKind.FRAME_OCR).text == "首帧证据仍可保留"


@pytest.mark.django_db
def test_qr_evidence_cannot_store_decoded_content():
    post = OriginalPost.objects.create(topic_id=302, content_id="video-302")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version="v1",
    )
    with pytest.raises(IntegrityError):
        VideoEvidence.objects.create(
            extraction=extraction,
            kind=VideoEvidenceKind.QR_CODE,
            start_ms=0,
            text="https://qr-target.example",
        )


def test_video_admin_models_are_read_only():
    request = RequestFactory().get("/admin/")
    extraction_admin = admin.site._registry[VideoExtraction]
    evidence_admin = admin.site._registry[VideoEvidence]

    assert extraction_admin.has_add_permission(request) is False
    assert extraction_admin.has_change_permission(request) is False
    assert evidence_admin.has_add_permission(request) is False
    assert evidence_admin.has_change_permission(request) is False


@pytest.mark.django_db
def test_continuous_qr_span_is_persisted_as_one_time_range():
    post = OriginalPost.objects.create(topic_id=306, content_id="video-306")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )

    _store_qr(extraction, QrSpan(start_ms=4_250, end_ms=5_250), sequence=0)

    evidence = VideoEvidence.objects.get(extraction=extraction, kind=VideoEvidenceKind.QR_CODE)
    assert (evidence.start_ms, evidence.end_ms, evidence.text) == (4_250, 5_250, "检测到二维码")


@pytest.mark.django_db
def test_video_evidence_is_text_only_ai_input_without_media_url():
    post = OriginalPost.objects.create(topic_id=303, content_id="video-303", content_text="正文")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/private.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        status=VideoExtractionStatus.PARTIAL,
        error_summary="audio_transcript_unavailable",
    )
    VideoEvidence.objects.create(
        extraction=extraction,
        kind=VideoEvidenceKind.QR_CODE,
        start_ms=12_400,
        text="检测到二维码",
    )
    _, input_text, _ = build_evaluation_input(post)
    assert "检测到二维码" in input_text
    assert "https://media.example/private.mp4" not in input_text
    assert "12.400" in input_text


@pytest.mark.django_db
def test_evaluation_input_applies_per_source_and_total_length_limits():
    post = OriginalPost.objects.create(
        topic_id=312, content_id="video-312", content_text="正文" * 7_000
    )
    media = PostMedia.objects.create(post=post, url="https://media.example/image.jpg")
    from reviews.models import OcrExtraction

    OcrExtraction.objects.create(post=post, media=media, provider="test", text="图片" * 5_000)

    content, ocr_text, _ = build_evaluation_input(post)

    assert len(content) == 12_000
    assert content.endswith("…")
    assert len(ocr_text) == 8_000
    assert ocr_text.endswith("…")


@pytest.mark.django_db
def test_contiguous_video_ocr_is_summarized_before_being_sent_to_ai():
    post = OriginalPost.objects.create(topic_id=308, content_id="video-308", content_text="正文")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    for sequence, (start_ms, text, confidence) in enumerate(
        (
            (0, "获得奖励 5000", 0.72),
            (500, "获得奖励 5000 社区礼包", 0.78),
            (1_000, "微信扫码咨询客服", 0.98),
        )
    ):
        VideoEvidence.objects.create(
            extraction=extraction,
            kind=VideoEvidenceKind.FRAME_OCR,
            start_ms=start_ms,
            sequence=sequence,
            text=text,
            confidence=confidence,
        )

    _, input_text, _ = build_evaluation_input(post)

    assert input_text.count("视频帧文字") == 1
    assert "视频帧文字汇总" in input_text
    assert "连续 3 帧" in input_text
    assert (
        "代表文字：00:00.000：获得奖励 5000；00:00.500：获得奖励 5000 社区礼包；"
        "00:01.000：微信扫码咨询客服"
    ) in input_text


@pytest.mark.django_db
def test_video_ocr_summary_limits_representatives_and_keeps_distinct_text():
    post = OriginalPost.objects.create(topic_id=309, content_id="video-309", content_text="正文")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    for sequence, (start_ms, text) in enumerate(
        (
            (0, "扫码加入官方群"),
            (250, "扫码加入官方群！"),
            (500, "下载私服领取十万奖励"),
            (750, "联系客服获取兑换码"),
            (1_000, "第四条不同文字"),
        )
    ):
        VideoEvidence.objects.create(
            extraction=extraction,
            kind=VideoEvidenceKind.FRAME_OCR,
            start_ms=start_ms,
            sequence=sequence,
            text=text,
            confidence=0.9,
        )

    _, input_text, _ = build_evaluation_input(post)

    assert "扫码加入官方群！" in input_text
    assert "下载私服领取十万奖励" in input_text
    assert "联系客服获取兑换码" in input_text
    assert "第四条不同文字" not in input_text


@pytest.mark.django_db
def test_ai_input_uses_only_the_current_video_extractor_version():
    post = OriginalPost.objects.create(topic_id=307, content_id="video-307", content_text="正文")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    old = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version="v1",
    )
    current = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
    )
    VideoEvidence.objects.create(
        extraction=old, kind=VideoEvidenceKind.FRAME_OCR, start_ms=0, text="过期证据"
    )
    VideoEvidence.objects.create(
        extraction=current, kind=VideoEvidenceKind.FRAME_OCR, start_ms=250, text="当前证据"
    )

    _, input_text, _ = build_evaluation_input(post)
    assert "当前证据" in input_text
    assert "过期证据" not in input_text


@pytest.mark.django_db
def test_video_jobs_are_idempotent_and_ai_waits_for_video(monkeypatch):
    post = OriginalPost.objects.create(topic_id=304, content_id="video-304")
    CandidatePost.objects.create(
        topic_id=304, content_id="video-304", origin_type=OriginType.ORIGINAL
    )
    PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    monkeypatch.setattr("reviews.services.runtime.video_enabled", True)
    monkeypatch.setattr("reviews.services.runtime.video_preflight_verified", True)
    _, ai_job = enqueue_evaluation_pipeline(post)
    video_job = BackgroundJob.objects.get(task_type="VIDEO_EXTRACT")
    assert ai_job.parameter_summary["video_job_ids"] == [video_job.pk]
    assert "https://media.example/video.mp4" not in str(video_job.parameter_summary)
    assert video_job.status == JobStatus.PENDING
    assert enqueue_evaluation_pipeline(post)[1].pk == ai_job.pk


@pytest.mark.django_db
def test_forced_re_evaluation_requeues_only_an_incomplete_video(monkeypatch):
    post = OriginalPost.objects.create(topic_id=317, content_id="video-317")
    CandidatePost.objects.create(
        topic_id=317, content_id="video-317", origin_type=OriginType.ORIGINAL
    )
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    monkeypatch.setattr("reviews.services.runtime.video_enabled", True)
    monkeypatch.setattr("reviews.services.runtime.video_preflight_verified", True)
    _, initial_ai_job = enqueue_evaluation_pipeline(post)
    initial_video_job = BackgroundJob.objects.get(task_type="VIDEO_EXTRACT")
    extraction = VideoExtraction.objects.get(media=media, extractor_version=VIDEO_EXTRACTOR_VERSION)
    extraction.status = VideoExtractionStatus.FAILED
    extraction.error_summary = "frame_decode_failed"
    extraction.save(update_fields=("status", "error_summary", "updated_at"))
    initial_video_job.status = JobStatus.SUCCEEDED
    initial_video_job.save(update_fields=("status", "updated_at"))

    _, forced_ai_job = enqueue_evaluation_pipeline(post, force=True)
    rerun_video_job = BackgroundJob.objects.get(
        pk=forced_ai_job.parameter_summary["video_job_ids"][0]
    )
    rerun_extraction = VideoExtraction.objects.get(
        pk=rerun_video_job.parameter_summary["video_extraction_id"]
    )

    assert forced_ai_job.pk != initial_ai_job.pk
    assert rerun_video_job.pk != initial_video_job.pk
    assert rerun_video_job.status == JobStatus.PENDING
    assert rerun_extraction.pk != extraction.pk
    assert rerun_extraction.attempt == 2
    assert extraction.status == VideoExtractionStatus.FAILED
    assert ":attempt:2" in rerun_video_job.idempotency_key
    assert "https://media.example/video.mp4" not in str(rerun_video_job.parameter_summary)

    _, repeated_forced_ai_job = enqueue_evaluation_pipeline(post, force=True)
    assert repeated_forced_ai_job.parameter_summary["video_job_ids"] == [rerun_video_job.pk]


@pytest.mark.django_db
def test_ai_input_uses_only_the_latest_attempt_for_each_current_video():
    post = OriginalPost.objects.create(topic_id=318, content_id="video-318", content_text="正文")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/video.mp4", media_type=MediaType.VIDEO
    )
    previous = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        attempt=1,
        status=VideoExtractionStatus.FAILED,
        error_summary="frame_decode_failed",
    )
    latest = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        attempt=2,
        status=VideoExtractionStatus.SUCCEEDED,
    )
    VideoEvidence.objects.create(
        extraction=previous,
        kind=VideoEvidenceKind.FRAME_OCR,
        start_ms=0,
        text="旧尝试的证据",
    )
    VideoEvidence.objects.create(
        extraction=latest,
        kind=VideoEvidenceKind.FRAME_OCR,
        start_ms=250,
        text="当前尝试的证据",
    )

    _, input_text, _ = build_evaluation_input(post)

    assert "当前尝试的证据" in input_text
    assert "旧尝试的证据" not in input_text


@pytest.mark.django_db
def test_ai_job_waits_for_video_without_consuming_its_retry_budget():
    post = OriginalPost.objects.create(topic_id=305, content_id="video-305")
    video_job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="video-wait-305",
        parameter_summary={"topic_id": post.topic_id},
        status=JobStatus.PENDING,
    )
    ai_job = BackgroundJob.objects.create(
        task_type="AI_EVALUATE",
        idempotency_key="ai-wait-305",
        parameter_summary={"topic_id": post.topic_id, "video_job_ids": [video_job.pk]},
    )

    execute_next_job(REVIEW_WORKER_HANDLERS, task_types=("AI_EVALUATE",))

    ai_job.refresh_from_db()
    assert ai_job.status == JobStatus.PENDING
    assert ai_job.retry_count == 0
    assert ai_job.error_summary == "等待视频提取任务完成"
    assert ai_job.next_run_at is not None
    assert ai_job.started_at is None


@pytest.mark.django_db
def test_duration_limit_uses_extraction_snapshot_and_persists_actual_duration(monkeypatch):
    post = OriginalPost.objects.create(topic_id=319, content_id="duration-snapshot")
    media = PostMedia.objects.create(
        post=post, url="https://media.example/too-long.mp4", media_type=MediaType.VIDEO
    )
    extraction = VideoExtraction.objects.create(
        post=post,
        media=media,
        media_fingerprint=media_fingerprint(media.url),
        extractor_version=VIDEO_EXTRACTOR_VERSION,
        duration_limit_seconds=30,
    )
    job = BackgroundJob.objects.create(
        task_type="VIDEO_EXTRACT",
        idempotency_key="duration-snapshot",
        parameter_summary={"video_extraction_id": extraction.pk},
    )
    monkeypatch.setattr("reviews.video_tasks.runtime.video_max_duration_seconds", 60)
    monkeypatch.setattr("reviews.video_tasks.VideoMediaReader.download", lambda *_args: None)
    monkeypatch.setattr("reviews.video_tasks.probe_video", lambda _path: VideoProbe(31_000, False))

    extract_video_handler(job)
    extraction.refresh_from_db()

    assert extraction.status == VideoExtractionStatus.SKIPPED_LIMIT
    assert extraction.error_summary == "video_duration_limit_exceeded"
    assert extraction.duration_ms == 31_000
    assert extraction.duration_limit_seconds == 30


@pytest.mark.django_db
def test_video_extraction_captures_duration_limit_when_enqueued(monkeypatch):
    post = OriginalPost.objects.create(topic_id=320, content_id="duration-limit-enqueue")
    CandidatePost.objects.create(
        topic_id=post.topic_id, content_id=post.content_id, origin_type=OriginType.ORIGINAL
    )
    PostMedia.objects.create(
        post=post, url="https://media.example/duration-limit.mp4", media_type=MediaType.VIDEO
    )
    monkeypatch.setattr("reviews.services.runtime.video_enabled", True)
    monkeypatch.setattr("reviews.services.runtime.video_preflight_verified", True)
    monkeypatch.setattr("reviews.services.runtime.video_max_duration_seconds", 30)

    enqueue_evaluation_pipeline(post)

    assert VideoExtraction.objects.get(post=post).duration_limit_seconds == 30
