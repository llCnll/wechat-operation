from unittest.mock import Mock, patch

import json

from config.cdp import CONTROLLED_LOGIN_URL, get_browser_login_status, open_controlled_login_page


@patch("config.cdp.httpx.Client")
def test_detects_authorized_plugin_page(mock_client):
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
