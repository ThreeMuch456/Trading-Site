"""One-time, idempotent import of the standalone tools' learning history.

The merged desk started every learning loop from an empty database, while months
of labeled history sat in the old standalone services' own SQLite files. This
module merges those files into the desk's live stores so the loops resume from
that history instead of re-earning it.

How it is used
  1. Copy each legacy database off its old Railway volume.
  2. Upload it to the desk's volume under ``<data dir>/legacy/`` using one of the
     file names in ``SOURCES`` below.
  3. Restart / redeploy. ``run_pending()`` (called once at boot, before the
     scheduler starts) merges whatever is there, writes a report to
     ``legacy/import_report.json`` (surfaced on /healthz) and renames each
     imported file to ``<name>.imported`` so it is never read twice.

Safety
  - Additive only: rows are inserted, never deleted, with one narrow exception
    for the Brief (see ``import_agent``). The desk's own rows win any conflict.
  - Idempotent: every insert is keyed on the table's natural key, so a second run
    over the same file adds nothing.
  - One transaction per source: a failure rolls that source back and leaves the
    file in place for another attempt; other sources still import.

Sources
  journal.db        Trading-Agent (AM Brief): sessions, outcomes, reviews
  touches.sqlite3   0DTE-Assistant: touches + daily_option_volume. The full
  0dte.sqlite3      0dte.sqlite3 works too; ``slim_zerodte`` cuts it to the two
                    tables that matter so the upload is a few MB, not hundreds.
  opportunities.db  Structure-Identifier: opportunities, outcomes, flow_daily, meta

CLI (local prep):  python -m engines.legacy_import slim-zerodte <src> <dst>
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from . import config as econf

log = logging.getLogger("engines.legacy_import")

REPORT_NAME = "import_report.json"

_AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL UNIQUE,
    fetched_at   TEXT    NOT NULL,
    market_data  TEXT    NOT NULL,
    macro_data   TEXT    NOT NULL,
    options_data TEXT    NOT NULL,
    gameplan     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    ticker       TEXT    NOT NULL,
    open         REAL,
    close        REAL,
    day_pct      REAL,
    bias_correct INTEGER,
    bull_triggered INTEGER,
    bear_triggered INTEGER,
    notes        TEXT,
    UNIQUE(session_id, ticker)
);
CREATE TABLE IF NOT EXISTS reviews (
    session_id   INTEGER PRIMARY KEY REFERENCES sessions(id),
    generated_at TEXT NOT NULL,
    data         TEXT NOT NULL,
    narrative    TEXT
);
"""

_SESSION_COLS = "date, fetched_at, market_data, macro_data, options_data, gameplan"
_OUTCOME_COLS = ("ticker, open, close, day_pct, bias_correct, bull_triggered, "
                 "bear_triggered, notes")


def legacy_dir(data_dir: str | None = None) -> str:
    return os.path.join(data_dir or econf.DATA_DIR, "legacy")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uri(path: str) -> str:
    return "file:" + quote(os.path.abspath(path).replace("\\", "/"), safe="/:")


def _open(dst: str, src: str) -> sqlite3.Connection:
    """Open the live store with the legacy file attached READ-ONLY as `legacy`.
    The main connection must itself be opened with uri=True for the ATTACH's
    mode=ro to be honoured."""
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    con = sqlite3.connect(_uri(dst), uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS legacy", (_uri(src) + "?mode=ro",))
    return con


def _tables(con: sqlite3.Connection, schema: str) -> dict:
    """{table name: CREATE sql} for one attached schema ('main' or 'legacy')."""
    return {r["name"]: r["sql"] for r in con.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(con: sqlite3.Connection, schema: str, table: str) -> list:
    return [r["name"] for r in con.execute(f"PRAGMA {schema}.table_info([{table}])")]


def _has_key(con: sqlite3.Connection, table: str) -> bool:
    """True when main.<table> has a primary key or unique index — i.e. INSERT OR
    IGNORE can dedupe. Without one, a re-run would duplicate rows."""
    if any(r["pk"] for r in con.execute(f"PRAGMA main.table_info([{table}])")):
        return True
    return any(r["unique"] for r in con.execute(f"PRAGMA main.index_list([{table}])"))


def _count(con: sqlite3.Connection, schema: str, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {schema}.[{table}]").fetchone()[0]


def merge_tables(src: str, dst: str, tables: list) -> dict:
    """Additively merge whole tables that carry their own natural key.

    For each table: create it in the live store if missing (using the legacy
    file's own CREATE statement), then INSERT OR IGNORE every legacy row over the
    columns both sides share. Existing live rows always win. A table with no key
    is only copied into an empty live table, so it can't duplicate on a re-run.
    Returns {table: {"legacy": n, "added": n}}."""
    con = _open(dst, src)
    out: dict = {}
    try:
        con.execute("BEGIN IMMEDIATE")
        legacy = _tables(con, "legacy")
        live = _tables(con, "main")
        for t in tables:
            if t not in legacy:
                out[t] = {"legacy": 0, "added": 0, "note": "not in legacy file"}
                continue
            if t not in live:
                con.execute(legacy[t])
            cols = [c for c in _columns(con, "legacy", t) if c in _columns(con, "main", t)]
            n_legacy = _count(con, "legacy", t)
            before = _count(con, "main", t)
            if not _has_key(con, t) and before:
                out[t] = {"legacy": n_legacy, "added": 0,
                          "note": "no key and live table not empty — skipped"}
                continue
            col_sql = ", ".join(f"[{c}]" for c in cols)
            con.execute(f"INSERT OR IGNORE INTO main.[{t}] ({col_sql}) "
                        f"SELECT {col_sql} FROM legacy.[{t}]")
            out[t] = {"legacy": n_legacy, "added": _count(con, "main", t) - before}
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------
# Per-source imports
# ---------------------------------------------------------------------------

def import_agent(src: str, dst: str) -> dict:
    """Merge the old AM Brief journal into the agent engine's journal.

    Sessions are keyed by date, but outcomes/reviews hang off an autoincrement
    session id, so ids are remapped per row rather than copied.

    Per legacy session:
      - date not in the live journal      -> insert it with its outcomes + review
      - date exists, live has NO graded outcome, legacy does
                                          -> take the legacy brief, outcomes and
                                             review for that date. The live row
                                             was never scored, so nothing learned
                                             is lost, and outcomes always grade
                                             the brief stored beside them.
      - otherwise                         -> keep the live row untouched
    Legacy sessions dated after the last graded legacy session are skipped: they
    are ungraded previews, and the desk generates its own."""
    con = _open(dst, src)
    stats = {"sessions_added": 0, "sessions_replaced": 0, "sessions_kept": 0,
             "previews_skipped": 0, "outcomes_added": 0, "reviews_added": 0}
    try:
        con.executescript(_AGENT_SCHEMA)
        con.execute("BEGIN IMMEDIATE")
        has_reviews = "reviews" in _tables(con, "legacy")
        last_graded = con.execute(
            "SELECT MAX(s.date) FROM legacy.sessions s JOIN legacy.outcomes o "
            "ON o.session_id = s.id WHERE o.bias_correct IS NOT NULL").fetchone()[0]
        graded_sql = ("SELECT COUNT(*) FROM {schema}.outcomes "
                      "WHERE session_id = ? AND bias_correct IS NOT NULL")

        for s in con.execute(f"SELECT id, {_SESSION_COLS} FROM legacy.sessions "
                             "ORDER BY date").fetchall():
            if last_graded is None or s["date"] > last_graded:
                stats["previews_skipped"] += 1
                continue
            live = con.execute("SELECT id FROM main.sessions WHERE date = ?",
                               (s["date"],)).fetchone()
            if live is None:
                cur = con.execute(
                    f"INSERT INTO main.sessions ({_SESSION_COLS}) VALUES (?,?,?,?,?,?)",
                    tuple(s[c.strip()] for c in _SESSION_COLS.split(",")))
                new_id = cur.lastrowid
                stats["sessions_added"] += 1
            else:
                new_id = live["id"]
                live_graded = con.execute(graded_sql.format(schema="main"),
                                          (new_id,)).fetchone()[0]
                legacy_graded = con.execute(graded_sql.format(schema="legacy"),
                                            (s["id"],)).fetchone()[0]
                if live_graded or not legacy_graded:
                    stats["sessions_kept"] += 1
                    continue
                con.execute(
                    "UPDATE main.sessions SET fetched_at=?, market_data=?, macro_data=?, "
                    "options_data=?, gameplan=? WHERE id=?",
                    (s["fetched_at"], s["market_data"], s["macro_data"],
                     s["options_data"], s["gameplan"], new_id))
                con.execute("DELETE FROM main.outcomes WHERE session_id = ?", (new_id,))
                con.execute("DELETE FROM main.reviews WHERE session_id = ?", (new_id,))
                stats["sessions_replaced"] += 1

            cur = con.execute(
                f"INSERT OR IGNORE INTO main.outcomes (session_id, {_OUTCOME_COLS}) "
                f"SELECT ?, {_OUTCOME_COLS} FROM legacy.outcomes WHERE session_id = ?",
                (new_id, s["id"]))
            stats["outcomes_added"] += cur.rowcount
            if has_reviews:
                cur = con.execute(
                    "INSERT OR IGNORE INTO main.reviews (session_id, generated_at, data, narrative) "
                    "SELECT ?, generated_at, data, narrative FROM legacy.reviews "
                    "WHERE session_id = ?", (new_id, s["id"]))
                stats["reviews_added"] += cur.rowcount
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return stats


def import_zerodte(src: str, dst: str) -> dict:
    """Merge 0DTE level touches (the learning record) and the daily 0DTE volume
    history (the 20-day baseline) — both keyed, so a plain additive merge."""
    return merge_tables(src, dst, ["touches", "daily_option_volume"])


def import_structure(src: str, dst: str) -> dict:
    """Merge the thin-zone opportunity log. Opportunities are keyed
    "{run_date}:{ticker}:{type}" and outcomes (opp_id, horizon), so legacy rows
    slot in beside the desk's own; opportunities too recent to be labeled yet are
    picked up by the desk's normal daily labeling pass."""
    return merge_tables(src, dst, ["meta", "opportunities", "outcomes", "flow_daily"])


def slim_zerodte(src: str, dst: str) -> dict:
    """Cut a full legacy 0dte.sqlite3 (hundreds of MB of replay data) down to the
    two tables the import reads, so the upload to the desk volume is small."""
    if os.path.exists(dst):
        os.remove(dst)
    return merge_tables(src, dst, ["touches", "daily_option_volume"])


# (file name in legacy/, live store file name under DATA_DIR, label, importer)
SOURCES = [
    ("journal.db",       "agent.sqlite3",    "agent",     import_agent),
    ("touches.sqlite3",  "zerodte.sqlite3",  "zerodte",   import_zerodte),
    ("0dte.sqlite3",     "zerodte.sqlite3",  "zerodte",   import_zerodte),
    ("opportunities.db", "opportunities.db", "structure", import_structure),
]


# ---------------------------------------------------------------------------
# Boot hook + report
# ---------------------------------------------------------------------------

def last_report(data_dir: str | None = None):
    """The persisted report of what has been imported so far, or None."""
    try:
        with open(os.path.join(legacy_dir(data_dir), REPORT_NAME), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def run_pending(data_dir: str | None = None) -> dict:
    """Import every legacy file waiting in <data dir>/legacy/. Safe to call on
    every boot: with nothing waiting it returns immediately."""
    ddir = data_dir or econf.DATA_DIR
    ldir = legacy_dir(ddir)
    if not os.path.isdir(ldir):
        return {}
    report = last_report(ddir) or {}
    ran = {}
    for fname, store, label, fn in SOURCES:
        src = os.path.join(ldir, fname)
        if not os.path.isfile(src):
            continue
        entry = {"file": fname, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            entry["result"] = fn(src, os.path.join(ddir, store))
            entry["status"] = "ok"
            os.replace(src, src + ".imported")
            log.info("legacy import %s (%s): %s", label, fname, entry["result"])
        except Exception as exc:  # noqa: BLE001 - one bad file must not block the others
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("legacy import %s (%s) failed", label, fname)
        ran[label] = report[label] = entry
    if ran:
        try:
            with open(os.path.join(ldir, REPORT_NAME), "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except Exception:
            log.warning("could not write legacy import report", exc_info=True)
    return ran


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "slim-zerodte":
        print(json.dumps(slim_zerodte(sys.argv[2], sys.argv[3]), indent=2))
    else:
        print(__doc__)
        sys.exit(2)
