"""
Orchestrator — the planning stage of the pipeline.

    Orchestrator → Data Agent(s) / News Agent → Analysis Agent

Its whole job is to turn a natural-language query into a **structured plan**:
which FRED series to fetch (if any), whether to search news (if any), over
what window, single-series vs. comparison, and — when it can't do that
cleanly — a structured refusal or a clarification request instead of a guess.
It never touches a tool itself; series resolution is against the local
catalog (`catalog.resolve` / `catalog.score_query`), and the news decision is
a keyword check — both project knowledge, not network calls.

Source routing is **explicit on the plan**, not inferred downstream:
`needs_data` and `needs_news` (bools) say which agents to invoke, and
`sources` is the derived list of both.

  "How has CPI changed over the last 5 years?"   -> needs_data only
  "What's driving recent inflation news?"        -> needs_data AND needs_news
  "What are the top headlines about the Fed?"    -> needs_news only

The plan carries a *list* of `FetchRequest`s; the orchestration layer runs one
Data Agent per request and, when needed, one News Agent, concurrently.

This orchestrator is **deterministic** — regex + dictionary lookups, no LLM,
so it has no instruction-following surface. An embedded "ignore previous
instructions…" in a query is just (uninterpretable) text: it resolves to no
series and comes back `cannot_fulfill`. When this is swapped for an LLM
planner, the defence against that is a tightly-scoped system prompt that keeps
the role narrow — never a keyword blocklist.

Errors are returned on the dataclass (`QueryPlan.error`), never raised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

import catalog
import dates

# How many series one query is allowed to pull — mirrors the compare_series
# cap so a plan can't fan out unboundedly.
MAX_SERIES = 4

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_LAST_N_RE = re.compile(r"(?:last|past|previous)\s+(\d{1,2})\s+years?", re.IGNORECASE)
_VAGUE_RECENT_RE = re.compile(
    r"\b(now|today|current|currently|latest|recent|recently|lately|nowadays|these days)\b",
    re.IGNORECASE,
)
# Presence of any of these is what makes a query a *news* query. Deliberately
# narrow — a false positive here just means an extra (cheap, harmless) News
# Agent call; a false negative means a news query gets treated as data-only,
# which the "what's driving recent inflation news" example is written to catch.
_NEWS_HINTS_RE = re.compile(
    r"\b(news|headline|headlines|article|articles|reporting|reports|coverage)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchRequest:
    """One series the Data Agent should retrieve.

    `search_text`, when set, records that the orchestrator could not resolve
    the query to a precise series name — `series_id` is a best guess, and the
    Data Agent confirms it via `search_series` first (or swaps in the top hit
    when the guess isn't among the first few; see `data_agent._resolve`).
    """

    series_id: str
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    frequency: str = "m"
    fetch_observations: bool = True
    fetch_metadata: bool = True
    search_text: str = ""


@dataclass(frozen=True)
class QueryPlan:
    user_query: str
    fetches: tuple[FetchRequest, ...] = ()
    rationale: str = ""
    comparison: bool = False           # multi-series comparison vs. single lookup
    resolution: str = "exact"          # "exact" | "search" | "none"
    assumptions: tuple[str, ...] = ()  # things the orchestrator had to guess
    requested_series: int = 0          # how many the query seemed to ask for
    clarification: str = ""            # question to put back to the user
    error: str | None = None           # None | empty_query | cannot_fulfill
                                       # | needs_clarification
    detail: str = ""
    # --- source routing: explicit on the plan, not inferred downstream ---
    needs_data: bool = False
    needs_news: bool = False
    news_query: str = ""
    news_start_date: str = ""
    news_end_date: str = ""

    @property
    def ok(self) -> bool:
        if self.error is not None:
            return False
        data_ok = (not self.needs_data) or bool(self.fetches)
        news_ok = (not self.needs_news) or bool(self.news_query)
        return (self.needs_data or self.needs_news) and data_ok and news_ok

    @property
    def status(self) -> str:
        return self.error or "ok"

    @property
    def sources(self) -> tuple[str, ...]:
        wanted = (("data", self.needs_data), ("news", self.needs_news))
        return tuple(s for s, want in wanted if want)

    @property
    def mode(self) -> str:
        if self.error is not None:
            return "error"
        if self.needs_news and not self.needs_data:
            return "news_only"
        if self.needs_news and self.needs_data:
            return "data_and_news"
        return "comparison" if self.comparison else "single_series"

    @property
    def capped(self) -> bool:
        """True when the query asked for more series than the plan carries."""
        return self.requested_series > len(self.fetches)

    @property
    def via_search(self) -> bool:
        return self.resolution == "search" or any(f.search_text for f in self.fetches)

    @property
    def tool_call_count(self) -> int:
        n = sum(f.fetch_observations + f.fetch_metadata for f in self.fetches)
        return n + (1 if self.needs_news else 0)


# --- date window ------------------------------------------------------


def _parse_window(query: str, today: date | None = None) -> tuple[str, str, str, tuple[str, ...]]:
    """(start_date, end_date, frequency, assumptions).

    `assumptions` is non-empty whenever no explicit range was given and the
    orchestrator had to pick a default — so an ambiguous date range is always
    *flagged*, never silently guessed.
    """
    today = today or date.today()
    q = query.lower()

    if "quarter" in q:
        freq = "q"
    elif "annual" in q or "yearly" in q:
        freq = "a"
    elif "daily" in q:
        freq = "d"
    else:
        freq = "m"

    m = _LAST_N_RE.search(q)
    if m:
        start = date(today.year - int(m.group(1)), today.month, 1)
        return start.isoformat(), today.isoformat(), freq, ()

    if "decade" in q:
        start = date(today.year - 10, today.month, 1)
        return start.isoformat(), today.isoformat(), freq, ()

    ytd = dates.this_year_window(query, today)
    if ytd:
        return ytd.start, ytd.end, freq, ()

    before = dates.before_year_window(query)
    if before:
        return before.start, before.end, freq, (before.note,)

    then = dates.now_vs_year(query)
    if then:
        # Two points in time, not a trend: fetch a window spanning both so the
        # first and latest values are the two ends, and say so.
        return f"{then}-01-01", today.isoformat(), freq, (
            f"'now vs {then}' is a two-point comparison; fetched {then}-01-01.."
            f"{today.isoformat()} so both ends are in the data — compare the first "
            f"and latest values, not the path between",
        )

    years = sorted({int(y) for y in _YEAR_RE.findall(query)})
    if len(years) >= 2:
        return f"{years[0]}-01-01", f"{years[-1]}-12-01", freq, ()
    if len(years) == 1:
        return f"{years[0]}-01-01", today.isoformat(), freq, ()

    event = dates.event_window(query, today)
    if event:
        return event.start, event.end, freq, (event.note,)

    # No explicit window — pick a default and say so.
    if _VAGUE_RECENT_RE.search(q):
        start = date(today.year - 1, today.month, 1)
        note = (f"no date range given ('recently'/'current'); assumed the last "
                f"12 months ({start.isoformat()}..{today.isoformat()})")
    else:
        start = date(today.year - 5, today.month, 1)
        note = (f"no date range given; assumed the last 5 years "
                f"({start.isoformat()}..{today.isoformat()})")
    return start.isoformat(), today.isoformat(), freq, (note,)


# News coverage is shallow (NewsAPI's free tier is ~1 month; `search_news`
# rejects ranges over a year). The data window can be years wide; the news
# window is clamped to this, independently, and the clamp is flagged.
_NEWS_MAX_WINDOW_DAYS = 60


def _news_window(start: str, end: str) -> tuple[str, str, str]:
    """(news_start, news_end, note) — clamps a wide data window down to
    something `search_news` accepts."""
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return start, end, ""
    floor = e - timedelta(days=_NEWS_MAX_WINDOW_DAYS)
    if s >= floor:
        return start, end, ""
    return floor.isoformat(), end, (
        f"news coverage is shallow; searched news only over {floor.isoformat()}..{end} "
        f"(the data window is wider)"
    )


# Words that mark a question as being about an economic or market indicator,
# even one the built-in catalog doesn't know (it has seven series; FRED has
# far more). Out-of-scope questions — the weather, restaurants, a bare
# prompt injection — contain none of these and are still refused.
_INDICATOR_RE = re.compile(
    r"(s&p\b|\bdow jones\b|\bnasdaq\b|\b(index|indices|stocks?|yields?|"
    r"prices?|sales|production|housing|starts|mortgages?|wages?|earnings|payrolls?|"
    r"claims|sentiment|confidence|inventor(y|ies)|debt|deficit|exchange rates?|"
    r"dollar|oil|gasoline|gold|money supply|m2|savings|spending|income|imports|"
    r"exports|trade balance|productivity|vacanc(y|ies)|delinquenc(y|ies))\b)",
    re.IGNORECASE,
)


# --- planning ---------------------------------------------------------


def _needs_clarification_for_too_many_series(user_query: str, exact: list[str]) -> QueryPlan:
    return QueryPlan(
        user_query,
        error="needs_clarification",
        requested_series=len(exact),
        clarification=(
            f"You named {len(exact)} series ({', '.join(exact)}). I can "
            f"compare at most {MAX_SERIES} at once — which {MAX_SERIES}?"
        ),
        detail=f"{len(exact)} series requested; the compare limit is {MAX_SERIES}.",
    )


def plan_query(user_query: str, today: date | None = None) -> QueryPlan:
    if not user_query or not user_query.strip():
        return QueryPlan(user_query, error="empty_query", detail="No query provided.")

    q = user_query.strip()
    start, end, freq, assumptions = _parse_window(q, today)
    exact = catalog.resolve(q)

    # --- news queries: decide data+news vs. news-only -------------------
    # A *precise* catalog hit (not the loose score_query fallback) is the
    # signal for "this news query also wants numbers" — see the module
    # docstring's three examples.
    if _NEWS_HINTS_RE.search(q):
        if exact and len(exact) > MAX_SERIES:
            return _needs_clarification_for_too_many_series(user_query, exact)

        needs_data = bool(exact)
        fetches = tuple(
            FetchRequest(series_id=sid, start_date=start, end_date=end, frequency=freq)
            for sid in exact
        )
        comparison = len(fetches) > 1
        news_start, news_end, news_note = _news_window(start, end)
        data_note = f", plus Data Agent for {', '.join(exact)}" if needs_data else ""
        rationale = (
            f"News Agent for {q!r} ({news_start}..{news_end}){data_note}"
            + (f" over {start}..{end}" if needs_data else "") + "."
        )
        return QueryPlan(
            user_query=user_query,
            fetches=fetches,
            rationale=rationale,
            comparison=comparison,
            resolution="exact" if needs_data else "none",
            assumptions=assumptions + ((news_note,) if news_note else ()),
            requested_series=len(exact),
            needs_data=needs_data,
            needs_news=True,
            news_query=q,
            news_start_date=news_start,
            news_end_date=news_end,
        )

    # --- pure data queries (unchanged from phases 1-3) -------------------
    if exact:
        if len(exact) > MAX_SERIES:
            return _needs_clarification_for_too_many_series(user_query, exact)
        return _build_plan(
            user_query, exact, start, end, freq,
            resolution="exact", assumptions=assumptions, requested_series=len(exact),
        )

    scored = catalog.score_query(q)
    if not scored and _INDICATOR_RE.search(q):
        # Names an indicator the built-in catalog doesn't have (the S&P 500,
        # housing starts, oil prices...). FRED has hundreds of thousands of
        # series: let the Data Agent find it by search instead of refusing.
        return QueryPlan(
            user_query=user_query,
            fetches=(FetchRequest(series_id="", start_date=start, end_date=end,
                                  frequency=freq, search_text=q),),
            rationale=f"Not in the built-in catalog; Data Agent searches FRED for {q!r}.",
            resolution="search",
            assumptions=assumptions + ("not one of the built-in series; the series is "
                                       "whatever FRED search ranks first",),
            requested_series=1,
            needs_data=True,
        )
    if not scored:
        return QueryPlan(
            user_query,
            error="cannot_fulfill",
            detail="This doesn't map to any economic series I can fetch from FRED, "
                   "and doesn't look like a news query either.",
        )

    # In scope, but not a precise series name → route through search_series.
    best = scored[0][0]
    fetch = FetchRequest(
        series_id=best, start_date=start, end_date=end, frequency=freq, search_text=q
    )
    return QueryPlan(
        user_query=user_query,
        fetches=(fetch,),
        comparison=False,
        resolution="search",
        assumptions=assumptions,
        requested_series=1,
        needs_data=True,
        rationale=(
            f"'{q}' is not a precise series name; closest catalog match is {best}. "
            f"Data Agent to confirm via search_series before fetching. "
            f"Window {start}..{end} at frequency '{freq}'."
        ),
    )


def plan_for_series(
    user_query: str,
    series_ids: list[str],
    *,
    today: date | None = None,
) -> QueryPlan:
    """Build a plan for an explicit list of series IDs (window still parsed
    from the query). For callers that already know which series they want —
    and for exercising the partial-failure path with a deliberately bad ID."""
    if not series_ids:
        return QueryPlan(user_query, error="no_series_identified", detail="No series given.")
    start, end, freq, assumptions = _parse_window(user_query, today)
    kept = list(series_ids)[:MAX_SERIES]
    return _build_plan(
        user_query, kept, start, end, freq,
        resolution="exact", assumptions=assumptions, requested_series=len(series_ids),
    )


def _build_plan(
    user_query: str,
    series: list[str],
    start: str,
    end: str,
    freq: str,
    *,
    resolution: str,
    assumptions: tuple[str, ...],
    requested_series: int,
) -> QueryPlan:
    fetches = tuple(
        FetchRequest(series_id=sid, start_date=start, end_date=end, frequency=freq)
        for sid in series
    )
    comparison = len(fetches) > 1
    kind = "comparison across" if comparison else "single-series lookup of"
    capped_note = ""
    if requested_series > len(fetches):
        capped_note = (
            f" Query implied {requested_series} series; kept the first "
            f"{len(fetches)} (compare limit is {MAX_SERIES})."
        )
    rationale = (
        f"{kind} {len(series)} series ({', '.join(series)}); "
        f"window {start}..{end} at frequency '{freq}'. "
        f"One Data Agent per series to fetch observations + metadata.{capped_note}"
    )
    return QueryPlan(
        user_query=user_query,
        fetches=fetches,
        rationale=rationale,
        comparison=comparison,
        resolution=resolution,
        assumptions=assumptions,
        requested_series=requested_series,
        needs_data=True,
    )
