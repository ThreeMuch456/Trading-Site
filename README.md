# Trading Desk — unified aggregator

A personal, unified front end for a SPY/QQQ/IWM daytrading workflow. It composes the
signals from six independently-deployed sibling tools into six cohesive pages, in a shared
dark terminal style (JetBrains Mono).

> **Status: Phase 0 (shell).** The site is real and navigable but still rendered from
> **sample data** (`desk/sample_data.py`). No live API wiring yet. See
> [BUILD-PLAN.md](BUILD-PLAN.md) for the full architecture and the remaining phases.

## Pages

| Route | Page | Composes |
|-------|------|----------|
| `/` | Overview | Morning Brief headline + per-section snapshots |
| `/gameplan` | Gameplan | AI bias/conviction **fused onto** the 0DTE mechanical level map |
| `/intraday` | Intraday | Level interactions + live flow skew + position-vs-levels |
| `/flow` | Flow | Short-dated index waves, session skew, EOD positioning |
| `/structure` | Structure | Scored thin-zone recs + rotation + quant + live-activation |
| `/eod` | EOD | ① Today graded + performance → ② Setup for tomorrow |
| `/healthz` | — | Health check |

Design decisions (aggregator vs. merge, the per-tool data sources, the Trading-Agent
synthesis reuse, the cohesion rules) live in [BUILD-PLAN.md](BUILD-PLAN.md).

## Architecture

- **Aggregator, not a merge.** Each sibling tool keeps its own Railway service; this app
  reads their JSON APIs and composes. It owns no market-data pollers.
- **Data-driven templates.** Every template renders from a `DeskSnapshot`-shaped dict
  (`desk/sample_data.py` today; live adapters in `desk/sources/*` from Phase 1). The
  templates never change when the data goes live.
- `templates/base.html` holds the shared chrome + nav; `static/styles.css` is the single
  stylesheet (same system as the sibling tools).

```
app.py                 # Flask routes + shared context (site name, nav, snapshot)
desk/sample_data.py    # Phase-0 mock snapshot (shape = the real view-model)
templates/             # base.html + one template per page
static/styles.css      # shared design system
mockups/               # original standalone HTML mockups (visual reference)
```

## Run locally

```bash
pip install -r requirements.txt
python app.py          # http://localhost:8000  (override with PORT)
```

## Deploy to Railway

Railway-ready (`Procfile` + `railway.json`, NIXPACKS). Build from `requirements.txt`,
start `gunicorn app:app`. `GET /healthz` backs the health check. Env: `SITE_NAME`
(optional). The per-tool service URLs/keys arrive in Phase 1 (see BUILD-PLAN.md §5).
