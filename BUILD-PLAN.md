# Trading Desk — Build Plan

Turning the `mockups/` command-center into a live, unified **Trading Desk** on Railway.

**Decided direction** (from review):
- **Aggregator, not a merge.** Each existing tool keeps running as its own Railway
  service. Trading Desk is a new, thin Flask app that *reads each tool's JSON API* and
  composes six pages. It owns no market-data pollers of its own.
- **Six pages off one `base.html`:** Overview · Gameplan · Intraday · Flow · Structure · EOD
  (the `mockups/` files become these templates).
- **Hybrid reads:** deterministic numbers (levels, scores, flow) as the backbone, a short
  AI summary on top for Gameplan / Intraday / EOD.
- **Trading-Agent is the narrative + learning brain.** It already synthesizes the SPY/QQQ/IWM
  morning gameplan (bias · conviction · bull/bear triggers · key levels · risk flags · overall
  risk rating · watch list) from market structure + macro + overnight globals + full options
  positioning, **and** grades each day's bias at EOD into a self-calibrating performance record.
  The desk **reuses it as the synthesis engine** rather than building a parallel one — see §6
  and the cohesion notes below.
- **One Massive key** administered once as a Railway **project shared variable**; the
  aggregator itself needs no Massive key — it reads the tools that already hold it.

**Cohesion decisions (from the Trading-Agent review):**
- **One gameplan, fused per ticker.** Each SPY/QQQ/IWM card = Trading-Agent's AI bias/conviction/
  cases/risk-flags sitting on top of 0DTE's mechanical level ladder. Narrative (why) + precision
  (where), cross-checked — Trading-Agent already consumes the same GEX/walls, so they agree.
- **One risk rating.** Trading-Agent's `overall_risk_rating` is *the* session risk read across the
  desk (Overview context, Gameplan header). No second rating is computed.
- **EOD = two halves.** ① *Today, graded* (Trading-Agent bias-correct, day %, Brier calibration,
  performance snapshot) → ② *Setup for tomorrow* (flow EOD lean + carried structure recs + macro).
- **Track records stay in their tools for now.** Trading-Agent calibration shows on EOD; the
  Structure win-rate and 0DTE touch records stay inside those tools, deep-linked (revisit a
  combined scorecard later).
- **One AI engine.** The morning gameplan and the EOD grade are Trading-Agent's existing Claude
  synthesis; the desk does not run its own. (At most a tiny optional intraday micro-summary — the
  Intraday page can otherwise be numbers-only tracking of the AM gameplan.)

---

## 1. Data-source map (what feeds each page)

Good news from the upstream audit: almost everything already exists as JSON.

| Page / element | Upstream service | Endpoint | Status |
|---|---|---|---|
| Overview — morning brief (structure, globals, macro, watch, risk) | Trading-Agent | `GET /api/brief.json` | **ADD (small)** |
| Gameplan — AI bias/conviction/cases/risk-flags | Trading-Agent | `GET /api/brief.json` | **ADD (small)** |
| Overview context (spot, regime) | 0DTE-Assistant | `GET /api/state` | **exists** |
| Gameplan (walls, flip, EM, observed%) | 0DTE-Assistant | `GET /api/state` | **exists** |
| Intraday (touches, position-vs-levels) | 0DTE-Assistant | `GET /api/state` | **exists** |
| Intraday / Flow skew by ticker | Options-Flow-Scanner | `GET /api/index-etfs` | **exists** |
| Flow — fresh waves | Options-Flow-Scanner | `GET /api/index-etfs/waves` | **exists** |
| Flow — EOD positioning | Options-Flow-Scanner | `GET /api/index-etfs/eod` | **exists** |
| Structure — rotation column | Options-Flow-Scanner | `GET /api/rotation` | **exists** |
| Structure — scored recs table | Structure-Identifier | `GET /api/recs.json` | **ADD (small)** |
| Structure — quant column | Stock-Analysis | `GET /api/analyze?ticker=X&ai=0` | **exists** |
| EOD — today graded + performance | Trading-Agent | `GET /api/brief.json` (outcomes) · `GET /api/performance.json` | **ADD (small)** |
| Structure/EOD — learning context (optional) | Structure-Identifier | `GET /export/opportunities.json` | exists |

**Upstream code changes are all small, read-only serializers over data that already exists**
(§8): `/api/brief.json` + `/api/performance.json` on Trading-Agent, and `/api/recs.json` on
Structure-Identifier. The Streamlit Sector-Scanner is **not** in the critical path (OFS
`/api/rotation` covers the rotation read); its price-based RRG can be folded in later (§8).

---

## 2. App structure

Mirror the Options-Flow-Scanner / 0DTE layout (Flask + Jinja templates + one stylesheet).

```
Trading-Site/
├── app.py                  # Flask app factory, routes, scheduler bootstrap
├── desk/
│   ├── __init__.py
│   ├── config.py           # env vars, service URLs/tokens, ET tz
│   ├── sources/            # one adapter per upstream service
│   │   ├── agent.py        #   /api/brief.json, /api/performance.json → BriefView
│   │   ├── zerodte.py      #   /api/state   → LevelsView
│   │   ├── flow.py         #   /api/index-etfs[/waves|/eod], /api/rotation
│   │   ├── structure.py    #   /api/recs.json, /export
│   │   └── stock.py        #   /api/analyze?ai=0
│   ├── models.py           # normalized view-model dataclasses (§4)
│   ├── fetch.py            # parallel fetch + per-source TTL cache + fail-soft
│   ├── synth.py            # deterministic backbone + Claude summary (§6)
│   ├── store.py            # SQLite snapshot persistence for the daily reads
│   └── schedule.py         # APScheduler jobs (gameplan / intraday / eod)
├── templates/
│   ├── base.html           # header, nav, context bar, footer  (from mockups/styles.css)
│   ├── overview.html  gameplan.html  intraday.html
│   ├── flow.html  structure.html  eod.html
│   ├── _context_bar.html  _ai_summary.html  _levels_card.html   # shared partials
│   └── error.html / degraded.html
├── static/styles.css       # = mockups/styles.css (single source of truth)
├── requirements.txt        # flask, gunicorn, requests, apscheduler, anthropic, tzdata
├── Procfile / railway.json  # gunicorn app:app --workers 1
└── mockups/                # keep as the visual reference
```

**Routes:** `/`, `/gameplan`, `/intraday`, `/flow`, `/structure`, `/eod`, `/healthz`,
and `/api/snapshot.json` (the composed view-model, handy for the smart-refresh poll and
for debugging). `--workers 1` because the scheduler + cache live in process (same
constraint the sibling apps document).

---

## 3. Normalized view-model (decouple templates from upstream shapes)

Every adapter maps its upstream JSON into small dataclasses in `desk/models.py`; templates
only ever see these. This is the same "schema map at the boundary" discipline the 0DTE app
uses — when an upstream response shape shifts, one adapter changes, not the templates.

```python
@dataclass
class Level:      name: str; price: float; dist_pct: float; kind: str   # callwall/putwall/zero/vol/em
@dataclass
class TickerLevels: symbol: str; spot: float; chg_pct: float; regime: str
                    levels: list[Level]; observed_pct: float; flow_age: str; degraded: bool
@dataclass
class Wave:       symbol: str; side: str; call_n: int; put_n: int; premium: float; lead: str
@dataclass
class Rec:        symbol: str; price: float; direction: str; zone: str; dist_pct: float
                  components: dict; rotation: str; quant: int|None; live: str; score: float
# Trading-Agent (the narrative brain): one AI read per ticker, fused onto TickerLevels in the view.
@dataclass
class TickerBias: symbol: str; bias: str; conviction: float; bull: str; bear: str; risk_flags: list[str]
@dataclass
class Brief:     structure: str; macro: str; gamma: str; risk_rating: str; watch: list[str]
                 globals: list[dict]; calendar: list[dict]; biases: dict[str, TickerBias]
@dataclass
class Grade:     symbol: str; bias: str; conviction: float; day_pct: float; correct: bool|None; note: str
@dataclass
class DeskSnapshot: asof: datetime; session_phase: str; risk_read: str           # = brief.risk_rating
                    brief: Brief; tickers: list[TickerLevels]; waves: list[Wave]
                    recs: list[Rec]; grades: list[Grade]; performance: dict        # Brier, accuracy buckets
                    summaries: dict[str,str]   # intraday micro-summary (gameplan/eod come from the brief)
```

---

## 4. Fetch strategy — fast, cheap, fail-soft

- **Never fetch upstream in the page request path on every view.** A background refresh
  (scheduler, §6) writes the composed `DeskSnapshot` to SQLite + an in-memory cache;
  pages render the latest snapshot. Same decoupled read pattern as Structure-Identifier.
- **Parallel + timeouts:** adapters run under `concurrent.futures` with a short per-call
  timeout (~4s). One slow/down service never blocks the others.
- **Fail-soft per section:** a failed source renders its card **degraded** (named source +
  last-good age), exactly like the source apps — the page always renders. Last-good values
  are held from the cache.
- **Short TTL on the live-ish bits** (intraday skew/position): 60s cache so an open tab's
  smart-refresh is cheap and upstream throttles (Massive ~5rpm) are never touched directly —
  the aggregator reads each tool's *already-computed* state, not Massive.

---

## 5. Env vars & Railway wiring

Aggregator service variables:

```
AGENT_URL=https://morning-brief.up.railway.app     AGENT_TOKEN=...      # if APP_TOKEN set
ZERO_DTE_URL=https://zero-dte.up.railway.app       ZERO_DTE_TOKEN=...   # X-App-Token
FLOW_URL=https://options-flow.up.railway.app
STRUCTURE_URL=https://structure.up.railway.app     STRUCTURE_TOKEN=...  # if EXPORT_TOKEN set
STOCK_URL=https://stock-deepdive.up.railway.app
SITE_NAME=Trading Desk
APP_TOKEN=...                                       # gate the public URL (like 0DTE)
# ANTHROPIC_API_KEY — only if we add the optional intraday micro-summary (§6).
#   The morning gameplan + EOD grade are synthesized by Trading-Agent, not the desk.
```

`MASSIVE_API_KEY` and `ANTHROPIC_API_KEY` stay **project shared variables** referenced by the
services that need them (Massive: 0DTE, Flow, Structure-live; Anthropic: Trading-Agent,
Stock-Analysis). The aggregator references **neither** unless the optional intraday summary is added.
Attach a small Railway **volume** only if we want EOD/gameplan snapshot history to survive
redeploys (§6) — otherwise the daily reads just rebuild on next schedule.

---

## 6. Synthesis layer (the check-ins)

**The heavy AI synthesis already exists in Trading-Agent** (morning gameplan) and runs its own
EOD grading + performance loop. The desk does **not** re-synthesize those — it reads them. The
desk's scheduler is mostly a *compose + cache* job, plus an optional tiny intraday summary.

`APScheduler`, America/New_York, NYSE-holiday aware (lift the holiday skip from the sibling apps):

| Job | Time (ET) | What it does | AI? |
|---|---|---|---|
| **Gameplan** | 09:35 (after Trading-Agent brief + 0DTE 09:30 freeze) | pull `/api/brief.json` + `/api/state`, fuse per ticker, cache snapshot | none (reads Trading-Agent's) |
| **Intraday** | every 15 min, 10:00–15:45 | pull 0DTE touches + flow, track vs. AM gameplan, cache | optional 1-line micro-summary |
| **EOD** | 16:15 (after Trading-Agent autogrades + final T-15 flow) | pull graded outcomes + performance + flow EOD lean, cache | none (reads Trading-Agent's) |

- **Hybrid rule (unchanged):** numbers are computed, never generated. Any AI text is fed *only*
  already-computed numbers and cannot invent a level. With Trading-Agent as the engine, most of
  the desk's AI need disappears.
- On-demand "refresh" button re-runs the intraday compose out of band (like Structure's `/refresh`).
- **Dependency ordering:** the desk's gameplan job must run *after* Trading-Agent's brief exists
  for the day; it degrades gracefully (shows levels-only) if the brief is late or failed.

---

## 7. Build phases (ship incrementally, validate each)

- **Phase 0 — Shell (fastest win).** Port the six `mockups/` pages into `base.html` +
  templates, serve the current mock JSON from `desk/sample_data.py`. Deploy to Railway,
  confirm it renders and navigates. *Now the mock IS the app, just not wired.*
- **Phase 1 — Brief + levels live.** Add Trading-Agent's `/api/brief.json` (§8), wire
  `agent.py` + `zerodte.py` → Overview morning brief, the fused Gameplan, Intraday, and the
  unified risk rating. Fail-soft + degraded. This is the daytrading core + the narrative spine.
- **Phase 2 — Flow live.** Wire `flow.py` → Flow page + intraday skew from the three
  `/api/index-etfs*` endpoints. (All exist — adapter + mapping only.)
- **Phase 3 — Structure live.** Add Structure's `/api/recs.json` (§8), wire `structure.py` +
  `/api/rotation` + Stock-Analysis quant → the recs table.
- **Phase 4 — EOD + schedule.** Add Trading-Agent's `/api/performance.json` (§8); wire the EOD
  two-halves (graded + performance, then setup-for-tomorrow). Scheduler + SQLite snapshot cache;
  optional intraday micro-summary.
- **Phase 5 — Polish.** `APP_TOKEN` gate, `/healthz` with per-source status, optional
  snapshot history/volume, optional RRG module (§8).

---

## 8. Upstream changes

**Required (all small, read-only serializers over existing data):**
- **Trading-Agent — `GET /api/brief.json`.** Return today's session row from `journal.db`:
  the `gameplan` JSON (per-ticker bias/conviction/cases/levels/risk-flags + market_structure,
  macro_context, gamma_summary, overall_risk_rating, watch_list), `market_data.global_markets`,
  the macro calendar, and `market_session`. All already stored; this is a thin serializer beside
  the existing `/` (HTML) route. Gate with its `APP_TOKEN`.
- **Trading-Agent — `GET /api/performance.json`.** Wrap the existing `journal.get_performance_stats()`
  (accuracy by ticker/bias/gamma/risk, Brier, baselines) + the day's `outcomes` rows. The function
  already exists — this just serves it.
- **Structure-Identifier — `GET /api/recs.json`.** Return the current persisted ranked set
  (symbol, price, direction, thin-zone kind + distance, the `{zone,flag,gex,rvol}` component
  flags, combined score, and today's `live` peak signal). The data already exists in
  `src/store.py`; thin serializer beside the existing `/` route. Token-gate with `EXPORT_TOKEN`.

**Optional (later):**
- **Price-based RRG rotation.** If the OFS flow-based `/api/rotation` isn't the rotation lens
  we want on the recs table, lift the RRG quadrant math out of `Sector-Scanner/app.py` into a
  small shared `desk/rotation.py` (yfinance-based, ~30 lines) rather than running Streamlit as
  a service. Decide after seeing Phase 3 with the flow-based rotation.

---

## 9. Testing

- `tests/test_sources_*.py` — each adapter against a captured upstream JSON fixture →
  asserts the view-model mapping (the brittle part).
- `tests/test_fetch.py` — fail-soft: a source raising/timing out yields a degraded section,
  never a 500; last-good is held.
- `tests/test_synth.py` — the deterministic backbone is computed (no network, no Claude);
  summary step is mocked.
- Template smoke test — each route renders with a sample snapshot and with an all-degraded
  snapshot.

---

## 10. Open decisions (not blockers)

1. **Intraday cadence** — 15 min matches the T-15 Massive clock; tighter just re-renders the
   same delayed data. Confirm 15 min is right.
2. **Rotation lens** — flow-based (OFS, free, already there) vs price-based RRG
   (Sector-Scanner logic). Default: flow-based for v1.
3. **Snapshot history** — keep a replay/history of daily reads (needs a Railway volume) or
   just serve the latest? Default: latest only to start.
4. **Auth** — single `APP_TOKEN` shared across the desk, or rely on each tool's own gate?
   Default: gate the desk too.
