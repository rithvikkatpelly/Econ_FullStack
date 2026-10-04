"""`root_agent` for ADK's developer tools:

    cd src && adk web          # browser dev UI, pick "econ_adk"
    cd src && adk run econ_adk # terminal

Uses AGENT_BACKEND like everything else: `stub` (default, offline) or
`gemini` (needs GEMINI_API_KEY, or Vertex AI settings). The trace it records
into has no listener; the app's own entry point is `pipeline.run`.
"""

from agents.trace import Trace
from econ_adk.pipeline import build_supervisor

root_agent = build_supervisor(Trace())
