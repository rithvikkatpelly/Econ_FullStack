"""
Model abstraction for the agent loop.

`Agent` (agents/base.py) only ever talks to a `Model`. Three implementations:

  * `AnthropicModel` — a real Claude tool-use turn via the Anthropic SDK.
  * `GeminiModel` — a real Gemini function-calling turn via the Google Gen AI
    SDK (`google-genai`), against either the Gemini API or Vertex AI.
  * `StubModel` — a deterministic planner (agents/stub.py) so the evaluation
    harness, CI, and the demo run with no API key and no network.

`make_model(role)` picks one from `AGENT_BACKEND` (`stub` | `anthropic` |
`gemini`); anything else falls back to the stub.

The loop's message history is always Anthropic-shaped (`tool_use` /
`tool_result` content blocks — see agents/base.py). `GeminiModel` translates
that history into Gemini `Content` on every turn, so the loop, the trace, and
the evals are provider-agnostic.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import cost_tracker

# Default Claude model for live runs. Opus 5 per the project's API guidance.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "8000"))

# Default Gemini model for live runs (latest stable Flash). Override with
# GEMINI_MODEL, e.g. a Pro model for the supervisor-heavy workloads.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# The google-genai SDK does not retry unless asked. One agent run is 5-10
# model calls and Gemini returns transient 503 "high demand" errors under
# load, so every call gets exponential backoff with jitter on 408/5xx.
# Total attempts, including the first.
GEMINI_RETRY_ATTEMPTS = int(os.environ.get("GEMINI_RETRY_ATTEMPTS", "4"))
_TRANSIENT_CODES = [408, 500, 502, 503, 504]

# 429 is handled separately (GeminiModel._with_quota_retry): Google says how
# long to wait (RetryInfo), often ~60 s on a per-minute quota — the free tier
# allows 5 requests/min/model, less than one agent run — and a blind 1-20 s
# backoff just burns attempts. We wait what we're told, up to this cap, a
# bounded number of times; a per-day quota fails fast since waiting can't help.
GEMINI_MAX_QUOTA_WAIT_S = float(os.environ.get("GEMINI_MAX_QUOTA_WAIT_S", "75"))
GEMINI_QUOTA_RETRIES = int(os.environ.get("GEMINI_QUOTA_RETRIES", "3"))


# When the primary model stays overloaded (503 "high demand" after the SDK's
# retries), fall back down this list. The newest models are the likeliest to
# be overloaded; one or two releases back usually has capacity.
_DEFAULT_FALLBACKS = "gemini-3.6-flash,gemini-3.5-flash"
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get("GEMINI_FALLBACK_MODELS", _DEFAULT_FALLBACKS).split(",")
    if m.strip()
]
# Circuit breaker: once a model has failed over, skip it for this long so the
# rest of the run (and other runs in this process) start on the fallback
# instead of each sitting through the retries again.
GEMINI_OVERLOAD_COOLDOWN_S = float(os.environ.get("GEMINI_OVERLOAD_COOLDOWN_S", "120"))
_overloaded_until: dict[str, float] = {}
_overloaded_lock = threading.Lock()


def reset_overload_state() -> None:
    """Forget every tripped circuit breaker (tests, or after a config change)."""
    with _overloaded_lock:
        _overloaded_until.clear()


def _mark_overloaded(model: str, seconds: float | None = None) -> None:
    with _overloaded_lock:
        _overloaded_until[model] = time.monotonic() + max(
            GEMINI_OVERLOAD_COOLDOWN_S, seconds or 0.0
        )


def _is_overloaded(model: str) -> bool:
    with _overloaded_lock:
        return _overloaded_until.get(model, 0.0) > time.monotonic()


# Gemini 3 rejects a thought signature another model produced ("Corrupted
# thought signature") and a function call with none ("missing a
# thought_signature") — both verified live. This documented value tells it to
# skip validation for turns it didn't produce, which is what lets a run fail
# over to another model mid-loop.
SKIP_THOUGHT_SIGNATURE = b"skip_thought_signature_validator"


def own_signatures(content) -> set[bytes]:
    """Every thought signature in a model turn (to recognise it later as ours)."""
    return {p.thought_signature for p in (content.parts or []) if p.thought_signature}


def resign_foreign_turns(contents: list, own: set[bytes]) -> list:
    """Copy of `contents` where each model turn this model didn't produce
    (it carries a signature not in `own`) has its signatures replaced by
    SKIP_THOUGHT_SIGNATURE, and its function calls signed with it. Turns this
    model produced are left exactly as they were — including parallel calls,
    of which only the first is signed."""
    out = []
    for content in contents:
        parts = content.parts or []
        foreign = content.role == "model" and any(
            p.thought_signature and p.thought_signature not in own for p in parts
        )
        if not foreign:
            out.append(content)
            continue
        out.append(content.model_copy(update={"parts": [
            p.model_copy(update={"thought_signature": SKIP_THOUGHT_SIGNATURE})
            if (p.function_call is not None or p.thought_signature) else p
            for p in parts
        ]}))
    return out


class QuotaExhausted(RuntimeError):
    """The model provider's quota is used up and waiting won't (quickly) fix
    it. Safe to show: carries no request details. `retry_after` is Google's
    own estimate, when it gave one (hours, for a per-day quota)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after

# Effort per role: the leaf specialists do bounded, well-specified work and run
# at low effort to keep cost down; the supervisor and report writer get more
# room. (output_config.effort, GA — see the claude-api guidance.)
_EFFORT_BY_ROLE = {
    "supervisor": "medium",
    "report_agent": "medium",
    "economic_data_agent": "low",
    "research_agent": "low",
    "risk_agent": "low",
}


@dataclass
class ToolRequest:
    id: str
    name: str
    input: dict


@dataclass
class ModelResponse:
    text: str = ""
    tool_requests: list[ToolRequest] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_requests)


class Model:
    """Interface. `role` identifies which agent this model is driving."""

    def __init__(self, role: str):
        self.role = role

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        raise NotImplementedError

    def stream_turn(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_text: Callable[[str], None],
    ) -> ModelResponse:
        """Like `turn`, but report answer text through `on_text` as it is
        produced. Backends without native streaming deliver it in one piece,
        so callers never need to know which kind they have."""
        resp = self.turn(system, messages, tools)
        if resp.text and not resp.wants_tools:
            on_text(resp.text)
        return resp


class AnthropicModel(Model):
    def __init__(self, role: str, client=None, model: str = ANTHROPIC_MODEL):
        super().__init__(role)
        import anthropic  # local import so the stub path needs no dependency

        self._client = client or anthropic.Anthropic()
        self._model = model

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=tools or [],
            thinking={"type": "adaptive"},
            output_config={"effort": _EFFORT_BY_ROLE.get(self.role, "medium")},
        )
        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            return ModelResponse(
                text=f"[model refused: {getattr(detail, 'category', 'unspecified')}]",
                stop_reason="refusal",
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
        text_parts, tool_reqs = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_reqs.append(ToolRequest(id=block.id, name=block.name, input=dict(block.input)))
        return ModelResponse(
            text="\n".join(text_parts).strip(),
            tool_requests=tool_reqs,
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


# Gemini finish reasons that mean "the model declined", mapped onto the same
# `stop_reason == "refusal"` the Anthropic path produces.
_GEMINI_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


class GeminiModel(Model):
    """One Gemini function-calling turn.

    Auth comes from the SDK's own environment handling: `GEMINI_API_KEY` (or
    `GOOGLE_API_KEY`) for the Gemini API, or `GOOGLE_GENAI_USE_VERTEXAI=true`
    plus `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` for Vertex AI — on
    Cloud Run the latter uses the service account, so no key is stored.

    Automatic function calling is disabled: the SDK must hand tool calls back
    to `Agent.run` so they go through `tools.call_tool` (validation, cost
    guardrail, audit log) and land in the trace like any other backend's.
    """

    def __init__(self, role: str, client=None, model: str = GEMINI_MODEL):
        super().__init__(role)
        from google import genai  # local import so the stub path needs no dependency
        from google.genai import types

        # Primary first, then the fallbacks; start on the first one whose
        # circuit breaker isn't tripped.
        self._chain = [model] + [m for m in GEMINI_FALLBACK_MODELS if m != model]
        model = next((m for m in self._chain if not _is_overloaded(m)), self._chain[-1])
        self._client = client or genai.Client(
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=GEMINI_RETRY_ATTEMPTS,
                    initial_delay=1.0,
                    max_delay=20.0,
                    http_status_codes=_TRANSIENT_CODES,
                )
            )
        )
        # Set by Agent so a quota wait shows up in the trace / UI timeline.
        self.notify: Callable[[dict], None] | None = None
        self._sleep = time.sleep
        self._model = model
        # Gemini 3 attaches `thought_signature`s to function-call parts that
        # must be sent back verbatim on the next turn. The loop rebuilds
        # history in Anthropic block shape, which can't carry them, so keep
        # each raw model turn here keyed by its first tool-call id and replay
        # it exactly when that turn reappears in `messages`.
        self._raw_turns: dict[str, Any] = {}
        # Signatures the *current* model produced; anything else in the
        # history is re-signed before sending (see resign_foreign_turns).
        self._own_sigs: set[bytes] = set()
        self._call_seq = 0

    # --- request translation ---------------------------------------------
    def _config(self, system: str, tools: list[dict]):
        from google.genai import types

        config: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": MAX_TOKENS,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            "thinking_config": types.ThinkingConfig(
                thinking_level=_EFFORT_BY_ROLE.get(self.role, "medium")
            ),
        }
        if tools:
            config["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t.get("description", ""),
                            parameters_json_schema=t["input_schema"],
                        )
                        for t in tools
                    ]
                )
            ]
        return types.GenerateContentConfig(**config)

    def _contents(self, messages: list[dict]) -> list:
        from google.genai import types

        names_by_id: dict[str, str] = {}
        contents = []
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                role = "user" if m["role"] == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part(text=content)]))
                continue

            if m["role"] == "assistant":
                calls = [b for b in content if b.get("type") == "tool_use"]
                for b in calls:
                    names_by_id[b["id"]] = b["name"]
                raw = self._raw_turns.get(calls[0]["id"]) if calls else None
                if raw is not None:
                    contents.append(raw)
                    continue
                parts = []
                for b in content:
                    if b.get("type") == "text" and b.get("text"):
                        parts.append(types.Part(text=b["text"]))
                    elif b.get("type") == "tool_use":
                        parts.append(
                            types.Part(
                                function_call=types.FunctionCall(
                                    id=_api_id(b["id"]), name=b["name"], args=b["input"]
                                )
                            )
                        )
                contents.append(types.Content(role="model", parts=parts or [types.Part(text="")]))
                continue

            parts = []
            for b in content:
                if b.get("type") != "tool_result":
                    continue
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=_api_id(b["tool_use_id"]),
                            name=names_by_id.get(b["tool_use_id"], "unknown_tool"),
                            response=_as_response_dict(b.get("content", "")),
                        )
                    )
                )
            contents.append(types.Content(role="user", parts=parts))
        return contents

    # --- quota-aware retry ------------------------------------------------
    def _with_quota_retry(self, call: Callable[[], Any], can_retry: Callable[[], bool] = bool):
        """Run `call`; on a 429, wait what Google asks (RetryInfo) and retry.
        `can_retry()` is checked first — a stream that already showed the user
        text must not start over."""
        from google.genai import errors

        for attempt in range(GEMINI_QUOTA_RETRIES + 1):
            try:
                return call()
            except errors.ClientError as exc:
                if exc.code != 429:
                    raise
                wait = _quota_wait(exc)
                if wait is None or attempt == GEMINI_QUOTA_RETRIES or not can_retry():
                    raise QuotaExhausted(
                        "The Gemini API quota for this key is used up for now.",
                        retry_after=_retry_delay(exc),
                    ) from exc
                if self.notify is not None:
                    self.notify({"type": "waiting", "reason": "rate_limited",
                                 "seconds": round(wait)})
                self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    # --- model fallback on overload ------------------------------------------
    def _with_fallback(self, call: Callable[[], Any], can_retry: Callable[[], bool] = bool):
        """Run `call`; if the current model is unusable — overloaded (503
        after the SDK's retries) or out of quota (quotas are per model) —
        trip its circuit breaker and retry on the next model in the chain.
        Works mid-loop too: the next attempt re-signs the earlier model's
        turns (`_signed`), which the new model then accepts."""
        from google.genai import errors

        while True:
            try:
                return call()
            except (errors.ServerError, QuotaExhausted) as exc:
                if isinstance(exc, errors.ServerError) and exc.code != 503:
                    raise
                nxt = self._chain.index(self._model) + 1 if self._model in self._chain else None
                if not can_retry() or nxt is None or nxt >= len(self._chain):
                    raise
                quota = isinstance(exc, QuotaExhausted)
                _mark_overloaded(self._model, exc.retry_after if quota else None)
                if self.notify is not None:
                    self.notify({"type": "fallback",
                                 "reason": "quota_exhausted" if quota else "overloaded",
                                 "from_model": self._model, "to_model": self._chain[nxt]})
                self._model = self._chain[nxt]
                self._own_sigs = set()  # everything so far is now foreign

    def _signed(self, contents: list) -> list:
        """The history as the model about to be called will accept it."""
        return resign_foreign_turns(contents, self._own_sigs)

    # --- the turn ------------------------------------------------------
    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        contents, config = self._contents(messages), self._config(system, tools)
        resp = self._with_fallback(
            lambda: self._with_quota_retry(
                lambda: self._client.models.generate_content(
                    model=self._model, contents=self._signed(contents), config=config
                ),
                can_retry=lambda: True,
            ),
            can_retry=lambda: True,
        )
        return self._interpret(resp)

    def stream_turn(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_text: Callable[[str], None],
    ) -> ModelResponse:
        """`generate_content_stream`: forward answer text as each chunk lands,
        then reassemble the chunks into one response and interpret it exactly
        like a non-streamed turn. Every raw part is kept, in order, so a
        function call's thought signature survives for replay."""
        from google.genai import types

        contents, config = self._contents(messages), self._config(system, tools)
        parts: list = []
        state: dict[str, Any] = {"finish": None, "usage": None, "feedback": None}
        emitted = False

        def consume() -> None:
            nonlocal emitted
            parts.clear()
            for chunk in self._client.models.generate_content_stream(
                model=self._model, contents=self._signed(contents), config=config
            ):
                state["usage"] = getattr(chunk, "usage_metadata", None) or state["usage"]
                state["feedback"] = getattr(chunk, "prompt_feedback", None) or state["feedback"]
                for cand in (getattr(chunk, "candidates", None) or [])[:1]:
                    state["finish"] = getattr(cand, "finish_reason", None) or state["finish"]
                    chunk_parts = (cand.content.parts if cand.content else None) or []
                    for part in chunk_parts:
                        parts.append(part)
                        if part.text and not part.thought and part.function_call is None:
                            emitted = True
                            on_text(part.text)

        self._with_fallback(
            lambda: self._with_quota_retry(consume, can_retry=lambda: not emitted),
            can_retry=lambda: not emitted,
        )
        finish, usage, feedback = state["finish"], state["usage"], state["feedback"]

        candidates = []
        if parts or finish is not None:
            candidates = [
                types.Candidate(
                    content=types.Content(role="model", parts=parts),
                    finish_reason=finish,
                )
            ]
        return self._interpret(
            types.GenerateContentResponse(
                candidates=candidates, usage_metadata=usage, prompt_feedback=feedback
            )
        )

    def _interpret(self, resp) -> ModelResponse:
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", None) or 0
        out_tok = (getattr(usage, "candidates_token_count", None) or 0) + (
            getattr(usage, "thoughts_token_count", None) or 0
        )

        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            feedback = getattr(resp, "prompt_feedback", None)
            reason = getattr(getattr(feedback, "block_reason", None), "name", None)
            return ModelResponse(
                text=f"[model refused: {(reason or 'no candidates').lower()}]",
                stop_reason="refusal",
                input_tokens=in_tok,
                output_tokens=out_tok,
            )

        cand = candidates[0]
        if cand.content is not None:
            self._own_sigs |= own_signatures(cand.content)
        finish = getattr(getattr(cand, "finish_reason", None), "name", None) or "STOP"
        if finish in _GEMINI_REFUSALS:
            return ModelResponse(
                text=f"[model refused: {finish.lower()}]",
                stop_reason="refusal",
                input_tokens=in_tok,
                output_tokens=out_tok,
            )

        text_parts, tool_reqs = [], []
        for part in getattr(cand.content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue  # thought summaries are not answer text
            fc = getattr(part, "function_call", None)
            if fc is not None:
                call_id = fc.id
                if not call_id:
                    # The Gemini API often omits ids. The loop needs one to
                    # pair results with calls; it stays local (see _api_id).
                    self._call_seq += 1
                    call_id = f"{_LOCAL_ID_PREFIX}{self._call_seq}"
                tool_reqs.append(ToolRequest(id=call_id, name=fc.name, input=dict(fc.args or {})))
            elif getattr(part, "text", None):
                text_parts.append(part.text)

        stop = "max_tokens" if finish == "MAX_TOKENS" else "end_turn"
        if tool_reqs:
            self._raw_turns[tool_reqs[0].id] = cand.content
            stop = "tool_use"
        return ModelResponse(
            # Text parts are fragments of one string (streaming splits them
            # anywhere), so they concatenate as-is.
            text="".join(text_parts).strip(),
            tool_requests=tool_reqs,
            stop_reason=stop,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )


def _retry_delay(exc) -> float | None:
    """Google's RetryInfo delay on an error, in seconds, if present."""
    body = exc.details if isinstance(exc.details, dict) else {}
    for d in (body.get("error") or {}).get("details") or []:
        if str(d.get("@type", "")).endswith("RetryInfo"):
            try:
                return float(str(d.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                return None
    return None


def _quota_wait(exc) -> float | None:
    """Seconds to wait before retrying a 429, from Google's RetryInfo; None
    when waiting won't help (a per-day quota, no hint, or longer than the
    cap)."""
    body = exc.details if isinstance(exc.details, dict) else {}
    delay, daily = _retry_delay(exc), False
    for d in (body.get("error") or {}).get("details") or []:
        if str(d.get("@type", "")).endswith("QuotaFailure"):
            daily = any("PerDay" in str(v.get("quotaId", "")) for v in d.get("violations", []))
    if daily or delay is None or delay > GEMINI_MAX_QUOTA_WAIT_S:
        return None
    return delay + 1.0  # their estimate is to the second; don't arrive early


_LOCAL_ID_PREFIX = "gemini-call-"


def _api_id(call_id: str) -> str | None:
    """Only echo ids Gemini issued; a locally synthesized one is never sent."""
    return None if call_id.startswith(_LOCAL_ID_PREFIX) else call_id


def _as_response_dict(content: Any) -> dict:
    """Gemini wants a function response as a JSON object. Tool results arrive
    JSON-encoded (agents/base.py); objects pass through, anything else is
    wrapped under "result"."""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return {"result": content}
    return content if isinstance(content, dict) else {"result": content}


class StubModel(Model):
    """Deterministic planner. Delegates the actual decision to agents/stub.py
    so the domain logic lives in one readable place."""

    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        from agents import stub

        resp = stub.plan(self.role, messages, tools)
        # Rough token accounting so cost/usage numbers in the eval report are
        # populated even offline (see cost_tracker's ~4 chars/token heuristic).
        serialized = system + "".join(str(m.get("content", "")) for m in messages)
        resp.input_tokens = cost_tracker.estimate_tokens(serialized)
        resp.output_tokens = cost_tracker.estimate_tokens(
            resp.text + "".join(str(t.input) for t in resp.tool_requests)
        )
        return resp


def make_model(role: str) -> Model:
    backend = os.environ.get("AGENT_BACKEND", "stub").strip().lower()
    if backend == "anthropic":
        return AnthropicModel(role)
    if backend == "gemini":
        return GeminiModel(role)
    return StubModel(role)
