"""Structure-Identifier engine (vendored) — the scored thin-zone watchlist.

The standalone tool screens a universe for thin volume-profile zones + flags, scores
an agreement gate across zone/flag/GEX/rvol, and logs every setup to a learning DB
so win-rates by setup/horizon self-calibrate. The desk consumes two read endpoints:
  /api/recs.json      — the current ranked setups (direction, thin zone + distance,
                        agreement components, combined score, live activation).
  /api/learning.json  — win-rate / return by setup type and horizon (the recursive
                        self-calibration), plus outcome count.

Like the tool itself, the web is a *stateless reader*: the heavy screen runs as a
job (`src.job.run_job`) that persists a pickled result set + a learning DB
(`src.store` / `src.oplog`); the reads only load and serialize that. So this engine
vendors the whole screen pipeline (all of `src/` except the matplotlib/plotly chart
modules and the Flask app) and:

  WRITES — the desk scheduler drives `run_job` daily after the close
  (web.daily_refresh_hour, default 17:00 ET) and a cheap pre-open re-price
  (web.reprice_hour/minute, default 09:15 ET). run_job also logs opportunities and
  labels the ones whose horizon elapsed — the learning loop — in one pass. A
  one-shot startup prime screens once if nothing is persisted yet, so a fresh deploy
  has a board within a few minutes (off the request path). Both daily jobs skip
  weekends/holidays via the vendored NYSE calendar.

  READS  — `/api/recs.json` + `/api/learning.json` reproduce the Flask handlers
  byte-for-byte over the vendored store/oplog/screen, with an mtime cache so the
  pickle is only re-read when the job rewrites it. Charts/report (matplotlib/plotly)
  and the /ticker, /live, /export routes are dashboard-only and not vendored.

Synergy (already wired in the desk's structure adapter, no code here): each rec's
ticker is enriched by the Phase-1 stock engine's /api/analyze?ai=0 deterministic
0-100 quant (`scorecard.overall`) and by OFS /api/rotation's sector quadrant — both
in-process cross-engine calls through the router.

Live when a Massive key is present (`serve` predicate) — the GEX agreement component
needs it, and this keeps the desk's keyless sample-mode contract intact. Persistent
state (bar cache, result pickle, learning DB) lands in the shared data dir via the
tool's own DATA_DIR override.
"""

import logging
import os
import threading
import time
from pathlib import Path

from .. import route, serve, jobs
from .. import config as econf

# Rebase the tool's persistent state (bar cache / result pickle / learning DB) onto
# the shared desk data dir. The vendored config reads DATA_DIR in Config.load (its
# _apply_data_dir_override), so this must be set before _cfg() loads the yaml.
os.environ.setdefault("DATA_DIR", econf.DATA_DIR)

log = logging.getLogger("engines.structure")

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

_cfg_obj = None
_cfg_lock = threading.RLock()
# Cache the loaded result set, re-read only when the job rewrites the pickle.
_cache: dict = {"results": [], "meta": {}, "mtime": 0.0}
_job_lock = threading.Lock()
_job_running = False


def _cfg():
    global _cfg_obj
    if _cfg_obj is None:
        with _cfg_lock:
            if _cfg_obj is None:
                from .config import Config
                _cfg_obj = Config.load(_CONFIG_PATH)
    return _cfg_obj


def _load_if_changed(cfg):
    """Reload the persisted results iff the store pickle is newer than cached."""
    from . import store
    m = store.mtime(cfg)
    if m and m > _cache["mtime"]:
        results, meta = store.load(cfg)
        if results is not None:
            _cache.update({"results": results, "meta": meta or {}, "mtime": m})
    return _cache["results"], _cache["meta"]


# ------------------------------------------------------------- read endpoints
def _produce_recs(params: dict) -> dict:
    """Reproduce `GET /api/recs.json` — the ranked setups, serialized from the
    persisted result set (nothing recomputed in the read path)."""
    from datetime import datetime
    cfg = _cfg()
    from .screen import _agreement_signals
    from . import live
    results, meta = _load_if_changed(cfg)

    run_date = (meta.get("last_run") or "").split(" ")[0] or datetime.now().date().isoformat()
    try:
        live_peaks = live.read_daily_summary(cfg, run_date)
    except Exception:
        live_peaks = {}

    out = []
    for r in results:
        aligned = r.aligned_zone
        if aligned is not None and aligned is r.resistance:
            direction, kind = "up", "resistance"
        elif aligned is not None and aligned is r.support:
            direction, kind = "down", "support"
        elif r.flag is not None:
            direction = "up" if r.flag.kind == "bull" else "down"
            kind = "resistance" if direction == "up" else "support"
        else:
            direction, kind = "up", "resistance"

        zone_score = aligned.score if aligned is not None else 0.0
        sig = _agreement_signals(zone_score, r.flag, r.gex_amp, r.rvol_amp, cfg)
        out.append({
            "ticker":         r.ticker,
            "price":          round(float(r.profile.current_price), 2) if r.profile else None,
            "direction":      direction,
            "zone":           kind,
            "distance_pct":   round(aligned.distance_pct, 2) if aligned is not None else None,
            "combined_score": round(float(r.combined_score), 3),
            "components":     {"zone": bool(sig["zone"]), "flag": bool(sig["flag"]),
                               "gex":  bool(sig["gex"]),  "rvol": bool(sig["rvol"])},
            "rvol":           r.rvol,
            "gex_amp":        round(r.gex_amp, 3),
            "rvol_amp":       round(r.rvol_amp, 3),
            "live_activated": bool((live_peaks.get(r.ticker) or {}).get("ever_activated")),
            "next_earnings":  r.next_earnings,
        })
    return {"as_of": meta.get("last_run"), "repriced_at": meta.get("repriced_at"),
            "count": len(out), "recs": out}


def _produce_learning(params: dict) -> dict:
    """Reproduce `GET /api/learning.json` — win-rate / return by setup type and
    horizon over the learning log. Never raises (returns available:false)."""
    from . import oplog
    cfg = _cfg()
    try:
        s = oplog.stats(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
    groups = [{
        "type":       g.get("type"),
        "horizon":    g.get("horizon"),
        "n":          g.get("n"),
        "win_pct":    round(g["win_pct"], 1) if g.get("win_pct") is not None else None,
        "avg_return": round(g["avg_ret"], 2) if g.get("avg_ret") is not None else None,
    } for g in s.get("groups", [])]
    return {
        "available": True,
        "n_outcomes": s.get("n_outcomes"),
        "min_samples": int(cfg.get("learn.min_label_samples", 50)),
        "groups": groups,
    }


route("/api/recs.json")(_produce_recs)
route("/api/learning.json")(_produce_learning)

serve("structure", econf.has_massive)


# ------------------------------------------------- scheduled jobs (the writer)
def _run_screen() -> None:
    """Full EOD screen (+ opportunity logging/labeling) — the heavy writer. Guarded
    against overlap; skips non-trading days."""
    global _job_running
    from . import schedule
    from datetime import datetime
    cfg = _cfg()
    tz = schedule.market_tz(cfg)
    if not schedule.is_trading_day(datetime.now(tz).date()):
        return
    with _job_lock:
        if _job_running:
            return
        _job_running = True
    try:
        from .job import run_job
        ranked = run_job(cfg, force_refresh=True)
        _load_if_changed(cfg)
        log.info("structure screen persisted %d ranked setups", len(ranked))
    except Exception:
        log.exception("structure screen job failed")
    finally:
        with _job_lock:
            _job_running = False


def _run_reprice() -> None:
    """Pre-open re-price of the persisted profiles against fresh quotes (cheap: no
    bar refetch, no profile rebuild). Skips non-trading days and overlaps."""
    from . import schedule, reprice
    from datetime import datetime
    cfg = _cfg()
    tz = schedule.market_tz(cfg)
    if not schedule.is_trading_day(datetime.now(tz).date()):
        return
    with _job_lock:
        if _job_running:
            return
    try:
        reprice.reprice(cfg)
        _load_if_changed(cfg)
        log.info("structure pre-open re-price done")
    except Exception:
        log.exception("structure reprice failed")


def _startup_prime() -> None:
    """Screen once at startup if nothing is persisted yet, so a fresh deploy has a
    board within a few minutes (off the request path; _run_screen guards overlap)."""
    cfg = _cfg()
    results, _ = _load_if_changed(cfg)
    if results:
        log.info("structure startup prime: %d setups already persisted", len(results))
        return
    log.info("structure startup prime: running first screen")
    threading.Thread(target=_run_screen, name="structure-prime", daemon=True).start()


def _register_jobs(scheduler, tz) -> None:
    """Attach the daily screen + pre-open re-price crons (in the screener's market
    tz, DST-safe) and a one-shot startup prime. next_refresh/is_trading_day inside
    the jobs skip holidays, which CronTrigger alone cannot."""
    from apscheduler.triggers.cron import CronTrigger
    cfg = _cfg()
    from . import schedule as sched_mod
    mtz = sched_mod.market_tz(cfg)
    refresh_hour = int(cfg.get("web.daily_refresh_hour", 17))
    reprice_hour = int(cfg.get("web.reprice_hour", 9))
    reprice_minute = int(cfg.get("web.reprice_minute", 15))

    scheduler.add_job(
        _run_screen, CronTrigger(day_of_week="mon-fri", hour=refresh_hour, minute=0, timezone=mtz),
        id="structure-screen", replace_existing=True, misfire_grace_time=3600,
        max_instances=1, coalesce=True)
    scheduler.add_job(
        _run_reprice, CronTrigger(day_of_week="mon-fri", hour=reprice_hour, minute=reprice_minute, timezone=mtz),
        id="structure-reprice", replace_existing=True, misfire_grace_time=1800,
        max_instances=1, coalesce=True)
    scheduler.add_job(_startup_prime, id="structure-prime", replace_existing=True)
    log.info("structure jobs registered (screen %02d:00 %s mon-fri; reprice %02d:%02d)",
             refresh_hour, mtz.key, reprice_hour, reprice_minute)


jobs(_register_jobs)
