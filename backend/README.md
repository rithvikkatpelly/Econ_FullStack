# Econ Data API (`backend/`)

A FastAPI HTTP interface over the FRED tool logic — a second, independently
deployable surface alongside the MCP server (`src/server.py`). Both call the
same `src/tools.py` (which composes `fred_client` + `security` + `cost_tracker`);
no logic is duplicated or moved.

```
backend/
  app/
    __init__.py   puts ../src on sys.path + loads .env into os.environ (must run first)
    main.py       FastAPI app: /health, POST /search, POST /observations, POST /compare, GET /metadata/{id}
    agent.py      POST /agent/ask + POST /agent/stream (SSE) — the multi-agent supervisor over HTTP
    schemas.py    pydantic request/response models (mirror src/tools.py output)
  core/
    config.py     pydantic-settings, backed by the repo-root .env
```

## Run

```bash
pip install -r ../requirements.txt
cd backend
uvicorn app.main:app --reload
```

Docs: http://127.0.0.1:8000/docs · see the root README's "Run the API" section
for `curl` examples and deployment notes.

Runs against the built-in synthetic fixture unless `FRED_API_KEY` is set in
`../.env`. The agent endpoints use the deterministic `stub` planner unless
`AGENT_BACKEND=gemini` (with `GEMINI_API_KEY`, or Vertex AI via
`GOOGLE_GENAI_USE_VERTEXAI=true`) — see the root README's "Ask the agent".

## Docker

Build context is the **repo root** (the API imports `src/`):

```bash
docker build -f backend/Dockerfile -t econ-data-api .
docker run -p 8080:8080 -e CORS_ALLOWED_ORIGINS=http://localhost:5173 econ-data-api
```

`backend/requirements.txt` is the slim runtime subset the image installs.
Full deployment notes: [`../DEPLOYMENT.md`](../DEPLOYMENT.md).
