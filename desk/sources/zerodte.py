"""0DTE-Assistant adapter — the live mechanical level engine.

Maps `GET /api/state` (per-ticker `engine.get_state()`) into: the Gameplan level
ladders + regime + observed%, the Overview context tiles, and the Intraday
touches + position-vs-levels. Spot is near-live; option-derived levels carry the
T-15 delay badge. Fails soft per ticker — one engine_error never blanks the rest.
"""

from datetime import datetime

from . import http_get_json

_REGIME = {"PIN": ("PIN", "pin"), "TREND": ("TREND", "trend"), "TRANSITION": ("TRANSITION", "transition")}


def collect(cfg: dict, timeout: float) -> dict:
    from .. import config
    p = http_get_json(cfg["url"] + "/api/state", cfg.get("token", ""), cfg.get("header", "X-App-Token"), timeout)
    tickers = p.get("tickers", {}) or {}

    levels, touches, position, tiles = {}, [], [], {}
    for sym in config.TICKERS:
        st = tickers.get(sym) or tickers.get(sym.upper())
        if not st or st.get("engine_error") or st.get("no_0dte"):
            levels[sym] = {"degraded": True}
            tiles[sym] = {"sym": sym, "price": "—", "chg": "—", "chg_class": "flat",
                          "note": "no data", "note_class": "flat"}
            continue

        spot = st.get("spot")
        pc = st.get("prior_close")
        chg = ((spot - pc) / pc * 100) if (spot and pc) else None
        rstate = st.get("regime") or {}
        rstate = (rstate.get("state") if isinstance(rstate, dict) else rstate) or ""
        regime, regime_class = _REGIME.get(str(rstate).upper(), (str(rstate).upper() or "—", "transition"))

        five = st.get("five", {}) or {}
        cw = _first(five.get("walls_above"))
        pw = _first(five.get("walls_below"))
        flip = _strike(five.get("flip"))
        band = (five.get("expected_move") or {}).get("band_085") or (five.get("expected_move") or {}).get("band_raw")
        em_hi = band[1] if band and len(band) == 2 else None
        em_lo = band[0] if band and len(band) == 2 else None

        massive = (st.get("sources", {}) or {}).get("massive", {}) or {}
        degraded = bool(massive.get("degraded")) or bool(st.get("map_frozen"))
        observed = _observed(cw, pw)

        levels[sym] = {
            "sym": sym, "regime": regime, "regime_class": regime_class,
            "spot": _px(spot), "chg": _pct(chg), "chg_class": _cls(chg),
            "levels": _ladder(spot, cw, pw, flip, em_hi, em_lo),
            "observed": observed, "observed_class": "var(--green)" if observed >= 60 else "var(--amber)",
            "flow_age": _flow_age(massive), "degraded": degraded,
        }
        tiles[sym] = {
            "sym": sym, "price": _px(spot), "chg": _pct(chg), "chg_class": _cls(chg),
            "note": _note(spot, flip, regime), "note_class": _note_class(spot, flip),
        }
        for t in (st.get("touches") or [])[:6]:
            touches.append(_touch(sym, t))
        position.append(_position(sym, spot, regime, regime_class, cw, pw, flip, em_hi))

    return {"levels": levels, "tiles": tiles, "touches": touches, "position": position}


def merge(s: dict, c: dict) -> None:
    by_sym = {t["sym"]: t for t in s.get("tickers", [])}
    for sym, lv in c["levels"].items():
        t = by_sym.get(sym)
        if not t or lv.get("degraded") and "levels" not in lv:
            if t:
                t["flow_age"] = "degraded"
            continue
        for k in ("regime", "regime_class", "spot", "chg", "chg_class",
                  "levels", "observed", "observed_class", "flow_age"):
            if k in lv:
                t[k] = lv[k]
    # Context tiles (SPY/QQQ/IWM) + VIX stays from sample (0DTE doesn't carry it).
    tiles = [c["tiles"][sym] for sym in (t["sym"] for t in s.get("tickers", [])) if sym in c["tiles"]]
    if tiles:
        s["context"]["tickers"] = tiles
    if c["touches"]:
        s["intraday"]["touches"] = c["touches"]
    if c["position"]:
        s["intraday"]["position"] = c["position"]


# ---- helpers ----

def _first(xs):
    return xs[0] if xs else None


def _strike(flip):
    if isinstance(flip, dict):
        return flip.get("strike") or flip.get("price")
    return flip


def _px(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) else "—"


def _pct(x):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "—"


def _cls(x):
    if not isinstance(x, (int, float)):
        return "flat"
    return "up" if x > 0.01 else ("down" if x < -0.01 else "flat")


def _dist(price, spot):
    if not (isinstance(price, (int, float)) and isinstance(spot, (int, float)) and spot):
        return "—"
    return f"{(price - spot) / spot * 100:+.2f}%"


def _observed(cw, pw):
    vals = [w.get("observed_share") for w in (cw, pw) if w and isinstance(w.get("observed_share"), (int, float))]
    return int(round(sum(vals) / len(vals) * 100)) if vals else 0


def _ladder(spot, cw, pw, flip, em_hi, em_lo):
    rows = []

    def add(name, kind, price, val_class):
        if isinstance(price, (int, float)):
            rows.append({"name": name, "kind": kind, "price": _px(price),
                         "dist": _dist(price, spot), "val_class": val_class, "_p": price})

    add("Call Wall", "callwall", cw.get("strike") if cw else None, "up")
    add("EM High", "em", em_hi, "")
    add("Zero Gamma", "zero", flip, "amber")
    add("Put Wall", "putwall", pw.get("strike") if pw else None, "down")
    add("EM Low", "em", em_lo, "")
    if isinstance(spot, (int, float)):
        rows.append({"name": "Spot", "kind": "spot", "price": _px(spot), "dist": "—",
                     "val_class": "", "is_spot": True, "_p": spot})
    rows.sort(key=lambda r: -r["_p"])
    for r in rows:
        r.pop("_p", None)
    return rows


def _flow_age(massive):
    asof = massive.get("asof")
    hm = _hhmm(asof)
    return f"flow T-15 · {hm}" if hm else "flow T-15"


def _note(spot, flip, regime):
    if isinstance(spot, (int, float)) and isinstance(flip, (int, float)):
        return "above zero-γ" if spot >= flip else "below zero-γ"
    return regime.lower() if regime and regime != "—" else "—"


def _note_class(spot, flip):
    if isinstance(spot, (int, float)) and isinstance(flip, (int, float)):
        return "up" if spot >= flip else "down"
    return "flat"


def _hhmm(iso):
    if not iso or not isinstance(iso, str):
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return ""


_DEFENDED = ("DEFEND",)
_BREACHED = ("BROKEN", "ATTACK", "BREACH")


def _touch(sym, t):
    label = str(t.get("label") or t.get("price_outcome") or "—")
    up = label.upper()
    if any(k in up for k in _DEFENDED):
        tag, cls = "Defended", "defended"
    elif any(k in up for k in _BREACHED) or str(t.get("price_outcome", "")).upper() == "BREACHED":
        tag, cls = "Breached", "breached"
    else:
        tag, cls = "Contested", "contested"
    strike = t.get("strike")
    kind = t.get("kind") or "level"
    return {
        "sym": sym,
        "time": _hhmm(t.get("exit_ts") or t.get("entry_ts")) or "—",
        "tag": tag, "tag_class": cls,
        "html": f"<b>{sym} {strike:g}</b> {kind}" if isinstance(strike, (int, float)) else f"<b>{sym}</b> {kind}",
    }


def _position(sym, spot, regime, regime_class, cw, pw, flip, em_hi):
    above = cw.get("strike") if cw else em_hi
    return {
        "sym": sym, "spot": _px(spot), "regime": regime, "regime_class": regime_class,
        "above": f"{above:g} CW" if isinstance(above, (int, float)) else "—",
        "above_dist": _dist(above, spot),
        "below": f"{pw.get('strike'):g} PW" if pw else "—",
        "below_dist": _dist(pw.get("strike") if pw else None, spot),
        "flip": _dist(flip, spot),
    }
