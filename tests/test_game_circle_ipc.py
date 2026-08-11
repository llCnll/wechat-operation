import threading

import pytest

from config.game_circle_ipc import (
    GameCircleIpcClient,
    GameCircleIpcError,
    GameCirclePostClient,
    GameCircleIpcServer,
    IpcRequest,
    WriteResultUnknownError,
)
from jobs.adapters import SourceBusinessDeletedError
from posts.game_catalog import ExecutionScope


@pytest.fixture
def ipc_server():
    received = []

    def dispatch(request: IpcRequest):
        received.append(request)
        if request.method == "POSTS_OPERATION":
            raise GameCircleIpcError("WRITE_RESULT_UNKNOWN", "上游写入结果待回读确认")
        return {"payload": {"errcode": 0}}

    server = GameCircleIpcServer(dispatch, capability="test-capability", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, received
    finally:
        server.shutdown()
        server.server_close()


def test_ipc_accepts_only_fixed_business_request_and_never_sends_credentials(ipc_server):
    server, received = ipc_server
    client = GameCircleIpcClient(port=server.server_address[1], capability="test-capability")
    client.call("POSTS_GET_METRICS", scope=ExecutionScope("game-a", 1), params={"topic_id": 1})

    assert received[0].method == "POSTS_GET_METRICS"
    assert received[0].scope == ExecutionScope("game-a", 1)


def test_interrupted_or_unknown_write_is_never_reported_as_retryable_success(ipc_server):
    server, _ = ipc_server
    client = GameCircleIpcClient(port=server.server_address[1], capability="test-capability")
    with pytest.raises(WriteResultUnknownError):
        client.call(
            "POSTS_OPERATION",
            scope=ExecutionScope("game-a", 1),
            params={"topic_id": 1, "action": "SINK", "payload": {}},
            operation_id="operation-1",
        )


def test_ipc_rejects_unknown_method_before_network_access():
    with pytest.raises(Exception, match="不支持"):
        GameCircleIpcClient(capability="test-capability").call("Runtime.evaluate")


def test_ipc_rejects_an_unauthorized_caller_before_dispatch(ipc_server):
    server, received = ipc_server
    client = GameCircleIpcClient(port=server.server_address[1], capability="wrong-capability")

    with pytest.raises(GameCircleIpcError, match="未授权"):
        client.call("POSTS_GET_METRICS", scope=ExecutionScope("game-a", 1), params={"topic_id": 1})

    assert received == []


def test_post_client_rehydrates_deleted_source_category_for_shared_refresh_boundary():
    class Ipc:
        def call(self, *_args, **_kwargs):
            raise GameCircleIpcError("SOURCE_DELETED", "原平台帖子已从管理队列删除")

    client = GameCirclePostClient(scope=ExecutionScope("game-a", 1), ipc=Ipc())

    with pytest.raises(SourceBusinessDeletedError, match="原平台帖子已从管理队列删除"):
        client.get_post_metrics(1)


def test_post_client_exposes_only_a_fixed_candidate_lookup():
    calls = []

    class Ipc:
        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {
                "item": {
                    "topic_id": 1464098814,
                    "content_id": "content-a",
                    "source_appid": "game-a",
                    "publish_time": None,
                }
            }

    client = GameCirclePostClient(scope=ExecutionScope("game-a", 1), ipc=Ipc())

    item = client.get_candidate_by_topic_id(1464098814)

    assert item is not None
    assert item.topic_id == 1464098814
    assert calls == [
        (
            ("POSTS_GET_CANDIDATE",),
            {"scope": ExecutionScope("game-a", 1), "params": {"topic_id": 1464098814}},
        )
    ]


def test_post_client_rejects_invalid_candidate_id_without_ipc_access():
    client = GameCirclePostClient(
        scope=ExecutionScope("game-a", 1),
        ipc=type("UnexpectedIpc", (), {"call": lambda *_args, **_kwargs: pytest.fail("unexpected IPC")})(),
    )

    with pytest.raises(Exception, match="帖子 ID 无效"):
        client.get_candidate_by_topic_id(0)
