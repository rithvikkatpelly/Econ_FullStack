# Architecture

## One system, two orchestrations

```
 Surfaces                     Orchestration                      Shared layers (one copy each)
 ─────────                    ─────────────                      ──────────────────────────────

 React UI (Cloud Run)                                            tools.py   the 4 FRED tools + search_news:
   │ POST /agent/stream ───▶ Supervisor + 4 specialists ──┐                 validate → fetch → cost guard
 FastAPI (Cloud Run)          ADK (default) or native,    │                 → shape; untrusted text wrapped
   │ /search /observations…   on Gemini (Vertex AI) or    ├────▶ catalog.py headline series, aliases,
   │ ─────────────────────────────────────────────────────┤                 search keywords + relevance
                              the offline stub            │      dates.py   events, "this year", now-vs-YYYY
 Streamlit, examples/ ─────▶ Orchestrator-worker pipeline ┤      security.py validation, untrusted wrapping
                              plan → Data/News Agents      │      cost_tracker per-run token budgets
                              (concurrent) → Analysis      │      fred_client / news_client
                              → Presentation, no model     │                 cache, live API or fixture
 Claude Desktop ──MCP──▶ server.py (rate limit, audit) ────┘
```

Three surfaces, two ways of orchestrating, and **one copy of everything
underneath**. Nothing above `tools.py` re-implements a tool, so the MCP
contract, the HTTP API and both agent pipelines can't drift apart; the
planners share `catalog.py` (which series exist, how to search FRED for the
rest) and `dates.py` (what "since the pandemic" means).

Why two orchestrations, and when each is used:

| | Supervisor + specialists | Orchestrator-worker pipeline |
|---|---|---|
| Entry point | `agents.supervisor.run_with_framework` | `orchestration.run_query` |
| Planning | a model (Gemini, Claude) or the offline stub, delegating with tool calls | deterministic `plan_query` → typed `QueryPlan` |
| Hand-offs | task strings between agents | typed dataclasses |
| Sources | FRED | FRED + news, fetched concurrently |
| Used by | the web app (`/agent/*`), the live demo | Streamlit, `examples/`, benchmarks |
| Graded by | `evals/dataset.jsonl` (50 cases) | `evals/pipeline_cases.py` (routing 18, execution 8) |

`python -m evals` grades **both** in one run and one report
([`evals/REPORT.md`](../evals/REPORT.md)).

## The hosted stack (Gemini full stack)

```
Browser ── React (Cloud Run, nginx)
   │  POST /agent/stream            ▲ text/event-stream: start · delegation ·
   │  {query, history≤3}            │   tool_call · agent_output · report_delta ·
   ▼                                │   final|error
FastAPI (Cloud Run) ─ backend/app/agent.py
   │  rate limit → run slot → run_budget → Supervisor in a worker thread
   │  Trace(listener=push) ──────────┘   (events only — never tool results)
   ▼
econ_adk/ (Google ADK: LlmAgents + AgentTools)  ── ResilientGemini ──▶ Gemini
   │   or agents/ (native loop, AGENT_FRAMEWORK=native) ── GeminiModel
   │                         (Gemini API key, or Vertex AI as the service account)
   ▼
tools.py ──▶ fred_client.py ──▶ FRED
```

`AGENT_FRAMEWORK` picks the orchestrator (`adk`, the default, or `native`)
and `AGENT_BACKEND` the model (`gemini` live, `stub` in tests and CI,
`anthropic` on the native orchestrator). Both orchestrators call the same
`tools.py` and write the same `Trace`, so nothing above or below them changes.

## The multi-agent layer

```
                    ┌── Economic Data Agent   tools: search_series,
                    │                          get_series_observations,
                    │                          compare_series, get_series_metadata
                    │
User ─▶ Supervisor ─┼── Research Agent         tools: get_series_metadata
       (delegates)  │
                    ├── Risk Agent             tools: none — reads indicators,
                    │                          emits RISK_SIGNAL + rationale
                    │
                    └── Report Agent           tools: none — grounded write-up
                                     │
                                     ▼
                              Final answer + Evidence
```

* Every agent — supervisor included — runs the **same tool-use loop**
  ([`agents/base.py`](../src/agents/base.py)). The supervisor's "tools" are
  four `delegate_to_*` calls; a specialist's tools are the real FRED tools.
* State between specialists is passed **explicitly** in the task string the
  supervisor writes. Specialists are stateless and independently testable.
* Each agent talks to a `Model` ([`agents/model.py`](../src/agents/model.py)):
  * `GeminiModel` — a real Gemini function-calling turn (Gemini API or
    Vertex AI); on the ADK orchestrator, `ResilientGemini` plays this role.
  * `AnthropicModel` — a real Claude tool-use turn (`claude-opus-5`).
  * `StubModel` — a deterministic offline planner
    ([`agents/stub.py`](../src/agents/stub.py)) so evals, CI, and the demo
    run with no API key. Same loop code either way; pick with
    `AGENT_BACKEND`.
* A single [`Trace`](../src/agents/trace.py) is threaded through the whole
  run and is the one thing the evaluation harness reads.

## Guardrails, and where they sit

| Guardrail | Layer | File |
|---|---|---|
| Input validation | `tools.py`, before any network call | `security.py` |
| Untrusted-content wrapping | `tools.py`, on every metadata response | `security.py` |
| Token / cost budget | `tools.py`, before returning a payload | `cost_tracker.py` |
| Per-agent iteration cap | agent loop | `agents/base.py` |
| Rate limiting | MCP boundary only | `rate_limit.py` |
| Audit logging | every `call_tool` | `audit_log.py` |

## Evaluation

`python -m evals` grades both orchestrations in one run:

* **Supervisor dataset** — [`dataset.jsonl`](../evals/dataset.jsonl), 50
  cases, on either orchestrator (`--framework adk|native`) and any backend:
  tool selection (required calls in order; extra calls reported), series
  grounding, argument validity, orchestration, groundedness, injection
  resistance, and the fetched period.
* **Pipeline suites** — [`pipeline_cases.py`](../evals/pipeline_cases.py):
  routing (is the plan right?) and execution (did the right workers run,
  retry, degrade?). Deterministic, always offline.

It writes [`evals/REPORT.md`](../evals/REPORT.md) and exits non-zero on any
regression, so CI fails loudly.

## The orchestrator-worker pipeline

```
run_query(query)
  │
  ├─ orchestrator.plan_query      NL query → QueryPlan (which series / news, window)
  │
  ├─ Data Agent × N  +  News Agent    one asyncio.gather; each .run() retried
  │     │                              once on a transient error before the
  │     ▼                              existing skip/degrade path
  │  [DataAgentResult], NewsAgentResult
  │
  ├─ analysis_agent.analyze      → AnalysisResult   (numbers only: %-change,
  │                                                  annualised rate, Pearson
  │                                                  correlation, matched news
  │                                                  themes, failed-series list)
  │
  └─ presentation_agent.present  → PresentationResult  (bounded sectioned
                                                        summary; failure reasons
                                                        mapped to safe labels)
```

Each stage hands the next a typed dataclass — never free text to re-parse.
`analysis_agent` computes; `presentation_agent` formats; the split keeps the
numeric layer with no user-facing-string concerns and the formatting layer
with no maths. Both are import-restricted (a test asserts neither can reach a
tool / FRED / the news API / the network).

## Phase 4: a second, heterogeneous source

`orchestrator.plan_query` now routes to Data Agent(s), a News Agent, or both,
explicit on the plan (`needs_data` / `needs_news`) — never inferred
downstream. When both are needed, `orchestration.run_query` builds a
`DataAgent` per series and one `NewsAgent`, then runs **all of them under one
`asyncio.gather`** so the FRED fetches and the news search genuinely overlap
in time rather than running data-then-news.

Everything about `search_news` deliberately mirrors the FRED tools —
[`news_client.py`](../src/news_client.py) mirrors `fred_client.py`
field-for-field, the tool follows the same validate → fetch → wrap →
structured-error shape in `tools.py`, and `cost_tracker.RunCost` needed zero
changes to add a `news_agent` cost row. The one thing that didn't generalize
for free: proving two *different* agent classes ran concurrently needed a
shared interval-overlap check, factored out into
[`agents/timing.py`](../src/agents/timing.py) rather than duplicated between
`DataFetchBatch.overlapped` and the orchestration layer's combined check.
