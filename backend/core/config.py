"""Centralized application settings for the HTTP interface.

pydantic-settings, backed by the repo-root ``.env`` (the same file the MCP
server and the agents read). This is the typed view of configuration; the
older modules under ``src/`` still read ``os.environ`` directly at import
time, so :meth:`Settings.apply_to_environ` pushes these values back out
before those modules are imported (see ``backend/app/__init__.py``).

Local dev vs. production: pydantic-settings resolves each field from, in
order, (1) a real environment variable, (2) ``.env``, (3) the default above —
the first one present wins. Locally that means ``.env`` (gitignored, never
committed). In the deployed container there is no ``.env`` file at all (see
``.dockerignore``): Cloud Run injects ``FRED_API_KEY`` as a real environment
variable, sourced from Secret Manager via the `--set-secrets` flag in
``.github/workflows/deploy.yml`` (`FRED_API_KEY=<secret-name>:latest`), so it
resolves at step (1) with no code path or config difference between the two —
only where the value physically comes from changes. See DEPLOYMENT.md for the
one-time Secret Manager setup. The Gemini agent backend follows the same rule:
``GEMINI_API_KEY`` from Secret Manager, or Vertex AI via the service account.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- FRED -------------------------------------------------------------
    # Leave FRED_API_KEY unset to run against the built-in synthetic fixture
    # (fred_client falls back to offline automatically). FRED_OFFLINE forces
    # it either way: "1"/"0", or "" for the automatic behaviour.
    fred_api_key: str = ""
    fred_offline: str = ""

    # --- Cost guardrail -------------------------------------------------
    # Rough per-session token budget enforced by cost_tracker.guard_or_shrink
    # on every /observations and /compare call — identical to the MCP path.
    session_token_budget: int = 50000

    # --- Agent (POST /agent/ask, /agent/stream) ---------------------------
    # Which model drives the supervisor + specialists: "stub" (deterministic,
    # free, the default), "gemini", or "anthropic". See src/agents/model.py.
    agent_backend: str = "stub"
    # Which orchestrator: "adk" (Google Agent Development Kit, the default) or
    # "native". See agents.supervisor.framework().
    agent_framework: str = "adk"
    # Gemini: either an API key (Gemini API) ...
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    # ... or Vertex AI with the runtime service account (no key at all). The
    # google-genai SDK reads these three straight from the environment.
    google_genai_use_vertexai: str = ""
    google_cloud_project: str = ""
    google_cloud_location: str = ""
    # Abuse/spend controls on the agent endpoints only. A live run is several
    # model calls, so these are far tighter than the per-tool limits.
    agent_rate_limit_per_min: float = 6
    agent_rate_limit_burst: float = 3
    agent_max_concurrent_runs: int = 2
    agent_max_query_chars: int = 500
    # Tool-data tokens one question may pull (cost_tracker.run_budget). Each
    # run gets its own, separate from SESSION_TOKEN_BUDGET, which the tool
    # endpoints share.
    agent_run_token_budget: int = 30000
    # On Gemini, answer on the offline stub once the model's quota is used up
    # (labelled in the response) instead of failing the question. What keeps a
    # free-tier demo usable after its ~20 requests/day. See app/agent.py.
    agent_stub_fallback: bool = True

    @property
    def agent_model_configured(self) -> bool:
        """Whether the selected backend has credentials (never the value)."""
        backend = self.agent_backend.strip().lower()
        if backend == "gemini":
            return bool(
                self.gemini_api_key
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or self.google_genai_use_vertexai.lower() in {"1", "true"}
            )
        if backend == "anthropic":
            return bool(os.environ.get("ANTHROPIC_API_KEY"))
        return True  # the stub needs nothing

    # --- API ----------------------------------------------------------
    api_title: str = "Econ Data API"
    api_version: str = "0.1.0"
    # Comma-separated; a plain string (not list[str]) so it can be passed as a
    # single --set-env-vars value without JSON-quoting. Defaults to the Vite
    # dev server.
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    def apply_to_environ(self) -> None:
        """Materialize settings into ``os.environ`` for the ``src/`` modules
        that read it directly (``fred_client``, ``cost_tracker``, ...).

        Precedence stays intuitive: a value already in the real environment
        wins over ``.env`` wins over a default here — we only fill blanks.
        """
        for name, value in {
            "FRED_API_KEY": self.fred_api_key,
            "FRED_OFFLINE": self.fred_offline,
            "SESSION_TOKEN_BUDGET": str(self.session_token_budget),
            "AGENT_BACKEND": self.agent_backend,
            "AGENT_FRAMEWORK": self.agent_framework,
            "GEMINI_API_KEY": self.gemini_api_key,
            "GEMINI_MODEL": self.gemini_model,
            "GOOGLE_GENAI_USE_VERTEXAI": self.google_genai_use_vertexai,
            "GOOGLE_CLOUD_PROJECT": self.google_cloud_project,
            "GOOGLE_CLOUD_LOCATION": self.google_cloud_location,
        }.items():
            if value != "" and name not in os.environ:
                os.environ[name] = value


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings instance."""
    return Settings()
