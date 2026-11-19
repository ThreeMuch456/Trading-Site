"""Desk Flow page parity with the scanner's own Index ETFs page.

The four bugs this pins: alert times printed in UTC as if ET, the 15-minute
rollup line summing every entry instead of the fresh ones, an EOD "lean" shown
for a ticker whose flow is not a wave, and the session date taken from the EOD
window (yesterday, in the morning).
"""
from datetime import datetime, timezone

from desk.sources import flow


def _alert(**kw):
    a = {"ticker": "SPY", "option_type": "call", "volume": 1000, "volume_delta": 0,
         "strike": 740, "expiry": "2026-11-20", "estimated_premium": 900000, "score": 60,
         "vol_oi_ratio": 1.0, "open_interest": 1000, "implied_volatility": 0.3, "delta": 0.4,
         "underlying_price": 739.0, "flags": [], "is_new": False, "hits": 1,
         "first_seen": "2026-11-19T14:51:30", "detected_at": "2026-11-19T14:51:30",
         "bucket": "1dte"}
    a.update(kw)
    return a


def test_naive_api_times_are_utc_and_shown_on_the_et_clock():
    # The flow store keeps naive UTC: 14:51 UTC is 09:51 ET in November.
    assert flow._hhmm("2026-11-19T14:51:30.123456") == "09:51"
    assert flow._hhmm("2026-11-19T09:51:30-05:00") == "09:51"   # an aware value is left alone
    assert flow._hhmm("") == "" and flow._hhmm(None) == ""
    e = flow._entry(_alert(hits=3, first_seen="2026-11-19T14:46:00"),
                    now=datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc))
    assert e["time"] == "09:51" and e["reflagged"] == "3× since 09:46 ET"
    assert e["px_age"] == "8m"


def test_entry_carries_flag_classes_tags_and_price_at_flag():
    e = flow._entry(_alert(flags=["WHALE", "+16,445 BURST", "NEW"],
                           flag_classes=["whale", "burst", "new"],
                           is_new=True, surge="call", eod=None, ask=2.776, bucket="1dte"))
    assert [f["cls"] for f in e["flags"]] == ["whale", "burst", "new"]   # NEW once, not twice
    assert e["surge"] == "call" and e["eod"] is None
    assert e["px"] == "$2.78" and e["bucket"] == "1dte"
    # Older payloads with no flag_classes still get coloured from the label.
    legacy = flow._entry(_alert(flags=["BIG $", "VOL≫OI", "3× VOL/OI"], is_new=True))
    assert [f["cls"] for f in legacy["flags"]] == ["big", "newoi", "unusual", "new"]
    assert flow._entry(_alert(ask=None))["px"] is None


def test_fresh_rollup_counts_only_entries_flagged_in_the_window():
    alerts = [_alert(volume=4723, volume_delta=4207, is_new=False),       # flagged hours ago
              _alert(option_type="put", volume=500, volume_delta=300, is_new=True),
              _alert(ticker="QQQ", volume=2000, volume_delta=900, is_new=True)]
    roll = {r["sym"]: r for r in flow._rollup(alerts, ["SPY", "QQQ", "IWM"])}
    assert roll["SPY"]["fresh"] == "+0C / +300P"                         # not +4,207C
    assert roll["QQQ"]["fresh"] == "+900C / +0P"
    assert "IWM" not in roll                                              # no entries: no card
    assert roll["SPY"]["call_pct_txt"] == f"{4723 / 5223 * 100:.1f}%"


def test_eod_chip_claims_a_lean_only_for_a_wave():
    spy = flow._eod_chip({"ticker": "SPY", "call_volume": 16888, "put_volume": 17896,
                          "call_share": 0.486, "direction": "put", "is_wave": False,
                          "contracts": 111, "premium": 10.8e6,
                          "top_contract": {"strike": 714, "option_type": "put", "expiry": "2026-11-20"}})
    assert spy["lean"] is None and spy["is_wave"] is False
    assert spy["detail"] == "48.6% calls · 111 prints · $10.8M"
    assert spy["led"] == "led by $714P exp 2026-11-20"
    qqq = flow._eod_chip({"ticker": "QQQ", "call_share": 0.23, "direction": "put", "is_wave": True})
    assert qqq["lean"] == "put"


def test_session_label_is_today_not_the_eod_window():
    assert flow._session_label("2026-11-19") == "0DTE / 1DTE · session of Thu Nov 19"
    meta = flow._eod_meta({"session_date": "2026-11-18",
                           "window_start": "2026-11-18T15:00:00-05:00"}, "2026-11-19")
    assert meta == {"session": "2026-11-18", "start": "3:00 PM", "prior": True,
                    "when": "prior session · read on today"}
    same = flow._eod_meta({"session_date": "2026-11-19", "window_start": "2026-11-19T20:00:00"},
                          "2026-11-19")                                    # naive UTC: 15:00 ET
    assert same["prior"] is False and same["start"] == "3:00 PM" and same["when"] == "next session"


def test_flow_page_renders_filters_and_no_hardcoded_lean_text():
    import app as appmod
    html = appmod.app.test_client().get("/flow").get_data(as_text=True)
    assert 'data-dte="0dte"' in html and 'id="fresh-toggle"' in html
    assert "SPY and QQQ close call-leaning" not in html                   # the old static sentence
    assert "data-rollup=\"all\"" in html and "Notable Index-ETF Flow" in html
