"""Local managed-game catalog and its optional upstream refresh."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from config.cdp import ManagedGameDTO

from .models import ManagedGame, OriginalPost

DEFAULT_GAME_APPID = "wx6d2454a8afcd9706"
DEFAULT_GAME_NAME = "城主别慌张"


@dataclass(frozen=True)
class GameOption:
    appid: str
    name: str
    last_synced_at: object | None = None


def ensure_default_game() -> ManagedGame:
    game, _ = ManagedGame.objects.get_or_create(
        appid=DEFAULT_GAME_APPID, defaults={"name": DEFAULT_GAME_NAME}
    )
    return game


def managed_game_options(*, persist_default: bool = True) -> list[GameOption]:
    """Return cached games plus historical AppIDs, optionally without changing the catalog."""
    if persist_default:
        ensure_default_game()
    cached = list(ManagedGame.objects.all())
    known = {game.appid for game in cached}
    options = [GameOption(game.appid, game.name, game.last_synced_at) for game in cached]
    if DEFAULT_GAME_APPID not in known:
        options.append(GameOption(DEFAULT_GAME_APPID, DEFAULT_GAME_NAME))
        known.add(DEFAULT_GAME_APPID)
    for appid in OriginalPost.objects.exclude(source_appid="").values_list(
        "source_appid", flat=True
    ).distinct():
        if appid not in known:
            options.append(GameOption(appid, f"未知游戏（{appid}）"))
    return sorted(
        options, key=lambda item: (item.appid != DEFAULT_GAME_APPID, item.name, item.appid)
    )


def active_game_appid(requested_appid: str, *, persist_default: bool = True) -> str:
    options = managed_game_options(persist_default=persist_default)
    available = {option.appid for option in options}
    return requested_appid if requested_appid in available else DEFAULT_GAME_APPID


@transaction.atomic
def sync_managed_games(games: Iterable[ManagedGameDTO]) -> int:
    """Upsert a successful source snapshot without deleting useful historical cache entries."""
    now = timezone.now()
    count = 0
    for game in games:
        if not game.appid or not game.name:
            continue
        ManagedGame.objects.update_or_create(
            appid=game.appid,
            defaults={"name": game.name[:255], "last_synced_at": now},
        )
        count += 1
    if count == 0:
        raise ValueError("游戏列表响应中没有有效游戏")
    ensure_default_game()
    return count
