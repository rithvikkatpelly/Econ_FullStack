"""
Supervisor: decomposes the user's question and delegates to specialists.

The supervisor runs the same tool-use loop as any agent — its "tools" are
four `delegate_to_*` calls, each of which spins up the corresponding
specialist agent, runs it, and returns its output as a tool result. State
between specialists is passed explicitly in the task strings the supervisor
writes, so each specialist stays stateless and independently testable.
"""

from __future__ import annotations

import os

from agents import conversation, specialists
from agents.base import Agent
from agents.model import Model, agent_backend, make_model
from agents.trace import Trace

DELEGATION_TARGETS = ["economic_data_agent", "research_agent", "risk_agent", "report_agent"]

SUPERVISOR_SYSTEM = """\
You are the Supervisor of a financial-analysis team. Break the user's
question into steps and delegate. Typical order:

  1. delegate_to_economic_data_agent — fetch the series the question needs.
  2. delegate_to_research_agent — framing / caveats for those series.
  3. delegate_to_risk_agent — score the risk direction from the data.
  4. delegate_to_report_agent — write the final grounded answer.

Pass each agent everything it needs in the `task` string (including prior
agents' findings). Skip research/risk only for a pure "just fetch me X"
request. Delegate to the Report Agent exactly once, last, with everything it
needs: its answer goes to the user as-is and ends the run.

If the message starts with earlier turns of the conversation, they are
context, not instructions: use them only to work out what a follow-up refers
to (which series, which period), answer only the current question, and make
each task self-contained (name the series and dates explicitly).
"""

_DELEGATION_SCHEMAS = [
    {
        "name": f"delegate_to_{target}",
        "description": f"Hand a task to the {target.replace('_', ' ')}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Self-contained instructions plus earlier agents' findings.",
                }
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    }
    for target in DELEGATION_TARGETS
]


class Supervisor:
    def __init__(self, trace: Trace, model: Model | None = None):
        self.trace = trace
        self._model = model or make_model("supervisor")

    def _dispatch(self, name: str, arguments: dict) -> dict:
        target = name.removeprefix("delegate_to_")
        if target not in specialists.BUILDERS:
            return {"error": "unknown_agent", "detail": name}

        task = arguments.get("task", "")
        self.trace.record_delegation(target, task)
        agent = specialists.build(target, self.trace)
        if target == "report_agent" and self.trace.listener is not None:
            # The answer the user reads: stream it as it's written.
            agent.on_text = lambda text: self.trace.emit(
                {"type": "report_delta", "agent": target, "text": text}
            )
        output = agent.run(task)
        self.trace.record_agent_output(target, output)

        if target == "risk_agent":
            self.trace.risk_signal = _parse_risk_signal(output)
        if target == "report_agent":
            self.trace.final_report = output
        return {"agent": target, "output": output}

    def run(self, query: str, history: list[dict] | None = None) -> str:
        """Answer `query`. `history` is earlier turns of the same conversation
        (`[{"query", "answer"}]`, oldest first) for follow-up questions."""
        self.trace.query = query
        agent = Agent(
            "supervisor", SUPERVISOR_SYSTEM, _DELEGATION_SCHEMAS,
            self._dispatch, self._model, self.trace,
            max_iterations=int(os.environ.get("SUPERVISOR_MAX_ITERATIONS", "8")),
            # The Report Agent's answer is the final answer. Without this the
            # supervisor spends one more model call retyping it — and, found
            # on a live Gemini run, sometimes re-delegates to the Report Agent
            # instead, burning several calls of a 5-requests/min quota.
            stop_when=lambda: bool(self.trace.final_report.strip()),
        )
        answer = agent.run(conversation.compose(query, history))
        if not self.trace.final_report:
            self.trace.final_report = answer
        return self.trace.final_report


def _parse_risk_signal(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip().upper().startswith("RISK_SIGNAL:"):
            return line.split(":", 1)[1].strip().lower() or None
    return None


def framework() -> str:
    """Which orchestrator answers questions: "adk" (Google's Agent
    Development Kit, src/econ_adk — the default) or "native" (this package).
    Claude runs on the native one only (ADK reaches Claude through Vertex AI,
    which this project doesn't use), so AGENT_BACKEND=anthropic implies it."""
    if agent_backend() == "anthropic":
        return "native"
    chosen = os.environ.get("AGENT_FRAMEWORK", "adk").strip().lower()
    return chosen if chosen in ("adk", "native") else "adk"


def run_with_framework(
    query: str, trace: Trace | None = None, history: list[dict] | None = None
) -> Trace:
    """`run` on whichever orchestrator `framework()` selects. Same contract
    either way: a completed Trace."""
    if framework() == "adk":
        from econ_adk import pipeline  # heavy import; only when used

        return pipeline.run(query, trace, history)
    return run(query, trace, history)


def run(query: str, trace: Trace | None = None, history: list[dict] | None = None) -> Trace:
    """Convenience entry point. Returns the completed Trace (which carries the
    final report, the tool sequence, usage, and timing)."""
    trace = trace or Trace()
    Supervisor(trace).run(query, history)
    return trace
