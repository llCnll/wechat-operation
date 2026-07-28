from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, RequestFactory
from django.utils import timezone

from jobs.models import BackgroundJob, JobStatus
from jobs.services import JobDeferred, complete_job
from jobs.permanent_mute import (
    AUTHOR_MUTE_STATUS_REFRESH_TASK,
    BLOCKED_USER_LIST_SYNC_TASK,
    PERMANENT_MUTE_RENEWAL_TASK,
    POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
    author_mute_status_refresh_handler,
    blocked_user_list_sync_handler,
    _next_status,
    _maintain_one,
    enqueue_author_mute_status_refresh,
    enqueue_blocked_user_list_sync,
    enqueue_post_author_mute_status_sync,
    enqueue_scheduled_permanent_mute_renewal,
    post_author_mute_status_sync_handler,
    WORKER_HANDLERS,
)
from posts.game_catalog import ExecutionScope, set_active_game
from posts.models import AuthorMuteStatus, CandidatePost, ManagedGame, OriginalPost, OriginType
from posts.services import (
    set_permanent_mute_intent,
    sync_author_mute_status,
)
from posts.views import _dashboard_operation_payload
from reviews.models import RecommendedAction


def _activate(appid="permanent-game"):
    game, _ = ManagedGame.objects.get_or_create(appid=appid, defaults={"name": "永久禁言测试游戏"})
    set_active_game(appid=game.appid)
    return game


def _status(*, appid="permanent-game", permanent=False, local_until=None):
    index = AuthorMuteStatus.objects.filter(source_appid=appid).count() + 1
    return AuthorMuteStatus.objects.create(
        openid=f"author-key-{index}",
        source_appid=appid,
        nick_name="测试作者",
        local_muted_until=local_until,
        permanent_mute_enabled=permanent,
        last_synced_at=timezone.now(),
        source_requested_at=timezone.now(),
    )


@pytest.mark.django_db
def test_permanent_intent_is_local_and_upstream_sync_does_not_clear_it():
    status = _status()
    user = get_user_model().objects.create_user(username="permanent-operator")
    set_permanent_mute_intent(status, enabled=True, user=user)

    # The state service changes only observed mute fields; it must not turn a
    # locally authorized long-term intent into a temporary mute.
    post = OriginalPost.objects.create(
        topic_id=77101,
        content_id="permanent-intent-post",
        source_appid="permanent-game",
        author_openid=status.openid,
    )
    sync_author_mute_status(
        post,
        {"data": {"topic": {"appid": "permanent-game", "creater": {"openid": status.openid}}}},
        source_appid="permanent-game",
    )
    status.refresh_from_db()
    assert status.permanent_mute_enabled is True
    assert status.permanent_mute_set_by_id == user.pk

    set_permanent_mute_intent(status, enabled=False)
    status.refresh_from_db()
    assert status.permanent_mute_enabled is False
    assert status.local_muted_until is None


@pytest.mark.django_db
def test_renewal_includes_expired_or_unobserved_local_mutes_but_not_other_game():
    _activate()
    target = _status(permanent=True, local_until=None)
    _status(appid="other-game", permanent=True, local_until=None)
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-renewal-selection",
        checkpoint={"execution_scope": ExecutionScope("permanent-game", 1).as_checkpoint()},
    )

    assert _next_status(job, ExecutionScope("permanent-game", 1)).pk == target.pk


@pytest.mark.django_db
def test_scheduled_renewal_is_idempotent_per_twelve_hour_slot():
    _activate()
    at_slot = timezone.now().astimezone(ZoneInfo("Asia/Shanghai")).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    first = enqueue_scheduled_permanent_mute_renewal(at_slot)
    second = enqueue_scheduled_permanent_mute_renewal(at_slot)

    assert first is not None and first.created is True
    assert second is not None and second.created is False
    assert first.job.pk == second.job.pk


@pytest.mark.django_db
def test_mute_management_displays_local_intent_and_async_controls():
    game = _activate()
    status = _status(permanent=True, local_until=timezone.now() + timedelta(days=2))
    user = get_user_model().objects.create_user(username="mute-list-operator")
    client = Client()
    client.force_login(user)

    response = client.get(f"/mute-management/?appid={game.appid}&kind=PERMANENT")
    page = response.content.decode()

    assert response.status_code == 200
    assert "mute-permanent-badge" in page
    assert "取消自动续期" in page
    assert "mute-row-more" not in page
    assert f'name="status_id" value="{status.pk}"' in page
    assert "mute-confirm-dialog" in page
    assert "window.confirm" not in page
    assert "执行一次" not in page  # Worker controls belong only to the system console.


@pytest.mark.django_db
def test_management_marks_existing_author_immediately_without_creating_a_job():
    game = _activate()
    status = _status(local_until=None)
    user = get_user_model().objects.create_user(username="immediate-permanent-operator")
    client = Client()
    client.force_login(user)

    response = client.post(
        "/mute-management/permanent/",
        {"action": "enable", "status_id": str(status.pk), "return_query": f"appid={game.appid}"},
    )

    assert response.status_code == 302
    status.refresh_from_db()
    assert status.permanent_mute_enabled is True
    assert not BackgroundJob.objects.filter(task_type=PERMANENT_MUTE_RENEWAL_TASK).exists()


@pytest.mark.django_db
def test_mute_management_explains_deleted_source_job_failure():
    game = _activate()
    user = get_user_model().objects.create_user(username="mute-feedback-operator")
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-mute-feedback",
        status=JobStatus.FAILED,
        checkpoint={"last_error": "游戏圈会话调用失败：SOURCE_DELETED"},
    )
    client = Client()
    client.force_login(user)

    response = client.get(f"/mute-management/?appid={game.appid}&job={job.pk}")

    assert "来源帖子已从管理队列删除" in response.content.decode()


@pytest.mark.django_db
def test_mute_maintenance_worker_registers_only_author_mute_maintenance_jobs():
    assert set(WORKER_HANDLERS) == {
        PERMANENT_MUTE_RENEWAL_TASK,
        AUTHOR_MUTE_STATUS_REFRESH_TASK,
        POST_AUTHOR_MUTE_STATUS_SYNC_TASK,
        BLOCKED_USER_LIST_SYNC_TASK,
    }


@pytest.mark.django_db
def test_batch_permanent_management_marks_all_authors_without_creating_a_job():
    game = _activate()
    first, second = _status(local_until=timezone.now() + timedelta(days=2)), _status()
    user = get_user_model().objects.create_user(username="batch-permanent-operator")
    client = Client()
    client.force_login(user)

    response = client.post(
        "/mute-management/permanent/",
        {
            "action": "enable",
            "status_id": [str(first.pk), str(second.pk)],
            "return_query": f"appid={game.appid}",
        },
    )

    assert response.status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.permanent_mute_enabled is True
    assert second.permanent_mute_enabled is True
    assert not BackgroundJob.objects.filter(task_type=PERMANENT_MUTE_RENEWAL_TASK).exists()


def test_mute_server_defaults_do_not_depend_on_dialog_javascript():
    request = RequestFactory().post("/", {})

    payload = _dashboard_operation_payload(request, RecommendedAction.MUTE)

    assert payload == {
        "block_reason_code": 1,
        "mute_days": 30,
        "permanent_mute": True,
    }


@pytest.mark.django_db
def test_renewal_threshold_selects_only_less_than_five_days():
    _activate()
    near = _status(permanent=True, local_until=timezone.now() + timedelta(days=4))
    _status(permanent=True, local_until=timezone.now() + timedelta(days=6))
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-renewal-threshold",
        checkpoint={"execution_scope": ExecutionScope("permanent-game", 1).as_checkpoint()},
    )

    assert _next_status(job, ExecutionScope("permanent-game", 1)).pk == near.pk


@pytest.mark.django_db
def test_renewal_remutes_a_permanently_marked_author_when_platform_deadline_is_zero():
    _activate()
    status = _status(permanent=True, local_until=None)
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-renewal-remute-zero",
        checkpoint={"execution_scope": ExecutionScope("permanent-game", 1).as_checkpoint()},
    )
    successful_audit = SimpleNamespace(result_code="OK")
    with (
        patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(object())),
        patch("jobs.permanent_mute._refresh_status", return_value=status),
        patch("jobs.permanent_mute.execute_author_operation", return_value=successful_audit) as execute,
    ):
        _maintain_one(job=job, status=status, scope=ExecutionScope("permanent-game", 1))

    assert [call.kwargs["action"] for call in execute.call_args_list] == ["MUTE"]


@pytest.mark.django_db
def test_renewal_skips_a_stale_candidate_after_user_detail_shows_a_long_deadline():
    _activate()
    status = _status(permanent=True, local_until=None)
    status.local_muted_until = timezone.now() + timedelta(days=30)
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-renewal-fresh-after-read",
        checkpoint={"execution_scope": ExecutionScope("permanent-game", 1).as_checkpoint()},
    )
    with (
        patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(object())),
        patch("jobs.permanent_mute._refresh_status", return_value=status),
        patch("jobs.permanent_mute.execute_author_operation") as execute,
    ):
        _maintain_one(job=job, status=status, scope=ExecutionScope("permanent-game", 1))

    execute.assert_not_called()


@pytest.mark.django_db
def test_author_status_refresh_freezes_the_saved_author_set_and_only_reads_users():
    _activate()
    first, second = (
        _status(local_until=timezone.now() + timedelta(days=2)),
        _status(permanent=True),
    )
    refresh = enqueue_author_mute_status_refresh()
    first.local_muted_until = None
    first.save(update_fields=["local_muted_until"])
    second.permanent_mute_enabled = False
    second.save(update_fields=["permanent_mute_enabled"])
    later = _status(permanent=True)
    source = SimpleNamespace(
        get_author_detail=Mock(
            side_effect=lambda openid: {
                "data": {
                    "user": {
                        "openid": openid,
                        "nick_name": f"刷新-{openid}",
                        "block_info": {"appid": "permanent-game", "block_end_time": 2_000_000_000},
                        "global_block_end_time": 0,
                    }
                }
            }
        )
    )
    with patch(
        "jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(source)
    ):
        author_mute_status_refresh_handler(refresh.job)

    refresh.job.refresh_from_db()
    first.refresh_from_db()
    second.refresh_from_db()
    later.refresh_from_db()
    assert refresh.job.checkpoint["processed"] == 2
    assert refresh.job.checkpoint["succeeded"] == 2
    assert refresh.job.checkpoint["status_ids"] == [first.pk, second.pk]
    assert first.local_muted_until is not None
    assert second.local_muted_until is not None
    assert later.local_muted_until is None
    assert source.get_author_detail.call_args_list == [
        call(first.openid),
        call(second.openid),
    ]


@pytest.mark.django_db
def test_read_maintenance_reuses_an_active_job_for_the_same_scope():
    _activate()

    first_refresh = enqueue_author_mute_status_refresh()
    second_refresh = enqueue_author_mute_status_refresh()
    first_scan = enqueue_post_author_mute_status_sync()
    second_scan = enqueue_post_author_mute_status_sync()

    assert first_refresh.created is True
    assert second_refresh.created is False
    assert second_refresh.job.pk == first_refresh.job.pk
    assert first_scan.created is True
    assert second_scan.created is False
    assert second_scan.job.pk == first_scan.job.pk

    complete_job(first_refresh.job)
    next_refresh = enqueue_author_mute_status_refresh()

    assert next_refresh.created is True
    assert next_refresh.job.pk != first_refresh.job.pk


@pytest.mark.django_db
def test_read_maintenance_retries_when_the_conflicting_job_has_already_finished():
    _activate()
    original_create = BackgroundJob.objects.create
    calls = 0

    def create_after_transient_conflict(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("unique conflict from a just-finished job")
        return original_create(*args, **kwargs)

    with patch("jobs.permanent_mute.BackgroundJob.objects.create", side_effect=create_after_transient_conflict):
        result = enqueue_author_mute_status_refresh()

    assert calls == 2
    assert result.created is True
    assert result.job.active_coalescing_key


@pytest.mark.django_db
def test_post_author_scan_deduplicates_authors_and_only_adds_muted_users():
    game = _activate()
    muted = OriginalPost.objects.create(
        topic_id=88001,
        content_id="muted-author-first",
        source_appid=game.appid,
        author_openid="muted-author",
    )
    OriginalPost.objects.create(
        topic_id=88002,
        content_id="muted-author-second",
        source_appid=game.appid,
        author_openid="muted-author",
    )
    OriginalPost.objects.create(
        topic_id=88003,
        content_id="normal-author",
        source_appid=game.appid,
        author_openid="normal-author",
    )
    OriginalPost.objects.create(
        topic_id=88004,
        content_id="missing-author",
        source_appid=game.appid,
    )
    scan = enqueue_post_author_mute_status_sync()
    future = int((timezone.now() + timedelta(days=7)).timestamp())
    source = SimpleNamespace(
        get_author_detail=Mock(
            side_effect=lambda openid: {
                "data": {
                    "user": {
                        "openid": openid,
                        "nick_name": openid,
                        **(
                            {"block_info": {"appid": game.appid, "block_end_time": future}}
                            if openid == "muted-author"
                            else {}
                        ),
                        "global_block_end_time": 0,
                    }
                }
            }
        )
    )
    with patch(
        "jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(source)
    ):
        post_author_mute_status_sync_handler(scan.job)

    scan.job.refresh_from_db()
    muted_status = AuthorMuteStatus.objects.get(openid="muted-author", source_appid=game.appid)
    assert source.get_author_detail.call_args_list == [call("muted-author"), call("normal-author")]
    assert muted_status.last_source_post_id == muted.pk
    assert muted_status.permanent_mute_enabled is False
    assert not AuthorMuteStatus.objects.filter(openid="normal-author").exists()
    assert scan.job.checkpoint["processed"] == 2
    assert scan.job.checkpoint["skipped_missing_openid"] == 1


@pytest.mark.django_db
def test_post_author_scan_refreshes_a_known_author_even_when_the_linked_post_is_deleted():
    game = _activate()
    post = OriginalPost.objects.create(
        topic_id=88014,
        content_id="deleted-known-author",
        source_appid=game.appid,
        author_openid="known-deleted-author",
        status=128,
    )
    scan = enqueue_post_author_mute_status_sync()
    source = SimpleNamespace(
        get_author_detail=Mock(
            return_value={
                "data": {
                    "user": {
                        "openid": post.author_openid,
                        "nick_name": "已删除帖作者",
                        "global_block_end_time": 0,
                    }
                }
            }
        )
    )

    with patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(source)):
        post_author_mute_status_sync_handler(scan.job)

    post.refresh_from_db()
    assert source.get_author_detail.call_args_list == [call(post.author_openid)]
    assert post.author_nick_name == "已删除帖作者"


@pytest.mark.django_db
def test_mute_management_queues_a_read_only_author_status_refresh():
    game = _activate()
    _status()
    user = get_user_model().objects.create_user(username="mute-status-refresh-operator")
    client = Client()
    client.force_login(user)

    response = client.post(
        "/mute-management/refresh-statuses/", {"return_query": f"appid={game.appid}"}
    )

    assert response.status_code == 302
    assert BackgroundJob.objects.filter(task_type=AUTHOR_MUTE_STATUS_REFRESH_TASK).exists()


@pytest.mark.django_db
def test_mute_management_queues_a_post_author_scan_without_permanent_intent():
    game = _activate()
    user = get_user_model().objects.create_user(username="post-author-scan-operator")
    client = Client()
    client.force_login(user)

    response = client.post(
        "/mute-management/scan-post-authors/", {"return_query": f"appid={game.appid}"}
    )

    assert response.status_code == 302
    assert BackgroundJob.objects.filter(task_type=POST_AUTHOR_MUTE_STATUS_SYNC_TASK).exists()


@pytest.mark.django_db
def test_mute_management_queues_platform_blocked_user_import():
    game = _activate()
    user = get_user_model().objects.create_user(username="blocked-user-import-operator")
    client = Client()
    client.force_login(user)

    response = client.post(
        "/mute-management/import-platform-blocked-users/",
        {"return_query": f"appid={game.appid}"},
    )

    assert response.status_code == 302
    job = BackgroundJob.objects.get(task_type=BLOCKED_USER_LIST_SYNC_TASK)
    assert job.checkpoint["page"] == 0
    assert job.checkpoint["page_size"] is None


@pytest.mark.django_db
def test_platform_blocked_user_import_uses_zero_based_page_and_minimal_projection():
    game = _activate()
    linked_post = OriginalPost.objects.create(
        topic_id=99001,
        content_id="existing-author-post",
        source_appid=game.appid,
        author_openid="existing-author",
    )
    existing = AuthorMuteStatus.objects.create(
        openid="existing-author",
        source_appid=game.appid,
        nick_name="旧昵称",
        global_muted_until=timezone.now() + timedelta(days=3),
        permanent_mute_enabled=True,
        last_source_post=linked_post,
        last_synced_at=timezone.now(),
        source_requested_at=timezone.now(),
    )
    page = enqueue_blocked_user_list_sync()
    end_date = (timezone.now().astimezone(ZoneInfo("Asia/Shanghai")) + timedelta(days=10)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    source = SimpleNamespace(
        list_blocked_users=Mock(
            return_value={
                "total": 2,
                "items": [
                    {"openid": "existing-author", "nick_name": "新昵称", "end_date": end_date},
                    {"openid": "new-author", "nick_name": "新作者", "end_date": end_date},
                ],
            }
        )
    )

    with patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(source)):
        blocked_user_list_sync_handler(page.job)

    page.job.refresh_from_db()
    existing.refresh_from_db()
    imported = AuthorMuteStatus.objects.get(openid="new-author", source_appid=game.appid)
    assert source.list_blocked_users.call_args_list == [call(page=0, page_size=500)]
    assert page.job.checkpoint["processed"] == page.job.checkpoint["imported"] == 2
    assert existing.nick_name == "新昵称"
    assert existing.global_muted_until is not None
    assert existing.permanent_mute_enabled is True
    assert existing.last_source_post_id == linked_post.pk
    assert imported.permanent_mute_enabled is False
    assert imported.global_muted_until is None


@pytest.mark.django_db
def test_platform_blocked_user_import_lowers_page_size_when_first_page_is_incomplete():
    _activate()
    page = enqueue_blocked_user_list_sync()
    end_date = (timezone.now().astimezone(ZoneInfo("Asia/Shanghai")) + timedelta(days=10)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    source = SimpleNamespace(
        list_blocked_users=Mock(
            side_effect=[
                {"total": 1, "items": []},
                {"total": 1, "items": [{"openid": "fallback-author", "nick_name": "回退作者", "end_date": end_date}]},
            ]
        )
    )

    with patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(source)):
        blocked_user_list_sync_handler(page.job)

    page.job.refresh_from_db()
    assert source.list_blocked_users.call_args_list == [
        call(page=0, page_size=500),
        call(page=0, page_size=200),
    ]
    assert page.job.checkpoint["page_size"] == 200
    assert page.job.checkpoint["completed"] is True


@pytest.mark.django_db
def test_platform_blocked_user_import_keeps_absent_local_author_unchanged():
    game = _activate()
    status = _status(permanent=True, local_until=timezone.now() + timedelta(days=2))
    page = enqueue_blocked_user_list_sync()
    source = SimpleNamespace(list_blocked_users=Mock(return_value={"total": 0, "items": []}))

    with patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(source)):
        blocked_user_list_sync_handler(page.job)

    status.refresh_from_db()
    assert status.source_appid == game.appid
    assert status.permanent_mute_enabled is True
    assert status.local_muted_until is not None


@pytest.mark.django_db
def test_renewal_overwrites_without_unmuting_and_keeps_intent_when_write_fails():
    _activate()
    status = _status(permanent=True, local_until=timezone.now() + timedelta(days=1))
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-renewal-state-machine",
        checkpoint={"execution_scope": ExecutionScope("permanent-game", 1).as_checkpoint()},
    )
    successful_audit = SimpleNamespace(result_code="OK")
    with (
        patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(object())),
        patch("jobs.permanent_mute._refresh_status", return_value=status),
        patch(
            "jobs.permanent_mute.execute_author_operation",
                return_value=successful_audit,
        ) as execute,
    ):
        _maintain_one(
            job=job, status=status, scope=ExecutionScope("permanent-game", 1)
        )
    assert [call.kwargs["action"] for call in execute.call_args_list] == ["MUTE"]

    status.permanent_mute_enabled = True
    status.save(update_fields=["permanent_mute_enabled"])
    with (
        patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(object())),
        patch("jobs.permanent_mute._refresh_status", return_value=status),
        patch(
            "jobs.permanent_mute.execute_author_operation",
                return_value=SimpleNamespace(result_code="ACTION_FAILED"),
        ),
        pytest.raises(ValueError, match="禁言回读未确认"),
    ):
        _maintain_one(
            job=job, status=status, scope=ExecutionScope("permanent-game", 1)
        )
    status.refresh_from_db()
    assert status.permanent_mute_enabled is True


@pytest.mark.django_db
def test_renewal_defers_when_the_session_service_is_recovering():
    _activate()
    status = _status(permanent=True, local_until=None)
    job = BackgroundJob.objects.create(
        task_type=PERMANENT_MUTE_RENEWAL_TASK,
        idempotency_key="permanent-renewal-session-recovering",
        checkpoint={"execution_scope": ExecutionScope("permanent-game", 1).as_checkpoint()},
    )
    with (
        patch("jobs.permanent_mute.GameCirclePostClient", return_value=nullcontext(object())),
        patch("jobs.permanent_mute._refresh_status", return_value=status),
        patch(
            "jobs.permanent_mute.execute_author_operation",
            return_value=SimpleNamespace(result_code="SESSION_RECOVERING"),
        ),
        pytest.raises(JobDeferred),
    ):
        _maintain_one(
            job=job, status=status, scope=ExecutionScope("permanent-game", 1)
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("result_code", "expected_intent"),
    (("OK", True), ("ALREADY_APPLIED", True), ("ACTION_FAILED", False)),
)
def test_human_mute_writes_permanent_intent_only_after_verified_readback(
    result_code, expected_intent
):
    game = _activate()
    user = get_user_model().objects.create_user(username=f"mute-{result_code.lower()}")
    post = OriginalPost.objects.create(
        topic_id=77200 if expected_intent else 77201,
        content_id=f"human-mute-{result_code}",
        source_appid=game.appid,
        author_openid="human-mute-author",
    )
    CandidatePost.objects.create(
        topic_id=post.topic_id,
        content_id=post.content_id,
        source_appid=game.appid,
        origin_type=OriginType.ORIGINAL,
    )
    status = AuthorMuteStatus.objects.create(
        openid=post.author_openid,
        source_appid=game.appid,
        last_synced_at=timezone.now(),
        source_requested_at=timezone.now(),
    )
    client = Client()
    client.force_login(user)
    audit = SimpleNamespace(result_code=result_code, action="MUTE")

    with (
        patch("posts.views.is_operation_enabled", return_value=True),
        patch("posts.views._source_for_post", return_value=nullcontext(object())),
            patch("posts.views.execute_author_operation_for_identity", return_value=audit),
    ):
        response = client.post(
            f"/posts/{post.topic_id}/operate/MUTE/",
            {
                "reason": "受控测试禁言",
                "block_reason": "1",
                "mute_days": "30",
                "permanent_mute": "1",
                "confirmation_key": f"mute-{result_code}",
            },
        )

    assert response.status_code == 302
    status.refresh_from_db()
    assert status.permanent_mute_enabled is expected_intent
