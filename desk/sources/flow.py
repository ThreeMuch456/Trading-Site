"""Options-Flow-Scanner adapter — short-dated index flow.

Maps three existing endpoints into the Flow page + the Intraday flow skew:
  /api/index-etfs/waves  -> fresh one-sided waves (banners) + intraday skew
  /api/index-etfs/eod    -> late-day positioning chips
  /api/index-etfs        -> session skew by ticker (aggregated notable flow)

Defensive: option_type may be 'call'/'put' or 'C'/'P'; a ticker with no fresh
flow simply doesn't appear in waves. All numeric fields are formatted here so the
templates stay dumb.
"""

from datetime import datetime
from html import escape

from . import http_get_json


def collect(cfg: dict, timeout: float) -> dict:
    base, tok, hdr = cfg["url"], cfg.get("token", ""), cfg.get("header", "Authorization")
    waves = http_get_json(base + "/api/index-etfs/waves", tok, hdr, timeout)
    eod = http_get_json(base + "/api/index-etfs/eod", tok, hdr, timeout)
    alerts = http_get_json(base + "/api/index-etfs", tok, hdr, timeout)

    from .. import config
    order = config.TICKERS

    wave_banners = [_wave(w) for w in waves if w.get("is_surge")]
    intraday_skew = _skew_from_waves(waves, order)
    session_skew = _session_skew(alerts, order)
    eod_chips = [_eod_chip(e) for e in (eod.get("tickers") or [])]
    label = _session_label(eod)

    surge_count = len(wave_banners)
    fresh_premium = sum(w.get("premium") or 0 for w in waves if w.get("is_surge"))
    call_leans = sum(1 for e in (eod.get("tickers") or []) if e.get("direction") == "call")
    eod_total = len(eod.get("tickers") or [])

    return {
        "flow": {"session_label": label, "waves": wave_banners,
                 "session_skew": session_skew, "eod": eod_chips},
        "intraday_skew": intraday_skew,
        "card": {"surge_count": surge_count, "fresh_premium": _usd(fresh_premium),
                 "eod_lean": f"Calls {call_leans}/{eod_total}" if eod_total else "—"},
    }


def merge(s: dict, c: dict) -> None:
    s["flow"].update(c["flow"])
    if c["intraday_skew"]:
        s["intraday"]["skew"] = c["intraday_skew"]
    # Keep the Overview flow snapshot card honest.
    for card in s.get("overview_cards", []):
        if card.get("href") != "flow":
            continue
        for st in card.get("stats", []):
            if st.get("l") == "Active waves":
                st["v"], st["badge_after"] = str(c["card"]["surge_count"]), ("call", "call")
            elif st.get("l") == "Fresh premium":
                st["v"] = c["card"]["fresh_premium"]
            elif st.get("l") == "EOD lean":
                st["v"] = c["card"]["eod_lean"]


# ---- helpers ----

def _is_call(ot) -> bool:
    return str(ot).lower() in ("call", "c")


def _wave(w: dict) -> dict:
    call_share = w.get("call_share")
    call_pct = int(round(call_share * 100)) if isinstance(call_share, (int, float)) else 50
    side = w.get("direction", "call")
    dom = call_pct if side == "call" else 100 - call_pct
    top = w.get("top_contract")
    lead = ""
    if top:
        cp = "C" if _is_call(top.get("option_type")) else "P"
        lead = f"led by ${_g(top.get('strike'))}{cp} exp {escape(str(top.get('expiry', '')))} (+{_n(top.get('volume_delta'))})"
    mins = w.get("window_minutes", 15)
    return {
        "sym": w.get("ticker", "—"), "side": side,
        "call_n": _n(w.get("call_fresh")), "put_n": _n(w.get("put_fresh")),
        "skew_pct": dom, "call_pct": call_pct, "lead": lead,
        "meta": f"{_usd(w.get('premium'))} fresh · {w.get('contracts', 0)} contract"
                f"{'s' if (w.get('contracts') or 0) != 1 else ''} · last {mins}m",
    }


def _skew_from_waves(waves: list, order: list) -> list:
    by = {w.get("ticker"): w for w in waves}
    out = []
    for sym in order:
        w = by.get(sym)
        if not w or not isinstance(w.get("call_share"), (int, float)):
            out.append({"sym": sym, "call_pct": 50, "label": "—"})
            continue
        cp = int(round(w["call_share"] * 100))
        out.append({"sym": sym, "call_pct": cp,
                    "label": f"{cp}% calls" if cp >= 50 else f"{100 - cp}% puts"})
    return out


def _session_skew(alerts: list, order: list) -> list:
    agg = {}
    for a in alerts:
        t = a.get("ticker")
        if t is None:
            continue
        c, p = agg.setdefault(t, [0, 0])
        v = a.get("volume") or 0
        if _is_call(a.get("option_type")):
            agg[t][0] = c + v
        else:
            agg[t][1] = p + v
    out = []
    for sym in order:
        c, p = agg.get(sym, [0, 0])
        tot = c + p
        if tot <= 0:
            out.append({"sym": sym, "call_pct": 50, "label": "—"})
            continue
        cp = int(round(c / tot * 100))
        out.append({"sym": sym, "call_pct": cp,
                    "label": f"{cp}% calls" if cp >= 50 else f"{100 - cp}% puts"})
    return out


def _eod_chip(e: dict) -> dict:
    call_share = e.get("call_share")
    call_pct = int(round(call_share * 100)) if isinstance(call_share, (int, float)) else 50
    side = e.get("direction", "call")
    dom = call_pct if side == "call" else 100 - call_pct
    expiries = e.get("expiries") or []
    exp = escape(str(expiries[0])) if expiries else "—"
    detail = f"{dom}% {side}s · {_usd(e.get('premium'))} · exp {exp}"
    return {"sym": e.get("ticker", "—"), "lean": side, "call_pct": call_pct, "detail": detail}


def _session_label(eod: dict) -> str:
    d = eod.get("session_date")
    return f"0DTE / 1DTE · session of {escape(str(d))}" if d else "0DTE / 1DTE"


def _usd(x) -> str:
    if not isinstance(x, (int, float)) or x == 0:
        return "$0"
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.0f}"


def _n(x) -> str:
    return f"{int(x):,}" if isinstance(x, (int, float)) else "0"


def _g(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else "—"
