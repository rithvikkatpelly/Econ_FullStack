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
from dataclasses import dataclass, field
from typing import Any

import cost_tracker

# Default Claude model for live runs. Opus 5 per the project's API guidance.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "8000"))

# Default Gemini model for live runs (latest stable Flash). Override with
# GEMINI_MODEL, e.g. a Pro model for the supervisor-heavy workloads.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

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

        self._client = client or genai.Client()
        self._model = model
        # Gemini 3 attaches `thought_signature`s to function-call parts that
        # must be sent back verbatim on the next turn. The loop rebuilds
        # history in Anthropic block shape, which can't carry them, so keep
        # each raw model turn here keyed by its first tool-call id and replay
        # it exactly when that turn reappears in `messages`.
        self._raw_turns: dict[str, Any] = {}
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

    # --- the turn ------------------------------------------------------
    def turn(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=self._contents(messages),
            config=self._config(system, tools),
        )
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
            text="\n".join(text_parts).strip(),
            tool_requests=tool_reqs,
            stop_reason=stop,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )


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
