"""Phase A of the flow predictiveness loop: per-ETF signal capture.

Tests the pure aggregation (dedup-by-latest-contract, premium/volume skew,
implied direction) and the flow_signals upsert/idempotency against a temp DB.
No Massive, no network, no SQLAlchemy engine — synthetic alert objects only.
"""
from datetime import date, time
from types import SimpleNamespace

from engines.flow import learning

_D = date(2026, 11, 13)
_RD = _D.isoformat()


def _alert(occ, otype, premium, volume, et_time, expiry="2026-12-19"):
    return SimpleNamespace(
        detected_at=learning._et_to_naive_utc(_D, et_time),
        option_ticker=occ,
        option_type=otype,
        estimated_premium=premium,
        volume=volume,
        expiry=expiry,
        vol_oi_ratio=5.0,
        delta=0.30,
        underlying_price=500.0,
        strike=505.0,
    )


def test_aggregate_dedups_to_latest_contract_reading():
    # Same call contract flagged twice (cumulative grows); one put contract.
    alerts = [
        _alert("O:SPY..C1", "call", 100_000, 1_000, time(10, 0)),
        _alert("O:SPY..P1", "put",   50_000,   500, time(11, 0)),
        _alert("O:SPY..C1", "call", 300_000, 3_000, time(13, 0)),
    ]

    # At 10:30 only the 10:00 call is in scope.
    a1030 = learning._aggregate(alerts, learning._et_to_naive_utc(_D, time(10, 30)), _RD)
    assert a1030["n_alerts"] == 1
    assert a1030["call_premium"] == 100_000 and a1030["put_premium"] == 0
    assert a1030["net_premium_skew"] == 1.0
    assert a1030["implied_dir"] == "up"

    # By EOD the call is deduped to its LATEST cumulative reading (300k, not 400k).
    aeod = learning._aggregate(alerts, learning._et_to_naive_utc(_D, time(16, 20)), _RD)
    assert aeod["n_alerts"] == 2
    assert aeod["call_premium"] == 300_000 and aeod["put_premium"] == 50_000
    assert abs(aeod["net_premium_skew"] - 0.7143) < 1e-3
    assert aeod["implied_dir"] == "up"


def test_aggregate_put_dominant_is_down_and_deadband_is_flat():
    down = learning._aggregate(
        [_alert("O:QQQ..P1", "put", 400_000, 4_000, time(10, 0)),
         _alert("O:QQQ..C1", "call", 100_000, 1_000, time(10, 0))],
        learning._et_to_naive_utc(_D, time(10, 30)), _RD)
    assert down["implied_dir"] == "down" and down["net_premium_skew"] < 0

    flat = learning._aggregate(
        [_alert("O:IWM..C1", "call", 101_000, 1_010, time(10, 0)),
         _alert("O:IWM..P1", "put",  100_000, 1_000, time(10, 0))],
        learning._et_to_naive_utc(_D, time(10, 30)), _RD)
    assert abs(flat["net_premium_skew"]) < learning._FLAT_BAND
    assert flat["implied_dir"] == "flat"


def test_aggregate_zerodte_share():
    agg = learning._aggregate(
        [_alert("O:SPY..C0", "call", 200_000, 2_000, time(10, 0), expiry=_RD),
         _alert("O:SPY..C1", "call", 200_000, 2_000, time(10, 0), expiry="2026-12-19")],
        learning._et_to_naive_utc(_D, time(10, 30)), _RD)
    assert agg["zerodte_share"] == 0.5


def test_signal_upsert_is_idempotent(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    agg = learning._aggregate(
        [_alert("O:SPY..C1", "call", 100_000, 1_000, time(10, 0))],
        learning._et_to_naive_utc(_D, time(10, 30)), _RD)
    row = learning._signal_row(_RD, "SPY", "10:30",
                               learning._et_to_naive_utc(_D, time(10, 30)), agg)

    with learning._db(db) as conn:
        assert learning._upsert_signals(conn, [row]) == 1
    assert learning.counts(db) == (1, 0)

    # Same id re-written, not duplicated.
    with learning._db(db) as conn:
        learning._upsert_signals(conn, [row])
    assert learning.counts(db) == (1, 0)
