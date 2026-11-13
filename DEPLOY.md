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

## Variables cheat-sheet (exact names)

**Two shared secrets you invent** and reuse across services:
`<ZERO_DTE_SECRET>` (0DTE `APP_TOKEN` = desk `ZERO_DTE_TOKEN`) ·
`<STRUCTURE_SECRET>` (Structure `EXPORT_TOKEN` = desk `STRUCTURE_TOKEN`).
Project shared: `MASSIVE_API_KEY`, `ANTHROPIC_API_KEY`.

| Service | Required | Volume | Notes |
|---|---|---|---|
| **Trading-Agent** | `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `ENABLE_SCHEDULER=1` | `/data` → `DB_PATH=/data/journal.db` | `MASSIVE_API_KEY` optional (yfinance fallback); `RUN_TOKEN` optional (gates `/run` only — brief JSON is public, so desk needs no AGENT_TOKEN) |
| **0DTE-Assistant** | `MASSIVE_API_KEY`, `APP_TOKEN=<ZERO_DTE_SECRET>` | `/data` → `DB_PATH=/data/0dte.sqlite3` | 1 worker (its railway.json pins it); gates every route |
| **Options-Flow-Scanner** | `MASSIVE_API_KEY`, `SECRET_KEY=<random>` | `/data` (or a Railway Postgres via `DATABASE_URL`) | endpoints are public → desk needs no FLOW_TOKEN; tune thresholds via its many `ETF_*`/`ROTATION_*` vars (all have defaults) |
| **Structure-Identifier** | `EXPORT_TOKEN=<STRUCTURE_SECRET>` | `/app/data` (irreplaceable learning DB) | `MASSIVE_API_KEY` only for the `/live` monitor; `RUN_SCHEDULER=true` default |
| **Stock-Analysis** | — | none | `ANTHROPIC_API_KEY` optional and NOT needed for the desk (it calls `?ai=0`) |
| **Trading-Site (desk)** | the 5 URLs + 2 tokens below | `/data` → `DESK_DB_PATH=/data/desk.sqlite3` | 1 worker; `APP_TOKEN` gates the desk itself |

**Desk service variables** (the 5 upstream URLs are each service's Railway public domain):
```
AGENT_URL=https://<trading-agent>.up.railway.app
ZERO_DTE_URL=https://<0dte>.up.railway.app     ZERO_DTE_TOKEN=<ZERO_DTE_SECRET>
FLOW_URL=https://<options-flow>.up.railway.app
STRUCTURE_URL=https://<structure>.up.railway.app   STRUCTURE_TOKEN=<STRUCTURE_SECRET>
STOCK_URL=https://<stock-analysis>.up.railway.app
APP_TOKEN=<desk-secret>            # then open the desk at ?token=<desk-secret>
DESK_SCHEDULER=1  DESK_TZ=America/New_York  DESK_DB_PATH=/data/desk.sqlite3
```
The desk references **no** `MASSIVE_API_KEY`/`ANTHROPIC_API_KEY`. In Railway you can write a URL as
`https://${{<ServiceName>.RAILWAY_PUBLIC_DOMAIN}}` instead of pasting the domain.

## Known gaps (code TODO, not deploy)
- Rotation uses OFS flow-based `/api/rotation`; a price-based RRG (lift from Sector-Scanner) is optional.
- Sector-Scanner (Streamlit) is intentionally off the critical path; not deployed unless its RRG is wanted.
- The desk EOD summary paragraph is deterministic; an optional AI intraday micro-summary is unused.

The BUILD-PLAN is otherwise fully realized: six live-wired pages, recursive learning in every page,
a desk-owned learning journal with session history, fail-soft fetch, opt-in scheduler, APP_TOKEN gate.
