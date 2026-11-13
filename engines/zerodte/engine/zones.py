"""Derived zones — mechanical only, from levels and regime.

No composite confidence score: each zone carries its component inputs
(distance, source magnitude, observed_share, touch history, relative volume)
as separate fields. Superseded zones are kept, flagged expired.
"""
from datetime import datetime

from . import touches as touches_mod


def _zone(kind, regime, low, high, source_level, spot, atr, rvol,
          targets, invalidation, touch_summary, direction):
    mid = (low + high) / 2.0
    dist = mid - spot
    return {
        "id": f"{regime}:{kind}:{source_level['strike']:g}",
        "kind": kind, "regime": regime,
        # Which way the zone's mechanics point, and the option side that
        # expresses that direction. Both are a fixed consequence of
        # (regime, side-of-spot) — not a view, and not a sizing or timing
        # judgement. Derived here rather than in the renderer so replay and
        # live agree and the reasoning stays in one place.
        "direction": direction,                              # 'up' | 'down'
        "option_side": "calls" if direction == "up" else "puts",
        "low": round(low, 4), "high": round(high, 4),
        "source_strike": source_level["strike"],
        "source_magnitude": source_level["dealer_gamma"],
        "observed_share": source_level["observed_share"],
        "dist_dollars": round(dist, 4),
        "dist_atr": round(abs(dist) / atr, 2) if atr else None,
        "rel_volume": rvol,
        "targets": targets,
        "invalidation": invalidation,
        "touch_history": touch_summary,
        "expired": False, "superseded_at": None,
    }


def derive_zones(regime_state: str, walls: dict, spot: float, atr: float | None,
                 max_pain: float | None, vwap: float | None, em_band: list | None,
                 rvol: float | None, touch_summaries: dict[float, list],
                 breached_strikes: set[float]) -> list[dict]:
    zones = []
    if spot <= 0:
        return zones

    def summary(strike):
        return touch_summaries.get(strike, [])

    if regime_state == "PIN":
        for side, lst in (("above", walls["above"]), ("below", walls["below"])):
            if not lst:
                continue
            w = lst[0]
            band = touches_mod.band_width(w["strike"], atr)
            # reaction zone offset inward (toward spot) by the band width
            if side == "above":
                low, high = w["strike"] - band, w["strike"]
                inval = f"> {w['strike'] + band:.2f} (wall + band) or REGIME_CHANGE to TREND"
            else:
                low, high = w["strike"], w["strike"] + band
                inval = f"< {w['strike'] - band:.2f} (wall - band) or REGIME_CHANGE to TREND"
            opposing = walls["below"][0]["strike"] if side == "above" and walls["below"] \
                else (walls["above"][0]["strike"] if side == "below" and walls["above"] else None)
            targets = []
            for name, px in (("max_pain", max_pain), ("opposing_wall", opposing),
                             ("vwap", vwap)):
                if px is not None:
                    targets.append({"name": name, "price": round(px, 4),
                                    "dist": round(abs(px - w["strike"]), 4)})
            targets.sort(key=lambda t: t["dist"])
            # PIN reaction: price runs into the wall and hedging pushes back,
            # so the zone points AWAY from the wall — down off the wall
            # overhead, up off the wall underneath.
            zones.append(_zone(f"reaction_{side}", "PIN", low, high, w, spot, atr,
                               rvol, targets, inval, summary(w["strike"]),
                               direction="down" if side == "above" else "up"))

    elif regime_state == "TREND":
        for side, lst in (("above", walls["above"]), ("below", walls["below"])):
            if not lst:
                continue
            w = lst[0]
            band = touches_mod.band_width(w["strike"], atr)
            confirmed = w["strike"] in breached_strikes
            if side == "above":
                low, high = w["strike"], w["strike"] + band
                nxt = walls["above"][1]["strike"] if len(walls["above"]) > 1 else None
                em_edge = em_band[1] if em_band else None
                inval = f"< {w['strike'] - band:.2f} (back inside wall) or REGIME_CHANGE to PIN"
            else:
                low, high = w["strike"] - band, w["strike"]
                nxt = walls["below"][1]["strike"] if len(walls["below"]) > 1 else None
                em_edge = em_band[0] if em_band else None
                inval = f"> {w['strike'] + band:.2f} (back inside wall) or REGIME_CHANGE to PIN"
            targets = []
            if nxt is not None:
                targets.append({"name": "next_wall", "price": round(nxt, 4),
                                "dist": round(abs(nxt - w["strike"]), 4)})
            if em_edge is not None:
                targets.append({"name": "em_band_edge", "price": round(em_edge, 4),
                                "dist": round(abs(em_edge - w["strike"]), 4)})
            # TREND trigger: the wall broke and hedging amplifies the move,
            # so the zone points THROUGH the wall — continuation, not reversal.
            z = _zone(f"trigger_{side}", "TREND", low, high, w, spot, atr,
                      rvol, targets, inval, summary(w["strike"]),
                      direction="up" if side == "above" else "down")
            z["breach_confirmed"] = confirmed
            zones.append(z)

    # TRANSITION: no fresh zones; existing ones stay until a regime resolves.
    return zones


def expire_zones(zones: list[dict], now_iso: str) -> None:
    for z in zones:
        if not z["expired"]:
            z["expired"] = True
            z["superseded_at"] = now_iso
