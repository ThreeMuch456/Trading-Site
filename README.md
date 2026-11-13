# Trading Desk — unified aggregator

A personal, unified front end for a SPY / QQQ / IWM daytrading workflow. It composes the
signals from six independently-deployed sibling tools into one cohesive site — a dark
terminal UI (JetBrains Mono) with the morning gameplan, live levels, options flow,
scored structure setups, and an end-of-day review, each tool's own **learning loop**
surfaced alongside its signal.

> **Aggregator, not a merge.** Each tool keeps running as its own service; this app reads
> their JSON APIs and composes. It owns no market-data pollers. See
> [BUILD-PLAN.md](BUILD-PLAN.md) for the architecture and [DEPLOY.md](DEPLOY.md) for hosting.

## Pages

| Route | Page | Composes | Learning element |
|-------|------|----------|------------------|
| `/` | Overview | morning-brief headline + live per-section snapshot cards | risk rating · desk journal |
| `/gameplan` | Gameplan | AI bias/conviction **fused onto** the 0DTE level map (per ticker) | per-ticker bias accuracy |
| `/intraday` | Intraday | level touches + live flow skew + position-vs-levels | touch follow-through calibration |
| `/flow` | Flow · Index ETFs | SPY/QQQ/IWM 0DTE/1DTE waves, skew, EOD positioning | — |
| `/flow/all` | Flow · All Tickers | unusual activity across the universe (sweeps) | — |
| `/flow/ticker/<sym>` | Flow · per-ticker | call/put volume, term structure, strike clustering | — |
| `/structure` | Structure | scored thin-zone recs + rotation + quant + live-activation | setup win-rates (self-calibration) |
| `/eod` | EOD | ① today graded + performance → ② setup for tomorrow | synthesized Track Record + session history |
| `/healthz` | — | health + live/degraded status | — |

## Architecture

- **Data-driven templates.** Every template renders from one `DeskSnapshot`-shaped dict.
  `desk/sources/*` adapters map each upstream JSON into that shape; `desk/fetch.py` fetches
  them **in parallel**, **caches**, and **fails soft** — a down source degrades its section
  to sample data (with a banner) rather than breaking the page.
- **Recursive learning.** Each tool's own loop is surfaced (Trading-Agent bias calibration /
  Brier, Structure win-rates, 0DTE touch outcomes), and `desk/store.py` keeps the desk's own
  SQLite journal of composed signals + graded biases — see the EOD Track Record and history.
- **No key in the desk.** It reads tools that already hold `MASSIVE_API_KEY` / `ANTHROPIC_API_KEY`;
  the desk needs neither (optional `APP_TOKEN` gates the public URL).

```
app.py                 routes, APP_TOKEN gate, error handlers, scheduler bootstrap
desk/
  config.py            env-driven source URLs/tokens, timeout, cache TTL
  fetch.py             parallel fetch, cache, fail-soft, central Overview-card synthesis
  sources/             one adapter per upstream (agent, zerodte, flow, structure)
  store.py             desk learning journal (SQLite): signals, graded biases, history
  schedule.py          optional APScheduler (09:35 / 15-min / 16:15 ET)
  sample_data.py       the mock snapshot (shape = the live view-model)
templates/  static/    base.html + one template per page; shared stylesheet
tests/test_sources.py  adapter mapping, fail-soft, gate, card derivation (10 tests)
mockups/               original standalone HTML mockups (visual reference)
```

## Run locally

```bash
pip install -r requirements.txt
python app.py          # http://localhost:8000  (sample data with no source URLs set)
python tests/test_sources.py
```

With no upstream URLs configured the site runs in **sample mode** (renders the mock
snapshot). Set a source's URL in `.env` to make that section live — each is independent.

## Configure

See [.env.example](.env.example). Sources: `AGENT_URL`, `ZERO_DTE_URL` (+`ZERO_DTE_TOKEN`),
`FLOW_URL`, `STRUCTURE_URL` (+`STRUCTURE_TOKEN`), `STOCK_URL`. Desk: `APP_TOKEN`,
`DESK_SCHEDULER=1`, `DESK_TZ`, `DESK_DB_PATH`, `DESK_FETCH_TIMEOUT`, `DESK_CACHE_TTL`.

## Deploy

Railway-ready (`Procfile` / `railway.json`, Nixpacks, `gunicorn app:app`). Full runbook in
[DEPLOY.md](DEPLOY.md): deploy the five upstream tools first (with their volumes + the shared
API keys), then the desk wired to their URLs with a `/data` volume for the learning journal.
Keep the desk at **one worker** when the scheduler is enabled. `GET /healthz` backs the check.
