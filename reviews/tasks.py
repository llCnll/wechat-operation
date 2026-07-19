from jobs.models import BackgroundJob, JobStatus
from posts.models import OriginalPost

from .adapters import OpenAiCompatibleOcrClient, OpenAiCompatibleReviewClient
from .models import OcrExtraction, PolicyVersion
from .services import OCR_EXTRACT_TASK, build_evaluation_input, persist_assessment

AI_EVALUATE_TASK = "AI_EVALUATE"


def reviewable_post_content(post: OriginalPost) -> str:
    """Include poll copy so hidden messages in option initials are reviewed with the post."""
    parts = [post.content_text]
    if post.ballot_text:
        parts.append(f"投票内容：\n{post.ballot_text}")
    return "\n\n".join(part for part in parts if part).strip()


def evaluate_post_handler(job: BackgroundJob) -> None:
    post = OriginalPost.objects.get(topic_id=int(job.parameter_summary["topic_id"]))
    ocr_job_id = job.parameter_summary.get("ocr_job_id")
    if ocr_job_id:
        ocr_job = BackgroundJob.objects.get(pk=ocr_job_id)
        if ocr_job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
            raise RuntimeError("等待 OCR 任务完成")
    policy = PolicyVersion.objects.filter(is_active=True).order_by("-created_at").first()
    content, ocr_text, fingerprint = build_evaluation_input(post)
    expected_fingerprint = job.parameter_summary.get("input_fingerprint")
    if expected_fingerprint and expected_fingerprint != fingerprint:
        raise RuntimeError("帖子内容已变化，等待重新入队")
    output = OpenAiCompatibleReviewClient().evaluate(content, ocr_text)
    persist_assessment(post, output, policy)


def extract_post_ocr_handler(job: BackgroundJob) -> None:
    post = OriginalPost.objects.get(topic_id=int(job.parameter_summary["topic_id"]))
    for media in post.media.filter(media_type="IMAGE"):
        try:
            client = OpenAiCompatibleOcrClient()
            text, confidence = client.extract_text(media.url)
        except Exception as exc:
            OcrExtraction.objects.update_or_create(
                post=post, media=media, provider="openai-compatible",
                defaults={"text": "", "confidence": None, "error_summary": str(exc)[:500]},
            )
        else:
            OcrExtraction.objects.update_or_create(
                post=post, media=media, provider="openai-compatible",
                defaults={"text": text, "confidence": confidence, "error_summary": ""},
            )


WORKER_HANDLERS = {
    OCR_EXTRACT_TASK: extract_post_ocr_handler,
    AI_EVALUATE_TASK: evaluate_post_handler,
}
