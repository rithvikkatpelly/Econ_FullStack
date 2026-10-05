"""The agent endpoints (backend/app/agent.py): POST /agent/ask and the
server-sent-event stream at POST /agent/stream.

Hermetic: conftest pins AGENT_BACKEND=stub and the offline FRED fixture, so
the full supervisor pipeline runs deterministically with no model or network.
"""

import json

import pytest
from fastapi.testclient import TestClient

QUERY = "Compare CPI and unemployment over the last 5 years and explain the relationship."


@pytest.fixture
def client():
    from app import agent
    from app.main import app

    agent._limiter.reset()
    agent.reset_live_pause()
    yield TestClient(app)
    agent.reset_live_pause()


def _events(response) -> list[dict]:
    frames = [f for f in response.text.split("\n\n") if f.strip()]
    assert all(f.startswith("data: ") for f in frames), frames
    return [json.loads(f.removeprefix("data: ")) for f in frames]


def test_health_reports_the_agent_backend(client):
    body = client.get("/health").json()
    assert body["agent_backend"] == "stub"
    assert body["agent_framework"] == "adk"  # the default orchestrator
    assert body["agent_model_configured"] is True


def test_ask_returns_the_full_trace(client):
    r = client.post("/agent/ask", json={"query": QUERY})
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "stub"
    assert [d["to"] for d in body["delegations"]] == [
        "economic_data_agent", "research_agent", "risk_agent", "report_agent"
    ]
    assert set(body["series_used"]) == {"CPIAUCSL", "UNRATE"}
    assert "Evidence" in body["final_report"]


def test_stream_is_sse_and_tells_the_story_in_order(client):
    r = client.post("/agent/stream", json={"query": QUERY})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"

    events = _events(r)
    types = [e["type"] for e in events]
    assert types[0] == "start" and events[0]["query"] == QUERY
    assert types[-1] == "final"

    delegations = [e["agent"] for e in events if e["type"] == "delegation"]
    assert delegations == ["economic_data_agent", "research_agent", "risk_agent", "report_agent"]
    # Each specialist's output is announced after its delegation, and its tool
    # calls fall between the two.
    first_out = types.index("agent_output")
    assert types.index("delegation") < types.index("tool_call") < first_out

    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls[0]["agent"] == "economic_data_agent"
    assert calls[0]["tool"] == "compare_series"
    assert calls[0]["ok"] is True
    assert set(calls[0]["arguments"]["series_ids"]) == {"CPIAUCSL", "UNRATE"}

    final = events[-1]
    assert final["backend"] == "stub"
    assert set(final["series_used"]) == {"CPIAUCSL", "UNRATE"}
    assert final["risk_signal"] in {"rising", "elevated", "stable", "easing"}
    assert "Evidence" in final["final_report"]
    # Monotonic clock on every event, for the timeline.
    elapsed = [e["elapsed_ms"] for e in events if "elapsed_ms" in e]
    assert elapsed == sorted(elapsed)


def test_stream_never_carries_tool_results(client):
    """Untrusted FRED notes (INJTEST is the poisoned fixture series) are
    fetched during the run but must not appear anywhere in the event feed."""
    r = client.post("/agent/stream", json={"query": "Explain the series INJTEST since 2020."})
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in r.text
    assert "attacker@example.com" not in r.text
    assert all("result" not in e for e in _events(r) if e["type"] == "tool_call")


def test_query_is_normalised_and_bounded(client):
    assert client.post("/agent/ask", json={"query": "  "}).status_code == 422
    assert client.post("/agent/ask", json={"query": "x" * 501}).status_code == 422
    r = client.post("/agent/stream", json={"query": "  Show   core PCE\nsince 2021. "})
    assert _events(r)[0]["query"] == "Show core PCE since 2021."


def test_rate_limit_is_per_client_and_structured(client):
    from app import agent

    statuses = [
        client.post("/agent/ask", json={"query": "Show core PCE since 2021."}).status_code
        for _ in range(int(agent.settings.agent_rate_limit_burst) + 1)
    ]
    assert statuses[:-1] == [200] * (len(statuses) - 1)
    assert statuses[-1] == 429

    r = client.post("/agent/ask", json={"query": "Show core PCE since 2021."})
    assert r.json()["detail"]["error"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    # A different client (Cloud Run forwards the caller in X-Forwarded-For)
    # has its own bucket.
    other = client.post(
        "/agent/ask",
        json={"query": "Show core PCE since 2021."},
        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
    )
    assert other.status_code == 200


def test_busy_when_every_slot_is_taken(client, monkeypatch):
    import threading

    from app import agent

    monkeypatch.setattr(agent, "_slots", threading.BoundedSemaphore(1))
    agent._slots.acquire()
    r = client.post("/agent/ask", json={"query": "Show core PCE since 2021."})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "agent_busy"
    agent._slots.release()
    assert client.post("/agent/ask", json={"query": "Show core PCE since 2021."}).status_code == 200


def test_failures_are_masked_and_release_the_slot(client, monkeypatch):
    import threading

    from app import agent

    def boom(*_a, **_k):
        raise RuntimeError("provider said: key=sk-secret-123")

    monkeypatch.setattr(agent, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(agent, "_run", boom)

    r = client.post("/agent/ask", json={"query": "Show core PCE since 2021."})
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "agent_error"
    assert "sk-secret" not in r.text

    r = client.post("/agent/stream", json={"query": "Show core PCE since 2021."})
    events = _events(r)
    assert [e["type"] for e in events] == ["start", "error"]
    assert "sk-secret" not in r.text
    # Both failed runs gave their slot back.
    assert agent._slots.acquire(blocking=False)
    agent._slots.release()


def test_stream_runs_on_the_gemini_backend(client, monkeypatch):
    """End to end over HTTP with AGENT_BACKEND=gemini: a scripted fake Gemini
    client drives the supervisor, which delegates once; the Report Agent's
    answer streams back as report_delta events, chunk by chunk."""
    types = pytest.importorskip("google.genai.types")
    import google.genai

    def resp(parts):
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=parts),
                                        finish_reason="STOP")],
        )

    script = {
        "supervisor": [
            resp([types.Part(function_call=types.FunctionCall(
                name="delegate_to_report_agent", args={"task": "Say hello."}))]),
            resp([types.Part(text="Hello from the report agent.")]),
        ],
    }
    report_chunks = ["Hello ", "from the ", "report agent."]

    class Models:
        def generate_content(self, *, model, contents, config):
            assert not config.system_instruction.startswith("You are the Report")
            return script["supervisor"].pop(0)

        def generate_content_stream(self, *, model, contents, config):
            assert config.system_instruction.startswith("You are the Report")
            for text in report_chunks:
                yield resp([types.Part(text=text)])

    class Client:
        models = Models()

        def __init__(self, **_kwargs):  # the real client gets retry options
            pass

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setenv("AGENT_FRAMEWORK", "native")  # fakes the native GeminiModel's client
    monkeypatch.setattr(google.genai, "Client", Client)

    events = _events(client.post("/agent/stream", json={"query": "Say hello."}))
    assert [e["type"] for e in events] == [
        "start", "delegation", "report_delta", "report_delta", "report_delta",
        "agent_output", "final",
    ]
    assert events[0]["backend"] == "gemini"
    assert [e["text"] for e in events if e["type"] == "report_delta"] == report_chunks
    assert events[-1]["final_report"] == "Hello from the report agent."


def test_stub_backend_delivers_the_report_as_one_delta(client):
    """Backends without native streaming still emit report_delta — once — so
    the UI has a single code path."""
    events = _events(client.post("/agent/stream", json={"query": QUERY}))
    deltas = [e for e in events if e["type"] == "report_delta"]
    assert len(deltas) == 1
    assert deltas[0]["text"] == events[-1]["final_report"]
    # It arrives after the Report Agent is delegated to, before it finishes.
    types = [e["type"] for e in events]
    assert types.index("report_delta") < len(types) - 2
    assert types[types.index("report_delta") + 1] == "agent_output"


def test_ask_endpoint_does_not_stream(client, monkeypatch):
    """No listener, no streaming: /agent/ask uses plain turns."""
    from agents.model import StubModel

    monkeypatch.setenv("AGENT_FRAMEWORK", "native")
    def boom(*_a, **_k):
        raise AssertionError("stream_turn used without a listener")

    monkeypatch.setattr(StubModel, "stream_turn", boom)
    assert client.post("/agent/ask", json={"query": QUERY}).status_code == 200


def test_quota_exhaustion_is_reported_as_such(client, monkeypatch):
    """A used-up model quota gets its own error code (and 429), so the UI can
    say "try again in a minute" instead of "rephrase your question"."""
    from agents.model import QuotaExhausted
    from app import agent

    def out_of_quota(*_a, **_k):
        raise QuotaExhausted("The Gemini API quota for this key is used up for now.")

    monkeypatch.setattr(agent, "_run", out_of_quota)
    r = client.post("/agent/ask", json={"query": "Show core PCE since 2021."})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "model_quota_exhausted"

    events = _events(client.post("/agent/stream", json={"query": "Show core PCE since 2021."}))
    assert events[-1]["type"] == "error"
    assert events[-1]["error"] == "model_quota_exhausted"


def test_the_framework_is_selectable_and_reported(client, monkeypatch):
    for framework in ("adk", "native"):
        monkeypatch.setenv("AGENT_FRAMEWORK", framework)
        events = _events(client.post("/agent/stream", json={"query": QUERY}))
        assert events[0]["framework"] == framework
        assert events[-1]["framework"] == framework
        assert set(events[-1]["series_used"]) == {"CPIAUCSL", "UNRATE"}


def test_claude_always_runs_on_the_native_orchestrator(monkeypatch):
    from agents.supervisor import framework

    monkeypatch.setenv("AGENT_FRAMEWORK", "adk")
    monkeypatch.setenv("AGENT_BACKEND", "anthropic")
    assert framework() == "native"


def test_stream_runs_on_adk_with_gemini(client, monkeypatch):
    """End to end over HTTP on the ADK pipeline with AGENT_BACKEND=gemini:
    ADK's own Gemini model class, scripted. The supervisor delegates once and
    the Report Agent's answer streams back in chunks."""
    from google.adk.models import Gemini, LlmResponse
    from google.genai import types as gt

    def content(*parts):
        return gt.Content(role="model", parts=list(parts))

    async def fake(self, llm_request, stream=False):
        system = str(llm_request.config.system_instruction or "")
        if system.startswith("You are the Report"):
            assert stream  # ResilientGemini streams internally for the report
            for chunk in ["Hello ", "from ADK."]:
                yield LlmResponse(partial=True, content=content(gt.Part(text=chunk)))
            yield LlmResponse(content=content(gt.Part(text="Hello from ADK.")))
            return
        yield LlmResponse(content=content(gt.Part(function_call=gt.FunctionCall(
            id="d1", name="report_agent", args={"request": "Say hello."}))))

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setenv("AGENT_FRAMEWORK", "adk")
    monkeypatch.setattr(Gemini, "generate_content_async", fake)

    events = _events(client.post("/agent/stream", json={"query": "Say hello."}))
    assert [e["type"] for e in events] == [
        "start", "delegation", "report_delta", "report_delta", "agent_output", "final",
    ]
    assert events[0]["framework"] == "adk" and events[0]["backend"] == "gemini"
    assert [e["text"] for e in events if e["type"] == "report_delta"] == ["Hello ", "from ADK."]
    # skip_summarization: the report is the answer; no supervisor restatement.
    assert events[-1]["final_report"] == "Hello from ADK."


# --- free-tier demo: answer on the stub once the Gemini quota is gone ---------


def _gemini_out_of_quota(monkeypatch, retry_after=None):
    """AGENT_BACKEND=gemini, where every live run hits a used-up quota; runs
    pinned to the stub go through for real. Returns the live-attempt count."""
    from agents.model import QuotaExhausted, agent_backend
    from app import agent

    real = agent._run_once
    attempts = []

    def run_once(req, trace):
        if agent_backend() == "gemini":
            attempts.append(req.query)
            trace.emit({"type": "report_delta", "agent": "report_agent", "text": "Half an ans"})
            raise QuotaExhausted("used up", retry_after=retry_after)
        return real(req, trace)

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setattr(agent, "_run_once", run_once)
    return attempts


def test_used_up_quota_is_answered_on_the_stub_and_labelled(client, monkeypatch):
    attempts = _gemini_out_of_quota(monkeypatch)
    events = _events(client.post("/agent/stream", json={"query": QUERY}))
    types = [e["type"] for e in events]

    assert events[0]["backend"] == "gemini"
    fallback = next(e for e in events if e["type"] == "fallback")
    assert fallback["restart"] is True and fallback["to_model"] == "offline stub"
    # The abandoned Gemini text came before the restart; the stub's answer after.
    assert types.index("report_delta") < types.index("fallback")
    assert "delegation" in types[types.index("fallback"):]
    final = events[-1]
    assert final["type"] == "final"
    assert final["backend"] == "stub" and final["degraded"] == "model_quota_exhausted"
    assert set(final["series_used"]) == {"CPIAUCSL", "UNRATE"}
    assert attempts == [QUERY]


def test_later_questions_skip_gemini_until_the_quota_resets(client, monkeypatch):
    attempts = _gemini_out_of_quota(monkeypatch)
    assert client.get("/health").json()["agent_answering_on"] == "gemini"
    client.post("/agent/ask", json={"query": QUERY})
    assert client.get("/health").json()["agent_answering_on"] == "stub"

    r = client.post("/agent/ask", json={"query": "Show core PCE since 2021."})
    assert r.status_code == 200
    assert r.json()["backend"] == "stub" and r.json()["degraded"] == "model_quota_exhausted"
    events = _events(client.post("/agent/stream", json={"query": QUERY}))
    assert events[0]["backend"] == "stub"
    assert next(e for e in events if e["type"] == "fallback")["restart"] is False
    assert len(attempts) == 1  # only the first question tried Gemini


def test_fallback_can_be_turned_off(client, monkeypatch):
    from app import agent

    _gemini_out_of_quota(monkeypatch)
    monkeypatch.setattr(agent.settings, "agent_stub_fallback", False)
    r = client.post("/agent/ask", json={"query": QUERY})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "model_quota_exhausted"
    assert not agent.live_paused()


def test_pause_lasts_until_google_says_or_the_pacific_midnight_reset():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import agent

    pt = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 10, 5, 15, 30, tzinfo=pt).timestamp()
    try:
        agent._pause_live(None, now=now)
        assert agent._live_paused_until == datetime(2026, 10, 6, tzinfo=pt).timestamp()
        agent.reset_live_pause()
        agent._pause_live(90.0, now=now)
        assert agent._live_paused_until == now + 90.0
        agent._pause_live(10.0, now=now)  # never shortens a pause
        assert agent._live_paused_until == now + 90.0
    finally:
        agent.reset_live_pause()


def test_backend_override_is_scoped_to_the_run(monkeypatch):
    from agents.model import agent_backend, backend_override

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    with backend_override("stub"):
        assert agent_backend() == "stub"
    assert agent_backend() == "gemini"


def test_real_daily_quota_error_on_adk_falls_back_to_the_stub(client, monkeypatch):
    """No mocked run: ADK's Gemini class raises Google's real per-day 429
    shape on every model, and the API still answers — on the stub."""
    import time

    from google.adk.models import Gemini
    from google.genai import errors

    from app import agent
    from econ_adk import pipeline

    async def out_of_quota(self, llm_request, stream=False):
        raise errors.ClientError(429, {"error": {"code": 429, "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17000s"},
        ]}})
        yield  # pragma: no cover

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setenv("AGENT_FRAMEWORK", "adk")
    monkeypatch.setattr(pipeline, "GEMINI_FALLBACK_MODELS", [])
    monkeypatch.setattr(Gemini, "generate_content_async", out_of_quota)

    events = _events(client.post("/agent/stream", json={"query": QUERY}))
    final = events[-1]
    assert final["type"] == "final", events
    assert final["backend"] == "stub" and final["degraded"] == "model_quota_exhausted"
    assert "Evidence" in final["final_report"]
    # Paused for Google's own estimate, not forever.
    assert 16900 < agent._live_paused_until - time.time() <= 17000
