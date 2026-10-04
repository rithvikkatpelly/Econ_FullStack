"""
The supervisor + specialists pipeline, built on Google's Agent Development Kit.

Same agents, same prompts, same tools and guardrails as the native
orchestrator in `agents/` — only the orchestration layer is ADK's:

    supervisor (LlmAgent)
      ├─ AgentTool(economic_data_agent)  ── FunctionTools → tools.call_tool → FRED
      ├─ AgentTool(research_agent)        ── FunctionTool  → get_series_metadata
      ├─ AgentTool(risk_agent)             (no tools)
      └─ AgentTool(report_agent, skip_summarization=True)   ← its answer ends the run

What stays ours, deliberately:

  * Tools. Each FunctionTool is a thin typed wrapper over `tools.call_tool`,
    so ADK calls get the same input validation, cost guardrail, structured
    errors and audit log as the MCP server and the native agents.
  * The `Trace`. ADK callbacks record every delegation, tool call, specialist
    output and token count into the same `agents.trace.Trace` the evals grade
    and the HTTP stream reads — so evals, `/agent/stream` and the UI work on
    either orchestrator unchanged.
  * The model when offline. `StubLlm` adapts the deterministic stub planner
    to ADK's `BaseLlm`, so CI and the evals run this ADK pipeline with no key.
    Live, the model is ADK's own `Gemini` class (`ResilientGemini`, which adds
    the quota waits and model fallback found on the first live runs).

`run(query)` is the entry point (returns a `Trace`, like
`agents.supervisor.run`). `econ_adk/agent.py` exposes `root_agent` for the
`adk web` / `adk run` developer tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.models import BaseLlm, Gemini, LlmRequest, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import PrivateAttr

import cost_tracker
import tools
from agents import conversation, specialists, stub
from agents.base import with_today
from agents.model import (
    _EFFORT_BY_ROLE,
    _TRANSIENT_CODES,
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    GEMINI_QUOTA_RETRIES,
    GEMINI_RETRY_ATTEMPTS,
    ModelResponse,
    QuotaExhausted,
    _is_overloaded,
    _mark_overloaded,
    _quota_wait,
    _retry_delay,
    own_signatures,
    resign_foreign_turns,
)
from agents.supervisor import SUPERVISOR_SYSTEM, _parse_risk_signal
from agents.trace import Trace

APP_NAME = "econ_adk"
SPECIALISTS = ("economic_data_agent", "research_agent", "risk_agent", "report_agent")

# Whole-run ceiling on model calls (ADK's own runaway guard; the native loop
# uses per-agent iteration caps instead). A normal run is ~10.
MAX_LLM_CALLS = int(os.environ.get("ADK_MAX_LLM_CALLS", "40"))


# --- tools: typed wrappers over the one shared implementation ------------------
# ADK builds each tool's schema from the signature and docstring, so these
# mirror tools.TOOL_SCHEMAS; the body always goes through tools.call_tool.


def search_series(search_text: str) -> dict:
    """Search for a FRED series ID from a plain-language description. Returns
    candidate series (id, title, units, frequency) only, never observations.
    Use this first when you have a concept ('unemployment rate') but not an ID.

    Args:
        search_text: e.g. 'core inflation', '10 year treasury yield'.
    """
    return tools.call_tool("search_series", {"search_text": search_text})


def get_series_observations(
    series_id: str, start_date: str, end_date: str, frequency: str = "m"
) -> dict:
    """Fetch observations for one known FRED series ID over a required date
    range (at most 25 years).

    Args:
        series_id: FRED series ID, e.g. 'UNRATE'.
        start_date: YYYY-MM-DD.
        end_date: YYYY-MM-DD.
        frequency: one of d, w, m, q, a (default m).
    """
    return tools.call_tool("get_series_observations", {
        "series_id": series_id, "start_date": start_date,
        "end_date": end_date, "frequency": frequency,
    })


def compare_series(
    series_ids: list[str], start_date: str, end_date: str, frequency: str = "m"
) -> dict:
    """Fetch 2-4 FRED series aligned on one date range, for questions about
    the relationship between them.

    Args:
        series_ids: 2 to 4 FRED series IDs.
        start_date: YYYY-MM-DD.
        end_date: YYYY-MM-DD.
        frequency: one of d, w, m, q, a (default m).
    """
    return tools.call_tool("compare_series", {
        "series_ids": series_ids, "start_date": start_date,
        "end_date": end_date, "frequency": frequency,
    })


def get_series_metadata(series_id: str) -> dict:
    """Units, frequency, last-updated date and source notes for a FRED
    series. The notes are external text, returned wrapped as untrusted data.

    Args:
        series_id: FRED series ID, e.g. 'CPIAUCSL'.
    """
    return tools.call_tool("get_series_metadata", {"series_id": series_id})


_DATA_TOOLS = [search_series, get_series_observations, compare_series, get_series_metadata]
_TOOLS_BY_AGENT = {
    "economic_data_agent": _DATA_TOOLS,
    "research_agent": [get_series_metadata],
    "risk_agent": [],
    "report_agent": [],
}
_SYSTEM_BY_AGENT = {
    "economic_data_agent": specialists.ECONOMIC_DATA_SYSTEM,
    "research_agent": specialists.RESEARCH_SYSTEM,
    "risk_agent": specialists.RISK_SYSTEM,
    "report_agent": specialists.REPORT_SYSTEM,
}


# --- models ---------------------------------------------------------------


class StubLlm(BaseLlm):
    """The deterministic offline planner (agents/stub.py) as an ADK model.

    Translates ADK's request (genai `Content`s) into the message shape the
    planner reads, and its decision back into an `LlmResponse`. Delegations
    are renamed both ways: the planner speaks `delegate_to_<agent>` with a
    `task`; ADK's AgentTool is named `<agent>` and takes a `request`.
    """

    role: str

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"stub-.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        messages = _to_messages(llm_request.contents)
        decision = stub.plan(self.role, messages, [])
        yield _to_llm_response(decision, messages)


def _to_messages(contents: list[types.Content]) -> list[dict]:
    """genai Contents → the Anthropic-shaped history the stub planner reads."""
    out: list[dict] = []
    for content in contents or []:
        parts = content.parts or []
        if content.role == "model":
            blocks = []
            for p in parts:
                if p.function_call:
                    name, args = p.function_call.name, dict(p.function_call.args or {})
                    if name in SPECIALISTS:
                        name, args = f"delegate_to_{name}", {"task": args.get("request", "")}
                    blocks.append({"type": "tool_use", "id": p.function_call.id,
                                   "name": name, "input": args})
                elif p.text:
                    blocks.append({"type": "text", "text": p.text})
            out.append({"role": "assistant", "content": blocks})
            continue
        responses = [p.function_response for p in parts if p.function_response]
        if responses:
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r.id,
                 "content": json.dumps(_result_payload(r.name, r.response), default=str)}
                for r in responses
            ]})
        else:
            text = "".join(p.text or "" for p in parts)
            out.append({"role": "user", "content": text})
    return out


def _result_payload(name: str, response: Any) -> Any:
    """AgentTool results come back as {"result": text}; the planner expects
    the native supervisor's {"agent", "output"} shape."""
    if name in SPECIALISTS:
        text = response.get("result", "") if isinstance(response, dict) else str(response)
        return {"agent": name, "output": text}
    return response


def _to_llm_response(decision: ModelResponse, messages: list[dict]) -> LlmResponse:
    parts: list[types.Part] = []
    if decision.text:
        parts.append(types.Part(text=decision.text))
    for req in decision.tool_requests:
        name, args = req.name, dict(req.input)
        if name.startswith("delegate_to_"):
            name, args = name.removeprefix("delegate_to_"), {"request": args.get("task", "")}
        parts.append(types.Part(function_call=types.FunctionCall(
            id=f"{req.id}-{uuid.uuid4().hex[:8]}", name=name, args=args,
        )))
    # Same rough ~4 chars/token accounting as the native StubModel, so the
    # trace's usage numbers are comparable offline.
    sent = "".join(str(m.get("content", "")) for m in messages)
    produced = decision.text + "".join(str(t.input) for t in decision.tool_requests)
    return LlmResponse(
        content=types.Content(role="model", parts=parts or [types.Part(text="")]),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=cost_tracker.estimate_tokens(sent),
            candidates_token_count=cost_tracker.estimate_tokens(produced),
        ),
    )


# Module-level (not a pydantic attribute: a function stored as a model
# attribute default gets bound like a method). Tests replace it.
_sleep: Callable[[float], Any] = asyncio.sleep


class ResilientGemini(Gemini):
    """ADK's own Gemini model plus what the first live runs showed it needs:

    * SDK retries on transient 408/5xx (via ADK's `retry_options`);
    * a 429 waits as long as Google's RetryInfo says (bounded) and says so;
      a per-day quota fails fast;
    * an overloaded or out-of-quota model fails over down
      GEMINI_FALLBACK_MODELS, behind the shared circuit breaker — mid-loop
      included: turns another model signed are re-signed with Google's
      skip-validator value, which the new model accepts (a foreign signature
      is otherwise rejected as "corrupted" — verified live);
    * the model choice is sticky for this agent's run, so a breaker expiring
      mid-run can't bounce it back to a model whose turns are now foreign;
    * a final failure is reported through `_on_failure` before it's raised,
      because ADK turns a nested agent's exception into AgentTool *text*.
    """

    _notify: Callable[[dict], None] | None = PrivateAttr(default=None)
    # Set for the Report Agent: stream internally and forward answer text as
    # it arrives. ADK's AgentTool runs nested agents unary, so this is the
    # one place the words can be caught as Gemini writes them.
    _on_text: Callable[[str], None] | None = PrivateAttr(default=None)
    _on_failure: Callable[[Exception], None] | None = PrivateAttr(default=None)
    _current: str | None = PrivateAttr(default=None)
    _own_sigs: set = PrivateAttr(default_factory=set)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        try:
            async for response in self._generate(llm_request, stream):
                yield response
        except Exception as exc:
            if self._on_failure is not None:
                self._on_failure(exc)
            raise

    async def _generate(
        self, llm_request: LlmRequest, stream: bool
    ) -> AsyncGenerator[LlmResponse, None]:
        chain = [self.model] + [m for m in GEMINI_FALLBACK_MODELS if m != self.model]
        if self._current is None:
            self._current = next((m for m in chain if not _is_overloaded(m)), chain[-1])
        original = list(llm_request.contents or [])
        emitted = [False]  # text already shown: no retry, no fallback
        while True:
            llm_request.model = self._current
            llm_request.contents = resign_foreign_turns(original, self._own_sigs)
            try:
                async for response in self._with_quota_retry(llm_request, stream, emitted):
                    if response.content is not None and not response.partial:
                        self._own_sigs |= own_signatures(response.content)
                    yield response
                return
            except (genai_errors.ServerError, QuotaExhausted) as exc:
                if isinstance(exc, genai_errors.ServerError) and exc.code != 503:
                    raise
                nxt = chain.index(self._current) + 1
                if emitted[0] or nxt >= len(chain):
                    raise
                quota = isinstance(exc, QuotaExhausted)
                _mark_overloaded(self._current, exc.retry_after if quota else None)
                self._emit({"type": "fallback",
                            "reason": "quota_exhausted" if quota else "overloaded",
                            "from_model": self._current, "to_model": chain[nxt]})
                self._current = chain[nxt]
                self._own_sigs = set()  # everything so far is now foreign

    async def _with_quota_retry(
        self, llm_request: LlmRequest, stream: bool, emitted: list[bool]
    ) -> AsyncGenerator[LlmResponse, None]:
        internal_stream = self._on_text is not None
        for attempt in range(GEMINI_QUOTA_RETRIES + 1):
            try:
                async for response in super().generate_content_async(
                    llm_request, stream or internal_stream
                ):
                    if internal_stream and not stream and response.partial:
                        for part in (response.content.parts if response.content else None) or []:
                            if part.text and not part.thought:
                                emitted[0] = True
                                self._on_text(part.text)
                        continue  # ADK gets only the final, assembled response
                    yield response
                return
            except genai_errors.ClientError as exc:
                if exc.code != 429:
                    raise
                wait = _quota_wait(exc)
                if wait is None or attempt == GEMINI_QUOTA_RETRIES or emitted[0]:
                    raise QuotaExhausted(
                        "The Gemini API quota for this key is used up for now.",
                        retry_after=_retry_delay(exc),
                    ) from exc
                self._emit({"type": "waiting", "reason": "rate_limited", "seconds": round(wait)})
                await _sleep(wait)

    def _emit(self, event: dict) -> None:
        if self._notify is not None:
            self._notify(event)


def _model_for(role: str, trace: Trace) -> BaseLlm:
    backend = os.environ.get("AGENT_BACKEND", "stub").strip().lower()
    if backend == "anthropic":
        raise ValueError(
            "The ADK pipeline runs on Gemini or the offline stub; "
            "use AGENT_FRAMEWORK=native for AGENT_BACKEND=anthropic."
        )
    if backend != "gemini":
        return StubLlm(model=f"stub-{role}", role=role)
    model = ResilientGemini(
        model=os.environ.get("GEMINI_MODEL", GEMINI_MODEL),
        retry_options=types.HttpRetryOptions(
            attempts=GEMINI_RETRY_ATTEMPTS, initial_delay=1.0, max_delay=20.0,
            http_status_codes=_TRANSIENT_CODES,
        ),
    )
    model._notify = lambda event: trace.emit({**event, "agent": role})

    def failed(exc: Exception) -> None:
        trace.model_failure = trace.model_failure or exc

    model._on_failure = failed
    if role == "report_agent" and trace.listener is not None:
        def stream_text(text: str) -> None:
            trace.streamed_report = True
            trace.emit({"type": "report_delta", "agent": role, "text": text})

        model._on_text = stream_text
    return model


def _content_config(role: str) -> types.GenerateContentConfig | None:
    if os.environ.get("AGENT_BACKEND", "stub").strip().lower() != "gemini":
        return None
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=_EFFORT_BY_ROLE.get(role, "medium"))
    )


# --- callbacks: ADK → our Trace -------------------------------------------------


class _TraceCallbacks:
    """Record what ADK does into the run's `Trace`, mirroring exactly what the
    native supervisor records — so the evals grade both identically."""

    def __init__(self, trace: Trace):
        self.trace = trace
        self._started: dict[str, float] = {}

    def before_tool(self, tool, args: dict, tool_context) -> None:
        self._started[tool_context.function_call_id or tool.name] = time.monotonic()
        if tool.name in SPECIALISTS:
            self.trace.record_delegation(tool.name, args.get("request", ""))
        return None

    def after_tool(self, tool, args: dict, tool_context, tool_response) -> None:
        if tool.name in SPECIALISTS and self.trace.model_failure is not None:
            # The specialist's model failed; AgentTool has turned that into a
            # text result. Don't let the supervisor read an error as findings
            # (or as the report): end the run with the real exception.
            raise self.trace.model_failure
        started = self._started.pop(tool_context.function_call_id or tool.name, time.monotonic())
        latency_ms = (time.monotonic() - started) * 1000.0
        if tool.name in SPECIALISTS:
            output = tool_response if isinstance(tool_response, str) else str(
                (tool_response or {}).get("result", "")
            )
            if tool.name == "report_agent" and not self.trace.streamed_report:
                # Not streamed (the stub, or no listener): one delta, as the
                # native path does for non-streaming backends.
                self.trace.emit({"type": "report_delta", "agent": tool.name, "text": output})
            self.trace.record_agent_output(tool.name, output)
            if tool.name == "risk_agent":
                self.trace.risk_signal = _parse_risk_signal(output)
            if tool.name == "report_agent":
                self.trace.final_report = output
            self.trace.record_tool_call(
                "supervisor", f"delegate_to_{tool.name}", {"task": args.get("request", "")},
                {"agent": tool.name, "output": output}, latency_ms,
            )
        else:
            result = tool_response if isinstance(tool_response, dict) else {"result": tool_response}
            self.trace.record_tool_call(
                tool_context.agent_name, tool.name, dict(args), result, latency_ms
            )
        return None

    def after_model(self, callback_context, llm_response: LlmResponse) -> None:
        usage = llm_response.usage_metadata
        if usage is not None:
            self.trace.add_usage(
                usage.prompt_token_count or 0,
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            )
        return None


# --- building and running -------------------------------------------------


def build_supervisor(trace: Trace) -> LlmAgent:
    """A fresh agent tree wired to `trace` (callbacks close over it)."""
    cb = _TraceCallbacks(trace)

    def agent(name: str, system: str, agent_tools: list) -> LlmAgent:
        return LlmAgent(
            name=name,
            model=_model_for(name, trace),
            instruction=with_today(system),
            tools=agent_tools,
            generate_content_config=_content_config(name),
            before_tool_callback=cb.before_tool,
            after_tool_callback=cb.after_tool,
            after_model_callback=cb.after_model,
        )

    specialist_tools = [
        AgentTool(
            agent(name, _SYSTEM_BY_AGENT[name],
                  [FunctionTool(f) for f in _TOOLS_BY_AGENT[name]]),
            # The report is the answer: return it as-is and end the run,
            # rather than spend a supervisor call restating it.
            skip_summarization=(name == "report_agent"),
        )
        for name in SPECIALISTS
    ]
    return agent("supervisor", SUPERVISOR_SYSTEM, specialist_tools)


async def run_async(
    query: str, trace: Trace | None = None, history: list[dict] | None = None
) -> Trace:
    trace = trace or Trace()
    trace.query = query
    runner = Runner(
        app_name=APP_NAME,
        agent=build_supervisor(trace),
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    message = types.Content(
        role="user", parts=[types.Part(text=conversation.compose(query, history))]
    )
    final_text = ""
    try:
        async for event in runner.run_async(
            user_id="econ-user",
            session_id=uuid.uuid4().hex,
            new_message=message,
            run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS),
        ):
            if event.error_message:
                if trace.model_failure is not None:
                    raise trace.model_failure
                raise RuntimeError(f"ADK run failed: {event.error_code}")
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(p.text or "" for p in event.content.parts if not p.thought)
    finally:
        await runner.close()
    if trace.model_failure is not None:
        raise trace.model_failure
    if not trace.final_report:
        trace.final_report = final_text
    return trace


def run(query: str, trace: Trace | None = None, history: list[dict] | None = None) -> Trace:
    """Synchronous entry point, same contract as `agents.supervisor.run`.

    Runs the ADK loop with `asyncio.run` on the calling thread (not ADK's
    sync `Runner.run`, which uses a background thread): context variables
    such as the per-run token budget then reach every tool call."""
    return asyncio.run(run_async(query, trace, history))
