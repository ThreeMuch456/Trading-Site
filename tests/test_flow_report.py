"""Phase C of the flow predictiveness loop: the report.

Seeds a temp learning DB with signal+outcome pairs and checks the per-bucket
hit-rate, skew correlation sign, flat exclusion, and the ready/accumulating
headline. Pure DB math, no network.
"""
from engines.flow import learning, report


def _insert(conn, run_date, cutoff, horizon, skew, raw, implied, correct):
    sid = f"{run_date}:SPY:{cutoff}"
    conn.execute(
        "INSERT OR REPLACE INTO flow_signals "
        "(id, run_date, ticker, cutoff, cutoff_ts, captured_at, n_alerts, "
        " call_premium, put_premium, call_volume, put_volume, net_premium_skew, "
        " score_weighted_skew, notable_premium, notable_contracts, implied_dir) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, run_date, "SPY", cutoff, run_date + "T15:30:00+00:00", "x", 2,
         1.0, 0.0, 1, 0, skew, skew, 1.0, 1, implied))
    signed = raw * ({"up": 1.0, "down": -1.0}.get(implied, 0.0))
    conn.execute(
        "INSERT OR REPLACE INTO flow_outcomes "
        "(signal_id, horizon, labeled_at, ref_price, exit_price, raw_return, "
        " signed_return, direction_correct) VALUES (?,?,?,?,?,?,?,?)",
        (sid, horizon, "x", 500.0, 500.0 * (1 + raw / 100), raw, signed, correct))


def _seed(db):
    # 6 directional 10:30/to_close rows: higher skew -> higher return (5 right, 1 wrong),
    # plus one flat row that must be excluded from the directional stats.
    data = [
        ("2026-11-02", 0.2, 0.30, "up", 1),
        ("2026-11-03", 0.3, 0.45, "up", 1),
        ("2026-11-04", 0.4, 0.55, "up", 1),
        ("2026-11-05", 0.5, 0.60, "up", 1),
        ("2026-11-06", 0.6, 0.80, "up", 1),
        ("2026-11-09", 0.1, -0.20, "up", 0),   # a miss
    ]
    with learning._db(db) as conn:
        for rd, skew, raw, implied, correct in data:
            _insert(conn, rd, "10:30", "to_close", skew, raw, implied, correct)
        _insert(conn, "2026-11-10", "10:30", "to_close", 0.0, 0.5, "flat", 0)


def test_bucket_stats_and_ready_headline(tmp_path, monkeypatch):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)
    monkeypatch.setattr(report, "MIN_SAMPLES", 5)

    s = report.summary(db)
    tc = next(b for b in s["buckets"] if b["cutoff"] == "10:30" and b["horizon"] == "to_close")

    assert tc["n"] == 7 and tc["n_directional"] == 6   # flat counted in n, not directional
    assert tc["hit_rate"] == round(100 * 5 / 6, 1)
    assert tc["ready"] is True
    assert tc["skew_corr"] is not None and tc["skew_corr"] > 0   # skew tracks return
    assert s["headline"].startswith("Most predictive")


def test_accumulating_headline_under_min(tmp_path, monkeypatch):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)
    monkeypatch.setattr(report, "MIN_SAMPLES", 100)

    s = report.summary(db)
    assert s["headline"].startswith("Accumulating")
    tc = next(b for b in s["buckets"] if b["cutoff"] == "10:30")
    assert tc["ready"] is False


def test_empty_db_is_accumulating(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    s = report.summary(db)
    assert s["n_sessions"] == 0 and s["buckets"] == []
    assert s["headline"].startswith("Accumulating")
