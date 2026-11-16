"""Watchlist alert-outcome loop (engines/flow/watch.py).

Pure selection logic on synthetic objects, capture/label against a temp learning
DB, one pass through the real ORM models on an in-memory database, and the
loop's place in the shared ledger and scorecard.
"""
import os
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from engines.flow import watch
from engines.learning import ledger, scorecard, stats as st


def _utc(day: str, hh: int, mm: int = 0) -> datetime:
    """A naive-UTC instant, as the scanner stores them (ET = UTC-5 in November)."""
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hh, mm)


def _entry(eid=1, day="2026-11-10", otype="call", price=2.0, under=100.0, expiry="2026-11-20"):
    return SimpleNamespace(id=eid, alert_id=eid * 10, ticker="NVDA", option_ticker=f"O:NVDA{eid}",
                           option_type=otype, expiry=expiry, strike=105.0, entry_price=price,
                           entry_underlying=under, added_at=_utc(day, 15, 0))   # 10:00 ET


def _alert(premium=900_000):
    return SimpleNamespace(estimated_premium=premium, vol_oi_ratio=6.0, delta=0.3,
                           implied_volatility=0.45, side="buy", expiry="2026-11-20",
                           underlying_price=100.0, strike=105.0, option_type="call")


def _snap(eid, day, hh, opt, under):
    at = _utc(day, hh)
    return {"entry_id": eid, "date": watch._et_date(at), "at": watch._iso(at),
            "option_price": opt, "underlying_price": under}


def test_signal_row_keeps_the_entry_and_the_raw_score_inputs():
    row = watch.signal_row(_entry(), _alert(), today="2026-11-10")
    assert row["id"] == "1" and row["run_date"] == "2026-11-10" and row["dte"] == 10
    assert (row["entry_price"], row["entry_underlying"]) == (2.0, 100.0)
    assert row["premium"] == 900_000 and row["vol_oi_ratio"] == 6.0 and row["delta"] == 0.3
    assert isinstance(row["score"], int) and row["score"] > 0
    # Captured on a later day, the score would use the wrong days-to-expiry: omit it.
    late = watch.signal_row(_entry(), _alert(), today="2026-11-13")
    assert late["score"] is None and late["premium"] == 900_000
    # Nothing to measure a move from -> no signal.
    assert watch.signal_row(_entry(price=0), _alert()) is None
    assert watch.signal_row(_entry(under=None), _alert()) is None


def test_outcomes_follow_the_session_calendar_and_wait_for_the_close():
    sig = watch.signal_row(_entry(), _alert(), today="2026-11-10")
    snaps = [
        _snap(1, "2026-11-10", 14, 9.0, 150.0),     # 09:00 ET — BEFORE the entry was added: ignored
        _snap(1, "2026-11-10", 18, 2.2, 101.0),
        _snap(1, "2026-11-10", 21, 2.4, 102.0),     # the entry session's last print
        _snap(1, "2026-11-11", 21, 3.0, 104.0),
        _snap(1, "2026-11-13", 21, 1.0, 97.0),      # no snapshot on the 12th for this entry
    ]
    sessions = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]

    # During the entry session nothing is final yet.
    assert watch.pick_outcomes(sig, snaps, sessions, set(), through="2026-11-09") == []
    # After that close: only the same-day horizon.
    first = watch.pick_outcomes(sig, snaps, sessions, set(), through="2026-11-10")
    assert [o["horizon"] for o in first] == ["0d"]
    assert first[0]["underlying_price"] == 102.0 and first[0]["raw_return"] == pytest.approx(2.0)
    assert first[0]["option_pnl_pct"] == pytest.approx(20.0) and first[0]["direction_correct"] == 1

    later = {o["horizon"]: o for o in
             watch.pick_outcomes(sig, snaps, sessions, {"0d"}, through="2026-11-13")}
    assert set(later) == {"1d", "3d"}                       # 5d: that session has not happened
    assert later["1d"]["outcome_date"] == "2026-11-11" and later["1d"]["raw_return"] == pytest.approx(4.0)
    # "3d" is the third session on the calendar (the 13th), not the third snapshot.
    assert later["3d"]["outcome_date"] == "2026-11-13" and later["3d"]["direction_correct"] == 0

    # A put is right when the underlying falls.
    put = watch.signal_row(_entry(otype="put"), _alert(), today="2026-11-10")
    o = watch.pick_outcomes(put, snaps, sessions, {"0d", "1d"}, through="2026-11-13")[0]
    assert o["raw_return"] == pytest.approx(-3.0) and o["signed_return"] == pytest.approx(3.0)
    assert o["direction_correct"] == 1


def test_a_session_without_a_snapshot_is_skipped_not_borrowed():
    sig = watch.signal_row(_entry(), _alert(), today="2026-11-10")
    snaps = [_snap(1, "2026-11-10", 21, 2.4, 102.0), _snap(1, "2026-11-12", 21, 3.0, 104.0)]
    sessions = ["2026-11-10", "2026-11-11", "2026-11-12"]
    out = watch.pick_outcomes(sig, snaps, sessions, set(), through="2026-11-12")
    assert [o["horizon"] for o in out] == ["0d"]            # no 1d: the 11th has no print for it


def test_closed_through_turns_over_after_the_delayed_close():
    assert watch.closed_through(datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)) == "2026-11-09"   # 15:00 ET
    assert watch.closed_through(datetime(2026, 11, 10, 21, 30, tzinfo=timezone.utc)) == "2026-11-10"  # 16:30 ET


def test_capture_and_label_are_insert_only_and_idempotent(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    pairs = [(_entry(1), _alert()), (_entry(2, otype="put"), _alert()), (_entry(3, price=0), _alert())]
    assert watch.capture(db, pairs, today="2026-11-10") == 2        # the unpriced entry is dropped
    assert watch.capture(db, pairs, today="2026-11-10") == 0
    assert watch.counts(db) == (2, 0)

    snaps = [_snap(1, "2026-11-10", 21, 2.4, 102.0), _snap(2, "2026-11-10", 21, 1.5, 102.0),
             _snap(1, "2026-11-11", 21, 3.0, 104.0), _snap(2, "2026-11-11", 21, 1.0, 104.0)]
    assert watch.label_due(db, snaps, through="2026-11-10") == 2    # both 0d
    assert watch.label_due(db, snaps, through="2026-11-11") == 2    # both 1d
    assert watch.label_due(db, snaps, through="2026-11-11") == 0
    # A later, different price for an already-labeled session never rewrites it.
    snaps[0] = _snap(1, "2026-11-10", 21, 9.9, 150.0)
    assert watch.label_due(db, snaps, through="2026-11-11") == 0
    assert watch.counts(db) == (2, 4)
    assert watch.label_due(db, [], through="2026-11-11") == 0


def test_capture_and_label_through_the_real_scanner_models(tmp_path, monkeypatch):
    """One pass through the ORM, so the queries in _load_entries / _load_snapshots
    are exercised against the actual tables."""
    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.orm import sessionmaker
    from engines.flow import db as fdb
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "flow.db"))
    fdb.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(fdb, "Session", Session)

    s = Session()
    alert = fdb.SweepAlert(ticker="NVDA", option_ticker="O:NVDA1", expiry="2026-11-20", strike=105.0,
                           option_type="call", estimated_premium=900_000, vol_oi_ratio=6.0,
                           delta=0.3, underlying_price=100.0, detected_at=_utc("2026-11-10", 15))
    s.add(alert)
    s.flush()
    entry = fdb.WatchlistEntry(alert_id=alert.id, ticker="NVDA", option_ticker="O:NVDA1",
                               expiry="2026-11-20", strike=105.0, option_type="call",
                               entry_price=2.0, entry_underlying=100.0, added_at=_utc("2026-11-10", 15))
    s.add(entry)
    s.flush()
    for day, opt, under in (("2026-11-10", 2.4, 102.0), ("2026-11-11", 3.0, 104.0)):
        s.add(fdb.PriceSnapshot(entry_id=entry.id, option_price=opt, underlying_price=under,
                                pnl_pct=0.0, captured_at=_utc(day, 21)))
    s.commit()
    s.close()

    db = str(tmp_path / "flow_learning.db")
    assert watch.capture(db, today="2026-11-10") == 1
    assert watch.label_due(db, through="2026-11-11") == 2
    rows = ledger.watch(str(tmp_path))
    assert {r["horizon"] for r in rows} == {"0d", "1d"}
    assert all(r["direction"] == 1 and r["premium"] == 900_000 and r["score"] for r in rows)


def _planted_watch(db, days=40, per_day=6, edge=0.6, seed=2):
    """Calls and puts whose underlying drifts `edge`% the alert's way by next session."""
    rng = random.Random(seed)
    pairs, snaps, eid = [], [], 0
    dates = [f"2026-{9 + i // 28:02d}-{1 + i % 28:02d}" for i in range(days + 1)]
    for i in range(days):
        for _ in range(per_day):
            eid += 1
            otype = rng.choice(["call", "put"])
            sign = 1 if otype == "call" else -1
            pairs.append((_entry(eid, dates[i], otype), _alert(premium=rng.choice([3e5, 9e5, 2e6]))))
            same = 100.0 * (1 + (sign * 0.0 + rng.gauss(0, 0.8)) / 100)
            nxt = 100.0 * (1 + (sign * edge + rng.gauss(0, 0.8)) / 100)
            snaps.append(_snap(eid, dates[i], 21, 2.0, same))
            snaps.append(_snap(eid, dates[i + 1], 21, 2.0, nxt))
        watch.capture(db, pairs[-per_day:], today=dates[i])
    watch.label_due(db, snaps, through=dates[-1])


def test_watch_loop_in_the_ledger_and_scorecard(tmp_path):
    d = str(tmp_path)
    assert ledger.watch(d) == []                                    # no store: empty, no error
    _planted_watch(os.path.join(d, "flow_learning.db"))
    rows = ledger.watch(d)
    assert len(rows) == 480 and {r["horizon"] for r in rows} == {"0d", "1d"}
    # Edge is the move in the alert's direction net of the average watched name.
    nxt = [r for r in rows if r["horizon"] == "1d"]
    assert sum(r["direction"] * r["edge"] for r in nxt) == pytest.approx(
        sum(r["edge"] * r["direction"] for r in nxt))
    assert abs(sum(r["ret"] for r in nxt) / len(nxt)
               - sum(r["ret"] - r["direction"] * r["edge"] for r in nxt) / len(nxt)) < 1e-9

    card = scorecard.build(d)
    assert [lp["loop"] for lp in card["loops"]] == list(ledger.LOOPS) and "watch" in ledger.LOOPS
    w = next(lp for lp in card["loops"] if lp["loop"] == "watch")
    cohorts = {c["cohort"]: c for c in w["families"][0]["cohorts"]}
    assert cohorts["next session"]["verdict"] == st.PAYING          # the planted drift
    assert cohorts["to that session's close"]["verdict"] == st.NOISE
    assert cohorts["next session"]["hit_rate"] > 60 and "avg_option_pnl" in cohorts["next session"]
    # The premium (and so the score) was random: it must not be credited with ranking.
    assert {c["verdict"] for c in w["families"][1]["cohorts"]} <= {st.NOISE, st.ACCUMULATING}


def test_flow_store_without_watch_tables_reads_as_an_empty_loop(tmp_path, caplog):
    """The ETF-flow loop creates flow_learning.db first; until the watchlist loop
    writes, its tables do not exist. That is an empty loop, not a warning."""
    from engines.flow import learning
    d = str(tmp_path)
    with learning._db(os.path.join(d, "flow_learning.db")):
        pass
    with caplog.at_level("WARNING"):
        assert ledger.watch(d) == []
    assert not caplog.records
