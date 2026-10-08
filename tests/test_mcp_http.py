"""The MCP server over HTTP: per-session and per-address rate limits
(src/server.py). stdio keeps its single fixed key."""

from types import SimpleNamespace

import pytest

import rate_limit
import server


def _ctx(session="s1", forwarded="203.0.113.7", client="10.0.0.1"):
    headers = {"mcp-session-id": session}
    if forwarded:
        headers["x-forwarded-for"] = f"{forwarded}, 10.1.1.1"
    request = SimpleNamespace(headers=headers, client=SimpleNamespace(host=client))
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


@pytest.fixture(autouse=True)
def _small_buckets(monkeypatch):
    session = rate_limit.RateLimiter(capacity=2, refill_per_sec=0.0)
    address = rate_limit.RateLimiter(capacity=5, refill_per_sec=0.0)
    monkeypatch.setattr(rate_limit, "limiter", session)
    monkeypatch.setattr(server, "_ADDRESS_LIMITER", address)


def test_stdio_uses_one_fixed_key():
    assert server._client_keys(None) == [("mcp-stdio", None)]


def test_http_is_keyed_on_session_and_first_forwarded_address():
    keys = [k for k, _ in server._client_keys(_ctx())]
    assert keys == ["mcp-session:s1", "mcp-addr:203.0.113.7"]
    keys = [k for k, _ in server._client_keys(_ctx(forwarded=""))]
    assert keys[1] == "mcp-addr:10.0.0.1"


def _call(ctx):
    return server.get_series_metadata("UNRATE", ctx=ctx)


def test_a_session_is_limited_on_its_own():
    a, b = _ctx(session="a"), _ctx(session="b", forwarded="198.51.100.9")
    assert [("error" in _call(a)) for _ in range(3)] == [False, False, True]
    assert "error" not in _call(b)  # another session, another address: untouched


def test_new_sessions_cant_reset_the_address_limit():
    results = [_call(_ctx(session=f"s{i}")) for i in range(7)]
    limited = [r.get("error") == "rate_limited" for r in results]
    assert limited == [False] * 5 + [True] * 2
