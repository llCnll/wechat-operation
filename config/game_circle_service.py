"""The sole owner of controlled Chrome game-circle sessions and credentials.

Session establishment is deliberately a service capability.  Callers may consume a
published lease, but cannot make a source request create a target, switch a game or
recover a browser session as a side effect.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, Iterator
from uuid import uuid4

from django.utils import timezone

from config.cdp import (
    CONTROLLED_LOGIN_URL,
    CdpBrowser,
    CdpBrowserUnavailableError,
    CdpGameFrame,
    CdpTargetUnavailableError,
    CdpUnavailableError,
    bind_game_identity,
)
from config.game_circle_adapter import (
    ExplicitOuterPage,
    GameCircleContractError,
    GameCircleOuterAdapter,
    GameSelectionError,
    OuterSessionUnreadyError,
    plugin_redirect_url,
)
from config.game_circle_ipc import GameCircleIpcError, IpcRequest, WriteResultUnknownError
from config.runtime_config import public_runtime_config
from config.settings import (
    GAME_CIRCLE_MAX_CONCURRENT_ACCESSES,
    GAME_CIRCLE_SESSION_RETIRE_SECONDS,
    RUNTIME_DIR,
)
from jobs.adapters import (
    PluginSessionExpiredError,
    SourceBusinessDeletedError,
    SourceContractError,
    SourceTransportError,
)
from posts.game_catalog import ExecutionScope, current_execution_scope
from posts.models import GameCircleLoginRuntimeState
from posts.source_client import CdpGameCircleClient
from reporting.source_client import CdpGameCircleMetricClient
from service_supervisor.runtime import RuntimeStore
from jobs.services import JobTerminalError
from posts.game_catalog import resolve_execution_scope


LOGIN_WAIT_SECONDS = 15 * 60
SESSION_TARGET_ROLES = frozenset(
    {"HOME_IDLE", "LOGIN_PENDING", "HOME_CANDIDATE", "PLUGIN_CANDIDATE", "LEASE_HOME", "LEASE_PLUGIN"}
)


class BootstrapIntent(str, Enum):
    """The caller's bounded establishment request.

    Keeping intent explicit prevents a control action from accidentally acquiring
    another action's side effects (for example, a login check switching games).
    """

    ESTABLISH_FRAME = "ESTABLISH_FRAME"
    CHECK_OUTER_SESSION = "CHECK_OUTER_SESSION"
    REPAIR_BROKEN_FRAME = "REPAIR_BROKEN_FRAME"
    KEEPALIVE = "KEEPALIVE"

    @property
    def checks_outer_only(self) -> bool:
        return self is BootstrapIntent.CHECK_OUTER_SESSION

    @property
    def requires_fresh_candidate(self) -> bool:
        """Only keepalive refreshes a healthy lease on purpose.

        Repair is a conditional replacement: it creates a candidate only after
        the current owner's registered HOME or Frame is observed missing.
        """
        return self is BootstrapIntent.KEEPALIVE


@dataclass
class OwnedTarget:
    target_id: str
    role: str
    owner_epoch: str
    appid: str = ""


@dataclass
class SessionLease:
    appid: str
    browser_generation: str
    home_target_id: str
    plugin_target_id: str
    source_token: str
    issued_at: datetime
    state: str = "ACTIVE"
    retire_not_before_at: datetime | None = None
    active_accesses: dict[str, "_AccessState"] = field(default_factory=dict)


@dataclass
class _AccessState:
    deadline_monotonic: float
    frames: list[Any] = field(default_factory=list)
    cancelled: bool = False


@dataclass(frozen=True)
class SessionAccess:
    access_id: str
    lease: SessionLease
    state: _AccessState


@dataclass
class _LoginPending:
    target_id: str
    browser_generation: str
    expires_at: datetime


@dataclass(frozen=True)
class BootstrapResult:
    """Sanitised outcome of one bounded session-establishment attempt."""

    lease_published: bool
    category: str = GameCircleLoginRuntimeState.ErrorCategory.NONE
    terminal: bool = False


@dataclass
class _BootstrapFlight:
    done: Event = field(default_factory=Event)
    result: BootstrapResult | None = None
    error: Exception | None = None


class _DeadlineBoundFrame:
    """Bind one source frame to its pinned lease access lifetime."""

    def __init__(self, service: "GameCircleSessionService", access: SessionAccess, frame: CdpGameFrame):
        self._service = service
        self._access = access
        self._frame = frame

    @property
    def timeout(self) -> float:
        return self._frame.timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._frame.timeout = value

    def evaluate(self, expression: str) -> Any:
        remaining = self._service._register_frame_for_access(self._access, self._frame)
        original = self._frame.timeout
        self._frame.timeout = min(original, remaining)
        try:
            return self._frame.evaluate(expression)
        finally:
            self._frame.timeout = original

    def close(self) -> None:
        try:
            self._frame.close()
        finally:
            self._service._unregister_frame_for_access(self._access, self._frame)


class GameCircleSessionService:
    """Current-owner, in-memory session service with a serial control lane."""

    def __init__(self, *, runtime_store: RuntimeStore | None = None) -> None:
        self.owner_epoch = uuid4().hex
        self._store = runtime_store or RuntimeStore(RUNTIME_DIR)
        self._lock = RLock()
        self._control_lane = RLock()
        self._browser_generation = ""
        self._leases: dict[str, list[SessionLease]] = {}
        self._owned_targets: dict[str, OwnedTarget] = {}
        self._bootstrap_flights: dict[tuple[str, BootstrapIntent], _BootstrapFlight] = {}
        self._login_pending: _LoginPending | None = None
        self._stopping = False
        self._maintenance_stop = Event()
        self._maintenance_thread: Thread | None = None
        self.max_concurrent_accesses = GAME_CIRCLE_MAX_CONCURRENT_ACCESSES
        self._save_projection()

    # -- lifecycle and safe projection -------------------------------------------------

    def start(self) -> None:
        """Start the non-blocking bootstrap and login-wait observer exactly once."""
        if self._maintenance_thread is not None:
            return
        self._maintenance_thread = Thread(target=self._run_maintenance, name="game-circle-session", daemon=True)
        self._maintenance_thread.start()

    def _run_maintenance(self) -> None:
        self.bootstrap_current_scope(intent=BootstrapIntent.ESTABLISH_FRAME)
        while not self._maintenance_stop.wait(2):
            self._observe_login_pending()
            self._repair_if_enabled()
            self._reclaim_retiring()

    def stop_accepting(self) -> None:
        with self._lock:
            self._stopping = True
            self._maintenance_stop.set()
            active = [state for leases in self._leases.values() for lease in leases for state in lease.active_accesses.values()]
        for state in active:
            self._cancel_access_state(state)
        if self._maintenance_thread is not None:
            self._maintenance_thread.join(timeout=3)
        self._close_owned_targets_for_shutdown()
        with self._lock:
            self._login_pending = None
            self._leases.clear()
            self._owned_targets.clear()
            self._save_projection()

    def _save_projection(self) -> None:
        """Persist counts and fixed states only; never browser or credential identifiers."""
        with self._lock:
            role_counts: dict[str, int] = {}
            for target in self._owned_targets.values():
                role_counts[target.role] = role_counts.get(target.role, 0) + 1
            self._store.write_json(
                self._store.game_circle_session_path,
                {
                    "target_role_counts": role_counts,
                    "active_lease_count": sum(
                        1 for leases in self._leases.values() for lease in leases if lease.state == "ACTIVE"
                    ),
                    "retiring_lease_count": sum(
                        1 for leases in self._leases.values() for lease in leases if lease.state == "RETIRING"
                    ),
                },
            )

    def _set_runtime(
        self,
        outer: str,
        frame: str,
        *,
        category: str = GameCircleLoginRuntimeState.ErrorCategory.NONE,
        actions: tuple[str, ...] = (),
        wait_until: datetime | None = None,
    ) -> None:
        row, _ = GameCircleLoginRuntimeState.objects.get_or_create(pk=1)
        legacy = (
            GameCircleLoginRuntimeState.LoginState.READY
            if frame == GameCircleLoginRuntimeState.FrameState.READY
            else GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED
            if outer == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING
            else GameCircleLoginRuntimeState.LoginState.UNAVAILABLE
            if outer == GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE
            else GameCircleLoginRuntimeState.LoginState.UNKNOWN
        )
        now = timezone.now()
        row.login_state = legacy
        row.outer_state = outer
        row.frame_state = frame
        row.available_actions = list(actions)
        row.login_wait_until = wait_until
        row.error_category = category
        row.checked_at = now
        if frame == GameCircleLoginRuntimeState.FrameState.READY:
            row.last_ready_at = now
        row.save(
            update_fields=[
                "login_state", "outer_state", "frame_state", "available_actions", "login_wait_until",
                "error_category", "checked_at", "last_ready_at", "updated_at",
            ]
        )

    def runtime_status(self) -> dict[str, object]:
        row = GameCircleLoginRuntimeState.objects.filter(pk=1).first()
        if row is None:
            return {"outer_state": "UNKNOWN", "frame_state": "UNKNOWN", "available_actions": []}
        return {
            "outer_state": row.outer_state,
            "frame_state": row.frame_state,
            "available_actions": list(row.available_actions or []),
            "login_wait_until": row.login_wait_until.isoformat() if row.login_wait_until else None,
            "error_category": row.error_category,
        }

    # -- current-owner target and lease bookkeeping -------------------------------------

    def _sync_generation(self, browser: CdpBrowser) -> str:
        generation = browser.browser_generation()
        with self._lock:
            if self._browser_generation and self._browser_generation != generation:
                self._leases.clear()
                self._owned_targets.clear()
                self._login_pending = None
            self._browser_generation = generation
            self._save_projection()
        return generation

    def _register_owned(self, target_id: str, role: str, *, appid: str = "") -> None:
        if not target_id:
            raise CdpTargetUnavailableError("受控 Chrome 未返回新 target")
        with self._lock:
            self._owned_targets[target_id] = OwnedTarget(target_id, role, self.owner_epoch, appid)
            self._save_projection()

    def _owned_target(self, role: str, *, appid: str = "") -> str | None:
        with self._lock:
            for target in self._owned_targets.values():
                if target.owner_epoch == self.owner_epoch and target.role == role and (not appid or target.appid == appid):
                    return target.target_id
        return None

    def _safe_close(self, browser: CdpBrowser, target_id: str) -> bool:
        with self._lock:
            if target_id not in self._owned_targets:
                return False
        try:
            browser.close_target(target_id)
        except CdpTargetUnavailableError:
            try:
                if browser.target_exists(target_id):
                    return False
            except CdpUnavailableError:
                return False
        except CdpUnavailableError:
            return False
        with self._lock:
            self._owned_targets.pop(target_id, None)
            if self._login_pending and self._login_pending.target_id == target_id:
                self._login_pending = None
            self._save_projection()
        return True

    def _close_owned_targets_for_shutdown(self) -> None:
        try:
            browser = CdpBrowser()
        except CdpUnavailableError:
            return
        with self._lock:
            target_ids = [target.target_id for target in self._owned_targets.values() if target.role in SESSION_TARGET_ROLES]
        for target_id in target_ids:
            self._safe_close(browser, target_id)

    def _active_lease(self, scope: ExecutionScope) -> SessionLease | None:
        with self._lock:
            for lease in self._leases.get(scope.target_appid, []):
                if lease.state == "ACTIVE" and lease.browser_generation == self._browser_generation:
                    return lease
        return None

    def _publish_lease(self, *, scope: ExecutionScope, generation: str, home: str, plugin: str, source_token: str) -> None:
        with self._lock:
            now = timezone.now()
            leases = self._leases.setdefault(scope.target_appid, [])
            for lease in leases:
                if lease.state == "ACTIVE":
                    lease.state = "RETIRING"
                    lease.retire_not_before_at = now + timedelta(seconds=GAME_CIRCLE_SESSION_RETIRE_SECONDS)
            leases.insert(0, SessionLease(scope.target_appid, generation, home, plugin, source_token, now))
            self._register_owned(home, "LEASE_HOME", appid=scope.target_appid)
            self._register_owned(plugin, "LEASE_PLUGIN", appid=scope.target_appid)
            self._save_projection()

    def _reclaim_retiring(self) -> None:
        """Close only eligible current-owner leases, outside bookkeeping locks."""
        with self._control_lane:
            with self._lock:
                now = timezone.now()
                candidates = [
                    lease
                    for leases in self._leases.values()
                    for lease in leases
                    if lease.state == "RETIRING"
                    and not lease.active_accesses
                    and lease.retire_not_before_at is not None
                    and lease.retire_not_before_at <= now
                ]
            if not candidates:
                return
            try:
                browser = CdpBrowser()
            except CdpUnavailableError:
                return
            reclaimed: list[SessionLease] = []
            for lease in candidates:
                with self._lock:
                    plugin_known = lease.plugin_target_id in self._owned_targets
                    home_known = lease.home_target_id in self._owned_targets
                plugin_closed = self._safe_close(browser, lease.plugin_target_id) if plugin_known else True
                home_closed = self._safe_close(browser, lease.home_target_id) if home_known else True
                if plugin_closed and home_closed:
                    reclaimed.append(lease)
            if not reclaimed:
                return
            with self._lock:
                for appid, leases in list(self._leases.items()):
                    retained = [lease for lease in leases if lease not in reclaimed]
                    if retained:
                        self._leases[appid] = retained
                    else:
                        self._leases.pop(appid, None)
                self._save_projection()

    # -- source access: fixed lease only -------------------------------------------------

    @contextmanager
    def access(self, scope: ExecutionScope, *, timeout_seconds: float = 30) -> Iterator[SessionAccess]:
        if self._stopping:
            raise GameCircleIpcError("SERVICE_UNAVAILABLE", "游戏圈会话服务正在停止")
        lease = self._active_lease(scope)
        if lease is None:
            raise GameCircleIpcError("SESSION_UNAVAILABLE", "当前游戏 Frame 尚未就绪，请先人工建立")
        access_id = uuid4().hex
        state = _AccessState(deadline_monotonic=monotonic() + timeout_seconds)
        with self._lock:
            if lease.state != "ACTIVE":
                raise GameCircleIpcError("SESSION_UNAVAILABLE", "当前游戏 Frame 尚未就绪，请先人工建立")
            if self.max_concurrent_accesses and len(lease.active_accesses) >= self.max_concurrent_accesses:
                raise GameCircleIpcError("RATE_LIMITED", "游戏圈会话当前请求过多")
            lease.active_accesses[access_id] = state
            self._save_projection()
        try:
            yield SessionAccess(access_id, lease, state)
        finally:
            self._cancel_access_state(state)
            with self._lock:
                lease.active_accesses.pop(access_id, None)
                self._save_projection()
            self._reclaim_retiring()

    def _cancel_access_state(self, state: _AccessState) -> None:
        with self._lock:
            if state.cancelled:
                return
            state.cancelled = True
            frames, state.frames = list(state.frames), []
        for frame in frames:
            try:
                frame.close()
            except Exception:
                pass

    def _register_frame_for_access(self, access: SessionAccess, frame: CdpGameFrame) -> float:
        with self._lock:
            remaining = access.state.deadline_monotonic - monotonic()
            if access.state.cancelled or remaining <= 0:
                raise SourceTransportError("游戏圈来源请求已超时或被取消")
            if frame not in access.state.frames:
                access.state.frames.append(frame)
            return remaining

    def _unregister_frame_for_access(self, access: SessionAccess, frame: CdpGameFrame) -> None:
        with self._lock:
            if frame in access.state.frames:
                access.state.frames.remove(frame)

    def _post_client(self, access: SessionAccess) -> CdpGameCircleClient:
        return CdpGameCircleClient(
            appid=access.lease.appid,
            frame=_DeadlineBoundFrame(self, access, CdpGameFrame(target_id=access.lease.plugin_target_id)),
            identity_token=access.lease.source_token,
        )

    def _metric_client(self, access: SessionAccess) -> CdpGameCircleMetricClient:
        return CdpGameCircleMetricClient(
            appid=access.lease.appid,
            frame=_DeadlineBoundFrame(self, access, CdpGameFrame(target_id=access.lease.plugin_target_id)),
            identity_token=access.lease.source_token,
        )

    # -- bootstrap and explicit controls -------------------------------------------------

    def bootstrap_current_scope(self, *, intent: BootstrapIntent) -> BootstrapResult:
        try:
            scope = current_execution_scope(persist_default=False)
        except ValueError:
            scope = None
        return self.bootstrap(
            scope=scope,
            intent=intent,
        )

    @staticmethod
    def _is_current_scope(scope: ExecutionScope) -> bool:
        try:
            return current_execution_scope(persist_default=False) == scope
        except ValueError:
            return False

    def bootstrap(
        self,
        *,
        scope: ExecutionScope | None,
        intent: BootstrapIntent,
    ) -> BootstrapResult:
        """Create one verified candidate lease, or publish an explicit unready state.

        The caller receives no target, credential or full URL.  An outer-session
        check stops before game selection; all other behaviours are named intents.
        """
        key = (scope.target_appid if scope else "__outer__", intent)
        with self._lock:
            waiting = self._bootstrap_flights.get(key)
            if waiting is None:
                waiting = _BootstrapFlight()
                self._bootstrap_flights[key] = waiting
                leader = True
            else:
                leader = False
        if not leader:
            waiting.done.wait(timeout=30)
            if waiting.error is not None:
                raise waiting.error
            return waiting.result or BootstrapResult(
                lease_published=False,
                category=GameCircleLoginRuntimeState.ErrorCategory.TARGET_UNAVAILABLE,
            )
        result = BootstrapResult(
            lease_published=False,
            category=GameCircleLoginRuntimeState.ErrorCategory.TARGET_UNAVAILABLE,
        )
        error: Exception | None = None
        try:
            with self._control_lane:
                result = self._bootstrap_locked(
                    scope=scope,
                    intent=intent,
                )
        except Exception as exc:
            error = exc
            raise
        finally:
            with self._lock:
                flight = self._bootstrap_flights.pop(key, None)
                if flight:
                    flight.result = result
                    flight.error = error
                    flight.done.set()
        return result

    def _ensure_home(self, browser: CdpBrowser) -> str:
        existing = self._owned_target("HOME_IDLE")
        if existing and browser.target_exists(existing):
            return existing
        target = browser.create_target("https://mp.weixin.qq.com/", background=True)
        self._register_owned(target, "HOME_IDLE")
        return target

    def _outer_session(self, home: str) -> tuple[ExplicitOuterPage, GameCircleOuterAdapter, str | None]:
        page = ExplicitOuterPage(target_id=home)
        if not page.wait_for_public_page():
            page.close()
            raise CdpTargetUnavailableError("受控 HOME 未在限定时间内就绪")
        outer = GameCircleOuterAdapter(page)
        if outer.scan_required():
            return page, outer, None
        try:
            return page, outer, outer.read_outer_token()
        except OuterSessionUnreadyError:
            return page, outer, None

    def _set_bootstrap_failure(
        self, scope: ExecutionScope | None, category: str, *, broken_active_lease: bool
    ) -> None:
        """Project failure without hiding a still-usable lease for the current scope."""
        if scope is not None:
            try:
                if current_execution_scope(persist_default=False) != scope:
                    return
            except ValueError:
                pass
        ready = not broken_active_lease and scope is not None and self._active_lease(scope) is not None
        self._set_runtime(
            GameCircleLoginRuntimeState.OuterState.OUTER_READY,
            GameCircleLoginRuntimeState.FrameState.READY if ready else GameCircleLoginRuntimeState.FrameState.UNREADY,
            category=GameCircleLoginRuntimeState.ErrorCategory.NONE if ready else category,
            actions=(
                (GameCircleLoginRuntimeState.Action.CHECK_LOGIN, GameCircleLoginRuntimeState.Action.KEEPALIVE)
                if ready
                else (GameCircleLoginRuntimeState.Action.BUILD_FRAME, GameCircleLoginRuntimeState.Action.CHECK_LOGIN)
            ),
        )

    def _bootstrap_locked(
        self,
        *,
        scope: ExecutionScope | None,
        intent: BootstrapIntent,
    ) -> BootstrapResult:
        home = plugin = ""
        page: ExplicitOuterPage | None = None
        close_candidate_home = True
        broken_active_lease = False
        try:
            self._set_runtime(
                GameCircleLoginRuntimeState.OuterState.BOOTSTRAPPING,
                GameCircleLoginRuntimeState.FrameState.BUILDING
                if scope and not intent.checks_outer_only
                else GameCircleLoginRuntimeState.FrameState.UNREADY,
            )
            browser = CdpBrowser()
            generation = self._sync_generation(browser)
            active_lease = self._active_lease(scope) if scope is not None else None
            if active_lease is not None:
                active_targets_exist = (
                    browser.target_exists(active_lease.home_target_id)
                    and browser.target_exists(active_lease.plugin_target_id)
                )
                broken_active_lease = not active_targets_exist
                if active_targets_exist and not intent.requires_fresh_candidate and not intent.checks_outer_only:
                    self._set_runtime(
                        GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                        GameCircleLoginRuntimeState.FrameState.READY,
                        actions=(GameCircleLoginRuntimeState.Action.CHECK_LOGIN, GameCircleLoginRuntimeState.Action.KEEPALIVE),
                    )
                    return BootstrapResult(False)
            if intent.checks_outer_only and active_lease is not None and not broken_active_lease:
                # Checking the outer session is observational: use the verified
                # lease HOME instead of creating an otherwise idle target.
                home = active_lease.home_target_id
                close_candidate_home = False
            else:
                home = self._ensure_home(browser)
            page, outer, token = self._outer_session(home)
            if token is None:
                self._register_login_pending(home, generation)
                close_candidate_home = False
                return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED, terminal=True)
            self._clear_login_pending()
            if intent.checks_outer_only:
                frame_ready = active_lease is not None and not broken_active_lease
                close_candidate_home = False
                self._set_runtime(
                    GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                    GameCircleLoginRuntimeState.FrameState.READY if frame_ready else GameCircleLoginRuntimeState.FrameState.UNREADY,
                    actions=(
                        (GameCircleLoginRuntimeState.Action.CHECK_LOGIN, GameCircleLoginRuntimeState.Action.KEEPALIVE)
                        if frame_ready
                        else (GameCircleLoginRuntimeState.Action.BUILD_FRAME,)
                    ),
                )
                return BootstrapResult(False)
            if scope is None:
                close_candidate_home = False
                self._set_runtime(
                    GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                    GameCircleLoginRuntimeState.FrameState.UNREADY,
                    actions=(GameCircleLoginRuntimeState.Action.CHECK_LOGIN,),
                )
                return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY, terminal=True)
            current_appid = outer.wait_for_readable_current_game()
            if intent is BootstrapIntent.KEEPALIVE and current_appid == scope.target_appid:
                intermediates = sorted({appid for appid in outer.list_switchable_game_appids() if appid != scope.target_appid})
                if not intermediates:
                    raise GameSelectionError("当前受控账号没有可用的保活中转游戏")
                outer.switch_to_game(intermediates[0])
                outer.reload_after_game_switch()
                outer.wait_for_current_game(intermediates[0])
                token = outer.read_outer_token()
            if current_appid != scope.target_appid or intent is BootstrapIntent.KEEPALIVE:
                outer.switch_to_game(scope.target_appid)
                outer.reload_after_game_switch()
                outer.wait_for_current_game(scope.target_appid)
                token = outer.read_outer_token()
            page.close()
            page = None
            plugin = browser.create_target(plugin_redirect_url(token), background=True)
            self._register_owned(plugin, "PLUGIN_CANDIDATE", appid=scope.target_appid)
            frame = CdpGameFrame(target_id=plugin)
            try:
                if not frame.wait_for_gamemp_iframe():
                    raise CdpTargetUnavailableError("候选插件缺少稳定游戏圈 iframe")
                source_token = bind_game_identity(scope.target_appid, frame=frame)
                verifier = CdpGameCircleClient(appid=scope.target_appid, frame=frame, identity_token=source_token)
                verifier.list_candidates(page=0, page_size=1, start_time=0, end_time=int(timezone.now().timestamp()))
            finally:
                frame.close()
            self._publish_lease(scope=scope, generation=generation, home=home, plugin=plugin, source_token=source_token)
            home = plugin = ""
            if self._is_current_scope(scope):
                self._set_runtime(
                    GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                    GameCircleLoginRuntimeState.FrameState.READY,
                    actions=(GameCircleLoginRuntimeState.Action.CHECK_LOGIN, GameCircleLoginRuntimeState.Action.KEEPALIVE),
                )
            else:
                # A frozen keepalive may finish after the operator selects a
                # different game. Its lease remains valid for its own scope,
                # but it must never overwrite the current-scope UI projection.
                self._set_runtime(
                    GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                    GameCircleLoginRuntimeState.FrameState.UNREADY,
                    category=GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY,
                    actions=(GameCircleLoginRuntimeState.Action.BUILD_FRAME,),
                )
            return BootstrapResult(True)
        except CdpBrowserUnavailableError:
            self._set_runtime(
                GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE,
                GameCircleLoginRuntimeState.FrameState.UNKNOWN,
                category=GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE,
                actions=(GameCircleLoginRuntimeState.Action.CHECK_LOGIN,),
            )
            return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE)
        except GameSelectionError:
            category = GameCircleLoginRuntimeState.ErrorCategory.GAME_NOT_SELECTABLE
            self._set_bootstrap_failure(scope, category, broken_active_lease=broken_active_lease)
            return BootstrapResult(
                False,
                category,
                terminal=True,
            )
        except GameCircleContractError:
            category = GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY
            self._set_bootstrap_failure(scope, category, broken_active_lease=broken_active_lease)
            return BootstrapResult(False, category)
        except CdpTargetUnavailableError:
            category = GameCircleLoginRuntimeState.ErrorCategory.TARGET_UNAVAILABLE
            self._set_bootstrap_failure(scope, category, broken_active_lease=broken_active_lease)
            return BootstrapResult(False, category)
        except (SourceContractError, SourceTransportError, PluginSessionExpiredError):
            category = GameCircleLoginRuntimeState.ErrorCategory.SOURCE_VERIFICATION_FAILED
            self._set_bootstrap_failure(scope, category, broken_active_lease=broken_active_lease)
            return BootstrapResult(False, category)
        finally:
            if page is not None:
                page.close()
            if "browser" in locals():
                if plugin:
                    self._safe_close(browser, plugin)
                if home and close_candidate_home:
                    self._safe_close(browser, home)

    def _repair_if_enabled(self) -> None:
        """A disabled repair loop has no browser side effects at all."""
        if public_runtime_config().get("session_repair_enabled") != "true":
            return
        try:
            scope = current_execution_scope(persist_default=False)
        except ValueError:
            return
        if self._active_lease(scope) is None:
            return
        try:
            self.bootstrap(scope=scope, intent=BootstrapIntent.REPAIR_BROKEN_FRAME)
        except CdpUnavailableError:
            self._set_runtime(
                GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE,
                GameCircleLoginRuntimeState.FrameState.UNKNOWN,
                category=GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE,
                actions=(GameCircleLoginRuntimeState.Action.CHECK_LOGIN,),
            )

    def keepalive(self, job) -> None:
        """Freeze scope on consumer ownership, then publish a verified candidate lease."""
        try:
            scope = resolve_execution_scope(job)
        except ValueError as exc:
            raise JobTerminalError("NO_ACTIVE_GAME_SCOPE") from exc
        result = self.bootstrap(
            scope=scope,
            intent=BootstrapIntent.KEEPALIVE,
        )
        if result.lease_published:
            return
        if result.terminal:
            raise JobTerminalError(result.category)
        raise RuntimeError(result.category)

    def _register_login_pending(self, home: str, generation: str) -> None:
        expires = timezone.now() + timedelta(seconds=LOGIN_WAIT_SECONDS)
        with self._lock:
            self._login_pending = _LoginPending(home, generation, expires)
            target = self._owned_targets.get(home)
            if target:
                target.role = "LOGIN_PENDING"
            self._save_projection()
        self._set_runtime(
            GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING,
            GameCircleLoginRuntimeState.FrameState.UNREADY,
            category=GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED,
            actions=(GameCircleLoginRuntimeState.Action.OPEN_LOGIN,),
            wait_until=expires,
        )

    def _clear_login_pending(self) -> None:
        with self._lock:
            pending = self._login_pending
            self._login_pending = None
            if pending and pending.target_id in self._owned_targets:
                self._owned_targets[pending.target_id].role = "HOME_IDLE"
            self._save_projection()

    def _observe_login_pending(self) -> None:
        """Inspect the registered login page under the same lane as Bootstrap."""
        with self._control_lane:
            self._observe_login_pending_locked()

    def _observe_login_pending_locked(self) -> None:
        with self._lock:
            pending = self._login_pending
        if pending is None:
            return
        if timezone.now() >= pending.expires_at:
            try:
                browser = CdpBrowser()
                self._sync_generation(browser)
                self._safe_close(browser, pending.target_id)
            except CdpUnavailableError:
                pass
            self._clear_login_pending()
            self._set_runtime(
                GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                GameCircleLoginRuntimeState.FrameState.UNREADY,
                category=GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED,
                actions=(GameCircleLoginRuntimeState.Action.OPEN_LOGIN,),
            )
            return
        try:
            browser = CdpBrowser(timeout=2)
            if self._sync_generation(browser) != pending.browser_generation or not browser.target_exists(pending.target_id):
                self._clear_login_pending()
                return
            page, outer, token = self._outer_session(pending.target_id)
            page.close()
            if token:
                self._clear_login_pending()
                self.bootstrap_current_scope(intent=BootstrapIntent.ESTABLISH_FRAME)
        except CdpUnavailableError:
            return

    def open_login_page(self) -> dict[str, str]:
        """The sole explicit action allowed to show or focus a target."""
        with self._control_lane:
            browser = CdpBrowser()
            generation = self._sync_generation(browser)
            with self._lock:
                pending = self._login_pending
            if pending and pending.browser_generation == generation and browser.target_exists(pending.target_id):
                browser.activate_target(pending.target_id)
                return {"status": "LOGIN_PAGE_REUSED"}
            target = browser.create_target(CONTROLLED_LOGIN_URL, background=False)
            self._register_owned(target, "LOGIN_PENDING")
            self._register_login_pending(target, generation)
            return {"status": "LOGIN_PAGE_OPENED"}

    # -- fixed IPC surface ---------------------------------------------------------------

    def handle_ipc(self, request: IpcRequest) -> dict[str, Any]:
        if request.method == "OPEN_CONTROLLED_LOGIN":
            try:
                return self.open_login_page()
            except CdpBrowserUnavailableError as exc:
                raise GameCircleIpcError("CHROME_UNAVAILABLE", "受控 Chrome 未启动") from exc
            except CdpUnavailableError as exc:
                raise GameCircleIpcError("LOGIN_PAGE_FAILED", "受控 Chrome 无法打开登录页") from exc
        if request.method == "CHECK_LOGIN":
            self.bootstrap_current_scope(intent=BootstrapIntent.CHECK_OUTER_SESSION)
            return {"status": "CHECK_COMPLETED", "runtime": self.runtime_status()}
        if request.method == "BUILD_CURRENT_GAME_FRAME":
            self.bootstrap_current_scope(intent=BootstrapIntent.ESTABLISH_FRAME)
            return {"status": "BOOTSTRAP_COMPLETED", "runtime": self.runtime_status()}
        if request.method == "GET_RUNTIME_STATUS":
            return {"runtime": self.runtime_status()}
        if request.method == "LIST_MANAGED_GAMES":
            return {"items": self.list_managed_games()}
        if request.scope is None:
            raise GameCircleIpcError("INVALID_SCOPE", "游戏圈操作范围无效")
        return self._execute_source_request(request)

    def _execute_source_request(self, request: IpcRequest) -> dict[str, Any]:
        requested_timeout = request.params.get("timeout_seconds", 30) if request.method == "POSTS_GET_METRICS" else 30
        try:
            timeout_seconds = float(requested_timeout)
        except (TypeError, ValueError) as exc:
            raise GameCircleIpcError("INVALID_REQUEST", "来源请求超时参数无效") from exc
        try:
            with self.access(request.scope, timeout_seconds=timeout_seconds) as access:
                if request.method.startswith("POSTS_"):
                    client = self._post_client(access)
                    try:
                        if request.method == "POSTS_LIST_CANDIDATES":
                            items = client.list_candidates(**{key: int(request.params[key]) for key in ("page", "page_size", "start_time", "end_time")})
                            return {"items": [{"topic_id": item.topic_id, "content_id": item.content_id, "source_appid": item.source_appid, "publish_time": int(item.publish_time.timestamp()) if item.publish_time else None} for item in items]}
                        if request.method == "POSTS_GET_CONTENT_CARD":
                            return {"payload": client.get_content_card(str(request.params["content_id"]))}
                        if request.method == "POSTS_GET_METRICS":
                            return {"payload": client.get_post_metrics(int(request.params["topic_id"]))}
                        if request.method == "POSTS_OPERATION":
                            payload = request.params.get("payload")
                            if not request.operation_id or not isinstance(payload, dict):
                                raise SourceContractError("人工操作请求无效")
                            return {"payload": client.perform_confirmed_operation(topic_id=int(request.params["topic_id"]), action=str(request.params["action"]), payload=payload)}
                    finally:
                        client.close()
                client = self._metric_client(access)
                try:
                    methods = {"METRIC_CIRCLE_FANS": client.circle_fans, "METRIC_CONTENT_FANS": client.content_fans, "METRIC_OPERATION_EFFECT": client.operation_effect, "METRIC_GROUP_CONTENT_ANALYSIS": client.group_content_analysis}
                    method = methods.get(request.method)
                    if method is None:
                        raise SourceContractError("不支持的游戏圈会话方法")
                    return {"payload": method(**request.params)}
                finally:
                    client.close()
        except GameCircleIpcError:
            raise
        except SourceBusinessDeletedError as exc:
            raise GameCircleIpcError("SOURCE_DELETED", "原平台帖子已从管理队列删除") from exc
        except PluginSessionExpiredError as exc:
            self._set_runtime(GameCircleLoginRuntimeState.OuterState.OUTER_READY, GameCircleLoginRuntimeState.FrameState.UNREADY, category=GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY, actions=(GameCircleLoginRuntimeState.Action.BUILD_FRAME,))
            raise GameCircleIpcError("SESSION_UNAVAILABLE", "当前游戏 Frame 已失效，请先人工建立") from exc
        except SourceContractError as exc:
            raise GameCircleIpcError("SOURCE_CONTRACT", "原平台响应不符合已验证契约") from exc
        except SourceTransportError as exc:
            if request.method == "POSTS_OPERATION":
                raise WriteResultUnknownError() from exc
            raise GameCircleIpcError("SOURCE_UNAVAILABLE", "原平台来源请求暂不可用") from exc

    def list_managed_games(self) -> list[dict[str, str]]:
        with self._control_lane:
            try:
                browser = CdpBrowser()
                self._sync_generation(browser)
                home = self._ensure_home(browser)
                page, outer, token = self._outer_session(home)
                try:
                    if token is None:
                        self._register_login_pending(home, self._browser_generation)
                        raise GameCircleIpcError("SESSION_UNAVAILABLE", "外层会话需要人工扫码")
                    return outer.list_managed_games()
                finally:
                    page.close()
            except GameCircleIpcError:
                raise
            except CdpUnavailableError as exc:
                raise GameCircleIpcError("SOURCE_UNAVAILABLE", "无法读取可管理游戏目录") from exc
            except GameCircleContractError as exc:
                raise GameCircleIpcError("SOURCE_CONTRACT", "原平台响应不符合已验证契约") from exc
