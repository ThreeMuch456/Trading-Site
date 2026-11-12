"""Desk learning store — the recursive-learning substrate.

The desk keeps its own small SQLite journal of every session's composed signals
(risk rating, per-ticker AI bias, the scored structure recs, the flow EOD lean)
and an independent log of graded bias outcomes. Over time this is a desk-owned
record — separate from each upstream tool's own loop — that the EOD Track Record
synthesizes alongside them.

Writes are best-effort and never block a page: every public call swallows DB
errors (a read-only / missing volume just means no desk history this run).
"""

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

DB_PATH = os.environ.get("DESK_DB_PATH",
                         str(Path(__file__).resolve().parent.parent / "data" / "desk.sqlite3"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_signals (
  date         TEXT PRIMARY KEY,
  recorded_at  TEXT,
  risk_rating  TEXT,
  biases       TEXT,   -- JSON {sym: {bias, conviction}}
  structure    TEXT,   -- JSON [{sym, score, rotation, live}]
  flow_eod     TEXT    -- JSON [{sym, lean, call_pct}]
);
CREATE TABLE IF NOT EXISTS graded_biases (
  date    TEXT,
  ticker  TEXT,
  bias    TEXT,
  correct INTEGER,     -- 1 / 0
  PRIMARY KEY (date, ticker)
);
"""


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def record_snapshot(s: dict, when: date | None = None) -> None:
    """Upsert today's composed signals (one row per session date)."""
    try:
        d = (when or date.today()).isoformat()
        biases = {t["sym"]: {"bias": t.get("bias"), "conviction": t.get("conviction")}
                  for t in s.get("tickers", [])}
        structure = [{"sym": r.get("sym"), "score": r.get("score"),
                      "rotation": r.get("rotation"), "live": r.get("live")}
                     for r in (s.get("structure", {}) or {}).get("recs", [])]
        flow_eod = [{"sym": e.get("sym"), "lean": e.get("lean"), "call_pct": e.get("call_pct")}
                    for e in (s.get("flow", {}) or {}).get("eod", [])]
        with _conn() as c:
            c.execute(
                "INSERT INTO daily_signals(date, recorded_at, risk_rating, biases, structure, flow_eod) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
                "recorded_at=excluded.recorded_at, risk_rating=excluded.risk_rating, "
                "biases=excluded.biases, structure=excluded.structure, flow_eod=excluded.flow_eod",
                (d, datetime.now().isoformat(timespec="seconds"),
                 (s.get("context", {}) or {}).get("risk_rating"),
                 json.dumps(biases), json.dumps(structure), json.dumps(flow_eod)))
    except Exception:
        pass


def ingest_grades(grades: list, when: date | None = None) -> None:
    """Record graded bias outcomes (desk-owned copy) from the EOD grade rows."""
    try:
        d = (when or date.today()).isoformat()
        with _conn() as c:
            for g in grades or []:
                correct = g.get("correct")
                bc = 1 if correct == "Yes" else (0 if correct == "No" else None)
                if bc is None:
                    continue
                c.execute(
                    "INSERT INTO graded_biases(date, ticker, bias, correct) VALUES(?,?,?,?) "
                    "ON CONFLICT(date, ticker) DO UPDATE SET bias=excluded.bias, correct=excluded.correct",
                    (d, g.get("sym"), g.get("bias"), bc))
    except Exception:
        pass


def summary() -> dict:
    try:
        with _conn() as c:
            sessions = c.execute("SELECT COUNT(*) n FROM daily_signals").fetchone()["n"]
            since = c.execute("SELECT MIN(date) d FROM daily_signals").fetchone()["d"]
            g = c.execute("SELECT COUNT(*) t, COALESCE(SUM(correct),0) ok FROM graded_biases").fetchone()
            return {"sessions": sessions, "since": since, "graded": g["t"] or 0, "correct": g["ok"] or 0}
    except Exception:
        return {"sessions": 0, "since": None, "graded": 0, "correct": 0}


def track_record_items() -> list:
    """Desk-owned learning lines for the EOD Track Record block."""
    s = summary()
    items = []
    if s["sessions"]:
        line = f"Desk journal: {s['sessions']} session{'s' if s['sessions'] != 1 else ''} logged"
        if s["since"]:
            line += f" since {s['since']}"
        items.append({"src": "Desk", "text": line})
    if s["graded"]:
        items.append({"src": "Desk",
                      "text": f"desk-tracked bias hit-rate {s['correct']}/{s['graded']} "
                              f"({s['correct'] / s['graded'] * 100:.0f}%)"})
    return items
