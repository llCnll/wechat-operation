from unittest.mock import Mock

import pytest

from config.cdp import CdpTargetUnavailableError
from jobs.adapters import (
    PluginSessionExpiredError,
    SourceBusinessDeletedError,
    SourceContractError,
    SourceTransportError,
)
from posts.source_client import BLOCKED_USER_LIST_PAGE_SIZE_REJECTED, CdpGameCircleClient
from reporting.source_client import CdpGameCircleMetricClient


def _client_with_captured_post(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    calls = []

    def post(method, body):
        calls.append((method, body))
        return {"errcode": 0}

    monkeypatch.setattr(client, "_post_json", post)
    return client, calls


def test_verified_sink_operation_uses_fixed_status(monkeypatch):
    client, calls = _client_with_captured_post(monkeypatch)

    result = client.perform_confirmed_operation(topic_id=321, action="SINK", payload={})

    assert result == {"errcode": 0}
    assert calls == [("topicsearchblock", {"topic_id": 321, "status": 8})]


def test_verified_restore_operation_uses_normal_status(monkeypatch):
    client, calls = _client_with_captured_post(monkeypatch)

    client.perform_confirmed_operation(topic_id=321, action="RESTORE", payload={})

    assert calls == [("topicsearchblock", {"topic_id": 321, "status": 2})]


def test_verified_block_operation_uses_reason_bitmask(monkeypatch):
    client, calls = _client_with_captured_post(monkeypatch)

    client.perform_confirmed_operation(
        topic_id=321, action="BLOCK", payload={"block_reason_code": 8 | 16}
    )

    assert calls == [
        (
            "topicsearchblock",
            {"topic_id": 321, "status": 4, "block_info": {"reason": 24}},
        )
    ]


def test_verified_mute_reuses_preflight_author_identity_without_second_read(monkeypatch):
    client, calls = _client_with_captured_post(monkeypatch)
    monkeypatch.setattr(
        client, "get_post_metrics", lambda _topic_id: pytest.fail("unexpected read")
    )
    mute_calls = []
    monkeypatch.setattr(
        client,
        "_post_gameopenmpwap",
        lambda method, body: mute_calls.append((method, body)) or {"errcode": 0},
    )

    client.perform_confirmed_operation(
        topic_id=321,
        action="MUTE",
        payload={
            "block_reason_code": 8,
            "mute_end_time": 2_000_000_000,
            "reason": "广告",
            "author_openid": "ephemeral-author",
        },
    )

    assert calls == []
    assert mute_calls == [
        (
            "cfcommcgi/spacexcontent_gamecircletopicmanage_topicblockuser",
            {
                "account_openid": "ephemeral-author",
                "end_time": 2_000_000_000,
                "block_reason_type": 8,
                "reason": "广告",
            },
        )
    ]


def test_verified_unmute_reuses_preflight_author_identity_and_module(monkeypatch):
    client, calls = _client_with_captured_post(monkeypatch)
    monkeypatch.setattr(
        client, "get_post_metrics", lambda _topic_id: pytest.fail("unexpected read")
    )
    unmute_calls = []
    monkeypatch.setattr(
        client,
        "_post_gameopenmpwap",
        lambda method, body: unmute_calls.append((method, body)) or {"errcode": 0},
    )

    client.perform_confirmed_operation(
        topic_id=321, action="UNMUTE", payload={"author_openid": "ephemeral-author"}
    )

    assert calls == []
    assert unmute_calls == [
        (
            "cfcommcgi/spacexcontent_gamecircletopicmanage_topicunblockuser",
            {"account_openid": "ephemeral-author", "module": "mmgamegamecirclemanageweb"},
        )
    ]


def test_author_detail_uses_the_fixed_user_query_contract(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    calls = []
    monkeypatch.setattr(
        client, "_get", lambda path, query: calls.append((path, query)) or {"errcode": 0}
    )

    assert client.get_author_detail("author-openid") == {"errcode": 0}
    assert calls == [
        (
            "getuserdetail",
            {
                "data": '{"query": 1, "topic_req": {"order_by": 1, "is_desc": true}, "openid": "author-openid", "page": 0, "page_size": 3, "need_user_info": true}'
            },
        )
    ]


def test_author_detail_rejects_an_unbounded_identity_before_source_access(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    monkeypatch.setattr(client, "_get", lambda *_args: pytest.fail("unexpected source read"))

    with pytest.raises(SourceContractError, match="作者用户 ID 无效"):
        client.get_author_detail("x" * 256)


def test_blocked_user_list_only_classifies_an_explicit_page_size_error_for_fallback(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    monkeypatch.setattr(
        client,
        "_get_gameopenmpwap",
        lambda *_args, **_kwargs: {"errcode": 400, "errmsg": "invalid page_size"},
    )

    with pytest.raises(SourceContractError) as exc_info:
        client.list_blocked_users(page=0, page_size=500)

    assert exc_info.value.safe_reason == BLOCKED_USER_LIST_PAGE_SIZE_REJECTED


def test_blocked_user_list_does_not_treat_a_platform_failure_as_pagination_rejection(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    monkeypatch.setattr(
        client,
        "_get_gameopenmpwap",
        lambda *_args, **_kwargs: {"errcode": -1, "errmsg": "system busy"},
    )

    with pytest.raises(SourceTransportError, match="平台禁言名单读取失败"):
        client.list_blocked_users(page=0, page_size=500)


def test_management_detail_deleted_error_is_mapped_to_business_deletion(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    client._last_request_at = 0.0
    client.min_interval_seconds = 0.0
    client.token = "transient-token"
    client.frame = type(
        "Frame",
        (),
        {"evaluate": lambda self, expression: {"status": 200, "payload": {"errcode": -10075007}}},
    )()

    with pytest.raises(SourceBusinessDeletedError):
        client.get_post_metrics(1434284478)


def test_management_detail_non_json_response_has_a_safe_reason():
    frame = Mock()
    frame.evaluate.return_value = {"status": 200, "payload": None, "response_kind": "OTHER"}
    client = CdpGameCircleClient(
        appid="game-a", frame=frame, identity_token="test-only", min_interval_seconds=0
    )

    with pytest.raises(SourceContractError) as exc_info:
        client.get_post_metrics(1434284478)

    assert exc_info.value.safe_reason == "NON_JSON_RESPONSE"
    assert str(exc_info.value) == "来源详情响应不是 JSON"


def test_verified_identity_rejection_rebinds_once_then_uses_the_new_in_memory_token():
    frame = Mock()
    frame.evaluate.side_effect = [
        {"status": 200, "payload": {"errcode": -2}},
        {"status": 200, "payload": {"errcode": 0, "rsp": {"topic_list": []}}},
    ]
    rebind = Mock(return_value="new-token")
    client = CdpGameCircleClient(
        appid="game-a",
        frame=frame,
        identity_token="old-token",
        min_interval_seconds=0,
        rebind_identity=rebind,
    )

    assert client.list_candidates(page=0, page_size=1, start_time=0, end_time=1) == []
    assert rebind.call_count == 1
    expressions = [call.args[0] for call in frame.evaluate.call_args_list]
    assert "old-token" in expressions[0]
    assert "new-token" in expressions[1]


def test_second_verified_identity_rejection_never_rebinds_or_replays_again():
    frame = Mock()
    frame.evaluate.side_effect = [
        {"status": 200, "payload": {"errcode": -2}},
        {"status": 200, "payload": {"errcode": -2}},
    ]
    rebind = Mock(return_value="new-token")
    client = CdpGameCircleClient(
        appid="game-a",
        frame=frame,
        identity_token="old-token",
        min_interval_seconds=0,
        rebind_identity=rebind,
    )

    with pytest.raises(PluginSessionExpiredError):
        client.list_candidates(page=0, page_size=1, start_time=0, end_time=1)
    assert rebind.call_count == 1


def test_post_client_preserves_a_missing_owned_target_as_session_expiry():
    frame = Mock()
    frame.evaluate.side_effect = CdpTargetUnavailableError("target missing")
    client = CdpGameCircleClient(
        appid="game-a", frame=frame, identity_token="test-only", min_interval_seconds=0
    )

    with pytest.raises(PluginSessionExpiredError, match="target"):
        client.list_candidates(page=0, page_size=1, start_time=0, end_time=1)


def test_metric_client_preserves_a_missing_owned_target_as_session_expiry():
    frame = Mock()
    frame.evaluate.side_effect = CdpTargetUnavailableError("target missing")
    client = CdpGameCircleMetricClient(
        appid="game-a", frame=frame, identity_token="test-only", min_interval_seconds=0
    )

    with pytest.raises(PluginSessionExpiredError, match="target"):
        client.circle_fans(start_ds=20260701, end_ds=20260702)


def test_bounded_management_detail_read_restores_the_default_timeout(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    client.frame = type("Frame", (), {"timeout": 15})()
    monkeypatch.setattr(client, "get_post_metrics", lambda _topic_id: {"data": {"topic": {}}})

    client.get_post_metrics_with_timeout(321, timeout_seconds=3)

    assert client.frame.timeout == 15


def test_bounded_author_detail_read_restores_the_default_timeout(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    client.frame = type("Frame", (), {"timeout": 15})()
    monkeypatch.setattr(client, "get_author_detail", lambda _openid: {"data": {"user": {}}})

    client.get_author_detail_with_timeout("author", timeout_seconds=3)

    assert client.frame.timeout == 15


@pytest.mark.parametrize(
    ("action", "payload"),
    (
        ("BLOCK", {}),
        ("BLOCK", {"block_reason_code": 32}),
        ("MUTE", {"block_reason_code": 32, "mute_end_time": 2_000_000_000}),
    ),
)
def test_unverified_or_invalid_operation_is_rejected(monkeypatch, action, payload):
    client, calls = _client_with_captured_post(monkeypatch)

    with pytest.raises(SourceContractError):
        client.perform_confirmed_operation(topic_id=321, action=action, payload=payload)

    assert calls == []
