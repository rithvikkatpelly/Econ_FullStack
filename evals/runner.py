"""
Load the dataset, replay every case through the supervisor, score it, and
return structured results. `report.py` turns those into evals/REPORT.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import cost_tracker
from agents import Trace
from agents.supervisor import Supervisor, framework
from evals import metrics

DATASET = Path(__file__).resolve().parent / "dataset.jsonl"

# Projected API cost of an equivalent live run, at claude-opus-5 list prices
# ($5 / $25 per 1M input / output tokens). Offline runs don't spend anything —
# this is a modelled figure so the cost column isn't empty.
_IN_PER_MTOK, _OUT_PER_MTOK = 5.0, 25.0


@dataclass
class CaseResult:
    id: str
    query: str
    scores: dict[str, float | None]
    leaf_tools: list[str]
    expected_leaf_tools: list[str]
    series_used: list[str]
    expected_series: list[str]
    delegations: list[str]
    risk_signal: str | None
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    projected_cost_usd: float
    # Set when the run itself blew up (a live provider error, say) rather than
    # scoring badly. The case fails; the rest of the suite still runs.
    error: str | None = None

    @property
    def extra_calls(self) -> int:
        return metrics.extra_calls(self.expected_leaf_tools, self.leaf_tools)

    @property
    def failed_checks(self) -> list[str]:
        return [m for m, v in self.scores.items() if v is not None and v < 1.0]

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(v == 1.0 for v in self.scores.values() if v is not None)


@dataclass
class Suite:
    backend: str
    framework: str = "native"
    results: list[CaseResult] = field(default_factory=list)


def load_cases(path: Path = DATASET) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def select_cases(cases: list[dict], max_cases: int | None) -> list[dict]:
    """Cap the suite for a paid run. Injection probes are always kept (they
    are the cases a model is most likely to regress on), the rest are taken
    in dataset order, and the result keeps dataset order."""
    if not max_cases or max_cases >= len(cases):
        return cases
    probes = [c for c in cases if "injection" in c["id"]]
    keep = {c["id"] for c in probes[:max_cases]}
    for c in cases:
        if len(keep) >= max_cases:
            break
        keep.add(c["id"])
    return [c for c in cases if c["id"] in keep]


def run_case(case: dict) -> CaseResult:
    cost_tracker.reset_budget()  # each case gets its own session budget
    trace = Trace()
    try:
        if framework() == "adk":
            from econ_adk import pipeline

            pipeline.run(case["query"], trace)
        else:
            Supervisor(trace).run(case["query"])
    except Exception as exc:  # noqa: BLE001 - recorded on the case, suite continues
        return _errored(case, trace, exc)
    scores = metrics.score_case(case, trace)
    cost = (
        trace.input_tokens / 1_000_000 * _IN_PER_MTOK
        + trace.output_tokens / 1_000_000 * _OUT_PER_MTOK
    )
    return CaseResult(
        id=case["id"],
        query=case["query"],
        scores=scores,
        leaf_tools=[c.name for c in trace.leaf_calls("economic_data_agent")],
        expected_leaf_tools=case["expected_leaf_tools"],
        series_used=trace.series_used,
        expected_series=case["expected_series"],
        delegations=[d.to for d in trace.delegations],
        risk_signal=trace.risk_signal,
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        elapsed_ms=round(trace.elapsed_ms, 1),
        projected_cost_usd=round(cost, 6),
    )


def _errored(case: dict, trace: Trace, exc: Exception) -> CaseResult:
    return CaseResult(
        id=case["id"],
        query=case["query"],
        scores={},
        leaf_tools=[c.name for c in trace.leaf_calls("economic_data_agent")],
        expected_leaf_tools=case["expected_leaf_tools"],
        series_used=trace.series_used,
        expected_series=case["expected_series"],
        delegations=[d.to for d in trace.delegations],
        risk_signal=trace.risk_signal,
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        elapsed_ms=round(trace.elapsed_ms, 1),
        projected_cost_usd=0.0,
        error=f"{type(exc).__name__}: {str(exc)[:200]}",
    )


def run_suite(max_cases: int | None = None) -> Suite:
    backend = os.environ.get("AGENT_BACKEND", "stub").strip().lower()
    # The harness is hermetic: force offline FRED unless the caller really
    # wants live data (and has said so alongside a live backend).
    os.environ.setdefault("FRED_OFFLINE", "1")
    suite = Suite(backend=backend, framework=framework())
    for case in select_cases(load_cases(), max_cases):
        suite.results.append(run_case(case))
    return suite
