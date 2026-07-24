from datetime import timedelta
from threading import Event, Thread

import pytest
from django.utils import timezone

from config.cdp import CdpTargetUnavailableError
from config.game_circle_ipc import GameCircleIpcError, IpcRequest
from config.game_circle_service import (
    BootstrapIntent,
    BootstrapResult,
    GameCircleSessionService,
    OwnedTarget,
    SessionLease,
    _LoginPending,
)
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
            assert background is False
            return "private-target"

        def target_exists(self, target_id):
            return target_id == "private-target"

        def activate_target(self, target_id):
            assert target_id == "private-target"

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    assert service.open_login_page()["status"] == "LOGIN_PAGE_OPENED"
    assert service.open_login_page()["status"] == "LOGIN_PAGE_REUSED"
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY
    assert state.available_actions == [GameCircleLoginRuntimeState.Action.OPEN_LOGIN]
    assert "private-target" not in str(service.runtime_status())


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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))
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
def test_bootstrap_scan_required_creates_no_frame_and_registers_one_wait(monkeypatch):
    created = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url, *, background):
            created.append(background)
            return "home"

        def target_exists(self, _target_id):
            return False

        def close_target(self, _target_id):
            return None

    class Page:
        def close(self):
            return None

    service = GameCircleSessionService()
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), object(), None))

    service.bootstrap(scope=ExecutionScope("game-a", 1), intent=BootstrapIntent.ESTABLISH_FRAME)

    assert created == [True]
    assert service._login_pending is not None
    state = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert state.outer_state == GameCircleLoginRuntimeState.OuterState.LOGIN_PENDING
    assert state.frame_state == GameCircleLoginRuntimeState.FrameState.UNREADY


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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), object(), "outer-token"))

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
    monkeypatch.setattr(service, "_outer_session", lambda target: (Page(), object(), "outer-token"))
    observed = []
    monkeypatch.setattr(service, "bootstrap_current_scope", lambda **kwargs: observed.append(kwargs))

    service._observe_login_pending()

    assert observed == [{"intent": BootstrapIntent.ESTABLISH_FRAME}]
    assert service._login_pending is None
    assert service._owned_targets["home"].role == "HOME_IDLE"


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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), object(), "outer-token"))

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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))

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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))
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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))

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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))
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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))
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
    monkeypatch.setattr(service, "_outer_session", lambda _home: (Page(), Outer(), "outer-token"))
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
