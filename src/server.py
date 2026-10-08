"""
Econ Data MCP Server

Exposes economic time series data from FRED (four tools) and news headlines
from NewsAPI.org (one tool) plus one resource. The tool logic lives in
`tools.py` so the MCP surface here and the multi-agent orchestrator in
`agents/` share one implementation. See README.md for the design rationale.

Run directly (stdio transport, for use with Claude Desktop):
    python src/server.py

Or over HTTP (MCP streamable-HTTP transport, e.g. on Cloud Run — see
mcp.Dockerfile), listening on $PORT at /mcp:
    MCP_TRANSPORT=streamable-http PORT=8080 python src/server.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

import audit_log
import fred_client
import rate_limit
import security
import tools

load_dotenv(Path(__file__).parent.parent / ".env")

mcp = FastMCP("econ-data")

# Over stdio one server process serves one client, so one fixed key is fine.
# Over HTTP, calls are limited per MCP session *and* per client address: the
# server hands out session ids, so a client could dodge a per-session limit
# by opening new sessions; the address bucket is looser (several users can
# share one address) but can't be reset that way.
_STDIO_CLIENT = "mcp-stdio"
_ADDRESS_LIMITER = rate_limit.RateLimiter(
    capacity=rate_limit.limiter.capacity * 4,
    refill_per_sec=rate_limit.limiter.refill_per_sec * 4,
)


def _client_keys(ctx: Context | None) -> list[tuple[str, rate_limit.RateLimiter | None]]:
    """(key, limiter) pairs this call is charged against."""
    try:
        request = ctx.request_context.request if ctx is not None else None
    except ValueError:  # no request context (direct call, tests)
        request = None
    if request is None or not hasattr(request, "headers"):
        return [(_STDIO_CLIENT, None)]
    session = request.headers.get("mcp-session-id") or "no-session"
    forwarded = request.headers.get("x-forwarded-for", "")
    address = (forwarded.split(",")[0].strip() if forwarded
               else (request.client.host if request.client else "unknown"))
    return [(f"mcp-session:{session}", None), (f"mcp-addr:{address}", _ADDRESS_LIMITER)]


def _guarded(tool_name: str, arguments: dict, ctx: Context | None = None) -> dict:
    """Rate-limit the untrusted MCP boundary, then run (and audit-log) the
    shared tool implementation."""
    keys = _client_keys(ctx)
    for key, bucket in keys:
        rejected = rate_limit.guard(key, tool_name, bucket)
        if rejected is not None:
            audit_log.record("rate_limited", caller=key, tool=tool_name)
            return rejected
    return tools.call_tool(tool_name, arguments, caller=keys[0][0])


@mcp.tool()
def search_series(search_text: str, ctx: Context = None) -> dict:
    """
    Search for a FRED series ID from a plain-language description.
    Does NOT return data — use get_series_observations with the returned
    series_id for that. Kept separate so a vague query doesn't accidentally
    pull a large data payload.

    Args:
        search_text: e.g. "unemployment rate", "core inflation", "10 year treasury"
    """
    return _guarded("search_series", {"search_text": search_text}, ctx)


@mcp.tool()
def get_series_observations(
    series_id: str, start_date: str, end_date: str, frequency: str = "m",
    ctx: Context = None,
) -> dict:
    """
    Fetch observations for one FRED series over a required date range.

    Args:
        series_id: FRED series ID, e.g. "UNRATE" (from search_series)
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        frequency: one of d, w, m, q, a (default monthly — ranges over ~2
            years are automatically thinned to stay within the session
            token budget; see cost_tracker.py)
    """
    return _guarded("get_series_observations", {
        "series_id": series_id, "start_date": start_date,
        "end_date": end_date, "frequency": frequency,
    }, ctx)


@mcp.tool()
def compare_series(
    series_ids: list[str], start_date: str, end_date: str, frequency: str = "m",
    ctx: Context = None,
) -> dict:
    """
    Fetch and align up to 4 series over the same date range for comparison.

    Args:
        series_ids: 1-4 FRED series IDs, e.g. ["CPIAUCSL", "UNRATE"]
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        frequency: one of d, w, m, q, a (default monthly)
    """
    return _guarded("compare_series", {
        "series_ids": series_ids, "start_date": start_date,
        "end_date": end_date, "frequency": frequency,
    }, ctx)


@mcp.tool()
def get_series_metadata(series_id: str, ctx: Context = None) -> dict:
    """
    Get units, frequency, last-updated date, and source notes for a series.
    Read-only, small response — no cost guardrail needed.

    Args:
        series_id: FRED series ID, e.g. "GDP"
    """
    return _guarded("get_series_metadata", {"series_id": series_id}, ctx)


@mcp.tool()
def search_news(query: str, start_date: str, end_date: str, ctx: Context = None) -> dict:
    """
    Search recent news headlines relevant to a topic.

    Returns up to 10 headlines (title, source, published date, short
    snippet) — never full article text, and never a summary/opinion of what
    they say. Titles and snippets come back wrapped as untrusted data, same
    pattern as get_series_metadata's 'notes' field.

    Args:
        query: e.g. "inflation", "federal reserve"
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
    """
    return _guarded("search_news", {
        "query": query, "start_date": start_date, "end_date": end_date,
    }, ctx)


@mcp.resource("fred://series/{series_id}/summary")
def series_summary(series_id: str) -> str:
    """
    Cheap, re-readable summary of a previously fetched series — lets the
    model reference a series again without re-invoking a tool call.
    """
    try:
        sid = security.validate_series_id(series_id)
        meta = fred_client.get_series_metadata(sid)
    except (security.ValidationError, fred_client.FredAPIError) as e:
        return f"Error: {e}"
    return (
        f"{meta['title']} ({meta['series_id']})\n"
        f"Units: {meta['units']} | Frequency: {meta['frequency']} | "
        f"Last updated: {meta['last_updated']}"
    )


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8080"))
    mcp.run(transport=transport)
