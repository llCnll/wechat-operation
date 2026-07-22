from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from config.cdp import CdpBrowserUnavailableError, CdpTargetUnavailableError
from config.game_circle_adapter import GameCircleContractError
from config.game_circle_service import GameCircleSessionService
from jobs.game_circle_refresh import (
    GAME_CIRCLE_LOGIN_PROBE_TASK,
    REFRESH_KEEPALIVE,
    REFRESH_RECOVER,
    SHANGHAI,
    enqueue_login_probe,
    enqueue_scheduled_session_refresh,
    enqueue_session_refresh,
)
from jobs.models import BackgroundJob, JobStatus
from posts.game_catalog import ExecutionScope, set_active_game
from posts.models import GameCircleLoginRuntimeState, ManagedGame


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def active_game():
    game = ManagedGame.objects.create(appid="game-a", name="游戏 A")
    set_active_game(appid=game.appid)


def test_refresh_admission_only_reuses_the_same_refresh_mode():
    keepalive = enqueue_session_refresh(refresh_mode=REFRESH_KEEPALIVE)
    recover = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER)
    second_keepalive = enqueue_session_refresh(refresh_mode=REFRESH_KEEPALIVE)
    second_recover = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER)

    assert keepalive.created is True
    assert recover.created is True
    assert recover.job.pk != keepalive.job.pk
    assert second_keepalive.job.pk == keepalive.job.pk
    assert second_recover.job.pk == recover.job.pk


def test_recover_keeps_a_frozen_source_scope_after_active_game_changes():
    other = ManagedGame.objects.create(appid="game-b", name="游戏 B")
    frozen = ExecutionScope("game-a", 1)
    set_active_game(appid=other.appid)
    job = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=frozen).job
    assert job.checkpoint["execution_scope"] == frozen.as_checkpoint()


def test_recover_admission_reuses_one_active_job_for_the_same_scope():
    scope = ExecutionScope("game-a", 1)

    first = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=scope)
    second = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=scope)

    assert first.created is True
    assert second.created is False
    assert second.job.pk == first.job.pk


def test_terminal_recover_releases_its_admission_key_for_a_new_job():
    scope = ExecutionScope("game-a", 1)
    first = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=scope).job
    first.status = JobStatus.SUCCEEDED
    first.save(update_fields=["status", "updated_at"])

    admitted = enqueue_session_refresh(refresh_mode=REFRESH_RECOVER, execution_scope=scope)
    first.refresh_from_db()

    assert admitted.created is True
    assert admitted.job.pk != first.pk
    assert first.status == JobStatus.SUCCEEDED
    assert first.idempotency_key.endswith(f":terminal:{first.pk}")


def test_scheduler_only_enqueues_six_beijing_refresh_slots():
    zone = ZoneInfo("Asia/Shanghai")
    assert enqueue_scheduled_session_refresh(datetime(2026, 7, 22, 3, 9, tzinfo=zone)) is None
    assert enqueue_scheduled_session_refresh(datetime(2026, 7, 22, 3, 10, tzinfo=zone)).created is True


def test_login_probe_is_scope_free_and_reused():
    first = enqueue_login_probe(source="manual")
    second = enqueue_login_probe(source="manual")
    assert first.created is True
    assert second.created is False
    assert first.job.task_type == GAME_CIRCLE_LOGIN_PROBE_TASK
    assert first.job.checkpoint == {}


def test_scheduler_probe_slot_is_idempotent_after_the_first_job_completes():
    now = datetime(2026, 7, 22, 10, 5, tzinfo=SHANGHAI)
    first = enqueue_login_probe(source="scheduler", now=now)
    first.job.status = "SUCCEEDED"
    first.job.save(update_fields=["status", "updated_at"])

    second = enqueue_login_probe(source="scheduler", now=now)

    assert second.created is False
    assert second.job.pk == first.job.pk


def test_scheduler_keepalive_slot_is_idempotent_after_the_first_job_completes():
    now = datetime(2026, 7, 22, 3, 10, tzinfo=SHANGHAI)
    first = enqueue_scheduled_session_refresh(now)
    assert first is not None
    first.job.status = "SUCCEEDED"
    first.job.save(update_fields=["status", "updated_at"])

    second = enqueue_scheduled_session_refresh(now)

    assert second is not None
    assert second.created is False
    assert second.job.pk == first.job.pk


def _refresh_job():
    return BackgroundJob.objects.create(
        task_type="GAME_CIRCLE_SESSION_REFRESH",
        idempotency_key="test:refresh:failure",
        parameter_summary={"refresh_mode": REFRESH_KEEPALIVE},
        checkpoint={"execution_scope": {"target_appid": "game-a", "scope_version": 1}},
    )


def _recover_job():
    return BackgroundJob.objects.create(
        task_type="GAME_CIRCLE_SESSION_REFRESH",
        idempotency_key="test:recover:failure",
        parameter_summary={"refresh_mode": REFRESH_RECOVER},
        checkpoint={"execution_scope": {"target_appid": "game-a", "scope_version": 1}},
    )


def test_target_failure_keeps_the_existing_outer_login_state(monkeypatch):
    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url):
            raise CdpTargetUnavailableError("candidate unavailable")

        def close_target(self, _target_id):
            return None

    row = GameCircleLoginRuntimeState.objects.create(
        pk=1,
        login_state=GameCircleLoginRuntimeState.LoginState.READY,
        error_category=GameCircleLoginRuntimeState.ErrorCategory.NONE,
    )
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    with pytest.raises(RuntimeError, match="候选页或插件未就绪"):
        GameCircleSessionService().refresh(_refresh_job())

    row.refresh_from_db()
    assert row.login_state == GameCircleLoginRuntimeState.LoginState.READY


def test_browser_failure_marks_only_the_chrome_unavailable_state(monkeypatch):
    class Browser:
        def browser_generation(self):
            raise CdpBrowserUnavailableError("browser down")

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)

    with pytest.raises(RuntimeError, match="受控 Chrome 不可用"):
        GameCircleSessionService().refresh(_refresh_job())

    row = GameCircleLoginRuntimeState.objects.get(pk=1)
    assert row.login_state == GameCircleLoginRuntimeState.LoginState.UNAVAILABLE
    assert row.error_category == GameCircleLoginRuntimeState.ErrorCategory.CHROME_UNAVAILABLE


def test_refresh_switches_once_then_builds_one_home_and_one_final_plugin(monkeypatch):
    created_urls = []
    events = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, url):
            created_urls.append(url)
            return ("home", "plugin")[len(created_urls) - 1]

        def close_target(self, _target_id):
            return None

    class Page:
        def __init__(self, *, target_id, **_kwargs):
            assert target_id == "home"

        def wait_for_public_page(self):
            events.append("home-ready")
            return True

        def close(self):
            events.append("home-closed")

    class Outer:
        def __init__(self, _page):
            pass

        def scan_required(self):
            return False

        def read_outer_token(self):
            events.append("outer-token")
            return "runtime-token"

        def wait_for_readable_current_game(self):
            events.append("current:game-b")
            return "game-b"

        def switch_to_game(self, appid):
            events.append(f"switch:{appid}")

        def reload_after_game_switch(self):
            events.append("home-reload")

        def wait_for_current_game(self, appid):
            events.append(f"current-ready:{appid}")

    class Frame:
        def __init__(self, *, target_id, **_kwargs):
            assert target_id == "plugin"

        def wait_for_gamemp_iframe(self):
            events.append("iframe-stable")
            return True

        def close(self):
            events.append("frame-closed")

    class Verifier:
        def __init__(self, *, appid, frame, identity_token):
            assert appid == "game-a"
            assert identity_token == "identity-token"

        def list_candidates(self, **kwargs):
            assert kwargs["page"] == 0
            assert kwargs["page_size"] == 1
            events.append("read-only-verified")

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.ExplicitOuterPage", Page)
    monkeypatch.setattr("config.game_circle_service.GameCircleOuterAdapter", Outer)
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)
    monkeypatch.setattr("config.game_circle_service.bind_game_identity", lambda _appid, *, frame: "identity-token")
    monkeypatch.setattr("config.game_circle_service.CdpGameCircleClient", Verifier)

    service = GameCircleSessionService()
    service.refresh(_refresh_job())

    assert created_urls[0] == "https://mp.weixin.qq.com/"
    assert "pluginRedirect" in created_urls[1]
    assert events == ["home-ready", "outer-token", "current:game-b", "switch:game-a", "home-reload", "current-ready:game-a", "outer-token", "home-closed", "iframe-stable", "read-only-verified", "frame-closed"]
    assert len(service._leases["game-a"]) == 1


def test_refresh_rejects_an_unavailable_switch_target_before_creating_a_plugin(monkeypatch):
    created_urls = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, url):
            created_urls.append(url)
            return "home"

        def close_target(self, _target_id):
            return None

    class Page:
        def __init__(self, **_kwargs):
            pass

        def wait_for_public_page(self):
            return True

        def close(self):
            return None

    class Outer:
        def __init__(self, _page):
            pass

        def scan_required(self):
            return False

        def read_outer_token(self):
            return "runtime-token"

        def wait_for_readable_current_game(self):
            return "game-b"

        def switch_to_game(self, _appid):
            raise GameCircleContractError("当前受控账号不支持切换至任务游戏")

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.ExplicitOuterPage", Page)
    monkeypatch.setattr("config.game_circle_service.GameCircleOuterAdapter", Outer)

    with pytest.raises(GameCircleContractError, match="不支持"):
        GameCircleSessionService().refresh(_refresh_job())

    assert created_urls == ["https://mp.weixin.qq.com/"]


def test_recover_at_target_rebuilds_without_switching(monkeypatch):
    events = []

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url):
            return "home" if not events else "plugin"

        def close_target(self, _target_id):
            return None

    class Page:
        def __init__(self, **_kwargs):
            pass

        def wait_for_public_page(self):
            return True

        def close(self):
            return None

    class Outer:
        def __init__(self, _page):
            pass

        def scan_required(self):
            return False

        def read_outer_token(self):
            return "runtime-token"

        def wait_for_readable_current_game(self):
            events.append("current:game-a")
            return "game-a"

        def switch_to_game(self, appid):
            events.append(f"switch:{appid}")

    class Frame:
        def __init__(self, **_kwargs):
            pass

        def wait_for_gamemp_iframe(self):
            return True

        def close(self):
            return None

    class Verifier:
        def __init__(self, **_kwargs):
            pass

        def list_candidates(self, **_kwargs):
            return None

    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.ExplicitOuterPage", Page)
    monkeypatch.setattr("config.game_circle_service.GameCircleOuterAdapter", Outer)
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)
    monkeypatch.setattr("config.game_circle_service.bind_game_identity", lambda *_args, **_kwargs: "identity-token")
    monkeypatch.setattr("config.game_circle_service.CdpGameCircleClient", Verifier)

    GameCircleSessionService().refresh(_recover_job())

    assert events == ["current:game-a"]


def test_keepalive_at_target_switches_through_a_random_permitted_intermediate(monkeypatch):
    events = []
    current = {"appid": "game-a"}

    class Browser:
        def browser_generation(self):
            return "browser"

        def create_target(self, _url):
            return "home" if not events else "plugin"

        def close_target(self, _target_id):
            return None

    class Page:
        def __init__(self, **_kwargs):
            pass

        def wait_for_public_page(self):
            return True

        def close(self):
            return None

    class Outer:
        def __init__(self, _page):
            pass

        def scan_required(self):
            return False

        def read_outer_token(self):
            events.append(f"token:{current['appid']}")
            return "runtime-token"

        def wait_for_readable_current_game(self):
            events.append(f"current:{current['appid']}")
            return current["appid"]

        def list_switchable_game_appids(self):
            events.append("list-switchable")
            return ["game-a", "game-b", "game-c"]

        def switch_to_game(self, appid):
            events.append(f"switch:{appid}")
            current["appid"] = appid

        def reload_after_game_switch(self):
            events.append("reload")

        def wait_for_current_game(self, appid):
            events.append(f"wait:{appid}")
            assert current["appid"] == appid

    class Frame:
        def __init__(self, **_kwargs):
            pass

        def wait_for_gamemp_iframe(self):
            return True

        def close(self):
            return None

    class Verifier:
        def __init__(self, **_kwargs):
            pass

        def list_candidates(self, **_kwargs):
            return None

    monkeypatch.setattr("config.game_circle_service.choice", lambda candidates: "game-b")
    monkeypatch.setattr("config.game_circle_service.CdpBrowser", Browser)
    monkeypatch.setattr("config.game_circle_service.ExplicitOuterPage", Page)
    monkeypatch.setattr("config.game_circle_service.GameCircleOuterAdapter", Outer)
    monkeypatch.setattr("config.game_circle_service.CdpGameFrame", Frame)
    monkeypatch.setattr("config.game_circle_service.bind_game_identity", lambda *_args, **_kwargs: "identity-token")
    monkeypatch.setattr("config.game_circle_service.CdpGameCircleClient", Verifier)

    GameCircleSessionService().refresh(_refresh_job())

    assert events == [
        "token:game-a",
        "current:game-a",
        "list-switchable",
        "switch:game-b",
        "reload",
        "wait:game-b",
        "token:game-b",
        "switch:game-a",
        "reload",
        "wait:game-a",
        "token:game-a",
    ]


def test_keepalive_selects_a_sorted_unique_candidate_without_publishing_lease(monkeypatch):
    class Outer:
        def list_switchable_game_appids(self):
            return ["game-c", "game-a", "game-b", "game-c"]

    selected_from = []

    def choose(candidates):
        selected_from.extend(candidates)
        return candidates[0]

    monkeypatch.setattr("config.game_circle_service.choice", choose)
    service = GameCircleSessionService()

    assert service._resolve_keepalive_intermediate(Outer(), "game-a") == "game-b"

    assert selected_from == ["game-b", "game-c"]
    assert service._leases == {}


def test_keepalive_rejects_missing_intermediate_without_publishing_lease():
    class Outer:
        def list_switchable_game_appids(self):
            return ["game-a"]

    service = GameCircleSessionService()

    with pytest.raises(GameCircleContractError, match="没有可用"):
        service._resolve_keepalive_intermediate(Outer(), "game-a")

    assert service._leases == {}
