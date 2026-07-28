"""Game-scoped official-author configuration and presentation projection."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .models import ManagedGame, OfficialAuthor, OriginalPost


class OfficialIdentity:
    VERIFIED = "VERIFIED_OFFICIAL"
    NAME_MATCH = "GAME_NAME_MATCH"
    PLAYER = "PLAYER"


OFFICIAL_IDENTITY_LABELS = {
    OfficialIdentity.VERIFIED: "官方作者（已核验）",
    OfficialIdentity.NAME_MATCH: "官方名称匹配",
    OfficialIdentity.PLAYER: "玩家内容",
}


@dataclass(frozen=True)
class OfficialIdentityPresentation:
    key: str
    label: str


def active_official_openids(*, appid: str) -> set[str]:
    return set(OfficialAuthor.objects.filter(source_appid=appid, disabled_at__isnull=True).values_list("openid", flat=True))


def _normalized_identity_name(value: str) -> str:
    """Compare display names with only controlled whitespace normalization."""
    return " ".join(value.split())


def identity_for_values(*, author_openid: str, author_nick_name: str, game_name: str, verified_openids: set[str]) -> OfficialIdentityPresentation:
    if author_openid and author_openid in verified_openids:
        key = OfficialIdentity.VERIFIED
    elif game_name and _normalized_identity_name(author_nick_name) == _normalized_identity_name(game_name):
        key = OfficialIdentity.NAME_MATCH
    else:
        key = OfficialIdentity.PLAYER
    return OfficialIdentityPresentation(key=key, label=OFFICIAL_IDENTITY_LABELS[key])


def apply_identity_presentation(posts: list[OriginalPost], *, appid: str, game_name: str) -> None:
    verified = active_official_openids(appid=appid)
    for post in posts:
        post.official_identity = identity_for_values(author_openid=post.author_openid, author_nick_name=post.author_nick_name, game_name=game_name, verified_openids=verified)


@transaction.atomic
def enable_official_author(*, appid: str, openid: str, nick_name: str, user) -> OfficialAuthor:
    if not ManagedGame.objects.filter(appid=appid).exists():
        raise ValueError("只能选择已同步的可管理游戏")
    author, created = OfficialAuthor.objects.select_for_update().get_or_create(
        source_appid=appid,
        openid=openid,
        defaults={"nick_name": nick_name, "verified_at": timezone.now(), "created_by": user},
    )
    if not created:
        author.nick_name = nick_name
        author.verified_at = timezone.now()
        author.disabled_at = None
        author.disabled_by = None
        author.save(update_fields=("nick_name", "verified_at", "disabled_at", "disabled_by", "updated_at"))
    return author


@transaction.atomic
def disable_official_author(*, author: OfficialAuthor, user) -> OfficialAuthor:
    author = OfficialAuthor.objects.select_for_update().get(pk=author.pk)
    if author.disabled_at is None:
        author.disabled_at = timezone.now()
        author.disabled_by = user
        author.save(update_fields=("disabled_at", "disabled_by", "updated_at"))
    return author
