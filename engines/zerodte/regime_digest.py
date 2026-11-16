"""Daily regime digest — one small, permanent row per ticker per session.

The engine's regime (PIN / TREND / TRANSITION) is only recorded as a stream of
REGIME_CHANGE events: around seventy per ticker per day, pruned after
RETAIN_EVENT_DAYS. That is fine for replaying a recent session and useless for
asking, months later, "what was the regime that morning, and did it matter?".

This reduces each session's stream to one row that is never pruned:

  open_regime   the regime in force at OPEN_AT (09:45 ET) — fifteen minutes after
                the open, once the baseline is frozen and the first readings are
                in. This is the field to use for anything predictive: it is known
                early in the session.
  *_share       share of regular hours (09:30-16:00) spent in each regime. These
                describe the whole day, so they are NOT known in advance.
  close_regime, n_changes

`run` digests every finished session that has events but no row yet, so it is
self-healing: called from the engine's periodic maintenance, before the prune.
`seed` loads the bundled digest of the standalone tool's history (the events
themselves were not imported, and most were already past retention).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

log = logging.getLogger("engines.zerodte.regime")

OPEN_AT = "09:45"
RTH_START, RTH_END = "09:30", "16:00"
REGIMES = ("PIN", "TREND", "TRANSITION")
SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed", "regime_daily.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_daily (
    session_date     TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    open_regime      TEXT,            -- regime in force at 09:45 ET
    close_regime     TEXT,            -- regime in force at 16:00 ET
    n_changes        INTEGER NOT NULL,
    pin_share        REAL,            -- share of 09:30-16:00 spent in each regime
    trend_share      REAL,
    transition_share REAL,
    source           TEXT NOT NULL,   -- 'engine' | 'legacy'
    PRIMARY KEY (session_date, ticker)
);
"""

_COLS = ("session_date", "ticker", "open_regime", "close_regime", "n_changes",
         "pin_share", "trend_share", "transition_share", "source")


def _minutes(ts: str) -> int:
    """Minutes past midnight on the event's own (ET) wall clock."""
    t = datetime.fromisoformat(ts)
    return t.hour * 60 + t.minute


def _hhmm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:])


def digest_day(events: list) -> dict | None:
    """Reduce one ticker-session's REGIME_CHANGE stream to its digest.

    events: [(ts_iso, from_regime, to_regime)] in time order. Returns None when
    there is nothing to digest. The regime before the first event is that event's
    `from` (None at a cold start, when the engine had not classified yet)."""
    if not events:
        return None
    marks = [(_minutes(ts), frm, to) for ts, frm, to in events]

    def at(minute: int):
        cur, seen = None, False
        for m, frm, to in marks:
            if m <= minute:
                cur, seen = to, True
            else:
                if not seen:
                    cur = frm
                break
        return cur

    a, b = _hhmm(RTH_START), _hhmm(RTH_END)
    held = dict.fromkeys(REGIMES, 0)
    cur, t = at(a), a
    for m, frm, to in marks:
        if m <= a:
            continue
        if m >= b:
            break
        if cur is None:
            cur = frm
        if cur in held:
            held[cur] += m - t
        cur, t = to, m
    if cur in held:
        held[cur] += b - t
    total = sum(held.values())
    share = (lambda k: round(held[k] / total, 4)) if total else (lambda k: None)
    return {"open_regime": at(_hhmm(OPEN_AT)), "close_regime": at(b), "n_changes": len(marks),
            "pin_share": share("PIN"), "trend_share": share("TREND"),
            "transition_share": share("TRANSITION")}


def _rows_from_events(raw: list, source: str) -> list:
    """raw: [(session_date, ticker, ts, payload_json)] ordered by date, ticker, ts."""
    by: dict = {}
    for d, tk, ts, payload in raw:
        try:
            p = json.loads(payload)
        except (TypeError, ValueError):
            continue
        by.setdefault((d, tk), []).append((ts, p.get("from"), p.get("to")))
    out = []
    for (d, tk), evts in sorted(by.items()):
        dg = digest_day(evts)
        if dg:
            out.append({"session_date": d, "ticker": tk, "source": source, **dg})
    return out


def _insert(conn, rows: list) -> int:
    before = conn.execute("SELECT COUNT(*) FROM regime_daily").fetchone()[0]
    conn.executemany(
        f"INSERT OR IGNORE INTO regime_daily ({', '.join(_COLS)}) "
        f"VALUES ({', '.join('?' * len(_COLS))})",
        [tuple(r.get(c) for c in _COLS) for r in rows])
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM regime_daily").fetchone()[0] - before


def run(db, through: str) -> int:
    """Digest every session up to and including `through` (the last FINISHED
    session date) that has regime events but no digest row. `db` is the engine's
    DB object; its lock serialises this with the pollers' writes."""
    with db._lock:
        conn = db._conn
        conn.executescript(SCHEMA)
        raw = conn.execute(
            "SELECT e.session_date, e.ticker, e.ts, e.payload FROM events e "
            "WHERE e.type = 'REGIME_CHANGE' AND e.session_date <= ? AND NOT EXISTS ("
            "  SELECT 1 FROM regime_daily r WHERE r.session_date = e.session_date "
            "  AND r.ticker = e.ticker) "
            "ORDER BY e.session_date, e.ticker, e.ts", (through,)).fetchall()
        return _insert(conn, _rows_from_events(raw, "engine")) if raw else 0


def seed(db, path: str = SEED_PATH) -> int:
    """Load the bundled legacy digest. Insert-only, so the engine's own rows win
    and running it on every boot costs one no-op statement."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return 0
    with db._lock:
        db._conn.executescript(SCHEMA)
        return _insert(db._conn, [{**r, "source": "legacy"} for r in rows])


def digest_file(sqlite_path: str) -> list:
    """Digest rows for every session in a standalone 0dte.sqlite3 (read-only).
    Used once, offline, to build the bundled seed."""
    import sqlite3
    from urllib.parse import quote
    uri = "file:" + quote(os.path.abspath(sqlite_path).replace("\\", "/"), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        raw = con.execute("SELECT session_date, ticker, ts, payload FROM events "
                          "WHERE type = 'REGIME_CHANGE' ORDER BY session_date, ticker, ts").fetchall()
    finally:
        con.close()
    return [{k: v for k, v in r.items() if k != "source"} for r in _rows_from_events(raw, "legacy")]
