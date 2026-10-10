"""Shared test setup: run everything offline, hermetic, and deterministic.

`src/` is put on the path by `[tool.pytest.ini_options] pythonpath` in
pyproject.toml — no sys.path juggling here.
"""

import pytest


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_OFFLINE", "1")
    monkeypatch.setenv("NEWS_OFFLINE", "1")
    monkeypatch.setenv("AGENT_BACKEND", "stub")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setenv("AGENT_TRACE_PATH", str(tmp_path / "agent_trace.log"))
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("NEWS_API_KEY", raising=False)
    monkeypatch.delenv("FRED_OFFLINE_LATENCY_MS", raising=False)  # keep the suite fast
    monkeypatch.delenv("NEWS_OFFLINE_LATENCY_MS", raising=False)

    import audit_log
    import cost_tracker
    import fred_client
    import news_client
    import rate_limit
    from agents import model as agent_model

    fred_client._cache.clear()
    news_client._cache.clear()
    audit_log.reset()
    rate_limit.limiter.reset()
    cost_tracker.reset_budget()
    agent_model.reset_overload_state()
    # The HTTP API's limiters, if the app has been imported by now.
    import sys

    if (main := sys.modules.get("app.main")) is not None:
        main._tool_limiter.reset()
    yield
