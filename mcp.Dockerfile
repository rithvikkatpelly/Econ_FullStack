# syntax=docker/dockerfile:1
#
# The MCP server (src/server.py) over HTTP — MCP's streamable-HTTP transport
# at /mcp, for clients that connect by URL instead of launching it over stdio.
# Build context is the repo root:
#
#   docker build -f mcp.Dockerfile -t econ-data-mcp .
#   docker run -p 8080:8080 econ-data-mcp     # offline fixture; -e FRED_API_KEY=... for live
#
# Sessions live in this process's memory, so the Cloud Run service runs one
# instance with session affinity (.github/workflows/deploy.yml).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIT_LOG_PATH=/tmp/audit.log \
    MCP_TRANSPORT=streamable-http

WORKDIR /app

# Just what server.py and the tool layer import. pydantic <2.14 for mcp 1.9.4
# (see requirements.txt).
RUN pip install "mcp==1.9.4" "pydantic>=2.6.0,<2.14" "httpx>=0.27.0" "python-dotenv>=1.0.0"

COPY src ./src

RUN useradd --create-home --uid 10001 appuser
USER appuser

WORKDIR /app/src
EXPOSE 8080
CMD ["python", "server.py"]
