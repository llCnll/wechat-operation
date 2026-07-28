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
    OuterLoginExpiredError,
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
    SourceAuthenticationRejectedError,
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
LOGIN_COMPLETION_SETTLE_SECONDS = 10
OUTER_SESSION_PROBE_SECONDS = 5 * 60
SOURCE_AUTH_RECHECK_COOLDOWN_SECONDS = 5
ZERO_LEASE_REPAIR_RETRY_SECONDS = 15
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

        Repair establishes a candidate only when the current scope has no
        usable lease, or its registered HOME or Frame is observed missing.
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
    settle_not_before_at: datetime | None = None


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


class _OuterSessionKind(str, Enum):
    NO_USABLE_TOKEN = "NO_USABLE_TOKEN"
    VERIFIED_WITH_TOKEN = "VERIFIED_WITH_TOKEN"
    TRANSIENT_UNREADY = "TRANSIENT_UNREADY"


@dataclass
class _OuterSession:
    """Facts observed from one service-owned HOME without conflating token absence."""

    page: ExplicitOuterPage
    outer: GameCircleOuterAdapter
    kind: _OuterSessionKind
    token: str | None = None
    current_appid: str | None = None


class _OuterRecheckOutcome(str, Enum):
    """The only outcomes a source-authentication recheck may publish."""

    OUTER_READY = "OUTER_READY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    INDETERMINATE = "INDETERMINATE"


class _LoginWaitResolution(str, Enum):
    """The only terminal transitions for a manually opened login page."""

    RECOVER_SESSION = "RECOVER_SESSION"
    REQUIRE_LOGIN = "REQUIRE_LOGIN"


@dataclass
class _OuterRecheckFlight:
    """One generation-scoped outer verification shared by source failures."""

    browser_generation: str
    expires_at_monotonic: float
    done: Event = field(default_factory=Event)
    outcome: _OuterRecheckOutcome = _OuterRecheckOutcome.INDETERMINATE


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
        self._next_outer_probe_at = 0.0
        self._next_zero_lease_repair_at = 0.0
        self._source_auth_recheck: _OuterRecheckFlight | None = None
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
            self._probe_outer_if_due()
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
        frame_scope_version: int | None = None,
        outer_observed: bool = False,
        outer_verified: bool = False,
    ) -> None:
        """Persist a projection without mistaking target liveness for login evidence."""
        row, _ = GameCircleLoginRuntimeState.objects.get_or_create(pk=1)
        legacy = self._legacy_login_state(outer, frame)
        now = timezone.now()
        row.login_state = legacy
        row.outer_state = outer
        row.frame_state = frame
        row.frame_scope_version = (
            frame_scope_version if frame == GameCircleLoginRuntimeState.FrameState.READY else None
        )
        row.available_actions = list(actions)
        row.login_wait_until = wait_until
        row.error_category = category
        if outer_observed or outer_verified:
            row.checked_at = now
        if outer_verified:
            row.outer_verified_at = now
        if frame == GameCircleLoginRuntimeState.FrameState.READY:
            row.last_ready_at = now
        row.save(
            update_fields=[
                "login_state", "outer_state", "frame_state", "frame_scope_version", "available_actions", "login_wait_until",
                "error_category", "checked_at", "outer_verified_at", "last_ready_at", "updated_at",
            ]
        )

    @staticmethod
    def _legacy_login_state(outer: str, frame: str) -> str:
        if frame == GameCircleLoginRuntimeState.FrameState.READY:
            return GameCircleLoginRuntimeState.LoginState.READY
        if outer in {
            GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING,
            GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED,
        }:
            return GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED
        if outer == GameCircleLoginRuntimeState.OuterState.BROWSER_UNAVAILABLE:
            return GameCircleLoginRuntimeState.LoginState.UNAVAILABLE
        return GameCircleLoginRuntimeState.LoginState.UNKNOWN

    def runtime_status(self) -> dict[str, object]:
        row = GameCircleLoginRuntimeState.objects.filter(pk=1).first()
        if row is None:
            return {
                "outer_state": "UNKNOWN",
                "frame_state": "UNKNOWN",
                "frame_scope_version": None,
                "available_actions": [],
            }
        return {
            "outer_state": row.outer_state,
            "frame_state": row.frame_state,
            "frame_scope_version": row.frame_scope_version,
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
        with self._lock:
            candidates = [
                (target.target_id, target.role)
                for target in self._owned_targets.values()
                if target.owner_epoch == self.owner_epoch and target.role in {"HOME_IDLE", "LOGIN_PENDING"}
            ]
        live: dict[str, list[str]] = {"LOGIN_PENDING": [], "HOME_IDLE": []}
        for target_id, role in candidates:
            if browser.target_exists(target_id):
                live[role].append(target_id)
                continue
            with self._lock:
                self._owned_targets.pop(target_id, None)
                if self._login_pending and self._login_pending.target_id == target_id:
                    self._login_pending = None
                self._save_projection()
        if live["LOGIN_PENDING"] or live["HOME_IDLE"]:
            # An explicit scan page is the user's active recovery context and
            # wins over an older idle page.  Only current-owner extras are safe
            # to close here.
            keeper = (live["LOGIN_PENDING"] or live["HOME_IDLE"])[0]
            for target_id in [*live["LOGIN_PENDING"], *live["HOME_IDLE"]]:
                if target_id == keeper:
                    continue
                self._safe_close(browser, target_id)
            return keeper
        target = browser.create_target("https://mp.weixin.qq.com/", background=True)
        self._register_owned(target, "HOME_IDLE")
        return target

    def _observable_home(self, browser: CdpBrowser) -> str:
        """Prefer the current lease HOME so UI and source calls share one page."""
        try:
            scope = current_execution_scope(persist_default=False)
        except ValueError:
            scope = None
        lease = self._active_lease(scope) if scope is not None else None
        if lease is not None and browser.target_exists(lease.home_target_id):
            return lease.home_target_id
        return self._ensure_home(browser)

    def _outer_session(self, home: str) -> _OuterSession:
        page = ExplicitOuterPage(target_id=home)
        if not page.wait_for_public_page():
            page.close()
            raise CdpTargetUnavailableError("受控 HOME 未在限定时间内就绪")
        outer = GameCircleOuterAdapter(page)
        current_appid: str | None = None
        try:
            verified = outer.wait_for_stable_outer_session()
            if verified is None:
                return _OuterSession(page, outer, _OuterSessionKind.NO_USABLE_TOKEN)
            current_appid = verified.appid
            return _OuterSession(
                page,
                outer,
                _OuterSessionKind.VERIFIED_WITH_TOKEN,
                verified.token,
                current_appid,
            )
        except OuterSessionUnreadyError:
            return _OuterSession(
                page,
                outer,
                _OuterSessionKind.TRANSIENT_UNREADY,
                current_appid=current_appid,
            )
        except (CdpUnavailableError, GameCircleContractError, OuterLoginExpiredError):
            page.close()
            raise

    def _retire_generation_leases(self) -> None:
        now = timezone.now()
        with self._lock:
            for leases in self._leases.values():
                for lease in leases:
                    if lease.browser_generation == self._browser_generation and lease.state == "ACTIVE":
                        lease.state = "RETIRING"
                        lease.retire_not_before_at = now
            self._save_projection()

    def _lease_is_active(self, lease: SessionLease) -> bool:
        with self._lock:
            return (
                lease.browser_generation == self._browser_generation
                and lease.state == "ACTIVE"
                and any(candidate is lease for candidate in self._leases.get(lease.appid, []))
            )

    def _project_current_scope_frame_unready(self, scope: ExecutionScope) -> None:
        if not self._is_current_scope(scope):
            return
        row = GameCircleLoginRuntimeState.objects.filter(pk=1).first()
        outer = row.outer_state if row is not None else GameCircleLoginRuntimeState.OuterState.UNKNOWN
        self._set_runtime(
            outer,
            GameCircleLoginRuntimeState.FrameState.UNREADY,
            category=GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY,
            actions=(
                GameCircleLoginRuntimeState.Action.BUILD_FRAME,
                GameCircleLoginRuntimeState.Action.CHECK_LOGIN,
            ),
        )

    def _retire_rejected_lease(self, scope: ExecutionScope, lease: SessionLease) -> None:
        """Retire only the lease that produced a source-authentication failure."""
        now = timezone.now()
        with self._lock:
            if self._lease_is_active(lease):
                lease.state = "RETIRING"
                lease.retire_not_before_at = now
            self._save_projection()
        self._project_current_scope_frame_unready(scope)

    def _source_auth_recheck_flight(self, lease: SessionLease) -> tuple[_OuterRecheckFlight, bool]:
        now = monotonic()
        with self._lock:
            existing = self._source_auth_recheck
            if (
                existing is not None
                and existing.browser_generation == lease.browser_generation
                and (not existing.done.is_set() or existing.expires_at_monotonic > now)
            ):
                return existing, False
            flight = _OuterRecheckFlight(
                browser_generation=lease.browser_generation,
                expires_at_monotonic=now + SOURCE_AUTH_RECHECK_COOLDOWN_SECONDS,
            )
            self._source_auth_recheck = flight
            return flight, True

    def _finish_source_auth_recheck(
        self, flight: _OuterRecheckFlight, outcome: _OuterRecheckOutcome
    ) -> None:
        with self._lock:
            flight.outcome = outcome
            flight.done.set()

    def _verify_after_source_rejection(self, scope: ExecutionScope, lease: SessionLease) -> None:
        """Classify a 401 once per generation without touching a replacement lease."""
        flight, leader = self._source_auth_recheck_flight(lease)
        if leader:
            outcome = _OuterRecheckOutcome.INDETERMINATE
            with self._control_lane:
                page = None
                try:
                    if not self._lease_is_active(lease):
                        return
                    outer_session = self._outer_session(lease.home_target_id)
                    page = outer_session.page
                    if outer_session.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                        self._set_login_required()
                        outcome = _OuterRecheckOutcome.LOGIN_REQUIRED
                    elif outer_session.kind == _OuterSessionKind.VERIFIED_WITH_TOKEN:
                        if self._is_current_scope(scope):
                            self._record_outer_verification(scope)
                        self._retire_rejected_lease(scope, lease)
                        outcome = _OuterRecheckOutcome.OUTER_READY
                    else:
                        # The source identity was rejected, so its lease can no
                        # longer serve requests. A transient outer observation
                        # cannot prove the outer session remains valid, though.
                        self._retire_rejected_lease(scope, lease)
                except OuterLoginExpiredError:
                    self._set_login_required()
                    outcome = _OuterRecheckOutcome.LOGIN_REQUIRED
                except (CdpUnavailableError, GameCircleContractError):
                    pass
                finally:
                    if page is not None:
                        page.close()
                    self._finish_source_auth_recheck(flight, outcome)
        else:
            flight.done.wait(timeout=30)
            with self._control_lane:
                if flight.outcome in {
                    _OuterRecheckOutcome.OUTER_READY,
                    _OuterRecheckOutcome.INDETERMINATE,
                }:
                    self._retire_rejected_lease(scope, lease)

    def _set_login_required(self) -> None:
        self._retire_generation_leases()
        self._set_runtime(
            GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED,
            GameCircleLoginRuntimeState.FrameState.UNREADY,
            category=GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED,
            actions=(GameCircleLoginRuntimeState.Action.OPEN_LOGIN,),
            outer_observed=True,
        )

    def _set_outer_session_unready(self) -> None:
        """Project an unusable controlled HOME without falsely requiring a scan."""
        self._clear_login_pending()
        self._set_runtime(
            GameCircleLoginRuntimeState.OuterState.UNKNOWN,
            GameCircleLoginRuntimeState.FrameState.UNREADY,
            category=GameCircleLoginRuntimeState.ErrorCategory.OUTER_SESSION_UNREADY,
            actions=(
                GameCircleLoginRuntimeState.Action.CHECK_LOGIN,
                GameCircleLoginRuntimeState.Action.OPEN_LOGIN,
            ),
            outer_observed=True,
        )

    def _record_outer_verification(self, scope: ExecutionScope | None) -> None:
        """Project one verified HOME observation without reviving a broken lease."""
        row, _ = GameCircleLoginRuntimeState.objects.get_or_create(pk=1)
        now = timezone.now()
        has_current_lease = scope is not None and self._active_lease(scope) is not None
        if scope is not None and not has_current_lease:
            row.frame_state = GameCircleLoginRuntimeState.FrameState.UNREADY
            row.frame_scope_version = None
            row.error_category = GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY
            row.available_actions = [
                GameCircleLoginRuntimeState.Action.BUILD_FRAME,
                GameCircleLoginRuntimeState.Action.CHECK_LOGIN,
            ]
        row.login_state = self._legacy_login_state(GameCircleLoginRuntimeState.OuterState.OUTER_READY, row.frame_state)
        row.outer_state = GameCircleLoginRuntimeState.OuterState.OUTER_READY
        row.checked_at = now
        row.outer_verified_at = now
        row.save(
            update_fields=[
                "login_state",
                "outer_state",
                "frame_state",
                "frame_scope_version",
                "available_actions",
                "error_category",
                "checked_at",
                "outer_verified_at",
                "updated_at",
            ]
        )

    def _probe_outer_if_due(self) -> None:
        if monotonic() < self._next_outer_probe_at:
            return
        try:
            scope = current_execution_scope(persist_default=False)
        except ValueError:
            scope = None
        lease = self._active_lease(scope) if scope is not None else None
        self._next_outer_probe_at = monotonic() + OUTER_SESSION_PROBE_SECONDS
        if lease is None:
            self._probe_idle_home()
            return
        with self._control_lane:
            page = None
            try:
                if not self._lease_is_active(lease):
                    return
                outer_session = self._outer_session(lease.home_target_id)
                page = outer_session.page
                if outer_session.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                    self._set_login_required()
                elif outer_session.kind == _OuterSessionKind.VERIFIED_WITH_TOKEN:
                    self._record_outer_verification(scope)
            except OuterLoginExpiredError:
                self._set_login_required()
            except (CdpUnavailableError, GameCircleContractError):
                pass
            finally:
                if page is not None:
                    page.close()

    def _set_bootstrap_failure(
        self,
        scope: ExecutionScope | None,
        category: str,
        *,
        broken_active_lease: bool,
        outer_verified: bool = False,
    ) -> None:
        """Project failure without hiding a still-usable lease for the current scope."""
        if scope is not None:
            try:
                if current_execution_scope(persist_default=False) != scope:
                    return
            except ValueError:
                pass
        ready = not broken_active_lease and scope is not None and self._active_lease(scope) is not None
        outer_state = (
            GameCircleLoginRuntimeState.OuterState.OUTER_READY
            if outer_verified or ready
            else GameCircleLoginRuntimeState.OuterState.UNKNOWN
        )
        self._set_runtime(
            outer_state,
            GameCircleLoginRuntimeState.FrameState.READY if ready else GameCircleLoginRuntimeState.FrameState.UNREADY,
            category=GameCircleLoginRuntimeState.ErrorCategory.NONE if ready else category,
            actions=(
                (GameCircleLoginRuntimeState.Action.CHECK_LOGIN, GameCircleLoginRuntimeState.Action.KEEPALIVE)
                if ready
                else (GameCircleLoginRuntimeState.Action.BUILD_FRAME, GameCircleLoginRuntimeState.Action.CHECK_LOGIN)
            ),
            frame_scope_version=scope.scope_version if ready and scope is not None else None,
            outer_verified=outer_verified,
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
        outer_verified = False
        try:
            with self._lock:
                if self._login_pending is not None:
                    return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED)
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
                        frame_scope_version=scope.scope_version if scope is not None else None,
                    )
                    return BootstrapResult(False)
            if intent.checks_outer_only and active_lease is not None and not broken_active_lease:
                # Checking the outer session is observational: use the verified
                # lease HOME instead of creating an otherwise idle target.
                home = active_lease.home_target_id
                close_candidate_home = False
            else:
                home = self._ensure_home(browser)
            try:
                outer_session = self._outer_session(home)
            except OuterLoginExpiredError:
                if active_lease is None and self._owned_target("HOME_IDLE") == home:
                    home = ""
                    close_candidate_home = False
                    self._set_login_required()
                    return BootstrapResult(
                        False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED, terminal=True
                    )
                self._set_login_required()
                return BootstrapResult(
                    False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED, terminal=True
                )
            page = outer_session.page
            outer = outer_session.outer
            if outer_session.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                # A stable owner HOME without a token is the single shared
                # observation for manual-login recovery.  URL and page copy are
                # deliberately irrelevant: both supported entry URLs can show a
                # QR page without changing their pathname.
                if active_lease is None and self._owned_target("HOME_IDLE") == home:
                    page.close()
                    page = None
                    self._hold_candidate_for_login_recovery(home, generation)
                    home = ""
                    close_candidate_home = False
                    return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED)
                self._set_login_required()
                return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED, terminal=True)
            if outer_session.kind == _OuterSessionKind.TRANSIENT_UNREADY:
                self._set_outer_session_unready()
                return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.OUTER_SESSION_UNREADY)
            outer_verified = True
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
                    frame_scope_version=scope.scope_version if frame_ready and scope is not None else None,
                    outer_verified=True,
                )
                return BootstrapResult(False)
            if scope is None:
                close_candidate_home = False
                self._set_runtime(
                    GameCircleLoginRuntimeState.OuterState.OUTER_READY,
                    GameCircleLoginRuntimeState.FrameState.UNREADY,
                    actions=(GameCircleLoginRuntimeState.Action.CHECK_LOGIN,),
                    outer_verified=True,
                )
                return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY, terminal=True)
            token = outer.read_usable_outer_token()
            current_appid = outer_session.current_appid
            assert current_appid is not None
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
                    frame_scope_version=scope.scope_version,
                    outer_verified=True,
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
        except OuterLoginExpiredError:
            self._set_login_required()
            return BootstrapResult(False, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED, terminal=True)
        except GameSelectionError:
            category = GameCircleLoginRuntimeState.ErrorCategory.GAME_NOT_SELECTABLE
            self._set_bootstrap_failure(
                scope, category, broken_active_lease=broken_active_lease, outer_verified=outer_verified
            )
            return BootstrapResult(
                False,
                category,
                terminal=True,
            )
        except GameCircleContractError:
            category = GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY
            self._set_bootstrap_failure(
                scope, category, broken_active_lease=broken_active_lease, outer_verified=outer_verified
            )
            return BootstrapResult(False, category)
        except CdpTargetUnavailableError:
            category = GameCircleLoginRuntimeState.ErrorCategory.TARGET_UNAVAILABLE
            self._set_bootstrap_failure(
                scope, category, broken_active_lease=broken_active_lease, outer_verified=outer_verified
            )
            return BootstrapResult(False, category)
        except (SourceContractError, SourceTransportError, PluginSessionExpiredError):
            category = GameCircleLoginRuntimeState.ErrorCategory.SOURCE_VERIFICATION_FAILED
            self._set_bootstrap_failure(
                scope, category, broken_active_lease=broken_active_lease, outer_verified=outer_verified
            )
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
        """Keep the current scope recoverable without taking over manual login.

        A healthy lease is checked immediately for a missing owned target.  When
        no lease is published, repair may establish one only from an already
        verified outer-session projection.  The Bootstrap path verifies that
        fact again before it creates a candidate, so a stale projection can at
        most become an explicit login state rather than publish a lease.
        """
        if public_runtime_config().get("session_repair_enabled") != "true":
            return
        try:
            scope = current_execution_scope(persist_default=False)
        except ValueError:
            return
        active_lease = self._active_lease(scope)
        if active_lease is None:
            with self._lock:
                login_pending = self._login_pending is not None
            if login_pending or monotonic() < self._next_zero_lease_repair_at:
                return
            outer_state = GameCircleLoginRuntimeState.objects.filter(pk=1).values_list(
                "outer_state", flat=True
            ).first()
            if outer_state != GameCircleLoginRuntimeState.OuterState.OUTER_READY:
                return
            result = self.bootstrap(scope=scope, intent=BootstrapIntent.REPAIR_BROKEN_FRAME)
            if result.lease_published or self._active_lease(scope) is not None:
                self._next_zero_lease_repair_at = 0.0
            elif self._login_pending is None:
                self._next_zero_lease_repair_at = monotonic() + ZERO_LEASE_REPAIR_RETRY_SECONDS
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
        """Keep one service-owned no-token HOME while manual login may recover."""
        with self._lock:
            if self._login_pending and self._login_pending.target_id == home:
                return
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
            outer_observed=True,
        )

    def _hold_candidate_for_login_recovery(self, home: str, generation: str) -> None:
        """Promote a new background Home to the single recoverable login target."""
        self._register_login_pending(home, generation)

    def _clear_login_pending(self) -> None:
        with self._lock:
            pending = self._login_pending
            self._login_pending = None
            if pending and pending.target_id in self._owned_targets:
                self._owned_targets[pending.target_id].role = "HOME_IDLE"
            self._save_projection()

    def _resolve_login_wait(self, resolution: _LoginWaitResolution) -> None:
        """End a login wait through the sole recovery or failure transition.

        A verified completion reuses the registered HOME through normal
        Bootstrap.  All failure transitions retain the sole observable HOME so
        a later operator action can reuse it; only a browser-generation change
        removes its entire owner set.
        """
        self._clear_login_pending()
        if resolution is _LoginWaitResolution.RECOVER_SESSION:
            self.bootstrap_current_scope(intent=BootstrapIntent.ESTABLISH_FRAME)
        else:
            self._set_login_required()

    def _login_wait_is_ready_for_verification(self, pending: _LoginPending) -> bool:
        """Advance the manual-login state without probing during token propagation.

        The platform can render its post-scan HOME before its outer credential is
        usable by ``getappinfo``.  We therefore observe token availability only,
        wait a fixed bounded interval after it first appears, then let the regular
        outer-session protocol make the authentication conclusion.
        """
        page: ExplicitOuterPage | None = None
        try:
            page = ExplicitOuterPage(target_id=pending.target_id)
            if not page.wait_for_public_page():
                return False
            outer = GameCircleOuterAdapter(page)
            try:
                outer.read_usable_outer_token()
            except OuterSessionUnreadyError:
                pending.settle_not_before_at = None
                return False
        finally:
            if page is not None:
                page.close()

        now = timezone.now()
        if pending.settle_not_before_at is None:
            pending.settle_not_before_at = now + timedelta(seconds=LOGIN_COMPLETION_SETTLE_SECONDS)
            return False
        return now >= pending.settle_not_before_at

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
            self._resolve_login_wait(_LoginWaitResolution.REQUIRE_LOGIN)
            return
        try:
            browser = CdpBrowser(timeout=2)
            if self._sync_generation(browser) != pending.browser_generation or not browser.target_exists(pending.target_id):
                self._resolve_login_wait(_LoginWaitResolution.REQUIRE_LOGIN)
                return
            if not self._login_wait_is_ready_for_verification(pending):
                return
            outer_session = self._outer_session(pending.target_id)
            outer_session.page.close()
            if outer_session.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                pending.settle_not_before_at = None
                return
            if outer_session.kind == _OuterSessionKind.VERIFIED_WITH_TOKEN:
                self._resolve_login_wait(_LoginWaitResolution.RECOVER_SESSION)
        except OuterLoginExpiredError:
            self._resolve_login_wait(_LoginWaitResolution.REQUIRE_LOGIN)
        except (CdpUnavailableError, GameCircleContractError):
            return

    def open_login_page(self) -> dict[str, str]:
        """Activate the sole observable HOME without perturbing an active lease."""
        with self._control_lane:
            browser = CdpBrowser()
            generation = self._sync_generation(browser)
            try:
                scope = current_execution_scope(persist_default=False)
            except ValueError:
                scope = None
            active_lease = self._active_lease(scope) if scope is not None else None
            target = self._observable_home(browser)
            browser.activate_target(target)
            if active_lease is not None and target == active_lease.home_target_id:
                return {"status": "CONTROLLED_SESSION_PAGE_OPENED"}
            with self._lock:
                if self._login_pending and self._login_pending.target_id == target:
                    return {"status": "CONTROLLED_SESSION_PAGE_OPENED"}
            page: ExplicitOuterPage | None = None
            try:
                observed = self._outer_session(target)
                page = observed.page
                if observed.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                    self._hold_candidate_for_login_recovery(target, generation)
                    return {"status": "CONTROLLED_SESSION_LOGIN_PENDING"}
                if observed.kind == _OuterSessionKind.VERIFIED_WITH_TOKEN:
                    self._record_outer_verification(scope)
                return {"status": "CONTROLLED_SESSION_PAGE_OPENED"}
            except OuterLoginExpiredError:
                self._set_login_required()
                return {"status": "CONTROLLED_SESSION_LOGIN_REQUIRED"}
            except GameCircleContractError:
                # A newly activated page can still be replacing its document.
                # Opening it remains useful to the operator; maintenance will
                # classify it once the target reaches a stable state.
                return {"status": "CONTROLLED_SESSION_PAGE_OPENED"}
            finally:
                if page is not None:
                    page.close()

    def _probe_idle_home(self) -> None:
        """Classify an existing owner HOME when no scope has an active lease.

        This does not create a target.  It lets startup Bootstrap, later
        low-frequency observation and the manual entry share the same stable
        no-token transition without turning document replacement into a scan.
        """
        with self._control_lane:
            page: ExplicitOuterPage | None = None
            try:
                browser = CdpBrowser(timeout=2)
                generation = self._sync_generation(browser)
                home = self._owned_target("HOME_IDLE")
                if home is None or not browser.target_exists(home):
                    return
                observed = self._outer_session(home)
                page = observed.page
                if observed.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                    self._hold_candidate_for_login_recovery(home, generation)
                elif observed.kind == _OuterSessionKind.VERIFIED_WITH_TOKEN:
                    try:
                        scope = current_execution_scope(persist_default=False)
                    except ValueError:
                        scope = None
                    self._record_outer_verification(scope)
            except OuterLoginExpiredError:
                self._set_login_required()
            except (CdpUnavailableError, GameCircleContractError):
                return
            finally:
                if page is not None:
                    page.close()

    # -- fixed IPC surface ---------------------------------------------------------------

    def _control_result(self, *, completed_status: str, result: BootstrapResult) -> dict[str, Any]:
        """Return the operator-facing outcome of a fixed session control.

        Bootstrap deliberately turns a conflicting control request into a
        no-op while a manual login wait owns the HOME. The IPC receipt must
        preserve that distinction: callers must not present a blocked action
        as a completed check or Frame establishment.
        """
        runtime = self.runtime_status()
        if runtime["outer_state"] == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING:
            status = "LOGIN_PENDING"
        elif result.category != GameCircleLoginRuntimeState.ErrorCategory.NONE:
            status = "CONTROL_NOT_COMPLETED"
        else:
            status = completed_status
        return {"status": status, "runtime": runtime}

    def handle_ipc(self, request: IpcRequest) -> dict[str, Any]:
        if request.method == "OPEN_CONTROLLED_LOGIN":
            try:
                return self.open_login_page()
            except CdpBrowserUnavailableError as exc:
                raise GameCircleIpcError("CHROME_UNAVAILABLE", "受控 Chrome 未启动") from exc
            except CdpUnavailableError as exc:
                raise GameCircleIpcError("LOGIN_PAGE_FAILED", "受控 Chrome 无法打开登录页") from exc
        if request.method == "CHECK_LOGIN":
            result = self.bootstrap_current_scope(intent=BootstrapIntent.CHECK_OUTER_SESSION)
            return self._control_result(completed_status="CHECK_COMPLETED", result=result)
        if request.method == "BUILD_CURRENT_GAME_FRAME":
            result = self.bootstrap_current_scope(intent=BootstrapIntent.ESTABLISH_FRAME)
            return self._control_result(completed_status="BOOTSTRAP_COMPLETED", result=result)
        if request.method == "GET_RUNTIME_STATUS":
            return {"runtime": self.runtime_status()}
        if request.method == "LIST_MANAGED_GAMES":
            return {"items": self.list_managed_games()}
        if request.scope is None:
            raise GameCircleIpcError("INVALID_SCOPE", "游戏圈操作范围无效")
        return self._execute_source_request(request)

    def _execute_source_request(self, request: IpcRequest) -> dict[str, Any]:
        requested_timeout = (
            request.params.get("timeout_seconds", 30)
            if request.method in {"POSTS_GET_METRICS", "POSTS_GET_AUTHOR_DETAIL"}
            else 30
        )
        try:
            timeout_seconds = float(requested_timeout)
        except (TypeError, ValueError) as exc:
            raise GameCircleIpcError("INVALID_REQUEST", "来源请求超时参数无效") from exc
        lease: SessionLease | None = None
        try:
            with self.access(request.scope, timeout_seconds=timeout_seconds) as access:
                lease = access.lease
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
                        if request.method == "POSTS_GET_AUTHOR_DETAIL":
                            openid = request.params.get("openid")
                            if not isinstance(openid, str) or not openid or len(openid) > 255:
                                raise SourceContractError("作者用户 ID 无效")
                            return {
                                "payload": client.get_author_detail_with_timeout(
                                    openid, timeout_seconds=timeout_seconds
                                )
                            }
                        if request.method == "POSTS_LIST_BLOCKED_USERS":
                            page = request.params.get("page")
                            page_size = request.params.get("page_size")
                            if not isinstance(page, int) or page < 0:
                                raise SourceContractError("禁言名单页码无效")
                            if not isinstance(page_size, int) or not 1 <= page_size <= 500:
                                raise SourceContractError("禁言名单分页大小无效")
                            return client.list_blocked_users(page=page, page_size=page_size)
                        if request.method == "POSTS_OPERATION":
                            payload = request.params.get("payload")
                            if not request.operation_id or not isinstance(payload, dict):
                                raise SourceContractError("人工操作请求无效")
                            topic_id = request.params.get("topic_id")
                            if topic_id is not None and not isinstance(topic_id, int):
                                raise SourceContractError("人工操作帖子 ID 无效")
                            return {"payload": client.perform_confirmed_operation(topic_id=topic_id, action=str(request.params["action"]), payload=payload)}
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
        except SourceAuthenticationRejectedError as exc:
            if lease is not None:
                self._verify_after_source_rejection(request.scope, lease)
            raise GameCircleIpcError("SESSION_UNAVAILABLE", "游戏圈会话已失效，请重新登录或建立 Frame") from exc
        except PluginSessionExpiredError as exc:
            if lease is not None:
                with self._control_lane:
                    self._retire_rejected_lease(request.scope, lease)
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
                generation = self._sync_generation(browser)
                home = self._ensure_home(browser)
                outer_session = self._outer_session(home)
                try:
                    if outer_session.kind == _OuterSessionKind.NO_USABLE_TOKEN:
                        self._hold_candidate_for_login_recovery(home, generation)
                        raise GameCircleIpcError("SESSION_UNAVAILABLE", "外层会话需要人工扫码")
                    if outer_session.kind == _OuterSessionKind.TRANSIENT_UNREADY:
                        self._set_outer_session_unready()
                        raise GameCircleIpcError("SESSION_UNAVAILABLE", "外层会话暂未就绪")
                    return outer_session.outer.list_managed_games()
                finally:
                    outer_session.page.close()
            except GameCircleIpcError:
                raise
            except OuterLoginExpiredError as exc:
                self._set_login_required()
                raise GameCircleIpcError("SESSION_UNAVAILABLE", "外层登录态已过期，请重新扫码") from exc
            except CdpUnavailableError as exc:
                raise GameCircleIpcError("SOURCE_UNAVAILABLE", "无法读取可管理游戏目录") from exc
            except GameCircleContractError as exc:
                raise GameCircleIpcError("SOURCE_CONTRACT", "原平台响应不符合已验证契约") from exc
