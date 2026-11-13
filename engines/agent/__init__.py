"""Trading-Agent engine (vendored) — the narrative + learning brain.

The standalone tool is a Flask dashboard whose two machine-readable endpoints the
desk consumes:
  /api/brief.json        — latest synthesized morning gameplan (bias/conviction/
                           bull-bear/levels/risk-flags + market-structure/gamma/
                           macro + overall_risk_rating + watch_list).
  /api/performance.json  — the learning record: accuracy by ticker/bias/gamma/risk,
                           Brier, baselines, plus the latest session's graded
                           outcomes.
Both are read-only serializers over a single SQLite journal, so this engine does
NOT run the 2 800-line Flask app — the two handlers are reproduced byte-for-byte
here over the vendored `journal` module.

Two halves, like every stateful engine:
  WRITES — a scheduled **morning brief** (`_do_run`, vendored verbatim from the
  tool): fetch market/global/macro/options data, then call Claude via
  `synthesize_gameplan.synthesize` to produce the gameplan, and upsert the session
  row. `synthesize` pulls `journal.get_performance_stats` to feed recent scored
  outcomes back into the prompt — the recursive calibration loop. A post-close
  **autograde** job (`grade_outcomes.autograde_date`, cheap — yfinance/Massive, no
  Claude) scores today's biases open→close into the `outcomes` table that
  performance.json reads. (The tool's heavier Claude "Previous Day" review is a
  dashboard-only feature the desk never reads, so it is not vendored.)

  READS  — `/api/brief.json` + `/api/performance.json`, straight off the journal.

Scheduling (desk scheduler, opt-in): the brief fires daily at BRIEF_HOUR in
BRIEF_TZ (defaults 05:00 America/Los_Angeles ≈ 08:00 ET) with :15/:30 retry passes
that no-op once today's brief exists; autograde fires at GRADE_HOUR:GRADE_MINUTE
(default 13:35 PT, after the close). A one-shot **startup prime** generates today's
brief if it is missing, so a fresh deploy goes live within a minute instead of
waiting for the next cron (closed days produce a next-session preview, exactly as
the tool frames them).

Live when an Anthropic key is present (`serve` predicate). Before the first brief
exists, brief.json returns available:false and the desk's agent adapter degrades
the AI sections to sample — the stateful-engine contract — until the prime lands.
"""

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime

from .. import route, serve, jobs
from .. import config as econf

# Point the vendored journal at the shared data dir BEFORE journal is imported
# anywhere (it reads DB_PATH at import). A Railway Postgres is not used here — the
# agent journal is a small SQLite file under the shared volume.
os.environ.setdefault("DB_PATH", econf.db_path("agent.sqlite3"))

log = logging.getLogger("engines.agent")

_run_lock = threading.Lock()
_run_state: dict = {"status": "idle", "message": "", "error": None, "last_run": None}


def _set_msg(msg: str) -> None:
    with _run_lock:
        _run_state["message"] = msg


# --------------------------------------------------------- the writer (brief)
def _do_run() -> None:
    """Generate and persist one morning brief. Vendored verbatim from the tool's
    background pipeline; only the imports are package-relative."""
    try:
        from .fetch_market_data import fetch_watchlist, fetch_futures, WATCHLIST
        from .fetch_global_markets import fetch_global_markets
        from .fetch_macro_calendar import fetch_macro_calendar
        from .fetch_options_data import fetch_options_snapshot
        from .synthesize_gameplan import synthesize
        from .journal import get_conn
        from .market_calendar import market_today

        fred_key = os.environ.get("FRED_API_KEY")
        fetched_at = datetime.now().isoformat(timespec="seconds")

        _set_msg("Fetching market data…")
        market_raw = fetch_watchlist(WATCHLIST)

        _set_msg("Fetching global / overnight markets…")
        global_markets = fetch_global_markets()

        _set_msg("Fetching index futures…")
        futures = fetch_futures()

        market_data = {"fetched_at": fetched_at, "watchlist": market_raw,
                       "global_markets": global_markets, "futures": futures}

        _set_msg("Fetching macro calendar…")
        macro_data = fetch_macro_calendar(fred_key)

        _set_msg("Fetching risk-free rate…")
        try:
            params = {"series_id": "DGS3MO", "api_key": fred_key, "file_type": "json",
                      "sort_order": "desc", "limit": 5}
            url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            rfr = 0.05
            for obs in data.get("observations", []):
                val = obs.get("value", ".")
                if val != ".":
                    rfr = round(float(val) / 100, 4)
                    break
        except Exception:
            rfr = 0.05

        _set_msg("Fetching options data…")
        opts_raw = fetch_options_snapshot(WATCHLIST, risk_free_rate=rfr)
        options_data = {"fetched_at": fetched_at, "options": opts_raw}

        _set_msg("Synthesizing gameplan with Claude…")
        gameplan = synthesize(market_data, macro_data, options_data)

        _set_msg("Saving to journal…")
        # The trading day this brief is *for*, on the market's clock — not the
        # server's — so an evening run doesn't file under tomorrow once UTC rolls.
        log_date = market_today().isoformat()
        conn = get_conn()
        conn.execute(
            """INSERT INTO sessions (date, fetched_at, market_data, macro_data, options_data, gameplan)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 fetched_at=excluded.fetched_at, market_data=excluded.market_data,
                 macro_data=excluded.macro_data, options_data=excluded.options_data,
                 gameplan=excluded.gameplan""",
            (log_date, fetched_at,
             json.dumps(market_data), json.dumps(macro_data),
             json.dumps(options_data), json.dumps(gameplan)),
        )
        conn.commit()
        conn.close()
        with _run_lock:
            _run_state.update({"status": "idle", "message": "", "error": None,
                               "last_run": fetched_at})
        log.info("morning brief generated for %s", log_date)
    except Exception as e:  # noqa: BLE001
        with _run_lock:
            stage = _run_state.get("message") or "?"
            detail = f"[{stage.rstrip('… ')}] {e}"
            _run_state.update({"status": "error", "message": detail, "error": detail})
        log.exception("morning brief generation failed")


def _trigger_run() -> str:
    """Start a brief generation in the background unless one is already running."""
    with _run_lock:
        if _run_state["status"] == "running":
            return "already_running"
        _run_state.update({"status": "running", "message": "Starting…", "error": None})
    threading.Thread(target=_do_run, name="agent-brief", daemon=True).start()
    return "started"


def _brief_exists(date_iso: str) -> bool:
    try:
        from .journal import get_conn
        conn = get_conn()
        row = conn.execute("SELECT 1 FROM sessions WHERE date = ?", (date_iso,)).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# ------------------------------------------------------------- read endpoints
def _produce_brief(params: dict) -> dict:
    """Reproduce `GET /api/brief.json` — the latest stored session row. Returns
    available:false (which the desk adapter degrades on) before the first brief."""
    from .journal import get_conn
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions ORDER BY date DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {"available": False, "error": "no brief generated yet"}
    market_data = json.loads(row["market_data"])
    macro_data = json.loads(row["macro_data"])
    return {
        "available": True,
        "date": row["date"],
        "fetched_at": row["fetched_at"],
        "gameplan": json.loads(row["gameplan"]),
        "global_markets": market_data.get("global_markets", {}),
        "macro": {
            "economic_releases": macro_data.get("economic_releases", []),
            "earnings": macro_data.get("earnings", []),
            "market_session": macro_data.get("market_session", {}),
        },
    }


def _produce_performance(params: dict) -> dict:
    """Reproduce `GET /api/performance.json` — learning-loop stats + the latest
    session's graded outcomes. Never raises (read endpoint the desk polls)."""
    from .journal import get_conn, get_performance_stats
    try:
        stats = get_performance_stats(n_sessions=30)
    except Exception as exc:  # noqa: BLE001
        stats = {"total": 0, "error": f"{type(exc).__name__}: {exc}"}
    conn = get_conn()
    row = conn.execute("SELECT id, date FROM sessions ORDER BY date DESC LIMIT 1").fetchone()
    outcomes = []
    if row:
        rows = conn.execute(
            "SELECT ticker, open, close, day_pct, bias_correct, notes "
            "FROM outcomes WHERE session_id = ? ORDER BY ticker", (row["id"],)).fetchall()
        outcomes = [dict(r) for r in rows]
    conn.close()
    return {"date": row["date"] if row else None, "stats": stats, "outcomes": outcomes}


route("/api/brief.json")(_produce_brief)
route("/api/performance.json")(_produce_performance)

serve("agent", lambda: bool(econf.ANTHROPIC_API_KEY))


# ------------------------------------------------- scheduled jobs (the writer)
def _scheduled_brief(is_retry: bool) -> None:
    """Daily brief. On closed days the synthesis frames it as a next-session
    preview, so this can fire every day. Retry passes skip once today's lands."""
    try:
        from .market_calendar import market_today
        if is_retry and _brief_exists(market_today().isoformat()):
            return
    except Exception:
        pass
    _trigger_run()


def _scheduled_autograde() -> None:
    """Score today's biases open→close after the close, feeding the learning loop
    (get_performance_stats → synthesize) for tomorrow's brief. Cheap: no Claude."""
    try:
        from .market_calendar import market_today
        from .grade_outcomes import autograde_date
        today = market_today().isoformat()
        autograde_date(today, os.environ.get("MASSIVE_API_KEY"))
        log.info("autograded outcomes for %s", today)
    except Exception:
        log.exception("scheduled autograde failed")


def _startup_prime() -> None:
    """Generate today's brief if it is missing, so a fresh deploy is live within a
    minute instead of waiting for the next cron (user-chosen behaviour)."""
    try:
        from .market_calendar import market_today
        today = market_today().isoformat()
        if _brief_exists(today):
            log.info("agent startup prime: brief for %s already present", today)
            return
        log.info("agent startup prime: generating brief for %s", today)
        _trigger_run()
    except Exception:
        log.exception("agent startup prime failed")


def _register_jobs(scheduler, tz) -> None:
    """Attach the brief + autograde crons (in the agent's own tz, DST-safe via the
    tz name) and a one-shot startup prime. The desk tz is ignored on purpose —
    these are wall-clock market-session times, not desk-local."""
    from apscheduler.triggers.cron import CronTrigger
    from zoneinfo import ZoneInfo

    sched_tz = ZoneInfo(os.environ.get("BRIEF_TZ", "America/Los_Angeles"))
    brief_hour = int(os.environ.get("BRIEF_HOUR", "5"))
    grade_hour = int(os.environ.get("GRADE_HOUR", "13"))
    grade_minute = int(os.environ.get("GRADE_MINUTE", "35"))

    scheduler.add_job(
        lambda: _scheduled_brief(is_retry=False),
        CronTrigger(hour=brief_hour, minute=0, timezone=sched_tz),
        id="agent-brief", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    for i, minute in enumerate((15, 30), start=1):
        scheduler.add_job(
            lambda: _scheduled_brief(is_retry=True),
            CronTrigger(hour=brief_hour, minute=minute, timezone=sched_tz),
            id=f"agent-brief-retry{i}", replace_existing=True,
            misfire_grace_time=1800, coalesce=True)
    scheduler.add_job(
        _scheduled_autograde,
        CronTrigger(hour=grade_hour, minute=grade_minute, timezone=sched_tz),
        id="agent-autograde", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # One-shot startup prime (runs off the request path; _do_run is threaded).
    scheduler.add_job(_startup_prime, id="agent-prime", replace_existing=True)
    log.info("agent jobs registered (brief %02d:00 %s + retries; autograde %02d:%02d)",
             brief_hour, sched_tz.key, grade_hour, grade_minute)


jobs(_register_jobs)
