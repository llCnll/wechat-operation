import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from config.cdp import ManagedGameDTO
from config.game_circle_ipc import GameCircleIpcError
from posts.admin import ManagedGameAdmin
from posts.game_catalog import (
    DEFAULT_GAME_APPID,
    managed_game_options,
    sync_managed_games,
)
from posts.models import ManagedGame, OriginalPost


@pytest.mark.django_db
def test_catalog_does_not_fabricate_default_game_and_keeps_unknown_historical_appid():
    existing_appids = set(ManagedGame.objects.values_list("appid", flat=True))
    OriginalPost.objects.create(topic_id=9001, content_id="unknown-game", source_appid="wx-history")

    options = managed_game_options()

    assert any(option.appid == "wx-history" and "未知游戏" in option.name for option in options)
    assert {option.appid for option in options if "未知游戏" not in option.name} == existing_appids


@pytest.mark.django_db
def test_read_only_catalog_options_do_not_manufacture_default_records():
    games_before = list(ManagedGame.objects.values_list("appid", "name"))

    options = managed_game_options(persist_default=False)

    assert [(option.appid, option.name) for option in options] == games_before
    assert list(ManagedGame.objects.values_list("appid", "name")) == games_before


@pytest.mark.django_db
def test_successful_sync_upserts_catalog_without_deleting_previous_records():
    existing_appids = set(ManagedGame.objects.values_list("appid", flat=True))
    ManagedGame.objects.create(appid="wx-legacy", name="旧游戏")

    count = sync_managed_games(
        [ManagedGameDTO("wx-new", "新游戏"), ManagedGameDTO("wx-legacy", "新名称")]
    )

    assert count == 2
    assert ManagedGame.objects.get(appid="wx-new").name == "新游戏"
    assert ManagedGame.objects.get(appid="wx-legacy").name == "新名称"
    assert existing_appids <= set(ManagedGame.objects.values_list("appid", flat=True))


@pytest.mark.django_db
def test_admin_sync_failure_keeps_cached_catalog(monkeypatch):
    game = ManagedGame.objects.create(appid="wx-cached", name="缓存游戏")
    monkeypatch.setattr(
        "posts.admin.list_managed_games", lambda: (_ for _ in ()).throw(GameCircleIpcError("SERVICE_UNAVAILABLE", "不可用"))
    )
    admin = ManagedGameAdmin(ManagedGame, AdminSite())
    request = RequestFactory().post("/admin/posts/managedgame/")
    messages = []
    monkeypatch.setattr(
        admin, "message_user", lambda request, message, **kwargs: messages.append(message)
    )

    admin.sync_from_authorized_platform(request, ManagedGame.objects.all())

    assert ManagedGame.objects.get(pk=game.pk).name == "缓存游戏"
    assert messages == ["同步失败，已保留本地游戏目录。"]


@pytest.mark.django_db
def test_admin_registers_sync_action_as_unbound_method(monkeypatch):
    game = ManagedGame.objects.create(appid="wx-sync", name="待同步游戏")
    admin = ManagedGameAdmin(ManagedGame, AdminSite())
    request = RequestFactory().post("/admin/posts/managedgame/")
    messages = []
    monkeypatch.setattr(
        "posts.admin.list_managed_games", lambda: [ManagedGameDTO("wx-sync", "已同步游戏")]
    )
    monkeypatch.setattr(
        admin, "message_user", lambda request, message, **kwargs: messages.append(message)
    )

    action, _, _ = admin.get_actions(request)["sync_from_authorized_platform"]
    action(admin, request, ManagedGame.objects.filter(pk=game.pk))

    assert ManagedGame.objects.get(pk=game.pk).name == "已同步游戏"
    assert messages == ["已同步 1 个可管理游戏。"]
