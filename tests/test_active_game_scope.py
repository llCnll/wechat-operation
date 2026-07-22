import pytest
from django.contrib.auth.models import User

from jobs.models import BackgroundJob
from posts.game_catalog import (
    active_game_appid,
    checkpoint_with_execution_scope,
    resolve_execution_scope,
    set_active_game,
)
from posts.models import ActiveGameScope, ManagedGame
from posts.models import OriginalPost
from posts.views import _require_active_post_scope


pytestmark = pytest.mark.django_db


def _game(appid: str) -> ManagedGame:
    return ManagedGame.objects.create(appid=appid, name=f"游戏-{appid}")


def test_active_scope_is_a_single_catalog_game_and_versions_changes():
    first = _game("game-first")
    second = _game("game-second")

    initial = set_active_game(appid=first.appid)
    changed = set_active_game(appid=second.appid)

    assert ActiveGameScope.objects.count() == 1
    assert changed.active_appid == second.appid
    assert changed.scope_version == initial.scope_version + 1
    assert active_game_appid() == second.appid


def test_execution_scope_is_stable_after_active_game_changes():
    first = _game("game-first")
    second = _game("game-second")
    set_active_game(appid=first.appid)
    job = BackgroundJob.objects.create(
        task_type="TEST", idempotency_key="scope-stable", parameter_summary={}
    )

    frozen = resolve_execution_scope(job)
    set_active_game(appid=second.appid)
    reused = resolve_execution_scope(job)

    assert frozen.target_appid == first.appid
    assert reused == frozen
    job.refresh_from_db()
    assert job.checkpoint["execution_scope"] == frozen.as_checkpoint()


def test_replacing_a_progress_checkpoint_preserves_the_frozen_scope():
    first = _game("game-first")
    second = _game("game-second")
    set_active_game(appid=first.appid)
    job = BackgroundJob.objects.create(
        task_type="TEST", idempotency_key="scope-progress", parameter_summary={}
    )
    frozen = resolve_execution_scope(job)

    job.checkpoint = checkpoint_with_execution_scope(job, {"page": 2, "complete": False})
    job.save(update_fields=["checkpoint", "updated_at"])
    set_active_game(appid=second.appid)

    assert resolve_execution_scope(job) == frozen
    assert job.checkpoint == {
        "page": 2,
        "complete": False,
        "execution_scope": frozen.as_checkpoint(),
    }


def test_execution_scope_rejects_a_compatibility_appid_outside_current_scope():
    first = _game("game-first")
    second = _game("game-second")
    set_active_game(appid=first.appid)
    job = BackgroundJob.objects.create(
        task_type="TEST", idempotency_key="scope-mismatch", parameter_summary={"appid": second.appid}
    )

    with pytest.raises(ValueError, match="不一致"):
        resolve_execution_scope(job)


def test_staff_scope_change_probe_and_frame_recover_only_enqueue_jobs(client):
    first = _game("game-first")
    second = _game("game-second")
    user = User.objects.create(username="operator", is_staff=True, is_superuser=True)
    client.force_login(user)

    response = client.post("/admin/runtime/active-game/", {"appid": first.appid, "return_to": "/admin/"})
    keepalive = client.post("/admin/runtime/session-keepalive/", {"return_to": "/admin/"})
    probe = client.post("/admin/runtime/login-probe/", {"return_to": "/admin/"})
    BackgroundJob.objects.filter(task_type="GAME_CIRCLE_SESSION_REFRESH").update(status="SUCCEEDED")
    recover = client.post("/admin/runtime/session-recover/", {"return_to": "/admin/"})
    switched = client.post("/admin/runtime/active-game/", {"appid": second.appid, "return_to": "/admin/"})

    assert response.status_code == 302
    assert keepalive.status_code == 302
    assert probe.status_code == 302
    assert recover.status_code == 302
    assert switched.status_code == 302
    assert ActiveGameScope.objects.get().active_appid == second.appid
    assert BackgroundJob.objects.filter(
        task_type="GAME_CIRCLE_LOGIN_PROBE", parameter_summary={"source": "manual"}
    ).count() == 1
    refresh_jobs = BackgroundJob.objects.filter(task_type="GAME_CIRCLE_SESSION_REFRESH")
    assert refresh_jobs.count() == 4
    assert refresh_jobs.filter(parameter_summary={"refresh_mode": "KEEPALIVE"}).count() == 1
    assert refresh_jobs.filter(parameter_summary={"refresh_mode": "RECOVER"}).count() == 3
    assert {
        tuple(job.checkpoint["execution_scope"].items()) for job in refresh_jobs
    } == {
        tuple({"target_appid": first.appid, "scope_version": 1}.items()),
        tuple({"target_appid": second.appid, "scope_version": 2}.items()),
    }


def test_direct_post_source_access_rejects_a_different_active_game():
    first = _game("game-first")
    second = _game("game-second")
    set_active_game(appid=first.appid)
    post = OriginalPost.objects.create(topic_id=1, content_id="post-one", source_appid=second.appid)

    with pytest.raises(ValueError, match="不一致"):
        _require_active_post_scope(post)
