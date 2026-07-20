import pytest

from jobs.adapters import SourceBusinessDeletedError, SourceContractError
from posts.source_client import CdpGameCircleClient


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
    monkeypatch.setattr(client, "get_post_metrics", lambda _topic_id: pytest.fail("unexpected read"))
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
    monkeypatch.setattr(client, "get_post_metrics", lambda _topic_id: pytest.fail("unexpected read"))
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


def test_bounded_management_detail_read_restores_the_default_timeout(monkeypatch):
    client = object.__new__(CdpGameCircleClient)
    client.frame = type("Frame", (), {"timeout": 15})()
    monkeypatch.setattr(client, "get_post_metrics", lambda _topic_id: {"data": {"topic": {}}})

    client.get_post_metrics_with_timeout(321, timeout_seconds=3)

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
