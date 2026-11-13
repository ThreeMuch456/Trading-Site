"""
Shared flow-signal logic used by both the scanner (deciding what to auto-watch)
and the web app (ranking the Sweeps page). Centralising it here guarantees the
two agree on what counts as notable, deep-ITM, expired, or a high-conviction
sweep — and keeps the thresholds defined in exactly one place.

Every function takes a SweepAlert-like object (anything with the attributes
estimated_premium, vol_oi_ratio, delta, strike, underlying_price, option_type,
expiry).
"""

import os
import math
from datetime import date, datetime, time, timedelta, timezone
import pytz
from dotenv import load_dotenv

load_dotenv()

ET = pytz.timezone("US/Eastern")

# ── Thresholds (single source of truth for scanner + web) ──────────────────────
HIGHLIGHT_MIN_PREMIUM = float(os.getenv("HIGHLIGHT_MIN_PREMIUM", 250_000))
HIGHLIGHT_MIN_VOL_OI  = float(os.getenv("HIGHLIGHT_MIN_VOL_OI", 3.0))
WHALE_PREMIUM         = float(os.getenv("WHALE_PREMIUM", 1_000_000))
DEEP_ITM_DELTA        = float(os.getenv("DEEP_ITM_DELTA", 0.95))
AUTO_WATCH_MIN_SCORE  = int(os.getenv("AUTO_WATCH_MIN_SCORE", 95))

# Far-OTM cut for the Index ETFs page. Delta is the primary test; the % OTM
# fallbacks (for rows without greeks) differ by DTE because the same delta sits
# much closer to the money on a 0DTE than a 1DTE — measured on 55 sessions of
# SPY/QQQ/IWM flow, |delta| 0.05 was a median 0.68% OTM on 0DTE, 1.64% on 1DTE.
ETF_MIN_DELTA         = float(os.getenv("ETF_MIN_DELTA", 0.05))
ETF_FAR_OTM_PCT_0DTE  = float(os.getenv("ETF_FAR_OTM_PCT_0DTE", 0.7))
ETF_FAR_OTM_PCT_1DTE  = float(os.getenv("ETF_FAR_OTM_PCT_1DTE", 1.6))


# Cached ET date, with the UTC instant it stops being true.
_today_et = (None, None)


def today_et():
    """
    Today's date on the ET clock, cached until the next ET midnight.

    These date helpers are called once per alert row, which is tens of thousands
    of times on the rotation page — pytz's timezone conversion accounted for
    ~1.2s of a 5s profile at 88k rows. The answer only changes once a day, so it
    is computed once and held until it genuinely goes stale. The expiry is a
    real timestamp rather than a TTL, so the value is never wrong.
    """
    global _today_et
    today, valid_until = _today_et
    now = datetime.now(timezone.utc)
    if today is None or now >= valid_until:
        today = now.astimezone(ET).date()
        valid_until = ET.localize(
            datetime.combine(today + timedelta(days=1), time(0, 0))
        ).astimezone(timezone.utc)
        _today_et = (today, valid_until)
    return today


def days_to_expiry(expiry: str):
    """Calendar days until expiry on the ET clock (negative once expired)."""
    try:
        return (date.fromisoformat(expiry) - today_et()).days
    except (TypeError, ValueError):
        return None


def trading_days_to_expiry(expiry: str):
    """
    Business days (Mon–Fri) until expiry on the ET clock: 0 = expires today,
    1 = next trading day, etc. Unlike calendar days this makes "1DTE" mean the
    next *session* — e.g. on a Friday, Monday's expiry is 1DTE, not 3.
    Note: weekends only; market holidays aren't accounted for.
    """
    try:
        exp = date.fromisoformat(expiry)
    except (TypeError, ValueError):
        return None
    today = today_et()
    if exp == today:
        return 0
    step = 1 if exp > today else -1
    count, d = 0, today
    while d != exp:
        d += timedelta(days=step)
        if d.weekday() < 5:          # Mon–Fri
            count += step
    return count


def is_expired(a) -> bool:
    """True if the option's expiry date has already passed (ET calendar)."""
    dte = days_to_expiry(a.expiry)
    return dte is not None and dte < 0


def is_deep_itm(a) -> bool:
    """
    Deep-in-the-money prints are usually spread legs, buy-writes, or exercises
    dressed up as size — near-pure intrinsic value, not directional bets.
    Use delta when available; fall back to moneyness when greeks are absent
    (Massive often omits greeks on deep-ITM contracts).
    """
    if a.delta is not None:
        return abs(a.delta) >= DEEP_ITM_DELTA
    u, k = a.underlying_price or 0, a.strike or 0
    if u <= 0 or k <= 0:
        return False
    if a.option_type == "call":
        return k <= u * 0.85
    return k >= u * 1.15


def is_far_otm(a) -> bool:
    """
    Far-out-of-the-money lottery tickets — the other end from is_deep_itm.

    On short-dated index ETFs these are cheap enough that they carry almost no
    premium (~0.5% of it below |delta| 0.05) but a lot of contracts (~15% of
    volume), so left in they swing the volume-based call/put reads on the Index
    ETFs page. Delta when available; otherwise % OTM against a DTE-dependent
    cut (0DTE for anything expiring today or earlier, the 1DTE cut beyond).
    """
    if a.delta is not None:
        return abs(a.delta) < ETF_MIN_DELTA
    u, k = a.underlying_price or 0, a.strike or 0
    if u <= 0 or k <= 0:
        return False
    otm_pct = (k - u) / u * 100 if a.option_type == "call" else (u - k) / u * 100
    tdte = trading_days_to_expiry(a.expiry)
    cut = ETF_FAR_OTM_PCT_0DTE if tdte is not None and tdte <= 0 else ETF_FAR_OTM_PCT_1DTE
    return otm_pct > cut


def is_notable(a) -> bool:
    """Qualifies for the Sweeps page: big premium OR unusual vol/OI."""
    return (a.estimated_premium or 0) >= HIGHLIGHT_MIN_PREMIUM or \
           (a.vol_oi_ratio or 0) >= HIGHLIGHT_MIN_VOL_OI


def score_alert(a) -> int:
    """
    Composite 0–100 significance score:
      - notional premium (log-scaled)            → up to 50 pts
      - vol/OI ratio (linear, capped at 15×)     → up to 30 pts
      - "aggressiveness"                          → up to 20 pts
          · OTM sweet spot (|delta| 0.15–0.45)    → up to 12 — conviction bets,
            not hedges or spread legs
          · short-dated (< 30 DTE)                → up to 8 — urgency
    Higher = larger, more unusual, more aggressive positioning.
    """
    premium = a.estimated_premium or 0
    ratio   = a.vol_oi_ratio or 0

    # Premium: $25k → 0 pts, $5M → 50 pts (log scale)
    lo, hi = math.log10(25_000), math.log10(5_000_000)
    if premium > 0:
        frac = (math.log10(premium) - lo) / (hi - lo)
        premium_pts = max(0.0, min(50.0, 50.0 * frac))
    else:
        premium_pts = 0.0

    # Vol/OI: 0 → 0 pts, 15+ → 30 pts (linear, capped)
    voloi_pts = min(30.0, 30.0 * min(ratio, 15.0) / 15.0)

    # OTM sweet spot via delta (moneyness fallback when greeks are absent)
    if a.delta is not None:
        d = abs(a.delta)
        if 0.15 <= d <= 0.45:
            delta_pts = 12.0
        elif d < 0.15:
            delta_pts = 12.0 * (d / 0.15)          # lottery tickets score less
        else:
            delta_pts = max(0.0, 12.0 * (0.95 - d) / 0.50)  # ITM scores less
    else:
        u, k = a.underlying_price or 0, a.strike or 0
        otm = (u > 0 and k > 0 and
               ((a.option_type == "call" and k > u) or
                (a.option_type == "put" and k < u)))
        delta_pts = 8.0 if otm else 0.0

    # Time urgency: near-dated flow is more deliberate than far-dated
    dte = days_to_expiry(a.expiry)
    if dte is None:
        dte_pts = 0.0
    elif dte <= 7:
        dte_pts = 8.0
    elif dte <= 30:
        dte_pts = 6.0
    elif dte <= 60:
        dte_pts = 3.0
    else:
        dte_pts = 0.0

    return round(premium_pts + voloi_pts + delta_pts + dte_pts)


def is_auto_watch(a) -> bool:
    """
    Whether an alert should be auto-added to the watchlist: a large *and*
    high-conviction sweep — whale-sized premium, significance score above the
    bar, not deep-ITM, and not already expired.
    """
    return (
        (a.estimated_premium or 0) >= WHALE_PREMIUM
        and not is_deep_itm(a)
        and not is_expired(a)
        and score_alert(a) >= AUTO_WATCH_MIN_SCORE
    )
