"""The five numbers: walls above/below (top three each), gamma flip,
0DTE max pain, expected move band."""

from .. import config


def walls(strike_map: dict[float, dict], spot: float) -> dict:
    """Wall above = largest POSITIVE dealer gamma above spot.
    Wall below = largest NEGATIVE dealer gamma below spot. Top three each."""
    above = [s for s in strike_map.values()
             if s["strike"] > spot and s["dealer_gamma"] > 0]
    below = [s for s in strike_map.values()
             if s["strike"] < spot and s["dealer_gamma"] < 0]
    above.sort(key=lambda s: -s["dealer_gamma"])
    below.sort(key=lambda s: s["dealer_gamma"])  # most negative first

    def fmt(s):
        return {"strike": s["strike"], "dealer_gamma": s["dealer_gamma"],
                "observed_share": s["observed_share"],
                "low_conf_share": s["low_conf_share"]}
    return {"above": [fmt(s) for s in above[:3]],
            "below": [fmt(s) for s in below[:3]]}


def max_pain(chain: list[dict]) -> float | None:
    """Strike minimizing total intrinsic payout to holders across the 0DTE chain."""
    strikes = sorted({c["strike"] for c in chain})
    if not strikes:
        return None
    calls = [(c["strike"], c["oi"] or 0) for c in chain if c["type"] == "C"]
    puts = [(c["strike"], c["oi"] or 0) for c in chain if c["type"] == "P"]
    best, best_pay = None, None
    for k in strikes:
        pay = sum(oi * max(0.0, k - st) for st, oi in calls) \
            + sum(oi * max(0.0, st - k) for st, oi in puts)
        if best_pay is None or pay < best_pay:
            best, best_pay = k, pay
    return best


def straddle_snapshot(chain: list[dict], spot: float) -> dict | None:
    """ATM straddle — captured once at the open and frozen.

    Preferred price is the quote midpoint; on plans without the quote
    entitlement (bid/ask absent on every contract) fall back to the mark
    (last trade / day close) and say so via price_source, because a
    trade-based straddle can be stale by a few minutes at the open."""
    strikes = sorted({c["strike"] for c in chain})
    if not strikes or spot <= 0:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    call = next((c for c in chain if c["strike"] == atm and c["type"] == "C"), None)
    put = next((c for c in chain if c["strike"] == atm and c["type"] == "P"), None)
    if not call or not put:
        return None

    def price(c):
        if c.get("bid") is not None and c.get("ask") is not None and c["ask"] > 0:
            return (c["bid"] + c["ask"]) / 2.0, "quote_mid"
        if c.get("mark") is not None and c["mark"] > 0:
            return c["mark"], "mark"
        return None, None

    cm, cs = price(call)
    pm, ps = price(put)
    if cm is None or pm is None:
        return None
    ivs = [c.get("iv") for c in (call, put) if c.get("iv")]
    return {"atm_strike": atm, "straddle_mid": cm + pm,
            "price_source": "quote_mid" if cs == ps == "quote_mid" else "mark",
            "atm_iv": (sum(ivs) / len(ivs)) if ivs else None}


def expected_move(straddle: dict | None, anchor_spot: float | None,
                  t_years_at_snapshot: float | None) -> dict | None:
    """Primary: straddle mid raw and x0.85. Secondary: IV-derived band.
    Both are shown; their divergence is informative."""
    if not straddle or not anchor_spot:
        return None
    em = straddle["straddle_mid"]
    out = {
        "anchor_spot": anchor_spot,
        "price_source": straddle.get("price_source", "quote_mid"),
        "straddle_raw": em,
        "straddle_085": em * config.EM_STRADDLE_MULT,
        "band_raw": [anchor_spot - em, anchor_spot + em],
        "band_085": [anchor_spot - em * config.EM_STRADDLE_MULT,
                     anchor_spot + em * config.EM_STRADDLE_MULT],
        "iv_band": None,
    }
    iv = straddle.get("atm_iv")
    if iv and t_years_at_snapshot and t_years_at_snapshot > 0:
        iv_em = anchor_spot * iv * (t_years_at_snapshot ** 0.5)
        out["iv_band"] = [anchor_spot - iv_em, anchor_spot + iv_em]
        out["iv_em"] = iv_em
    return out
