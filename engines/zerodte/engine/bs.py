"""Black-Scholes pieces needed for the flip sweep."""
import math

from .. import config


def bs_gamma(spot: float, strike: float, iv: float, t_years: float,
             r: float = config.RISK_FREE_RATE) -> float:
    """Gamma per share. Guards degenerate inputs near expiry."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-7 or iv is None or iv <= 1e-4:
        return 0.0
    st = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / st
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return phi / (spot * st)


def dollar_gamma(contracts: float, gamma: float, spot: float) -> float:
    """Dollar gamma per 1% move for a signed contract position."""
    return contracts * gamma * 100.0 * spot * spot * 0.01
