"""Cloud Trace for agent runs (backend/app/telemetry.py): one root span per
question carrying the run's shape, and setup that never takes the API down."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def spans(monkeypatch):
    from app import telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", provider.get_tracer("test"))
    return exporter


def test_each_question_gets_a_root_span_with_the_run_shape(spans):
    from app import agent
    from app.main import app

    agent._limiter.reset()
    query = {"query": "Compare CPI and unemployment since 2019."}
    r = TestClient(app).post("/agent/ask", json=query)
    assert r.status_code == 200
    (span,) = [s for s in spans.get_finished_spans() if s.name == "agent.question"]
    a = span.attributes
    assert a["agent.framework"] == "adk" and a["agent.backend"] == "stub"
    assert set(a["agent.series_used"].split(",")) == {"CPIAUCSL", "UNRATE"}
    assert a["agent.tool_calls"] > 0
    events = [e.name for e in span.events]
    assert "delegation" in events and "tool_call" in events
    # The question itself (user input) is not recorded.
    assert not any("Compare CPI" in str(v) for v in a.values())


def test_tracing_is_off_unless_asked_and_setup_never_raises(monkeypatch):
    from app import telemetry

    monkeypatch.delenv("CLOUD_TRACE", raising=False)
    assert telemetry.setup() is False

    import google.adk.telemetry.google_cloud as gc

    def broken(**_kw):
        raise RuntimeError("no credentials")

    monkeypatch.setenv("CLOUD_TRACE", "1")
    monkeypatch.setattr(gc, "get_gcp_exporters", broken)
    assert telemetry.setup() is False
