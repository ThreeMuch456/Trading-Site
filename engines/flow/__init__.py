"""Options-Flow-Scanner engine (vendored) — short-dated index flow + sweeps + rotation.

The OFS tool is database-backed: a background **scanner** polls Massive and writes
flagged contracts into a SQLite table, and the web endpoints only read/aggregate that
table. So this engine has two halves:

  WRITES — the vendored scanner (`scanner.run_scan`, market-hours-gated) runs on the
  desk scheduler (registered via `jobs()`), writing to a SQLite file under the shared
  data dir (or a Railway Postgres if DATABASE_URL is set).

  READS  — rather than re-implement the hundreds of lines of aggregation that live as
  closures inside OFS's `create_app()`, the engine runs that real Flask app in-process
  and calls its API endpoints through a Flask **test client**. Zero reimplementation,
  byte-identical contracts. The six endpoints the desk consumes are registered as
  engine routes that forward to the test client:
    /api/index-etfs[/waves|/eod] · /api/sweeps · /api/ticker/<sym> · /api/rotation

With an empty table (no scan has run yet) every endpoint returns empty-but-valid JSON,
so the desk's flow adapter renders "no notable flow" rather than failing.

Note: the vendored scanner still uses OFS's own Massive client (`massive_client.py`),
not the shared `engines.massive` rate limiter. Unifying the request budget across
engines is deferred to Phase 6; in isolation the scanner behaves exactly as it did
standalone.
"""

import logging
import os
import threading
from urllib.parse import urlencode

from .. import route, route_prefix, serve, jobs
from .. import config as econf

# Point the vendored OFS db layer at the shared data dir — unless DATABASE_URL is
# already set (e.g. a Railway Postgres), which takes precedence. Must happen before
# db.py is imported (lazily, in _ensure_db); db.py reads DATABASE_URL at import.
os.environ.setdefault("DATABASE_URL", "sqlite:///" + econf.db_path("flow.db").replace("\\", "/"))

log = logging.getLogger("engines.flow")

_client = None
_db_ready = False
# Reentrant: _get_client() holds this lock and then calls _ensure_db(), which
# re-acquires it. A plain Lock deadlocks there.
_lock = threading.RLock()

# Serializes scans. The one-shot prime job and the first interval tick are
# distinct APScheduler jobs, so flow-scan's max_instances=1 does not stop them
# from running at the same time on startup — two concurrent run_scan() calls in
# two sessions both miss an existing contract_volume_state row and both INSERT it,
# tripping the UNIQUE(option_ticker) constraint. A non-blocking lock makes an
# overlapping scan skip this tick rather than race (you never want two concurrent
# scans against Massive anyway).
_scan_lock = threading.Lock()


def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    with _lock:
        if _db_ready:
            return
        from .db import init_db
        init_db()
        _db_ready = True


def _get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _ensure_db()
                from .web.app import create_app
                app = create_app()
                app.testing = True
                _client = app.test_client()
    return _client


def _call(path: str, params: dict | None = None) -> dict:
    """Forward a read endpoint to the in-process OFS app via its test client."""
    url = path + ("?" + urlencode(params) if params else "")
    resp = _get_client().get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"flow {path}: HTTP {resp.status_code}")
    return resp.get_json()


# ---- read endpoints (exact JSON contracts the desk flow adapter consumes) ----
route("/api/index-etfs")(lambda params: _call("/api/index-etfs", params))
route("/api/index-etfs/waves")(lambda params: _call("/api/index-etfs/waves", params))
route("/api/index-etfs/eod")(lambda params: _call("/api/index-etfs/eod", params))
route("/api/sweeps")(lambda params: _call("/api/sweeps", params))
route("/api/rotation")(lambda params: _call("/api/rotation", params))
route_prefix("/api/ticker/")(lambda tail, params: _call("/api/ticker/" + tail, params))

serve("flow", econf.has_massive)


# ---- scanner (the writer), driven by the desk scheduler ----

def _safe_scan() -> None:
    if not _scan_lock.acquire(blocking=False):
        log.info("flow scan already running — skipping this trigger")
        return
    try:
        _ensure_db()
        from . import scanner
        scanner.run_scan()   # market-hours gated; no-op outside the session window
    except Exception:
        log.exception("flow scan failed")
    finally:
        _scan_lock.release()


def _register_jobs(scheduler, tz) -> None:
    """Add the scanner job to the desk scheduler on the OFS scan cadence."""
    from apscheduler.triggers.interval import IntervalTrigger
    from . import scanner
    scheduler.add_job(_safe_scan, IntervalTrigger(minutes=scanner.SCAN_INTERVAL_MINUTES),
                      id="flow-scan", max_instances=1, coalesce=True,
                      replace_existing=True)
    # Prime once at startup so a freshly-deployed desk has flow within a cycle
    # rather than waiting a full interval (still market-hours gated inside run_scan).
    scheduler.add_job(_safe_scan, id="flow-scan-prime", replace_existing=True)
    log.info("flow scanner job registered (every %d min)", scanner.SCAN_INTERVAL_MINUTES)


jobs(_register_jobs)
