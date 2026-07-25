"""Local-only Chrome DevTools Protocol helpers for the authorized browser session."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from config.settings import CONTROLLED_CHROME_CDP_URL


class CdpUnavailableError(Exception):
    """Base class for safe, classified controlled-CDP failures."""


class CdpBrowserUnavailableError(CdpUnavailableError):
    """The Supervisor-owned Chrome process or browser CDP endpoint is unavailable."""


class CdpTargetUnavailableError(CdpUnavailableError):
    """A connected browser cannot use an explicit owned target or iframe."""


@dataclass(frozen=True)
class ManagedGameDTO:
    appid: str
    name: str


CONTROLLED_LOGIN_URL = "https://mp.weixin.qq.com/cgi-bin/loginpage"


class CdpBrowser:
    """Browser-level target lifecycle; callers must retain every returned target ID."""

    def __init__(self, cdp_url: str = CONTROLLED_CHROME_CDP_URL, timeout: float = 15):
        self.cdp_url = cdp_url.rstrip("/")
        self.timeout = timeout

    def browser_generation(self) -> str:
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(f"{self.cdp_url}/json/version")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CdpBrowserUnavailableError("受控 Chrome CDP 不可用") from exc
        websocket_url = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
        if not isinstance(websocket_url, str) or not websocket_url:
            raise CdpBrowserUnavailableError("受控 Chrome CDP 未返回浏览器标识")
        return sha256(websocket_url.encode()).hexdigest()[:24]

    def close_browser(self) -> None:
        """Request graceful shutdown of the fixed controlled browser endpoint."""
        self._browser_command("Browser.close", {})

    def create_target(self, url: str, *, background: bool = False) -> str:
        if not isinstance(url, str) or not url.startswith("https://mp.weixin.qq.com/"):
            raise CdpTargetUnavailableError("不允许创建非受控公众平台 target")
        target_id = self._browser_command(
            "Target.createTarget", {"url": url, "background": background}
        ).get("targetId", "")
        if not isinstance(target_id, str) or not target_id:
            raise CdpTargetUnavailableError("受控 Chrome 未返回新 target")
        return target_id

    def close_target(self, target_id: str) -> None:
        if not target_id:
            return
        result = self._browser_command("Target.closeTarget", {"targetId": target_id})
        if result.get("success") is not True:
            raise CdpTargetUnavailableError("受控 Chrome target 未确认关闭")

    def activate_target(self, target_id: str) -> None:
        """Make an explicitly operator-requested target visible."""
        if not target_id:
            raise CdpTargetUnavailableError("受控 Chrome target 无效")
        self._browser_command("Target.activateTarget", {"targetId": target_id})

    def target_exists(self, target_id: str) -> bool:
        if not target_id:
            return False
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(f"{self.cdp_url}/json/list")
                response.raise_for_status()
                pages = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CdpBrowserUnavailableError("受控 Chrome CDP 不可用") from exc
        return isinstance(pages, list) and any(
            isinstance(page, dict) and page.get("id") == target_id for page in pages
        )

    def _browser_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(f"{self.cdp_url}/json/version")
                response.raise_for_status()
                metadata = response.json()
            websocket_url = (
                metadata.get("webSocketDebuggerUrl") if isinstance(metadata, dict) else None
            )
            if not isinstance(websocket_url, str) or not websocket_url:
                raise ValueError("missing browser websocket")
            websocket = connect(websocket_url, proxy=None, close_timeout=self.timeout)
        except (httpx.HTTPError, OSError, ValueError, WebSocketException) as exc:
            raise CdpBrowserUnavailableError("受控 Chrome 未启动或无法管理 target") from exc
        try:
            websocket.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                message = json.loads(websocket.recv(timeout=self.timeout))
                if message.get("id") != 1:
                    continue
                result = message.get("result")
                if message.get("error") or not isinstance(result, dict):
                    raise CdpTargetUnavailableError("受控 Chrome target 操作失败")
                return result
        except (OSError, ValueError, WebSocketException) as exc:
            raise CdpTargetUnavailableError("受控 Chrome target 操作失败") from exc
        finally:
            websocket.close()


def open_controlled_login_page(cdp_url: str = CONTROLLED_CHROME_CDP_URL) -> str:
    """Open the verified public-platform login page in the controlled Chrome."""
    try:
        return CdpBrowser(cdp_url=cdp_url, timeout=2).create_target(CONTROLLED_LOGIN_URL)
    except CdpBrowserUnavailableError as exc:
        raise CdpBrowserUnavailableError("受控 Chrome 未启动或无法创建登录页") from exc


class CdpGameFrame:
    """Execute JavaScript only in the authorized gamemp iframe."""

    def __init__(
        self,
        cdp_url: str = CONTROLLED_CHROME_CDP_URL,
        timeout: float = 15,
        *,
        target_id: str,
        frame_id: str | None = None,
    ):
        self.cdp_url = cdp_url.rstrip("/")
        self.timeout = timeout
        self._websocket: Any | None = None
        self._context_id: int | None = None
        self._sequence = 0
        self.target_id = target_id
        self.frame_id = frame_id

    def _find_page(self) -> dict[str, Any]:
        pages = self._find_pages()
        matches = [page for page in pages if page.get("id") == self.target_id]
        if len(matches) == 1:
            return matches[0]
        raise CdpTargetUnavailableError("已登记 CDP target 不存在或不唯一")

    def _find_pages(self) -> list[dict[str, Any]]:
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.get(f"{self.cdp_url}/json/list")
                response.raise_for_status()
                pages = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CdpBrowserUnavailableError("受控 Chrome CDP 不可用") from exc

        if not isinstance(pages, list):
            raise CdpTargetUnavailableError("受控 Chrome 未返回页面列表")
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

    def _gamemp_frame_id(self) -> str | None:
        """Return the current explicit target's gamemp frame ID without selecting another tab."""
        page = self._find_page()
        websocket: Any | None = None
        try:
            websocket = connect(
                page["webSocketDebuggerUrl"], proxy=None, close_timeout=self.timeout
            )
            self._websocket = websocket
            tree = self._call("Page.getFrameTree").get("frameTree", {})
            return self._find_gamemp_frame(tree)
        except (OSError, ValueError, WebSocketException) as exc:
            raise CdpTargetUnavailableError("受控 Chrome target 通信失败") from exc
        finally:
            self.close()

    def has_gamemp_iframe(self) -> bool:
        """Check the explicitly selected page for the expected plugin iframe.

        This deliberately never falls back to another CDP target.  It is used
        while validating a candidate plugin and after an account switch.
        """
        return self._gamemp_frame_id() is not None

    def wait_for_gamemp_iframe(
        self, *, timeout_seconds: float = 10, stability_seconds: float = 2
    ) -> bool:
        """Wait until the explicit iframe survives a short, condition-based stability window."""
        deadline = monotonic() + timeout_seconds
        observed_frame_id: str | None = None
        observed_at: float | None = None
        while True:
            try:
                frame_id = self._gamemp_frame_id()
                if frame_id:
                    if frame_id != observed_frame_id:
                        observed_frame_id = frame_id
                        observed_at = monotonic()
                    elif observed_at is not None and monotonic() - observed_at >= stability_seconds:
                        return True
                else:
                    observed_frame_id = None
                    observed_at = None
            except CdpTargetUnavailableError:
                # Target.createTarget is asynchronous; a just-created target can
                # briefly be absent from /json/list while the browser is healthy.
                observed_frame_id = None
                observed_at = None
            if monotonic() >= deadline:
                return False
            sleep(min(0.2, max(0.0, deadline - monotonic())))

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._websocket is None:
            raise CdpTargetUnavailableError("CDP target 连接未建立")
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
                raise CdpTargetUnavailableError(f"CDP target 调用失败：{method}")
            return message.get("result", {})

    def _ensure_context(self) -> int:
        if self._context_id is not None:
            return self._context_id
        page = self._find_page()
        try:
            self._websocket = connect(
                page["webSocketDebuggerUrl"], proxy=None, close_timeout=self.timeout
            )
            tree = self._call("Page.getFrameTree")
            frame_id = self.frame_id or self._find_gamemp_frame(tree.get("frameTree", {}))
            if not frame_id:
                raise CdpTargetUnavailableError("未找到游戏圈插件 iframe")
            world = self._call(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": "wechat-operations-readonly"},
            )
            context_id = world.get("executionContextId")
            if not context_id:
                raise CdpTargetUnavailableError("无法创建游戏圈 iframe 执行上下文")
            self._context_id = int(context_id)
            return self._context_id
        except (OSError, ValueError, WebSocketException) as exc:
            self.close()
            raise CdpTargetUnavailableError("受控 Chrome target 通信失败") from exc
        except CdpUnavailableError:
            self.close()
            raise

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
            raise CdpTargetUnavailableError("受控 Chrome target 通信失败") from exc

        remote = result.get("result", {})
        if result.get("exceptionDetails") or remote.get("subtype") == "error":
            raise CdpTargetUnavailableError("游戏圈 iframe 请求执行失败")
        if "value" not in remote:
            raise CdpTargetUnavailableError("游戏圈 iframe 未返回 JSON 结果")
        return remote["value"]


class CdpMpPage(CdpGameFrame):
    """Execute read-only code in the outer public-platform page, never in the game iframe."""

    def wait_for_public_page(self, *, timeout_seconds: float = 10) -> bool:
        """Wait until the newly created explicit target has navigated to mp.weixin.qq.com."""
        deadline = monotonic() + timeout_seconds
        while True:
            try:
                self._ensure_context()
                return True
            except CdpTargetUnavailableError:
                self.close()
            if monotonic() >= deadline:
                return False
            sleep(min(0.2, max(0.0, deadline - monotonic())))

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
                raise CdpTargetUnavailableError("未找到受控公众平台页面")
            world = self._call(
                "Page.createIsolatedWorld",
                {"frameId": frame.get("id"), "worldName": "wechat-operations-catalog"},
            )
            context_id = world.get("executionContextId")
            if not context_id:
                raise CdpTargetUnavailableError("无法创建公众平台页面执行上下文")
            self._context_id = int(context_id)
            return self._context_id
        except (OSError, ValueError, WebSocketException) as exc:
            self.close()
            raise CdpTargetUnavailableError("公众平台 target 通信失败") from exc

    def body_text(self) -> str:
        value = self.evaluate("(() => ({text: document.body ? document.body.innerText : ''}))()")
        text = value.get("text") if isinstance(value, dict) else ""
        if not isinstance(text, str):
            raise CdpTargetUnavailableError("公众平台页面状态无效")
        return text


def bind_game_identity(
    appid: str,
    *,
    frame: CdpGameFrame,
) -> str:
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
    value = frame.evaluate(expression)
    if isinstance(value, dict) and value.get("loginExpired") is True:
        raise CdpTargetUnavailableError("游戏圈登录态已失效，请重新登录")
    if not isinstance(value, dict) or value.get("errcode") != 0 or not value.get("token"):
        raise CdpTargetUnavailableError("插件身份绑定失败或未返回 token")
    return str(value["token"])
