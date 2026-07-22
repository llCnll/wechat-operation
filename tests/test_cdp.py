import json
from unittest.mock import Mock, patch

import httpx
import pytest

from config.cdp import (
    CONTROLLED_LOGIN_URL,
    CdpBrowser,
    CdpBrowserUnavailableError,
    CdpGameFrame,
    CdpTargetUnavailableError,
    CdpUnavailableError,
    bind_game_identity,
    open_controlled_login_page,
)


@patch.object(CdpGameFrame, "evaluate", return_value={"errcode": -1, "loginExpired": True})
def test_binding_reports_expired_plugin_login(mock_evaluate):
    try:
        bind_game_identity("wx6d2454a8afcd9706", frame=CdpGameFrame(target_id="owned"))
    except CdpUnavailableError as exc:
        assert str(exc) == "游戏圈登录态已失效，请重新登录"
    else:
        raise AssertionError("expected expired login error")


@patch("config.cdp.connect")
@patch("config.cdp.httpx.Client")
def test_opens_only_the_fixed_login_page_in_controlled_chrome(mock_client, mock_connect):
    response = Mock()
    response.json.return_value = {"webSocketDebuggerUrl": "ws://chrome.test/browser"}
    mock_client.return_value.__enter__.return_value.get.return_value = response
    websocket = mock_connect.return_value
    websocket.recv.return_value = json.dumps({"id": 1, "result": {"targetId": "new-tab"}})

    open_controlled_login_page()

    command = json.loads(websocket.send.call_args.args[0])
    assert command["method"] == "Target.createTarget"
    assert command["params"]["url"] == CONTROLLED_LOGIN_URL
    assert CONTROLLED_LOGIN_URL == "https://mp.weixin.qq.com/cgi-bin/loginpage"
    websocket.close.assert_called_once()


@patch("config.cdp.httpx.Client")
def test_explicit_target_never_falls_back_to_page_list_order(mock_client):
    response = Mock()
    response.json.return_value = [
        {"id": "first", "type": "page", "url": "https://mp.weixin.qq.com/", "webSocketDebuggerUrl": "ws://first"},
        {"id": "owned", "type": "page", "url": "https://mp.weixin.qq.com/", "webSocketDebuggerUrl": "ws://owned"},
    ]
    mock_client.return_value.__enter__.return_value.get.return_value = response

    frame = CdpGameFrame(target_id="owned")

    assert frame._find_page()["id"] == "owned"


@patch("config.cdp.connect")
@patch("config.cdp.httpx.Client")
def test_browser_target_lifecycle_uses_only_returned_target_id(mock_client, mock_connect):
    response = Mock()
    response.json.return_value = {"webSocketDebuggerUrl": "ws://browser"}
    mock_client.return_value.__enter__.return_value.get.return_value = response
    socket = mock_connect.return_value
    socket.recv.side_effect = [
        json.dumps({"id": 1, "result": {"targetId": "owned-target"}}),
        json.dumps({"id": 1, "result": {"success": True}}),
    ]
    browser = CdpBrowser()

    target_id = browser.create_target("https://mp.weixin.qq.com/")
    browser.close_target(target_id)

    assert target_id == "owned-target"
    commands = [json.loads(call.args[0]) for call in socket.send.call_args_list]
    assert commands[0]["params"]["url"] == "https://mp.weixin.qq.com/"
    assert commands[1]["params"] == {"targetId": "owned-target"}


@patch("config.cdp.connect")
@patch("config.cdp.httpx.Client")
def test_close_target_requires_an_explicit_success_response(mock_client, mock_connect):
    response = Mock()
    response.json.return_value = {"webSocketDebuggerUrl": "ws://browser"}
    mock_client.return_value.__enter__.return_value.get.return_value = response
    socket = mock_connect.return_value
    socket.recv.return_value = json.dumps({"id": 1, "result": {"success": False}})

    with pytest.raises(CdpUnavailableError, match="未确认关闭"):
        CdpBrowser().close_target("owned-target")


@patch("config.cdp.httpx.Client")
def test_browser_endpoint_failure_is_distinct_from_target_failure(mock_client):
    mock_client.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("down")

    with pytest.raises(CdpBrowserUnavailableError):
        CdpBrowser().browser_generation()


@patch("config.cdp.connect")
@patch("config.cdp.httpx.Client")
def test_browser_command_protocol_failure_is_a_target_failure(mock_client, mock_connect):
    response = Mock()
    response.json.return_value = {"webSocketDebuggerUrl": "ws://browser"}
    mock_client.return_value.__enter__.return_value.get.return_value = response
    mock_connect.return_value.recv.return_value = json.dumps({"id": 1, "error": {"message": "bad"}})

    with pytest.raises(CdpTargetUnavailableError):
        CdpBrowser().create_target("https://mp.weixin.qq.com/")


def test_plugin_iframe_wait_retries_the_explicit_target_condition(monkeypatch):
    frame = CdpGameFrame(target_id="owned")
    calls = iter((None, "frame-1", "frame-1"))
    monkeypatch.setattr(frame, "_gamemp_frame_id", lambda: next(calls, "frame-1"))

    assert frame.wait_for_gamemp_iframe(timeout_seconds=1, stability_seconds=0) is True


def test_outer_page_wait_retries_until_the_target_finishes_navigation(monkeypatch):
    from config.cdp import CdpMpPage

    page = CdpMpPage(target_id="owned")
    calls = iter((CdpTargetUnavailableError("loading"), 1))

    def ensure():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(page, "_ensure_context", ensure)
    monkeypatch.setattr(page, "close", lambda: None)

    assert page.wait_for_public_page(timeout_seconds=1) is True
