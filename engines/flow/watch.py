"""Watchlist alert-outcome loop — closes the scanner's second open loop.

The scanner auto-watches whale alerts (signals.is_auto_watch) and snapshots each
watched contract's price through the session, so the Watchlist page can show a
running P&L. But nothing ever asked the question the P&L exists to answer — did
following the alert pay? — and the snapshots are purged after 15 days, so the
evidence was being thrown away. `score_alert`'s weights (premium, vol/OI, delta,
DTE) have therefore never been checked against a single outcome.

This module keeps that evidence, in the same shape as the ETF-flow loop
(learning.py / label.py) and in the same separate stdlib-sqlite store
(flow_learning.db), so the scanner's own ORM database is only ever read:

  capture()    after the close, one `watch_signals` row per watchlist entry:
               the contract, the entry prices, and the RAW features the score is
               built from (so the score can be re-weighted later, not just
               re-judged).
  label_due()  one `watch_outcomes` row per (entry, horizon) once that horizon's
               session has a snapshot: the underlying's move in the alert's
               direction, and the option's own P&L.

Horizons are counted in sessions that actually have snapshots — "1d" is the next
day the scanner ran — so holidays need no calendar. The outcome is the entry's
last snapshot of that session (the close, on a 15-minute delay).

No network: every price used here was already fetched by the scanner.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .. import config as econf
from . import learning

log = logging.getLogger("engines.flow.watch")

# (label, sessions after the entry's own). "0d" is the entry session's close.
HORIZONS: list[tuple[str, int]] = [("0d", 0), ("1d", 1), ("3d", 3), ("5d", 5)]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_signals (
    id               TEXT PRIMARY KEY,   -- the watchlist entry id, as text
    entry_id         INTEGER NOT NULL,
    alert_id         INTEGER,
    run_date         TEXT NOT NULL,      -- ET date the entry was added
    added_at         TEXT NOT NULL,      -- ISO8601 UTC
    ticker           TEXT NOT NULL,
    option_ticker    TEXT,
    option_type      TEXT NOT NULL,      -- "call" | "put"
    expiry           TEXT NOT NULL,
    strike           REAL NOT NULL,
    dte              INTEGER,            -- calendar days to expiry on run_date
    entry_price      REAL NOT NULL,      -- option price when added
    entry_underlying REAL NOT NULL,      -- underlying price when added
    score            INTEGER,            -- signals.score_alert at capture (NULL if captured late)
    premium          REAL,               -- the raw inputs to that score:
    vol_oi_ratio     REAL,
    delta            REAL,
    implied_volatility REAL,
    side             TEXT,
    captured_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_watch_signals_run_date ON watch_signals (run_date);

CREATE TABLE IF NOT EXISTS watch_outcomes (
    signal_id         TEXT NOT NULL,
    horizon           TEXT NOT NULL,     -- "0d" | "1d" | "3d" | "5d"
    outcome_date      TEXT NOT NULL,     -- ET date of the snapshot used
    labeled_at        TEXT NOT NULL,
    option_price      REAL NOT NULL,
    option_pnl_pct    REAL NOT NULL,     -- the contract's own % change from entry
    underlying_price  REAL NOT NULL,
    raw_return        REAL NOT NULL,     -- underlying % change from entry
    signed_return     REAL NOT NULL,     -- raw_return in the alert's direction (call +, put -)
    direction_correct INTEGER NOT NULL,  -- 1 if signed_return > 0
    PRIMARY KEY (signal_id, horizon),
    FOREIGN KEY (signal_id) REFERENCES watch_signals (id)
);
"""


@contextmanager
def _db(path: str | None = None):
    """The learning DB with this module's tables ensured (see learning._db)."""
    with learning._db(path) as conn:
        conn.executescript(_SCHEMA)
        yield conn


def _et_date(dt: datetime) -> str:
    """ET calendar date of a datetime the scanner stored (naive = UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(econf.ET).date().isoformat()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── Pure parts (testable without a database) ────────────────────────────────────

def signal_row(entry, alert, today: str | None = None) -> dict | None:
    """One watch_signals row from a watchlist entry and the alert that created it.

    `entry` / `alert` are anything with the ORM models' attributes. Returns None
    when the entry has no usable entry prices (nothing to measure a move from).
    The score is only recorded when capture happens on the entry's own date:
    score_alert reads days-to-expiry off today's clock, so scoring an older
    entry would silently use the wrong urgency points."""
    price, under = entry.entry_price or 0, entry.entry_underlying or 0
    if price <= 0 or under <= 0 or entry.added_at is None:
        return None
    run_date = _et_date(entry.added_at)
    try:
        from datetime import date
        dte = (date.fromisoformat(entry.expiry) - date.fromisoformat(run_date)).days
    except (TypeError, ValueError):
        dte = None
    score = None
    if alert is not None and (today or learning._today_et_date()) == run_date:
        try:
            from .signals import score_alert
            score = int(score_alert(alert))
        except Exception:
            score = None
    return {
        "id": str(entry.id), "entry_id": int(entry.id),
        "alert_id": getattr(entry, "alert_id", None),
        "run_date": run_date, "added_at": _iso(entry.added_at),
        "ticker": entry.ticker, "option_ticker": getattr(entry, "option_ticker", None),
        "option_type": entry.option_type, "expiry": entry.expiry, "strike": float(entry.strike),
        "dte": dte, "entry_price": float(price), "entry_underlying": float(under),
        "score": score,
        "premium": getattr(alert, "estimated_premium", None),
        "vol_oi_ratio": getattr(alert, "vol_oi_ratio", None),
        "delta": getattr(alert, "delta", None),
        "implied_volatility": getattr(alert, "implied_volatility", None),
        "side": getattr(alert, "side", None),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def closed_through(now: datetime | None = None) -> str:
    """The last ET date whose session is over, so its final snapshot is the close.
    Today once the delayed closing prints are in (16:20 ET, the same cutoff the
    ETF-flow loop uses for "eod"); otherwise yesterday."""
    from datetime import time, timedelta
    now = (now or datetime.now(timezone.utc)).astimezone(econf.ET)
    d = now.date() if now.time() >= time(16, 20) else now.date() - timedelta(days=1)
    return d.isoformat()


def pick_outcomes(signal: dict, snapshots: list, session_dates: list, done: set,
                  through: str) -> list:
    """Outcome rows that can be written now for one signal.

    snapshots      this entry's snapshots: dicts with date (ET), at (sortable),
                   option_price, underlying_price
    session_dates  every ET date on which the scanner took ANY snapshot, sorted —
                   the session calendar horizons are counted on
    done           horizons already labeled for this signal
    through        last ET date whose session is complete (see closed_through)

    A horizon is labeled only from a snapshot taken on its own session and after
    the entry was added; a session the entry has no snapshot on is skipped rather
    than borrowed from a neighbour. An outcome is never written for a session
    still in progress, because once written it is never revised."""
    later = [d for d in session_dates if d > signal["run_date"]]
    sign = 1.0 if signal["option_type"] == "call" else -1.0
    out = []
    for label, n in HORIZONS:
        if label in done:
            continue
        if n == 0:
            target = signal["run_date"]
        elif len(later) >= n:
            target = later[n - 1]
        else:
            continue                                   # that session has not happened yet
        if target > through:
            continue                                   # ...or has not finished
        day = [s for s in snapshots if s["date"] == target and s["at"] > signal["added_at"]
               and (s["option_price"] or 0) > 0 and (s["underlying_price"] or 0) > 0]
        if not day:
            continue
        last = max(day, key=lambda s: s["at"])
        raw = (last["underlying_price"] / signal["entry_underlying"] - 1.0) * 100.0
        signed = raw * sign
        out.append({
            "signal_id": signal["id"], "horizon": label, "outcome_date": target,
            "labeled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "option_price": float(last["option_price"]),
            "option_pnl_pct": round((last["option_price"] / signal["entry_price"] - 1.0) * 100.0, 4),
            "underlying_price": float(last["underlying_price"]),
            "raw_return": round(raw, 4), "signed_return": round(signed, 4),
            "direction_correct": 1 if signed > 0 else 0,
        })
    return out


# ── Reads from the scanner's database (the only place the ORM is touched) ───────

def _load_entries() -> list:
    """[(entry, alert_or_None)] for every watchlist entry, active or retired."""
    from .db import Session, SweepAlert, WatchlistEntry
    sess = Session()
    try:
        entries = sess.query(WatchlistEntry).all()
        ids = [e.alert_id for e in entries if e.alert_id is not None]
        alerts = ({a.id: a for a in sess.query(SweepAlert).filter(SweepAlert.id.in_(ids)).all()}
                  if ids else {})
        pairs = [(e, alerts.get(e.alert_id)) for e in entries]
        sess.expunge_all()
        return pairs
    finally:
        sess.close()


def _load_snapshots() -> list:
    """Every retained price snapshot as a plain dict (see pick_outcomes)."""
    from .db import PriceSnapshot, Session
    sess = Session()
    try:
        rows = sess.query(PriceSnapshot.entry_id, PriceSnapshot.captured_at,
                          PriceSnapshot.option_price, PriceSnapshot.underlying_price).all()
    finally:
        sess.close()
    return [{"entry_id": r[0], "date": _et_date(r[1]), "at": _iso(r[1]),
             "option_price": r[2], "underlying_price": r[3]}
            for r in rows if r[1] is not None]


# ── Capture / label ──────────────────────────────────────────────────────────────

_SIGNAL_COLS = ("id", "entry_id", "alert_id", "run_date", "added_at", "ticker", "option_ticker",
                "option_type", "expiry", "strike", "dte", "entry_price", "entry_underlying",
                "score", "premium", "vol_oi_ratio", "delta", "implied_volatility", "side",
                "captured_at")
_OUTCOME_COLS = ("signal_id", "horizon", "outcome_date", "labeled_at", "option_price",
                 "option_pnl_pct", "underlying_price", "raw_return", "signed_return",
                 "direction_correct")


def capture(db_path: str | None = None, pairs: list | None = None,
            today: str | None = None) -> int:
    """Record every watchlist entry not yet captured. Insert-only: a signal's
    entry prices and features are fixed the day it is added and never rewritten.
    Returns rows written."""
    pairs = _load_entries() if pairs is None else pairs
    rows = [r for r in (signal_row(e, a, today) for e, a in pairs) if r]
    if not rows:
        return 0
    sql = (f"INSERT OR IGNORE INTO watch_signals ({', '.join(_SIGNAL_COLS)}) "
           f"VALUES ({', '.join('?' * len(_SIGNAL_COLS))})")
    with _db(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM watch_signals").fetchone()[0]
        conn.executemany(sql, [tuple(r[c] for c in _SIGNAL_COLS) for r in rows])
        return conn.execute("SELECT COUNT(*) FROM watch_signals").fetchone()[0] - before


def label_due(db_path: str | None = None, snapshots: list | None = None,
              through: str | None = None) -> int:
    """Label every (signal, horizon) whose session is complete and has a snapshot.
    Idempotent: an outcome, once written, is never revised. Returns rows written."""
    snapshots = _load_snapshots() if snapshots is None else snapshots
    if not snapshots:
        return 0
    through = through or closed_through()
    session_dates = sorted({s["date"] for s in snapshots})
    by_entry: dict = {}
    for s in snapshots:
        by_entry.setdefault(s["entry_id"], []).append(s)
    wrote = 0
    sql = (f"INSERT OR IGNORE INTO watch_outcomes ({', '.join(_OUTCOME_COLS)}) "
           f"VALUES ({', '.join('?' * len(_OUTCOME_COLS))})")
    with _db(db_path) as conn:
        done: dict = {}
        for r in conn.execute("SELECT signal_id, horizon FROM watch_outcomes"):
            done.setdefault(r["signal_id"], set()).add(r["horizon"])
        want = {h for h, _ in HORIZONS}
        for sig in conn.execute("SELECT * FROM watch_signals").fetchall():
            have = done.get(sig["id"], set())
            if have >= want:
                continue
            rows = pick_outcomes(dict(sig), by_entry.get(sig["entry_id"], []), session_dates,
                                 have, through)
            if rows:
                conn.executemany(sql, [tuple(r[c] for c in _OUTCOME_COLS) for r in rows])
                wrote += len(rows)
    return wrote


def counts(db_path: str | None = None) -> tuple[int, int]:
    with _db(db_path) as conn:
        return (conn.execute("SELECT COUNT(*) FROM watch_signals").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM watch_outcomes").fetchone()[0])
