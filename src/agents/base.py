"""
The agent tool-use loop.

`Agent.run(task)` runs the standard loop — ask the model, execute any tool
calls it requests, feed the results back, repeat until it answers or the
iteration cap is hit. The supervisor and all four specialists are just an
`Agent` with a different system prompt, tool list, and dispatch function.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date

from agents.model import Model, ModelResponse
from agents.trace import Trace

# Hard cap on model turns per agent. A specialist that can't finish in this
# many rounds is a bug (or a prompt-injection loop) — stop rather than spin.
MAX_ITERATIONS = 6

Dispatch = Callable[[str, dict], dict]


class Agent:
    def __init__(
        self,
        name: str,
        system: str,
        tools: list[dict],
        dispatch: Dispatch,
        model: Model,
        trace: Trace,
        max_iterations: int = MAX_ITERATIONS,
        stop_when: Callable[[], bool] | None = None,
    ):
        self.name = name
        self.system = system
        self.tools = tools
        self.dispatch = dispatch
        self.model = model
        self.trace = trace
        self.max_iterations = max_iterations
        # Checked after each round of tool calls: True ends the run without
        # another model turn (the supervisor stops once the report exists).
        self.stop_when = stop_when
        # Set to receive answer text as the model produces it (the supervisor
        # does this for the Report Agent so the UI can stream the answer).
        self.on_text: Callable[[str], None] | None = None
        # A backend that may pause (e.g. waiting out a provider quota) says so
        # through `notify`; route it into the trace so the UI can show it.
        if hasattr(model, "notify"):
            model.notify = lambda event: trace.emit({**event, "agent": name})

    def run(self, task: str) -> str:
        messages: list[dict] = [{"role": "user", "content": task}]
        system = with_today(self.system)

        for _ in range(self.max_iterations):
            if self.on_text is None:
                resp = self.model.turn(system, messages, self.tools)
            else:
                resp = self.model.stream_turn(system, messages, self.tools, self.on_text)
            self.trace.add_usage(resp.input_tokens, resp.output_tokens)
            messages.append({"role": "assistant", "content": _assistant_content(resp)})

            if not resp.wants_tools:
                return resp.text

            results = []
            for req in resp.tool_requests:
                started = time.monotonic()
                result = self.dispatch(req.name, req.input)
                latency_ms = (time.monotonic() - started) * 1000.0
                self.trace.record_tool_call(
                    self.name, req.name, req.input, result, latency_ms
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": req.id,
                        "content": json.dumps(result, default=str),
                        "is_error": bool(isinstance(result, dict) and result.get("error")),
                    }
                )
            messages.append({"role": "user", "content": results})
            if self.stop_when is not None and self.stop_when():
                return ""

        return (
            "Stopped: hit the "
            f"{self.max_iterations}-iteration cap without a final answer."
        )


def with_today(system: str) -> str:
    """Append today's date to a system prompt. Found on the first live Gemini
    run: asked for "the last 5 years" in October 2026, the model fetched
    2019-2024 — it has no idea what today is unless told. Appended at the end
    so the static prompt stays a stable, cacheable prefix."""
    return f"{system.rstrip()}\n\nToday's date is {date.today().isoformat()}."


def _assistant_content(resp: ModelResponse) -> list[dict]:
    """Rebuild the assistant turn as content blocks so the next request (and
    the Anthropic API) sees a well-formed history."""
    blocks: list[dict] = []
    if resp.text:
        blocks.append({"type": "text", "text": resp.text})
    for req in resp.tool_requests:
        blocks.append(
            {"type": "tool_use", "id": req.id, "name": req.name, "input": req.input}
        )
    return blocks or [{"type": "text", "text": ""}]
