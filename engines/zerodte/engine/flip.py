"""Gamma flip via proper re-pricing.

Sweep hypothetical spot, recompute every 0DTE option's gamma with
Black-Scholes at that spot (current IV, actual time to expiry), sum signed
dealer exposure, and find ALL zero crossings. If none in +/-5%, widen to
+/-10% before reporting none — and an unqualified 'none' is treated as an
error condition upstream, not a fact.
"""
from .. import config
from . import bs


def _exposure_curve(positions: dict[str, float], meta: dict[str, dict],
                    spots: list[float], t_years: float) -> list[float]:
    # Pre-extract usable contracts once.
    legs = []
    for key, contracts in positions.items():
        if contracts == 0:
            continue
        m = meta.get(key)
        if not m or m.get("iv") in (None, 0):
            continue
        legs.append((contracts, m["strike"], m["iv"]))
    curve = []
    for s in spots:
        total = 0.0
        for contracts, strike, iv in legs:
            g = bs.bs_gamma(s, strike, iv, t_years)
            total += bs.dollar_gamma(contracts, g, s)
        curve.append(total)
    return curve


def _crossings(spots: list[float], curve: list[float]) -> list[float]:
    out = []
    for i in range(1, len(curve)):
        a, b = curve[i - 1], curve[i]
        if a == 0.0:
            out.append(spots[i - 1])
        elif (a < 0 < b) or (a > 0 > b):
            frac = abs(a) / (abs(a) + abs(b))
            out.append(spots[i - 1] + frac * (spots[i] - spots[i - 1]))
    return out


def find_flips(positions: dict[str, float], meta: dict[str, dict],
               spot: float, t_years: float) -> dict:
    """Returns {flips: [...], nearest: float|None, local_gamma: float,
                widened: bool, error: str|None}."""
    result = {"flips": [], "nearest": None, "local_gamma": 0.0,
              "widened": False, "error": None}
    if spot <= 0 or not positions:
        result["error"] = "no positions or invalid spot"
        return result

    for pct, widened in ((config.FLIP_SWEEP_PCT, False),
                         (config.FLIP_SWEEP_WIDE_PCT, True)):
        n = int(round(2 * pct / config.FLIP_SWEEP_STEP))
        spots = [spot * (1 - pct + i * config.FLIP_SWEEP_STEP) for i in range(n + 1)]
        curve = _exposure_curve(positions, meta, spots, t_years)
        if not any(curve):
            result["error"] = "exposure curve is identically zero"
            return result
        # local gamma at the sweep point nearest current spot
        mid_idx = min(range(len(spots)), key=lambda i: abs(spots[i] - spot))
        result["local_gamma"] = curve[mid_idx]
        flips = _crossings(spots, curve)
        if flips:
            result["flips"] = flips
            result["nearest"] = min(flips, key=lambda f: abs(f - spot))
            result["widened"] = widened
            return result

    # No crossing even at +/-10% — report none but flag it as suspect.
    result["error"] = "no zero crossing within +/-10% sweep (treat as range bug)"
    return result
