# Econ Data frontend (`frontend/`)

React + Vite + TypeScript web app over the FastAPI backend (`backend/app`):
an "Ask the agent" chat that streams the multi-agent pipeline's progress as
a live activity timeline, a hero carousel of headline indicators, a tabbed explorer (chart / compare /
find / details), recharts line charts, CSV export. Dark theme with a lime
accent, responsive down to phone width, keyboard- and screen-reader-friendly.

```bash
cd frontend
npm install
cp .env.example .env      # VITE_API_BASE_URL, defaults to http://127.0.0.1:8000
npm run dev               # http://localhost:5173
```

The backend must be running (`cd backend && uvicorn app.main:app --reload`)
and its `CORS_ALLOWED_ORIGINS` must include the frontend's origin — the
default already lists `http://localhost:5173`.

```
src/
  api/client.ts     fetch wrapper + one function per endpoint
  api/types.ts      mirrors backend/app/schemas.py
  api/agent.ts      /agent/stream client: POST + fetch stream reader → typed events
  catalog.ts        featured series + example topics (display copy only)
  format.ts         units-aware formatting, change (pts vs %), frequency rules
  dates.ts          range presets (1Y/5Y/10Y/20Y) and helpers
  useSnapshots.ts   hero data: fetched once, cached 6h in localStorage
  explorer.ts       the "open the explorer with X" request type
  components/
    AskAgent                                                          question box, activity timeline, grounded answer
    Hero, Comparisons, Marquee, HowItWorks, CtaBanner, Nav, Footer   landing sections
    Explorer + Chart/Compare/Search/Details panels                    the tools
    SeriesChart, Sparkline, controls, common                          shared pieces
  App.tsx           page composition + API health/"demo data" banner
```

Design notes: series are never charted on a zero-based axis (CPI ~300 would
look flat); compared series are rebased to 100 by default; "Automatic"
frequency uses each series' native one because FRED can only *lower* a
frequency; the marquee pauses on hover and all motion respects
`prefers-reduced-motion`.
