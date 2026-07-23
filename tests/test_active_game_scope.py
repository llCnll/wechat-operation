import pytest
from django.contrib.auth.models import User

from config.game_circle_ipc import GameCircleIpcError
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


def test_staff_scope_change_uses_explicit_session_controls_and_only_manual_keepalive_is_queued(client):
    first = _game("game-first")
    second = _game("game-second")
    user = User.objects.create(username="operator", is_staff=True, is_superuser=True)
    client.force_login(user)

    response = client.post("/admin/runtime/active-game/", {"appid": first.appid, "return_to": "/admin/"})
    keepalive = client.post("/admin/runtime/session-keepalive/", {"return_to": "/admin/"})
    probe = client.post("/admin/runtime/session-check/", {"return_to": "/admin/"})
    recover = client.post("/admin/runtime/current-game-frame/", {"return_to": "/admin/"})
    switched = client.post("/admin/runtime/active-game/", {"appid": second.appid, "return_to": "/admin/"})

    assert response.status_code == 302
    assert keepalive.status_code == 302
    assert probe.status_code == 302
    assert recover.status_code == 302
    assert switched.status_code == 302
    assert ActiveGameScope.objects.get().active_appid == second.appid
    keepalive_jobs = BackgroundJob.objects.filter(task_type="GAME_CIRCLE_SESSION_KEEPALIVE")
    assert keepalive_jobs.count() == 1
    assert keepalive_jobs.get().parameter_summary == {"trigger": "manual"}
    assert keepalive_jobs.get().checkpoint == {}
    assert BackgroundJob.objects.count() == 1


def test_staff_catalog_sync_uses_the_session_backed_catalog_service(client, monkeypatch):
    user = User.objects.create(username="catalog-operator", is_staff=True, is_superuser=True)
    client.force_login(user)
    calls = []
    monkeypatch.setattr(
        "config.supervisor_views.sync_authorized_managed_games",
        lambda: calls.append("sync") or 2,
    )

    response = client.post("/admin/runtime/game-catalog/sync/", {"return_to": "/admin/"}, follow=True)

    assert calls == ["sync"]
    assert "已同步 2 个可管理游戏。" in response.content.decode()


def test_staff_catalog_sync_preserves_existing_catalog_on_session_failure(client, monkeypatch):
    _game("catalog-existing")
    user = User.objects.create(username="catalog-failure-operator", is_staff=True, is_superuser=True)
    client.force_login(user)

    def unavailable():
        raise GameCircleIpcError("SESSION_UNAVAILABLE", "当前游戏 Frame 尚未就绪")

    monkeypatch.setattr("config.supervisor_views.sync_authorized_managed_games", unavailable)
    response = client.post("/admin/runtime/game-catalog/sync/", {"return_to": "/admin/"}, follow=True)

    assert "同步可管理游戏失败，已保留本地目录。" in response.content.decode()
    assert ManagedGame.objects.filter(appid="catalog-existing").exists()


def test_direct_post_source_access_rejects_a_different_active_game():
    first = _game("game-first")
    second = _game("game-second")
    set_active_game(appid=first.appid)
    post = OriginalPost.objects.create(topic_id=1, content_id="post-one", source_appid=second.appid)

    with pytest.raises(ValueError, match="不一致"):
        _require_active_post_scope(post)
