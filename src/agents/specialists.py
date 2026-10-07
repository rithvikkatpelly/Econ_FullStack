"""
The four specialist agents. Each is an `Agent` with a focused system prompt
and a deliberately small tool surface.

  * Economic Data Agent — the only agent with the data-fetching tools.
  * Research Agent      — metadata/source notes only; supplies framing.
  * Risk Agent          — no tools; reads the assembled indicators, emits a
                          machine-readable RISK_SIGNAL line plus rationale.
  * Report Agent        — no tools; writes the final grounded answer.
"""

from __future__ import annotations

import catalog
import tools
from agents.base import Agent
from agents.model import Model, make_model
from agents.trace import Trace

_METADATA_ONLY = [s for s in tools.TOOL_SCHEMAS if s["name"] == "get_series_metadata"]

# The headline series, so the agent can fetch them by ID without a search.
# (The live eval caught Gemini searching four times for "GDP".)
_KNOWN_SERIES = "\n".join(
    f"  {s.id}: {s.title} ({s.aliases[0]})"
    for s in catalog.CATALOG.values()
    if s.id != "INJTEST"
)

ECONOMIC_DATA_SYSTEM = f"""\
You are the Economic Data Agent. Given a data request, resolve the right FRED
series IDs and fetch the observations needed to answer it, in as few calls as
possible.

Series you can fetch directly by ID, no search needed:
{_KNOWN_SERIES}

Rules:
- If the request names one of the series above (or gives a series ID), fetch
  it directly. Call search_series only for a concept described some other
  way, at most once per concept, then use the best match.
- Prefer compare_series when the task is about 2-4 series over one window;
  use get_series_observations for a single series. Fetch each series once.
- Don't call get_series_metadata: source notes are the Research Agent's job.
- Always pass an explicit start_date and end_date.
- When done, reply with a compact summary: each series ID, its units, the
  date range fetched, and the start/latest values. Do not editorialize —
  analysis is another agent's job.
"""

RESEARCH_SYSTEM = """\
You are the Research Agent. Provide short, factual framing for the economic
indicators in the task: what each one measures, the usual caveats, and any
structural breaks worth noting. You may call get_series_metadata for source
notes. Two or three sentences per indicator. Cite series IDs. No forecasts.
"""

RISK_SYSTEM = """\
You are the Risk Agent. You are given fetched indicator data and research
framing. Assess the direction of the risk the user asked about.

Respond with exactly this shape:
  RISK_SIGNAL: <rising|elevated|stable|easing>
  <2–4 sentences of rationale grounded in the specific numbers you were given>

Do not fetch data. Do not hedge into a non-answer.
"""

REPORT_SYSTEM = """\
You are the Report Agent. Write the final answer for the user: a tight
narrative (no more than ~150 words) that directly answers the question,
followed by an "Evidence" section listing every FRED series ID used and the
risk signal exactly as the Risk Agent gave it (rising, elevated, stable or
easing). Ground every claim in the data you were handed. If the data was
insufficient, say so plainly.
"""


def economic_data_agent(model: Model, trace: Trace) -> Agent:
    return Agent(
        "economic_data_agent", ECONOMIC_DATA_SYSTEM, tools.TOOL_SCHEMAS,
        tools.call_tool, model, trace,
    )


def research_agent(model: Model, trace: Trace) -> Agent:
    return Agent(
        "research_agent", RESEARCH_SYSTEM, _METADATA_ONLY, tools.call_tool, model, trace,
    )


def _no_tools(name: str, arguments: dict) -> dict:  # pragma: no cover - never called
    return {"error": "no_tools", "detail": f"{name} has no tools available."}


def risk_agent(model: Model, trace: Trace) -> Agent:
    return Agent("risk_agent", RISK_SYSTEM, [], _no_tools, model, trace)


def report_agent(model: Model, trace: Trace) -> Agent:
    return Agent("report_agent", REPORT_SYSTEM, [], _no_tools, model, trace)


BUILDERS = {
    "economic_data_agent": economic_data_agent,
    "research_agent": research_agent,
    "risk_agent": risk_agent,
    "report_agent": report_agent,
}


def build(name: str, trace: Trace, model: Model | None = None) -> Agent:
    return BUILDERS[name](model or make_model(name), trace)
