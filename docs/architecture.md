# Architecture

## Two surfaces, one tool implementation

```
                        ┌───────────────────────────┐
Claude Desktop ───MCP──▶ │  server.py (FastMCP)      │
                        │   rate limit + audit log  │
                        └────────────┬──────────────┘
                                     │
                                     ▼
                        ┌───────────────────────────┐
                        │  tools.py                 │   ← single implementation
                        │   validate → FRED → cost  │      of the 4 tools
                        │   guard → shape           │
                        └────────────┬──────────────┘
                                     │
                                     ▼
                        ┌───────────────────────────┐
                        │  fred_client.py           │
                        │   cache · offline fixture │
                        └────────────┬──────────────┘
                                     │
                        ┌────────────┴──────────────┐
                        │  catalog.py               │  ← which series exist,
                        │   aliases · search terms  │     shared by fixture,
                        └───────────────────────────┘     stub planner, evals
                                     ▲
                        ┌────────────┴──────────────┐
User query ───────────▶ │  agents/ (orchestrator)   │
                        └───────────────────────────┘
```

`server.py` and `agents/` never re-implement a tool — they both call
`tools.py`. That is what keeps the MCP contract and the agent contract from
drifting.

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

[`evals/`](../evals) replays [`dataset.jsonl`](../evals/dataset.jsonl)
through the supervisor and grades each run on tool selection, series
grounding, argument validity, orchestration, groundedness, and injection
resistance. `python -m evals` writes [`evals/REPORT.md`](../evals/REPORT.md)
and exits non-zero on any regression, so CI fails loudly.

> **Note:** the two sections above describe the original supervisor +
> 4-specialist pipeline. A second, newer pipeline
> (`orchestrator.py` → `data_agent.py`/`news_agent.py` → `analysis_agent.py`
> → `presentation_agent.py`, wired by `src/orchestration.py`) was built
> alongside it in later phases — see [`README.md`](../README.md) §§1–6 and the
> note below. The two aren't yet reconciled into one diagram; that's tracked
> in [`ROADMAP.md`](../ROADMAP.md).

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
