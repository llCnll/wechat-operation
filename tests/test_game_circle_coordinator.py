from datetime import timedelta
from contextlib import contextmanager

import pytest
from django.utils import timezone

from config.cdp import CdpTargetUnavailableError
from config.game_circle_ipc import GameCircleIpcError, IpcRequest
from config.game_circle_service import GameCircleSessionService, OwnedTarget, SessionLease, _DeadlineBoundFrame
from jobs.adapters import PluginSessionExpiredError, SourceContractError, SourceTransportError
from jobs.game_circle_refresh import REFRESH_RECOVER
from posts.game_catalog import ExecutionScope


def _lease(appid="game-a", *, state="ACTIVE"):
    return SessionLease(
        lease_id="lease-1",
        appid=appid,
        browser_generation="browser",
        home_target_id="home-1",
        plugin_target_id="plugin-1",
        plugin_generation=1,
        source_token="test-only-token",
        issued_at=timezone.now(),
        state=state,
    )


def test_access_is_released_when_a_source_request_raises():
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease()
    service._leases[lease.appid] = [lease]

    with pytest.raises(RuntimeError):
        with service.access(ExecutionScope("game-a", 1)):
            raise RuntimeError("request failed")

    assert lease.active_accesses == {}


def test_retiring_lease_cannot_be_rebound(monkeypatch):
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease(state="RETIRING")
    service._leases[lease.appid] = [lease]

    with pytest.raises(Exception, match="退役"):
        service._rebind_active(lease)


def test_runtime_projection_never_contains_source_token(tmp_path):
    from service_supervisor.runtime import RuntimeStore

    service = GameCircleSessionService(runtime_store=RuntimeStore(tmp_path))
    initial_projection = RuntimeStore(tmp_path).read_json(RuntimeStore(tmp_path).game_circle_session_path)
    assert initial_projection["leases"] == []
    service._browser_generation = "browser"
    service._leases["game-a"] = [_lease()]
    service._save_projection()

    assert "source_token" not in str(RuntimeStore(tmp_path).read_json(RuntimeStore(tmp_path).game_circle_session_path))


def test_new_service_cleans_only_prior_registered_session_targets(monkeypatch, tmp_path):
    from service_supervisor.runtime import RuntimeStore

    store = RuntimeStore(tmp_path)
    store.write_json(
        store.game_circle_session_path,
        {
            "owner_epoch": "old-owner",
            "browser_generation": "browser",
            "leases": [],
            "targets": [
                {"target_id": "old-home", "role": "LEASE_HOME", "owner_epoch": "old-owner"},
                {"target_id": "old-plugin", "role": "LEASE_PLUGIN", "owner_epoch": "old-owner"},
                {"target_id": "login-page", "role": "LOGIN_PAGE", "owner_epoch": "old-owner"},
            ],
        },
    )

    class Browser:
        def __init__(self):
            self.closed = []

        def browser_generation(self):
            return "browser"

        def close_target(self, target_id):
            self.closed.append(target_id)

    browser = Browser()
    service = GameCircleSessionService(runtime_store=store)

    service._sync_generation(browser)

    assert browser.closed == ["old-home", "old-plugin"]
    projection = store.read_json(store.game_circle_session_path)
    assert "orphaned_targets" not in projection


def test_new_service_retains_an_orphan_target_when_closure_is_not_confirmed(tmp_path):
    from service_supervisor.runtime import RuntimeStore

    store = RuntimeStore(tmp_path)
    store.write_json(
        store.game_circle_session_path,
        {
            "owner_epoch": "old-owner",
            "browser_generation": "browser",
            "leases": [],
            "targets": [{"target_id": "old-plugin", "role": "LEASE_PLUGIN", "owner_epoch": "old-owner"}],
        },
    )

    class Browser:
        def browser_generation(self):
            return "browser"

        def close_target(self, _target_id):
            raise CdpTargetUnavailableError("not closed")

        def target_exists(self, _target_id):
            return True

    service = GameCircleSessionService(runtime_store=store)

    service._sync_generation(Browser())

    projection = store.read_json(store.game_circle_session_path)
    assert projection["orphaned_browser_generation"] == "browser"
    assert projection["orphaned_targets"] == [
        {"target_id": "old-plugin", "role": "LEASE_PLUGIN", "owner_epoch": "old-owner"}
    ]


def test_shutdown_closes_current_owned_targets_but_preserves_login_page(monkeypatch, tmp_path):
    from service_supervisor.runtime import RuntimeStore

    class Browser:
        closed = []

        def close_target(self, target_id):
            self.closed.append(target_id)

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    service = GameCircleSessionService(runtime_store=RuntimeStore(tmp_path))
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch),
        "login": OwnedTarget("login", "LOGIN_PAGE", service.owner_epoch),
    }

    service.stop_accepting()

    assert Browser.closed == ["home", "plugin"]
    assert "login" in service._owned_targets


def test_verified_lease_reclaims_only_current_owner_login_pages(tmp_path):
    from service_supervisor.runtime import RuntimeStore

    class Browser:
        def __init__(self):
            self.closed = []

        def close_target(self, target_id):
            self.closed.append(target_id)

    service = GameCircleSessionService(runtime_store=RuntimeStore(tmp_path))
    service._owned_targets = {
        "login": OwnedTarget("login", "LOGIN_PAGE", service.owner_epoch),
        "manual": OwnedTarget("manual", "LOGIN_PAGE", "another-owner"),
        "lease": OwnedTarget("lease", "LEASE_HOME", service.owner_epoch),
    }
    browser = Browser()

    service._reclaim_replaced_login_pages(browser)

    assert browser.closed == ["login"]
    assert "login" not in service._owned_targets
    assert "manual" in service._owned_targets
    assert "lease" in service._owned_targets


def test_new_active_lease_retires_previous_without_closing_it_early(monkeypatch):
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    old = _lease()
    old.active_accesses["in-flight"] = (timezone.now(), timezone.now() + timedelta(seconds=30))
    service._leases["game-a"] = [old]

    service._publish_lease(
        scope=ExecutionScope("game-a", 1),
        generation="browser",
        home="home-2",
        plugin="plugin-2",
        source_token="new-token",
    )

    assert service._leases["game-a"][0].state == "ACTIVE"
    assert old.state == "RETIRING"
    assert old in service._leases["game-a"]


def test_access_deadline_and_service_stop_close_registered_frames():
    class Frame:
        timeout = 15

        def __init__(self):
            self.closed = False

        def evaluate(self, _expression):
            return {"ok": True}

        def close(self):
            self.closed = True

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease()
    service._leases[lease.appid] = [lease]

    with service.access(ExecutionScope("game-a", 1)) as access:
        frame = Frame()
        bound = _DeadlineBoundFrame(service, access, frame)
        assert bound.evaluate("({})") == {"ok": True}
        service.stop_accepting()
        assert frame.closed is True
        assert access.state.cancelled is True

    assert lease.active_accesses == {}


def test_expired_access_rejects_cdp_before_evaluation():
    class Frame:
        timeout = 15

        def __init__(self):
            self.called = False

        def evaluate(self, _expression):
            self.called = True
            return {"ok": True}

        def close(self):
            return None

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease()
    service._leases[lease.appid] = [lease]

    with service.access(ExecutionScope("game-a", 1)) as access:
        access.state.deadline_monotonic = 0
        frame = Frame()
        with pytest.raises(SourceTransportError, match="超时"):
            _DeadlineBoundFrame(service, access, frame).evaluate("({})")
        assert frame.called is False


def test_outer_token_waits_for_the_created_home_to_finish_redirecting(monkeypatch):
    class Outer:
        def __init__(self):
            self.calls = 0

        def scan_required(self):
            return False

        def read_outer_token(self):
            self.calls += 1
            if self.calls == 1:
                from config.game_circle_adapter import OuterSessionUnreadyError

                raise OuterSessionUnreadyError("not ready")
            return "test-token"

    monkeypatch.setattr("config.game_circle_service.sleep", lambda _seconds: None)

    assert GameCircleSessionService._require_outer_token(Outer()) == "test-token"


def test_catalog_sync_uses_and_cleans_an_owned_candidate(monkeypatch, tmp_path):
    from service_supervisor.runtime import RuntimeStore

    class Browser:
        closed = []

        def browser_generation(self):
            return "browser"

        def create_target(self, _url):
            return "catalog-home"

        def target_count(self):
            return 2

        def close_target(self, target_id):
            self.closed.append(target_id)

    class Page:
        def __init__(self, **_kwargs):
            pass

        def wait_for_public_page(self):
            return True

        def close(self):
            return None

    class Adapter:
        def __init__(self, _page):
            pass

        def list_managed_games(self):
            return [{"appid": "game-a", "name": "游戏 A"}]

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.ExplicitOuterPage", Page)
    monkeypatch.setattr("config.game_circle_service.GameCircleOuterAdapter", Adapter)
    service = GameCircleSessionService(runtime_store=RuntimeStore(tmp_path))

    assert service.list_managed_games() == [{"appid": "game-a", "name": "游戏 A"}]
    assert Browser.closed == ["catalog-home"]
    assert service._owned_targets == {}


def test_missing_source_target_enqueues_recovery_and_returns_the_fixed_ipc_result(monkeypatch):
    service = GameCircleSessionService()
    scope = ExecutionScope("game-a", 1)
    enqueued = []
    monkeypatch.setattr(
        service,
        "_execute_source_request",
        lambda _request: (_ for _ in ()).throw(PluginSessionExpiredError("target missing")),
    )
    monkeypatch.setattr(
        "config.game_circle_service.enqueue_session_refresh",
        lambda **kwargs: enqueued.append(kwargs),
    )

    with pytest.raises(GameCircleIpcError) as exc_info:
        service.handle_ipc(IpcRequest("POSTS_GET_METRICS", scope, {"topic_id": 1}, ""))

    assert exc_info.value.category == "SESSION_RECOVERING"
    assert str(exc_info.value) == "游戏圈会话正在恢复，请稍后重新确认"
    assert enqueued == [{"refresh_mode": REFRESH_RECOVER, "execution_scope": scope}]


def test_source_contract_reason_is_projected_without_source_details(monkeypatch):
    service = GameCircleSessionService()
    scope = ExecutionScope("game-a", 1)
    monkeypatch.setattr(
        service,
        "_execute_source_request",
        lambda _request: (_ for _ in ()).throw(
            SourceContractError("untrusted upstream body", safe_reason="NON_JSON_RESPONSE")
        ),
    )

    with pytest.raises(GameCircleIpcError) as exc_info:
        service.handle_ipc(IpcRequest("POSTS_GET_METRICS", scope, {"topic_id": 1}, ""))

    assert exc_info.value.category == "SOURCE_CONTRACT"
    assert str(exc_info.value) == "来源详情响应不是 JSON"


def test_post_metrics_uses_the_default_bounded_timeout_when_omitted(monkeypatch):
    service = GameCircleSessionService()
    scope = ExecutionScope("game-a", 1)
    received_timeouts = []

    @contextmanager
    def access(_scope, *, timeout_seconds):
        received_timeouts.append(timeout_seconds)
        yield object()

    class Source:
        def get_post_metrics(self, topic_id):
            assert topic_id == 1
            return {"data": {"topic": {}}}

        def close(self):
            pass

    monkeypatch.setattr(service, "access", access)
    monkeypatch.setattr(service, "_post_client", lambda _access: Source())

    assert service._execute_source_request(
        IpcRequest("POSTS_GET_METRICS", scope, {"topic_id": 1}, "")
    ) == {"payload": {"data": {"topic": {}}}}
    assert received_timeouts == [30]


def test_owned_target_cleanup_allows_zero_tabs_when_chrome_is_supervisor_managed():
    class Browser:
        closed = []

        def close_target(self, target_id):
            self.closed.append(target_id)

    service = GameCircleSessionService()
    browser = Browser()
    service._owned_targets["candidate"] = OwnedTarget("candidate", "HOME_CANDIDATE", service.owner_epoch)

    assert service._safe_close(browser, "candidate") is True
    assert browser.closed == ["candidate"]
    assert "candidate" not in service._owned_targets


def test_retiring_lease_is_retained_until_all_owned_targets_are_closed(monkeypatch):
    class Browser:
        def __init__(self):
            self.closed = []

        def close_target(self, target_id):
            self.closed.append(target_id)

    browser = Browser()
    service = GameCircleSessionService()
    lease = _lease(state="RETIRING")
    lease.retire_not_before_at = timezone.now() - timedelta(seconds=1)
    service._leases[lease.appid] = [lease]
    service._owned_targets = {
        "home-1": OwnedTarget("home-1", "LEASE_HOME", service.owner_epoch, lease.lease_id, lease.appid),
        "plugin-1": OwnedTarget("plugin-1", "LEASE_PLUGIN", service.owner_epoch, lease.lease_id, lease.appid),
    }
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", lambda: browser)

    with service._lock:
        service._reclaim_retiring_locked()

    assert service._leases.get(lease.appid) is None
    assert set(browser.closed) == {"home-1", "plugin-1"}
    assert service._owned_targets == {}
