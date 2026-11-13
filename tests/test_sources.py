"""Adapter + fail-soft tests for the desk data layer.

Runs without a network: the upstream HTTP calls are monkeypatched to return
captured fixtures (a real Morning Brief payload, a representative 0DTE state).
Run with pytest, or directly:  python tests/test_sources.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = ROOT / "desk" / "sources" / "fixtures"

from desk import fetch, sample_data            # noqa: E402
from desk.sources import agent, zerodte, flow, structure   # noqa: E402


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


CFG = {"url": "http://upstream", "token": "", "header": "Authorization"}


def test_agent_maps_brief_and_risk():
    brief, perf = _load("brief.json"), _load("performance.json")

    def dispatch(url, *a, **k):
        return perf if url.endswith("/api/performance.json") else brief
    agent.http_get_json = dispatch
    c = agent.collect(CFG, 2.0)

    assert c["risk_rating"] == "High" and c["risk_class"] == "off"   # overall_risk_rating: high
    assert c["brief"]["globals"], "global markets should map"
    assert c["brief"]["globals"][0]["region"] == "Asia"
    assert c["brief"]["watch"], "watch list should map"

    spy = c["biases"]["SPY"]
    assert spy["bias_class"] in ("bullish", "bearish", "neutral", "cautious")
    # This captured session predates the conviction field — must degrade, not crash.
    assert spy["conviction"] == "—" and spy["conviction_pct"] == 0

    # EOD grading + synthesized track record (the learning loop).
    grades = {g["sym"]: g for g in c["eod"]["grades"]}
    assert grades["SPY"]["correct"] == "Yes" and grades["IWM"]["correct"] == "No"
    assert any(t["label"] == "Brier" and t["value"] == "0.21" for t in c["eod"]["performance"])
    assert c["track_record"]["lines"] and "Gameplan bias" in c["track_record"]["lines"][0]["text"]
    assert c["calibration"]["SPY"]["acc"] == 61 and c["calibration"]["SPY"]["n"] == 82

    s = sample_data.snapshot()
    agent.merge(s, c)
    assert s["context"]["risk_rating"] == "High"
    assert any(t["sym"] == "SPY" and t["bias"] == spy["bias"] for t in s["tickers"])
    assert s["eod"]["grades"][0]["correct"] in ("Yes", "No")
    assert s["eod"]["track_record"]["lines"]
    assert next(t for t in s["tickers"] if t["sym"] == "SPY")["calib"]["acc"] == 61
    print("  agent: brief+risk, EOD grades+Brier, track record, calibration, merge OK")


def test_app_token_gate():
    import os
    import app as appmod
    with appmod.app.test_client() as cl:
        os.environ["APP_TOKEN"] = "secret"
        try:
            assert cl.get("/").status_code == 401                # no token -> blocked
            assert cl.get("/healthz").status_code == 200         # health stays open
            assert cl.get("/?token=secret").status_code == 200   # query-param auth
            assert cl.get("/gameplan").status_code == 200        # cookie persists
        finally:
            os.environ.pop("APP_TOKEN", None)
    print("  gate: APP_TOKEN enforced, healthz open, cookie persists")


def test_store_logs_and_grades(tmp=None):
    import tempfile
    from desk import store
    store.DB_PATH = tempfile.mktemp(suffix=".sqlite3")
    s = sample_data.snapshot()
    store.record_snapshot(s)
    store.ingest_grades(s["eod"]["grades"])          # sample: Yes/Yes/No
    summary = store.summary()
    assert summary["sessions"] == 1 and summary["graded"] == 3 and summary["correct"] == 2
    items = store.track_record_items()
    assert any("Desk journal" in it["text"] for it in items)
    assert any("hit-rate 2/3" in it["text"] for it in items)
    print("  store: snapshot logged, grades ingested, desk track-record items OK")


def test_zerodte_maps_levels_and_touches():
    touchstats = {"available": True, "n": 420, "labels": {
        "DEFENDED": {"n": 180, "rejected": 130, "breached": 40, "churn": 10},
        "ATTACKED — BROKEN": {"n": 96, "rejected": 23, "breached": 70, "churn": 3},
    }}

    def dispatch(url, *a, **k):
        return touchstats if url.endswith("/api/touch-stats.json") else _load("state.json")
    zerodte.http_get_json = dispatch
    c = zerodte.collect(CFG, 2.0)

    spy = c["levels"]["SPY"]
    assert spy["spot"] == "678.90" and spy["regime"] == "PIN"
    names = [lv["name"] for lv in spy["levels"]]
    assert "Call Wall" in names and "Put Wall" in names and "Zero Gamma" in names and "Spot" in names
    cw = next(lv for lv in spy["levels"] if lv["name"] == "Call Wall")
    assert cw["price"] == "684.00"
    # ladder is price-descending; call wall (684) above spot (678.90)
    assert spy["levels"][0]["name"] == "Call Wall"
    assert 60 <= spy["observed"] <= 80   # mean of 0.71 / 0.68

    assert c["touches"], "touches should map"
    assert c["touches"][0]["tag_class"] in ("defended", "breached", "contested")
    assert any(t["sym"] == "QQQ" and t["tag_class"] == "breached" for t in c["touches"])

    # Level-behaviour calibration (recursive learning).
    assert c["touch_stats"]["rows"][0]["label"] == "DEFENDED"   # largest n first
    assert c["touch_stats"]["rows"][0]["hold_pct"] == 72        # 130/180
    assert "defended levels held 72%" in c["track_line"]["text"]

    s = sample_data.snapshot()
    zerodte.merge(s, c)
    assert next(t for t in s["tickers"] if t["sym"] == "SPY")["spot"] == "678.90"
    assert s["context"]["tickers"][0]["price"] == "678.90"
    assert s["intraday"]["touch_stats"]["rows"]
    assert any("defended levels held" in ln["text"] for ln in s["eod"]["track_record"]["lines"])
    print("  zerodte: SPY levels/regime/observed/touches mapped, ladder ordered, merge OK")


def test_flow_maps_waves_eod_and_skew():
    fixtures = {
        "/api/index-etfs/waves": _load("index_etfs_waves.json"),
        "/api/index-etfs/eod": _load("index_etfs_eod.json"),
        "/api/index-etfs": _load("index_etfs.json"),
    }

    def dispatch(url, *a, **k):
        for path in ("/api/index-etfs/waves", "/api/index-etfs/eod", "/api/index-etfs"):
            if url.endswith(path):
                return fixtures[path]
        raise AssertionError(f"unexpected url {url}")
    flow.http_get_json = dispatch

    c = flow.collect(CFG, 2.0)
    # Only the two is_surge tickers become wave banners (IWM is not a surge).
    assert len(c["flow"]["waves"]) == 2
    q = c["flow"]["waves"][0]
    assert q["sym"] == "QQQ" and q["call_pct"] == 80 and q["side"] == "call"
    assert "led by $604C" in q["lead"] and "$1.9M fresh" in q["meta"]

    skews = {d["sym"]: d for d in c["flow"]["session_skew"]}
    assert skews["SPY"]["call_pct"] == 57 and skews["QQQ"]["call_pct"] == 68
    assert skews["IWM"]["label"].endswith("puts")

    eod = {d["sym"]: d for d in c["flow"]["eod"]}
    assert eod["SPY"]["lean"] == "call" and eod["IWM"]["lean"] == "put"
    assert "$4.1M" in eod["SPY"]["detail"]

    s = sample_data.snapshot()
    flow.merge(s, c)
    assert s["flow"]["waves"][0]["sym"] == "QQQ"
    assert len(s["intraday"]["skew"]) == 3
    fcard = next(cd for cd in s["overview_cards"] if cd["href"] == "flow")
    assert any(st.get("l") == "Active waves" and st["v"] == "2" for st in fcard["stats"])
    print("  flow: waves/session-skew/eod mapped, intraday skew + overview card merged")


def test_flow_sweeps_and_ticker():
    flow.http_get_json = lambda *a, **k: _load("sweeps.json")
    rows = flow.fetch_sweeps(CFG, 2.0)
    assert len(rows) == 2 and rows[0]["sym"] == "NVDA"
    assert rows[0]["cp"] == "C" and rows[0]["premium"] == "$3.2M" and rows[0]["is_new"] is True
    assert rows[1]["cp"] == "P" and rows[1]["voi"] == "3.3"

    flow.http_get_json = lambda *a, **k: _load("ticker.json")
    tk = flow.fetch_ticker(CFG, "NVDA", 2.0)
    assert tk["sym"] == "NVDA" and tk["lean"] == "call" and tk["call_pct"] == 71   # premium-weighted
    assert tk["call_vol"] == "142,000" and tk["premium"] == "$18.4M"
    assert tk["by_expiry"][0]["expiry"] == "2026-11-14" and tk["by_expiry"][0]["call_pct"] == 73
    assert tk["by_strike"][0]["strike"] == "190" and tk["by_strike"][0]["call_pct"] == 86
    print("  flow drill: sweeps rows + per-ticker lean/expiry/strike mapped")


def test_structure_maps_recs_rotation_quant():
    import urllib.parse as up
    from desk import config
    config.SOURCES["flow"]["url"] = "http://flow"
    config.SOURCES["stock"]["url"] = "http://stock"
    recs, rot = _load("recs.json"), _load("rotation.json")
    quant = {"NVDA": 72, "AMD": 68, "META": 54, "XOM": 63, "TSLA": 41}

    learning = {"available": True, "n_outcomes": 310, "min_samples": 40, "groups": [
        {"type": "resistance", "horizon": 5, "n": 48, "win_pct": 61.0, "avg_return": 1.4},
        {"type": "support", "horizon": 5, "n": 39, "win_pct": 54.0, "avg_return": 0.6},
        {"type": "resistance", "horizon": 10, "n": 40, "win_pct": 58.0, "avg_return": 2.0},
    ]}

    def dispatch(url, *a, **k):
        if url.endswith("/api/recs.json"):
            return recs
        if url.endswith("/api/rotation"):
            return rot
        if url.endswith("/api/learning.json"):
            return learning
        if "/api/analyze" in url:
            t = up.parse_qs(up.urlparse(url).query).get("ticker", ["?"])[0]
            return {"scorecard": {"overall": quant.get(t)}}
        raise AssertionError(f"unexpected url {url}")
    structure.http_get_json = dispatch

    c = structure.collect(CFG, 2.0)
    # Self-calibration (recursive learning): shortest horizon, sample-gated.
    assert c["learning"]["horizon"] == 5
    lr = {r["type"]: r for r in c["learning"]["rows"]}
    assert lr["Resistance"]["enough"] is True and lr["Support"]["enough"] is False
    assert "resistance setups win 61%" in c["track_line"]["text"]
    rows = {r["sym"]: r for r in c["recs"]}
    nv = rows["NVDA"]
    assert nv["dir"] == "▲ Up" and nv["zone"] == "Thin resistance" and nv["dist"] == "+0.9%"
    assert nv["comp"]["z"] is True and nv["comp"]["g"] == "amp" and nv["comp"]["r"] == "amp"
    assert nv["rotation"] == "Leading" and nv["rotation_class"] == "q-leading"
    assert nv["quant"] == "72" and nv["quant_class"] == "up"
    assert nv["live"] == "Firing" and nv["score"] == "0.88" and nv["score_color"] == "var(--green-hot)"
    assert rows["META"]["rotation"] == "Weakening" and rows["META"]["dir"] == "▼ Down" and rows["META"]["zone"] == "Thin support"
    assert rows["TSLA"]["rotation"] == "Lagging" and rows["TSLA"]["quant_class"] == "down"
    assert c["card"]["passing"] == 5 and c["card"]["firing"] == 2 and c["card"]["top"] == "0.88"

    s = sample_data.snapshot()
    structure.merge(s, c)
    assert s["structure"]["recs"][0]["sym"] == "NVDA"
    scard = next(cd for cd in s["overview_cards"] if cd["href"] == "structure")
    assert any(st.get("l") == "Passing gate" and st["v"] == "5" for st in scard["stats"])
    assert s["structure"]["learning"]["rows"]
    assert any("resistance setups win" in ln["text"] for ln in s["eod"]["track_record"]["lines"])
    config.SOURCES["flow"]["url"] = ""
    config.SOURCES["stock"]["url"] = ""
    print("  structure: recs+rotation+quant+win-rates mapped, track line, merge OK")


def test_derive_overview_cards():
    s = sample_data.snapshot()
    fetch._derive_overview_cards(s)
    cards = {c["href"]: c for c in s["overview_cards"]}
    g = cards["gameplan"]["stats"]
    assert any(st.get("l") == "SPY regime" and st["badge"][0] == "PIN" for st in g)
    it = cards["intraday"]["stats"]
    assert any(st.get("l") == "Touches today" and st["v"] == str(len(s["intraday"]["touches"])) for st in it)
    e = cards["eod"]["stats"]
    assert any(st.get("l") == "Plan grade" and st["v"] == "Good" for st in e)       # 2/3 correct
    assert any(st.get("l") == "Carried" and st["v"] == "4" for st in e)
    print("  overview: cards derived from live snapshot (regimes / touches / grade)")


def test_fetch_fails_soft(monkeypatch=None):
    from desk import config
    # Enable just the agent source, then make its fetch blow up.
    config.SOURCES["agent"]["url"] = "http://unreachable"
    config.SOURCES["zerodte"]["url"] = ""
    config.SOURCES["flow"]["url"] = ""
    def boom(*a, **k):
        raise OSError("connection refused")
    agent.http_get_json = boom

    s = fetch.get_snapshot(force=True)
    assert "agent" in s["degraded"], "failed source must be recorded"
    assert s["live"] is True and s["mock"] is False
    # Section falls back to sample — page still renders with real content.
    assert "Market structure" in s["brief"]["structure_html"]
    config.SOURCES["agent"]["url"] = ""   # reset so later calls are sample-mode
    print("  fetch: agent failure degraded + fell back to sample, page intact")


def test_sample_mode_when_unconfigured():
    from desk import config
    for name in ("agent", "zerodte", "flow", "structure", "stock"):
        config.SOURCES[name]["url"] = ""
    s = fetch.get_snapshot(force=True)
    assert s["mock"] is True and s["live"] is False and s["degraded"] == {}
    print("  fetch: no URLs -> sample mode")


if __name__ == "__main__":
    for fn in (test_agent_maps_brief_and_risk, test_zerodte_maps_levels_and_touches,
               test_flow_maps_waves_eod_and_skew, test_flow_sweeps_and_ticker,
               test_structure_maps_recs_rotation_quant,
               test_store_logs_and_grades, test_app_token_gate, test_derive_overview_cards,
               test_fetch_fails_soft, test_sample_mode_when_unconfigured):
        fn()
    print("ALL PASS")
