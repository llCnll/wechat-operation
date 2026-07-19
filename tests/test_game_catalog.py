import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from config.cdp import CdpUnavailableError, ManagedGameDTO, list_managed_games
from posts.admin import ManagedGameAdmin
from posts.game_catalog import (
    DEFAULT_GAME_APPID,
    DEFAULT_GAME_NAME,
    managed_game_options,
    sync_managed_games,
)
from posts.models import ManagedGame, OriginalPost


@pytest.mark.django_db
def test_catalog_has_default_game_and_keeps_unknown_historical_appid():
    OriginalPost.objects.create(topic_id=9001, content_id="unknown-game", source_appid="wx-history")

    options = managed_game_options()

    assert options[0].appid == DEFAULT_GAME_APPID
    assert options[0].name == DEFAULT_GAME_NAME
    assert any(option.appid == "wx-history" and "未知游戏" in option.name for option in options)


@pytest.mark.django_db
def test_read_only_catalog_options_include_default_without_persisting_it():
    games_before = list(ManagedGame.objects.values_list("appid", "name"))

    options = managed_game_options(persist_default=False)

    assert options[0].appid == DEFAULT_GAME_APPID
    assert list(ManagedGame.objects.values_list("appid", "name")) == games_before


@pytest.mark.django_db
def test_successful_sync_upserts_catalog_without_deleting_previous_records():
    ManagedGame.objects.create(appid="wx-legacy", name="旧游戏")

    count = sync_managed_games(
        [ManagedGameDTO("wx-new", "新游戏"), ManagedGameDTO("wx-legacy", "新名称")]
    )

    assert count == 2
    assert ManagedGame.objects.get(appid="wx-new").name == "新游戏"
    assert ManagedGame.objects.get(appid="wx-legacy").name == "新名称"
    assert ManagedGame.objects.get(appid=DEFAULT_GAME_APPID).name == DEFAULT_GAME_NAME


def test_outer_page_game_reader_returns_only_normalized_fields(monkeypatch):
    class Page:
        def __init__(self, *args, **kwargs):
            pass

        def evaluate(self, expression):
            assert "getWxaList" in expression
            return {"ok": True, "games": [{"appid": "wx-a", "name": "游戏 A"}]}

        def close(self):
            pass

    monkeypatch.setattr("config.cdp.CdpMpPage", Page)

    assert list_managed_games() == [ManagedGameDTO("wx-a", "游戏 A")]


@pytest.mark.django_db
def test_admin_sync_failure_keeps_cached_catalog(monkeypatch):
    game = ManagedGame.objects.create(appid="wx-cached", name="缓存游戏")
    monkeypatch.setattr(
        "posts.admin.list_managed_games", lambda: (_ for _ in ()).throw(CdpUnavailableError())
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
