"""The supervisor + specialists pipeline on Google's Agent Development Kit.

`pipeline.run(query)` mirrors `agents.supervisor.run`; `agent.root_agent` is
what `adk web` / `adk run` load (point them at this package's parent, src/).
"""

import sys
import warnings
from pathlib import Path

# ADK announces this experimental flag (its own default) on every tool
# declaration; it is informational, not about our code.
warnings.filterwarnings("ignore", message=r".*JSON_SCHEMA_FOR_FUNC_DECL.*")
# ...and this one whenever the Report Agent streams (ResilientGemini).
warnings.filterwarnings("ignore", message=r".*PROGRESSIVE_SSE_STREAMING.*")

# Let `import tools`, `import agents`, ... resolve from src/ however ADK's
# tooling imported this package.
_SRC = str(Path(__file__).resolve().parent.parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from . import agent  # noqa: E402,F401  (ADK's tools look for econ_adk.agent.root_agent)
