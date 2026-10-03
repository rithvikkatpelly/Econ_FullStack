# Roadmap

The project is built in layers, each landing as its own commit so the history
reads as a development story rather than one drop.

### Done

- [x] MCP server with four narrow, typed FRED tools + structured errors
- [x] In-memory idempotency cache
- [x] Context/cost optimization: prompt-cache-friendly layout, result shaping,
      per-session token budget guardrail with a shrink fallback
- [x] Input validation + prompt-injection defense + secrets hygiene
- [x] Security test suite
- [x] **Shared tool implementation** — MCP surface and agent surface call one
      `tools.py`, so they can't drift
- [x] **Multi-agent orchestration** — supervisor + Economic Data / Research /
      Risk / Report specialists, each running the same tool-use loop
- [x] **Model abstraction** — real Claude backend (`claude-opus-5`) plus a
      deterministic offline stub so everything runs keyless in CI
- [x] **Offline FRED fixture** — hermetic evals and tests
- [x] **Evaluation harness** — fixed dataset, expected tool-call sequences,
      six scored metrics, Markdown report, non-zero exit on regression
- [x] **Rate limiting** — token bucket at the MCP boundary
- [x] **Audit logging** — append-only JSONL of every call, rejection, limit hit
- [x] CI: tests + eval suite on every push
- [x] **Sequential agent pipeline** — orchestrator → data agent → analysis
      agent, typed dataclass hand-offs, per-agent + running-total cost
- [x] **Multi-series parallelism** — one Data Agent per series via
      `asyncio.gather`, partial-failure tolerance, cross-series correlation
- [x] **Per-worker retry** — each `.run()` retried once on a transient error
      (rate limit / provider 5xx / blip) before the skip/degrade path;
      `PipelineResult.retries` records where it fired
- [x] **Presentation worker** — `analysis_agent` does only maths;
      `presentation_agent` does only formatting (bounded, sectioned, safe
      failure labels). Both import-restricted like the Analysis Agent.
- [x] **Orchestrator routing eval** — 18 structural cases (single/comparison/
      cap/ambiguous/out-of-scope/injection/vague-date), 15 passing + 3 tracked
      `xfail` gaps
- [x] **HTTP interface + web UI** — `backend/app` (FastAPI, one endpoint per
      tool) is a second deployable surface over the same `src/tools.py` the MCP
      server calls; `backend/core/config.py` centralizes config with
      pydantic-settings. `frontend/` is a React + Vite page on top. `src/server.py`
      is untouched and still runs standalone.
- [x] **Second data source (news) + cross-source reasoning** — `news_client.py`
      mirrors `fred_client.py`; a News Agent runs concurrently with the Data
      Agent(s) under one `asyncio.gather`; the Analysis Agent separates
      "Data:" (numeric, FRED) from "Headlines suggest:" (hedged, unverified).
      Stress-tested the injection defense against a source built for
      adversarial text — 5/5 passing (`tests/test_news_injection.py`)

- [x] **Gemini full stack** — `GeminiModel` (Google Gen AI SDK, Gemini API
      or Vertex AI) behind the same `Model` interface, with thought-signature
      replay; `Trace` emits progress events; `POST /agent/ask` +
      `POST /agent/stream` (SSE) run the supervisor over HTTP with a
      per-client rate limit, run cap, and masked errors; a React "Ask the
      agent" section draws the live activity timeline; Cloud Run deploy runs
      the agents on Gemini via Vertex AI as the service account (no stored key)
- [x] **Streaming the supervisor's progress** (per-delegation events) — the
      `Trace` listener above
- [x] CI green again: repo root on pytest's `pythonpath` (bare `pytest`
      couldn't import `evals`), and the deploy job skips instead of failing
      until GCP is configured

### Next

- [ ] Live Gemini eval run in CI (gated, on a schedule, with a spend cap) —
      `AGENT_BACKEND=gemini python -m evals` against a Vertex AI project
- [ ] Per-run token budget (contextvar) so concurrent `/agent/*` runs can't
      drain each other through the process-global `SESSION_TOKEN_BUDGET`
- [ ] Global agent rate limits (Memorystore) instead of per-instance buckets
- [ ] Export `Trace` events as OpenTelemetry spans to Cloud Trace
- [ ] Multi-turn follow-ups in "Ask the agent" (pass prior turns as context)
- [ ] Stream the report text token-by-token (Gemini `generate_content_stream`)

- [ ] Wire `FetchRequest.search_text` through the Data Agent (act on the
      "route via search_series" routing decision, don't just record it)
- [ ] Search-backed catalog so series outside the fixed 7 are reachable
- [ ] Relative-event and compound date parsing in the orchestrator
- [ ] Reconcile the two agent pipelines (supervisor+specialists vs.
      orchestrator/data+news/analysis) into one diagram and one eval harness
- [x] **SQLite TTL cache** — `src/cache.py` `TTLCache` replaces the plain
      dict in `fred_client` / `news_client` (drop-in: same `key in c`,
      `c[key]`, iteration). Per-entry expiry; `:memory:` by default,
      `CACHE_PATH` to persist across restarts.
- [ ] Expand the supervisor eval dataset toward 50 cases; add adversarial queries
- [ ] Live-backend eval run in CI (gated, on a schedule, with a spend cap)
- [ ] Deploy the MCP server over HTTP with per-session rate-limit keys
- [ ] Observability: structured spans per agent, exported to a trace viewer
- [ ] A short screen recording in the README
