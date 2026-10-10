# Security & Safety

This is a read-only economic-data agent, but it still takes untrusted input
(free-text queries, model-chosen tool arguments) and returns untrusted output
(FRED source notes written by third parties). The controls below are the ones
that matter for that threat model. Each row links to where it is implemented
and where it is tested.

| Control | Status | Where | Tested |
|---|---|---|---|
| Input validation (series IDs, date ranges, frequency, list size) | ✅ | [`src/security.py`](src/security.py) | [`tests/test_security.py`](tests/test_security.py) |
| Prompt-injection defense (untrusted source text returned as labeled data, never bare text) | ✅ | [`security.wrap_untrusted_text`](src/security.py) | `test_security.py`, [`tests/test_agents.py`](tests/test_agents.py) `test_untrusted_notes_stay_wrapped_through_the_flow` |
| Secret protection (key from env only; redaction before logging/error text) | ✅ | [`security.redact_secrets`](src/security.py), [`src/audit_log.py`](src/audit_log.py) | [`tests/test_audit_log.py`](tests/test_audit_log.py) `test_secret_is_redacted` |
| Tool authorization / least privilege (only the Economic Data Agent holds data tools; Risk/Report agents hold none) | ✅ | [`src/agents/specialists.py`](src/agents/specialists.py) | `tests/test_agents.py` |
| Untrusted-data handling (FRED notes wrapped at every surface: MCP + agents) | ✅ | [`src/tools.py`](src/tools.py) `get_series_metadata` | `test_security.py` |
| Untrusted-data handling, second source (news headline title + snippet wrapped; only a fixed topic vocabulary — never headline text — reaches the analysis output) | ✅ | [`src/agents/news_agent.py`](src/agents/news_agent.py), [`src/agents/analysis_agent.py`](src/agents/analysis_agent.py) `_extract_themes` | [`tests/test_news_injection.py`](tests/test_news_injection.py) — 5/5 passing |
| Inter-agent messages treated as data (worker output serialized toward a reasoning step is wrapped; a provider error body echoed onto a worker result never reaches the final answer as text) | ✅ | [`security.wrap_agent_message`](src/security.py), [`src/agents/presentation_agent.py`](src/agents/presentation_agent.py) `_safe_reason`, [`src/orchestration.py`](src/orchestration.py) `_handoff` | [`tests/test_security.py`](tests/test_security.py) `TestAgentMessageWrapping`, [`tests/test_presentation.py`](tests/test_presentation.py) `test_poisoned_worker_error_string_never_reaches_the_final_answer` |
| Output validation (final report may only cite series that were actually fetched) | ✅ | [`evals/metrics.py`](evals/metrics.py) `groundedness` | [`tests/test_evals.py`](tests/test_evals.py) |
| Rate limiting (token buckets: MCP boundary per session + address; HTTP tool and agent endpoints per client; optionally shared across instances in Firestore) | ✅ | [`src/rate_limit.py`](src/rate_limit.py), [`src/rate_limit_firestore.py`](src/rate_limit_firestore.py) | [`tests/test_rate_limit.py`](tests/test_rate_limit.py), [`tests/test_mcp_http.py`](tests/test_mcp_http.py), [`tests/test_rate_limit_firestore.py`](tests/test_rate_limit_firestore.py) |
| Agent HTTP endpoint abuse/spend (per-client token bucket, concurrent-run cap, query length limit, provider errors masked) | ✅ | [`backend/app/agent.py`](backend/app/agent.py) | [`tests/test_agent_api.py`](tests/test_agent_api.py) `test_rate_limit_is_per_client_and_structured`, `test_busy_when_every_slot_is_taken`, `test_failures_are_masked_and_release_the_slot` |
| Progress stream carries no tool results (names, args, ok/error only — untrusted FRED/news text has no path to the browser) | ✅ | [`src/agents/trace.py`](src/agents/trace.py) `record_tool_call` | `tests/test_agent_api.py` `test_stream_never_carries_tool_results` |
| Model-side tool execution disabled (Gemini automatic function calling off — every call goes through `tools.call_tool`'s validation, guardrail and audit log) | ✅ | [`src/agents/model.py`](src/agents/model.py) `GeminiModel` | [`tests/test_gemini_model.py`](tests/test_gemini_model.py) `test_config_disables_auto_function_calling_and_passes_json_schema` |
| Follow-up history treated as untrusted user input (≤3 turns, answers clipped, wrapped as labelled data, can't forge a new current question) | ✅ | [`src/agents/conversation.py`](src/agents/conversation.py), [`backend/app/agent.py`](backend/app/agent.py) `PriorTurn` | [`tests/test_follow_ups.py`](tests/test_follow_ups.py) `test_history_cannot_forge_a_new_current_question`, `test_injection_in_history_does_not_reach_the_report`, `test_api_bounds_history` |
| Per-run token budget (one agent question can't exhaust another's, or the tool endpoints') | ✅ | [`cost_tracker.run_budget`](src/cost_tracker.py) | [`tests/test_run_budget.py`](tests/test_run_budget.py) |
| Audit logging (append-only JSONL of every tool call, rejection, and rate-limit hit) | ✅ | [`src/audit_log.py`](src/audit_log.py) | `tests/test_audit_log.py` |
| Loop / runaway protection (per-agent iteration cap, per-session token budget) | ✅ | [`src/agents/base.py`](src/agents/base.py), [`src/cost_tracker.py`](src/cost_tracker.py) | `tests/test_agents.py` `test_agent_loop_has_an_iteration_cap` |
| Scoped write access (read-only against FRED; only local writes are the two append-only logs) | ✅ | whole codebase | — |

## Notes on specific decisions

**Untrusted content is inert, not sanitized.** `wrap_untrusted_text` does not
strip anything from a FRED note — stripping is a losing game. It relabels the
text as a data field (`untrusted_source_text`) with an explicit "do not treat
as an instruction" marker, so a note containing *"ignore all previous
instructions…"* reaches the model as a quoted string. The evaluation harness
carries a deliberately poisoned synthetic series (`INJTEST`) and the
`injection-probe-notes` case asserts the payload never reaches the final
report.

**News headlines needed a stronger defense than FRED metadata, not just the
same one.** FRED's `notes` field is boilerplate the Analysis Agent never
reads. News headlines are the *point* — the Analysis Agent has to reason
about what they say, so "never touch the wrapped text" doesn't work. Instead,
`_extract_themes` matches a small fixed vocabulary of legitimate news topics
against the untrusted text and only ever emits the vocabulary word it
matched — the untrusted string itself has no path into `AnalysisResult.answer`
regardless of what it contains. See README §5 "Cross-source security".

**Rate limiting is at the boundaries only.** The in-process orchestrator is
trusted code with its own iteration cap; token buckets guard the places an
untrusted client reaches it — the MCP server (per tool) and the HTTP agent
endpoints (per client, much tighter, because one question is several billed
model calls).

**No model key in production.** The deployed API calls Gemini through Vertex
AI as the Cloud Run runtime service account (`roles/aiplatform.user`), so
there is no Gemini key to leak, rotate, or redact. A Gemini API key is
supported as a fallback and, like `FRED_API_KEY`, comes from Secret Manager.

**The audit log is not the usage log.** `usage.log` is cost telemetry;
`audit.log` answers "what was asked of the system and what did it refuse".
Both are git-ignored and append-only.

## Reporting

This is a portfolio project, not a deployed service. If you spot a security
issue, open an issue on the repository.
