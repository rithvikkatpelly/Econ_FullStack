"""
Aggregate a `Suite` into headline numbers and a Markdown report.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime

from evals.pipeline import Row
from evals.runner import CaseResult, Suite

_METRIC_ORDER = [
    "tool_selection",
    "series_grounding",
    "argument_validity",
    "orchestration",
    "groundedness",
    "injection_resistance",
    "period",
]


def aggregate(suite: Suite) -> dict:
    per_metric: dict[str, float] = {}
    for m in _METRIC_ORDER:
        vals = [r.scores[m] for r in suite.results if r.scores.get(m) is not None]
        if vals:
            per_metric[m] = round(statistics.mean(vals), 4)

    n = len(suite.results)
    passed = sum(1 for r in suite.results if r.passed)
    mean_latency = statistics.mean(r.elapsed_ms for r in suite.results) if n else 0.0
    return {
        "backend": suite.backend,
        "framework": suite.framework,
        "n_cases": n,
        "pass_rate": round(passed / n, 4) if n else 0.0,
        "passed": passed,
        "metrics": per_metric,
        "mean_latency_ms": round(mean_latency, 1),
        "total_input_tokens": sum(r.input_tokens for r in suite.results),
        "total_output_tokens": sum(r.output_tokens for r in suite.results),
        "projected_total_cost_usd": round(sum(r.projected_cost_usd for r in suite.results), 4),
        "extra_calls": sum(r.extra_calls for r in suite.results if not r.error),
    }


def _case_row(r: CaseResult) -> str:
    mark = "✅" if r.passed else ("💥" if r.error else "❌")
    tools = " → ".join(r.leaf_tools) or (f"error: {r.error}" if r.error else "—")
    checks = ", ".join(m.replace("_", " ") for m in r.failed_checks)
    failed = "error" if r.error else (checks or "—")
    extra = "—" if r.error else str(r.extra_calls)
    return (
        f"| {mark} | `{r.id}` | {tools} | {extra} | "
        f"{','.join(r.series_used) or '—'} | {r.risk_signal or '—'} | {failed} | "
        f"{r.elapsed_ms:.0f} | {r.input_tokens + r.output_tokens} |"
    )


def _pipeline_section(routing: list[Row], execution: list[Row]) -> str:
    if not routing and not execution:
        return ""

    def score(rows: list[Row]) -> str:
        return f"{sum(r.passed for r in rows)}/{len(rows)}"

    rows = "\n".join(
        f"| {'✅' if r.passed else ('⚠️' if r.xfail else '❌')} | {suite} | `{r.id}` | "
        f"{r.category} | {'; '.join(r.problems) or '—'} |"
        for suite, group in (("routing", routing), ("execution", execution))
        for r in group
    )
    return f"""
## Orchestrator-worker pipeline

The second pipeline (`src/orchestration.py`: orchestrator → Data/News Agents →
Analysis → Presentation) is deterministic, so its suites always run offline and
gate on every backend: **routing {score(routing)}** (is the plan right?) and
**execution {score(execution)}** (did the right workers run, retry, degrade?).

| | Suite | Case | Category | Problems |
|---|---|---|---|---|
{rows}
"""


def to_markdown(suite: Suite, routing: list[Row] | None = None,
                execution: list[Row] | None = None) -> str:
    agg = aggregate(suite)
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    metric_lines = "\n".join(
        f"| {m.replace('_', ' ')} | {agg['metrics'][m] * 100:.1f}% |"
        for m in _METRIC_ORDER
        if m in agg["metrics"]
    )
    case_lines = "\n".join(_case_row(r) for r in suite.results)
    run_line = (
        f"orchestrator: `{agg['framework']}` · backend: `{agg['backend']}` · "
        f"{agg['n_cases']} cases"
    )
    return f"""# Evaluation report

_Generated {ts} · {run_line}_

**{agg['passed']}/{agg['n_cases']} cases pass all applicable checks
({agg['pass_rate'] * 100:.0f}%).**

| Metric | Score |
|---|---|
{metric_lines}

Extra data-agent tool calls beyond the expected ones (reported, not graded):
**{agg['extra_calls']}** across the suite.

Performance (this run): mean wall time **{agg['mean_latency_ms']:.0f} ms/query**,
{agg['total_input_tokens'] + agg['total_output_tokens']:,} total tokens,
projected cost at `claude-opus-5` list prices **${agg['projected_total_cost_usd']:.4f}**
for the whole suite.

{_backend_note(agg['backend'])}

## Per-case results

| | Case | Data-agent tools | Extra | Series | Risk | Failed checks | ms | Tokens |
|---|---|---|---|---|---|---|---|---|
{case_lines}
{_pipeline_section(routing or [], execution or [])}"""


def _backend_note(backend: str) -> str:
    if backend == "stub":
        return (
            "> The `stub` backend uses a deterministic offline planner, so its scores are a\n"
            "> regression fence on tool-contract and orchestration logic, not a measure of\n"
            "> model quality. Run `AGENT_BACKEND=gemini python -m evals` (or `anthropic`) for that."
        )
    return (
        f"> Live `{backend}` run against the offline FRED fixture: these scores measure the\n"
        "> model's tool selection, grounding and injection resistance. Token counts are the\n"
        "> provider's; the cost line is modelled at the rates above, not the provider's bill."
    )


def print_summary(suite: Suite) -> None:
    agg = aggregate(suite)
    print(f"framework={agg['framework']}  backend={agg['backend']}  cases={agg['n_cases']}  "
          f"pass={agg['passed']}/{agg['n_cases']} ({agg['pass_rate'] * 100:.0f}%)")
    for m, v in agg["metrics"].items():
        print(f"  {m:<22} {v * 100:5.1f}%")
    print(f"  extra_tool_calls        {agg['extra_calls']}")
    print(f"  mean_latency_ms         {agg['mean_latency_ms']:.0f}")
    print(f"  projected_cost_usd      ${agg['projected_total_cost_usd']:.4f}")
