"""Fixed, loopback-only IPC contract for the game-circle session service.

The contract is deliberately narrower than an RPC bridge: callers can request one
named business operation with an execution scope.  They can never submit CDP
methods, JavaScript, URLs, target identifiers, paths, or authentication material.
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import socketserver
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from config.settings import GAME_CIRCLE_IPC_PORT, GAME_CIRCLE_IPC_TIMEOUT_SECONDS
from jobs.adapters import (
    CandidatePostDTO,
    PluginSessionExpiredError,
    SourceBusinessDeletedError,
    SourceContractError,
    SourceTransportError,
    WriteResultUnknownError as SourceWriteResultUnknownError,
)
from posts.game_catalog import ExecutionScope

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_AUTHOR_OPENID_LENGTH = 255
IPC_CAPABILITY_ENV = "OPERATION_GAME_CIRCLE_IPC_CAPABILITY"
POST_METHODS = frozenset(
    {
        "POSTS_LIST_CANDIDATES",
        "POSTS_GET_CONTENT_CARD",
        "POSTS_GET_METRICS",
        "POSTS_GET_AUTHOR_DETAIL",
        "POSTS_LIST_BLOCKED_USERS",
        "POSTS_OPERATION",
    }
)
METRIC_METHODS = frozenset(
    {
        "METRIC_CIRCLE_FANS",
        "METRIC_CONTENT_FANS",
        "METRIC_OPERATION_EFFECT",
        "METRIC_GROUP_CONTENT_ANALYSIS",
    }
)
CONTROL_METHODS = frozenset(
    {
        "OPEN_CONTROLLED_LOGIN",
        "CHECK_LOGIN",
        "BUILD_CURRENT_GAME_FRAME",
        "GET_RUNTIME_STATUS",
        "LIST_MANAGED_GAMES",
    }
)
ALL_METHODS = POST_METHODS | METRIC_METHODS | CONTROL_METHODS


class GameCircleIpcError(SourceTransportError):
    """A safe fixed IPC failure that never contains service internals."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class WriteResultUnknownError(GameCircleIpcError, SourceWriteResultUnknownError):
    """A write may have reached the upstream service before IPC was interrupted."""

    def __init__(self):
        super().__init__("WRITE_RESULT_UNKNOWN", "上游写入结果待回读确认")


def is_session_recovering_error(error: BaseException) -> bool:
    """Identify the one IPC outcome that may defer a source Worker safely."""
    return isinstance(error, GameCircleIpcError) and error.category == "SESSION_RECOVERING"


@dataclass(frozen=True)
class IpcRequest:
    method: str
    scope: ExecutionScope | None
    params: dict[str, Any]
    operation_id: str


def ipc_capability_from_environment() -> str:
    capability = os.getenv(IPC_CAPABILITY_ENV, "")
    if not capability:
        raise GameCircleIpcError("SERVICE_UNAVAILABLE", "游戏圈会话服务能力不可用")
    return capability


def _scope_payload(scope: ExecutionScope) -> dict[str, object]:
    return {"target_appid": scope.target_appid, "scope_version": scope.scope_version}


def _parse_request(payload: object) -> IpcRequest:
    if not isinstance(payload, dict):
        raise GameCircleIpcError("INVALID_REQUEST", "游戏圈会话请求无效")
    method = payload.get("method")
    params = payload.get("params", {})
    operation_id = payload.get("operation_id", "")
    if method not in ALL_METHODS or not isinstance(params, dict):
        raise GameCircleIpcError("INVALID_REQUEST", "游戏圈会话请求无效")
    if not isinstance(operation_id, str) or len(operation_id) > 100:
        raise GameCircleIpcError("INVALID_REQUEST", "游戏圈会话请求无效")
    scope_payload = payload.get("execution_scope")
    scope = None
    if method not in CONTROL_METHODS:
        if not isinstance(scope_payload, dict):
            raise GameCircleIpcError("INVALID_SCOPE", "游戏圈操作范围无效")
        appid, version = scope_payload.get("target_appid"), scope_payload.get("scope_version")
        if not isinstance(appid, str) or not appid or not isinstance(version, int) or version < 1:
            raise GameCircleIpcError("INVALID_SCOPE", "游戏圈操作范围无效")
        scope = ExecutionScope(appid, version)
    elif scope_payload is not None:
        raise GameCircleIpcError("INVALID_REQUEST", "游戏圈会话请求无效")
    return IpcRequest(method=method, scope=scope, params=params, operation_id=operation_id)


class _IpcHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            self._write({"ok": False, "category": "INVALID_REQUEST", "message": "游戏圈会话请求无效"})
            return
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise GameCircleIpcError("INVALID_REQUEST", "游戏圈会话请求无效")
            capability = payload.pop("capability", "")
            expected = self.server.capability  # type: ignore[attr-defined]
            if (
                not isinstance(capability, str)
                or len(capability) != len(expected)
                or not hmac.compare_digest(capability, expected)
            ):
                raise GameCircleIpcError("UNAUTHORIZED", "游戏圈会话请求未授权")
            request = _parse_request(payload)
            result = self.server.dispatch(request)  # type: ignore[attr-defined]
            self._write({"ok": True, "result": result})
        except GameCircleIpcError as exc:
            self._write({"ok": False, "category": exc.category, "message": str(exc)})
        except Exception:
            self._write({"ok": False, "category": "SERVICE_FAILURE", "message": "游戏圈会话服务执行失败"})

    def _write(self, value: dict[str, object]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(encoded) <= MAX_RESPONSE_BYTES:
            self.wfile.write(encoded)


class GameCircleIpcServer(socketserver.ThreadingTCPServer):
    """Threaded loopback listener; no network interface is ever configurable."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        dispatch: Callable[[IpcRequest], dict[str, Any]],
        *,
        capability: str,
        port: int = GAME_CIRCLE_IPC_PORT,
    ):
        if not capability:
            raise ValueError("游戏圈 IPC capability 不能为空")
        self.dispatch = dispatch
        self.capability = capability
        super().__init__(("127.0.0.1", port), _IpcHandler)


class GameCircleIpcClient:
    def __init__(
        self,
        *,
        port: int = GAME_CIRCLE_IPC_PORT,
        timeout: float = GAME_CIRCLE_IPC_TIMEOUT_SECONDS,
        capability: str | None = None,
    ):
        self.port = port
        self.timeout = timeout
        self._capability = capability

    def call(
        self,
        method: str,
        *,
        scope: ExecutionScope | None = None,
        params: dict[str, Any] | None = None,
        operation_id: str = "",
    ) -> dict[str, Any]:
        if method not in ALL_METHODS:
            raise SourceContractError("不支持的游戏圈会话方法")
        capability = self._capability if self._capability is not None else ipc_capability_from_environment()
        payload: dict[str, object] = {
            "method": method,
            "params": params or {},
            "operation_id": operation_id,
            "capability": capability,
        }
        if method not in CONTROL_METHODS:
            if scope is None:
                raise SourceContractError("游戏圈操作缺少执行范围")
            payload["execution_scope"] = _scope_payload(scope)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            if len(encoded) > MAX_REQUEST_BYTES:
                raise SourceContractError("游戏圈会话请求过大")
            with socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout) as connection:
                connection.settimeout(self.timeout)
                connection.sendall(encoded)
                chunks = bytearray()
                while len(chunks) <= MAX_RESPONSE_BYTES:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            if method == "POSTS_OPERATION":
                raise WriteResultUnknownError() from exc
            raise GameCircleIpcError("SERVICE_UNAVAILABLE", "游戏圈会话服务不可用") from exc
        try:
            response = json.loads(bytes(chunks).split(b"\n", 1)[0])
        except (ValueError, IndexError) as exc:
            if method == "POSTS_OPERATION":
                raise WriteResultUnknownError() from exc
            raise GameCircleIpcError("SERVICE_UNAVAILABLE", "游戏圈会话服务不可用") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            category = response.get("category") if isinstance(response, dict) else "SERVICE_FAILURE"
            message = response.get("message") if isinstance(response, dict) else "游戏圈会话服务执行失败"
            if category == "WRITE_RESULT_UNKNOWN":
                raise WriteResultUnknownError()
            raise GameCircleIpcError(str(category), str(message))
        result = response.get("result")
        if not isinstance(result, dict):
            raise GameCircleIpcError("SERVICE_FAILURE", "游戏圈会话服务响应无效")
        return result

    def list_managed_games(self) -> list[dict[str, str]]:
        items = self.call("LIST_MANAGED_GAMES").get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("appid"), str)
            or not isinstance(item.get("name"), str)
            for item in items
        ):
            raise GameCircleIpcError("SERVICE_FAILURE", "游戏目录响应无效")
        return [{"appid": item["appid"], "name": item["name"]} for item in items]


class GameCirclePostClient:
    """Caller-side source facade: serializes only fixed post business methods."""

    def __init__(self, *, scope: ExecutionScope, ipc: GameCircleIpcClient | None = None):
        self.requires_operation_id = True
        self.scope = scope
        self.appid = scope.target_appid
        self._ipc = ipc or GameCircleIpcClient()

    def list_candidates(self, *, page: int, page_size: int, start_time: int, end_time: int) -> list[CandidatePostDTO]:
        result = self._ipc.call("POSTS_LIST_CANDIDATES", scope=self.scope, params={"page": page, "page_size": page_size, "start_time": start_time, "end_time": end_time})
        items = result.get("items")
        if not isinstance(items, list):
            raise SourceContractError("游戏圈会话响应结构无效")
        parsed: list[CandidatePostDTO] = []
        for item in items:
            if not isinstance(item, dict):
                raise SourceContractError("游戏圈会话响应结构无效")
            published = item.get("publish_time")
            parsed.append(CandidatePostDTO(int(item["topic_id"]), str(item.get("content_id", "")), str(item.get("source_appid", self.appid)), datetime.fromtimestamp(int(published), tz=UTC) if published else None))
        return parsed

    def get_content_card(self, content_id: str) -> dict[str, Any]:
        return self._payload("POSTS_GET_CONTENT_CARD", {"content_id": content_id})

    def get_post_metrics(self, topic_id: int) -> dict[str, Any]:
        return self._payload("POSTS_GET_METRICS", {"topic_id": topic_id})

    def get_post_metrics_with_timeout(self, topic_id: int, *, timeout_seconds: float) -> dict[str, Any]:
        return self._payload("POSTS_GET_METRICS", {"topic_id": topic_id, "timeout_seconds": timeout_seconds})

    def get_author_detail(self, openid: str) -> dict[str, Any]:
        if not isinstance(openid, str) or not openid or len(openid) > MAX_AUTHOR_OPENID_LENGTH:
            raise SourceContractError("作者用户 ID 无效")
        return self._payload("POSTS_GET_AUTHOR_DETAIL", {"openid": openid})

    def get_author_detail_with_timeout(
        self, openid: str, *, timeout_seconds: float
    ) -> dict[str, Any]:
        if not isinstance(openid, str) or not openid or len(openid) > MAX_AUTHOR_OPENID_LENGTH:
            raise SourceContractError("作者用户 ID 无效")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise SourceContractError("作者详情读取超时参数无效")
        return self._payload(
            "POSTS_GET_AUTHOR_DETAIL",
            {"openid": openid, "timeout_seconds": timeout_seconds},
        )

    def list_blocked_users(self, *, page: int, page_size: int) -> dict[str, Any]:
        """Read a minimal page from the platform's current blocked-user list."""
        if not isinstance(page, int) or page < 0:
            raise SourceContractError("禁言名单页码无效")
        if not isinstance(page_size, int) or not 1 <= page_size <= 500:
            raise SourceContractError("禁言名单分页大小无效")
        result = self._ipc.call(
            "POSTS_LIST_BLOCKED_USERS",
            scope=self.scope,
            params={"page": page, "page_size": page_size},
        )
        total = result.get("total")
        items = result.get("items")
        if not isinstance(total, int) or total < 0 or not isinstance(items, list):
            raise SourceContractError("游戏圈禁言名单响应结构无效")
        return {"total": total, "items": items}

    def perform_confirmed_operation(self, *, topic_id: int | None, action: str, payload: dict[str, Any], operation_id: str = "") -> dict[str, Any]:
        return self._payload("POSTS_OPERATION", {"topic_id": topic_id, "action": action, "payload": payload}, operation_id=operation_id or str(uuid4()))

    def _payload(self, method: str, params: dict[str, Any], *, operation_id: str = "") -> dict[str, Any]:
        try:
            result = self._ipc.call(
                method,
                scope=self.scope,
                params=params,
                operation_id=operation_id,
            )
        except GameCircleIpcError as exc:
            # The session service transports this verified upstream condition as
            # SOURCE_DELETED.  Rehydrate it at the post-source boundary so every
            # caller shares the explicit deleted/unreadable fallback semantics.
            if exc.category == "SOURCE_DELETED":
                raise SourceBusinessDeletedError(str(exc)) from exc
            raise
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise SourceContractError("游戏圈会话响应结构无效")
        return payload

    def close(self) -> None:
        return None

    def __enter__(self) -> "GameCirclePostClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class GameCircleMetricClient:
    """Caller-side facade for the bounded reporting source methods."""

    def __init__(self, *, scope: ExecutionScope, ipc: GameCircleIpcClient | None = None):
        self.scope = scope
        self.appid = scope.target_appid
        self._ipc = ipc or GameCircleIpcClient()

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        payload = self._ipc.call(method, scope=self.scope, params=params).get("payload")
        if not isinstance(payload, dict):
            raise SourceContractError("游戏圈指标响应结构无效")
        return payload

    def circle_fans(self, **params: Any) -> dict[str, Any]: return self._call("METRIC_CIRCLE_FANS", **params)
    def content_fans(self, **params: Any) -> dict[str, Any]: return self._call("METRIC_CONTENT_FANS", **params)
    def operation_effect(self, **params: Any) -> dict[str, Any]: return self._call("METRIC_OPERATION_EFFECT", **params)
    def group_content_analysis(self, **params: Any) -> dict[str, Any]: return self._call("METRIC_GROUP_CONTENT_ANALYSIS", **params)
    def close(self) -> None: return None

    def __enter__(self) -> "GameCircleMetricClient": return self
    def __exit__(self, *_args: object) -> None: self.close()
