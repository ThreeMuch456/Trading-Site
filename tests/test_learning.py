"""Shared learning layer: one stats method, one ledger, one verdict vocabulary.

Three things are pinned here: the statistics are numerically right (against
textbook values and against the Structure engine's own clustered estimator), the
ledger projects each engine's store faithfully and never writes, and the
scorecard's verdicts mean what the vocabulary says on planted data.
"""
import json
import math
import os
import random
import sqlite3

import pytest

from engines import legacy_import as li
from engines.learning import ledger, scorecard, stats as st


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_student_t_matches_textbook_values():
    assert st.t_two_sided_p(2.0, 10) == pytest.approx(0.07339, abs=1e-4)
    assert st.t_two_sided_p(-2.0, 10) == pytest.approx(0.07339, abs=1e-4)
    assert st.t_two_sided_p(1.96, 1e6) == pytest.approx(0.05, abs=1e-4)      # -> normal
    assert st.t_two_sided_p(12.706, 1) == pytest.approx(0.05, abs=1e-4)      # heavy tail
    assert st.t_crit(10, 0.95) == pytest.approx(2.2281, abs=1e-3)
    assert st.t_crit(5.6, 0.90) > st.t_crit(60, 0.90)                         # fractional df ok
    assert st.t_two_sided_p(float("inf"), 5) == 0.0
    assert st.t_two_sided_p(1.0, 0) is None


def test_estimators_match_the_structure_engine_when_there_is_no_overlap():
    """At overlap 0 the shared estimators must reproduce the CR1 date-clustered
    standard errors the Structure tuner has always used."""
    np = pytest.importorskip("numpy")
    oplog = pytest.importorskip("engines.structure.oplog")
    rng = random.Random(11)
    dates = [f"2026-09-{1 + i % 23:02d}" for i in range(400)]
    day = {d: rng.gauss(0, 1) for d in set(dates)}
    x = [day[d] + rng.gauss(0.2, 2) for d in dates]
    hi = [rng.random() < 0.4 for _ in x]

    e = st.mean_effect(x, dates)
    assert e.effect == pytest.approx(float(np.mean(x)))
    assert e.se == pytest.approx(oplog.clustered_mean_se(x, dates))
    assert (e.n, e.n_clusters, e.eff_clusters, e.df) == (400, 23, 23.0, 22.0)

    g = st.gap_effect(x, hi, dates)
    xa = np.asarray(x)
    assert g.effect == pytest.approx(float(xa[np.asarray(hi)].mean() - xa[~np.asarray(hi)].mean()))
    assert g.se == pytest.approx(oplog.clustered_gap_se(x, hi, dates))


def test_overlapping_horizons_widen_the_error_bar_and_cut_the_degrees_of_freedom():
    """A 10-day outcome logged on consecutive dates is nearly the same draw each
    day. Treating those dates as independent understates the error; the overlap
    correction must widen it, and must leave genuinely independent dates alone."""
    rng = random.Random(3)
    dates = [f"d{i:03d}" for i in range(120)]
    shocks = [rng.gauss(0, 1) for _ in range(130)]
    rolling = [sum(shocks[i:i + 10]) for i in range(120)]      # shares 9 of 10 shocks with its neighbour
    naive, robust = st.mean_effect(rolling, dates), st.mean_effect(rolling, dates, overlap=9)
    assert robust.se > 2 * naive.se
    assert (naive.eff_clusters, robust.eff_clusters) == (120.0, 12.0)
    assert robust.df == 11.0 and abs(robust.t) < abs(naive.t)

    iid = [rng.gauss(0, 1) for _ in range(120)]
    a, b = st.mean_effect(iid, dates), st.mean_effect(iid, dates, overlap=9)
    assert a.se <= b.se < 2 * a.se                             # never tighter; no blow-up


def _null_rejection_rate(overlap: int, corrected: bool, trials: int = 300) -> float:
    """Share of simulated no-edge samples in which a 10%-level test finds an edge.
    120 dates x 6 rows; each date's outcome is a rolling (overlap+1)-day window."""
    rng = random.Random(1)
    dates = [f"d{i:03d}" for i in range(120) for _ in range(6)]
    hits = 0
    for _ in range(trials):
        shocks = [rng.gauss(0, 1) for _ in range(120 + overlap)]
        x = []
        for i in range(120):
            day = sum(shocks[i:i + overlap + 1]) / math.sqrt(overlap + 1)
            x += [day + rng.gauss(0, 1.5) for _ in range(6)]
        hits += st.mean_effect(x, dates, overlap=overlap if corrected else 0).p < 0.10
    return hits / trials


def test_error_bars_are_calibrated_on_overlapping_outcomes():
    """The reason the correction exists, as a measurement: with NO true edge, a
    10%-level test should fire about 10% of the time. Plain date-clustering on
    10-day outcomes fires several times that; the corrected test must not."""
    assert _null_rejection_rate(overlap=9, corrected=False) > 0.40
    assert 0.04 <= _null_rejection_rate(overlap=9, corrected=True) <= 0.16
    assert 0.04 <= _null_rejection_rate(overlap=0, corrected=True) <= 0.16


def test_missing_dates_keep_their_place_in_the_lag_structure():
    """A cohort absent on some dates must still be lagged on the loop's calendar,
    not on its own compressed list of dates."""
    calendar = [f"d{i:02d}" for i in range(40)]
    # Runs of similar values: neighbours in the compressed list look persistent.
    vals = ([1.0] * 5 + [2.0] * 5) * 2
    dates = [calendar[i] for i in range(0, 40, 2)]
    compressed = st.mean_effect(vals, dates, overlap=1)
    on_calendar = st.mean_effect(vals, dates, overlap=1, order=calendar)
    assert on_calendar.se != pytest.approx(compressed.se)
    # On the calendar every neighbour is an empty date, so no autocovariance is
    # added: only the small-sample factor differs from the no-overlap error.
    plain = st.mean_effect(vals, dates)
    assert on_calendar.se == pytest.approx(plain.se * math.sqrt((20 - 1) / (20 - 3)))


def test_degenerate_inputs_do_not_raise_or_produce_invalid_json():
    assert st.mean_effect([], []).effect is None
    assert st.mean_effect([1.0], ["a"]).se is None                    # one date: no error bar
    assert st.mean_effect([None, float("nan")], ["a", "b"]).n == 0
    flat = st.mean_effect([-0.5] * 6, ["a", "a", "b", "b", "c", "c"])  # every date identical
    assert math.isinf(flat.t) and flat.p == 0.0
    json.dumps(flat.as_dict(), allow_nan=False)                       # inf -> null
    assert st.gap_effect([1, 2, 3], [True, True, True], ["a", "b", "c"]).effect is None


def test_benjamini_hochberg_and_descriptives():
    assert st.bh_qvalues([0.01, 0.04, None, 0.03, 0.005]) == pytest.approx(
        [0.02, 0.04, None, 0.04, 0.02])
    assert st.bh_qvalues([None, None]) == [None, None]
    lo, hi = st.wilson(12, 52)
    assert lo < 12 / 52 < hi and 0 < lo and hi < 1
    assert st.wilson(0, 0) is None
    assert st.brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    thr, mask = st.median_split([1, 1, 1, 5, None])                   # ties go low
    assert thr == 1 and mask == [False, False, False, True, False]


def _effect(n, dates, effect, p, overlap=0):
    return st.Effect(n=n, n_clusters=dates, eff_clusters=dates / (overlap + 1.0),
                     effect=effect, se=1.0, t=effect, df=dates - 1.0, p=p)


def test_verdicts_follow_the_vocabulary():
    family = [
        _effect(200, 60, +0.8, 0.001),              # strong and positive
        _effect(200, 60, -0.8, 0.002),              # strong and negative
        _effect(200, 60, +0.1, 0.60),               # judged, nothing there
        _effect(12, 60, +3.0, 0.0001),              # too few observations
        _effect(500, 9, +3.0, 0.0001),              # too few dates
        _effect(500, 66, +3.0, 0.0001, overlap=9),  # 66 dates but only 6.6 independent windows
    ]
    verdicts = [v for v, _ in st.judge(family)]
    assert verdicts == [st.PAYING, st.INVERTED, st.NOISE,
                        st.ACCUMULATING, st.ACCUMULATING, st.ACCUMULATING]
    # Only the three judged cohorts count as tests for the correction.
    assert [q is not None for _, q in st.judge(family)] == [True, True, True, False, False, False]
    assert set(st.VERDICTS) == {st.PAYING, st.INVERTED, st.NOISE, st.ACCUMULATING}
    # ...and an accumulating cohort says what it still needs.
    assert st.shortfall(family[0]) is None
    assert st.shortfall(family[3]) == {"observations": 18, "dates": 0}
    assert st.shortfall(family[4]) == {"observations": 0, "dates": 6}
    assert st.shortfall(family[5]) == {"observations": 0, "dates": 34}     # 100 dates for 10 windows


def test_many_tests_need_more_than_one_lucky_p_value():
    """One p=0.03 among twenty unrelated tests is what chance alone produces."""
    family = [_effect(200, 60, 1.0, 0.03)] + [_effect(200, 60, 0.1, 0.5 + i / 50) for i in range(19)]
    assert st.judge(family)[0][0] == st.NOISE
    assert st.judge(family[:1])[0][0] == st.PAYING                    # alone, it would pass


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_mirrored_constants_match_their_engines():
    oplog = pytest.importorskip("engines.structure.oplog")
    from engines.agent import grade_outcomes as go
    assert ledger.STRUCTURE_FLOW_FEATURES == oplog.FLOW_SPLIT_FEATURES
    assert ledger.NEUTRAL_PCT_FALLBACK == go.NEUTRAL_PCT_FALLBACK
    assert ledger.CAUTIOUS_PCT_FALLBACK == go.CAUTIOUS_PCT_FALLBACK
    assert go._grade_bias("neutral", 0.24, 1.0) == 1 and go._grade_bias("neutral", 0.26, 1.0) == 0
    assert go._grade_bias("cautious", 0.49, 1.0) == 1 and go._grade_bias("cautious", 0.51, 1.0) == 0
    assert (ledger.NEUTRAL_EM_FRACTION, ledger.CAUTIOUS_EM_FRACTION) == (0.25, 0.5)


def _brief_store(path, calls):
    """calls: [(date, {ticker: (bias, conviction, day_pct, bias_correct, em)})]"""
    con = sqlite3.connect(path)
    con.executescript(li._AGENT_SCHEMA)
    for date, per in calls:
        plan = {"overall_risk_rating": "elevated",
                "gameplan": {t: {"bias": b, "conviction": c} for t, (b, c, *_r) in per.items()}}
        opts = {"options": {t: {"gamma_environment": "negative",
                                "expected_move": {"move_pct": em}}
                            for t, (*_x, em) in per.items()}}
        cur = con.execute(
            "INSERT INTO sessions (date, fetched_at, market_data, macro_data, options_data, gameplan) "
            "VALUES (?,?,?,?,?,?)", (date, "t", "{}", "{}", json.dumps(opts), json.dumps(plan)))
        for t, (_b, _c, pct, correct, _em) in per.items():
            con.execute("INSERT INTO outcomes (session_id, ticker, day_pct, bias_correct) "
                        "VALUES (?,?,?,?)", (cur.lastrowid, t, pct, correct))
    con.commit()
    con.close()


def _structure_store(path, rows):
    """rows: [(run_date, ticker, type, direction, horizon, excess, win, score, features)]"""
    oplog = pytest.importorskip("engines.structure.oplog")
    con = sqlite3.connect(path)
    con.executescript(oplog._SCHEMA)
    for d, tk, typ, direction, h, excess, win, score, feats in rows:
        oid = f"{d}:{tk}:{typ}"
        con.execute("INSERT OR IGNORE INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, d + "T21:00:00+00:00", d, tk, typ, direction, 100.0, 2.0, None, None,
                     score, score, json.dumps(feats)))
        raw = 0.5 if excess is None else excess          # excess None = labeled with no benchmark
        con.execute(
            "INSERT INTO outcomes (opp_id, horizon_days, labeled_at, n_bars, fwd_return, "
            "signed_return, mfe_atr, mae_atr, bench_return, excess_return, win) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (oid, h, "t", h, raw, raw, 0.0, 0.0, None if excess is None else 0.0, excess, win))
    con.commit()
    con.close()


def _zerodte_store(path, touches):
    """touches: [(date, ticker, kind, outcome, mag_before)]"""
    from engines.zerodte import db as zdb
    con = sqlite3.connect(path)
    con.executescript(zdb._SCHEMA)
    for i, (d, tk, kind, outcome, mag) in enumerate(touches):
        payload = {"id": f"{kind}:{i}", "strike": 500.0, "kind": kind, "price_outcome": outcome,
                   "label": {"REJECTED": "DEFENDED", "BREACHED": "SLIPPED"}.get(outcome, "CONTESTED"),
                   "flow": {"mag_before": mag, "mag_after": mag}}
        con.execute("INSERT INTO touches VALUES (?,?,?,?)", (d, tk, f"{kind}:{i}", json.dumps(payload)))
    con.commit()
    con.close()


def test_brief_rows_score_timing_not_drift(tmp_path):
    """Three up days. Calling them all bullish earns the drift and nothing else;
    the edge must net that out, so it sums to zero."""
    d = str(tmp_path)
    _brief_store(os.path.join(d, "agent.sqlite3"), [
        ("2026-11-10", {"SPY": ("Bullish", 0.6, 1.0, 1, 1.0)}),
        ("2026-11-11", {"SPY": ("bullish", 0.6, 2.0, 1, 1.0)}),
        ("2026-11-12", {"SPY": ("bearish", 0.4, 3.0, 0, 1.0)}),
        ("2026-11-13", {"SPY": ("neutral", None, 0.2, 1, 1.0), "QQQ": ("neutral", None, 0.9, 0, None)}),
    ])
    rows = ledger.brief(d)
    spy = [r for r in rows if r["ticker"] == "SPY"]
    assert [r["direction"] for r in spy] == [1, 1, -1, 0]
    drift = (1.0 + 2.0 + 3.0 + 0.2) / 4
    assert [r["edge"] for r in spy[:3]] == pytest.approx([1.0 - drift, 2.0 - drift, -(3.0 - drift)])
    assert spy[3]["edge"] is None and spy[3]["hit"] == 1
    # Band flags say what a neutral call would have scored on ANY day.
    assert [r["in_neutral_band"] for r in spy] == [0, 0, 0, 1]             # band = 0.25 * 1.0
    qqq = [r for r in rows if r["ticker"] == "QQQ"][0]
    assert qqq["in_neutral_band"] == 0 and qqq["in_cautious_band"] == 0    # fallbacks 0.3 / 0.5
    assert ledger.coverage(rows) == {"observations": 5, "dates": 4, "first": "2026-11-10",
                                     "last": "2026-11-13", "tickers": 2}


def test_structure_zerodte_and_missing_stores(tmp_path):
    d = str(tmp_path)
    _structure_store(os.path.join(d, "opportunities.db"), [
        ("2026-11-02", "AAPL", "support", "up", 5, 1.5, 1, 0.7, {"iv_skew": 0.1, "flow_pocket_share": True}),
        ("2026-11-02", "AAPL", "support", "up", 10, None, 1, 0.7, {"iv_skew": 0.1}),
        ("2026-11-03", "MSFT", "flag", "down", 5, -0.5, 0, 0.2, {}),
    ])
    rows = ledger.structure(d)
    assert [(r["type"], r["horizon"], r["edge"], r["direction"]) for r in rows] == [
        ("support", 5, 1.5, 1), ("support", 10, None, 1), ("flag", 5, -0.5, -1)]
    assert rows[0]["features"]["iv_skew"] == 0.1
    assert rows[0]["features"]["flow_pocket_share"] is None            # a bool is not a number
    assert rows[2]["features"]["iv_skew"] is None

    _zerodte_store(os.path.join(d, "zerodte.sqlite3"), [
        ("2026-11-12", "SPY", "wall_above", "REJECTED", -4e8),
        ("2026-11-12", "SPY", "wall_below", "BREACHED", 2e8),
        ("2026-11-12", "QQQ", "wall_below", "CHURN", None),
    ])
    z = sorted(ledger.zerodte(d), key=lambda r: (r["ticker"], r["kind"]), reverse=True)
    assert [(r["ticker"], r["kind"], r["hit"], r["wall_size"]) for r in z] == [
        ("SPY", "wall_below", 0, 2e8), ("SPY", "wall_above", 1, 4e8),
        ("QQQ", "wall_below", None, None)]

    assert ledger.flow(d) == [] and ledger.brief(d) == []              # stores not there
    with open(os.path.join(d, "flow_learning.db"), "w") as f:
        f.write("not a database")
    assert ledger.flow(d) == []                                        # unreadable: no rows, no raise


def test_the_ledger_never_writes_to_a_store(tmp_path):
    d = str(tmp_path)
    path = os.path.join(d, "zerodte.sqlite3")
    _zerodte_store(path, [("2026-11-12", "SPY", "wall_above", "REJECTED", 1e8)])
    before = (os.path.getsize(path), os.stat(path).st_mtime_ns, sorted(os.listdir(d)))
    scorecard.build(d)
    assert (os.path.getsize(path), os.stat(path).st_mtime_ns, sorted(os.listdir(d))) == before
    con = ledger._ro(path)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM touches")
    con.close()


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------

def _planted_structure(path, n_dates=60):
    """support earns +1% net of benchmark, resistance loses 1%, flags are noise —
    each with a shared daily shock so same-day rows really are correlated."""
    rng = random.Random(7)
    rows = []
    for i in range(n_dates):
        d = f"2026-{7 + i // 28:02d}-{1 + i % 28:02d}"
        shock = rng.gauss(0, 0.6)
        for j in range(6):
            tk = f"T{j}"
            rows.append((d, tk, "support", "up", 5, 1.0 + shock + rng.gauss(0, 1.5), 1, rng.random(), {}))
            rows.append((d, tk, "resistance", "down", 5, -1.0 + shock + rng.gauss(0, 1.5), 0, rng.random(), {}))
            rows.append((d, tk, "flag", "up", 5, shock + rng.gauss(0, 1.5), 1, rng.random(), {}))
            rows.append((d, tk, "support", "up", 10, 1.0 + shock + rng.gauss(0, 1.5), 1, rng.random(), {}))
    _structure_store(path, rows)


def test_scorecard_reads_planted_edges_correctly(tmp_path):
    d = str(tmp_path)
    _planted_structure(os.path.join(d, "opportunities.db"))
    card = scorecard.build(d)
    json.dumps(card, allow_nan=False)                                  # strictly valid JSON
    assert [lp["loop"] for lp in card["loops"]] == list(ledger.LOOPS)
    s = next(lp for lp in card["loops"] if lp["loop"] == "structure")
    setup = {c["cohort"]: c for c in s["families"][0]["cohorts"]}
    assert setup["support · 5d"]["verdict"] == st.PAYING
    assert setup["resistance · 5d"]["verdict"] == st.INVERTED
    assert setup["flag · 5d"]["verdict"] == st.NOISE
    # 60 dates of 10-day outcomes are only 6 independent windows: not judged yet.
    assert setup["support · 10d"]["verdict"] == st.ACCUMULATING
    assert setup["support · 10d"]["needs"] == {"observations": 0, "dates": 40}
    assert setup["support · 10d"]["effect"] > 0.5 and setup["support · 10d"]["q"] is None
    assert setup["support · 5d"]["ci_low"] > 0 > setup["resistance · 5d"]["ci_high"]
    # The score was random, so it must not be credited with ranking anything.
    assert {c["verdict"] for c in s["families"][1]["cohorts"]} <= {st.NOISE, st.ACCUMULATING}
    assert s["headline"] == {"paying": 1, "inverted": 1, "noise": 1, "accumulating": 1}
    # Empty loops are present, empty, and say so.
    f = next(lp for lp in card["loops"] if lp["loop"] == "flow")
    assert f["coverage"]["observations"] == 0 and f["families"][0]["cohorts"] == []
    assert scorecard.summary(card)["structure"]["headline"]["paying"] == 1


def test_zerodte_reports_the_hold_rate_but_only_judges_relative_questions(tmp_path):
    """Held and broke are defined asymmetrically by the engine, so there is no
    coin-flip baseline: the absolute rate is described, never given a verdict."""
    d = str(tmp_path)
    rng = random.Random(9)
    touches = []
    for i in range(40):
        day = f"2026-09-{1 + i % 28:02d}" if i < 28 else f"2026-10-{i - 27:02d}"
        for tk in ("SPY", "QQQ"):
            for kind, p_hold in (("wall_above", 0.6), ("wall_below", 0.15)):
                for _ in range(4):
                    touches.append((day, tk, kind,
                                    "REJECTED" if rng.random() < p_hold else "BREACHED",
                                    rng.uniform(1e8, 9e8)))
            touches.append((day, tk, "wall_above", "CHURN", 1e8))
    _zerodte_store(os.path.join(d, "zerodte.sqlite3"), touches)
    z = next(lp for lp in scorecard.build(d)["loops"] if lp["loop"] == "zerodte")
    assert [f["family"] for f in z["families"]] == ["separates"]
    desc = z["descriptive"]
    assert desc["touches"] == len(touches) and desc["churned"] == 80
    assert desc["held"] + desc["broke"] == len(touches) - 80 and 0 < desc["hold_rate"] < 100
    cohorts = {c["cohort"]: c for c in z["families"][0]["cohorts"]}
    kind = cohorts["wall_above minus wall_below"]
    assert kind["verdict"] == st.PAYING and 30 < kind["effect"] < 60        # planted 45pp gap
    assert cohorts["SPY minus the other tickers"]["verdict"] == st.NOISE
    assert not any("label" in c["cohort"].lower() for c in cohorts.values())


def test_brief_range_calls_are_judged_against_their_base_rate(tmp_path):
    """A neutral call that is right 30% of the time is not bad if only 30% of all
    days stay in the band — it is exactly uninformative."""
    d = str(tmp_path)
    rng = random.Random(2)
    calls = []
    for i in range(60):
        day = f"2026-{8 + i // 28:02d}-{1 + i % 28:02d}"
        per = {}
        for tk in ("SPY", "QQQ", "IWM"):
            pct = rng.gauss(0.05, 0.8)
            quiet = abs(pct) <= 0.25
            if rng.random() < 0.5:
                per[tk] = ("neutral", None, pct, int(quiet), 1.0)
            else:
                per[tk] = ("bullish", 0.55, pct, int(pct > 0), 1.0)
        calls.append((day, per))
    _brief_store(os.path.join(d, "agent.sqlite3"), calls)
    b = next(lp for lp in scorecard.build(d)["loops"] if lp["loop"] == "brief")
    fams = {f["family"]: f for f in b["families"]}
    neutral = fams["range"]["cohorts"][0]
    assert neutral["cohort"] == "neutral" and neutral["verdict"] == st.NOISE
    assert abs(neutral["effect"]) < 15 and neutral["base_rate"] == pytest.approx(neutral["hit_rate"], abs=15)
    assert fams["direction"]["cohorts"][0]["cohort"] == "all directional calls"
    assert fams["conviction"]["cohorts"][0]["verdict"] == st.ACCUMULATING   # every conviction equal


def test_cache_rebuilds_only_when_a_store_changes(tmp_path, monkeypatch):
    d = str(tmp_path)
    path = os.path.join(d, "zerodte.sqlite3")
    _zerodte_store(path, [("2026-11-12", "SPY", "wall_above", "REJECTED", 1e8)])
    first = scorecard.cached(d, max_age=0)
    assert scorecard.cached(d, max_age=0) is first                     # nothing written
    _zerodte_store(path, [("2026-11-13", "SPY", "wall_above", "BREACHED", 1e8)])
    second = scorecard.cached(d, max_age=0)
    assert second is not first
    assert second["loops"][3]["coverage"]["observations"] == 2
    _zerodte_store(path, [("2026-11-14", "SPY", "wall_above", "BREACHED", 1e8)])
    assert scorecard.cached(d, max_age=3600) is second                 # within max_age: no rebuild


def test_routes_serve_the_scorecard():
    import engines
    card = engines.produce("local://learning/api/scorecard.json")
    assert [lp["loop"] for lp in card["loops"]] == list(ledger.LOOPS)
    one = engines.produce("local://learning/api/scorecard.json?loop=brief")
    assert [lp["loop"] for lp in one["loops"]] == ["brief"]

    import app as appmod
    client = appmod.app.test_client()
    r = client.get("/api/learning.json?loop=zerodte")
    assert r.status_code == 200 and [lp["loop"] for lp in r.get_json()["loops"]] == ["zerodte"]
    assert set(client.get("/healthz").get_json()["learning"]) == set(ledger.LOOPS)


# ---------------------------------------------------------------------------
# display model + pages (desk/learning_view.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def planted_view(tmp_path, monkeypatch):
    """The display model wired to a scorecard built from planted structure data."""
    from desk import learning_view as lv
    d = str(tmp_path)
    _planted_structure(os.path.join(d, "opportunities.db"))
    card = scorecard.build(d)
    monkeypatch.setattr(lv, "_scorecard", lambda: card)
    return lv


def test_view_says_what_the_scorecard_decided(planted_view):
    lv = planted_view
    page = lv.page()
    assert page["available"] and [lp["key"] for lp in page["loops"]] == list(ledger.LOOPS)
    assert [v["key"] for v in page["legend"]] == ["paying", "inverted", "noise", "accumulating"]
    s = next(lp for lp in page["loops"] if lp["key"] == "structure")
    assert s["sessions"] == 60 and s["verdict"] == "paying"
    assert "Paying: support · 5d." in s["read"] and "Reliably wrong: resistance · 5d." in s["read"]
    assert "1 still accumulating (soonest needs 40 more sessions)." in s["read"]
    lead = s["families"][0]
    assert lead["open"] and not s["families"][1]["open"]              # diagnostics fold away
    rows = {r["cohort"]: r for r in lead["rows"]}
    paying, waiting = rows["support · 5d"], rows["support · 10d"]
    # Every verdict is a glyph and a word, never a colour alone.
    assert (paying["v"]["glyph"], paying["v"]["label"]) == ("▲", "Paying")
    assert paying["judged"] and 50 < paying["mark"]["dot"] <= 100     # right of zero
    assert paying["mark"]["lo"] > 50                                  # whole interval clears zero
    assert rows["resistance · 5d"]["mark"]["dot"] < 50
    assert not waiting["judged"] and waiting["needs"] == "40 more sessions"
    assert waiting["progress"] == 60                                  # 60 of the 100 sessions needed
    assert paying["needs"] == "" and paying["progress"] is None
    assert lead["has_overlap"] and lead["axis"]["lo"].startswith("−")
    # An empty loop reads as such rather than as a verdict.
    flow = next(lp for lp in page["loops"] if lp["key"] == "flow")
    assert flow["read"] == "No labelled history yet." and flow["sessions"] == 0


def test_axis_is_set_by_judged_cohorts_and_outliers_are_clipped():
    from desk import learning_view as lv
    judged = {"cohort": "a", "verdict": "noise", "effect": 0.2, "ci_low": -0.4, "ci_high": 0.8,
              "n": 100, "n_clusters": 60, "eff_clusters": 60.0}
    wild = {"cohort": "b", "verdict": "accumulating", "effect": -48.0, "ci_low": -90.0,
            "ci_high": -6.0, "n": 4, "n_clusters": 2, "eff_clusters": 2.0,
            "needs": {"observations": 26, "dates": 13}}
    assert lv._axis_max([judged, wild]) == 0.8
    assert lv._axis_max([wild]) == 50                                 # nothing judged: estimates
    row = lv._row(wild, "percentage points", 0.8)
    assert row["mark"]["clipped"] and row["mark"]["dot"] == 0.0
    assert row["needs"] == "13 more sessions · 26 more observations"
    assert lv._row(judged, "% return", 0.8)["effect"] == "+0.20"
    assert lv._row(judged, "percentage points", 0.8)["effect"] == "+0.2"
    assert [lv._nice_ceiling(x) for x in (0.22, 1.02, 2.33, 13.5, 48.0)] == [0.25, 1.5, 2.5, 15, 50]


def test_interval_clear_of_zero_but_held_back_by_the_family_is_flagged():
    from desk import learning_view as lv
    c = {"cohort": "bullish", "verdict": "noise", "effect": 0.23, "ci_low": 0.01, "ci_high": 0.45,
         "n": 75, "n_clusters": 34, "eff_clusters": 34.0}
    assert lv._row(c, "% move", 0.5)["dagger"]
    assert not lv._row({**c, "ci_low": -0.01}, "% move", 0.5)["dagger"]
    assert not lv._row({**c, "verdict": "paying"}, "% move", 0.5)["dagger"]


def test_digest_and_structure_tiles(planted_view):
    lv = planted_view
    lines = lv.digest()
    assert [ln["src"] for ln in lines] == ["Structure setups"]        # loops with no history are omitted
    assert lines[0]["verdict"] == "paying" and lines[0]["v"]["label"] == "Paying"
    tiles = lv.structure_tiles()
    assert tiles["sessions"] == 60 and [t["label"] for t in tiles["tiles"]] == [
        "flag · 5d", "resistance · 5d", "support · 5d", "support · 10d"]
    support = tiles["tiles"][2]
    assert support["value"].startswith("+") and support["value"].endswith("%")
    assert "win 100%" in support["sub"] and support["verdict"] == "paying"
    assert tiles["tiles"][3]["needs"] == "40 more sessions"


def test_scorecard_digest_replaces_the_adapters_track_lines_only_when_there_is_history(
        planted_view, monkeypatch):
    from desk import fetch
    adapter_lines = [{"src": "Structure", "text": "resistance setups win 68% at the 5d horizon"}]
    s = {"eod": {"track_record": {"lines": list(adapter_lines)}}, "structure": {}}
    fetch._attach_scorecard(s)
    assert s["eod"]["track_record"]["scorecard"] is True
    assert [ln["src"] for ln in s["eod"]["track_record"]["lines"]] == ["Structure setups"]
    assert s["structure"]["scorecard"]["tiles"]

    monkeypatch.setattr(planted_view, "_scorecard", lambda: None)     # nothing readable
    s = {"eod": {"track_record": {"lines": list(adapter_lines)}}, "structure": {}}
    fetch._attach_scorecard(s)
    assert s["eod"]["track_record"]["lines"] == adapter_lines and "scorecard" not in s["structure"]


def test_learning_page_renders(planted_view):
    import app as appmod
    client = appmod.app.test_client()
    r = client.get("/learning")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "What the Desk Has Earned" in html and 'id="loop-structure"' in html
    assert html.count('class="ci"') >= 4 and "ci-dot hollow" in html  # unjudged = hollow dot
    assert "40 more sessions" in html and "Indep. windows" in html
    assert "var REFRESH_MS = 0;" in html                              # no auto-reload here...
    assert "var REFRESH_MS = 60000;" in client.get("/eod").get_data(as_text=True)   # ...only here
    assert ">Learning</a>" in client.get("/").get_data(as_text=True)  # in the nav


def test_learning_page_without_a_scorecard_says_so(monkeypatch):
    from desk import learning_view as lv
    import app as appmod
    monkeypatch.setattr(lv, "_scorecard", lambda: None)
    r = appmod.app.test_client().get("/learning")
    assert r.status_code == 200 and "not available right now" in r.get_data(as_text=True)
    assert lv.digest() == [] and lv.structure_tiles() is None
