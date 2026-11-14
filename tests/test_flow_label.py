"""Phase B of the flow predictiveness loop: outcome labeling from price bars.

Drives label.label_due with a fake Massive client (canned minute bars) against a
temp learning DB seeded with signals. Verifies each horizon's ref/exit, the
signed-return direction check, and idempotency. No network.
"""
from datetime import date, datetime, time

from engines import config as econf
from engines.flow import label, learning

_D = date(2026, 11, 13)      # a Friday
_RD = _D.isoformat()
_D1 = date(2026, 11, 16)     # next trading session (Mon)


def _bar(d, t, o, h, l, c):
    return {"ts": datetime.combine(d, t, tzinfo=econf.ET).isoformat(),
            "o": o, "h": h, "l": l, "c": c, "v": 1000}


class _FakeClient:
    def __init__(self, bars_by_date):
        self.bars = bars_by_date

    def fetch_minute_bars(self, ticker, d):
        return list(self.bars.get(d.isoformat(), []))


def _agg(implied):
    skew = 0.5 if implied == "up" else (-0.5 if implied == "down" else 0.0)
    return {"n_alerts": 2, "call_premium": 1.0, "put_premium": 0.0,
            "call_volume": 1, "put_volume": 0, "net_premium_skew": skew,
            "net_volume_skew": skew, "score_weighted_skew": skew,
            "notable_premium": 1.0, "notable_contracts": 1,
            "zerodte_share": 0.0, "implied_dir": implied}


def _seed(db):
    rows = []
    for cutoff, t, implied in [("10:30", time(10, 30), "up"),
                               ("12:00", time(12, 0), "down"),
                               ("eod", time(16, 20), "up")]:
        ts = learning._et_to_naive_utc(_D, t)
        rows.append(learning._signal_row(_RD, "SPY", cutoff, ts, _agg(implied)))
    with learning._db(db) as conn:
        learning._upsert_signals(conn, rows)


def _client():
    # Rising session: open 500 -> close 505; next session closes 508.
    return _FakeClient({
        _RD: [_bar(_D, time(9, 30), 500, 501, 499.5, 500.5),
              _bar(_D, time(10, 30), 501, 502.5, 500.8, 502),
              _bar(_D, time(14, 0), 503, 505.5, 502.5, 504),
              _bar(_D, time(16, 0), 504.5, 505.2, 504, 505)],
        _D1.isoformat(): [_bar(_D1, time(9, 30), 505, 506, 504.8, 505.5),
                          _bar(_D1, time(16, 0), 507, 508.5, 506.5, 508)],
    })


def test_label_produces_all_horizons(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)

    assert label.label_due(db_path=db, client=_client()) == 4   # 10:30, 12:00, eod×2
    assert learning.counts(db) == (3, 4)

    with learning._db(db) as conn:
        out = {(r["signal_id"].split(":", 2)[2], r["horizon"]): r
               for r in conn.execute("SELECT * FROM flow_outcomes").fetchall()}

    # 10:30 up-skew on an up day -> ref=price@10:30=502, exit=close=505, correct.
    tc = out[("10:30", "to_close")]
    assert tc["ref_price"] == 502 and tc["exit_price"] == 505
    assert tc["signed_return"] > 0 and tc["direction_correct"] == 1

    # 12:00 DOWN-skew on an up day -> same move, wrong direction.
    assert out[("12:00", "to_close")]["direction_correct"] == 0
    assert out[("12:00", "to_close")]["signed_return"] < 0

    # eod open->close: ref = session open.
    oc = out[("eod", "open_close")]
    assert oc["ref_price"] == 500 and oc["exit_price"] == 505
    assert abs(oc["raw_return"] - 1.0) < 1e-6

    # eod overnight: ref = today close, exit = next session close.
    nd = out[("eod", "next_day")]
    assert nd["ref_price"] == 505 and nd["exit_price"] == 508


def test_label_is_idempotent(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)
    c = _client()
    assert label.label_due(db_path=db, client=c) == 4
    assert label.label_due(db_path=db, client=c) == 0   # nothing new
    assert learning.counts(db) == (3, 4)


def test_next_day_waits_for_next_session(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)
    # Only the run_date's bars exist — next_day can't be labeled yet.
    only_today = _FakeClient({_RD: _client().bars[_RD]})
    assert label.label_due(db_path=db, client=only_today) == 3   # 2×to_close + open_close
    sigs, outs = learning.counts(db)
    assert outs == 3
    # Once the next session appears, the overnight outcome completes.
    assert label.label_due(db_path=db, client=_client()) == 1
    assert learning.counts(db) == (3, 4)
