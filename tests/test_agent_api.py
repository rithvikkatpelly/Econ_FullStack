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
    return TestClient(app)


def _events(response) -> list[dict]:
    frames = [f for f in response.text.split("\n\n") if f.strip()]
    assert all(f.startswith("data: ") for f in frames), frames
    return [json.loads(f.removeprefix("data: ")) for f in frames]


def test_health_reports_the_agent_backend(client):
    body = client.get("/health").json()
    assert body["agent_backend"] == "stub"
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
    client drives the supervisor, which delegates once and answers."""
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
        "report_agent": [resp([types.Part(text="Hello from the report agent.")])],
    }

    class Models:
        def generate_content(self, *, model, contents, config):
            role = ("report_agent" if config.system_instruction.startswith("You are the Report")
                    else "supervisor")
            return script[role].pop(0)

    class Client:
        models = Models()

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setattr(google.genai, "Client", Client)

    events = _events(client.post("/agent/stream", json={"query": "Say hello."}))
    assert [e["type"] for e in events] == ["start", "delegation", "agent_output", "final"]
    assert events[0]["backend"] == "gemini"
    assert events[-1]["final_report"] == "Hello from the report agent."
