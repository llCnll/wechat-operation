from unittest.mock import Mock

import pytest

from config.cdp import CdpTargetUnavailableError
from config.game_circle_adapter import (
    ExplicitOuterPage,
    GameCircleContractError,
    GameCircleOuterAdapter,
    OuterLoginExpiredError,
    OuterSessionUnreadyError,
    plugin_redirect_url,
)
from config.game_circle_session import (
    SourceIdentityResult,
    classify_source_identity_result,
    validate_refresh_preconditions,
    RefreshDirectFailure,
)


def test_current_game_contract_uses_absolute_gamemp_url_from_the_outer_page():
    page = Mock()
    page.evaluate.return_value = {"status": 200, "errcode": 0, "appid": "game-a"}

    adapter = GameCircleOuterAdapter(page)

    assert adapter.read_current_game() == "game-a"
    expression = page.evaluate.call_args.args[0]
    assert "https://gamemp.weixin.qq.com/cgi-bin/gamewxagsettingwap/getappinfo" in expression
    assert "cache: 'no-store'" in expression
    assert "headers:" not in expression


def test_explicit_outer_page_passes_its_owned_target_to_the_cdp_base():
    page = ExplicitOuterPage(target_id="owned-home", cdp_url="http://127.0.0.1:19999")

    assert page.target_id == "owned-home"


def test_current_game_contract_returns_the_actual_outer_game():
    page = Mock()
    page.evaluate.return_value = {"status": 200, "errcode": 0, "appid": "game-b"}

    assert GameCircleOuterAdapter(page).read_current_game() == "game-b"


@pytest.mark.parametrize("errcode", [40001, 40042])
def test_current_game_contract_classifies_the_verified_expired_login_response(errcode):
    page = Mock()
    page.evaluate.return_value = {"status": 401, "errcode": errcode}

    with pytest.raises(OuterLoginExpiredError) as exc_info:
        GameCircleOuterAdapter(page).read_current_game()

    assert exc_info.value.safe_reason == "LOGIN_EXPIRED"


def test_current_game_wait_retries_until_the_verified_contract_is_readable(monkeypatch):
    adapter = GameCircleOuterAdapter(Mock())
    calls = iter(
        [
            GameCircleContractError("当前游戏回读失败：来源 HTTP 404", safe_reason="HTTP_STATUS"),
            "game-a",
        ]
    )
    monkeypatch.setattr("config.game_circle_adapter.sleep", lambda _seconds: None)

    def read_once():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(adapter, "read_current_game", read_once)

    assert adapter.wait_for_readable_current_game(timeout_seconds=1) == "game-a"


@pytest.mark.parametrize(
    ("value", "reason", "message"),
    [
        ({"status": 403}, "HTTP_STATUS", "HTTP 403"),
        ({"status": 200, "nonJson": True}, "NON_JSON_RESPONSE", "不是 JSON"),
        ({"status": 200, "errcode": 1}, "BUSINESS_REJECTED", "业务拒绝"),
        ({"status": 200, "errcode": 0, "appid": ""}, "INVALID_PAYLOAD", "无有效游戏"),
    ],
)
def test_current_game_contract_projects_only_safe_failure_reasons(value, reason, message):
    page = Mock()
    page.evaluate.return_value = value

    with pytest.raises(GameCircleContractError, match=message) as exc_info:
        GameCircleOuterAdapter(page).read_current_game()

    assert exc_info.value.safe_reason == reason


def test_switch_uses_the_directory_validated_target_and_relative_outer_endpoint():
    page = Mock()
    page.evaluate.side_effect = [
        {"kind": "response", "status": 200, "ret": 0, "appids": ["game-a", "game-b"], "switchPermitted": True},
        {"kind": "response", "status": 200, "ret": 0},
    ]

    GameCircleOuterAdapter(page).switch_to_game("game-b")

    expression = page.evaluate.call_args_list[1].args[0]
    assert "/wxamp/cgi/switchAccount?token=" in expression
    assert "new URLSearchParams({appid: targetAppid})" in expression


def test_switch_reload_uses_the_fixed_outer_home_route():
    page = Mock()

    GameCircleOuterAdapter(page).reload_after_game_switch()

    assert "window.location.assign('/')" in page.evaluate.call_args.args[0]
    page.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        ({"appids": ["game-a"], "switchPermitted": True}, "不支持"),
        ({"appids": ["game-a", "game-b"], "switchPermitted": False}, "不允许"),
    ],
)
def test_switch_rejects_unavailable_or_unpermitted_target(listing, message):
    page = Mock()
    page.evaluate.return_value = {"kind": "response", "status": 200, "ret": 0, **listing}

    with pytest.raises(GameCircleContractError, match=message):
        GameCircleOuterAdapter(page).switch_to_game("game-b")


def test_switchable_game_directory_preserves_duplicates_for_coordinator_safety():
    page = Mock()
    page.evaluate.return_value = {
        "kind": "response",
        "status": 200,
        "ret": 0,
        "appids": ["game-a", "game-b", "game-b"],
        "switchPermitted": True,
    }

    assert GameCircleOuterAdapter(page).list_switchable_game_appids() == ["game-a", "game-b", "game-b"]


def test_only_verified_source_query_rejection_invalidates_identity_lease():
    assert classify_source_identity_result(-2) == SourceIdentityResult.TOKEN_REJECTED
    assert classify_source_identity_result(1) == SourceIdentityResult.BUSINESS_ERROR
    assert classify_source_identity_result(0) == SourceIdentityResult.ACCEPTED


def test_fixed_plugin_route_uses_runtime_token_without_exposing_it_in_state():
    url = plugin_redirect_url("test-runtime-value")
    assert url.startswith("https://mp.weixin.qq.com/wxamp/frame/pluginRedirect/pluginRedirect?")
    assert "custom=path%3D%2Fminigame%40community" in url


@pytest.mark.parametrize(
    ("scan_required", "has_token", "message"),
    [
        (True, True, "人工扫码"),
        (False, False, "人工扫码"),
    ],
)
def test_refresh_direct_failure_boundaries(scan_required, has_token, message):
    with pytest.raises(RefreshDirectFailure, match=message):
        validate_refresh_preconditions(scan_required=scan_required, has_outer_token=has_token)


def test_outer_scan_probe_only_classifies_visible_login_markers():
    page = Mock()
    for text in ("请使用微信扫描二维码登录", "微信扫一扫，选择公众平台账号登录"):
        page.body_text.return_value = text
        assert GameCircleOuterAdapter(page).scan_required() is True
    page.body_text.return_value = "已进入管理首页"
    assert GameCircleOuterAdapter(page).scan_required() is False


def test_login_diagnosis_waits_for_a_scan_page_after_the_home_context_reloads(monkeypatch):
    page = Mock()
    page.body_text.side_effect = [
        CdpTargetUnavailableError("execution context was destroyed"),
        "微信扫码登录",
    ]
    adapter = GameCircleOuterAdapter(page)
    monkeypatch.setattr("config.game_circle_adapter.sleep", lambda _seconds: None)

    assert adapter.wait_for_login_diagnosis(timeout_seconds=1) is None
    assert page.body_text.call_count == 2
    page.evaluate.assert_not_called()


def test_missing_outer_token_has_a_dedicated_non_chrome_failure_type():
    page = Mock()
    page.evaluate.return_value = {"token": None}

    with pytest.raises(OuterSessionUnreadyError, match="缺少运行时 token"):
        GameCircleOuterAdapter(page).read_outer_token()
