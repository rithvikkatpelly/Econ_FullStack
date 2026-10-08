"""FastAPI entry point for the Econ Data API.

A second, independently deployable interface over the *same* tool logic the
MCP server uses. Every endpoint delegates to ``src/tools.py`` — the one
implementation that composes ``fred_client`` (fetch + cache), ``security``
(input validation), and ``cost_tracker`` (the per-session token guardrail).
The MCP server (``src/server.py``) is a sibling caller of that module and is
completely unaffected by this package.

    validate (security)  ->  fetch (fred_client)  ->  guardrail (cost_tracker)
    ->  tools.py returns {"error": code, ...} on failure, never raises
    ->  this layer maps that dict to an HTTP status code

Run it (from the backend/ directory):

    uvicorn app.main:app --reload

Interactive docs at http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

import fred_client
import tools
from app import agent, telemetry
from app.schemas import (
    CompareRequest,
    MetadataResponse,
    ObservationsRequest,
    SearchRequest,
    SearchResponse,
)
from core.config import get_settings

settings = get_settings()


# --- Structured logging ---------------------------------------------------
#
# One JSON object per line on stdout. Cloud Run ships container stdout/stderr
# to Cloud Logging automatically (no agent/config needed on this end); when a
# line is valid JSON, Cloud Logging parses it into jsonPayload fields instead
# of one opaque textPayload string, and a top-level "severity" key maps onto
# the LogEntry's own severity field. Locally this just prints JSON lines —
# same code path, easy to pipe through `jq`.
class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"severity": record.levelname, "message": record.getMessage()}
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload)


logger = logging.getLogger("econ_data_api")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(_JSONFormatter())
    logger.addHandler(_handler)

# Cloud Trace for the agent runs, when CLOUD_TRACE=1 (app/telemetry.py).
telemetry.setup()

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=(
        "HTTP interface over the same FRED tool logic as the MCP server "
        "(src/server.py). Offline synthetic fixture unless FRED_API_KEY is set."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


app.include_router(agent.router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled exception",
            extra={"fields": {"method": request.method, "path": request.url.path}},
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "request",
        extra={
            "fields": {
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            }
        },
    )
    return response


# --- Structured-error translation ----------------------------------------

# tools.* returns {"error": <code>, ...} instead of raising. Map each code to
# the HTTP status that fits it; anything unrecognised is a 500.
_ERROR_STATUS = {
    "validation_error": 422,
    "bad_arguments": 422,
    "series_not_found": 404,
    "fred_api_error": 502,
    "news_api_error": 502,
    "session_budget_exceeded": 429,
    "unknown_tool": 500,
}


def _unwrap(result: dict) -> dict:
    """Return ``result`` on success; raise ``HTTPException`` with the
    structured error dict as ``detail`` on failure."""
    if "error" not in result:
        return result

    err = dict(result)
    detail_text = str(err.get("detail") or "").lower()
    # Offline: an unknown series surfaces as a generic fred_api_error from the
    # fixture ("No synthetic fixture for series 'X'"). That's really a 404.
    if err["error"] == "fred_api_error" and (
        "synthetic fixture" in detail_text or "no metadata found" in detail_text
    ):
        err = {
            "error": "series_not_found",
            "detail": err.get("detail"),
            "suggestion": "Call POST /search to find a valid series ID.",
        }

    raise HTTPException(status_code=_ERROR_STATUS.get(err["error"], 500), detail=err)


# --- Endpoints — one per MCP tool ---------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness/startup probe target (see .github/workflows/deploy.yml's
    --startup-probe). Confirms config actually loaded, not just that the
    process is up:

    - ``fred_api_key_configured``: whether FRED_API_KEY resolved to a real
      value (never the value itself) — the thing most likely to be
      misconfigured on a fresh deploy (wrong Secret Manager binding, etc).
    - ``offline``: whether calls are actually being served from the
      synthetic fixture or the live FRED API — the two can disagree, e.g. a
      key is set but FRED_OFFLINE=1 forces the fixture anyway.

    - ``agent_backend`` / ``agent_model_configured``: which model drives
      ``/agent/*`` and whether its credentials resolved (a Gemini key, or
      Vertex AI mode) — again never the value.
    - ``agent_answering_on``: the backend the next question will actually
      run on — ``stub`` while a used-up Gemini quota has paused live runs.

    There's no database in this project (backend/app is stateless — FRED is
    the only backing store), so there's nothing else to check here.
    """
    return {
        "status": "ok",
        "fred_api_key_configured": bool(os.environ.get("FRED_API_KEY")),
        "offline": fred_client._offline(),
        "agent_backend": agent.backend_name(),
        "agent_framework": agent.framework(),
        "agent_model_configured": settings.agent_model_configured,
        "agent_answering_on": agent.effective_backend(),
    }


@app.post("/search", response_model=SearchResponse, tags=["tools"])
def search(req: SearchRequest) -> dict:
    """`search_series` — plain-language concept → candidate series IDs. Never
    returns observations."""
    return _unwrap(tools.search_series(req.search_text))


@app.post("/observations", tags=["tools"])
def observations(req: ObservationsRequest) -> dict:
    """`get_series_observations` — one series over a required date range.
    Returns ``{"series_id", "observations": [{"date","value"}], "_cost"}``.
    Long ranges are thinned to fit the token budget (a ``"note"`` says so);
    a range that still doesn't fit is a 429."""
    return _unwrap(
        tools.get_series_observations(
            req.series_id, req.start_date, req.end_date, req.frequency
        )
    )


@app.post("/compare", tags=["tools"])
def compare(req: CompareRequest) -> dict:
    """`compare_series` — 2–4 series aligned over one date range. Returns
    ``{"series": {series_id: [{"date","value"}]}, "_cost"}``."""
    return _unwrap(
        tools.compare_series(
            req.series_ids, req.start_date, req.end_date, req.frequency
        )
    )


@app.get("/metadata/{series_id}", response_model=MetadataResponse, tags=["tools"])
def metadata(series_id: str) -> dict:
    """`get_series_metadata` — units, frequency, last-updated, source notes.
    ``notes`` comes back wrapped as untrusted data (never an instruction)."""
    return _unwrap(tools.get_series_metadata(series_id))
