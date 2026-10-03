"""
Execution trace for a single supervisor run.

One `Trace` is threaded through the supervisor and every specialist agent.
It is the single source of truth the evaluation harness reads: which tools
were called, in what order, with what arguments, whether they errored, and
how many tokens were spent.

It is also the progress feed: give it a `listener` and every delegation,
specialist output, and tool call is pushed to it as a small JSON-safe event
the moment it is recorded. The HTTP layer streams those to the browser as
server-sent events (backend/app/agent.py); nothing else in the agent code
knows streaming exists.
"""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Cap on any free-text field in a streamed event. Tool *results* are never
# streamed at all — only names, arguments, and ok/error — so a big payload or
# untrusted FRED/news text can't ride the event feed into the browser.
_EVENT_TEXT_LIMIT = 600


@dataclass
class ToolCall:
    agent: str
    name: str
    arguments: dict
    ok: bool
    error: str | None = None
    latency_ms: float = 0.0


@dataclass
class Delegation:
    to: str
    task: str


@dataclass
class Trace:
    query: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    delegations: list[Delegation] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)
    final_report: str = ""
    # Populated by the risk agent so evals can check it without parsing prose.
    risk_signal: str | None = None
    # Progress listener (see module docstring). Excluded from equality/repr.
    listener: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)

    # --- events --------------------------------------------------------
    def emit(self, event: dict) -> None:
        """Push one progress event to the listener. Best-effort: a broken
        listener (client gone, queue closed) must never break the run."""
        if self.listener is None:
            return
        event.setdefault("elapsed_ms", round(self.elapsed_ms, 1))
        with contextlib.suppress(Exception):
            self.listener(event)

    # --- recording -----------------------------------------------------
    def record_tool_call(
        self, agent: str, name: str, arguments: dict, result: dict, latency_ms: float
    ) -> None:
        err = result.get("error") if isinstance(result, dict) else None
        self.tool_calls.append(
            ToolCall(
                agent=agent,
                name=name,
                arguments=arguments,
                ok=err is None,
                error=err,
                latency_ms=latency_ms,
            )
        )
        if name.startswith("delegate_to_"):
            # Already streamed as `delegation` + `agent_output`; this record
            # only lands after the specialist has finished.
            return
        self.emit({
            "type": "tool_call",
            "agent": agent,
            "tool": name,
            "arguments": _clip(arguments),
            "ok": err is None,
            "error": err,
            "latency_ms": round(latency_ms, 1),
        })

    def record_delegation(self, to: str, task: str) -> None:
        self.delegations.append(Delegation(to=to, task=task))
        self.emit({"type": "delegation", "agent": to, "task": _clip(task)})

    def record_agent_output(self, agent: str, output: str) -> None:
        """A specialist finished. Only the event is recorded here — the
        supervisor already keeps what it needs (risk signal, final report)."""
        self.emit({"type": "agent_output", "agent": agent, "output": _clip(output)})

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    # --- views -------------------------------------------------------
    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000.0

    # The four real FRED tools — everything else in tool_calls is a
    # supervisor `delegate_to_*` call.
    LEAF_TOOLS = (
        "search_series", "get_series_observations", "compare_series", "get_series_metadata",
    )

    @property
    def tool_sequence(self) -> list[str]:
        return [c.name for c in self.tool_calls]

    @property
    def leaf_tool_sequence(self) -> list[str]:
        """FRED tool calls only, in execution order — what the evals grade."""
        return [c.name for c in self.tool_calls if c.name in self.LEAF_TOOLS]

    def leaf_calls(self, agent: str | None = None) -> list["ToolCall"]:
        calls = [c for c in self.tool_calls if c.name in self.LEAF_TOOLS]
        return [c for c in calls if agent is None or c.agent == agent]

    @property
    def series_used(self) -> list[str]:
        """Every FRED series ID that was actually fetched — the grounding set."""
        seen: list[str] = []
        for c in self.tool_calls:
            if not c.ok:
                continue
            if c.name == "get_series_observations":
                sid = c.arguments.get("series_id")
                if sid and sid.upper() not in seen:
                    seen.append(sid.upper())
            elif c.name == "compare_series":
                for sid in c.arguments.get("series_ids", []):
                    if sid.upper() not in seen:
                        seen.append(sid.upper())
        return seen

    @property
    def had_error(self) -> bool:
        return any(not c.ok for c in self.tool_calls)

    def summary(self) -> dict[str, Any]:
        """The run's outcome without the per-call detail — the payload of the
        stream's closing `final` event."""
        return {
            "final_report": self.final_report,
            "series_used": self.series_used,
            "risk_signal": self.risk_signal,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "tool_sequence": self.tool_sequence,
            "leaf_tool_sequence": self.leaf_tool_sequence,
            "tool_calls": [vars(c) for c in self.tool_calls],
            "delegations": [vars(d) for d in self.delegations],
            "series_used": self.series_used,
            "risk_signal": self.risk_signal,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "final_report": self.final_report,
        }


def _clip(value: Any) -> Any:
    """Bound free text in an event; recurse one level into dicts/lists so
    tool arguments stay readable but small."""
    if isinstance(value, str):
        if len(value) <= _EVENT_TEXT_LIMIT:
            return value
        return value[:_EVENT_TEXT_LIMIT] + "…"
    if isinstance(value, dict):
        return {k: _clip(v) if isinstance(v, str) else v for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) if isinstance(v, str) else v for v in value[:10]]
    return value
