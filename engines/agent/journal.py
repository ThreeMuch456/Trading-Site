"""
Trading journal — step 4 of the trading agent.
Records each morning's gameplan and end-of-day outcomes in SQLite.

Usage:
  python journal.py log       --date 2026-06-16   # save today's brief output to journal
  python journal.py eod       --date 2026-06-16   # record what actually happened (manual)
  python journal.py autograde --date 2026-06-16   # auto-score outcomes via Massive.com/yfinance
  python journal.py show      --date 2026-06-16   # print a saved gameplan
  python journal.py review                        # accuracy summary across all logged days
  python journal.py list                          # list all logged dates
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime

from .market_calendar import market_today
from pathlib import Path

# DB_PATH defaults to a file next to this module, but can be overridden via the
# DB_PATH env var so it can point at a mounted persistent volume (e.g. on Railway,
# where the container filesystem is ephemeral and wiped on every deploy).
DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "journal.db"))
TICKERS = ["SPY", "QQQ", "IWM"]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL UNIQUE,   -- YYYY-MM-DD
    fetched_at   TEXT    NOT NULL,
    market_data  TEXT    NOT NULL,          -- JSON blob
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
    day_pct      REAL,                      -- actual % change on the day
    bias_correct INTEGER,                   -- 1=yes 0=no NULL=unscored
    bull_triggered INTEGER,                 -- 1 if bull case played out
    bear_triggered INTEGER,                 -- 1 if bear case played out
    notes        TEXT,
    UNIQUE(session_id, ticker)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_log(args):
    """Save a full morning brief JSON (piped from run_morning_brief.py --json) to the journal."""
    raw = sys.stdin.read().strip()
    if not raw:
        print("ERROR: pipe JSON from run_morning_brief.py --json", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON — {e}", file=sys.stderr)
        sys.exit(1)

    log_date = args.date or data.get("fetched_at", "")[:10] or market_today().isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO sessions (date, fetched_at, market_data, macro_data, options_data, gameplan)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 fetched_at   = excluded.fetched_at,
                 market_data  = excluded.market_data,
                 macro_data   = excluded.macro_data,
                 options_data = excluded.options_data,
                 gameplan     = excluded.gameplan""",
            (
                log_date,
                data.get("fetched_at", ""),
                json.dumps(data.get("market_data", {})),
                json.dumps(data.get("macro_data", {})),
                json.dumps(data.get("options_data", {})),
                json.dumps(data.get("gameplan", {})),
            ),
        )
        conn.commit()
        print(f"Logged session for {log_date}.")
    finally:
        conn.close()


def cmd_eod(args):
    """Interactively record end-of-day outcomes for a session."""
    log_date = args.date or market_today().isoformat()
    conn = get_conn()

    row = conn.execute("SELECT * FROM sessions WHERE date = ?", (log_date,)).fetchone()
    if not row:
        print(f"No session found for {log_date}. Run 'log' first.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    session_id = row["id"]
    gameplan = json.loads(row["gameplan"])
    ticker_plans = gameplan.get("gameplan", {})

    print(f"\nEnd-of-day outcomes for {log_date}")
    print("-" * 40)

    for ticker in TICKERS:
        plan = ticker_plans.get(ticker, {})
        print(f"\n{ticker}")
        print(f"  Bias:      {plan.get('bias', 'n/a')}")
        print(f"  Bull case: {plan.get('bull_case', 'n/a')}")
        print(f"  Bear case: {plan.get('bear_case', 'n/a')}")

        try:
            open_px  = float(input("  Open price:  ") or 0) or None
            close_px = float(input("  Close price: ") or 0) or None
            day_pct  = round((close_px - open_px) / open_px * 100, 2) if open_px and close_px else None

            bull_hit = input("  Bull case triggered? (y/n/blank): ").strip().lower()
            bear_hit = input("  Bear case triggered? (y/n/blank): ").strip().lower()
            correct  = input("  Bias correct? (y/n/blank): ").strip().lower()
            notes    = input("  Notes: ").strip() or None
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            conn.close()
            sys.exit(0)

        conn.execute(
            """INSERT INTO outcomes
                 (session_id, ticker, open, close, day_pct,
                  bias_correct, bull_triggered, bear_triggered, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, ticker) DO UPDATE SET
                 open=excluded.open, close=excluded.close, day_pct=excluded.day_pct,
                 bias_correct=excluded.bias_correct, bull_triggered=excluded.bull_triggered,
                 bear_triggered=excluded.bear_triggered, notes=excluded.notes""",
            (
                session_id, ticker, open_px, close_px, day_pct,
                1 if correct == "y" else (0 if correct == "n" else None),
                1 if bull_hit == "y" else (0 if bull_hit == "n" else None),
                1 if bear_hit == "y" else (0 if bear_hit == "n" else None),
                notes,
            ),
        )

    conn.commit()
    conn.close()
    print(f"\nOutcomes saved for {log_date}.")


def cmd_autograde(args):
    """Auto-score end-of-day outcomes from actual price bars (Massive.com, yfinance fallback)."""
    import os
    from .grade_outcomes import autograde_date

    log_date = args.date or market_today().isoformat()
    try:
        result = autograde_date(log_date, os.environ.get("MASSIVE_API_KEY"))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if "skipped" in result:
        print(result["skipped"])
        return

    print(f"\nAuto-graded {log_date}")
    print("-" * 40)
    for ticker, o in result.items():
        if "error" in o:
            print(f"  {ticker}: {o['error']}")
            continue
        correct = {1: "YES", 0: "NO", None: "?"}.get(o["bias_correct"], "?")
        print(f"  {ticker}  open={o['open']}  close={o['close']}  "
              f"day={o['day_pct']:+.2f}%  bias_correct={correct}  {o['notes']}")
    print()


def cmd_show(args):
    """Print the saved gameplan for a date."""
    log_date = args.date or market_today().isoformat()
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE date = ?", (log_date,)).fetchone()
    conn.close()

    if not row:
        print(f"No session found for {log_date}.")
        sys.exit(1)

    gameplan = json.loads(row["gameplan"])
    sep = "-" * 60

    print(f"\n{'=' * 60}")
    print(f"  GAMEPLAN  {log_date}  (fetched {row['fetched_at']})")
    print(f"{'=' * 60}\n")
    print(f"MARKET STRUCTURE\n{sep}")
    print(gameplan.get("market_structure", ""))
    print(f"\nMACRO CONTEXT\n{sep}")
    print(gameplan.get("macro_context", ""))
    print(f"\nGAMMA SUMMARY\n{sep}")
    print(gameplan.get("gamma_summary", ""))
    print(f"\nOVERALL RISK: {gameplan.get('overall_risk_rating', '').upper()}")
    print(f"\nGAMEPLAN\n{sep}")
    for ticker, plan in gameplan.get("gameplan", {}).items():
        print(f"\n  {ticker}  [{plan.get('bias','').upper()}]")
        print(f"  Bull:  {plan.get('bull_case','')}")
        print(f"  Bear:  {plan.get('bear_case','')}")
        levels = plan.get("key_levels", [])
        if levels:
            print(f"  Levels: {' | '.join(levels)}")
        flags = plan.get("risk_flags", [])
        if flags:
            print(f"  Flags:  {' | '.join(flags)}")
    watch = gameplan.get("watch_list", [])
    if watch:
        print(f"\nWATCH TODAY\n{sep}")
        for i, item in enumerate(watch, 1):
            print(f"  {i}. {item}")

    # Show outcomes if recorded
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM outcomes WHERE session_id = ? ORDER BY ticker", (row["id"],)
    ).fetchall()
    conn.close()

    if rows:
        print(f"\nOUTCOMES\n{sep}")
        for o in rows:
            correct = {1: "YES", 0: "NO", None: "?"}.get(o["bias_correct"], "?")
            print(f"  {o['ticker']}  open={o['open']}  close={o['close']}  "
                  f"day={o['day_pct']:+.2f}%  bias_correct={correct}"
                  + (f"  notes: {o['notes']}" if o["notes"] else ""))

    print(f"\n{'=' * 60}\n")


def cmd_list(args):
    """List all logged dates."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.date, s.fetched_at, COUNT(o.id) as outcomes "
        "FROM sessions s LEFT JOIN outcomes o ON o.session_id = s.id "
        "GROUP BY s.id ORDER BY s.date DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No sessions logged yet.")
        return

    print(f"\n{'Date':<14} {'Fetched at':<22} {'Outcomes'}")
    print("-" * 45)
    for r in rows:
        print(f"{r['date']:<14} {r['fetched_at']:<22} {r['outcomes']}/{len(TICKERS)}")
    print()


def cmd_review(args):
    """Print accuracy summary across all scored sessions."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.date, o.ticker, o.day_pct, o.bias_correct,
                  o.bull_triggered, o.bear_triggered, o.notes,
                  json_extract(s.gameplan, '$.gameplan.' || o.ticker || '.bias') as bias
           FROM outcomes o
           JOIN sessions s ON s.id = o.session_id
           WHERE o.bias_correct IS NOT NULL
           ORDER BY s.date DESC, o.ticker"""
    ).fetchall()
    conn.close()

    if not rows:
        print("No scored outcomes yet. Use 'eod' to record outcomes.")
        return

    total = len(rows)
    correct = sum(1 for r in rows if r["bias_correct"] == 1)
    accuracy = correct / total * 100 if total else 0

    print(f"\n{'=' * 60}")
    print(f"  ACCURACY REVIEW  ({total} scored trades)")
    print(f"{'=' * 60}")
    print(f"\n  Overall bias accuracy: {correct}/{total}  ({accuracy:.1f}%)\n")

    # Per-ticker breakdown
    for ticker in TICKERS:
        t_rows = [r for r in rows if r["ticker"] == ticker]
        if not t_rows:
            continue
        t_correct = sum(1 for r in t_rows if r["bias_correct"] == 1)
        print(f"  {ticker}: {t_correct}/{len(t_rows)} ({t_correct/len(t_rows)*100:.0f}%)")

    print(f"\n{'Date':<14} {'Ticker':<8} {'Bias':<12} {'Day%':>7} {'Correct'}")
    print("-" * 55)
    for r in rows:
        correct_str = {1: "YES", 0: "NO"}.get(r["bias_correct"], "?")
        day_str = f"{r['day_pct']:+.2f}%" if r["day_pct"] is not None else "n/a"
        print(f"{r['date']:<14} {r['ticker']:<8} {(r['bias'] or 'n/a'):<12} "
              f"{day_str:>7}  {correct_str}")

    print(f"\n{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Performance stats (used by synthesize_gameplan and web dashboard)
# ---------------------------------------------------------------------------

def get_performance_stats(n_sessions: int = 30) -> dict:
    """Return accuracy stats for the last n_sessions scored sessions."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT s.date, o.ticker, o.day_pct, o.bias_correct,
                   o.bull_triggered, o.bear_triggered, o.notes,
                   json_extract(s.gameplan, '$.overall_risk_rating') as risk_rating,
                   json_extract(s.gameplan, '$.gameplan.' || o.ticker || '.bias') as bias,
                   json_extract(s.gameplan, '$.gameplan.' || o.ticker || '.conviction') as conviction,
                   json_extract(s.options_data, '$.options.' || o.ticker || '.gamma_environment') as gamma_env
            FROM outcomes o
            JOIN sessions s ON s.id = o.session_id
            WHERE o.bias_correct IS NOT NULL
            ORDER BY s.date DESC
            LIMIT ?
        """, (n_sessions * len(TICKERS),)).fetchall()

        if not rows:
            return {
                "total": 0, "correct": 0, "accuracy": 0,
                "sessions_scored": 0, "by_ticker": {}, "by_bias": {},
                "by_gamma": {}, "by_risk": {}, "recent": [],
                "baselines": {}, "triggers": {}, "brier": None,
            }

        def _stats(group: list) -> dict:
            t = len(group)
            c = sum(1 for r in group if r["bias_correct"] == 1)
            return {"correct": c, "total": t, "accuracy": round(c / t, 3) if t else 0}

        buckets: dict = {"ticker": {}, "bias": {}, "gamma": {}, "risk": {}}
        for r in rows:
            buckets["ticker"].setdefault(r["ticker"], []).append(r)
            buckets["bias"].setdefault((r["bias"] or "unknown").lower(), []).append(r)
            buckets["gamma"].setdefault(r["gamma_env"] or "unknown", []).append(r)
            buckets["risk"].setdefault(r["risk_rating"] or "unknown", []).append(r)

        total = len(rows)
        correct = sum(1 for r in rows if r["bias_correct"] == 1)

        # Baselines — what the model's accuracy has to beat to add value.
        # Mirror the grader's scratch band so the comparison is apples-to-apples.
        try:
            from .grade_outcomes import DIRECTIONAL_SCRATCH_PCT as _scratch
        except Exception:
            _scratch = 0.15
        moved = [r for r in rows if r["day_pct"] is not None and abs(r["day_pct"]) >= _scratch]
        always_bull = _stats([])  # placeholder shape when no movers
        if moved:
            c = sum(1 for r in moved if r["day_pct"] > 0)
            always_bull = {"correct": c, "total": len(moved),
                           "accuracy": round(c / len(moved), 3)}
        # Persistence: predict today's direction = previous scored day's direction.
        by_tk: dict = {}
        for r in rows:
            by_tk.setdefault(r["ticker"], []).append(r)
        p_correct = p_total = 0
        for tk_rows in by_tk.values():
            ordered = sorted(tk_rows, key=lambda r: r["date"])
            for prev, cur in zip(ordered, ordered[1:]):
                if prev["day_pct"] is None or cur["day_pct"] is None:
                    continue
                if abs(prev["day_pct"]) < _scratch or abs(cur["day_pct"]) < _scratch:
                    continue
                p_total += 1
                if (prev["day_pct"] > 0) == (cur["day_pct"] > 0):
                    p_correct += 1
        baselines = {
            "always_bullish": always_bull,
            "persistence": {"correct": p_correct, "total": p_total,
                            "accuracy": round(p_correct / p_total, 3) if p_total else 0},
        }

        # Bull/bear trigger hit rates — grades the levels separately from the bias.
        def _trig(field: str) -> dict:
            scored = [r for r in rows if r[field] is not None]
            hits = sum(1 for r in scored if r[field] == 1)
            return {"correct": hits, "total": len(scored),
                    "accuracy": round(hits / len(scored), 3) if scored else 0}
        triggers = {"bull": _trig("bull_triggered"), "bear": _trig("bear_triggered")}

        # Brier score over rows where the model stated a conviction (lower is
        # better; 0.25 = always saying 50/50, so under 0.25 means calibrated skill).
        conv_rows = [r for r in rows if r["conviction"] is not None]
        brier = round(sum((float(r["conviction"]) - r["bias_correct"]) ** 2
                          for r in conv_rows) / len(conv_rows), 3) if conv_rows else None

        return {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 3) if total else 0,
            "sessions_scored": len(set(r["date"] for r in rows)),
            "by_ticker": {k: _stats(v) for k, v in buckets["ticker"].items()},
            "by_bias":   {k: _stats(v) for k, v in buckets["bias"].items()},
            "by_gamma":  {k: _stats(v) for k, v in buckets["gamma"].items()},
            "by_risk":   {k: _stats(v) for k, v in buckets["risk"].items()},
            "baselines": baselines,
            "triggers":  triggers,
            "brier":     brier,
            "recent": [
                {
                    "date": r["date"], "ticker": r["ticker"],
                    "bias": r["bias"] or "?", "day_pct": r["day_pct"],
                    "conviction": r["conviction"],
                    "bias_correct": r["bias_correct"],
                    "bull_triggered": r["bull_triggered"],
                    "bear_triggered": r["bear_triggered"],
                    "gamma_env": r["gamma_env"] or "?",
                    "risk_rating": r["risk_rating"] or "?",
                    "notes": r["notes"] or "",
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Trading journal")
    sub = parser.add_subparsers(dest="cmd")

    p_log = sub.add_parser("log", help="Save today's brief (pipe JSON via stdin)")
    p_log.add_argument("--date", default=None, help="Override date (YYYY-MM-DD)")

    p_eod = sub.add_parser("eod", help="Record end-of-day outcomes")
    p_eod.add_argument("--date", default=None)

    p_auto = sub.add_parser("autograde", help="Auto-score outcomes via Massive.com/yfinance")
    p_auto.add_argument("--date", default=None)

    p_show = sub.add_parser("show", help="Show a saved gameplan")
    p_show.add_argument("--date", default=None)

    sub.add_parser("list", help="List all logged dates")
    sub.add_parser("review", help="Accuracy summary")

    args = parser.parse_args()

    dispatch = {
        "log":       cmd_log,
        "eod":       cmd_eod,
        "autograde": cmd_autograde,
        "show":      cmd_show,
        "list":      cmd_list,
        "review":    cmd_review,
    }

    if not args.cmd or args.cmd not in dispatch:
        parser.print_help()
        sys.exit(0)

    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
