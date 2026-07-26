from datetime import timedelta
from threading import Event, Thread

import pytest
from django.utils import timezone

from config.cdp import CdpTargetUnavailableError
from config.game_circle_adapter import GameCircleContractError, OuterLoginExpiredError
from config.game_circle_ipc import GameCircleIpcError, IpcRequest
from config.game_circle_service import (
    BootstrapIntent,
    BootstrapResult,
    GameCircleSessionService,
    LOGIN_COMPLETION_SETTLE_SECONDS,
    OwnedTarget,
    SessionLease,
    _OuterSession,
    _OuterSessionKind,
    _LoginPending,
)
from jobs.adapters import PluginSessionExpiredError
from posts.game_catalog import ExecutionScope
from posts.models import GameCircleLoginRuntimeState
from service_supervisor.runtime import RuntimeStore


@pytest.fixture(autouse=True)
def isolate_game_circle_runtime_store(monkeypatch, tmp_path):
    """Keep session projections out of the live Supervisor runtime on Windows."""
    monkeypatch.setattr("config.game_circle_service.RUNTIME_DIR", tmp_path)


def _lease(appid="game-a"):
    return SessionLease(
        appid=appid,
        browser_generation="browser",
        home_target_id="home",
        plugin_target_id="plugin",
        source_token="test-only-token",
        issued_at=timezone.now(),
    )


def _verified_outer(page, outer, token="outer-token"):
    if hasattr(outer, "__dict__") and not hasattr(outer, "read_usable_outer_token"):
        outer.read_usable_outer_token = lambda: token
    current_appid = (
        outer.wait_for_readable_current_game()
        if hasattr(outer, "wait_for_readable_current_game")
        else "game-a"
    )
    return _OuterSession(
        page,
        outer,
        _OuterSessionKind.VERIFIED_WITH_TOKEN,
        token,
        current_appid,
    )


def _scan_required_outer(page, outer):
    return _OuterSession(page, outer, _OuterSessionKind.SCAN_REQUIRED)


def _token_unready_outer(page, outer):
    return _OuterSession(
        page,
        outer,
        _OuterSessionKind.VERIFIED_TOKEN_UNREADY,
        current_appid="game-a",
    )


@pytest.mark.django_db
def test_runtime_projection_never_contains_credentials_or_target_identifiers(tmp_path):
    store = RuntimeStore(tmp_path)
    service = GameCircleSessionService(runtime_store=store)
    service._browser_generation = "browser"
    service._leases["game-a"] = [_lease()]
    service._save_projection()

    projection = store.read_json(store.game_circle_session_path)
    rendered = str(projection)
    assert "test-only-token" not in rendered
    assert "home" not in rendered
    assert "plugin" not in rendered
    assert projection["active_lease_count"] == 1


@pytest.mark.django_db
def test_source_without_lease_has_no_bootstrap_or_browser_side_effect(monkeypatch):
    service = GameCircleSessionService()
    scope = ExecutionScope("game-a", 1)
    monkeypatch.setattr(service, "bootstrap", lambda **_kwargs: pytest.fail("source must not bootstrap"))

    with pytest.raises(GameCircleIpcError) as raised:
        with service.access(scope):
            pass

    assert raised.value.category == "SESSION_UNAVAILABLE"


def test_keepalive_does_not_join_a_non_keepalive_bootstrap_flight(monkeypatch):
    service = GameCircleSessionService()
    scope = ExecutionScope("game-a", 1)
    entered = Event()
    release = Event()
    calls = []
    results = {}

    def bootstrap_locked(**kwargs):
        is_keepalive = kwargs["intent"] is BootstrapIntent.KEEPALIVE
        calls.append(is_keepalive)
        if not is_keepalive:
            entered.set()
            assert release.wait(timeout=2)
        return BootstrapResult(lease_published=is_keepalive)

    monkeypatch.setattr(service, "_bootstrap_locked", bootstrap_locked)
    first = Thread(target=lambda: results.setdefault("normal", service.bootstrap(scope=scope, intent=BootstrapIntent.ESTABLISH_FRAME)))
    second = Thread(target=lambda: results.setdefault("keepalive", service.bootstrap(scope=scope, intent=BootstrapIntent.KEEPALIVE)))

    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [False, True]
    assert results["normal"].lease_published is False
    assert results["keepalive"].lease_published is True


@pytest.mark.django_db
def test_source_access_is_released_on_exception(tmp_path):
    service = GameCircleSessionService(runtime_store=RuntimeStore(tmp_path))
    service._browser_generation = "browser"
    lease = _lease()
    service._leases["game-a"] = [lease]

    with pytest.raises(RuntimeError):
        with service.access(ExecutionScope("game-a", 1)):
            raise RuntimeError("boom")

    assert lease.active_accesses == {}


@pytest.mark.django_db
def test_source_expiry_never_queues_recovery(monkeypatch):
    service = GameCircleSessionService()
    scope = ExecutionScope("game-a", 1)
    monkeypatch.setattr(
        service,
        "_execute_source_request",
        lambda _request: (_ for _ in ()).throw(CdpTargetUnavailableError("gone")),
    )

    with pytest.raises(CdpTargetUnavailableError):
        service.handle_ipc(IpcRequest("POSTS_GET_METRICS", scope, {"topic_id": 1}, ""))


@pytest.mark.django_db
def test_login_pending_is_unique_and_exposes_only_fixed_projection(monkeypatch):
    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            return "private-target"

        def target_exists(self, target_id):
            return target_id == "private-target"

        def activate_target(self, target_id):
            assert target_id == "private-target"

    GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED,
        frame_state=GameCircleLoginRuntimeState.FrameState.UNREADY,
        available_actions=[GameCircleLoginRuntimeState.Action.OPEN_LOGIN],
    )
    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    assert service.open_login_page()["status"] == "CONTROLLED_SESSION_PAGE_OPENED"
    assert service.open_login_page()["status"] == "CONTROLLED_SESSION_PAGE_OPENED"
    assert "private-target" not in str(service.runtime_status())


@pytest.mark.django_db
def test_open_login_page_opens_the_observable_controlled_session_page(monkeypatch):
    GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.UNKNOWN,
        frame_state=GameCircleLoginRuntimeState.FrameState.UNREADY,
    )
    service = GameCircleSessionService()
    class Browser:
        def browser_generation(self):
            return "browser"
        def target_exists(self, _target_id):
            return False
        def create_target(self, _url, *, background):
            assert background is True
            return "controlled-home"
        def activate_target(self, target_id):
            assert target_id == "controlled-home"

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    assert service.open_login_page() == {"status": "CONTROLLED_SESSION_PAGE_OPENED"}


@pytest.mark.django_db
def test_unverified_bootstrap_failure_projects_unknown_outer_state(monkeypatch):
    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            return "candidate-home"

        def close_target(self, _target_id):
            return None

        def target_exists(self, _target_id):
            return False

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (_ for _ in ()).throw(GameCircleContractError("request failed")),
    )

    result = service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert result.category == GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.UNKNOWN
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY
    assert GameCircleLoginRuntimeState.Action.OPEN_LOGIN not in state.available_actions


@pytest.mark.django_db
def test_open_login_page_after_outer_recovery_remains_observable(monkeypatch):
    GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.OUTER_READY,
        frame_state=GameCircleLoginRuntimeState.FrameState.BUILDING,
        available_actions=[],
    )
    service = GameCircleSessionService()

    class Browser:
        def browser_generation(self):
            return "browser"
        def target_exists(self, _target_id):
            return False
        def create_target(self, _url, *, background):
            assert background is True
            return "controlled-home"
        def activate_target(self, target_id):
            assert target_id == "controlled-home"

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    assert service.open_login_page() == {"status": "CONTROLLED_SESSION_PAGE_OPENED"}


@pytest.mark.django_db
def test_open_controlled_session_page_reuses_the_current_lease_home(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]

    class Browser:
        def browser_generation(self):
            return "browser"
        def target_exists(self, target_id):
            return target_id == "home"
        def create_target(self, *_args, **_kwargs):
            pytest.fail("a live lease HOME must be the observable session page")
        def activate_target(self, target_id):
            assert target_id == "home"

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)

    assert service.open_login_page() == {"status": "CONTROLLED_SESSION_PAGE_OPENED"}


def test_ensure_home_keeps_login_pending_page_ahead_of_idle_page(monkeypatch):
    service = GameCircleSessionService()
    service._owned_targets = {
        "idle": OwnedTarget("idle", "HOME_IDLE", service.owner_epoch),
        "pending": OwnedTarget("pending", "LOGIN_PENDING", service.owner_epoch),
    }
    closed = []

    class Browser:
        def target_exists(self, target_id):
            return target_id in {"idle", "pending"}
        def close_target(self, target_id):
            closed.append(target_id)

    assert service._ensure_home(Browser()) == "pending"
    assert closed == ["idle"]
    assert set(service._owned_targets) == {"pending"}


@pytest.mark.django_db
def test_bootstrap_publishes_only_after_frame_identity_and_minimum_read(monkeypatch):
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, url, *, background):
            created.append((url, background))
            return "home" if len(created) == 1 else "plugin"

        def target_exists(self, _target_id):
            return False

        def close_target(self, _target_id):
            return None

        def close_target(self, _target_id):
            return None

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return "game-a"

        def switch_to_game(self, _appid):
            pytest.fail("matching game must not switch")

    class Frame:
        def __init__(self, *, target_id):
            assert target_id == "plugin"

        def wait_for_gamemp_iframe(self):
            return True

        def close(self):
            return None

    class Verifier:
        def __init__(self, **_kwargs):
            return None

        def list_candidates(self, **kwargs):
            assert kwargs["page_size"] == 1
            return []

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)
    monkeypatch.setattr("config.game_circle_service.bind_game_identity", lambda *_args, **_kwargs: "source-token")
    monkeypatch.setattr("config.game_circle_service.CdpGameCircleClient", Verifier)
    monkeypatch.setattr(service, "_is_current_scope", lambda _scope: True)

    service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    assert [background for _url, background in created] == [True, True]
    assert service._active_lease(ExecutionScope("game-a", 1)) is not None
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.READY


@pytest.mark.django_db
def test_bootstrap_scan_route_holds_the_candidate_for_bounded_session_recovery(monkeypatch):
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            created.append(background)
            return "home"

        def target_exists(self, _target_id):
            return True

        def close_target(self, _target_id):
            return None

    class Page:
        def close(self):
            return None

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _scan_required_outer(Page(), object()))

    service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    assert created == [True]
    assert service._login_pending is not None
    assert service._login_pending.target_id == "home"
    assert service._owned_targets["home"].role == "LOGIN_PENDING"
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY


@pytest.mark.django_db
def test_bootstrap_outer_rejection_holds_the_candidate_for_bounded_session_recovery(monkeypatch):
    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            return "home"

        def target_exists(self, _target_id):
            return True

        def close_target(self, _target_id):
            return None

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (_ for _ in ()).throw(OuterLoginExpiredError("expired")),
    )

    result = service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    assert result.terminal is False
    assert service._login_pending is not None
    assert service._owned_targets["home"].role == "LOGIN_PENDING"
    assert GameCircleLoginRuntimeState.objects.get(pk=1).outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING


@pytest.mark.django_db
def test_open_login_activates_the_recoverable_candidate_instead_of_creating_another_tab(monkeypatch):
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._owned_targets["candidate-home"] = OwnedTarget(
        "candidate-home", "LOGIN_PENDING", service.owner_epoch
    )
    service._login_pending = _LoginPending(
        "candidate-home", "browser", timezone.now() + timedelta(minutes=1)
    )
    GameCircleLoginRuntimeState.objects.create(
        outer_state=GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING,
        frame_state=GameCircleLoginRuntimeState.FrameState.UNREADY,
        available_actions=[GameCircleLoginRuntimeState.Action.OPEN_LOGIN],
    )

    class Browser:
        def browser_generation(self):
            return "browser"

        def target_exists(self, target_id):
            return target_id == "candidate-home"

        def activate_target(self, target_id):
            assert target_id == "candidate-home"

        def create_target(self, *_args, **_kwargs):
            pytest.fail("recoverable candidate must be reused")

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    assert service.open_login_page() == {"status": "CONTROLLED_SESSION_PAGE_OPENED"}


@pytest.mark.django_db
def test_outer_session_check_preserves_ready_frame_actions(monkeypatch):
    scope = ExecutionScope("game-a", 1)

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            pytest.fail("an existing healthy lease HOME must be reused for an outer check")

        def target_exists(self, _target_id):
            return True

    class Page:
        def close(self):
            return None

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), object()))

    service.bootstrap(scope=scope, intent=BootstrapIntent.CHECK_OUTER_SESSION)

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.READY
    assert state.available_actions == [
        GameCircleLoginRuntimeState.Action.CHECK_LOGIN,
        GameCircleLoginRuntimeState.Action.KEEPALIVE,
    ]


@pytest.mark.django_db
def test_manual_frame_establishment_reuses_a_healthy_active_lease_without_opening_home(monkeypatch):
    scope = ExecutionScope("game-a", 1)

    class Browser:
        def browser_generation(self):
            return "browser"

        def target_exists(self, _target_id):
            return True

        def create_target(self, *_args, **_kwargs):
            pytest.fail("a healthy active lease must not create an idle HOME")

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    result = service.bootstrap(scope=scope, intent=BootstrapIntent.ESTABLISH_FRAME)

    assert result.lease_published is False
    assert GameCircleLoginRuntimeState.objects.get(pk=1).frame_state == GameCircleLoginRuntimeState.FrameState.READY


@pytest.mark.django_db
def test_scan_completion_reuses_registered_home_before_bootstrap(monkeypatch):
    class Browser:
        def __init__(self, **_kwargs):
            return None

        def browser_generation(self):
            return "browser"

        def target_exists(self, target_id):
            return target_id == "home"

    class Page:
        def close(self):
            return None

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._owned_targets["home"] = OwnedTarget("home", "LOGIN_PENDING", service.owner_epoch)
    service._login_pending = _LoginPending("home", "browser", timezone.now() + timedelta(minutes=1))
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda target: _verified_outer(Page(), object()))
    monkeypatch.setattr(service, "_login_wait_is_ready_for_verification", lambda _pending: True)
    observed = []
    monkeypatch.setattr(service, "bootstrap_current_scope", lambda **kwargs: observed.append(kwargs))

    service._observe_login_pending()

    assert observed == [{"intent": BootstrapIntent.ESTABLISH_FRAME}]
    assert service._login_pending is None
    assert service._owned_targets["home"].role == "HOME_IDLE"


@pytest.mark.django_db
def test_lost_pending_login_target_reenters_regular_bootstrap(monkeypatch):
    pending = _LoginPending("login-home", "browser", timezone.now() + timedelta(minutes=1))
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._owned_targets[pending.target_id] = OwnedTarget(
        pending.target_id, "LOGIN_PENDING", service.owner_epoch
    )
    service._login_pending = pending

    class Browser:
        def __init__(self, **_kwargs):
            return None

        def browser_generation(self):
            return "browser"

        def target_exists(self, _target_id):
            return False

    resumed = []
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(
        service, "bootstrap_current_scope", lambda **kwargs: resumed.append(kwargs)
    )

    service._observe_login_pending_locked()

    assert service._login_pending is None
    assert resumed == [{"intent": BootstrapIntent.ESTABLISH_FRAME}]


@pytest.mark.django_db
def test_login_wait_defers_outer_verification_for_ten_seconds_after_scan_disappears(monkeypatch):
    service = GameCircleSessionService()
    pending = _LoginPending("login-home", "browser", timezone.now() + timedelta(minutes=1))

    class Page:
        def __init__(self, **_kwargs):
            return None

        def wait_for_public_page(self):
            return True

        def close(self):
            return None

    class Outer:
        def __init__(self, _page):
            return None

        def is_login_page(self):
            return False

        def scan_required(self):
            return False

    monkeypatch.setattr("config.game_circle_service.ExplicitOuterPage", Page)
    monkeypatch.setattr("config.game_circle_service.GameCircleOuterAdapter", Outer)

    assert service._login_wait_is_ready_for_verification(pending) is False
    assert pending.settle_not_before_at is not None
    assert pending.settle_not_before_at - timezone.now() <= timedelta(seconds=LOGIN_COMPLETION_SETTLE_SECONDS)


@pytest.mark.django_db
def test_login_wait_does_not_probe_while_the_post_scan_settle_window_is_open(monkeypatch):
    service = GameCircleSessionService()
    pending = _LoginPending(
        "login-home",
        "browser",
        timezone.now() + timedelta(minutes=1),
        settle_not_before_at=timezone.now() + timedelta(seconds=LOGIN_COMPLETION_SETTLE_SECONDS),
    )
    service._login_pending = pending

    class Browser:
        def __init__(self, **_kwargs):
            return None

        def browser_generation(self):
            return "browser"

        def target_exists(self, target_id):
            return target_id == pending.target_id

    service._browser_generation = "browser"
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_login_wait_is_ready_for_verification", lambda _pending: False)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _target: pytest.fail("getappinfo must not run during the settle window"),
    )

    service._observe_login_pending_locked()

    assert service._login_pending is pending


@pytest.mark.django_db
def test_outer_ready_without_current_scope_never_creates_a_plugin(monkeypatch):
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            created.append(background)
            return "home"

        def target_exists(self, _target_id):
            return False

    class Page:
        def close(self):
            return None

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), object()))

    service.bootstrap(scope=None, intent=BootstrapIntent.ESTABLISH_FRAME)

    assert created == [True]
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY


@pytest.mark.django_db
def test_unselectable_target_is_not_published(monkeypatch):
    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            return "home"

        def target_exists(self, _target_id):
            return False

        def close_target(self, _target_id):
            return None

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return "other-game"

        def switch_to_game(self, _appid):
            from config.game_circle_adapter import GameSelectionError

            raise GameSelectionError("当前受控账号不支持切换至任务游戏")

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))

    service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    assert service._active_lease(ExecutionScope("game-a", 1)) is None
    assert GameCircleLoginRuntimeState.objects.get(pk=1).error_category == GameCircleLoginRuntimeState.ErrorCategory.GAME_NOT_SELECTABLE


@pytest.mark.django_db
def test_unstable_frame_is_not_published(monkeypatch):
    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            return "home" if not hasattr(self, "created") else "plugin"

        def target_exists(self, _target_id):
            return False

        def close_target(self, _target_id):
            return None

    # Return stable distinct IDs without observing URLs or credentials.
    browser = Browser()
    browser.created = False
    calls = []
    closed = []
    browser.create_target = lambda _url, *, background: (calls.append(background) or ("home" if len(calls) == 1 else "plugin"))
    browser.close_target = lambda target_id: closed.append(target_id)

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return "game-a"

    class Frame:
        def __init__(self, **_kwargs):
            return None

        def wait_for_gamemp_iframe(self):
            return False

        def close(self):
            return None

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", lambda: browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)

    service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    assert service._active_lease(ExecutionScope("game-a", 1)) is None
    assert GameCircleLoginRuntimeState.objects.get(pk=1).frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY
    assert closed == ["plugin", "home"]


@pytest.mark.django_db
def test_keepalive_intermediate_failure_closes_the_candidate_home(monkeypatch):
    closed = []
    switches = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            return "candidate-home"

        def target_exists(self, _target_id):
            return False

        def close_target(self, target_id):
            closed.append(target_id)

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return "game-a"

        def list_switchable_game_appids(self):
            return ["game-a", "game-b"]

        def switch_to_game(self, appid):
            switches.append(appid)
            if appid == "game-a":
                raise CdpTargetUnavailableError("target game readback failed")

        def reload_after_game_switch(self):
            return None

        def wait_for_current_game(self, _appid):
            return None

        def read_outer_token(self):
            return "outer-token"

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))

    result = service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.KEEPALIVE)

    assert result.lease_published is False
    assert result.category == GameCircleLoginRuntimeState.ErrorCategory.TARGET_UNAVAILABLE
    assert switches == ["game-b", "game-a"]
    assert closed == ["candidate-home"]


@pytest.mark.django_db
def test_failed_keepalive_preserves_current_active_lease_projection(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    closed = []
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            created.append(background)
            return "candidate-home" if len(created) == 1 else "candidate-plugin"

        def target_exists(self, _target_id):
            return True

        def close_target(self, target_id):
            closed.append(target_id)

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return "game-a"

        def list_switchable_game_appids(self):
            return ["game-b"]

        def switch_to_game(self, _appid):
            return None

        def reload_after_game_switch(self):
            return None

        def wait_for_current_game(self, _appid):
            return None

        def read_outer_token(self):
            return "outer-token"

    class Frame:
        def __init__(self, **_kwargs):
            return None

        def wait_for_gamemp_iframe(self):
            return False

        def close(self):
            return None

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    old = _lease()
    service._leases[scope.target_appid] = [old]
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch, scope.target_appid),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch, scope.target_appid),
    }
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))
    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)

    result = service.bootstrap(scope=scope, intent=BootstrapIntent.KEEPALIVE)

    assert result.lease_published is False
    assert service._active_lease(scope) is old
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.READY
    assert state.error_category == GameCircleLoginRuntimeState.ErrorCategory.NONE


@pytest.mark.django_db
def test_repair_replaces_a_lease_when_its_frame_target_disappears(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    old = _lease()
    service._leases[scope.target_appid] = [old]
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch, scope.target_appid),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch, scope.target_appid),
    }

    class Browser:
        def browser_generation(self):
            return "browser"

        def target_exists(self, target_id):
            return target_id != "plugin"

    captured = []
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.public_runtime_config", lambda: {"session_repair_enabled": "true"})
    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(service, "bootstrap", lambda **kwargs: captured.append(kwargs))

    service._repair_if_enabled()

    assert captured == [{"scope": scope, "intent": BootstrapIntent.REPAIR_BROKEN_FRAME}]


@pytest.mark.django_db
def test_repair_of_a_healthy_lease_does_not_create_a_candidate(monkeypatch):
    scope = ExecutionScope("game-a", 1)

    class Browser:
        def browser_generation(self):
            return "browser"

        def target_exists(self, _target_id):
            return True

        def create_target(self, *_args, **_kwargs):
            pytest.fail("healthy repair must not replace a lease")

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    result = service.bootstrap(scope=scope, intent=BootstrapIntent.REPAIR_BROKEN_FRAME)

    assert result.lease_published is False
    assert GameCircleLoginRuntimeState.objects.get(pk=1).frame_state == GameCircleLoginRuntimeState.FrameState.READY


@pytest.mark.django_db
def test_repair_replacement_bypasses_active_lease_short_circuit(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            created.append(True)
            return "new-home" if len(created) == 1 else "new-plugin"

        def target_exists(self, target_id):
            return target_id != "plugin"

        def close_target(self, _target_id):
            return None

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return scope.target_appid

    class Frame:
        def __init__(self, **_kwargs):
            return None

        def wait_for_gamemp_iframe(self):
            return True

        def close(self):
            return None

    class Verifier:
        def __init__(self, **_kwargs):
            return None

        def list_candidates(self, **_kwargs):
            return []

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    old = _lease()
    service._leases[scope.target_appid] = [old]
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch, scope.target_appid),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch, scope.target_appid),
    }
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)
    monkeypatch.setattr("config.game_circle_service.bind_game_identity", lambda *_args, **_kwargs: "source-token")
    monkeypatch.setattr("config.game_circle_service.CdpGameCircleClient", Verifier)
    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)

    result = service.bootstrap(scope=scope, intent=BootstrapIntent.REPAIR_BROKEN_FRAME)

    assert result.lease_published is True
    assert service._active_lease(scope) is not old
    assert old.state == "RETIRING"
    assert created == [True, True]
    assert not any(target.role == "HOME_IDLE" for target in service._owned_targets.values())


@pytest.mark.django_db
def test_failed_repair_of_a_broken_lease_projects_frame_unready(monkeypatch):
    """A broken lease is retained for access cleanup, never for READY projection."""
    scope = ExecutionScope("game-a", 1)
    closed = []
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def target_exists(self, _target_id):
            return False

        def create_target(self, _url, *, background):
            assert background is True
            created.append(True)
            return "candidate-home" if len(created) == 1 else "candidate-plugin"

        def close_target(self, target_id):
            closed.append(target_id)

    class Page:
        def close(self):
            return None

    class Outer:
        def wait_for_readable_current_game(self):
            return scope.target_appid

    class Frame:
        def __init__(self, **_kwargs):
            return None

        def wait_for_gamemp_iframe(self):
            return False

        def close(self):
            return None

    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch, scope.target_appid),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch, scope.target_appid),
    }
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), Outer()))
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)
    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)

    result = service.bootstrap(scope=scope, intent=BootstrapIntent.REPAIR_BROKEN_FRAME)

    assert result.lease_published is False
    assert GameCircleLoginRuntimeState.objects.get(pk=1).frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY


@pytest.mark.django_db
def test_old_scope_completion_never_marks_new_current_scope_ready(monkeypatch, tmp_path):
    service = GameCircleSessionService(runtime_store=RuntimeStore(tmp_path))
    service._browser_generation = "browser"
    scope = ExecutionScope("old-game", 1)
    monkeypatch.setattr(service, "_is_current_scope", lambda _scope: False)

    service._publish_lease(
        scope=scope,
        generation="browser",
        home="home",
        plugin="plugin",
        source_token="test-token",
    )
    # The projection branch is the same one used after a frozen keepalive
    # publishes an old scope while a new operator scope is active.
    service._set_runtime(
        GameCircleLoginRuntimeState.OuterState.OUTER_READY,
        GameCircleLoginRuntimeState.FrameState.UNREADY,
        category=GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY,
        actions=(GameCircleLoginRuntimeState.Action.BUILD_FRAME,),
    )

    assert service._active_lease(scope) is not None
    assert GameCircleLoginRuntimeState.objects.get(pk=1).frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY


@pytest.mark.django_db
def test_retiring_lease_is_reclaimed_without_a_following_source_access(monkeypatch):
    class Browser:
        def __init__(self):
            self.closed = []

        def close_target(self, target_id):
            self.closed.append(target_id)
            return None

    service = GameCircleSessionService()
    lease = _lease()
    lease.state = "RETIRING"
    lease.retire_not_before_at = timezone.now() - timedelta(seconds=1)
    service._leases["game-a"] = [lease]
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch, "game-a"),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch, "game-a"),
    }
    browser = Browser()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", lambda: browser)

    class TrackingLane:
        entered = 0

        def __enter__(self):
            self.entered += 1

        def __exit__(self, *_args):
            return False

    lane = TrackingLane()
    service._control_lane = lane

    service._reclaim_retiring()
    assert "game-a" not in service._leases
    assert browser.closed == ["plugin", "home"]
    assert lane.entered == 1


@pytest.mark.django_db
def test_retiring_lease_forgets_a_target_already_removed_by_the_browser(monkeypatch):
    class Browser:
        def __init__(self):
            self.closed = []

        def close_target(self, target_id):
            if target_id == "plugin":
                raise CdpTargetUnavailableError("already gone")
            self.closed.append(target_id)

        def target_exists(self, target_id):
            return target_id != "plugin"

    service = GameCircleSessionService()
    lease = _lease()
    lease.state = "RETIRING"
    lease.retire_not_before_at = timezone.now() - timedelta(seconds=1)
    service._leases["game-a"] = [lease]
    service._owned_targets = {
        "home": OwnedTarget("home", "LEASE_HOME", service.owner_epoch, "game-a"),
        "plugin": OwnedTarget("plugin", "LEASE_PLUGIN", service.owner_epoch, "game-a"),
    }
    browser = Browser()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", lambda: browser)

    service._reclaim_retiring()

    assert "game-a" not in service._leases
    assert service._owned_targets == {}
    assert browser.closed == ["home"]


@pytest.mark.django_db
def test_outer_probe_does_not_overwrite_an_unready_frame(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]
    service._set_runtime(
        GameCircleLoginRuntimeState.OuterState.OUTER_READY,
        GameCircleLoginRuntimeState.FrameState.UNREADY,
        category=GameCircleLoginRuntimeState.ErrorCategory.FRAME_UNREADY,
        actions=(GameCircleLoginRuntimeState.Action.BUILD_FRAME,),
    )

    class Page:
        def close(self):
            return None

    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _verified_outer(Page(), object()))

    service._probe_outer_if_due()

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY
    assert state.available_actions == [GameCircleLoginRuntimeState.Action.BUILD_FRAME]
    assert state.outer_verified_at is not None


@pytest.mark.django_db
def test_outer_probe_keeps_existing_lease_on_a_temporary_contract_failure(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease()
    service._leases[scope.target_appid] = [lease]
    service._set_runtime(
        GameCircleLoginRuntimeState.OuterState.OUTER_READY,
        GameCircleLoginRuntimeState.FrameState.READY,
        frame_scope_version=scope.scope_version,
        outer_verified=True,
    )
    verified_at = GameCircleLoginRuntimeState.objects.get(pk=1).outer_verified_at
    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (_ for _ in ()).throw(GameCircleContractError("temporary")),
    )

    service._probe_outer_if_due()

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert lease.state == "ACTIVE"
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    assert state.outer_verified_at == verified_at


@pytest.mark.django_db
def test_outer_probe_keeps_leases_when_login_is_verified_but_home_token_is_unready(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease()
    service._leases[scope.target_appid] = [lease]

    class Page:
        def close(self):
            return None

    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: _token_unready_outer(Page(), object()),
    )

    service._probe_outer_if_due()

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert lease.state == "ACTIVE"
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.OUTER_READY
    assert state.outer_verified_at is not None


@pytest.mark.django_db
def test_expired_outer_recheck_retires_all_generation_leases(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    rejected = _lease("game-a")
    sibling = _lease("game-b")
    service._leases = {"game-a": [rejected], "game-b": [sibling]}
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (_ for _ in ()).throw(OuterLoginExpiredError("expired")),
    )

    service._verify_after_source_rejection(scope, rejected)

    assert rejected.state == "RETIRING"
    assert sibling.state == "RETIRING"
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY
    assert state.login_state == GameCircleLoginRuntimeState.LoginState.SCAN_REQUIRED


@pytest.mark.django_db
def test_source_recheck_with_verified_token_unready_retires_only_the_rejected_lease(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    rejected = _lease("game-a")
    sibling = _lease("game-b")
    service._leases = {"game-a": [rejected], "game-b": [sibling]}

    class Page:
        def close(self):
            return None

    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: _token_unready_outer(Page(), object()),
    )

    service._verify_after_source_rejection(scope, rejected)

    assert rejected.state == "RETIRING"
    assert sibling.state == "ACTIVE"


@pytest.mark.django_db
def test_pending_login_contract_failure_keeps_the_maintenance_loop_observable(monkeypatch):
    service = GameCircleSessionService()
    pending = _LoginPending("login-home", "browser", timezone.now() + timedelta(minutes=1))
    service._login_pending = pending

    class Browser:
        def __init__(self, **_kwargs):
            return None

        def browser_generation(self):
            return "browser"

        def target_exists(self, target_id):
            return target_id == pending.target_id

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (_ for _ in ()).throw(GameCircleContractError("temporary")),
    )

    service._observe_login_pending_locked()

    assert service._login_pending is pending


@pytest.mark.django_db
def test_expired_login_wait_projects_login_required_not_outer_ready(monkeypatch):
    service = GameCircleSessionService()
    pending = _LoginPending("login-home", "browser", timezone.now() - timedelta(seconds=1))
    service._login_pending = pending

    class Browser:
        def browser_generation(self):
            return "browser"

        def close_target(self, _target_id):
            return None

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    service._observe_login_pending_locked()

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert service._login_pending is None
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY


@pytest.mark.django_db
def test_managed_game_directory_expiry_projects_login_required(monkeypatch):
    service = GameCircleSessionService()

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            return "home"

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (_ for _ in ()).throw(OuterLoginExpiredError("expired")),
    )

    with pytest.raises(GameCircleIpcError) as raised:
        service.list_managed_games()

    assert raised.value.category == "SESSION_UNAVAILABLE"
    assert GameCircleLoginRuntimeState.objects.get(pk=1).outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_REQUIRED


@pytest.mark.django_db
def test_managed_game_directory_unready_session_never_projects_outer_ready(monkeypatch):
    service = GameCircleSessionService()

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            assert background is True
            return "home"

    class Page:
        def close(self):
            return None

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: _token_unready_outer(Page(), object()))

    with pytest.raises(GameCircleIpcError) as raised:
        service.list_managed_games()

    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert raised.value.category == "SESSION_UNAVAILABLE"
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.UNKNOWN
    assert state.error_category == GameCircleLoginRuntimeState.ErrorCategory.OUTER_SESSION_UNREADY


@pytest.mark.django_db
def test_stale_source_rejection_never_retires_a_replacement_lease(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    rejected = _lease()
    replacement = _lease()
    rejected.state = "RETIRING"
    service._leases[scope.target_appid] = [replacement, rejected]
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: pytest.fail("a superseded lease must not trigger an outer recheck"),
    )

    service._verify_after_source_rejection(scope, rejected)

    assert replacement.state == "ACTIVE"


@pytest.mark.django_db
def test_concurrent_source_rejections_share_one_outer_recheck(monkeypatch):
    scope_a = ExecutionScope("game-a", 1)
    scope_b = ExecutionScope("game-b", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease_a = _lease("game-a")
    lease_b = _lease("game-b")
    service._leases = {"game-a": [lease_a], "game-b": [lease_b]}
    entered = Event()
    release = Event()
    calls = []

    class Page:
        def close(self):
            return None

    def outer_session(_home):
        calls.append(True)
        entered.set()
        assert release.wait(timeout=2)
        return _verified_outer(Page(), object())

    monkeypatch.setattr(service, "_outer_session", outer_session)
    # This test exercises the flight protocol only; avoid writing Django state
    # from the two worker threads.
    monkeypatch.setattr(service, "_is_current_scope", lambda _scope: False)
    first = Thread(target=lambda: service._verify_after_source_rejection(scope_a, lease_a))
    second = Thread(target=lambda: service._verify_after_source_rejection(scope_b, lease_b))

    first.start()
    assert entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [True]
    assert lease_a.state == "RETIRING"
    assert lease_b.state == "RETIRING"


@pytest.mark.django_db
def test_outer_probe_is_throttled_for_a_healthy_lease(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    service._leases[scope.target_appid] = [_lease()]
    calls = []

    class Page:
        def close(self):
            return None

    monkeypatch.setattr("config.game_circle_service.current_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(
        service,
        "_outer_session",
        lambda _home: (calls.append(True) or _verified_outer(Page(), object())),
    )

    service._probe_outer_if_due()
    service._probe_outer_if_due()

    assert calls == [True]


@pytest.mark.django_db
def test_publishing_a_refreshed_lease_keeps_an_inflight_old_access_usable():
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    old = _lease()
    service._leases[scope.target_appid] = [old]

    with service.access(scope) as access:
        service._publish_lease(
            scope=scope,
            generation="browser",
            home="new-home",
            plugin="new-plugin",
            source_token="new-source-token",
        )

        assert access.lease is old
        assert old.state == "RETIRING"
        assert access.access_id in old.active_accesses
        assert service._active_lease(scope) is not old


@pytest.mark.django_db
def test_verified_frame_identity_rejection_retires_its_lease(monkeypatch):
    scope = ExecutionScope("game-a", 1)
    service = GameCircleSessionService()
    service._browser_generation = "browser"
    lease = _lease()
    service._leases[scope.target_appid] = [lease]
    monkeypatch.setattr(service, "_is_current_scope", lambda value: value == scope)

    class Client:
        def get_post_metrics(self, _topic_id):
            raise PluginSessionExpiredError("identity expired")

        def close(self):
            return None

    monkeypatch.setattr(service, "_post_client", lambda _access: Client())

    with pytest.raises(GameCircleIpcError) as raised:
        service._execute_source_request(IpcRequest("POSTS_GET_METRICS", scope, {"topic_id": 1}, ""))

    assert raised.value.category == "SESSION_UNAVAILABLE"
    assert lease.state == "RETIRING"
