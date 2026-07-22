import threading

import pytest

from config.game_circle_ipc import (
    GameCircleIpcClient,
    GameCircleIpcError,
    GameCircleIpcServer,
    IpcRequest,
    WriteResultUnknownError,
)
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
