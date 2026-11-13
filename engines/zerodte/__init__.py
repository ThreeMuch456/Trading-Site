"""0DTE-Assistant engine (vendored) — live mechanical SPY/QQQ/IWM levels.

The standalone tool is a FastAPI app whose per-ticker `TickerEngine` is populated
by **async pollers** (one underlying loop @15s, one options loop @60s per ticker)
and read, synchronously, by `/api/state`. The engine math, the async `MassiveClient`
(httpx), and the yfinance underlying feed are all vendored BYTE-IDENTICAL under
`engines/zerodte/{config,db,engine,sources}`. Only the *driver* changes.

Why a background event loop rather than a sync rewrite: `MassiveClient` holds one
`httpx.AsyncClient` plus an asyncio sliding-window rate limiter, and an AsyncClient
is bound to the loop it runs on — you cannot `asyncio.run()` a fresh loop each poll
and reuse the pool. So this module runs ONE persistent asyncio loop in a daemon
thread and drives the real coroutines on it:

  WRITES — the desk scheduler fires two interval jobs (`zerodte-underlying`,
  `zerodte-options`) that marshal the engines' real `poll_underlying()` /
  `poll_options()` coroutines onto that loop via `run_coroutine_threadsafe`. A
  one-shot `zerodte-prime` job does the slow startup (yfinance history + 0DTE
  expiry discovery) off the request path. Everything is market-hours gated exactly
  as the standalone pollers were.

  READS  — `/api/state` and `/api/touch-stats.json` reproduce the FastAPI routes
  byte-for-byte. `get_state()` is pure CPU; it is marshalled onto the loop too so
  it runs atomically between a poller's awaits — the same single-threaded-mutation
  guarantee the original had under its single ASGI loop. `touch_label_stats()` is a
  locked SQLite read, called directly.

Without a Massive key the source is not registered (`serve` predicate false) and the
desk renders the Gameplan/Intraday pages from sample data. With a key but before the
scheduler has primed/polled, every engine returns an empty-but-valid state and the
flow adapter degrades gracefully rather than failing — the stateful-engine contract
the desk already relies on for Flow.
"""

import asyncio
import logging
import math
import threading
from datetime import datetime, timedelta

from .. import route, serve, jobs
from .. import config as econf
from . import config as zconf

log = logging.getLogger("engines.zerodte")

# --- singleton state (built once, lives for the process) ---
_db = None
_massive = None
_engines: dict = {}
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_built = False
_started = False
_lock = threading.RLock()


# ---------------------------------------------------------------- poll window
def _in_poll_window(now: datetime | None = None) -> bool:
    """Weekday 09:00–16:20 ET (zconf.POLL_WINDOW_*); ALWAYS_POLL ignores it.
    Outside the window state is still served — polling just stops spending
    request budget on sources with nothing new to say."""
    if zconf.ALWAYS_POLL:
        return True
    now = now or datetime.now(zconf.ET)
    if now.weekday() >= 5:
        return False
    t = now.timetz().replace(tzinfo=None)
    return zconf.POLL_WINDOW_START <= t <= zconf.POLL_WINDOW_END


def _last_completed_session(today):
    """Most recent weekday strictly before `today` — a range that actually holds
    bars, for the account-level stock-aggs entitlement probe."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# --------------------------------------------------------- loop plumbing
def _start_loop() -> None:
    """Spin up the persistent asyncio loop in a daemon thread (idempotent)."""
    global _loop, _loop_thread
    if _loop is not None:
        return
    loop = asyncio.new_event_loop()

    def _run_forever():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    th = threading.Thread(target=_run_forever, name="zerodte-loop", daemon=True)
    th.start()
    _loop, _loop_thread = loop, th


def _run(coro, timeout: float):
    """Marshal a coroutine onto the engine loop and wait (bounded)."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)


def _ensure_built() -> None:
    """Fast, network-free: build DB + shared MassiveClient + the per-ticker
    engines and start the loop. Does NOT load history or discover expiry — that
    is the prime job's slow work, kept off the request path."""
    global _db, _massive, _engines, _built
    if _built:
        return
    with _lock:
        if _built:
            return
        from .db import DB
        _db = DB(econf.db_path("zerodte.sqlite3"))
        _start_loop()

        async def _build():
            from .sources.massive import MassiveClient
            from .engine.orchestrator import TickerEngine
            massive = MassiveClient()
            engines = {t: TickerEngine(t, _db, massive) for t in zconf.TICKERS}
            return massive, engines

        # Build the AsyncClient-holding client ON the loop so its pool binds there.
        _massive, _engines = _run(_build(), timeout=30)
        _built = True
        log.info("zerodte engines built (%d tickers)", len(_engines))


def _prime() -> None:
    """Slow startup, off the request path: load each engine's day history and
    discover the 0DTE expiry, then probe account-level stock-aggs entitlement
    once (mirrors the standalone lifespan)."""
    global _started
    _ensure_built()
    if _started:
        return

    async def _start_all():
        await asyncio.gather(*(e.start() for e in _engines.values()))
        if zconf.MASSIVE_API_KEY and zconf.TICKERS:
            try:
                probe_day = _last_completed_session(datetime.now(zconf.ET).date())
                await _massive.probe_aggs_entitlement(zconf.TICKERS[0], probe_day)
            except Exception:
                log.warning("stock aggs entitlement probe failed", exc_info=True)

    _run(_start_all(), timeout=120)
    _started = True
    log.info("zerodte engines primed (history + expiry discovery done)")


# --------------------------------------------------------- the writer (polls)
async def _poll_all(method: str) -> None:
    async def one(e):
        try:
            await e.maybe_roll_session()
            await getattr(e, method)()
        except Exception:
            log.exception("%s %s failed", e.ticker, method)
    await asyncio.gather(*(one(e) for e in _engines.values()))


def _poll_underlying() -> None:
    _ensure_built()
    if not _in_poll_window():
        return
    try:
        _run(_poll_all("poll_underlying"), timeout=45)
    except Exception:
        log.exception("zerodte underlying poll error")


def _poll_options() -> None:
    _ensure_built()
    if not _in_poll_window():
        return
    try:
        _run(_poll_all("poll_options"), timeout=90)
    except Exception:
        log.exception("zerodte options poll error")


def _maintenance() -> None:
    """Daily retention prune so the shared volume stays flat (cheap no-op when
    nothing is out of window)."""
    if not _built or _db is None:
        return
    try:
        today = datetime.now(zconf.ET).date().isoformat()
        deleted = _db.prune(today)
        if any(deleted.values()):
            log.info("zerodte retention prune: %s",
                     {k: v for k, v in deleted.items() if v})
    except Exception:
        log.exception("zerodte maintenance error")


# ------------------------------------------------------------- read endpoints
def _json_safe(obj):
    """JSON has no NaN/Infinity; one non-finite float would 500 the whole state
    payload. Non-finite floats become null (rendered as an em dash)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _ticker_state(ticker: str, engine) -> dict:
    """One engine failing must not blank the other two — report the failure in
    that ticker's own slot (byte-identical to the standalone route)."""
    try:
        return engine.get_state()
    except Exception as ex:  # noqa: BLE001
        log.exception("%s: get_state failed", ticker)
        return {
            "ticker": ticker,
            "engine_error": f"{type(ex).__name__}: {ex}",
            "session_date": engine.session_date,
            "ts": datetime.now(zconf.ET).isoformat(),
            "no_0dte": False,
            "expiry": engine.expiry.isoformat() if engine.expiry else None,
            "sources": {
                "underlying": {**engine.underlying_status, "age_s": None, "degraded": True},
                "massive": {**engine.option_status, "age_s": None, "degraded": True},
            },
            "events": engine.events[-20:],
        }


def _produce_state(params: dict) -> dict:
    """Reproduce FastAPI `GET /api/state`. get_state() is marshalled onto the
    engine loop so it reads a snapshot atomic w.r.t. the pollers."""
    _ensure_built()

    async def _astate():
        return {
            "ts": datetime.now(zconf.ET).isoformat(),
            "polling": _in_poll_window(),
            "tickers": {t: _ticker_state(t, e) for t, e in _engines.items()},
        }

    return _json_safe(_run(_astate(), timeout=12))


def _produce_touch_stats(params: dict) -> dict:
    """Reproduce `GET /api/touch-stats.json` — level-behaviour calibration read
    straight off the touches table (its own lock); a DB hiccup returns
    available:false rather than raising."""
    _ensure_built()
    try:
        stats = _db.touch_label_stats()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
    return {"available": True, **stats}


route("/api/state")(_produce_state)
route("/api/touch-stats.json")(_produce_touch_stats)

serve("zerodte", econf.has_massive)


# ------------------------------------------------- scheduler job registration
def _register_jobs(scheduler, tz) -> None:
    from apscheduler.triggers.interval import IntervalTrigger

    # One-shot slow startup (history + expiry discovery), off the request path.
    scheduler.add_job(_prime, id="zerodte-prime", replace_existing=True)
    # Fast underlying clock.
    scheduler.add_job(
        _poll_underlying,
        IntervalTrigger(seconds=zconf.UNDERLYING_POLL_SECONDS),
        id="zerodte-underlying", max_instances=1, coalesce=True,
        replace_existing=True)
    # Slow options clock (T-15 chain + flow).
    scheduler.add_job(
        _poll_options,
        IntervalTrigger(seconds=zconf.OPTION_POLL_SECONDS),
        id="zerodte-options", max_instances=1, coalesce=True,
        replace_existing=True)
    # Daily retention prune.
    scheduler.add_job(
        _maintenance, IntervalTrigger(hours=6),
        id="zerodte-maintenance", max_instances=1, coalesce=True,
        replace_existing=True)
    log.info("zerodte jobs registered (underlying %ds / options %ds)",
             zconf.UNDERLYING_POLL_SECONDS, zconf.OPTION_POLL_SECONDS)


jobs(_register_jobs)
