# Trading Terminal — Unified Hub

A standalone landing page ("front door") for the options-trading toolset. It presents
each tool as a card styled to match the **Morning Brief** (`Trading-Agent`) dashboard
— dark Bloomberg-terminal look, JetBrains Mono — and links out to each tool's own
deployed URL.

> **This hub is fully self-contained.** It does **not** import, run, or modify any of
> the sibling projects. Each existing app keeps working exactly as before; this page
> only links to them. That isolation is intentional (standalone test for now).

## Tools linked from the hub

| # | Tool | Project folder | Env var | Status |
|---|------|----------------|---------|--------|
| 01 | Morning Brief | `Trading-Agent` | `MORNING_BRIEF_URL` | Live |
| 02 | Stock Deep Dive | `Stock-Analysis` | `STOCK_DEEPDIVE_URL` | Live |
| 03 | SPY Power Hour | `SPY-Power-Hour` | `SPY_POWER_HOUR_URL` | Live |
| 04 | Thin-Zone Screener | `Structure-Identifier` | `THIN_ZONE_URL` | Live |
| 05 | Options Flow Scanner | `Options-Flow-Scanner` | `OPTIONS_FLOW_URL` | Coming soon (placeholder card, no link) |

Each live card links to the URL from its env var. If the var is unset, the card
renders unlinked with a `Set <VAR>` hint — nothing breaks, you just fill it in later.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # optional — fill in URLs, or leave blank
python app.py               # serves on http://localhost:8000  (override with PORT)
```

Open <http://localhost:8000>. With no `.env`, all four live cards show a `Set <VAR>`
hint and the Options Flow card shows as "Soon" — the page still renders fully.

## Configure the links

Set the deployed URL for each tool via environment variable (in `.env` locally, or in
the Railway service's variables):

```
MORNING_BRIEF_URL=https://morning-brief.up.railway.app
STOCK_DEEPDIVE_URL=https://stock-deep-dive.up.railway.app
SPY_POWER_HOUR_URL=https://spy-power-hour.up.railway.app
THIN_ZONE_URL=https://thin-zone.up.railway.app
```

`SITE_NAME` overrides the header/title text (default: `Trading Terminal`).

## Deploy to Railway

Same pattern as the sibling projects — this is its own service:

- **Build:** Nixpacks (auto-detected from `requirements.txt`)
- **Start:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2` (see `Procfile` / `railway.json`)
- Set the `*_URL` variables in the service so the cards resolve.

`GET /healthz` returns `{"status": "ok"}` for health checks.

## Shared API keys across sections (Massive, etc.)

Each tool is deployed as its own service and reads its own environment, so an API
key like `MASSIVE_API_KEY` (Massive.com options data) would otherwise be pasted into
every service that needs it. Three sections call Massive:

| Section | Service | Needs `MASSIVE_API_KEY` |
|---------|---------|--------------------------|
| Morning Brief (`Trading-Agent`) | its own | Yes |
| SPY Power Hour (`SPY-Power-Hour`) | its own | Yes |
| Thin-Zone Screener (`Structure-Identifier`) | its own | Yes |
| Stock Deep Dive (`Stock-Analysis`) | its own | No (`ANTHROPIC_API_KEY` only) |
| Options Flow Scanner (`Options-Flow-Scanner`) | its own | No (free yfinance) |
| **This hub (`Trading-Site`)** | this service | **No key — links only** |

To administer **one key for the whole site** instead of one per service, use
**Railway project-level shared variables**:

1. In the Railway **project** (not a single service) → **Variables** → add a shared
   variable, e.g. `MASSIVE_API_KEY = <your key>`.
2. In each service that needs it, reference the shared variable
   (`${{shared.MASSIVE_API_KEY}}`), or enable "Add to all services".
3. Rotating the key is then a single edit at the project level; every section picks
   up the new value on redeploy.

The same approach works for any cross-tool key (e.g. `ANTHROPIC_API_KEY`). This keeps
the existing apps untouched — they still just read `os.environ["MASSIVE_API_KEY"]`;
Railway simply supplies the one shared value to each.

> The hub itself needs none of these keys — it only links out. It is listed above
> for completeness.

## Roadmap / next steps

- **Options Flow Scanner** — finish the underlying project, then flip its card from
  `soon` to `live` and add `OPTIONS_FLOW_URL` (change is a one-line status edit in
  `PROJECTS` in `app.py`).
- **True in-process unification** (optional, later) — mounting the tools at sub-paths
  (`/brief`, `/deep-dive`, …) in one process is possible via WSGI `DispatcherMiddleware`,
  but requires small opt-in guards in a couple of the sibling apps (e.g. SPY-Power-Hour
  runs a scheduler + backfill at import time). Deferred to preserve the existing apps
  untouched.
- **Synthesized cross-tool data** — a future shared data layer could surface highlights
  from each tool on the hub itself (e.g. today's risk rating, top thin zones).
```
