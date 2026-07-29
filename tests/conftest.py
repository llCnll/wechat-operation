"""Shared pytest safety rails for external process boundaries."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
import pytest


def allowed_local_http_hosts(hosts: tuple[object, ...]) -> frozenset[str]:
    """Validate explicit test-only HTTP exceptions without allowing egress."""

    allowed_hosts = set()
    for host in hosts:
        if not isinstance(host, str):
            raise pytest.UsageError("allow_http_hosts 只接受本机 HTTP 主机名")
        normalized = host.lower()
        try:
            is_loopback = ip_address(normalized).is_loopback
        except ValueError:
            is_loopback = normalized == "localhost"
        if not is_loopback:
            raise pytest.UsageError(
                f"allow_http_hosts 只允许本机 loopback 主机：{host}"
            )
        allowed_hosts.add(normalized)
    return frozenset(allowed_hosts)


@pytest.fixture(autouse=True)
def block_unmocked_httpx_requests(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
):
    """Fail at the httpx send boundary before a test can wait on real I/O."""

    original_send = httpx.Client.send
    test_node = request.node
    marker = test_node.get_closest_marker("allow_http_hosts")
    allowed_hosts = allowed_local_http_hosts(marker.args if marker else ())

    def blocked_send(_client: httpx.Client, request: httpx.Request, *args, **kwargs):
        if isinstance(_client._transport, httpx.MockTransport):
            return original_send(_client, request, *args, **kwargs)
        parsed = urlsplit(str(request.url))
        host = parsed.hostname or "unknown-host"
        if host in allowed_hosts:
            return original_send(_client, request, *args, **kwargs)
        pytest.fail(
            f"未模拟的 HTTP 请求：{test_node.nodeid} {request.method} {host}",
            pytrace=False,
        )

    monkeypatch.setattr(httpx.Client, "send", blocked_send)
