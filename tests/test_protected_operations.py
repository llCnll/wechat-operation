import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from config.settings import runtime
from posts.models import CandidatePost, OriginalPost, OriginType
from reviews.adapters import AssessmentOutput
from reviews.services import persist_assessment


@pytest.mark.django_db
def test_admin_block_confirmation_creates_disabled_audit_without_upstream_write(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", False)
    monkeypatch.setattr(runtime, "enable_block_writes", False)
    user = get_user_model().objects.create_superuser("admin-test", "admin@test.invalid", "password")
    post = OriginalPost.objects.create(topic_id=501, content_id="protected-501")
    CandidatePost.objects.create(
        topic_id=501, content_id="protected-501", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="KEEP", overall_confidence=0.5
        ),
    )
    client = Client()
    client.force_login(user)
    url = f"/admin/reviews/reviewtask/{task.pk}/confirm-block/"

    assert client.get(url).status_code == 200
    response = client.post(
        url,
        {"reason": "人工确认违规", "block_reason": "8", "confirmed": "yes"},
    )
    assert response.status_code == 302
    assert task.operation_audits.get().result_code == "CAPABILITY_DISABLED"


@pytest.mark.django_db
def test_admin_mute_confirmation_creates_disabled_audit_without_upstream_write(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", False)
    monkeypatch.setattr(runtime, "enable_mute_writes", False)
    user = get_user_model().objects.create_superuser("mute-admin", "mute@test.invalid", "password")
    post = OriginalPost.objects.create(topic_id=502, content_id="protected-502")
    CandidatePost.objects.create(
        topic_id=502, content_id="protected-502", origin_type=OriginType.ORIGINAL
    )
    _, task = persist_assessment(
        post,
        AssessmentOutput(
            content_category="OTHER", recommended_action="MUTE", overall_confidence=0.5
        ),
    )
    client = Client()
    client.force_login(user)
    response = client.post(
        f"/admin/reviews/reviewtask/{task.pk}/confirm-mute/",
        {
            "reason": "广告欺诈",
            "block_reason": "8",
            "mute_days": "7",
            "confirmed": "yes",
        },
    )
    assert response.status_code == 302
    assert task.operation_audits.get().result_code == "CAPABILITY_DISABLED"
