"""Read-only game-circle clients backed by the authorized Chrome iframe."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any

from config.cdp import CdpGameFrame, CdpTargetUnavailableError, CdpUnavailableError
from jobs.adapters import (
    CandidatePostDTO,
    PluginSessionExpiredError,
    SourceAuthenticationRejectedError,
    SourceBusinessDeletedError,
    SourceContractError,
    SourceTransportError,
)

BLOCKED_USER_LIST_PAGE_SIZE_REJECTED = "BLOCKED_USER_LIST_PAGE_SIZE_REJECTED"
_PAGE_SIZE_ERROR_MARKERS = ("page_size", "page size", "分页大小", "页大小")


class CdpGameCircleClient:
    """Private upstream reads executed in Chrome; no credential is stored by this service."""

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
            raise SourceContractError("缺少游戏 appid")
        self.appid = appid
        if not identity_token:
            raise SourceContractError("缺少受控来源身份会话")
        self.frame = frame
        self.token = identity_token
        self._rebind_identity = rebind_identity
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    def _request_path(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        allow_application_error: bool = False,
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
            url.searchParams.set('token', "__SOURCE_TOKEN__");
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
            try {{
                return {{
                    status: response.status,
                    payload: JSON.parse(text),
                    response_kind: response.headers.get('content-type')?.includes('application/json')
                        ? 'DECLARED_JSON' : 'OTHER',
                    redirected: Boolean(response.redirected)
                }};
            }} catch (_) {{
                return {{
                    status: response.status,
                    payload: null,
                    response_kind: response.headers.get('content-type')?.includes('application/json')
                        ? 'DECLARED_JSON' : 'OTHER',
                    redirected: Boolean(response.redirected)
                }};
            }}
        }})()"""
        for attempt in range(2):
            try:
                result = self.frame.evaluate(
                    expression.replace('"__SOURCE_TOKEN__"', json.dumps(self.token))
                )
            except CdpTargetUnavailableError as exc:
                raise PluginSessionExpiredError("已登记游戏圈 target 不可用") from exc
            except CdpUnavailableError as exc:
                raise SourceTransportError("原平台 CDP 请求失败") from exc
            self._last_request_at = monotonic()
            if not isinstance(result, dict):
                raise SourceContractError(
                    "来源 CDP 返回结构无效", safe_reason="INVALID_EVALUATION_RESULT"
                )
            if result.get("status") == 401:
                raise SourceAuthenticationRejectedError("来源请求被认证拒绝")
            if not isinstance(result.get("payload"), dict):
                raise SourceContractError("来源详情响应不是 JSON", safe_reason="NON_JSON_RESPONSE")
            payload = result["payload"]
            if result.get("status") == 200 and payload.get("errcode") == 0:
                return payload
            if payload.get("errcode") == -2:
                if attempt == 0 and self._rebind_identity is not None:
                    self.token = self._rebind_identity()
                    continue
                raise PluginSessionExpiredError("来源身份会话已失效")
            if allow_application_error:
                return payload
            if path.endswith("/topicsearchgetdetail") and payload.get("errcode") == -10075007:
                raise SourceBusinessDeletedError("原平台帖子已从管理队列删除")
            raise SourceTransportError("原平台返回错误")
        raise AssertionError("来源身份重绑循环不应到达此处")

    def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request_path("/cgi-bin/gamecircleopenmpweb/" + method, params=params)

    def _get_gameopenmpwap(
        self, method: str, params: dict[str, Any], *, allow_application_error: bool = False
    ) -> dict[str, Any]:
        """Read one fixed game-management endpoint through the bound frame."""
        return self._request_path(
            "/cgi-bin/gameopenmpwap/" + method,
            params=params,
            allow_application_error=allow_application_error,
        )

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
        return self._parse_candidate_list(payload)

    def get_candidate_by_topic_id(self, topic_id: int) -> CandidatePostDTO | None:
        """Resolve one known topic through the fixed candidate-list contract."""
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id <= 0:
            raise SourceContractError("帖子 ID 无效")
        payload = self._get(
            "gettopiclist",
            {
                "data": json.dumps(
                    {
                        "req": {
                            "topic_id": topic_id,
                            "page": 0,
                            "page_size": 10,
                            "skip_check_author": True,
                            "order_by": 1,
                            "is_desc": True,
                        }
                    },
                    separators=(",", ":"),
                )
            },
        )
        matches = [item for item in self._parse_candidate_list(payload) if item.topic_id == topic_id]
        if not matches:
            return None
        if len(matches) != 1:
            raise SourceContractError("指定帖子候选响应不唯一")
        return matches[0]

    def _parse_candidate_list(self, payload: dict[str, Any]) -> list[CandidatePostDTO]:
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

    def list_topic_replies(
        self, *, topic_id: int, topic_openid: str, page: int, limit: int
    ) -> dict[str, Any]:
        """Read a bounded top-level-reply page without retaining the topic body."""
        payload = self._get(
            "topicsearchgetdetail",
            {
                "data": json.dumps(
                    {
                        "topic_id": topic_id,
                        "page_type": 1,
                        "cur_page": page,
                        "limit": limit,
                        "need_topic": True,
                        "openid": topic_openid,
                    },
                    separators=(",", ":"),
                )
            },
        )
        try:
            data = payload["data"]
            records = data["reply_list"]
            if not isinstance(data, dict) or not isinstance(records, list):
                raise TypeError("reply_list is not a list")
            topic = data["topic"]
            if not isinstance(topic, dict):
                raise TypeError("topic is not an object")
            creator = topic.get("creater", topic.get("creator"))
            if (
                topic.get("topic_id") != topic_id
                or topic.get("appid") != self.appid
                or not isinstance(creator, dict)
                or creator.get("openid") != topic_openid
            ):
                raise TypeError("reply page topic identity is invalid")
            replies = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                creator = record.get("creator")
                if not isinstance(creator, dict):
                    creator = {}
                replies.append(
                    {
                        "reply_id": str(record.get("reply_id", "")),
                        "openid": str(creator.get("openid", "")),
                        "nick_name": str(creator.get("nick_name", "")),
                        "create_time": record.get("create_time"),
                        "content": str(record.get("content", "")),
                    }
                )
            has_next = data.get("has_next")
            total_count = data.get("total_count")
            if not isinstance(has_next, bool):
                raise TypeError("reply page state is invalid")
            if total_count is not None and (not isinstance(total_count, int) or total_count < 0):
                raise TypeError("reply total is invalid")
            return {
                "items": replies,
                "has_next": has_next,
                "total_count": total_count,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceContractError("帖子评论响应结构无效") from exc

    def get_author_detail(self, openid: str) -> dict[str, Any]:
        """Read one author's current mute state without depending on a topic ID."""
        if not isinstance(openid, str) or not openid or len(openid) > 255:
            raise SourceContractError("作者用户 ID 无效")
        return self._get(
            "getuserdetail",
            {
                "data": json.dumps(
                    {
                        "query": 1,
                        "topic_req": {"order_by": 1, "is_desc": True},
                        "openid": openid,
                        "page": 0,
                        "page_size": 3,
                        "need_user_info": True,
                    }
                )
            },
        )

    def list_blocked_users(self, *, page: int, page_size: int) -> dict[str, Any]:
        """Read a minimal current-game blocked-user page without author history."""
        if not isinstance(page, int) or page < 0:
            raise SourceContractError("禁言名单页码无效")
        if not isinstance(page_size, int) or not 1 <= page_size <= 500:
            raise SourceContractError("禁言名单分页大小无效")
        payload = self._get_gameopenmpwap(
            "cfcommcgi/spacexcontent_gamecircletopicmanage_getblockuserlist",
            {"data": json.dumps({"page": page, "page_size": page_size}, separators=(",", ":"))},
            allow_application_error=True,
        )
        if payload.get("errcode") != 0:
            message = payload.get("errmsg")
            if isinstance(message, str) and any(
                marker in message.casefold() for marker in _PAGE_SIZE_ERROR_MARKERS
            ):
                raise SourceContractError(
                    "平台禁言名单不接受当前分页大小",
                    safe_reason=BLOCKED_USER_LIST_PAGE_SIZE_REJECTED,
                )
            raise SourceTransportError("平台禁言名单读取失败")
        try:
            data = payload["data"]
            total = int(data["total"])
            items = data["blocked_user_list"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceContractError("平台禁言名单响应结构无效") from exc
        if total < 0 or not isinstance(items, list):
            raise SourceContractError("平台禁言名单响应结构无效")
        return {
            "total": total,
            "items": [
                {
                    "openid": item.get("openid"),
                    "nick_name": item.get("name"),
                    "end_date": item.get("end_date"),
                }
                for item in items
                if isinstance(item, dict)
            ],
        }

    def get_author_detail_with_timeout(
        self, openid: str, *, timeout_seconds: float
    ) -> dict[str, Any]:
        """Bound one author-state read without changing the client's default timeout."""
        if timeout_seconds <= 0:
            raise SourceTransportError("作者状态读取已超过允许的验证时间")
        original_timeout = self.frame.timeout
        self.frame.timeout = min(original_timeout, timeout_seconds)
        try:
            return self.get_author_detail(openid)
        finally:
            self.frame.timeout = original_timeout

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
        self, *, topic_id: int | None, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a verified post operation or an OpenID-targeted author operation."""
        if action in {"SINK", "RESTORE", "BLOCK"} and (
            not isinstance(topic_id, int) or topic_id <= 0
        ):
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
                if not isinstance(topic_id, int) or topic_id <= 0:
                    raise SourceContractError("禁言目标身份标识无效")
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
                if not isinstance(topic_id, int) or topic_id <= 0:
                    raise SourceContractError("解除禁言目标身份标识无效")
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
