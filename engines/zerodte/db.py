"""SQLite persistence. Everything keyed by session date so a day can be
replayed and the classifier evaluated after the close."""
import json
import os
import sqlite3
import threading

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    session_date TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots ON snapshots(session_date, ticker, ts);

CREATE TABLE IF NOT EXISTS events (
    session_date TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events ON events(session_date, ts);

CREATE TABLE IF NOT EXISTS touches (
    session_date TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    touch_id     TEXT NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (session_date, ticker, touch_id)
);

CREATE TABLE IF NOT EXISTS prints (
    session_date TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    contract     TEXT NOT NULL,
    side         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    price        REAL NOT NULL,
    at_ask       INTEGER,
    low_conf     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prints ON prints(session_date, ticker, contract, ts);

CREATE TABLE IF NOT EXISTS bars (
    session_date TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (session_date, ticker, ts)
);

CREATE TABLE IF NOT EXISTS daily_option_volume (
    ticker       TEXT NOT NULL,
    session_date TEXT NOT NULL,
    volume       REAL NOT NULL,
    PRIMARY KEY (ticker, session_date)
);
"""


class DB:
    def __init__(self, path: str = config.DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def save_snapshot(self, session_date, ticker, ts, payload: dict):
        self._exec("INSERT INTO snapshots VALUES (?,?,?,?)",
                   (session_date, ticker, ts, json.dumps(payload)))

    def save_event(self, session_date, ticker, ts, type_, payload: dict):
        self._exec("INSERT INTO events VALUES (?,?,?,?,?)",
                   (session_date, ticker, ts, type_, json.dumps(payload)))

    def touch_label_stats(self, since: str | None = None) -> dict:
        """Aggregate resolved touches by flow label and price outcome — the level
        behaviour learning record. Answers: when a level was DEFENDED/ATTACKED/…,
        how often did price actually reject vs. breach? Spans the retained history.
        """
        sql, params = "SELECT payload FROM touches", ()
        if since:
            sql += " WHERE session_date >= ?"
            params = (since,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        labels: dict = {}
        n_total = 0
        for (payload,) in rows:
            try:
                t = json.loads(payload)
            except Exception:
                continue
            label = (t.get("label") or "").strip()
            outcome = (t.get("price_outcome") or "").strip().upper()
            if not label or not outcome:
                continue
            n_total += 1
            d = labels.setdefault(label, {"n": 0, "rejected": 0, "breached": 0, "churn": 0})
            d["n"] += 1
            if outcome == "REJECTED":
                d["rejected"] += 1
            elif outcome == "BREACHED":
                d["breached"] += 1
            else:
                d["churn"] += 1
        return {"n": n_total, "labels": labels}

    def upsert_touch(self, session_date, ticker, touch_id, payload: dict):
        self._exec("INSERT INTO touches VALUES (?,?,?,?) "
                   "ON CONFLICT(session_date,ticker,touch_id) DO UPDATE SET payload=excluded.payload",
                   (session_date, ticker, touch_id, json.dumps(payload)))

    def save_prints(self, session_date, ticker, rows):
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO prints VALUES (?,?,?,?,?,?,?,?,?)",
                [(session_date, ticker, r["ts"], r["contract"], r["side"],
                  r["size"], r["price"],
                  None if r.get("at_ask") is None else int(r["at_ask"]),
                  int(r["low_conf"])) for r in rows])
            self._conn.commit()

    def save_bar(self, session_date, ticker, ts, o, h, l, c, v):
        self._exec("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                   (session_date, ticker, ts, o, h, l, c, v))

    def save_daily_option_volume(self, ticker, session_date, volume):
        self._exec("INSERT OR REPLACE INTO daily_option_volume VALUES (?,?,?)",
                   (ticker, session_date, volume))

    def avg_daily_option_volume(self, ticker, exclude_date, n=20):
        """Average total 0DTE volume over prior stored sessions.
        Returns (avg, count) — count tells the UI how much history backs it."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT volume FROM daily_option_volume WHERE ticker=? AND session_date<? "
                "ORDER BY session_date DESC LIMIT ?", (ticker, exclude_date, n)).fetchall()
        if not rows:
            return None, 0
        vols = [r[0] for r in rows]
        return sum(vols) / len(vols), len(vols)

    def prune(self, today_iso: str) -> dict:
        """Delete rows past their retention windows and reclaim the space.
        daily_option_volume is never pruned — it is the 20-day learning
        baseline and costs one row per ticker per day."""
        from datetime import date, timedelta
        today = date.fromisoformat(today_iso)

        def cutoff(days):
            return (today - timedelta(days=days)).isoformat()

        plan = [("snapshots", config.RETAIN_SNAPSHOT_DAYS),
                ("prints", config.RETAIN_PRINT_DAYS),
                ("bars", config.RETAIN_BAR_DAYS),
                ("events", config.RETAIN_EVENT_DAYS),
                ("touches", config.RETAIN_TOUCH_DAYS)]
        deleted = {}
        with self._lock:
            for table, keep in plan:
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE session_date < ?",
                    (cutoff(keep),))
                deleted[table] = cur.rowcount
            self._conn.commit()
            if any(deleted.values()):
                self._conn.execute("VACUUM")
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self._path + suffix)
            except OSError:
                pass
        return total

    def list_sessions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_date, COUNT(*) FROM snapshots "
                "GROUP BY session_date ORDER BY session_date DESC").fetchall()
        return [{"date": r[0], "snapshots": r[1]} for r in rows]

    def load_replay(self, session_date):
        with self._lock:
            snaps = self._conn.execute(
                "SELECT ticker, ts, payload FROM snapshots WHERE session_date=? ORDER BY ts",
                (session_date,)).fetchall()
            events = self._conn.execute(
                "SELECT ticker, ts, type, payload FROM events WHERE session_date=? ORDER BY ts",
                (session_date,)).fetchall()
            touches = self._conn.execute(
                "SELECT ticker, touch_id, payload FROM touches WHERE session_date=?",
                (session_date,)).fetchall()
            bars = self._conn.execute(
                "SELECT ticker, ts, o,h,l,c,v FROM bars WHERE session_date=? ORDER BY ts",
                (session_date,)).fetchall()
        out = {"snapshots": {}, "events": [], "touches": {}, "bars": {}}
        for ticker, ts, payload in snaps:
            out["snapshots"].setdefault(ticker, []).append({"ts": ts, **json.loads(payload)})
        for ticker, ts, type_, payload in events:
            out["events"].append({"ticker": ticker, "ts": ts, "type": type_,
                                  "payload": json.loads(payload)})
        for ticker, touch_id, payload in touches:
            out["touches"].setdefault(ticker, []).append(json.loads(payload))
        for ticker, ts, o, h, l, c, v in bars:
            out["bars"].setdefault(ticker, []).append(
                {"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v})
        return out
