# Deployment

The repo has three runnable surfaces. Only the **HTTP API + frontend** are
meant to be *hosted*; the MCP server runs on the user's own machine next to
Claude Desktop, and the Streamlit page is a local demo.

```
frontend/ (Cloud Run, nginx) ──HTTP/SSE──▶ backend/app (Cloud Run, FastAPI) ──▶ FRED API
                                              │   └─ /agent/* ──▶ Gemini (Vertex AI)
       src/server.py (MCP, local) ───────────┘  same src/tools.py, no shared process
```

The hosted stack is all Google: React on Cloud Run, FastAPI on Cloud Run, and
the multi-agent pipeline behind `/agent/stream` running on Gemini through
Vertex AI, authenticated as the Cloud Run service account (no model key).

**What's already built and committed:**

- `backend/Dockerfile`, `backend/requirements.txt` — containerized API,
  listens on `$PORT` (Cloud Run's contract), non-root user.
- `frontend/Dockerfile`, `frontend/nginx.conf` — static Vite build served by
  nginx, also listens on 8080.
- CORS (`backend/app/main.py` + `CORS_ALLOWED_ORIGINS` in
  `backend/core/config.py`) — already wired, just needs the deployed
  frontend's URL.
- `GET /health` — reports `status`, whether `FRED_API_KEY` actually resolved
  (`fred_api_key_configured`, never the value itself), `offline`, and which
  model drives the agents (`agent_backend`, `agent_model_configured`). No
  database in this project, so there's nothing else to check.
- `POST /agent/ask`, `POST /agent/stream` (`backend/app/agent.py`) — the
  supervisor + specialist agents over HTTP, the second streamed as
  server-sent events for the UI's activity timeline. Per-client rate limit,
  a concurrent-run cap, and a query length limit are built in.
- Structured JSON logging (`backend/app/main.py`) — one line per request
  (method, path, status, latency) to stdout, which Cloud Run ships to Cloud
  Logging automatically; no sidecar or extra config needed on this end.
- `.github/workflows/deploy.yml` — builds both images, pushes to Artifact
  Registry, deploys both to Cloud Run on every push to `main`, and wires the
  backend's `--startup-probe` to `GET /health` (so a revision that boots but
  can't actually serve never receives traffic).

**What this document covers:** the one-time GCP setup that workflow depends
on. After it's done once, deploying is just `git push`. Nothing in this
document has been run — no GCP project exists for this repo yet. The exact
commands below are what to run to bring one up.

---

## 0. What you'll need

- A Google account (personal Gmail is fine) and a project with billing
  linked — Cloud Run's free tier is generous (2M requests/month before any
  charge) and both services here deploy with `--min-instances=0`, so an
  idle demo costs close to nothing.
- A FRED API key: https://fred.stlouisfed.org/docs/api/api_key.html (free).
- Admin access to this GitHub repo, to add Actions secrets/variables.

---

## 1. Create a project and install the CLI

```bash
brew install --cask google-cloud-sdk
gcloud auth login

gcloud projects create YOUR_PROJECT_ID   # or use an existing project
gcloud config set project YOUR_PROJECT_ID
```

Replace `YOUR_PROJECT_ID` everywhere below with your actual project ID (not
the display name).

---

## 2. Enable the required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  aiplatform.googleapis.com
```

(`aiplatform` is Vertex AI — what the agents call Gemini through.)

---

## 3. Create an Artifact Registry repo for the Docker images

```bash
REGION=us-central1   # pick any region; use the SAME one everywhere below

gcloud artifacts repositories create econ-data \
  --repository-format=docker \
  --location="$REGION" \
  --description="Econ Data API + frontend images"
```

---

## 4. Store the FRED API key in Secret Manager

```bash
printf '%s' 'YOUR_FRED_API_KEY' | gcloud secrets create fred-api-key \
  --data-file=- --replication-policy=automatic
```

To rotate it later: `printf '%s' 'NEW_KEY' | gcloud secrets versions add fred-api-key --data-file=-`

(This is the **only** secret the deployed API needs. Gemini is reached
through Vertex AI as the runtime service account — step 5 — so there is no
model key to store. `NEWS_API_KEY` and `ANTHROPIC_API_KEY` in `.env.example`
are for local use only and never need to exist in GCP.)

**Optional — Gemini API key instead of Vertex AI.** If you'd rather bill
through a Gemini API key (https://aistudio.google.com/apikey):

```bash
printf '%s' 'YOUR_GEMINI_API_KEY' | gcloud secrets create gemini-api-key \
  --data-file=- --replication-policy=automatic
```

…grant the runtime service account access to it the same way as
`fred-api-key` in step 5, and set the `GEMINI_API_KEY_SECRET` variable in
step 7. The workflow then uses the key and skips the Vertex settings.

---

## 5. Create a service account for GitHub Actions

```bash
gcloud iam service-accounts create github-deployer \
  --display-name="GitHub Actions deployer"

PROJECT_ID=$(gcloud config get-value project)
SA_EMAIL="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

for ROLE in \
  roles/run.admin \
  roles/artifactregistry.writer \
  roles/iam.serviceAccountUser
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE"
done
```

The **runtime** service account (the identity the deployed container itself
runs as — the default Compute Engine SA, not the deployer above) separately
needs permission to read the secret:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding fred-api-key \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor"
```

…and permission to call Gemini on Vertex AI:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/aiplatform.user"
```

(Easy to miss: `roles/run.admin` on the *deployer* only lets it deploy
revisions — it does not let the *running container* read the secret or call
Vertex AI. Both service accounts need their own grants.)

---

## 6. Set up Workload Identity Federation (keyless GitHub auth)

Lets GitHub Actions authenticate as `github-deployer` without a long-lived
JSON key ever touching a GitHub secret.

```bash
gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions pool"

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='rithvikkatpelly/Econ_FullStack'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

gcloud iam service-accounts add-iam-policy-binding \
  "github-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/rithvikkatpelly/Econ_FullStack"
```

Get the provider's full resource name — needed for the GitHub secret next:

```bash
gcloud iam workload-identity-pools providers describe "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)"
# -> projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

---

## 7. Add GitHub repository secrets and variables

**Settings → Secrets and variables → Actions** on the GitHub repo.

**Secrets** tab:

| Name | Value |
|---|---|
| `WIF_PROVIDER` | the full provider resource name from step 6 |
| `WIF_SERVICE_ACCOUNT` | `github-deployer@YOUR_PROJECT_ID.iam.gserviceaccount.com` |

**Variables** tab:

| Name | Value |
|---|---|
| `GCP_PROJECT_ID` | your project ID |
| `GCP_REGION` | `us-central1` (or whatever region you used in step 3) |
| `AR_REPO` | `econ-data` |
| `BACKEND_SERVICE` | `econ-data-api` |
| `FRONTEND_SERVICE` | `econ-data-frontend` |
| `FRED_API_KEY_SECRET` | `fred-api-key` |

Optional:

| Name | Default | Value |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | any Gemini model ID available to your project |
| `VERTEX_LOCATION` | `global` | a Vertex AI region, if you need data residency |
| `GEMINI_API_KEY_SECRET` | *(unset → Vertex AI)* | `gemini-api-key`, to use an API key instead (step 4) |

None of these variables are secret — they're just names/IDs the workflow
interpolates into `gcloud` commands (`.github/workflows/deploy.yml`). Until
`GCP_PROJECT_ID` exists the deploy job is **skipped**, not failed, so pushes
to `main` stay green before GCP is set up.

---

## 8. Deploy

Push to `main` (or run the workflow manually from the **Actions** tab —
it also has `workflow_dispatch`) and watch **Deploy to Cloud Run**. It:

1. Builds `backend/Dockerfile` (context: repo root) and pushes it to
   Artifact Registry.
2. Deploys it to Cloud Run as `econ-data-api`, wiring `FRED_API_KEY` from
   Secret Manager, `AGENT_BACKEND=gemini` plus the Vertex AI settings, and a
   `--startup-probe` against `GET /health` — a revision only starts
   receiving traffic once that returns 200.
3. Builds `frontend/Dockerfile` with `VITE_API_BASE_URL` set to the
   backend's just-deployed URL, pushes and deploys it as
   `econ-data-frontend`.
4. Updates the backend's `CORS_ALLOWED_ORIGINS` to the frontend's URL.

The job summary prints both live URLs when it's done. **Cloud Run URL
format:** `https://<SERVICE_NAME>-<hash>.<REGION>.run.app`, e.g.
`https://econ-data-api-a1b2c3d4e5-uc.a.run.app` — the `<hash>` is assigned by
Cloud Run and not predictable in advance, which is exactly why the workflow
reads it back with `gcloud run services describe` rather than constructing it.

Once deployed, hit it the same way as local dev, against the real URL:

```bash
curl -X POST https://econ-data-api-a1b2c3d4e5-uc.a.run.app/observations \
  -H "Content-Type: application/json" \
  -d '{"series_id": "UNRATE", "start_date": "2021-01-01", "end_date": "2024-01-01", "frequency": "m"}'
```

And the agents, streamed (`-N` turns off curl's buffering so you see each
event as the agents work):

```bash
curl -N -X POST https://econ-data-api-a1b2c3d4e5-uc.a.run.app/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "Compare CPI and unemployment since 2019 and explain the relationship."}'
```

(Substitute your actual backend URL from the job summary or
`gcloud run services describe econ-data-api --region=$REGION --format='value(status.url)'`.)

If you'd rather validate the Dockerfiles by hand before trusting CI, the same
`docker build` / `gcloud run deploy` commands from the workflow file can be
run locally — substitute your own values for the `${VAR}` references.

---

## 9. Known gap: this deploys the API publicly, unauthenticated

`--allow-unauthenticated` is intentional (there's no login system on this
project — it's a stateless data API), but it means anyone with the URL can
call it. See "Known gaps to close before real traffic" below. The tool
endpoints have no rate limiting or API-key check; FRED API keys aren't
billed per call, so the exposure there is "someone exhausts your daily FRED
quota," not a surprise bill.

The agent endpoints **are** billed per call (each question is several
Gemini requests), so they carry their own guardrails: a per-client token
bucket (`AGENT_RATE_LIMIT_PER_MIN`, default 6/min with a burst of 3), at
most `AGENT_MAX_CONCURRENT_RUNS` (default 2) runs per instance, and with
`--max-instances=3` that bounds the worst case to 6 concurrent runs. Set a
[budget alert](https://cloud.google.com/billing/docs/how-to/budgets) on the
project before sharing the URL widely.

---

## 10. Cost control and cleanup

- Both services deploy with `--min-instances=0` (scale to zero) — no charge
  while idle, at the cost of a cold start (a few seconds) on the first
  request after idle.
- To tear everything down and stop all billing:

```bash
gcloud run services delete econ-data-api --region="$REGION" --quiet
gcloud run services delete econ-data-frontend --region="$REGION" --quiet
gcloud artifacts repositories delete econ-data --location="$REGION" --quiet
gcloud secrets delete fred-api-key --quiet
gcloud secrets delete gemini-api-key --quiet   # only if you created it
```

---

## Troubleshooting

- **Frontend loads but every request fails with a CORS error in the browser
  console** — the backend's `CORS_ALLOWED_ORIGINS` doesn't include the
  frontend's actual URL yet. Re-run the "Point backend CORS at the deployed
  frontend" step (re-run the workflow), or set it by hand:
  `gcloud run services update econ-data-api --region=$REGION --update-env-vars=CORS_ALLOWED_ORIGINS=https://your-frontend-url`.
- **Backend returns 502 / `fred_api_error` on every request** — check
  `gcloud run services logs read econ-data-api --region=$REGION`. Most
  likely the secret isn't reaching the container: confirm the runtime
  service account has `roles/secretmanager.secretAccessor` on `fred-api-key`
  (step 5) and that the deploy command's `--set-secrets` value matches the
  secret name in the `FRED_API_KEY_SECRET` variable (step 7).
- **`gcloud auth configure-docker` / image push fails with permission
  denied** — the deployer service account is missing
  `roles/artifactregistry.writer`; re-run the binding command from step 5.
- **Cloud Run deploy fails with "Container failed to start and listen on the
  port defined by the PORT environment variable"** — check
  `gcloud run services logs read econ-data-api --region=$REGION` for the
  actual startup error. `backend/Dockerfile`'s `CMD` already reads `$PORT`
  (`uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}`), so this
  usually means the app raised on import — a bad `FRED_API_KEY` env var
  format, or a dependency missing from `backend/requirements.txt`.
- **`/agent/stream` ends with an `error` event (`agent_error`) on every
  question** — the log line `agent run failed` carries the exception type.
  `PermissionDenied` means the runtime service account is missing
  `roles/aiplatform.user` (step 5) or `aiplatform.googleapis.com` isn't
  enabled (step 2); `NotFound` on the model usually means `GEMINI_MODEL`
  isn't available in `VERTEX_LOCATION` — `global` has the widest coverage.
  `GET /health` shows `agent_backend` and `agent_model_configured` for a
  quick check.
- **The activity timeline appears all at once instead of step by step** —
  something between the browser and the API is buffering the response. The
  API sends `Cache-Control: no-cache` and `X-Accel-Buffering: no`; Cloud Run
  itself streams, but a CDN or proxy in front of it may need buffering
  disabled for `text/event-stream`.
- **GitHub Actions auth step fails with "audience" or "no matching
  provider" errors** — the `--attribute-condition` in step 6 pins the
  provider to exactly `rithvikkatpelly/Econ_FullStack`; a fork or a
  renamed repo needs that value (and the workload-identity-pools binding's
  `member=principalSet://...attribute.repository/...`) updated to match.

---

## Backend — environment variables

Set on the Cloud Run service (`--set-env-vars` / `--set-secrets` in
`.github/workflows/deploy.yml`, or `gcloud run services update` by hand).
Never commit real values — `.env` stays local-only and is excluded from the
Docker build context (`.dockerignore`).

| Var | Needed | Notes |
|---|---|---|
| `FRED_API_KEY` | for live data | Via Secret Manager (`--set-secrets`), not a plain env var. Without it the API serves the synthetic fixture — fine for a demo, misleading for a real deployment. |
| `FRED_OFFLINE` | set to `0` in prod | Makes a missing key fail loudly (fixture won't silently serve fake data) rather than degrading quietly. The workflow sets this. |
| `CORS_ALLOWED_ORIGINS` | **yes** | Must list the deployed frontend's exact origin. The workflow sets this automatically after the frontend deploys (step 8.4); comma-separated, no trailing slash, if you ever add a second allowed origin by hand. |
| `AGENT_BACKEND` | set by the workflow | `gemini` in production. `stub` (the default) runs the deterministic offline planner — no model calls. |
| `GEMINI_MODEL` | optional | Defaults to `gemini-3.8-flash`. |
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | set by the workflow | Route Gemini calls through Vertex AI as the runtime service account. Omitted when `GEMINI_API_KEY` is used instead. |
| `GEMINI_API_KEY` | only without Vertex AI | Via Secret Manager, like `FRED_API_KEY`. |
| `AGENT_RATE_LIMIT_PER_MIN`, `AGENT_RATE_LIMIT_BURST`, `AGENT_MAX_CONCURRENT_RUNS`, `AGENT_MAX_QUERY_CHARS` | optional | Agent-endpoint spend guards; defaults 6, 3, 2, 500. Rate-limit buckets and the run cap are per instance. |
| `SESSION_TOKEN_BUDGET` | optional | Defaults to 50000. This is a **per-process** budget today (module-level singleton in `cost_tracker.py`), not per-user — see "Known gaps" below. |
| `AUDIT_LOG_PATH` | already set | `backend/Dockerfile` sets this to `/tmp/audit.log`. The container runs as a non-root `appuser` (see the Dockerfile), which can't create a new file under `/app/backend` (owned by root); `/tmp` is always writable. Audit logging is designed to fail open on a write error (`audit_log.py`), so this isn't fatal either way — it just silently stops logging if pointed somewhere unwritable. Cloud Run's filesystem (including `/tmp`) is ephemeral regardless — it doesn't survive a restart or scale-to-zero, so treat this as debug-tail-the-logs, not a durable audit trail, until it's shipped somewhere external. |

`CACHE_PATH` is left at `:memory:` — the FRED cache is per-process and resets
on every new revision. That's acceptable (FRED data for a closed window is
immutable and re-fetching is cheap); only set it to a mounted file if you
want the cache to survive restarts.

---

## Known gaps to close before real traffic

- **No auth / no rate limiting on the HTTP tool endpoints.** `/agent/*` is
  rate-limited (above), but `/search`, `/observations`, `/compare` and
  `/metadata` are not; `src/rate_limit.py` otherwise guards only the MCP
  boundary. A public
  deployment needs at least an API key check or a reverse-proxy rate limit,
  or it's an open proxy to your FRED key (see §9 above for how bad that
  actually is). The MCP server's `rate_limit.guard` could be lifted into a
  FastAPI dependency — same token-bucket, keyed on client IP or an API key.
- **The landing page spends from that shared budget.** The hero carousel
  fetches seven series (~1.7k of the default 50k tokens) on a browser's first
  visit, then caches them in `localStorage` for six hours. That's fine for a
  demo, but with many first-time visitors it will drain the process-wide
  allowance and later requests get a 429 (the UI shows "The demo's data
  allowance is used up" and the rest of the page keeps working). Per-client
  budgets, or a server-side snapshot endpoint that doesn't bill the session,
  are the real fix.
- **The token budget is process-global.** One busy client can exhaust it for
  everyone until the process restarts. For multi-user hosting, key the
  budget by session/API-key instead of the module-level singleton (there's a
  `# TODO`-style note to this effect already in `cost_tracker.py`).
- **`FRED_API_KEY` in error text.** `audit_log.py` already redacts it; spot-
  check that a forced upstream error (e.g. an invalid key) doesn't echo the
  key in a 502 body before going public.
- **Agent guardrails are per instance.** The rate-limit buckets and the
  concurrent-run cap live in process memory, so each Cloud Run instance
  enforces its own. `--max-instances` is what bounds the total; a shared
  store (Memorystore/Redis) would make the limits global.
- **Observability.** Structured request logs go to Cloud Logging; there's no
  per-agent tracing yet. Exporting the `Trace` events as OpenTelemetry spans
  (Cloud Trace) is the natural next step — the event hook already exists.
- **Frontend bundle size.** ~540 KB, mostly `recharts`; consider
  `build.rollupOptions.output.manualChunks` if it matters. Cosmetic, not
  blocking.
