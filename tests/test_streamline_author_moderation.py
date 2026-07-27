from contextlib import nullcontext

import pytest
from django.contrib.auth import get_user_model

from config.settings import runtime
from jobs.adapters import SourceBusinessDeletedError, SourceTransportError
from jobs.models import BackgroundJob
from jobs.task_types import FILTERED_POST_REFRESH_TASK
from posts.dashboard_filters import OriginalPostFilter
from posts.models import ActiveGameScope, AuthorMuteStatus, ManagedGame, OriginalPost
from posts.tasks import filtered_post_refresh_handler
from reviews.models import (
    AiOperationResolution,
    AiOperationReview,
    AiOperationReviewStatus,
    OperationAudit,
    RecommendedAction,
    ReviewTask,
)
from reviews.services import execute_author_operation_for_identity, submit_ai_operation_resolution


def _user_detail(*, openid: str, appid: str, muted_until: int = 0) -> dict:
    return {
        "data": {
            "user": {
                "openid": openid,
                "nick_name": "测试作者",
                "block_info": {"appid": appid, "block_end_time": muted_until},
                "global_block_end_time": 0,
            }
        }
    }


@pytest.mark.django_db
def test_author_operation_is_idempotent_and_never_uses_a_post(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    readback_delays = []
    monkeypatch.setattr("reviews.services.sleep", readback_delays.append)
    user = get_user_model().objects.create_user(username="author-target-operator")

    class Source:
        def __init__(self):
            self.until = 0
            self.writes = []
            self.reads = 0

        def get_author_detail(self, openid):
            self.reads += 1
            return _user_detail(openid=openid, appid="game-a", muted_until=self.until)

        def perform_confirmed_operation(self, *, topic_id, action, payload):
            self.writes.append((topic_id, action, payload))
            self.until = payload["mute_end_time"]
            return {"errcode": 0}

    source = Source()
    kwargs = {
        "openid": "author-a",
        "source_appid": "game-a",
        "user": user,
        "action": RecommendedAction.MUTE,
        "reason": "受控测试",
        "source": source,
        "upstream_payload": {"block_reason_code": 1, "mute_days": 30},
        "confirmation_key": "same-confirmation",
    }
    first = execute_author_operation_for_identity(**kwargs)

    assert source.reads == 2  # one identity check and one delayed readback
    assert readback_delays == pytest.approx([2])

    second = execute_author_operation_for_identity(**kwargs)

    assert first.pk == second.pk
    assert source.writes and len(source.writes) == 1
    assert source.writes[0][0] is None
    assert AuthorMuteStatus.objects.filter(openid="author-a", source_appid="game-a").exists()


@pytest.mark.django_db
def test_filtered_refresh_does_not_advance_cursor_past_retryable_failure(monkeypatch):
    game = ManagedGame.objects.create(appid="filter-game", name="筛选测试")
    ActiveGameScope.objects.create(pk=1, active_game=game)
    first = OriginalPost.objects.create(
        topic_id=101, content_id="filter-first", source_appid=game.appid, author_openid="a"
    )
    OriginalPost.objects.create(topic_id=102, content_id="filter-second", source_appid=game.appid)
    job = BackgroundJob.objects.create(
        task_type=FILTERED_POST_REFRESH_TASK,
        idempotency_key="filtered-refresh-retryable",
        parameter_summary={"appid": game.appid, "filters": OriginalPostFilter().as_parameters()},
        checkpoint={"execution_scope": {"target_appid": game.appid, "scope_version": 1}},
    )

    class Source:
        def get_post_metrics(self, topic_id):
            if topic_id == first.topic_id:
                raise SourceBusinessDeletedError("deleted")
            raise SourceTransportError("temporary transport failure")

        def get_author_detail(self, openid):
            return _user_detail(openid=openid, appid=game.appid)

        def close(self):
            pass

    monkeypatch.setattr("posts.tasks._source_client", lambda _scope: nullcontext(Source()))
    with pytest.raises(SourceTransportError):
        filtered_post_refresh_handler(job)

    job.refresh_from_db()
    assert job.checkpoint["last_topic_id"] == first.topic_id
    assert job.checkpoint["failed"] == 1
    assert "author_openids" not in job.checkpoint


@pytest.mark.django_db
def test_ai_resolution_terminalizes_when_author_identity_is_missing(monkeypatch):
    monkeypatch.setattr(runtime, "enable_upstream_writes", True)
    monkeypatch.setattr(runtime, "enable_mute_writes", True)
    user = get_user_model().objects.create_user(username="resolution-terminal-operator")
    post = OriginalPost.objects.create(
        topic_id=103,
        content_id="resolution-missing-author",
        source_appid="game-a",
        status=2,
    )
    task = ReviewTask.objects.create(post=post, suggested_action=RecommendedAction.SINK)
    automated = OperationAudit.objects.create(
        task=task,
        operation_id="resolution-missing-author-audit",
        idempotency_key="resolution-missing-author-audit",
        action=RecommendedAction.SINK,
        reason="AI 自动处置",
        result_code="OK",
    )
    review = AiOperationReview.objects.create(
        audit=automated,
        status=AiOperationReviewStatus.PENDING,
    )

    with pytest.raises(ValueError, match="作者身份"):
        submit_ai_operation_resolution(
            review,
            user=user,
            post_decision="KEEP_CURRENT",
            author_decision=RecommendedAction.MUTE,
            reason="补充禁言",
            confirmation_key="missing-author-confirmation-1",
            source=object(),
        )

    first = AiOperationResolution.objects.get(confirmation_key="missing-author-confirmation-1")
    assert first.result_code == "FAILED"
    assert first.completed_at is not None

    with pytest.raises(ValueError, match="作者身份"):
        submit_ai_operation_resolution(
            review,
            user=user,
            post_decision="KEEP_CURRENT",
            author_decision=RecommendedAction.MUTE,
            reason="再次确认",
            confirmation_key="missing-author-confirmation-2",
            source=object(),
        )

    assert AiOperationResolution.objects.filter(review=review, result_code="FAILED").count() == 2
