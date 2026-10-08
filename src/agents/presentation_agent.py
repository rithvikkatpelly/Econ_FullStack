"""
Presentation Agent — the last worker in the pipeline.

It turns the Analysis Agent's *structured* output (`AnalysisResult`: numbers,
correlations, matched news themes, a list of failed series) into a bounded,
sectioned summary — never a raw dump of everything fetched. This is the same
"shape the result before returning it" discipline `cost_tracker.py` applies to
tool payloads, moved to the end of the agent pipeline: the Analysis Agent now
does *only* the maths, and formatting lives here.

No tools, no network — like the Analysis Agent, this module imports nothing
that can reach FRED, the news API, or the MCP tools (enforced by a test).

Trust boundary: a failed worker's `reason` string can originate outside our
code (a FRED/NewsAPI error body, a poisoned upstream message). `_safe_reason`
maps it through a fixed set of labels rather than interpolating the raw string
into `summary`, the same fixed-vocabulary defense `analysis_agent._extract_themes`
uses for headline text. See `security.wrap_agent_message`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.analysis_agent import AnalysisResult

# Failed-fetch reasons are mapped to these before they reach the summary — a
# raw external error string never lands in user-facing text.
_SAFE_REASON = {
    "rate_limited": "rate-limited",
    "fred_api_error": "data provider error",
    "news_api_error": "news provider error",
    "validation_error": "invalid request",
    "not_found_by_search": "no matching FRED series found",
    "session_budget_exceeded": "response budget exceeded",
    "insufficient_data": "not enough data points",
    "no_headlines": "no headlines found",
}


def _safe_reason(reason: str | None) -> str:
    if not reason:
        return "fetch failed"
    for code, label in _SAFE_REASON.items():
        if code in reason:
            return label
    return "fetch failed"


@dataclass
class Section:
    label: str  # "data" | "headlines" | "note" | "unavailable"
    text: str


@dataclass
class PresentationResult:
    summary: str = ""                                   # flat rendering — PipelineResult.answer
    sections: list[Section] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.summary)


def _failure_reasons(analysis: AnalysisResult) -> str:
    bits = []
    if analysis.failed_series:
        bits.append(
            "; ".join(
                f"{f['series_id'] or 'requested series'}: {_safe_reason(f.get('reason'))}"
                for f in analysis.failed_series
            )
        )
    if analysis.news and analysis.news.error:
        bits.append(f"news: {_safe_reason(analysis.news.error)}")
    return "; ".join(bits) or "no data"


def present(analysis: AnalysisResult) -> PresentationResult:
    """`AnalysisResult` → `PresentationResult`. Deterministic; no side effects."""
    if analysis.error == "no_analyzable_data":
        text = f"Could not analyse the requested data ({_failure_reasons(analysis)})."
        return PresentationResult(
            summary=text,
            sections=[Section("unavailable", text)],
            error="no_analyzable_data",
        )

    usable = [a for a in analysis.per_series if a.error is None]
    data_parts = [a.summary for a in usable]
    if analysis.cross_series and analysis.cross_series.summary:
        data_parts.append(analysis.cross_series.summary)

    news_parts: list[str] = []
    if analysis.news is not None and analysis.news.error is None:
        news_parts = [analysis.news.summary]

    sections: list[Section] = []
    if data_parts:
        sections.append(Section("data", " ".join(data_parts)))
    if news_parts:
        sections.append(Section("headlines", " ".join(news_parts)))
    if analysis.failed_series:
        notes = "; ".join(
            f"{f['series_id']} ({_safe_reason(f.get('reason'))})"
            for f in analysis.failed_series
        )
        sections.append(
            Section("note", f"{len(analysis.failed_series)} series could not be fetched — {notes}.")
        )

    rendered = []
    for s in sections:
        if s.label == "data":
            rendered.append("Data: " + s.text)
        elif s.label == "headlines":
            rendered.append(
                "Headlines suggest (unverified reporting, not confirmed fact): " + s.text
            )
        elif s.label == "note":
            rendered.append("Note: " + s.text)
        else:
            rendered.append(s.text)

    return PresentationResult(summary="  ".join(rendered), sections=sections, error=None)
