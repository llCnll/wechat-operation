"""Local managed-game catalog and its optional upstream refresh."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from config.cdp import ManagedGameDTO

from .models import ActiveGameScope, ManagedGame, OriginalPost

if TYPE_CHECKING:
    from jobs.models import BackgroundJob

# Deprecated read-only compatibility export. It is deployment configuration, never a
# business AppID embedded in code; new callers must resolve ActiveGameScope instead.
DEFAULT_GAME_APPID = str(settings.DEFAULT_GAME_APPID).strip()


@dataclass(frozen=True)
class GameOption:
    appid: str
    name: str
    last_synced_at: object | None = None


@dataclass(frozen=True)
class ExecutionScope:
    target_appid: str
    scope_version: int

    def as_checkpoint(self) -> dict[str, object]:
        return {"target_appid": self.target_appid, "scope_version": self.scope_version}


def initialize_active_game_scope(*, updated_by=None) -> ActiveGameScope | None:
    """Initialize the singleton only from a configured game already in the local catalog."""
    configured_appid = str(settings.DEFAULT_GAME_APPID).strip()
    if not configured_appid:
        return None
    game = ManagedGame.objects.filter(appid=configured_appid).first()
    if game is None:
        return None
    scope, _ = ActiveGameScope.objects.get_or_create(
        pk=1,
        defaults={"active_game": game, "updated_by": updated_by},
    )
    return scope


def managed_game_options(*, persist_default: bool = True) -> list[GameOption]:
    """Return the locally synchronized catalog without manufacturing upstream games."""
    if persist_default:
        initialize_active_game_scope()
    cached = list(ManagedGame.objects.all())
    known = {game.appid for game in cached}
    options = [GameOption(game.appid, game.name, game.last_synced_at) for game in cached]
    for appid in OriginalPost.objects.exclude(source_appid="").values_list(
        "source_appid", flat=True
    ).distinct():
        if appid not in known:
            options.append(GameOption(appid, f"未知游戏（{appid}）"))
    return sorted(options, key=lambda item: (item.name, item.appid))


def active_game_appid(requested_appid: str = "", *, persist_default: bool = True) -> str:
    """Return the configured scope and reject a conflicting compatibility parameter."""
    if persist_default:
        initialize_active_game_scope()
    scope = ActiveGameScope.objects.select_related("active_game").filter(pk=1).first()
    if scope is None:
        raise ValueError("尚未配置当前操作游戏；请先同步目录并在系统配置中选择游戏")
    if requested_appid and requested_appid != scope.active_appid:
        raise ValueError("请求游戏与当前操作游戏不一致")
    return scope.active_appid


def current_execution_scope(
    requested_appid: str = "", *, persist_default: bool = True
) -> ExecutionScope:
    """Read the active scope for a synchronous, operator-confirmed source action."""
    if persist_default:
        initialize_active_game_scope()
    scope = ActiveGameScope.objects.select_related("active_game").filter(pk=1).first()
    if scope is None:
        raise ValueError("尚未配置当前操作游戏；请先同步目录并在系统配置中选择游戏")
    if requested_appid and requested_appid != scope.active_appid:
        raise ValueError("请求游戏与当前操作游戏不一致")
    return ExecutionScope(scope.active_appid, scope.scope_version)


@transaction.atomic
def set_active_game(*, appid: str, updated_by=None) -> ActiveGameScope:
    """Atomically change the sole operation scope after validating the local catalog."""
    game = ManagedGame.objects.filter(appid=appid).first()
    if game is None:
        raise ValueError("只能选择已同步的可管理游戏")
    scope = ActiveGameScope.objects.select_for_update().filter(pk=1).first()
    if scope is None:
        return ActiveGameScope.objects.create(active_game=game, updated_by=updated_by)
    if scope.active_appid != appid:
        scope.active_game = game
        scope.scope_version += 1
    scope.updated_by = updated_by
    scope.save()
    return scope


@transaction.atomic
def resolve_execution_scope(job: "BackgroundJob") -> ExecutionScope:
    """Freeze one job's game range before its first browser-side operation."""
    locked_job = type(job).objects.select_for_update().get(pk=job.pk)
    checkpoint = dict(locked_job.checkpoint or {})
    saved_scope = checkpoint.get("execution_scope")
    if isinstance(saved_scope, dict):
        appid = saved_scope.get("target_appid")
        version = saved_scope.get("scope_version")
        if (
            isinstance(appid, str)
            and appid
            and isinstance(version, int)
            and version > 0
            and ManagedGame.objects.filter(appid=appid).exists()
        ):
            job.checkpoint = checkpoint
            return ExecutionScope(target_appid=appid, scope_version=version)
        raise ValueError("任务执行范围检查点无效")

    scope = ActiveGameScope.objects.select_for_update().filter(pk=1).first()
    if scope is None:
        raise ValueError("尚未配置当前操作游戏")
    requested_appid = str((locked_job.parameter_summary or {}).get("appid", "")).strip()
    if requested_appid and requested_appid != scope.active_appid:
        raise ValueError("任务游戏与当前操作游戏不一致")
    execution_scope = ExecutionScope(
        target_appid=scope.active_appid,
        scope_version=scope.scope_version,
    )
    checkpoint["execution_scope"] = execution_scope.as_checkpoint()
    locked_job.checkpoint = checkpoint
    locked_job.save(update_fields=["checkpoint", "updated_at"])
    job.checkpoint = checkpoint
    return execution_scope


def checkpoint_with_execution_scope(
    job: "BackgroundJob", checkpoint: dict[str, object]
) -> dict[str, object]:
    """Attach an already-frozen execution scope to a replacement checkpoint.

    Handlers intentionally replace their small progress checkpoints as they
    advance.  The scope is infrastructure state, not handler progress, so it
    must survive each replacement until the job reaches its terminal state.
    """
    saved_scope = (job.checkpoint or {}).get("execution_scope")
    if not isinstance(saved_scope, dict):
        return checkpoint
    if "execution_scope" in checkpoint and checkpoint["execution_scope"] != saved_scope:
        raise ValueError("任务执行范围检查点不能被覆盖")
    return {**checkpoint, "execution_scope": saved_scope}


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
    initialize_active_game_scope()
    return count


def sync_authorized_managed_games() -> int:
    """Refresh the local catalogue through the fixed session-service IPC intent."""
    # This local import avoids making the catalog model layer initialise the IPC
    # module during Django app loading.
    from config.game_circle_ipc import GameCircleIpcClient

    items = GameCircleIpcClient().list_managed_games()
    return sync_managed_games(ManagedGameDTO(**item) for item in items)
