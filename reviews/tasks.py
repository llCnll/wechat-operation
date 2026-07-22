from config.settings import runtime
from jobs.models import BackgroundJob, JobStatus
from jobs.services import JobDeferred
from jobs.task_types import AI_EVALUATE_TASK
from posts.models import OriginalPost

from .adapters import (
    OpenAiCompatibleOcrClient,
    OpenAiCompatibleReviewClient,
    review_prompt_version,
)
from .models import OcrExtraction, PolicyVersion
from .reply_faq import available_reply_faq_rules, available_reply_faq_version
from .services import (
    OCR_EXTRACT_TASK,
    build_evaluation_input,
    current_video_extractions,
    persist_assessment,
)

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
            raise JobDeferred("等待 OCR 任务完成")
    for video_job_id in job.parameter_summary.get("video_job_ids", []):
        video_job = BackgroundJob.objects.get(pk=video_job_id)
        if video_job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
            raise JobDeferred("等待视频提取任务完成")
    if not (runtime.ai_base_url and runtime.ai_api_key and runtime.ai_model):
        raise JobDeferred("等待 AI 服务地址、密钥和模型配置", delay_seconds=60)
    policy = PolicyVersion.objects.filter(is_active=True).order_by("-created_at").first()
    content, ocr_text, fingerprint = build_evaluation_input(post)
    expected_fingerprint = job.parameter_summary.get("input_fingerprint")
    if expected_fingerprint and expected_fingerprint != fingerprint:
        raise RuntimeError("帖子内容已变化，等待重新入队")
    has_video_evidence = bool(current_video_extractions(post))
    faq_version = available_reply_faq_version(appid=post.source_appid)
    faq_rules = available_reply_faq_rules(appid=post.source_appid, content=f"{content}\n{ocr_text}")
    output = OpenAiCompatibleReviewClient().evaluate(
        content, ocr_text, has_video_evidence=has_video_evidence, faq_rules=faq_rules
    )
    persist_assessment(
        post,
        output,
        policy,
        prompt_version=review_prompt_version(
            has_video_evidence=has_video_evidence,
            faq_version=faq_version,
        ),
        faq_rules=faq_rules,
        faq_version=faq_version,
    )


def extract_post_ocr_handler(job: BackgroundJob) -> None:
    post = OriginalPost.objects.get(topic_id=int(job.parameter_summary["topic_id"]))
    for media in post.media.filter(media_type="IMAGE"):
        try:
            client = OpenAiCompatibleOcrClient()
            text, confidence = client.extract_text(media.url)
        except Exception as exc:
            OcrExtraction.objects.update_or_create(
                post=post,
                media=media,
                provider="openai-compatible",
                defaults={"text": "", "confidence": None, "error_summary": str(exc)[:500]},
            )
        else:
            OcrExtraction.objects.update_or_create(
                post=post,
                media=media,
                provider="openai-compatible",
                defaults={"text": text, "confidence": confidence, "error_summary": ""},
            )


WORKER_HANDLERS = {
    OCR_EXTRACT_TASK: extract_post_ocr_handler,
    AI_EVALUATE_TASK: evaluate_post_handler,
}
