import pytest

from jobs.adapters import CandidatePostDTO
from jobs.models import BackgroundJob
from posts.ingestion import ingest_page, ingest_window
from posts.models import CandidatePost, OriginType


class FakeSource:
    def list_candidates(self, **kwargs):
        return [CandidatePostDTO(topic_id=9, content_id="original-9", source_appid="game")]

    def get_content_card(self, content_id):
        return {"data": {"content_card": {"base_content": {"content": "正文"}}}}

    def get_post_metrics(self, topic_id):
        return {"data": {"topic": {"view_count": 1}}}


@pytest.mark.django_db
def test_ingestion_upserts_candidate_and_classifies_original():
    assert ingest_page(FakeSource(), page=0, page_size=10, start_time=1, end_time=2) == 1
    candidate = CandidatePost.objects.get(topic_id=9)
    assert candidate.origin_type == OriginType.ORIGINAL


@pytest.mark.django_db
def test_duplicate_candidate_is_updated_without_creating_duplicate_post():
    source = FakeSource()
    ingest_page(source, page=0, page_size=10, start_time=1, end_time=2)
    ingest_page(source, page=0, page_size=10, start_time=1, end_time=2)
    assert CandidatePost.objects.count() == 1


class MissingContentIdSource(FakeSource):
    def list_candidates(self, **kwargs):
        return [CandidatePostDTO(topic_id=10, content_id="", source_appid="game")]


@pytest.mark.django_db
def test_missing_content_id_remains_pending():
    ingest_page(MissingContentIdSource(), page=0, page_size=10, start_time=1, end_time=2)
    candidate = CandidatePost.objects.get(topic_id=10)
    assert candidate.origin_type == OriginType.CLASSIFICATION_PENDING
    assert "缺少 content_id" in candidate.classification_error


@pytest.mark.django_db
def test_window_ingestion_persists_page_checkpoint():
    job = BackgroundJob.objects.create(task_type="INGEST", idempotency_key="ingest:window")
    assert ingest_window(FakeSource(), job, page_size=10, start_time=1, end_time=2) == 1
    job.refresh_from_db()
    assert job.checkpoint == {"page": 1, "last_processed_count": 1}


@pytest.mark.django_db
def test_window_ingestion_resumes_from_saved_page_checkpoint():
    job = BackgroundJob.objects.create(task_type="INGEST", idempotency_key="ingest:resume")
    source = FakeSource()
    ingest_window(source, job, page_size=10, start_time=1, end_time=2)
    ingest_window(source, job, page_size=10, start_time=1, end_time=2)
    job.refresh_from_db()
    assert job.checkpoint["page"] == 2


class InvalidDetailSource(FakeSource):
    def get_content_card(self, content_id):
        return {"data": {}}


@pytest.mark.django_db
def test_invalid_detail_is_left_pending_without_aborting_page():
    assert ingest_page(InvalidDetailSource(), page=0, page_size=10, start_time=1, end_time=2) == 1
    candidate = CandidatePost.objects.get(topic_id=9)
    assert candidate.origin_type == OriginType.CLASSIFICATION_PENDING
    assert candidate.classification_error == "内容卡片响应结构无效"
