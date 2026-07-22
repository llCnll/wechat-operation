"""The sole owner of controlled Chrome game-circle sessions and credentials."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from secrets import choice
from threading import Event, RLock
from time import monotonic, sleep
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
    open_controlled_login_page,
)
from config.game_circle_adapter import ExplicitOuterPage, GameCircleContractError, GameCircleOuterAdapter, OuterSessionUnreadyError, plugin_redirect_url
from config.game_circle_ipc import GameCircleIpcError, IpcRequest, WriteResultUnknownError
from config.game_circle_session import RefreshDirectFailure, RefreshMode, validate_refresh_preconditions
from config.settings import (
    GAME_CIRCLE_MAX_CONCURRENT_ACCESSES,
    GAME_CIRCLE_MAX_REQUESTS_PER_SECOND,
    GAME_CIRCLE_SESSION_RETIRE_SECONDS,
    RUNTIME_DIR,
)
from jobs.adapters import CandidatePostDTO, PluginSessionExpiredError, SourceBusinessDeletedError, SourceContractError, SourceTransportError
from jobs.game_circle_refresh import REFRESH_RECOVER, enqueue_session_refresh
from jobs.services import JobDeferred, JobTerminalError
from posts.game_catalog import ExecutionScope, resolve_execution_scope
from posts.models import GameCircleLoginRuntimeState
from posts.source_client import CdpGameCircleClient
from reporting.source_client import CdpGameCircleMetricClient
from service_supervisor.runtime import RuntimeStore


@dataclass
class OwnedTarget:
    target_id: str
    role: str
    owner_epoch: str
    lease_id: str = ""
    appid: str = ""


@dataclass
class SessionLease:
    lease_id: str
    appid: str
    browser_generation: str
    home_target_id: str
    plugin_target_id: str
    plugin_generation: int
    source_token: str
    issued_at: datetime
    state: str = "ACTIVE"
    retire_not_before_at: datetime | None = None
    active_accesses: dict[str, "_AccessState"] = field(default_factory=dict)


@dataclass
class _AccessState:
    started_at: datetime
    deadline_at: datetime
    deadline_monotonic: float
    cancelled: bool = False
    frames: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class SessionAccess:
    access_id: str
    lease: SessionLease
    state: _AccessState


ORPHAN_CLEANUP_ROLES = frozenset(
    {
        "HOME_CANDIDATE",
        "PLUGIN_CANDIDATE",
        "LEASE_HOME",
        "LEASE_PLUGIN",
        "PROBE_CANDIDATE",
        "CATALOG_CANDIDATE",
    }
)


class _DeadlineBoundFrame:
    """Bind one CDP frame to the owning access deadline and cancellation signal."""

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

    def _call_with_deadline(self, method, *args, **kwargs):
        remaining = self._service._register_frame_for_access(self._access, self._frame)
        original_timeout = self._frame.timeout
        self._frame.timeout = min(original_timeout, remaining)
        try:
            return method(*args, **kwargs)
        finally:
            self._frame.timeout = original_timeout

    def evaluate(self, expression: str) -> Any:
        return self._call_with_deadline(self._frame.evaluate, expression)

    def has_gamemp_iframe(self) -> bool:
        return self._call_with_deadline(self._frame.has_gamemp_iframe)

    def close(self) -> None:
        try:
            self._frame.close()
        finally:
            self._service._unregister_frame_for_access(self._access, self._frame)


class GameCircleSessionService:
    """Single-process Chrome/session owner; its runtime projection contains no credentials."""

    def __init__(self, *, runtime_store: RuntimeStore | None = None) -> None:
        self.owner_epoch = uuid4().hex
        self._store = runtime_store or RuntimeStore(RUNTIME_DIR)
        self._lock = RLock()
        self._browser_generation = ""
        self._leases: dict[str, list[SessionLease]] = {}
        self._owned_targets: dict[str, OwnedTarget] = {}
        self._orphaned_browser_generation, self._orphaned_targets = self._load_orphaned_targets()
        self._refreshing: dict[str, Event] = {}
        self._binding: dict[tuple[str, str], Event] = {}
        self.max_concurrent_accesses = GAME_CIRCLE_MAX_CONCURRENT_ACCESSES
        self.max_requests_per_second = GAME_CIRCLE_MAX_REQUESTS_PER_SECOND
        self._stopping = False
        # A service owner never inherits an in-memory source identity from its
        # predecessor. Publish that fact immediately so Web cannot mistake a
        # stale runtime projection for a usable Frame.
        self._save_projection()

    def stop_accepting(self) -> None:
        with self._lock:
            self._stopping = True
            active = [state for leases in self._leases.values() for lease in leases for state in lease.active_accesses.values()]
        for state in active:
            self._cancel_access_state(state)
        self._close_owned_targets_for_shutdown()
        with self._lock:
            self._leases.clear()
            self._save_projection()

    def _load_orphaned_targets(self) -> tuple[str, list[OwnedTarget]]:
        """Read only the prior owner's explicit target registry, never the browser tab list."""
        try:
            projection = self._store.read_json(self._store.game_circle_session_path, {})
        except OSError:
            return "", []
        if not isinstance(projection, dict):
            return "", []
        orphan_generation = projection.get("orphaned_browser_generation")
        orphan_targets = projection.get("orphaned_targets")
        generation = orphan_generation if isinstance(orphan_generation, str) else projection.get("browser_generation")
        raw_targets = orphan_targets if isinstance(orphan_targets, list) else projection.get("targets")
        if not isinstance(generation, str) or not generation or not isinstance(raw_targets, list):
            return "", []
        targets: list[OwnedTarget] = []
        seen: set[str] = set()
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            target_id, role, owner_epoch = item.get("target_id"), item.get("role"), item.get("owner_epoch")
            if (
                not isinstance(target_id, str)
                or not target_id
                or target_id in seen
                or role not in ORPHAN_CLEANUP_ROLES
                or not isinstance(owner_epoch, str)
                or not owner_epoch
            ):
                continue
            seen.add(target_id)
            targets.append(OwnedTarget(target_id, role, owner_epoch))
        return generation, targets

    def _close_owned_targets_for_shutdown(self) -> None:
        """Best-effort close only this service owner's session targets; keep manual login open."""
        try:
            browser = CdpBrowser()
        except CdpUnavailableError:
            return
        with self._lock:
            target_ids = [
                owned.target_id
                for owned in self._owned_targets.values()
                if owned.owner_epoch == self.owner_epoch and owned.role in ORPHAN_CLEANUP_ROLES
            ]
        for target_id in target_ids:
            self._safe_close(browser, target_id)

    def _cleanup_orphaned_targets(self, browser: CdpBrowser, generation: str) -> None:
        """Clean explicit prior-owner targets only when they belong to this Chrome process."""
        with self._lock:
            orphan_generation = self._orphaned_browser_generation
            targets = list(self._orphaned_targets)
        if not targets:
            return
        if orphan_generation != generation:
            with self._lock:
                self._orphaned_browser_generation = ""
                self._orphaned_targets = []
                self._save_projection()
            return
        remaining: list[OwnedTarget] = []
        for owned in targets:
            try:
                browser.close_target(owned.target_id)
            except CdpUnavailableError:
                try:
                    exists = browser.target_exists(owned.target_id)
                except CdpUnavailableError:
                    exists = True
                if exists:
                    remaining.append(owned)
        with self._lock:
            self._orphaned_targets = remaining
            self._orphaned_browser_generation = generation if remaining else ""
            self._save_projection()

    def _cancel_access_state(self, state: _AccessState) -> None:
        with self._lock:
            if state.cancelled:
                return
            state.cancelled = True
            frames = list(state.frames)
            state.frames.clear()
        for frame in frames:
            try:
                frame.close()
            except Exception:
                pass

    def _register_frame_for_access(self, access: SessionAccess, frame: CdpGameFrame) -> float:
        with self._lock:
            state = access.state
            remaining = state.deadline_monotonic - monotonic()
            if state.cancelled or remaining <= 0:
                raise SourceTransportError("游戏圈来源请求已超时或被取消")
            if frame not in state.frames:
                state.frames.append(frame)
            return remaining

    def _unregister_frame_for_access(self, access: SessionAccess, frame: CdpGameFrame) -> None:
        with self._lock:
            try:
                access.state.frames.remove(frame)
            except ValueError:
                pass

    def _save_projection(self) -> None:
        with self._lock:
            projection = {
                "owner_epoch": self.owner_epoch,
                "browser_generation": self._browser_generation,
                "leases": [
                    {
                        "lease_id": lease.lease_id,
                        "appid": lease.appid,
                        "browser_generation": lease.browser_generation,
                        "plugin_generation": lease.plugin_generation,
                        "state": lease.state,
                        "issued_at": lease.issued_at.isoformat(),
                        "retire_not_before_at": lease.retire_not_before_at.isoformat() if lease.retire_not_before_at else None,
                        "active_request_count": len(lease.active_accesses),
                    }
                    for leases in self._leases.values()
                    for lease in leases
                ],
                "targets": [
                    {
                        "target_id": owned.target_id,
                        "role": owned.role,
                        "owner_epoch": owned.owner_epoch,
                        "lease_id": owned.lease_id,
                        "appid": owned.appid,
                    }
                    for owned in self._owned_targets.values()
                ],
            }
            if self._orphaned_targets:
                projection["orphaned_browser_generation"] = self._orphaned_browser_generation
                projection["orphaned_targets"] = [
                    {"target_id": owned.target_id, "role": owned.role, "owner_epoch": owned.owner_epoch}
                    for owned in self._orphaned_targets
                ]
            self._store.write_json(self._store.game_circle_session_path, projection)

    def _sync_generation(self, browser: CdpBrowser) -> str:
        generation = browser.browser_generation()
        self._cleanup_orphaned_targets(browser, generation)
        with self._lock:
            if self._browser_generation and self._browser_generation != generation:
                self._leases.clear()
                # The browser process has changed, therefore every former target is gone.
                self._owned_targets.clear()
            self._browser_generation = generation
            self._save_projection()
        return generation

    def _register_owned(self, target_id: str, role: str, *, lease_id: str = "", appid: str = "") -> None:
        if not target_id:
            raise SourceContractError("受控会话 target 无效")
        with self._lock:
            self._owned_targets[target_id] = OwnedTarget(target_id, role, self.owner_epoch, lease_id, appid)
            self._save_projection()

    def _safe_close(self, browser: CdpBrowser, target_id: str) -> bool:
        """Close one owned target and retain its registration until closure is confirmed."""
        with self._lock:
            owned = self._owned_targets.get(target_id)
            if owned is None or owned.owner_epoch != self.owner_epoch:
                return True
        try:
            browser.close_target(target_id)
        except CdpUnavailableError:
            try:
                if not browser.target_exists(target_id):
                    with self._lock:
                        self._owned_targets.pop(target_id, None)
                        self._save_projection()
                    return True
            except CdpUnavailableError:
                pass
            return False
        else:
            with self._lock:
                self._owned_targets.pop(target_id, None)
                self._save_projection()
            return True

    def _reclaim_replaced_login_pages(self, browser: CdpBrowser) -> None:
        """Close only this owner's login pages after a verified lease replaces them."""
        with self._lock:
            target_ids = [
                owned.target_id
                for owned in self._owned_targets.values()
                if owned.owner_epoch == self.owner_epoch and owned.role == "LOGIN_PAGE"
            ]
        for target_id in target_ids:
            self._safe_close(browser, target_id)

    def _active_lease(self, scope: ExecutionScope) -> SessionLease | None:
        with self._lock:
            for lease in self._leases.get(scope.target_appid, []):
                if lease.state == "ACTIVE" and lease.browser_generation == self._browser_generation:
                    return lease
        return None

    @contextmanager
    def access(self, scope: ExecutionScope, *, timeout_seconds: float = 30) -> Iterator[SessionAccess]:
        """Atomically pin the current ACTIVE lease; all exits release the same access id."""
        if self._stopping:
            raise GameCircleIpcError("SERVICE_UNAVAILABLE", "游戏圈会话服务正在停止")
        lease = self._active_lease(scope)
        if lease is None:
            enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=scope)
            raise JobDeferred("游戏圈会话正在恢复", delay_seconds=5)
        access_id = uuid4().hex
        now = timezone.now()
        state = _AccessState(
            started_at=now,
            deadline_at=now + timedelta(seconds=timeout_seconds),
            deadline_monotonic=monotonic() + timeout_seconds,
        )
        with self._lock:
            if lease.state != "ACTIVE":
                raise JobDeferred("游戏圈会话正在切换", delay_seconds=5)
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
                self._reclaim_retiring_locked()
                self._save_projection()

    def _reclaim_retiring_locked(self) -> None:
        now = timezone.now()
        browser = None
        for appid, leases in list(self._leases.items()):
            retained: list[SessionLease] = []
            for lease in leases:
                reclaimable = lease.state == "RETIRING" and not lease.active_accesses and lease.retire_not_before_at and lease.retire_not_before_at <= now
                if not reclaimable:
                    retained.append(lease)
                    continue
                if browser is None:
                    try:
                        browser = CdpBrowser()
                    except Exception:
                        retained.append(lease)
                        continue
                plugin_closed = self._safe_close(browser, lease.plugin_target_id)
                home_closed = self._safe_close(browser, lease.home_target_id)
                if not (plugin_closed and home_closed):
                    retained.append(lease)
            if retained:
                self._leases[appid] = retained
            else:
                self._leases.pop(appid, None)

    def _rebind_active(self, lease: SessionLease, access: SessionAccess | None = None) -> str:
        """At most one fresh binding per active lease; retiring leases never bind again."""
        key = (lease.appid, lease.lease_id)
        leader = False
        with self._lock:
            if lease.state != "ACTIVE" or self._active_lease(ExecutionScope(lease.appid, 1)) is not lease:
                raise PluginSessionExpiredError("退役游戏圈会话不能重新绑定")
            if access is None:
                raise PluginSessionExpiredError("游戏圈身份重绑缺少请求上下文")
            waiting = self._binding.get(key)
            if waiting is None:
                waiting = Event()
                self._binding[key] = waiting
                leader = True
        if not leader:
            waiting.wait(timeout=max(0, access.state.deadline_monotonic - monotonic()))
            if lease.state == "ACTIVE" and lease.source_token:
                return lease.source_token
            raise PluginSessionExpiredError("游戏圈身份重新绑定失败")
        try:
            browser = CdpBrowser()
            if self._sync_generation(browser) != lease.browser_generation:
                raise PluginSessionExpiredError("浏览器会话已更换")
            raw_frame = CdpGameFrame(target_id=lease.plugin_target_id)
            frame = _DeadlineBoundFrame(self, access, raw_frame)
            try:
                if not frame.has_gamemp_iframe():
                    raise PluginSessionExpiredError("游戏圈插件已失效")
                token = bind_game_identity(lease.appid, frame=frame)
            finally:
                frame.close()
            with self._lock:
                if lease.state != "ACTIVE":
                    raise PluginSessionExpiredError("游戏圈会话已退役")
                lease.source_token = token
            return token
        finally:
            with self._lock:
                done = self._binding.pop(key, None)
                if done:
                    done.set()

    def _post_client(self, access: SessionAccess) -> CdpGameCircleClient:
        frame = _DeadlineBoundFrame(self, access, CdpGameFrame(target_id=access.lease.plugin_target_id))
        return CdpGameCircleClient(
            appid=access.lease.appid,
            frame=frame,
            identity_token=access.lease.source_token,
            rebind_identity=lambda: self._rebind_active(access.lease, access),
        )

    def _metric_client(self, access: SessionAccess) -> CdpGameCircleMetricClient:
        frame = _DeadlineBoundFrame(self, access, CdpGameFrame(target_id=access.lease.plugin_target_id))
        return CdpGameCircleMetricClient(
            appid=access.lease.appid,
            frame=frame,
            identity_token=access.lease.source_token,
            rebind_identity=lambda: self._rebind_active(access.lease, access),
        )

    def _publish_lease(self, *, scope: ExecutionScope, generation: str, home: str, plugin: str, source_token: str) -> None:
        with self._lock:
            leases = self._leases.setdefault(scope.target_appid, [])
            next_generation = max((lease.plugin_generation for lease in leases), default=0) + 1
            now = timezone.now()
            for lease in leases:
                if lease.state == "ACTIVE":
                    lease.state = "RETIRING"
                    lease.retire_not_before_at = now + timedelta(seconds=GAME_CIRCLE_SESSION_RETIRE_SECONDS)
                    for target_id in (lease.home_target_id, lease.plugin_target_id):
                        owned = self._owned_targets.get(target_id)
                        if owned:
                            owned.role = "LEASE_HOME" if target_id == lease.home_target_id else "LEASE_PLUGIN"
            lease = SessionLease(uuid4().hex, scope.target_appid, generation, home, plugin, next_generation, source_token, now)
            leases.insert(0, lease)
            self._register_owned(home, "LEASE_HOME", lease_id=lease.lease_id, appid=scope.target_appid)
            self._register_owned(plugin, "LEASE_PLUGIN", lease_id=lease.lease_id, appid=scope.target_appid)
            self._save_projection()

    @staticmethod
    def _require_outer_token(outer: GameCircleOuterAdapter) -> str:
        deadline = monotonic() + 10
        while True:
            if outer.scan_required():
                raise RefreshDirectFailure("需要人工扫码")
            try:
                return outer.read_outer_token()
            except OuterSessionUnreadyError as exc:
                if monotonic() >= deadline:
                    raise RefreshDirectFailure("需要人工扫码") from exc
                # The HOME target has been created but is still redirecting.  This
                # waits for a verified page condition instead of sleeping blindly.
                sleep(min(0.2, max(0.0, deadline - monotonic())))

    @classmethod
    def _switch_and_confirm(cls, outer: GameCircleOuterAdapter, target_appid: str) -> str:
        """Switch one owned HOME and return its token only after the readback."""
        outer.switch_to_game(target_appid)
        outer.reload_after_game_switch()
        outer.wait_for_current_game(target_appid)
        return cls._require_outer_token(outer)

    @staticmethod
    def _resolve_keepalive_intermediate(outer: GameCircleOuterAdapter, target_appid: str) -> str:
        """Choose a permitted non-target game without trusting upstream ordering."""
        candidates = sorted({appid for appid in outer.list_switchable_game_appids() if appid != target_appid})
        if not candidates:
            raise GameCircleContractError("当前受控账号没有可用的保活中转游戏")
        return choice(candidates)

    def _prepare_recover_context(
        self, outer: GameCircleOuterAdapter, scope: ExecutionScope, current_appid: str, token: str
    ) -> str:
        if current_appid == scope.target_appid:
            return token
        return self._switch_and_confirm(outer, scope.target_appid)

    def _prepare_keepalive_context(
        self, outer: GameCircleOuterAdapter, scope: ExecutionScope, current_appid: str, token: str
    ) -> str:
        if current_appid != scope.target_appid:
            return self._switch_and_confirm(outer, scope.target_appid)
        intermediate = self._resolve_keepalive_intermediate(outer, scope.target_appid)
        self._switch_and_confirm(outer, intermediate)
        return self._switch_and_confirm(outer, scope.target_appid)

    def refresh(self, job) -> None:
        """Build a candidate pair, verify it read-only, then atomically publish a lease."""
        try:
            scope = resolve_execution_scope(job)
            mode = RefreshMode(str(job.parameter_summary.get("refresh_mode", "")))
        except (TypeError, ValueError) as exc:
            raise JobTerminalError("会话刷新任务范围或模式无效") from exc
        with self._lock:
            existing = self._refreshing.get(scope.target_appid)
            if existing is None:
                existing = Event()
                self._refreshing[scope.target_appid] = existing
                leader = True
            else:
                leader = False
        if not leader:
            raise JobDeferred("同一游戏会话正在刷新", delay_seconds=5)
        browser = CdpBrowser()
        home = plugin = ""
        try:
            generation = self._sync_generation(browser)
            home = browser.create_target("https://mp.weixin.qq.com/")
            self._register_owned(home, "HOME_CANDIDATE", appid=scope.target_appid)
            outer_page = ExplicitOuterPage(target_id=home)
            outer = GameCircleOuterAdapter(outer_page)
            try:
                if not outer_page.wait_for_public_page():
                    raise CdpTargetUnavailableError("候选 HOME 未就绪")
                token = self._require_outer_token(outer)
                validate_refresh_preconditions(scan_required=False, has_outer_token=bool(token))
                current_appid = outer.wait_for_readable_current_game()
                if mode is RefreshMode.RECOVER:
                    token = self._prepare_recover_context(outer, scope, current_appid, token)
                else:
                    token = self._prepare_keepalive_context(outer, scope, current_appid, token)
            finally:
                outer_page.close()
            plugin = browser.create_target(plugin_redirect_url(token))
            self._register_owned(plugin, "PLUGIN_CANDIDATE", appid=scope.target_appid)
            frame = CdpGameFrame(target_id=plugin)
            try:
                if not frame.wait_for_gamemp_iframe():
                    raise CdpTargetUnavailableError("候选插件缺少游戏圈 iframe")
                token = bind_game_identity(scope.target_appid, frame=frame)
                verifier = CdpGameCircleClient(appid=scope.target_appid, frame=frame, identity_token=token)
                verifier.list_candidates(page=0, page_size=1, start_time=0, end_time=int(timezone.now().timestamp()))
            finally:
                frame.close()
            self._publish_lease(scope=scope, generation=generation, home=home, plugin=plugin, source_token=token)
            self._reclaim_replaced_login_pages(browser)
            self._set_login_state(GameCircleLoginRuntimeState.LoginState.READY, GameCircleLoginRuntimeState.ErrorCategory.NONE)
            home = plugin = ""
        except RefreshDirectFailure as exc:
            self._set_login_state(GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED)
            raise JobTerminalError(str(exc)) from exc
        except GameCircleContractError:
            # A verified upstream contract failure is retryable and does not answer
            # whether the independent outer browser login is available.
            raise
        except CdpTargetUnavailableError as exc:
            # Browser CDP is reachable, but this candidate target or iframe is not
            # ready/usable. Keep the current login projection and use normal retry.
            raise RuntimeError("游戏圈会话候选页或插件未就绪") from exc
        except CdpBrowserUnavailableError as exc:
            self._set_login_state(GameCircleLoginRuntimeState.LoginState.UNAVAILABLE, GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE)
            raise RuntimeError("受控 Chrome 不可用") from exc
        except CdpUnavailableError as exc:
            # Future CDP subtypes must not silently become a browser-outage state.
            raise RuntimeError("游戏圈会话 CDP 协议异常") from exc
        finally:
            if plugin:
                self._safe_close(browser, plugin)
            if home:
                self._safe_close(browser, home)
            with self._lock:
                done = self._refreshing.pop(scope.target_appid, None)
                if done:
                    done.set()

    def _set_login_state(self, state: str, category: str) -> None:
        now = timezone.now()
        row, _ = GameCircleLoginRuntimeState.objects.get_or_create(pk=1)
        row.login_state = state
        row.error_category = category
        row.checked_at = now
        if state == GameCircleLoginRuntimeState.LoginState.READY:
            row.last_ready_at = now
        row.save(update_fields=["login_state", "error_category", "checked_at", "last_ready_at", "updated_at"])

    def probe_login(self, job=None) -> None:
        browser = CdpBrowser(timeout=2)
        target = ""
        try:
            self._sync_generation(browser)
            target = browser.create_target("https://mp.weixin.qq.com/")
            self._register_owned(target, "PROBE_CANDIDATE")
            page = ExplicitOuterPage(target_id=target, timeout=2)
            outer = GameCircleOuterAdapter(page)
            try:
                if not page.wait_for_public_page():
                    raise CdpTargetUnavailableError("登录探测 HOME 未就绪")
                try:
                    self._require_outer_token(outer)
                except RefreshDirectFailure:
                    if outer.scan_required():
                        self._set_login_state(GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED, GameCircleLoginRuntimeState.ErrorCategory.SCAN_REQUIRED)
                    else:
                        self._set_login_state(GameCircleLoginRuntimeState.LoginState.UNKNOWN, GameCircleLoginRuntimeState.ErrorCategory.OUTER_SESSION_UNREADY)
                else:
                    self._set_login_state(GameCircleLoginRuntimeState.LoginState.READY, GameCircleLoginRuntimeState.ErrorCategory.NONE)
            finally:
                page.close()
        except CdpBrowserUnavailableError:
            self._set_login_state(GameCircleLoginRuntimeState.LoginState.UNAVAILABLE, GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE)
        except CdpUnavailableError:
            self._set_login_state(GameCircleLoginRuntimeState.LoginState.UNKNOWN, GameCircleLoginRuntimeState.ErrorCategory.PROBE_FAILED)
        except Exception:
            self._set_login_state(GameCircleLoginRuntimeState.LoginState.UNKNOWN, GameCircleLoginRuntimeState.ErrorCategory.PROBE_FAILED)
        finally:
            if target:
                self._safe_close(browser, target)

    def handle_ipc(self, request: IpcRequest) -> dict[str, Any]:
        if request.method == "OPEN_CONTROLLED_LOGIN":
            try:
                target_id = open_controlled_login_page()
            except CdpBrowserUnavailableError as exc:
                raise GameCircleIpcError("CHROME_UNAVAILABLE", "受控 Chrome 未启动或无法创建登录页") from exc
            except CdpUnavailableError as exc:
                raise GameCircleIpcError("LOGIN_PAGE_FAILED", "受控 Chrome 无法打开登录页") from exc
            self._register_owned(target_id, "LOGIN_PAGE")
            return {"status": "LOGIN_PAGE_OPENED"}
        if request.method == "LOGIN_PROBE":
            self.probe_login()
            return {"status": "PROBE_COMPLETED"}
        if request.method == "LIST_MANAGED_GAMES":
            return {"items": self.list_managed_games()}
        if request.scope is None:
            raise GameCircleIpcError("INVALID_SCOPE", "游戏圈操作范围无效")
        try:
            return self._execute_source_request(request)
        except JobDeferred as exc:
            raise GameCircleIpcError("SESSION_RECOVERING", "游戏圈会话正在恢复，请稍后重新确认") from exc
        except PluginSessionExpiredError as exc:
            enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=request.scope)
            raise GameCircleIpcError("SESSION_RECOVERING", "游戏圈会话正在恢复，请稍后重新确认") from exc
        except SourceBusinessDeletedError as exc:
            raise GameCircleIpcError("SOURCE_DELETED", "原平台帖子已从管理队列删除") from exc
        except SourceContractError as exc:
            messages = {
                "INVALID_EVALUATION_RESULT": "来源 CDP 返回结构无效",
                "NON_JSON_RESPONSE": "来源详情响应不是 JSON",
            }
            raise GameCircleIpcError(
                "SOURCE_CONTRACT", messages.get(exc.safe_reason, "原平台响应不符合已验证契约")
            ) from exc
        except SourceTransportError as exc:
            if request.method == "POSTS_OPERATION":
                raise WriteResultUnknownError() from exc
            raise GameCircleIpcError("SOURCE_UNAVAILABLE", "原平台来源请求暂不可用") from exc

    def _execute_source_request(self, request: IpcRequest) -> dict[str, Any]:
        requested_timeout = (
            request.params.get("timeout_seconds", 30)
            if request.method == "POSTS_GET_METRICS"
            else 30
        )
        try:
            timeout_seconds = float(requested_timeout)
        except (TypeError, ValueError) as exc:
            raise SourceContractError("来源请求超时参数无效") from exc
        if not 0 < timeout_seconds <= 30:
            raise SourceContractError("来源请求超时参数无效")
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
                        action = str(request.params["action"])
                        payload = request.params.get("payload")
                        if not request.operation_id or not isinstance(payload, dict):
                            raise SourceContractError("人工操作请求无效")
                        return {"payload": client.perform_confirmed_operation(topic_id=int(request.params["topic_id"]), action=action, payload=payload)}
                finally:
                    client.close()
            client = self._metric_client(access)
            try:
                methods = {
                    "METRIC_CIRCLE_FANS": client.circle_fans,
                    "METRIC_CONTENT_FANS": client.content_fans,
                    "METRIC_OPERATION_EFFECT": client.operation_effect,
                    "METRIC_GROUP_CONTENT_ANALYSIS": client.group_content_analysis,
                }
                method = methods.get(request.method)
                if method is None:
                    raise SourceContractError("不支持的游戏圈会话方法")
                return {"payload": method(**request.params)}
            finally:
                client.close()

    def list_managed_games(self) -> list[dict[str, str]]:
        """Synchronize the catalog through a registered, short-lived owned target."""
        browser = CdpBrowser()
        target = ""
        page = None
        try:
            self._sync_generation(browser)
            target = browser.create_target("https://mp.weixin.qq.com/")
            self._register_owned(target, "CATALOG_CANDIDATE")
            page = ExplicitOuterPage(target_id=target)
            if not page.wait_for_public_page():
                raise CdpTargetUnavailableError("目录同步 HOME 未就绪")
            return GameCircleOuterAdapter(page).list_managed_games()
        except CdpUnavailableError as exc:
            raise GameCircleIpcError("SOURCE_UNAVAILABLE", "无法读取可管理游戏目录") from exc
        except GameCircleContractError as exc:
            raise GameCircleIpcError("SOURCE_CONTRACT", "原平台响应不符合已验证契约") from exc
        finally:
            if page is not None:
                page.close()
            if target:
                self._safe_close(browser, target)
