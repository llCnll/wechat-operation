"""Read-only private metric endpoints owned by the reporting domain."""

from __future__ import annotations

import json
from time import monotonic, sleep
from collections.abc import Callable
from typing import Any

from config.cdp import CdpGameFrame, CdpTargetUnavailableError, CdpUnavailableError
from jobs.adapters import PluginSessionExpiredError, SourceAuthenticationRejectedError, SourceContractError, SourceTransportError


class CdpGameCircleMetricClient:
    """Use the authorized iframe for aggregate metrics without sharing post APIs."""

    def __init__(
        self,
        *,
        appid: str,
        min_interval_seconds: float = 0.12,
        frame: CdpGameFrame,
        identity_token: str,
        rebind_identity: Callable[[], str] | None = None,
    ):
        if not appid:
            raise SourceContractError("指标读取缺少游戏 appid")
        self.appid = appid
        if not identity_token:
            raise SourceContractError("缺少受控来源身份会话")
        self.frame = frame
        self.token = identity_token
        self._rebind_identity = rebind_identity
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        delay = self.min_interval_seconds - (monotonic() - self._last_request_at)
        if delay > 0:
            sleep(delay)
        method = "POST" if body is not None else "GET"
        query_json = json.dumps(params or {}, ensure_ascii=False)
        body_json = json.dumps(body, ensure_ascii=False) if body is not None else None
        expression = f"""(async () => {{
            const url = new URL({json.dumps(path)}, window.location.origin);
            for (const [key, value] of Object.entries({query_json})) {{
                url.searchParams.set(key, String(value));
            }}
            url.searchParams.set('token', "__SOURCE_TOKEN__");
            url.searchParams.set('auth_type', '4');
            const response = await fetch(url.toString(), {{
                method: {json.dumps(method)}, credentials: 'include',
                headers: {{
                    'content-type': 'application/json;charset=UTF-8',
                    'x-requested-with': 'XMLHttpRequest'
                }},
                body: {json.dumps(body_json)}
            }});
            const text = await response.text();
            try {{ return {{status: response.status, payload: JSON.parse(text)}}; }}
            catch (_) {{ return {{status: response.status, payload: null}}; }}
        }})()"""
        for attempt in range(2):
            try:
                result = self.frame.evaluate(
                    expression.replace('"__SOURCE_TOKEN__"', json.dumps(self.token))
                )
            except CdpTargetUnavailableError as exc:
                raise PluginSessionExpiredError("已登记游戏圈 target 不可用") from exc
            except CdpUnavailableError as exc:
                raise SourceTransportError("指标接口 CDP 请求失败") from exc
            self._last_request_at = monotonic()
            if isinstance(result, dict) and result.get("status") == 401:
                raise SourceAuthenticationRejectedError("指标请求被认证拒绝")
            if not isinstance(result, dict) or result.get("status") != 200:
                raise SourceTransportError("指标接口传输失败")
            payload = result.get("payload")
            if not isinstance(payload, dict):
                raise SourceContractError("指标接口响应非 JSON")
            if payload.get("errcode") != -2:
                return payload
            if attempt == 0 and self._rebind_identity is not None:
                self.token = self._rebind_identity()
                continue
            raise PluginSessionExpiredError("来源身份会话已失效")
        raise AssertionError("来源身份重绑循环不应到达此处")

    def circle_fans(
        self, *, start_ds: int, end_ds: int, cur_page: int = 0, per_page: int = 366
    ) -> dict[str, Any]:
        return self._request(
            "/cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_datacenter_getgroupuserdetaildata",
            body={
                "type": 1,
                "start_ds": start_ds,
                "end_ds": end_ds,
                "desc_order": False,
                "cur_page": cur_page,
                "per_page": per_page,
            },
        )

    def content_fans(
        self,
        *,
        metric_type: int,
        begin_time: int,
        end_time: int,
        cur_page: int = 0,
        per_page: int = 366,
    ) -> dict[str, Any]:
        return self._request(
            "/cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_dataauthor_getauthordetaildata",
            body={
                "type": metric_type,
                "begin_time": begin_time,
                "end_time": end_time,
                "cur_page": cur_page,
                "per_page": per_page,
            },
        )

    def operation_effect(
        self, *, ds_begin: int, ds_end: int, cur_page: int = 0, per_page: int = 366
    ) -> dict[str, Any]:
        return self._request(
            "/cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_gamegroupmap_getdetaildata",
            params={
                "data": json.dumps(
                    {
                        "ds_begin": ds_begin,
                        "ds_end": ds_end,
                        "appid": self.appid,
                        "cur_page": cur_page,
                        "per_page": per_page,
                    },
                    separators=(",", ":"),
                )
            },
        )

    def group_content_analysis(
        self,
        *,
        start_ds: int,
        end_ds: int,
        data_flag: str,
        cur_page: int = 0,
        per_page: int = 366,
    ) -> dict[str, Any]:
        return self._request(
            "/cgi-bin/gameopenmpwap/cfcommcgi/spacexgamemp_datacenter_getgroupconsumedetaildata",
            params={
                "data": json.dumps(
                    {
                        "start_ds": start_ds,
                        "end_ds": end_ds,
                        "desc_order": False,
                        "cur_page": cur_page,
                        "per_page": per_page,
                        data_flag: True,
                    },
                    separators=(",", ":"),
                )
            },
        )

    def close(self) -> None:
        self.frame.close()
