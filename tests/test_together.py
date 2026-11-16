"""Phase 5: the daily 0DTE regime digest and the cross-signal ("together") loop.

The digest turns a noisy event stream into one permanent row per ticker-session;
the together loop joins the Brief's calls to that row and to the 10:30 ETF flow
lean, and asks whether any of it changes how the calls do.
"""
import json
import os
import random

import pytest

from engines.learning import ledger, scorecard, stats as st
from engines.zerodte import regime_digest as rd

from test_learning import _brief_store


def _ts(day: str, hhmm: str) -> str:
    return f"{day}T{hhmm}:00-05:00"


# ---------------------------------------------------------------------------
# regime digest
# ---------------------------------------------------------------------------

def test_digest_day_reads_the_open_and_the_time_spent_in_each_regime():
    d = "2026-11-10"
    events = [
        (_ts(d, "09:31"), None, "TREND"),          # cold start: nothing classified before this
        (_ts(d, "09:40"), "TREND", "PIN"),
        (_ts(d, "11:30"), "PIN", "TRANSITION"),
        (_ts(d, "12:00"), "TRANSITION", "TREND"),
        (_ts(d, "16:05"), "TREND", "PIN"),         # after the close: not part of the session
    ]
    g = rd.digest_day(events)
    assert g["open_regime"] == "PIN"               # in force at 09:45
    assert g["close_regime"] == "TREND" and g["n_changes"] == 5
    # 09:31-09:40 TREND (9) + 12:00-16:00 TREND (240); 09:40-11:30 PIN (110); 30 TRANSITION.
    total = 9 + 240 + 110 + 30                     # the unclassified first minute is not counted
    assert g["trend_share"] == pytest.approx(249 / total, abs=1e-4)
    assert g["pin_share"] == pytest.approx(110 / total, abs=1e-4)
    assert g["transition_share"] == pytest.approx(30 / total, abs=1e-4)
    assert rd.digest_day([]) is None
    # A regime carried in from before the open is read from the first event's `from`.
    late = rd.digest_day([(_ts(d, "10:30"), "PIN", "TREND")])
    assert late["open_regime"] == "PIN" and late["pin_share"] == pytest.approx(60 / 390, abs=1e-4)


def test_bundled_seed_is_a_clean_digest():
    with open(rd.SEED_PATH, encoding="utf-8") as f:
        rows = json.load(f)
    assert len(rows) == 192 and {r["ticker"] for r in rows} == {"SPY", "QQQ", "IWM"}
    assert len({(r["session_date"], r["ticker"]) for r in rows}) == 192       # one per ticker-session
    assert {r["open_regime"] for r in rows} <= set(rd.REGIMES)
    for r in rows:
        assert r["pin_share"] + r["trend_share"] + r["transition_share"] == pytest.approx(1.0, abs=1e-3)
        assert "source" not in r                                             # stamped on load


def test_run_digests_finished_sessions_once_and_seed_never_overwrites(tmp_path):
    from engines.zerodte.db import DB
    db = DB(str(tmp_path / "zerodte.sqlite3"))
    try:
        for day in ("2026-11-12", "2026-11-13"):
            db.save_event(day, "SPY", _ts(day, "09:31"), "REGIME_CHANGE", {"from": None, "to": "PIN"})
            db.save_event(day, "SPY", _ts(day, "13:00"), "REGIME_CHANGE", {"from": "PIN", "to": "TREND"})
            db.save_event(day, "SPY", _ts(day, "09:31"), "NEW_WALL", {"strike": 600})   # not a regime event
        assert rd.run(db, "2026-11-12") == 1           # the 13th is still in progress
        assert rd.run(db, "2026-11-13") == 1
        assert rd.run(db, "2026-11-13") == 0           # idempotent
        row = db._conn.execute("SELECT open_regime, source, n_changes FROM regime_daily "
                               "WHERE session_date='2026-11-13'").fetchone()
        assert tuple(row) == ("PIN", "engine", 2)
        # The bundled history loads beside it; the engine's own 11-13 row is kept.
        added = rd.seed(db)
        assert added == 190 and rd.seed(db) == 0       # 192 legacy rows, two already present
        kept = db._conn.execute("SELECT source FROM regime_daily WHERE session_date='2026-11-13' "
                                "AND ticker='SPY'").fetchone()[0]
        assert kept == "engine"
        # Pruning the event stream leaves the digest alone.
        db.prune("2027-06-01")
        assert db._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM regime_daily").fetchone()[0] == 192
        assert len(ledger.regime_days(str(tmp_path))) == 192
    finally:
        db._conn.close()


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------

def _regime_store(path, rows):
    """rows: [(date, ticker, open_regime, trend_share)]"""
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript(rd.SCHEMA)
    con.executemany(
        "INSERT INTO regime_daily VALUES (?,?,?,?,?,?,?,?,?)",
        [(d, tk, reg, "TREND", 10, round(1 - ts, 4), ts, 0.0, "legacy") for d, tk, reg, ts in rows])
    con.commit()
    con.close()


def _flow_store(path, rows):
    """rows: [(date, ticker, implied_dir, ret_1030_to_close)]"""
    from engines.flow import learning
    with learning._db(path) as con:
        for d, tk, lean, ret in rows:
            sid = f"{d}:{tk}:10:30"
            con.execute(
                "INSERT INTO flow_signals (id, run_date, ticker, cutoff, cutoff_ts, captured_at, "
                "n_alerts, call_premium, put_premium, call_volume, put_volume, notable_premium, "
                "notable_contracts, implied_dir) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, d, tk, "10:30", "t", "t", 5, 1.0, 1.0, 1, 1, 0.0, 0, lean))
            sign = {"up": 1, "down": -1}.get(lean, 0)
            con.execute(
                "INSERT INTO flow_outcomes (signal_id, horizon, labeled_at, ref_price, exit_price, "
                "raw_return, signed_return, direction_correct) VALUES (?,?,?,?,?,?,?,?)",
                (sid, "to_close", "t", 100.0, 100.0 + ret, ret, ret * sign, int(ret * sign > 0)))


def test_together_attaches_the_same_days_signals_and_drops_calls_with_none(tmp_path):
    d = str(tmp_path)
    _brief_store(os.path.join(d, "agent.sqlite3"), [
        ("2026-11-10", {"SPY": ("bullish", 0.55, 0.8, 1, 1.0), "QQQ": ("neutral", None, 0.1, 1, 1.0)}),
        ("2026-11-11", {"SPY": ("bearish", 0.5, 0.6, 0, 1.0)}),
        ("2026-11-12", {"SPY": ("bullish", 0.5, -0.4, 0, 1.0)}),      # nothing to join to
    ])
    _regime_store(os.path.join(d, "zerodte.sqlite3"),
                  [("2026-11-10", "SPY", "PIN", 0.2), ("2026-11-10", "QQQ", "TREND", 0.7)])
    _flow_store(os.path.join(d, "flow_learning.db"),
                [("2026-11-10", "SPY", "up", 0.5), ("2026-11-11", "SPY", "up", -0.3),
                 ("2026-11-10", "QQQ", "flat", 0.2)])
    rows = {(r["date"], r["ticker"]): r for r in ledger.together(d)}
    assert set(rows) == {("2026-11-10", "SPY"), ("2026-11-10", "QQQ"), ("2026-11-11", "SPY")}

    a = rows[("2026-11-10", "SPY")]                    # bullish, PIN, flow up
    assert (a["regime"], a["flow_dir"], a["agree"]) == ("PIN", 1, True)
    assert a["abs_ret"] == 0.8 and a["trend_share"] == 0.2
    drift = (0.5 - 0.3) / 2                            # SPY's average 10:30-to-close move
    assert a["after_edge"] == pytest.approx(0.5 - drift)
    b = rows[("2026-11-11", "SPY")]                    # bearish against an up lean, no regime row
    assert (b["regime"], b["agree"]) == (None, False)
    assert b["after_edge"] == pytest.approx(-(-0.3 - drift))
    q = rows[("2026-11-10", "QQQ")]                    # a neutral call and a flat lean: no agreement
    assert q["regime"] == "TREND" and q["agree"] is None and q["after_edge"] is None
    assert "together" in ledger.LOOPS and ledger.together(str(tmp_path / "empty")) == []


def _planted(tmp_path, days=60, seed=6):
    """Sessions that open in TREND really do move more; the Brief's calls really do
    hold better after 10:30 when flow agrees; the regime does nothing for the calls."""
    rng = random.Random(seed)
    d = str(tmp_path)
    calls, regimes, flows = [], [], []
    for i in range(days):
        day = f"2026-{8 + i // 28:02d}-{1 + i % 28:02d}"
        per = {}
        for tk in ("SPY", "QQQ", "IWM"):
            reg = rng.choice(["PIN", "TREND", "TRANSITION"])
            size = abs(rng.gauss(0, 0.5)) + (0.7 if reg == "TREND" else 0.15)
            bias = rng.choice(["bullish", "bearish"])
            right = rng.random() < 0.5
            pct = size if (bias == "bullish") == right else -size
            per[tk] = (bias, 0.5, pct, int(right), 1.0)
            regimes.append((day, tk, reg, min(0.95, size / 2)))
            agrees = rng.random() < 0.5
            lean = ("up" if bias == "bullish" else "down") if agrees else \
                   ("down" if bias == "bullish" else "up")
            called = 1 if bias == "bullish" else -1
            flows.append((day, tk, lean, called * (0.5 if agrees else -0.3) + rng.gauss(0, 0.4)))
        calls.append((day, per))
    _brief_store(os.path.join(d, "agent.sqlite3"), calls)
    _regime_store(os.path.join(d, "zerodte.sqlite3"), regimes)
    _flow_store(os.path.join(d, "flow_learning.db"), flows)
    return d


def test_together_loop_judges_each_question_separately(tmp_path):
    d = _planted(tmp_path)
    card = scorecard.build(d)
    json.dumps(card, allow_nan=False)
    t = next(lp for lp in card["loops"] if lp["loop"] == "together")
    assert t["coverage"]["dates"] == 60 and t["coverage"]["observations"] == 180
    fams = {f["family"]: f for f in t["families"]}
    assert list(fams) == ["regime_calls", "regime_range", "regime_describes", "flow_agreement"]

    # The regime was planted to do nothing for the calls themselves...
    assert {c["verdict"] for c in fams["regime_calls"]["cohorts"]} == {st.NOISE}
    # ...and to mark out the larger days.
    size = {c["cohort"]: c for c in fams["regime_range"]["cohorts"]}
    assert size["TREND at 09:45 minus the rest"]["verdict"] == st.PAYING
    assert 0.3 < size["TREND at 09:45 minus the rest"]["effect"] < 0.8          # planted 0.55
    assert size["PIN at 09:45 minus the rest"]["effect"] < 0

    # The whole-day check is real but flagged as description, not prediction.
    assert fams["regime_describes"]["descriptive"] is True
    assert "descriptive" not in fams["regime_range"]

    agree = fams["flow_agreement"]["cohorts"][0]
    assert agree["cohort"] == "flow agrees minus flow disagrees"
    assert agree["verdict"] == st.PAYING and 0.5 < agree["effect"] < 1.1         # planted 0.8


def test_descriptive_findings_are_never_reported_as_something_to_act_on(tmp_path, monkeypatch):
    """A whole-day regime share that lines up with the day's move is true and
    untradeable. It must show on the page, flagged, and stay out of the digest."""
    from desk import learning_view as lv
    d = str(tmp_path)
    rng = random.Random(12)
    calls, regimes = [], []
    for i in range(50):
        day = f"2026-{8 + i // 28:02d}-{1 + i % 28:02d}"
        per = {}
        for tk in ("SPY", "QQQ", "IWM"):
            share = rng.random()
            size = abs(rng.gauss(0, 0.3)) + share                     # more TREND time, bigger day
            bias = rng.choice(["bullish", "bearish"])
            right = rng.random() < 0.5
            per[tk] = (bias, 0.5, size if (bias == "bullish") == right else -size, int(right), 1.0)
            regimes.append((day, tk, rng.choice(["PIN", "TREND", "TRANSITION"]), round(share, 3)))
        calls.append((day, per))
    _brief_store(os.path.join(d, "agent.sqlite3"), calls)
    _regime_store(os.path.join(d, "zerodte.sqlite3"), regimes)
    card = scorecard.build(d)
    monkeypatch.setattr(lv, "_scorecard", lambda: card)

    t = next(lp for lp in lv.page()["loops"] if lp["key"] == "together")
    describes = next(f for f in t["families"] if f["key"] == "regime_describes")
    assert describes["rows"][0]["verdict"] == "paying" and describes["descriptive"]
    assert describes["summary"].startswith("describes, does not predict")
    assert "In the diagnostics" not in t["read"]                      # not advertised
    line = next(ln for ln in lv.digest() if ln["src"] == "Signals together")
    assert "TREND" not in line["text"] and line["verdict"] == "noise"
