"""
Data Agent — the only agent in the pipeline with tool access.

One `DataAgent` instance is responsible for exactly one `FetchRequest` (one
series). The orchestration layer creates one per request in the plan and, for
a multi-series query, runs them concurrently (`fetch_all(..., parallel=True)`
→ `asyncio.gather` over `asyncio.to_thread`, since the FRED client is sync).

Each instance calls the existing MCP tools (`tools.call_tool`) — it never
re-implements their logic — and returns a strict `DataAgentResult` for its one
series. `_ensure_wrapped_notes` keeps the FRED `notes` field inside the
`security.wrap_untrusted_text` envelope so the provenance label survives the
hand-off to the Analysis Agent.

Errors are collected onto the result (`SeriesData.error`,
`DataAgentResult.error`), never raised — one bad series never kills the batch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import catalog
import security
import tools
from agents import timing
from agents.orchestrator import FetchRequest, QueryPlan

_WRAPPED_KEYS = {"untrusted_source", "untrusted_source_text", "note"}


@dataclass
class SeriesData:
    series_id: str
    title: str = ""
    units: str = ""
    frequency: str = ""
    start_date: str = ""
    end_date: str = ""
    observations: list[dict] = field(default_factory=list)  # [{"date","value"}, ...]
    metadata: dict = field(default_factory=dict)            # notes wrapped as untrusted
    error: str | None = None
    # How the ID was settled when the orchestrator only had a best guess
    # (FetchRequest.search_text): "confirmed" (the guess was among the search
    # hits), "replaced" (the top hit was used instead), or "search_failed"
    # (kept the guess). Empty when no search was needed.
    resolution: str = ""

    @property
    def observation_count(self) -> int:
        return len(self.observations)


@dataclass
class DataAgentResult:
    """The output of one Data Agent instance — one series."""

    request: FetchRequest
    series: SeriesData | None = None
    error: str | None = None  # request-level failure (the agent couldn't run at all)

    @property
    def series_id(self) -> str:
        """The series actually fetched — after search, possibly not the
        orchestrator's guess."""
        return self.series.series_id if self.series else self.request.series_id

    @property
    def ok(self) -> bool:
        return self.error is None and self.series is not None and self.series.error is None

    @property
    def failure_reason(self) -> str | None:
        if self.error:
            return self.error
        if self.series and self.series.error:
            return self.series.error
        return None


@dataclass
class DataFetchBatch:
    """The results of running every Data Agent for one plan, in request order,
    plus timing/concurrency info for inspection."""

    results: list[DataAgentResult]
    agents: list[DataAgent]
    wall_seconds: float
    parallel: bool

    @property
    def overlapped(self) -> bool:
        """True if any two agents' execution windows overlapped in time — i.e.
        they really did run concurrently, not one after another."""
        return timing.overlapped(self.agents)

    @property
    def ok_results(self) -> list[DataAgentResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[dict]:
        return [
            {"series_id": r.series_id, "reason": r.failure_reason}
            for r in self.results
            if not r.ok
        ]


def _ensure_wrapped_notes(metadata: dict) -> dict:
    """Guarantee metadata['notes'] is the labeled untrusted-data structure,
    whatever `get_series_metadata` handed back."""
    notes = metadata.get("notes")
    if isinstance(notes, dict) and set(notes) >= _WRAPPED_KEYS:
        return metadata
    metadata = dict(metadata)
    metadata["notes"] = security.wrap_untrusted_text(
        "fred_series_notes", notes if isinstance(notes, str) else ""
    )
    return metadata


# How many search hits count as "the guess is confirmed".
SEARCH_CONFIRM_TOP_N = 3


def _resolve(req: FetchRequest) -> tuple[str, str]:
    """(series ID to fetch, resolution). With `search_text` set the
    orchestrator only guessed the series: confirm it with search_series, or
    take the top hit if the guess isn't among the first few."""
    if not req.search_text:
        return req.series_id, ""
    results = _search(req.search_text)
    hits = [r["series_id"] for r in results if r.get("series_id")]
    if not req.series_id:
        # No guess at all (an indicator outside the catalog): only trust hits
        # that are about the question (catalog.relevant_hits).
        hits = catalog.relevant_hits(req.search_text, results)
    if not hits:
        return req.series_id, "search_failed"
    if req.series_id in hits[:SEARCH_CONFIRM_TOP_N]:
        return req.series_id, "confirmed"
    return hits[0], "replaced"


def _search(text: str) -> list[dict]:
    """search_series on the question's keywords, dropping trailing words
    until something matches (at most catalog.SEARCH_MAX_TRIES calls)."""
    for query in catalog.search_queries(text):
        found = tools.call_tool("search_series", {"search_text": query}, caller="data_agent")
        results = [] if found.get("error") else found.get("results", [])
        if results:
            return results
    return []


def _fetch_one(req: FetchRequest) -> SeriesData:
    """Blocking fetch for one series via the existing MCP tools."""
    series_id, resolution = _resolve(req)
    data = SeriesData(
        series_id=series_id,
        resolution=resolution,
        start_date=req.start_date,
        end_date=req.end_date,
        frequency=req.frequency,
    )
    if not series_id:
        # Search-only request and nothing matched: say so, don't call the
        # tools with an empty ID.
        data.error = "not_found_by_search"
        return data

    if req.fetch_metadata:
        meta = tools.call_tool(
            "get_series_metadata", {"series_id": series_id}, caller="data_agent"
        )
        if meta.get("error"):
            data.error = f"metadata: {meta['error']}"
            return data  # a bad series ID fails here — don't bother fetching data
        data.title = meta.get("title", "")
        data.units = meta.get("units", "")
        data.frequency = meta.get("frequency", req.frequency)
        data.metadata = _ensure_wrapped_notes(meta)

    if req.fetch_observations:
        obs = tools.call_tool(
            "get_series_observations",
            {
                "series_id": series_id,
                "start_date": req.start_date,
                "end_date": req.end_date,
                "frequency": req.frequency,
            },
            caller="data_agent",
        )
        if obs.get("error"):
            data.error = f"observations: {obs['error']}"
        else:
            data.observations = obs.get("observations", [])

    return data


class DataAgent:
    """One instance ↔ one series. Independent of every other instance."""

    def __init__(self, request: FetchRequest):
        self.request = request
        self.started_at: float | None = None
        self.finished_at: float | None = None

    async def run(self) -> DataAgentResult:
        self.started_at = time.monotonic()
        try:
            series = await asyncio.to_thread(_fetch_one, self.request)
            result = DataAgentResult(request=self.request, series=series)
        except Exception as exc:  # never let one agent's crash kill the gather
            result = DataAgentResult(
                request=self.request, error=f"data_agent_exception: {exc}"
            )
        self.finished_at = time.monotonic()
        return result

    @property
    def elapsed(self) -> float:
        if self.started_at is not None and self.finished_at is not None:
            return self.finished_at - self.started_at
        return 0.0


async def _gather(agents: list[DataAgent], *, parallel: bool) -> list[DataAgentResult]:
    if parallel:
        return list(await asyncio.gather(*(a.run() for a in agents)))
    return [await a.run() for a in agents]


def build_agents(plan: QueryPlan) -> list[DataAgent]:
    """One `DataAgent` per FetchRequest in the plan. Exposed so the
    orchestration layer can compose these with a `NewsAgent` under one shared
    `asyncio.gather` when a query needs both sources concurrently — `fetch_all`
    below is the data-only convenience wrapper for standalone use."""
    return [DataAgent(req) for req in plan.fetches]


def fetch_all(plan: QueryPlan, *, parallel: bool = True) -> DataFetchBatch:
    """Run one Data Agent per FetchRequest and collect the results in request
    order. `parallel=True` runs them concurrently; `parallel=False` is the same
    code path, one at a time (used to benchmark the difference).

    A single-series plan is just a batch of length 1 — identical behaviour to
    the sequential phase-1 pipeline. Data-only: when a plan also needs news,
    the orchestration layer uses `build_agents` directly instead so both
    sources share one `asyncio.gather`.
    """
    if not plan.ok:
        bad = FetchRequest(series_id="", start_date="", end_date="")
        return DataFetchBatch(
            results=[DataAgentResult(request=bad, error=plan.error or "invalid_plan")],
            agents=[],
            wall_seconds=0.0,
            parallel=parallel,
        )

    agents = build_agents(plan)
    started = time.monotonic()
    results = asyncio.run(_gather(agents, parallel=parallel))
    wall = time.monotonic() - started
    return DataFetchBatch(
        results=results, agents=agents, wall_seconds=wall, parallel=parallel
    )
