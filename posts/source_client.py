"""Read-only game-circle clients backed by the authorized Chrome iframe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any

from config.cdp import CdpGameFrame, CdpUnavailableError, bind_game_identity
from jobs.adapters import (
    CandidatePostDTO,
    SourceBusinessDeletedError,
    SourceContractError,
    SourceTransportError,
)


class CdpGameCircleClient:
    """Private upstream reads executed in Chrome; no credential is stored by this service."""

    def __init__(
        self,
        *,
        appid: str,
        cdp_url: str = "http://127.0.0.1:9222",
        min_interval_seconds: float = 0.12,
    ):
        if not appid:
            raise SourceContractError("缺少游戏 appid")
        self.appid = appid
        self.frame = CdpGameFrame(cdp_url)
        self.token = bind_game_identity(appid, cdp_url)
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    def _request_path(
        self, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        http_method = "POST" if body is not None else "GET"
        delay = self.min_interval_seconds - (monotonic() - self._last_request_at)
        if delay > 0:
            sleep(delay)
        encoded_path = json.dumps(path)
        body_text = json.dumps(body, ensure_ascii=False) if body is not None else None
        encoded_body = json.dumps(body_text)
        expression = f"""(async () => {{
            const path = {encoded_path};
            const url = new URL(path, window.location.origin);
            const params = {json.dumps(params or {}, ensure_ascii=False)};
            for (const [key, value] of Object.entries(params)) {{
                url.searchParams.set(key, String(value));
            }}
            url.searchParams.set('token', {json.dumps(self.token)});
            url.searchParams.set('auth_type', '4');
            const response = await fetch(url.toString(), {{
                method: {json.dumps(http_method)}, credentials: 'include',
                headers: {{
                    'content-type': 'application/json;charset=UTF-8',
                    'x-requested-with': 'XMLHttpRequest'
                }},
                body: {encoded_body}
            }});
            const text = await response.text();
            try {{ return {{status: response.status, payload: JSON.parse(text)}}; }}
            catch (_) {{ return {{status: response.status, payload: null}}; }}
        }})()"""
        try:
            result = self.frame.evaluate(expression)
        except CdpUnavailableError as exc:
            raise SourceTransportError("原平台 CDP 请求失败") from exc
        self._last_request_at = monotonic()
        if not isinstance(result, dict) or not isinstance(result.get("payload"), dict):
            raise SourceContractError("原平台响应非 JSON")
        payload = result["payload"]
        if result.get("status") != 200 or payload.get("errcode") != 0:
            if path.endswith("/topicsearchgetdetail") and payload.get("errcode") == -10075007:
                raise SourceBusinessDeletedError("原平台帖子已从管理队列删除")
            raise SourceTransportError("原平台返回错误")
        return payload

    def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request_path("/cgi-bin/gamecircleopenmpweb/" + method, params=params)

    def _post_json(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_path("/cgi-bin/gamecircleopenmpweb/" + method, body=body)

    def _post_gameopenmpwap(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_path("/cgi-bin/gameopenmpwap/" + method, body=body)

    def list_candidates(
        self, *, page: int, page_size: int, start_time: int, end_time: int
    ) -> list[CandidatePostDTO]:
        payload = self._get(
            "gettopiclist",
            {
                "data": json.dumps(
                    {
                        "req": {
                            "page": page,
                            "page_size": page_size,
                            "skip_check_author": True,
                            "order_by": 1,
                            "is_desc": True,
                            "start_time": start_time,
                            "end_time": end_time,
                        }
                    },
                    separators=(",", ":"),
                )
            },
        )
        try:
            records = payload["rsp"]["topic_list"]
            if not isinstance(records, list):
                raise TypeError("topic_list is not a list")
            return [
                CandidatePostDTO(
                    topic_id=int(record["topic_id"]),
                    content_id=str(record.get("content_id", "")),
                    source_appid=str(record.get("appid", self.appid)),
                    publish_time=(
                        datetime.fromtimestamp(int(record["publish_time"]), tz=UTC)
                        if record.get("publish_time")
                        else None
                    ),
                )
                for record in records
                if isinstance(record, dict)
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SourceContractError("gettopiclist 响应结构无效") from exc

    def get_content_card(self, content_id: str) -> dict[str, Any]:
        if not content_id:
            raise SourceContractError("缺少 content_id")
        return self._get(
            "getcontentcarddetail",
            {"content_id": content_id, "need_comment": "false", "preview_key": ""},
        )

    def get_post_metrics(self, topic_id: int) -> dict[str, Any]:
        return self._get(
            "topicsearchgetdetail",
            {"data": json.dumps({"topic_id": topic_id, "need_topic": True, "limit": 0})},
        )

    def get_post_metrics_with_timeout(
        self, topic_id: int, *, timeout_seconds: float
    ) -> dict[str, Any]:
        """Bound a single detail read without changing the client's normal read timeout."""
        if timeout_seconds <= 0:
            raise SourceTransportError("详情读取已超过允许的验证时间")
        original_timeout = self.frame.timeout
        self.frame.timeout = min(original_timeout, timeout_seconds)
        try:
            return self.get_post_metrics(topic_id)
        finally:
            self.frame.timeout = original_timeout

    def perform_confirmed_operation(
        self, *, topic_id: int, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute only the independently verified topic status operations."""
        if topic_id <= 0:
            raise SourceContractError("帖子 ID 无效")
        if action == "SINK":
            request_body = {"topic_id": topic_id, "status": 8}
        elif action == "RESTORE":
            request_body = {"topic_id": topic_id, "status": 2}
        elif action == "BLOCK":
            reason = payload.get("block_reason_code")
            if not isinstance(reason, int) or reason <= 0 or reason & ~0b11111:
                raise SourceContractError("屏蔽原因码无效或未验证")
            request_body = {
                "topic_id": topic_id,
                "status": 4,
                "block_info": {"reason": reason},
            }
        elif action == "MUTE":
            reason = payload.get("block_reason_code")
            end_time = payload.get("mute_end_time")
            if not isinstance(reason, int) or reason <= 0 or reason & ~0b11111:
                raise SourceContractError("禁言原因码无效或未验证")
            if not isinstance(end_time, int) or end_time <= 0:
                raise SourceContractError("禁言截止时间无效")
            account_openid = payload.get("author_openid")
            if not isinstance(account_openid, str) or not account_openid:
                metrics = self.get_post_metrics(topic_id)
                try:
                    account_openid = metrics["data"]["topic"]["creater"]["openid"]
                except (KeyError, TypeError) as exc:
                    raise SourceContractError("无法从当前帖子读取禁言目标") from exc
            if not isinstance(account_openid, str) or not account_openid:
                raise SourceContractError("禁言目标身份标识无效")
            request_body = {
                "account_openid": account_openid,
                "end_time": end_time,
                "block_reason_type": reason,
                "reason": str(payload.get("reason", "")),
            }
            return self._post_gameopenmpwap(
                "cfcommcgi/spacexcontent_gamecircletopicmanage_topicblockuser",
                request_body,
            )
        elif action == "UNMUTE":
            account_openid = payload.get("author_openid")
            if not isinstance(account_openid, str) or not account_openid:
                metrics = self.get_post_metrics(topic_id)
                try:
                    account_openid = metrics["data"]["topic"]["creater"]["openid"]
                except (KeyError, TypeError) as exc:
                    raise SourceContractError("无法从当前帖子读取解除禁言目标") from exc
            if not isinstance(account_openid, str) or not account_openid:
                raise SourceContractError("解除禁言目标身份标识无效")
            return self._post_gameopenmpwap(
                "cfcommcgi/spacexcontent_gamecircletopicmanage_topicunblockuser",
                {"account_openid": account_openid, "module": "mmgamegamecirclemanageweb"},
            )
        else:
            raise SourceContractError("该上游操作尚未验证")
        return self._post_json("topicsearchblock", request_body)

    def close(self) -> None:
        self.frame.close()


# Backward-compatible name retained while callers move to the CDP-only client.
WechatGameCircleClient = CdpGameCircleClient
