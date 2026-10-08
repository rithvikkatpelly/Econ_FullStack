"""Cloud Trace for the agent runs, opt-in with CLOUD_TRACE=1.

ADK already emits OpenTelemetry spans for every agent invocation, model call
and tool call; this sends them to Cloud Trace through Google's Telemetry API
(ADK's own exporter), and `question_span` adds one root span per question so
the native orchestrator is covered too. Without CLOUD_TRACE, OpenTelemetry's
no-op tracer is used and none of this costs anything.

The runtime service account needs roles/telemetry.tracesWriter, and the
project the telemetry.googleapis.com API (DEPLOYMENT.md).
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator

from opentelemetry import trace

logger = logging.getLogger("econ_data_api")
_tracer = trace.get_tracer("econ_data_api")


def enabled() -> bool:
    return os.environ.get("CLOUD_TRACE", "").strip().lower() in {"1", "true", "yes"}


def setup() -> bool:
    """Install the Cloud Trace exporter if CLOUD_TRACE is on. Never raises:
    tracing must not take the API down."""
    if not enabled():
        return False
    try:
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers
        from opentelemetry.sdk.resources import Resource

        maybe_set_otel_providers(
            [get_gcp_exporters(enable_cloud_tracing=True)],
            Resource.create({"service.name": os.environ.get("K_SERVICE", "econ-data-api")}),
        )
        return True
    except Exception:  # noqa: BLE001 - logged, API keeps serving
        logger.exception("Cloud Trace setup failed; continuing without tracing")
        return False


@contextlib.contextmanager
def question_span(framework: str, follow_up: bool) -> Iterator[trace.Span]:
    """The root span for one question. The question text isn't recorded —
    it's user input; the shape of the run is."""
    with _tracer.start_as_current_span(
        "agent.question", attributes={"agent.framework": framework, "agent.follow_up": follow_up}
    ) as span:
        yield span


def record_run(span: trace.Span, trace_, usage: dict) -> None:
    """What happened, on the span: which backend answered (and why, if not the
    configured one), tokens, series, and one event per delegation/tool call."""
    span.set_attributes({
        "agent.backend": usage.get("backend", ""),
        "agent.degraded": usage.get("degraded") or "",
        "agent.input_tokens": trace_.input_tokens,
        "agent.output_tokens": trace_.output_tokens,
        "agent.data_tokens": usage.get("data_tokens", 0),
        "agent.series_used": ",".join(trace_.series_used),
        "agent.risk_signal": trace_.risk_signal or "",
        "agent.tool_calls": len(trace_.tool_calls),
    })
    for d in trace_.delegations:
        span.add_event("delegation", {"agent": d.to})
    for c in trace_.tool_calls:
        span.add_event("tool_call", {"agent": c.agent, "tool": c.name, "ok": c.ok,
                                     "latency_ms": round(c.latency_ms, 1)})
