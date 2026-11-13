# Railway Deployment — Trading Desk (single unified service)

The desk is now **one deploy**. The five sibling tools' compute is vendored into the
`engines/` package and runs in-process; the desk calls Massive/Anthropic/FRED
directly. There are **no upstream service URLs** any more — one Railway service, one
volume, one scheduler.

> Migrating from the old six-service setup? Delete the five upstream services and
> their URL/token variables. Only this service remains. The standalone repos stay as
> source-of-truth but are no longer deployed.

## 0. Prerequisites
- [ ] `Trading-Site` `main` pushed to GitHub (`github.com/ThreeMuch456`).
- [ ] **Massive API key** (the one key that turns the desk live).
- [ ] **Anthropic API key** (morning brief). **FRED API key** (optional — macro calendar).

## 1. Create the service
- [ ] New Railway service from `Trading-Site`. Builder: **Nixpacks** (auto).
- [ ] Start command is pinned in `Procfile` / `railway.json`:
      `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1`.
- [ ] **Keep it at 1 worker.** The scheduler, the 60 s snapshot cache, and every
      engine's in-memory state (the frozen 09:30 0DTE baseline, the Flow test client,
      the loaded Structure result set) all live in-process. A second worker would run
      a second scheduler and a second set of engines against the same volume.
- [ ] Healthcheck path: `/healthz` (already set in `railway.json`).

## 2. Attach one volume
- [ ] Attach a Railway **volume** (any mount path, e.g. `/data`). That's all — the
      desk auto-detects `RAILWAY_VOLUME_MOUNT_PATH`, so **every** engine's state lands
      on it: `flow.db`, `zerodte.sqlite3`, `agent.sqlite3`, `opportunities.db`
      (Structure), the Structure result pickle + bar cache, and `desk.sqlite3` (the
      compose-layer journal). Without a volume these live in the image and vanish on
      every redeploy — losing the learning history that the whole recursive loop
      depends on.
- [ ] To override the location explicitly, set `DESK_DATA_DIR=/data` (it wins over the
      auto-detected path).

## 3. Set variables
See `.env.example` for the full annotated list. The minimum to go fully live:
```
MASSIVE_API_KEY=<key>              # turns every live engine on
ANTHROPIC_API_KEY=<key>            # morning brief (Gameplan/Overview AI, EOD grading)
FRED_API_KEY=<key>                 # optional — macro calendar in the brief
APP_TOKEN=<invent-a-secret>        # gate the public desk; then open it at ?token=<secret>
DESK_SCHEDULER=1                   # run the scheduler + ALL engine background jobs
DESK_TZ=America/New_York           # tz for the desk's own refresh crons
MASSIVE_RPM=<see §Massive budget>  # per-engine request budget (default 5)
```
Optional schedule overrides (defaults are sensible): `BRIEF_HOUR` / `BRIEF_TZ` /
`GRADE_HOUR` / `GRADE_MINUTE` (morning brief + grade); Structure's screen/reprice
times live in `engines/structure/config.yaml`.

### What each key turns on
| Key | Engines it activates | Without it |
|---|---|---|
| `MASSIVE_API_KEY` | 0DTE levels, Flow, Structure (GEX), stock quant | desk renders **sample** data |
| `ANTHROPIC_API_KEY` | Morning brief + EOD bias grading (agent) | Gameplan/Overview/EOD AI stay sample |
| `FRED_API_KEY` | macro calendar inside the brief | brief notes the calendar feed is degraded |
| (none) | — | fully navigable sample desk |

`stock` is enrichment-only (it feeds Structure's quant column); it never flips the
desk live on its own.

## 4. Scheduler — one process owns every cadence
With `DESK_SCHEDULER=1` a single `BackgroundScheduler` runs, in this order of wiring:

| Job(s) | When (tz) | What |
|---|---|---|
| desk `gameplan` / `intraday` / `eod` | 09:35 · 10–15 every 15 min · 16:15 ET | recompose + journal the snapshot off the request path |
| `flow-scan` (+ prime) | every `SCAN_INTERVAL_MINUTES`, market-hours gated | Flow scanner writes sweeps → `flow.db` |
| `zerodte-underlying` / `-options` / `-maintenance` (+ prime) | 15 s / 60 s, 09:00–16:20 ET | 0DTE pollers drive the engine loop; daily prune |
| `agent-brief` (+ :15/:30 retries) / `agent-autograde` (+ startup prime) | 05:00 `BRIEF_TZ` / 13:35 | generate the Claude brief; grade yesterday open→close |
| `structure-screen` / `structure-reprice` (+ startup prime) | 17:00 / 09:15 ET, mon–fri | EOD thin-zone screen + learning log; pre-open re-price |

**First boot:** the one-shot *prime* jobs fire immediately, so a fresh deploy fills
in within a minute or two (the brief generates if today's is missing; Structure runs
its first screen; 0DTE/Flow begin polling if inside market hours) rather than waiting
for the next cron. All heavy primes run off the request path.

Holiday/weekend handling is built in: each engine gates on the market calendar (the
0DTE poll window, the Structure NYSE-calendar guard, the brief's next-session preview
framing), so the crons can fire every weekday safely.

## 5. Massive budget (per-engine; the stagger that keeps it under the limit)
There is **no single shared Massive limiter** — each live consumer (0DTE pollers, the
Flow scanner, Structure's GEX, the stock enrichment) applies `MASSIVE_RPM` as its
*own* rolling-minute cap, and each already prioritises its critical calls (e.g. 0DTE
reserves budget for the chain snapshot over best-effort print fetches).

The cadences are **largely staggered**, so sustained overlap is limited:
- **Morning brief** 08:00 ET (pre-market) → before the 0DTE poll window opens.
- **0DTE pollers** 09:00–16:20 ET → the dominant, continuous consumer.
- **Flow scanner** periodic, market-hours → the one real overlap with 0DTE.
- **Structure screen + GEX** 17:00 ET (post-close) → after 0DTE's window.
- **Autograde** 16:35 ET → after 0DTE's window.
- **Stock quant** on-demand when a page composes (brief fan-out) → short bursts.

The only sustained concurrency is **0DTE + the Flow scanner during market hours**. So
set `MASSIVE_RPM` to roughly **half** your account's true per-minute limit to leave
headroom for that overlap (e.g. account allows ~10/min → `MASSIVE_RPM=5`). The
limiters queue rather than fail, so oversubscription shows up as polls arriving a
little late, not as errors; `429`s from the provider mean the account cap (not
`MASSIVE_RPM`) was exceeded — lower `MASSIVE_RPM`.

## 6. Verify end-to-end
- [ ] `GET /healthz` → `{"status":"ok","live":true,"sources_degraded":{}}` once the
      primes have run (empty `sources_degraded` = every live engine reachable).
- [ ] Each page renders live; a degraded engine shows its sample section + a banner,
      never a 500.
- [ ] After a full session: the EOD page shows graded outcomes and the Track Record
      accrues (0DTE touch calibration, Structure win-rates, brief Brier, desk journal
      all grow on the volume).
- [ ] Logs show `desk scheduler started` followed by each engine's
      `… jobs registered` line, and the prime jobs running once.

## 7. Local run
```
pip install -r requirements.txt
cp .env.example .env     # fill in MASSIVE_API_KEY / ANTHROPIC_API_KEY (+ DESK_SCHEDULER=1 to test crons)
python app.py            # http://localhost:8000
```
With no keys the desk runs in full sample mode (every page navigable). State is
written under `./data/` unless `DESK_DATA_DIR` is set.

## Notes / optional later
- Sector-Scanner (Streamlit RRG) stays off the critical path — OFS `/api/rotation`
  covers sector rotation; a price-based RRG is an optional future add.
- The EOD summary paragraph is deterministic; an AI intraday micro-summary is unused.
- A true cross-engine shared Massive limiter was considered and deliberately **not**
  built: flow (sync) and 0DTE (async) use different clients whose own critical-vs-
  best-effort prioritisation a shared FIFO would flatten, risking the 0DTE chain
  fetch. Per-engine budgets + the stagger above are the chosen control (see §5).
