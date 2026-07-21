import json
from unittest.mock import Mock, patch

from config.cdp import (
    CONTROLLED_LOGIN_URL,
    CdpGameFrame,
    CdpUnavailableError,
    bind_game_identity,
    get_browser_login_status,
    open_controlled_login_page,
)


@patch.object(CdpGameFrame, "evaluate", return_value={"text": "游戏圈"})
@patch("config.cdp.httpx.Client")
def test_detects_authorized_plugin_page(mock_client, mock_evaluate):
    response = Mock()
    response.json.return_value = [
        {
            "type": "page",
            "title": "小程序",
            "url": "https://mp.weixin.qq.com/wxamp/frame/pluginRedirect",
        }
    ]
    mock_client.return_value.__enter__.return_value.get.return_value = response

    status = get_browser_login_status()

    assert status.valid is True
    assert status.page_title == "小程序"
    mock_evaluate.assert_called_once()


@patch.object(CdpGameFrame, "evaluate", return_value={"text": "登录超时，请重新登录"})
@patch("config.cdp.httpx.Client")
def test_marks_login_invalid_when_plugin_reports_timeout(mock_client, mock_evaluate):
    response = Mock()
    response.json.return_value = [
        {
            "type": "page",
            "title": "小程序",
            "url": "https://mp.weixin.qq.com/wxamp/frame/pluginRedirect",
        }
    ]
    mock_client.return_value.__enter__.return_value.get.return_value = response

    status = get_browser_login_status()

    assert status.valid is False
    assert status.reason == "游戏圈登录超时，请重新登录"


@patch.object(
    CdpGameFrame,
    "evaluate",
    side_effect=CdpUnavailableError("未找到游戏圈插件 iframe"),
)
@patch("config.cdp.httpx.Client")
def test_marks_login_invalid_when_plugin_iframe_is_missing(mock_client, mock_evaluate):
    response = Mock()
    response.json.return_value = [
        {
            "type": "page",
            "title": "小程序",
            "url": "https://mp.weixin.qq.com/wxamp/frame/pluginRedirect",
        }
    ]
    mock_client.return_value.__enter__.return_value.get.return_value = response

    status = get_browser_login_status()

    assert status.valid is False
    assert status.reason == "游戏圈插件页已退出或登录超时，请重新登录"


@patch.object(CdpGameFrame, "evaluate", return_value={"errcode": -1, "loginExpired": True})
def test_binding_reports_expired_plugin_login(mock_evaluate):
    try:
        bind_game_identity("wx6d2454a8afcd9706")
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
    websocket.close.assert_called_once()
