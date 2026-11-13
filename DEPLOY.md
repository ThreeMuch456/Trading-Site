# Railway Deployment — TODO / runbook

The desk is an **aggregator**: it reads the other tools' JSON APIs. So hosting means
deploying each upstream service **and** the desk, wiring the desk to their URLs. All
six are separate Railway services in one Railway **project** (shared variables live at
the project level).

## 0. Prerequisites
- [ ] Feature branches pushed to GitHub (`github.com/ThreeMuch456`) — merge each into its
      repo's default branch (or point Railway at the branch):
  - `Trading-Site` → `phase-0-shell` (Phases 0–8)
  - `Trading-Agent` → `add-desk-json-endpoints` (`/api/brief.json`, `/api/performance.json`)
  - `Structure-Identifier` → `add-recs-json-endpoint` (`/api/recs.json`, `/api/learning.json`)
  - `0DTE-Assistant` → `add-touch-stats-endpoint` (`/api/touch-stats.json`)
- [ ] Massive API key; Anthropic API key.

## 1. Project-level shared variables (set once, reference per service)
- [ ] `MASSIVE_API_KEY` — referenced by 0DTE, Options-Flow, Structure (`${{shared.MASSIVE_API_KEY}}`)
- [ ] `ANTHROPIC_API_KEY` — referenced by Trading-Agent, Stock-Analysis
- [ ] Decide a shared secret for cross-service gating (see tokens below).

## 2. Deploy the upstream services (each its own Railway service)
Deploy these **first** — the desk needs their public URLs.

| Service | Repo | Needs | Volume | Notes |
|---|---|---|---|---|
| Morning Brief | `Trading-Agent` | `ANTHROPIC_API_KEY`, `MASSIVE_API_KEY` | `/data` (journal.db) | `RUN_TOKEN` gates `/run`; brief JSON is public |
| 0DTE Levels | `0DTE-Assistant` | `MASSIVE_API_KEY` | `/data` (0dte.sqlite3) | **1 worker**, frozen 09:30 baseline; `APP_TOKEN` → desk's `ZERO_DTE_TOKEN` |
| Options Flow | `Options-Flow-Scanner` | `MASSIVE_API_KEY` | optional | serves `/api/index-etfs*`, `/api/rotation`, `/api/sweeps`, `/api/ticker/<sym>` |
| Structure | `Structure-Identifier` | `MASSIVE_API_KEY` (for `/live`) | **`/app/data`** (opportunities.db — irreplaceable) | `EXPORT_TOKEN` → desk's `STRUCTURE_TOKEN`; 1 worker |
| Stock Deep-Dive | `Stock-Analysis` | `ANTHROPIC_API_KEY` | none (cache only) | `/api/analyze?ai=0` is data-only |

- [ ] Each: generate a public domain, confirm `GET /healthz` (or `/health`) returns 200.
- [ ] Record each service's public URL for step 3.

## 3. Deploy the desk (`Trading-Site`)
- [ ] New service from `Trading-Site`. Build: Nixpacks; start `gunicorn app:app` (see `railway.json`/`Procfile`).
- [ ] **Keep it at 1 worker** when `DESK_SCHEDULER=1` (scheduler + cache live in-process).
- [ ] Set service variables (see `.env.example`):
  - `AGENT_URL`, `ZERO_DTE_URL` + `ZERO_DTE_TOKEN`, `FLOW_URL`, `STRUCTURE_URL` + `STRUCTURE_TOKEN`, `STOCK_URL`
  - `APP_TOKEN` — gate the public desk (then open it at `?token=…`, cookie persists)
  - `DESK_SCHEDULER=1`, `DESK_TZ=America/New_York`
  - `DESK_DB_PATH=/data/desk.sqlite3`
- [ ] **Attach a volume at `/data`** so the desk journal (learning store) survives redeploys.
- [ ] The desk needs **no** `MASSIVE_API_KEY`; it only needs `ANTHROPIC_API_KEY` if the optional
      intraday micro-summary is ever enabled (not currently used).

## 4. Verify end-to-end
- [ ] Desk `/healthz` → `{"live": true, "sources_degraded": {}}` (empty = all sources reachable).
- [ ] Each page loads with live data; a down source shows the degraded banner, not a 500.
- [ ] After a full session: EOD page shows graded outcomes + the Track Record accrues (desk journal grows).

## 5. Post-deploy / nice-to-have
- [ ] Set `RETAIN_*` / volume sizes per each tool's README (0DTE prints are bulky).
- [ ] Confirm the daily schedules fire (desk 09:35/15-min/16:15; Structure 17:00 + 09:15 re-price; Agent AM).
- [ ] Consider a status page or uptime check on each `/healthz`.

## Known gaps (code TODO, not deploy)
- Overview snapshot cards are partly live (risk, flow, structure counts) but still mostly sample summaries.
- Rotation uses OFS flow-based `/api/rotation`; a price-based RRG (lift from Sector-Scanner) is optional.
- Snapshot history/replay for the desk is not built (would use the `/data` volume).
