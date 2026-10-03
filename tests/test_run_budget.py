"""Per-run token budgets (cost_tracker.run_budget).

The process-wide budget is what the MCP server and the HTTP tool endpoints
share. An agent run gets its own, so concurrent questions can't drain each
other — or be starved by the landing page's tool calls.
"""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

import cost_tracker
import tools

RANGE = ("2015-01-01", "2020-01-01", "m")


def test_scoped_budget_is_spent_instead_of_the_global_one():
    with cost_tracker.run_budget(10_000) as scoped:
        result = tools.get_series_observations("UNRATE", *RANGE)
        assert "error" not in result
        assert cost_tracker.current_budget() is scoped
    assert scoped.used_tokens > 0
    assert cost_tracker.budget.used_tokens == 0
    # Outside the block the global budget is back in charge.
    assert cost_tracker.current_budget() is cost_tracker.budget


def test_a_tight_run_budget_refuses_without_touching_the_global_one():
    with cost_tracker.run_budget(20):
        result = tools.get_series_observations("UNRATE", *RANGE)
    assert result["error"] == "session_budget_exceeded"
    assert cost_tracker.budget.used_tokens == 0


def test_concurrent_runs_are_isolated_from_each_other():
    """Two threads, each in its own run: each sees only its own spend."""
    used: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, series: str) -> None:
        with cost_tracker.run_budget(10_000) as b:
            barrier.wait()  # both scopes are open at the same time
            tools.get_series_observations(series, *RANGE)
            barrier.wait()
            used[name] = b.used_tokens

    threads = [
        threading.Thread(target=worker, args=("a", "UNRATE")),
        threading.Thread(target=worker, args=("b", "CPIAUCSL")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert used["a"] > 0 and used["b"] > 0
    # Each run saw only one fetch — not the sum of both.
    with cost_tracker.run_budget(10_000) as solo:
        tools.get_series_observations("UNRATE", *RANGE)
    assert used["a"] == solo.used_tokens
    assert cost_tracker.budget.used_tokens == 0


def test_run_budget_follows_the_run_into_to_thread_workers():
    """The orchestration pipeline fans out with asyncio.to_thread; those
    workers must spend from the run's budget, not the global one."""

    async def fan_out():
        await asyncio.gather(
            asyncio.to_thread(tools.get_series_observations, "UNRATE", *RANGE),
            asyncio.to_thread(tools.get_series_observations, "CPIAUCSL", *RANGE),
        )

    with cost_tracker.run_budget(10_000) as scoped:
        asyncio.run(fan_out())
    assert scoped.used_tokens > 0
    assert cost_tracker.budget.used_tokens == 0


def test_try_record_is_atomic_under_contention():
    b = cost_tracker.SessionBudget(limit_tokens=100)
    wins = []
    lock = threading.Lock()

    def spend():
        ok = b.try_record("t", 10)
        with lock:
            wins.append(ok)

    threads = [threading.Thread(target=spend) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 10
    assert b.used_tokens == 100


@pytest.fixture
def client():
    from app import agent
    from app.main import app

    agent._limiter.reset()
    return TestClient(app)


def test_an_exhausted_shared_budget_no_longer_breaks_agent_questions(client):
    cost_tracker.budget.limit_tokens = 0  # e.g. drained by the landing page
    r = client.post("/observations", json={
        "series_id": "UNRATE", "start_date": RANGE[0], "end_date": RANGE[1], "frequency": "m",
    })
    assert r.status_code == 429

    r = client.post("/agent/ask", json={"query": "Show core PCE since 2021."})
    assert r.status_code == 200
    body = r.json()
    assert all(c["ok"] for c in body["tool_calls"])
    assert 0 < body["data_tokens"] <= body["data_token_budget"]


def test_final_stream_event_reports_the_run_spend(client):
    r = client.post("/agent/stream", json={"query": "Show core PCE since 2021."})
    final = [line for line in r.text.split("\n\n") if '"type": "final"' in line][0]
    assert '"data_tokens"' in final and '"data_token_budget": 30000' in final


def test_agent_runs_have_a_configurable_budget(client, monkeypatch):
    from app import agent

    monkeypatch.setattr(agent.settings, "agent_run_token_budget", 20)
    body = client.post("/agent/ask", json={"query": "Show core PCE since 2021."}).json()
    errors = [c["error"] for c in body["tool_calls"] if not c["ok"]]
    assert "session_budget_exceeded" in errors
    assert body["data_token_budget"] == 20
    assert cost_tracker.budget.used_tokens == 0
