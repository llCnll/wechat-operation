"""Explicit-target CDP adapter for the verified game-circle outer-page protocol.

Authentication values are deliberately created only inside JavaScript evaluation and
are never returned except for the ephemeral source-identity binding path owned by
the session coordinator.
"""

from __future__ import annotations

from time import monotonic, sleep
from urllib.parse import urlencode
from typing import Any

from config.cdp import CdpMpPage
from config.settings import CONTROLLED_CHROME_CDP_URL


class GameCircleContractError(RuntimeError):
    """A verified upstream response did not satisfy its safe projection contract."""

    def __init__(self, message: str, *, safe_reason: str = "CONTRACT_INVALID") -> None:
        super().__init__(message)
        self.safe_reason = safe_reason


class OuterSessionUnreadyError(GameCircleContractError):
    """A fresh HOME exists but does not expose a usable outer session token."""


class GameSelectionError(GameCircleContractError):
    """The current outer account cannot select a requested managed game."""


class ExplicitOuterPage(CdpMpPage):
    """A root-frame executor that connects only to a coordinator-owned target ID."""

    def __init__(self, *, target_id: str, cdp_url: str = CONTROLLED_CHROME_CDP_URL, timeout: float = 15):
        if not target_id:
            raise GameCircleContractError("缺少受控 HOME target")
        super().__init__(cdp_url=cdp_url, timeout=timeout, target_id=target_id)

    def _find_page(self) -> dict[str, Any]:
        pages = self._find_pages()
        matches = [page for page in pages if page.get("id") == self.target_id]
        if len(matches) != 1:
            raise GameCircleContractError("已登记 HOME target 不存在或不唯一")
        return matches[0]


class GameCircleOuterAdapter:
    """Projection of only the externally verified outer-page methods."""

    def __init__(self, page: ExplicitOuterPage):
        self.page = page

    def read_outer_token(self) -> str:
        value = self.page.evaluate(
            "(() => ({token: new URL(window.location.href).searchParams.get('token')}))()"
        )
        token = value.get("token") if isinstance(value, dict) else None
        if not isinstance(token, str) or not token:
            raise OuterSessionUnreadyError("当前 HOME 缺少运行时 token")
        return token

    def scan_required(self) -> bool:
        text = self.page.body_text()
        return any(marker in text for marker in ("扫码登录", "请使用微信扫描"))

    def _wxa_list_response(self) -> dict[str, Any]:
        value = self.page.evaluate(
            """(async () => {
              const token = new URL(window.location.href).searchParams.get('token');
              if (!token) return {kind: 'missing_token'};
              const response = await fetch('/wxamp/cgi/getWxaList?token=' + encodeURIComponent(token)
                + '&lang=zh_CN&random=' + Math.random(), {credentials: 'include'});
              const text = await response.text();
              try {
                const payload = JSON.parse(text);
                const records = Array.isArray(payload.wax_list) ? payload.wax_list : [];
                return {kind: 'response', status: response.status, ret: payload.ret,
                  errcode: payload.errcode, appids: records.map(item => item && item.appid)
                    .filter(Boolean).map(String),
                  games: records.map(item => item && item.appid && ({appid: String(item.appid),
                    name: String(item.app_name || item.nickname || item.name || '')})).filter(Boolean),
                  switchPermitted: payload.account_switch_permit === 1
                    || payload.account_switch_permit === '1'
                    || payload.account_switch_permit === true};
              } catch (_) { return {kind: 'non_json', status: response.status}; }
            })()"""
        )
        if not isinstance(value, dict) or value.get("kind") != "response":
            raise GameCircleContractError("游戏列表响应无效")
        if value.get("status") != 200 or not (value.get("ret") == 0 or value.get("errcode") == 0):
            raise GameCircleContractError("游戏列表业务失败")
        return value

    def list_managed_games(self) -> list[dict[str, str]]:
        records = self._wxa_list_response().get("games")
        if not isinstance(records, list):
            raise GameCircleContractError("游戏列表结构无效")
        games: list[dict[str, str]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            appid, name = record.get("appid"), record.get("name")
            if isinstance(appid, str) and appid and isinstance(name, str) and name:
                games.append({"appid": appid, "name": name})
        if not games:
            raise GameCircleContractError("游戏列表结构无效")
        return games

    def read_current_game(self) -> str:
        """Read the current game from the verified outer-page contract."""
        value = self.page.evaluate(
            """(async () => {
              const response = await fetch('https://gamemp.weixin.qq.com/cgi-bin/gamewxagsettingwap/getappinfo', {credentials: 'include'});
              const text = await response.text();
              try { const payload = JSON.parse(text); return {status: response.status, errcode: payload.errcode,
                appid: payload.data && payload.data.app_info && payload.data.app_info.appid}; }
              catch (_) { return {status: response.status, nonJson: true}; }
            })()"""
        )
        if not isinstance(value, dict):
            raise GameCircleContractError(
                "当前游戏回读失败：CDP 返回结构无效", safe_reason="INVALID_EVALUATION_RESULT"
            )
        status = value.get("status")
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise GameCircleContractError(
                "当前游戏回读失败：来源 HTTP 状态异常", safe_reason="HTTP_STATUS"
            )
        if status != 200:
            raise GameCircleContractError(
                f"当前游戏回读失败：来源 HTTP {status}", safe_reason="HTTP_STATUS"
            )
        if value.get("nonJson") is True:
            raise GameCircleContractError(
                "当前游戏回读失败：来源响应不是 JSON", safe_reason="NON_JSON_RESPONSE"
            )
        if value.get("errcode") != 0:
            raise GameCircleContractError(
                "当前游戏回读失败：来源业务拒绝", safe_reason="BUSINESS_REJECTED"
            )
        appid = value.get("appid")
        if not isinstance(appid, str) or not appid:
            raise GameCircleContractError(
                "当前游戏回读失败：来源载荷无有效游戏", safe_reason="INVALID_PAYLOAD"
            )
        return appid

    def wait_for_readable_current_game(self, *, timeout_seconds: float = 10) -> str:
        """Wait for the verified current-game contract, not merely page navigation."""
        deadline = monotonic() + timeout_seconds
        last_error: GameCircleContractError | None = None
        while True:
            try:
                return self.read_current_game()
            except GameCircleContractError as exc:
                last_error = exc
            if monotonic() >= deadline:
                if last_error is not None:
                    raise GameCircleContractError(
                        f"当前游戏回读超时：{last_error}", safe_reason=last_error.safe_reason
                    ) from last_error
                raise GameCircleContractError("当前游戏回读超时")
            sleep(min(0.2, max(0.0, deadline - monotonic())))

    def switch_to_game(self, target_appid: str) -> None:
        """Perform one direct, directory-validated outer-account switch."""
        listing = self._wxa_list_response()
        appids = listing.get("appids")
        if not isinstance(appids, list) or appids.count(target_appid) != 1:
            raise GameSelectionError("当前受控账号不支持切换至任务游戏")
        if listing.get("switchPermitted") is not True:
            raise GameSelectionError("当前受控账号不允许切换游戏")
        value = self.page.evaluate(
            """(async (targetAppid) => {
              const token = new URL(window.location.href).searchParams.get('token');
              if (!token) return {kind: 'missing_token'};
              const response = await fetch('/wxamp/cgi/switchAccount?token=' + encodeURIComponent(token), {
                method: 'POST', credentials: 'include',
                headers: {'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'},
                body: new URLSearchParams({appid: targetAppid}),
              });
              const text = await response.text();
              try { const payload = JSON.parse(text); return {kind: 'response', status: response.status,
                ret: payload.ret, errcode: payload.errcode}; }
              catch (_) { return {kind: 'non_json', status: response.status}; }
            })(%s)""" % repr(target_appid)
        )
        if not isinstance(value, dict) or value.get("kind") != "response":
            raise GameCircleContractError("游戏切换响应无效")
        if value.get("status") != 200 or not (value.get("ret") == 0 or value.get("errcode") == 0):
            raise GameCircleContractError("游戏切换业务失败")

    def list_switchable_game_appids(self) -> list[str]:
        """Return raw directory AppIDs only after the platform permits switching.

        The coordinator intentionally keeps duplicate entries: uniqueness is a
        safety decision for the caller, rather than an accidental side effect of
        de-duplicating an upstream response.
        """
        listing = self._wxa_list_response()
        appids = listing.get("appids")
        if not isinstance(appids, list) or not all(isinstance(appid, str) and appid for appid in appids):
            raise GameCircleContractError("游戏列表结构无效")
        if listing.get("switchPermitted") is not True:
            raise GameSelectionError("当前受控账号不允许切换游戏")
        return appids

    def reload_after_game_switch(self) -> None:
        """Reload after a switch and discard the navigation-invalidated context.

        The outer page's isolated execution context belongs to the pre-switch
        document.  It must not be reused for the read-back or token read of the
        new account; the next adapter call will attach a fresh context to the
        same explicit HOME target.
        """
        self.page.evaluate("(() => { window.location.assign('/'); return {scheduled: true}; })()")
        self.page.close()

    def wait_for_current_game(self, target_appid: str, *, timeout_seconds: float = 10) -> None:
        """Wait for the single HOME target to finish its switch-triggered reload."""
        deadline = monotonic() + timeout_seconds
        while True:
            try:
                remaining = max(0.0, deadline - monotonic())
                if self.wait_for_readable_current_game(timeout_seconds=remaining) == target_appid:
                    return
            except GameCircleContractError:
                pass
            if monotonic() >= deadline:
                raise GameCircleContractError("切换后当前游戏回读失败")
            sleep(min(0.2, max(0.0, deadline - monotonic())))

def plugin_redirect_url(runtime_token: str) -> str:
    """Construct the fixed plugin route; the token remains only in the call stack."""
    if not runtime_token:
        raise GameCircleContractError("插件路由缺少运行时 token")
    return "https://mp.weixin.qq.com/wxamp/frame/pluginRedirect/pluginRedirect?" + urlencode(
        {
            "title": "",
            "action": "plugin_redirect",
            "token": runtime_token,
            "lang": "zh_CN",
            "plugin_uin": "1029",
            "simple": "1",
            "nosidebar": "1",
            "custom": "path=/minigame@community/minigame/page/home",
        }
    )
