"""Phase D of the flow predictiveness loop: the auto-tuner.

Seeds a bucket where score-weighted skew is the real directional signal and
net-premium skew is noise, and checks the tuner picks the right metric, finds a
significant edge, and that under-sampled buckets are left untuned.
"""
from engines.flow import learning, report, tune


def _insert(conn, i, cutoff, horizon, net_skew, sw_skew, raw):
    rd = f"2026-10-{(i % 28) + 1:02d}"
    sid = f"{rd}:SPY:{cutoff}:{i}"       # i keeps ids unique within a run_date
    implied = "up" if net_skew > 0 else ("down" if net_skew < 0 else "flat")
    conn.execute(
        "INSERT OR REPLACE INTO flow_signals "
        "(id, run_date, ticker, cutoff, cutoff_ts, captured_at, n_alerts, "
        " call_premium, put_premium, call_volume, put_volume, net_premium_skew, "
        " score_weighted_skew, notable_premium, notable_contracts, implied_dir) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, rd, "SPY", cutoff, rd + "T15:30:00+00:00", "x", 2, 1.0, 0.0, 1, 0,
         net_skew, sw_skew, 1.0, 1, implied))
    conn.execute(
        "INSERT OR REPLACE INTO flow_outcomes "
        "(signal_id, horizon, labeled_at, ref_price, exit_price, raw_return, "
        " signed_return, direction_correct) VALUES (?,?,?,?,?,?,?,?)",
        (sid, horizon, "x", 500.0, 500.0 * (1 + raw / 100), raw, raw, 1 if raw > 0 else 0))


def _seed(db):
    with learning._db(db) as conn:
        # Predictive bucket: score-weighted skew tracks the move (20/24 right);
        # net-premium skew is uncorrelated noise -> tuner must pick sw.
        for i in range(24):
            up = i < 12
            sw = 0.4 if up else -0.4
            net = 0.02 * (1 if i % 2 == 0 else -1)      # ~zero correlation
            correct = (i % 12) >= 2                      # 10 of every 12 right
            raw = (0.5 if up else -0.5) if correct else (-0.3 if up else 0.3)
            _insert(conn, i, "10:30", "to_close", net, sw, raw)

        # Under-sampled bucket: 5 rows, must stay untuned.
        for i in range(5):
            _insert(conn, 100 + i, "eod", "next_day", 0.3, 0.3, 0.4)


def test_tuner_picks_metric_and_finds_edge(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)

    assert tune.tune(db) == 1                 # only the ready bucket
    act = tune.active(db)
    assert ("10:30", "to_close") in act
    assert ("eod", "next_day") not in act     # under-sampled, skipped

    t = act[("10:30", "to_close")]
    assert t["metric"] == "score_weighted_skew"
    assert t["direction_sign"] == 1
    assert t["significant"] == 1
    assert t["hit_rate_above"] > 70 and t["z"] > 1.64


def test_report_attaches_tuning(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    _seed(db)
    tune.tune(db)

    s = report.summary(db)
    tc = next(b for b in s["buckets"] if b["cutoff"] == "10:30" and b["horizon"] == "to_close")
    assert tc["tuning"] is not None
    assert tc["tuning"]["metric"] == "score_weighted_skew"
    assert tc["tuning"]["significant"] is True


def test_tune_empty_db_is_noop(tmp_path):
    db = str(tmp_path / "flow_learning.db")
    assert tune.tune(db) == 0
    assert tune.active(db) == {}
