# First live Gemini eval

[`evals/REPORT.gemini.md`](../evals/REPORT.gemini.md) is the first run of the
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

## What the run fixed (already in the code)

The first attempt never finished. It surfaced two bugs, both fixed and
covered by tests:

- **No request timeout.** One question sat on a silent connection to Google
  for 50 minutes. Both Gemini clients now share `GEMINI_TIMEOUT_S` (120 s
  without progress).
- **Dropped connections weren't retried.** `httpx.ReadError` has no HTTP
  status, so the SDK's retries never saw it and the question failed. Both
  orchestrators now retry it with 1-2-4 s backoff, unless answer text has
  already reached the user.

## What to change next

1. **Prompt the supervisor to skip Research/Risk on plain fetches** (the
   orchestration gap), and the data agent to stop searching once it has an
   ID (the GDP loop).
2. **Split tool selection into two metrics:** "the required calls happened,
   in order" (graded) and "extra calls" (reported), so a defensible metadata
   lookup and a 10-call loop stop scoring the same.
3. **Rerun on a fresh day's quota** and compare with this baseline.
