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
from desk.sources import agent, zerodte        # noqa: E402


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


CFG = {"url": "http://upstream", "token": "", "header": "Authorization"}


def test_agent_maps_brief_and_risk():
    agent.http_get_json = lambda *a, **k: _load("brief.json")
    c = agent.collect(CFG, 2.0)

    assert c["risk_rating"] == "High" and c["risk_class"] == "off"   # overall_risk_rating: high
    assert c["brief"]["globals"], "global markets should map"
    assert c["brief"]["globals"][0]["region"] == "Asia"
    assert c["brief"]["watch"], "watch list should map"

    spy = c["biases"]["SPY"]
    assert spy["bias_class"] in ("bullish", "bearish", "neutral", "cautious")
    # This captured session predates the conviction field — must degrade, not crash.
    assert spy["conviction"] == "—" and spy["conviction_pct"] == 0

    s = sample_data.snapshot()
    agent.merge(s, c)
    assert s["context"]["risk_rating"] == "High"
    assert any(t["sym"] == "SPY" and t["bias"] == spy["bias"] for t in s["tickers"])
    print("  agent: risk=High, globals+watch mapped, missing-conviction handled, merge OK")


def test_zerodte_maps_levels_and_touches():
    zerodte.http_get_json = lambda *a, **k: _load("state.json")
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

    s = sample_data.snapshot()
    zerodte.merge(s, c)
    assert next(t for t in s["tickers"] if t["sym"] == "SPY")["spot"] == "678.90"
    assert s["context"]["tickers"][0]["price"] == "678.90"
    print("  zerodte: SPY levels/regime/observed/touches mapped, ladder ordered, merge OK")


def test_fetch_fails_soft(monkeypatch=None):
    from desk import config
    # Enable just the agent source, then make its fetch blow up.
    config.SOURCES["agent"]["url"] = "http://unreachable"
    config.SOURCES["zerodte"]["url"] = ""
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
    config.SOURCES["agent"]["url"] = ""
    config.SOURCES["zerodte"]["url"] = ""
    s = fetch.get_snapshot(force=True)
    assert s["mock"] is True and s["live"] is False and s["degraded"] == {}
    print("  fetch: no URLs -> sample mode")


if __name__ == "__main__":
    for fn in (test_agent_maps_brief_and_risk, test_zerodte_maps_levels_and_touches,
               test_fetch_fails_soft, test_sample_mode_when_unconfigured):
        fn()
    print("ALL PASS")
