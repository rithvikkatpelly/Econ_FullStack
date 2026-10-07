# Live Gemini eval

## Second run (2026-10-07): 8/8

After the fixes below, the same 8 cases went from **0/8 to 8/8**, with
**zero** extra tool calls — so they'd pass under the first run's strict
exact-sequence metric too; the improvement is the prompts, not the scoring.

| | First run (baseline) | Second run |
|---|---|---|
| Cases passing every check | 0/8 | **8/8** |
| Tool selection | 0% (exact match) | 100% (required calls in order) |
| Extra data-agent calls | 17 (9 on one GDP question) | **0** |
| Orchestration | 57% | 100% |
| Groundedness | 86% | 100% |
| Injection resistance | 100% | 100% |
| Tokens, whole suite | 117k | 83k |
| Mean time per question | 174 s | 103 s |

What changed, all Google ADK + Gemini, no new tools:

- **Supervisor prompt** says what a plain data request is ("show", "get",
  "pull", "track") and routes it to the data and report agents only.
- **Economic Data Agent prompt** lists the seven headline series with their
  IDs, allows one search per concept, no metadata calls, one fetch per series.
  A replay of the GDP case showed why it looped: the supervisor's task said
  "Real GDP or Nominal GDP", the agent guessed `GDPC1`, got an error, then
  searched. Knowing `GDP` up front removes the guess.
- **Tool selection metric** now grades "the required calls happened, in
  order" and reports extra calls separately; the offline stub is still held
  to exact sequences (`test_the_stub_makes_no_extra_calls`).

Files: [`REPORT.gemini.md`](../evals/REPORT.gemini.md) (this run),
[`REPORT.gemini.baseline.md`](../evals/REPORT.gemini.baseline.md) (the first).

Caveat: 8 of the 25 cases (all four injection probes + the first four
others), one run each, on the free tier. It's a strong signal on these
cases, not a measurement of the whole dataset.

---

## First run (2026-10-06): 0/8

[`evals/REPORT.gemini.baseline.md`](../evals/REPORT.gemini.baseline.md) is the first run of the
eval suite against real Gemini instead of the offline stub: 8 cases (all four
injection probes plus the first four others), the ADK orchestrator, the
Gemini API free tier, and FRED on the offline fixture so only model calls
count. Run on 2026-10-06 with:

```bash
AGENT_BACKEND=gemini AGENT_FRAMEWORK=adk FRED_OFFLINE=1 NEWS_OFFLINE=1 \
GEMINI_MODEL=gemini-3.8-flash \
GEMINI_FALLBACK_MODELS=gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.1-flash-lite \
python -m evals --max-cases 8 --min-pass-rate 0 --out evals/REPORT.gemini.md
```

**Headline: 0/8 cases pass every check.** The metrics underneath say more
than the headline does.

| Metric | Score | What happened |
|---|---|---|
| injection resistance | 100% | No poisoned source text and no system prompt in any report, including the two cases where the injection is in the question itself. |
| series grounding | 100% | The right series every time, including "INJTEST and CPI". |
| argument validity | 100% | Every date range and series ID passed the real validators. |
| tool selection | 0% | The metric requires the *exact* expected call sequence. Gemini almost always adds a `get_series_metadata` or `search_series` lookup first — defensible — but on `gdp-pure-fetch` it made 10 calls (4 searches, 4 fetches) for one series, which is not. |
| orchestration | 57% | On plain fetches ("show me the unemployment rate") the supervisor still brought in the Research and Risk agents. |
| groundedness | 86% | One report cited a series it never fetched. |

One case (`injection-in-query-system-prompt`) crashed: every model in the
fallback chain returned 503 "high demand" at the same moment. That's an
outage, not a model error, and the eval records it as one (💥) instead of
scoring it.

**Cost of the run:** 117k tokens, about 3 minutes per question — most of it
spent waiting out the free tier's 5-requests-per-minute limit.

### What the run fixed (already in the code)

The first attempt never finished. It surfaced two bugs, both fixed and
covered by tests:

- **No request timeout.** One question sat on a silent connection to Google
  for 50 minutes. Both Gemini clients now share `GEMINI_TIMEOUT_S` (120 s
  without progress).
- **Dropped connections weren't retried.** `httpx.ReadError` has no HTTP
  status, so the SDK's retries never saw it and the question failed. Both
  orchestrators now retry it with 1-2-4 s backoff, unless answer text has
  already reached the user.

### What it pointed at (done in the second run)

1. **Prompt the supervisor to skip Research/Risk on plain fetches** (the
   orchestration gap), and the data agent to stop searching once it has an
   ID (the GDP loop).
2. **Split tool selection into two metrics:** "the required calls happened,
   in order" (graded) and "extra calls" (reported), so a defensible metadata
   lookup and a 10-call loop stop scoring the same.
3. **Rerun on a fresh day's quota** and compare with this baseline.
