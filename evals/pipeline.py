"""
The orchestrator-worker pipeline's two eval suites (src/orchestration.py):

  routing    `orchestrator.plan_query` only — is the *plan* right?
  execution  the whole pipeline — which workers ran, retries, graceful
             degradation, and whether the answer matches the routing.

Cases live in `pipeline_cases.py`. Both suites are deterministic (no model)
and always run on the offline FRED/news fixtures, so `python -m evals` grades
them alongside the supervisor dataset on any backend, and pytest
(`tests/test_routing.py`, `tests/test_pipeline_eval.py`) prints the tables.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass

from evals import pipeline_cases


@dataclass
class Row:
    id: str
    category: str
    problems: list[str]
    xfail: str = ""

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def hard_failure(self) -> bool:
        return bool(self.problems) and not self.xfail


@contextlib.contextmanager
def _offline() -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in ("FRED_OFFLINE", "NEWS_OFFLINE")}
    os.environ.update(FRED_OFFLINE="1", NEWS_OFFLINE="1")
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_routing() -> list[Row]:
    from agents.orchestrator import plan_query

    with _offline():
        return [Row(c.id, c.category, c.check(plan_query(c.query)), c.xfail)
                for c in pipeline_cases.ROUTING_CASES]


def run_execution() -> list[Row]:
    from agents.orchestrator import plan_for_series
    from orchestration import run_query

    rows = []
    with _offline():
        for c in pipeline_cases.PIPELINE_CASES:
            plan = plan_for_series(c.query, c.series) if c.series is not None else None
            result = run_query(c.query, plan=plan) if plan else run_query(c.query)
            rows.append(Row(c.id, c.category, c.check(result), c.xfail))
    return rows


def table(title: str, rows: list[Row]) -> str:
    """The plain-text table pytest prints."""
    passed = sum(r.passed for r in rows)
    xfailed = sum(bool(r.problems and r.xfail) for r in rows)
    failed = sum(r.hard_failure for r in rows)
    lines = [f"\n{title}: {passed}/{len(rows)} passed"
             + (f", {xfailed} known gaps" if xfailed else "")
             + (f", {failed} FAILING" if failed else "")]
    for r in rows:
        mark = "PASS" if r.passed else ("XFAIL" if r.xfail else "FAIL")
        lines.append(f"  [{mark:^5}] {r.id:<28} {r.category}")
        lines += [f"          - {p}" for p in r.problems]
        if r.xfail and r.problems:
            lines.append(f"          (known gap: {r.xfail})")
    return "\n".join(lines) + "\n"
