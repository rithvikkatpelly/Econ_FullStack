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

- [x] **Stub fallback for a used-up quota** — a used-up quota no longer fails the
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
- [x] **Acted on it: 0/8 → 8/8** — supervisor routes plain data requests
      past Research/Risk; the data agent knows the headline series IDs and
      stops searching; tool selection grades required calls in order and
      reports extras (0 on the rerun). Tokens 117k → 83k.

- [x] **Live on Cloud Run** (https://econ-data-frontend-kio6fmbpta-uc.a.run.app) — project `econ-fullstack-510918`:
      frontend and API on Cloud Run as dedicated least-privilege service
      accounts, Gemini on Vertex AI, FRED key in Secret Manager, images in
      Artifact Registry with a cleanup policy, $5 budget alert, keyless deploys
      from GitHub. First deploy found two things: the startup probe's 9 s
      window was too short for a cold start, and CORS needed both of Cloud
      Run's URLs for the frontend. All Google; the Render + Firebase config
      was dropped.

### Next

- [ ] Run the live eval on all 25 cases (needs a couple of fresh days of
      free-tier quota, or Vertex AI once deployed)
- [ ] Global agent rate limits (Memorystore) instead of per-instance buckets
- [x] **Cloud Trace** (`CLOUD_TRACE=1`, `backend/app/telemetry.py`): ADK's
      own spans for each agent, model and tool call, exported through Google's
      Telemetry API, under one root span per question with the run's shape
      (framework, the backend that answered, stub fallback, tokens, series)
      and an event per delegation and tool call. The question text isn't
      recorded. Free tier: 2.5M spans/month.

- [x] **`FetchRequest.search_text` acted on** — the pipeline's Data Agent
      runs `search_series` when the orchestrator only guessed the series:
      keeps the guess if it's in the top 3 hits, else takes the top hit, and
      records which (`SeriesData.resolution`)
- [x] **Series beyond the fixed 7** — live, FRED search reaches any series
      and the agents fetch it (verified on the deployed app: housing starts,
      `HOUST`); the stub reaches them through search too; the UI charts any ID
- [x] **Event and two-point dates** (`src/dates.py`, shared by the
      orchestrator and the stub): "since the pandemic" → March 2020, "since
      the Great Recession" → Dec 2007, "before 2008" → 2003-2007, "this
      year" → Jan 1; "now vs 2008" fetches both ends and flags it as a
      two-point comparison. Each reading is stated in `plan.assumptions`.
- [x] **Indicators outside the catalog are searched, not refused** — the
      orchestrator routes "the S&P 500" to FRED search; the Data Agent
      searches with keywords (FRED matches every word, so a whole question
      finds nothing) and trusts only a hit whose title shares a word with
      the question. Live: SP500, +10.7% year to date. The routing eval's
      three known gaps are all fixed: 18/18, no exceptions.
- [x] **Two pipelines, one diagram and one eval harness** —
      `docs/architecture.md` opens with one diagram: three surfaces, the two
      orchestrations, and the single shared layer under both (tools, catalog
      + search, dates, security, budgets, clients), plus when each is used.
      `python -m evals` grades the supervisor dataset *and* the pipeline's
      routing + execution suites (moved to `evals/pipeline_cases.py`) into
      one report, failing on a regression in any of them.
- [x] **SQLite TTL cache** — `src/cache.py` `TTLCache` replaces the plain
      dict in `fred_client` / `news_client` (drop-in: same `key in c`,
      `c[key]`, iteration). Per-entry expiry; `:memory:` by default,
      `CACHE_PATH` to persist across restarts.
- [x] **Eval dataset at 50 cases** (10 adversarial): event dates, "this
      year", follow-ups (incl. a poisoned history), outside-catalog searches,
      more phrasings, comparisons, analysis questions, and injection via role
      override, fake tool results, markdown exfiltration. New `period` check
      grades the fetched date range; cases can carry `history`. They found
      three stub mistakes ("how has X changed" read as analysis, "since the
      Great Recession" read as recession analysis, generic "inflation"
      adding headline CPI beside core PCE), fixed. 50/50 on both orchestrators.
- [x] **MCP server over HTTP** — `MCP_TRANSPORT=streamable-http` serves
      `/mcp`; rate-limited per MCP session and per client address (4x looser,
      but new sessions can't reset it), verified with the MCP client over real
      HTTP. `mcp.Dockerfile` + an optional deploy step (one instance, session
      affinity). Cloud Run service pending a fresh `gcloud` login.
- [ ] A short screen recording in the README
