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

- [x] **Per-run token budget** — `cost_tracker.run_budget` (a ContextVar
      that follows the run into worker threads); each `/agent/*` question
      gets its own, so concurrent runs and the landing page can't starve each
      other. Shared-budget check-and-record made atomic.
- [x] **Streamed answers** — `Model.stream_turn`; Gemini's
      `generate_content_stream` for the Report Agent, emitted as
      `report_delta` events and typed out in the UI
- [x] **Follow-up questions** — up to 3 earlier turns sent as wrapped,
      bounded context (`agents/conversation.py`); the stub resolves "what
      about since 2015?" by inheriting the earlier turn's series
- [x] **Live Gemini eval in CI** — weekly + on demand, opt-in, case-capped
      (probes always kept), pass-rate gate, per-case error isolation

- [x] **First live Gemini runs** — found and fixed: no SDK retries on
      transient 503s; 429s now wait Google's `retryDelay` (shown in the UI)
      and per-day quotas fail fast; model fallback + circuit breaker on
      overload/quota; agents now told today's date ("last 5 years" was
      2019-2024 in 2026); supervisor stops once the report exists (it was
      retyping it, or re-delegating)

- [x] **Google Agent Development Kit** — `src/econ_adk`: the supervisor +
      specialists as ADK `LlmAgent`s / `AgentTool`s / `FunctionTool`s, now
      the default orchestrator (`AGENT_FRAMEWORK=adk`). Proven equivalent to
      the native one on all 20 eval cases; CI grades both. `StubLlm` keeps it
      keyless offline; `ResilientGemini` (ADK's Gemini + quota waits,
      fallback, report streaming, failure surfacing) runs it live — verified
      end to end on live Gemini through the web UI. `adk web` works.
- [x] **Mid-loop model fallback** — Gemini 3 rejects foreign thought
      signatures (verified live); a failover now re-signs only the turns the
      new model didn't produce with `skip_thought_signature_validator`

- [x] **Billing-free public demo, configured** — `render.yaml` (API on
      Render's free plan), `firebase.json` + `demo.yml` (frontend on Firebase
      Hosting), Gemini API free tier. A used-up quota no longer fails the
      question: the API reruns it on the offline stub (per-run
      `backend_override`, a ContextVar), labels it (`degraded`, a UI notice,
      a `restart` fallback event that clears the half-streamed answer) and
      skips Gemini until the reset (Google's `retryDelay`, else midnight
      Pacific); `/health` reports `agent_answering_on`. "Waking the server"
      banner for the free host's cold start.

- [x] **Follow-ups inherit the earlier period** on the stub, not just the
      series ("and core PCE?" after a 2016-2020 question fetches 2016-2020)
- [x] **Adversarial eval cases** (20 → 25): injection in the question itself
      (system-prompt leak, exfiltration), the poisoned series inside a
      comparison, ID + concept mixed, a request over the 4-series cap. They
      found two stub bugs (an uppercase ID hid the concepts beside it; the
      report cited series a capped comparison never fetched), now fixed.
- [x] **Live-run hardening** — the first live eval hung for 50 minutes on a
      silent connection and lost cases to `httpx.ReadError`: both Gemini
      clients now share a request timeout (`GEMINI_TIMEOUT_S`) and retry
      dropped connections with backoff (never after text reached the user)
- [x] **First live Gemini eval published** — `evals/REPORT.gemini.md`, read
      in `docs/live-eval.md`: 0/8 strict, but 100% injection resistance,
      series grounding and argument validity; the misses are extra tool
      calls and over-delegation

### Next

- [ ] Go live: create the Render service and Firebase project
      (DEPLOYMENT.md, "Free public demo") and link the URL from the README
- [ ] Act on the first live eval (docs/live-eval.md): supervisor skips
      Research/Risk on plain fetches; data agent stops searching once it has
      an ID; split tool selection into "required calls, in order" (graded)
      and "extra calls" (reported); rerun against the 0/8 baseline
- [ ] Global agent rate limits (Memorystore) instead of per-instance buckets
- [ ] Export `Trace` events as OpenTelemetry spans to Cloud Trace

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
- [ ] Expand the supervisor eval dataset toward 50 cases (25 now, 5 adversarial)
- [ ] Deploy the MCP server over HTTP with per-session rate-limit keys
- [ ] Observability: structured spans per agent, exported to a trace viewer
- [ ] A short screen recording in the README
