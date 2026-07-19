"""Local-only Chrome DevTools Protocol helpers for the authorized browser session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect


class CdpUnavailableError(Exception):
    """Chrome or the authorized game-circle iframe is unavailable."""


@dataclass(frozen=True)
class BrowserLoginStatus:
    valid: bool
    reason: str
    page_title: str = ""


@dataclass(frozen=True)
class ManagedGameDTO:
    appid: str
    name: str


CONTROLLED_LOGIN_URL = (
    "https://mp.weixin.qq.com/wxamp/frame/pluginRedirect/pluginRedirect?title=&action="
    "plugin_redirect&lang=zh_CN&plugin_uin=1029&simple=1&nosidebar=1&custom="
    "path%3D%2Fminigame%40community%2Fminigame%2Fpage%2Fhome"
)


def open_controlled_login_page(cdp_url: str = "http://127.0.0.1:9222") -> None:
    """Open only the fixed, unauthenticated plugin URL in the CDP-controlled Chrome."""
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            response = client.get(f"{cdp_url.rstrip('/')}/json/version")
            response.raise_for_status()
            metadata = response.json()
            websocket_url = (
                metadata.get("webSocketDebuggerUrl") if isinstance(metadata, dict) else None
            )
        if not isinstance(websocket_url, str) or not websocket_url:
            raise ValueError("missing browser websocket")
        websocket = connect(websocket_url, proxy=None, close_timeout=2)
    except (httpx.HTTPError, OSError, ValueError, WebSocketException) as exc:
        raise CdpUnavailableError("受控 Chrome 未启动或无法创建登录页") from exc

    try:
        websocket.send(
            json.dumps(
                {"id": 1, "method": "Target.createTarget", "params": {"url": CONTROLLED_LOGIN_URL}}
            )
        )
        while True:
            message = json.loads(websocket.recv(timeout=2))
            if message.get("id") != 1:
                continue
            result = message.get("result")
            if message.get("error") or not isinstance(result, dict) or not result.get("targetId"):
                raise CdpUnavailableError("受控 Chrome 未能打开登录页")
            return
    except (OSError, ValueError, WebSocketException) as exc:
        raise CdpUnavailableError("受控 Chrome 未能打开登录页") from exc
    finally:
        websocket.close()


class CdpGameFrame:
    """Execute JavaScript only in the authorized gamemp iframe."""

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222", timeout: float = 15):
        self.cdp_url = cdp_url.rstrip("/")
        self.timeout = timeout
        self._websocket: Any | None = None
        self._context_id: int | None = None
        self._sequence = 0

    def _find_page(self) -> dict[str, Any]:
        pages = self._find_pages()
        if not pages:
            raise CdpUnavailableError("未找到受控微信页面")
        return pages[0]

    def _find_pages(self) -> list[dict[str, Any]]:
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(f"{self.cdp_url}/json/list")
                response.raise_for_status()
                pages = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CdpUnavailableError("Chrome CDP 不可用") from exc

        if not isinstance(pages, list):
            raise CdpUnavailableError("未找到受控微信页面")
        return [
            item
            for item in pages
            if isinstance(item, dict)
            and item.get("type") == "page"
            and urlsplit(str(item.get("url", ""))).netloc == "mp.weixin.qq.com"
            and item.get("webSocketDebuggerUrl")
        ]

    @staticmethod
    def _find_gamemp_frame(frame_tree: dict[str, Any]) -> str | None:
        frame = frame_tree.get("frame", {})
        if urlsplit(str(frame.get("url", ""))).netloc == "gamemp.weixin.qq.com":
            return str(frame.get("id"))
        for child in frame_tree.get("childFrames", []):
            frame_id = CdpGameFrame._find_gamemp_frame(child)
            if frame_id:
                return frame_id
        return None

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._websocket is None:
            raise CdpUnavailableError("CDP 连接未建立")
        self._sequence += 1
        identifier = self._sequence
        self._websocket.send(
            json.dumps({"id": identifier, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self._websocket.recv(timeout=self.timeout))
            if message.get("id") != identifier:
                continue
            if "error" in message:
                raise CdpUnavailableError(f"CDP 调用失败：{method}")
            return message.get("result", {})

    def _ensure_context(self) -> int:
        if self._context_id is not None:
            return self._context_id
        for page in self._find_pages():
            try:
                self._websocket = connect(
                    page["webSocketDebuggerUrl"], proxy=None, close_timeout=self.timeout
                )
                tree = self._call("Page.getFrameTree")
                frame_id = self._find_gamemp_frame(tree.get("frameTree", {}))
                if not frame_id:
                    self.close()
                    continue
                world = self._call(
                    "Page.createIsolatedWorld",
                    {"frameId": frame_id, "worldName": "wechat-operations-readonly"},
                )
                context_id = world.get("executionContextId")
                if not context_id:
                    raise CdpUnavailableError("无法创建游戏圈 iframe 执行上下文")
                self._context_id = int(context_id)
                return self._context_id
            except (OSError, ValueError, WebSocketException) as exc:
                self.close()
                raise CdpUnavailableError("Chrome CDP 通信失败") from exc
            except CdpUnavailableError:
                self.close()
                raise
        raise CdpUnavailableError("未找到游戏圈插件 iframe")

    def close(self) -> None:
        if self._websocket is not None:
            self._websocket.close()
        self._websocket = None
        self._context_id = None
        self._sequence = 0

    def evaluate(self, expression: str) -> Any:
        """Evaluate an async expression in gamemp and return JSON-serializable output."""
        try:
            context_id = self._ensure_context()
            result = self._call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": context_id,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
        except (OSError, ValueError, WebSocketException) as exc:
            self.close()
            raise CdpUnavailableError("Chrome CDP 通信失败") from exc

        remote = result.get("result", {})
        if result.get("exceptionDetails") or remote.get("subtype") == "error":
            raise CdpUnavailableError("游戏圈 iframe 请求执行失败")
        if "value" not in remote:
            raise CdpUnavailableError("游戏圈 iframe 未返回 JSON 结果")
        return remote["value"]


class CdpMpPage(CdpGameFrame):
    """Execute read-only code in the outer public-platform page, never in the game iframe."""

    def _ensure_context(self) -> int:
        if self._context_id is not None:
            return self._context_id
        page = self._find_page()
        try:
            self._websocket = connect(
                page["webSocketDebuggerUrl"], proxy=None, close_timeout=self.timeout
            )
            tree = self._call("Page.getFrameTree").get("frameTree", {})
            frame = tree.get("frame", {})
            if urlsplit(str(frame.get("url", ""))).netloc != "mp.weixin.qq.com":
                raise CdpUnavailableError("未找到受控公众平台页面")
            world = self._call(
                "Page.createIsolatedWorld",
                {"frameId": frame.get("id"), "worldName": "wechat-operations-catalog"},
            )
            context_id = world.get("executionContextId")
            if not context_id:
                raise CdpUnavailableError("无法创建公众平台页面执行上下文")
            self._context_id = int(context_id)
            return self._context_id
        except (OSError, ValueError, WebSocketException) as exc:
            self.close()
            raise CdpUnavailableError("公众平台 CDP 通信失败") from exc


def list_managed_games(cdp_url: str = "http://127.0.0.1:9222") -> list[ManagedGameDTO]:
    """Read the current account's game list without exposing page credentials."""
    expression = """(async () => {
        const token = new URL(window.location.href).searchParams.get('token');
        if (!token) return {ok: false};
        const response = await fetch('/wxamp/cgi/getWxaList?token=' + encodeURIComponent(token)
          + '&lang=zh_CN&random=' + Math.random(), {credentials: 'include'});
        const payload = await response.json();
        const records = Array.isArray(payload.wax_list) ? payload.wax_list
          : Array.isArray(payload.data && payload.data.list) ? payload.data.list : [];
        const games = records.filter(item => item && item.appid)
          .map(item => ({
            appid: String(item.appid),
            name: String(item.app_name || item.nickname || item.name || ''),
          }))
          .filter(item => item.name);
        return {ok: payload.ret === 0 || payload.errcode === 0, games};
    })()"""
    page = CdpMpPage(cdp_url)
    try:
        value = page.evaluate(expression)
    finally:
        page.close()
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise CdpUnavailableError("获取可管理游戏列表失败或会话无效")
    records = _game_records(value.get("games"))
    if not records:
        raise CdpUnavailableError("可管理游戏列表响应结构无效")
    return records


def _game_records(payload: object) -> list[ManagedGameDTO]:
    """Validate the minimal projected records returned from the page context."""
    if not isinstance(payload, list):
        return []
    games = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        appid, name = item.get("appid"), item.get("name")
        if isinstance(appid, str) and appid and isinstance(name, str) and name:
            games.append(ManagedGameDTO(appid, name))
    return games


def get_browser_login_status(cdp_url: str = "http://127.0.0.1:9222") -> BrowserLoginStatus:
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            response = client.get(f"{cdp_url.rstrip('/')}/json/list")
            response.raise_for_status()
            pages = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CdpUnavailableError("Chrome CDP 不可用") from exc

    if not isinstance(pages, list):
        raise CdpUnavailableError("Chrome CDP 返回无效页面列表")
    mp_pages = [
        page
        for page in pages
        if isinstance(page, dict)
        and page.get("type") == "page"
        and urlsplit(str(page.get("url", ""))).netloc == "mp.weixin.qq.com"
    ]
    if not mp_pages:
        return BrowserLoginStatus(False, "未找到微信受控页面")
    title = next(
        (
            str(page.get("title", ""))
            for page in mp_pages
            if str(page.get("title", "")) not in {"", "微信公众平台", "登录"}
        ),
        str(mp_pages[0].get("title", "")),
    )
    if not title or title in {"微信公众平台", "登录"}:
        return BrowserLoginStatus(False, "微信页面等待扫码登录", title)

    frame = CdpGameFrame(cdp_url, timeout=2)
    try:
        value = frame.evaluate("(() => ({text: document.body ? document.body.innerText : ''}))()")
    except CdpUnavailableError:
        return BrowserLoginStatus(False, "游戏圈插件页已退出或登录超时，请重新登录", title)
    finally:
        frame.close()
    if not isinstance(value, dict):
        return BrowserLoginStatus(False, "游戏圈插件页状态无效，请重新登录", title)
    text = value.get("text", "")
    if isinstance(text, str) and ("登录超时" in text or "请重新登录" in text):
        return BrowserLoginStatus(False, "游戏圈登录超时，请重新登录", title)
    return BrowserLoginStatus(True, "已检测到有效的受控微信插件页", title)


def bind_game_identity(appid: str, cdp_url: str = "http://127.0.0.1:9222") -> str:
    """Bind one game identity and return its ephemeral token without persisting it."""
    body = {
        "biz_type": 1,
        "identity": {"mini_game_identity": {"appid": appid}},
        "scene": 1,
    }
    expression = f"""(async () => {{
        const response = await fetch('/cgi-bin/gameopenmpwap/binduseridentity?auth_type=4', {{
            method: 'POST', credentials: 'include',
            headers: {{'content-type': 'application/json'}},
            body: JSON.stringify({json.dumps(body)})
        }});
        const payload = await response.json();
        const pageText = document.body ? document.body.innerText : '';
        return {{
            errcode: payload.errcode,
            token: payload.data && payload.data.token,
            loginExpired: pageText.includes('登录超时') || pageText.includes('请重新登录')
        }};
    }})()"""
    frame = CdpGameFrame(cdp_url)
    try:
        value = frame.evaluate(expression)
    finally:
        frame.close()
    if isinstance(value, dict) and value.get("loginExpired") is True:
        raise CdpUnavailableError("游戏圈登录态已失效，请重新登录")
    if not isinstance(value, dict) or value.get("errcode") != 0 or not value.get("token"):
        raise CdpUnavailableError("插件身份绑定失败或未返回 token")
    return str(value["token"])
