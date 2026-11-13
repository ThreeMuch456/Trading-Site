"""Run the screener across a universe: build profiles, detect zones and flags, score, rank."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
from tqdm import tqdm

from .config import Config
from .data import DataFeed, fetch_next_earnings
from .detect import ThinZone, detect_thin_zones
from .flags import Flag, detect_flag
from .flow import FlowInfo, prefetch_scanner_flow, snapshot_flow
from .gex import GexInfo, fetch_gex
from .profile import VolumeProfile, build_profile
from .schedule import market_tz


@dataclass
class Result:
    ticker: str
    bars: pd.DataFrame
    profile: VolumeProfile
    support: ThinZone | None
    resistance: ThinZone | None
    flag: Flag | None = None
    gex: GexInfo | None = None
    combined_score: float = 0.0
    rvol: float | None = None            # last full day's volume / trailing-average day
    adv_dollar: float | None = None      # average daily $ volume (normalizes GEX)
    next_earnings: str | None = None     # ISO date of the next earnings report
    days_to_earnings: float | None = None  # calendar days from the screen to it
    gex_amp: float = 0.0                 # the 0..1 GEX amplification actually scored
    rvol_amp: float = 0.0                # the 0..1 RVOL amplification actually scored
    # Options flow (src/flow.py) — logged as learning features only, never scored.
    flow: FlowInfo | None = None         # from the cached Massive chain snapshot
    scanner_flow: dict | None = None     # Options Flow Scanner 24h rollup

    @property
    def aligned_zone(self) -> ThinZone | None:
        """The thin zone in the setup's direction: the flag's direction when
        there is a flag (bull -> resistance, bear -> support), else the
        better-scored zone. Drives the dashboard's Next HVN target column."""
        if self.flag is not None:
            return self.resistance if self.flag.kind == "bull" else self.support
        s = self.support.score    if self.support    else 0.0
        t = self.resistance.score if self.resistance else 0.0
        if s <= 0.0 and t <= 0.0:
            return None
        return self.resistance if t >= s else self.support


def _daily_sums(bars: pd.DataFrame, values: pd.Series) -> pd.Series:
    """Sum an intraday series into trading days (zero days = closed, dropped)."""
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.empty:
        return pd.Series(dtype=float)
    daily = values.resample("1D").sum()
    return daily[daily > 0]


def compute_rvol(bars: pd.DataFrame, baseline_days: int = 20) -> float | None:
    """Relative volume: the latest day's share volume over the average of the
    prior `baseline_days` days *at the same point in the session* (first-N-bars
    truncation), so an intraday refresh isn't understated by a partial day.
    After the close it's simply day-vs-average-day. > 1 means the stock is
    trading hotter than usual — the push that carries price through an air pocket."""
    if "volume" not in bars or not isinstance(bars.index, pd.DatetimeIndex) or bars.empty:
        return None
    vols = bars["volume"].groupby(bars.index.date, sort=True)
    days = [(d, v.to_numpy(dtype=float)) for d, v in vols if v.sum() > 0]
    if len(days) < 6:             # too little history for a meaningful baseline
        return None
    last_vols = days[-1][1]
    m = len(last_vols)            # bars seen so far in the latest session
    base_days = days[max(0, len(days) - 1 - baseline_days):-1]
    base = float(np.mean([v[:m].sum() for _, v in base_days]))
    if base <= 0:
        return None
    return round(float(last_vols.sum()) / base, 3)


def compute_adv_dollar(bars: pd.DataFrame, baseline_days: int = 20) -> float | None:
    """Average daily dollar volume over the trailing window (Σ close·volume per
    day, averaged). Used to normalize net GEX so 'big' means big *for this name*."""
    if "volume" not in bars or "close" not in bars:
        return None
    daily = _daily_sums(bars, bars["close"] * bars["volume"])
    if daily.empty:
        return None
    return float(daily.iloc[-baseline_days:].mean())


def _closeness(zone: ThinZone, cfg: Config) -> float:
    """1.0 when the LVN is right at price, decaying to 0 at max_distance_pct."""
    max_pct = float(cfg.get("detect.max_distance_pct", 8.0)) or 1.0
    return max(0.0, 1.0 - zone.distance_pct / max_pct)


def score_zone(zone: ThinZone | None, cfg: Config) -> float:
    """Combine closeness, depth, width, and clearance into a 0..1 thinness score."""
    if zone is None:
        return 0.0

    w_close = float(cfg.get("score.w_closeness", 0.40))
    w_depth = float(cfg.get("score.w_depth",     0.30))
    w_width = float(cfg.get("score.w_width",     0.20))
    w_clear = float(cfg.get("score.w_clearance", 0.10))

    max_pct    = float(cfg.get("detect.max_distance_pct", 8.0)) or 1.0
    width_norm = min(1.0, zone.width_pct / max_pct)

    score = (
        w_close * _closeness(zone, cfg)
        + w_depth * zone.depth
        + w_width * width_norm
        + w_clear * (1.0 if zone.clearance else 0.0)
    )
    return round(float(score), 4)


def _agreement_signals(zone_s: float, flag: Flag | None, gex_a: float, rvol_a: float,
                       cfg: Config | None) -> dict[str, bool]:
    """Which of {zone, flag, gex, rvol} clear their own quality bar. A signal
    only counts if it's a *meaningful* reading, not just nonzero — e.g. a zone
    score of 0.05 or a 1%-over-baseline RVOL blip shouldn't count as corroboration."""
    def w(key: str, default: float) -> float:
        return float(cfg.get(key, default)) if cfg is not None else default

    return {
        "zone": zone_s >= w("score.agreement.min_zone_score", 0.4),
        "flag": flag is not None and flag.score >= w("score.agreement.min_flag_score", 0.4),
        "gex":  gex_a >= w("score.agreement.min_gex_amp", 0.2),
        "rvol": rvol_a >= w("score.agreement.min_rvol_amp", 0.2),
    }


def _combined(
    support: ThinZone | None,
    resistance: ThinZone | None,
    flag: Flag | None,
    gex: GexInfo | None = None,
    cfg: Config | None = None,
    adv_dollar: float | None = None,
    rvol: float | None = None,
) -> float:
    """Single ranking score rewarding *coherent* setups: a flag plus thin space in
    the direction the flag points. A bull flag wants room to run up (thin
    resistance); a bear flag wants an air pocket down (thin support). Pairing a
    bull flag with thin support — opposite directions — earns no zone credit.
    With no flag, fall back to the better of the two thin zones.

    The aligned zone's contribution is then scaled by `score.combined.side_weight`
    for its side. The 2026-08-26 excess-return calibration found the zone edge is
    real but one-sided — resistance earns +2.04% over SPY at 10d while support
    reaches its pocket just as often (67%) and still trails the market — so support
    is de-ranked rather than dropped: it keeps ranking, logging and calibrating, so
    the learning loop can still tell us when the regime turns. The weight scales the
    RANK only; the raw zone score still drives the agreement gate and the learning DB.

    Two amplification bonuses then apply, each only when there's a real setup to
    amplify (base > 0):
      GEX  — negative net dealer gamma means hedging trades WITH the move. Scaled
             by magnitude: |net GEX| / (gex_full_at_adv_frac × avg daily $ volume),
             capped at 1 (falls back to binary when ADV$ is unknown). Positive or
             unknown gamma earns nothing.
      RVOL — an air pocket needs a push to get traversed; relative volume above
             1× earns credit linearly up to rvol_full_at.

    Component agreement (noise filter): a single weak/lone signal — a mediocre
    zone with nothing else, or a flag with nothing else — no longer scores. At
    least `score.agreement.min_signals` (default 2) of {zone, flag, gex, rvol}
    must each clear their own quality bar, AND (when score.agreement.require_zone
    is on, the default) a real aligned ZONE must be among them. Requiring the zone
    demotes the flag from a standalone thesis to a pure amplifier: the /learning
    calibration showed the flag score is anti-predictive (higher-scored flags won
    less), so a flag with no air pocket to run into no longer ranks. Set
    require_zone: false to restore the old zone-OR-flag structural rule, or
    score.agreement.enabled: false to drop the gate entirely."""
    def w(key: str, default: float) -> float:
        return float(cfg.get(key, default)) if cfg is not None else default

    w_zone = w("score.combined.w_zone", 0.45)
    w_flag = w("score.combined.w_flag", 0.25)
    w_gex  = w("score.combined.w_gex",  0.15)
    w_rvol = w("score.combined.w_rvol", 0.15)

    # Per-side de-rank. `zone_s` stays the side's RAW score (what the agreement
    # gate and the learning DB see); `zone_r` is the ranked contribution, damped
    # by side_weight. Keeping them separate is what makes this a de-rank rather
    # than a back-door suppression: damping support to 0.5 must not push a 0.7
    # zone under the 0.4 agreement bar and silently delete the setup.
    def side_w(side: str) -> float:
        # A half-written knob (`support:` with no value parses as None) must not
        # take the screen down, and must not silently read as 0 either — an
        # unreadable side weight means "no preference expressed", i.e. 1.0.
        if cfg is None:
            return 1.0
        raw = cfg.get(f"score.combined.side_weight.{side}", 1.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 1.0

    if flag is None:
        # Choose the side on its RANKED value, so a de-ranked side can no longer
        # win the selection on raw score alone; ties fall back to the raw score.
        cands = [(z.score * side_w(name), z.score)
                 for name, z in (("support", support), ("resistance", resistance))
                 if z is not None]
        zone_r, zone_s = max(cands) if cands else (0.0, 0.0)
        base = w_zone * zone_r
    else:
        side    = "resistance" if flag.kind == "bull" else "support"
        aligned = resistance if flag.kind == "bull" else support
        zone_s  = aligned.score if aligned is not None else 0.0
        base    = w_zone * (zone_s * side_w(side)) + w_flag * flag.score

    if base <= 0:
        return 0.0

    gex_a  = gex_amp(gex, adv_dollar, cfg)
    rvol_a = rvol_amp(rvol, cfg)

    agreement_on = bool(cfg.get("score.agreement.enabled", True)) if cfg is not None else True
    if agreement_on:
        sig = _agreement_signals(zone_s, flag, gex_a, rvol_a, cfg)
        min_signals = int(w("score.agreement.min_signals", 2))
        require_zone = bool(cfg.get("score.agreement.require_zone", True)) if cfg is not None else True
        structural = sig["zone"] if require_zone else (sig["zone"] or sig["flag"])
        if not structural or sum(sig.values()) < min_signals:
            return 0.0

    return round(base + w_gex * gex_a + w_rvol * rvol_a, 4)


def gex_amp(gex: GexInfo | None, adv_dollar: float | None, cfg: Config | None = None) -> float:
    """The 0..1 GEX amplification term (also logged as a learning feature)."""
    if gex is None or gex.net_gex >= 0:
        return 0.0
    if adv_dollar and adv_dollar > 0:
        frac = float(cfg.get("score.combined.gex_full_at_adv_frac", 0.05)) if cfg else 0.05
        full_at = frac * adv_dollar
        return min(1.0, abs(gex.net_gex) / full_at) if full_at > 0 else 0.0
    # Gamma sign known, scale NOT measurable (no ADV$). This used to return a full
    # 1.0 — maximum credit for "we could not measure the exposure." The /learning
    # gamma table put that cohort at 31.8% (5d) / 38.6% (10d) win, the worst on the
    # board by a wide margin, so the fallback was paying the most to the weakest
    # rows. Unmeasurable magnitude now scores zero: an unverifiable claim earns no
    # bonus. The names still appear (the sign is logged and rankable elsewhere).
    return 0.0


def rvol_amp(rvol: float | None, cfg: Config | None = None) -> float:
    """The 0..1 RVOL amplification term (also logged as a learning feature)."""
    if rvol is None or rvol <= 1.0:
        return 0.0
    full_at = float(cfg.get("score.combined.rvol_full_at", 2.0)) if cfg else 2.0
    return min(1.0, (rvol - 1.0) / max(full_at - 1.0, 1e-9))


def rescore(r: Result, cfg: Config) -> None:
    """(Re)compute a Result's zone scores, amplification terms, and combined score
    in place, from whatever its zones/profile currently hold.

    Single definition of "how a Result is scored", shared by the full screen below
    and by the pre-open re-price pass (src/reprice.py), so the two can never drift
    into ranking the same setup differently."""
    if r.support is not None:
        r.support.score = score_zone(r.support, cfg)
    if r.resistance is not None:
        r.resistance.score = score_zone(r.resistance, cfg)
    r.gex_amp  = gex_amp(r.gex, r.adv_dollar, cfg)
    r.rvol_amp = rvol_amp(r.rvol, cfg)
    r.combined_score = _combined(r.support, r.resistance, r.flag, r.gex, cfg,
                                 adv_dollar=r.adv_dollar, rvol=r.rvol)


def screen_universe(
    tickers: list[str],
    cfg: Config,
    feed: DataFeed | None = None,
    force_refresh: bool = False,
) -> list[Result]:
    feed = feed or DataFeed(cfg)
    results: list[Result] = []

    for ticker in tqdm(tickers, desc="screening", unit="ticker"):
        try:
            bars    = feed.get_bars(ticker, force_refresh=force_refresh)
            profile = build_profile(ticker, bars, cfg)
            if profile is None:
                continue

            zones      = detect_thin_zones(profile, cfg)
            support    = zones["support"]
            resistance = zones["resistance"]

            flag = detect_flag(bars, cfg)

            results.append(Result(
                ticker=ticker,
                bars=bars,
                profile=profile,
                support=support,
                resistance=resistance,
                flag=flag,
                rvol=compute_rvol(bars, int(cfg.get("score.rvol_baseline_days", 20))),
                adv_dollar=compute_adv_dollar(bars),
            ))
        except Exception as exc:  # noqa: BLE001 - one bad ticker shouldn't kill the run
            print(f"[screen] {ticker} failed: {exc}")

    # Dealer positioning: fetch net GEX per ticker (parallel, cached daily), then
    # score. GEX failures are neutral — the screen never depends on options data.
    gex_map: dict[str, GexInfo | None] = {}
    if results and cfg.get("gex.enabled", True):
        gex_map = _prefetch_gex([(r.ticker, r.profile.current_price) for r in results], cfg)

    # Next earnings dates (parallel, cached) — the catalyst column. Best-effort.
    earnings_map: dict[str, str | None] = {}
    if results and cfg.get("earnings.enabled", True):
        earnings_map = _prefetch_earnings([r.ticker for r in results], cfg)

    # Options flow: snapshot features read the chain the GEX phase just cached
    # (no extra fetch); the scanner rollup is an optional HTTP call. Both are
    # learning-log features only — neither touches rescore().
    scanner_map: dict[str, dict | None] = {}
    flow_on = results and cfg.get("flow.enabled", True)
    if flow_on:
        scanner_map = prefetch_scanner_flow([r.ticker for r in results], cfg)
    session = datetime.now(market_tz(cfg)).date()

    today = date.today()
    for r in results:
        r.gex = gex_map.get(r.ticker)
        if flow_on:
            r.flow = snapshot_flow(r.ticker, r.profile.current_price, cfg, session)
            r.scanner_flow = scanner_map.get(r.ticker)
        r.next_earnings = earnings_map.get(r.ticker)
        if r.next_earnings:
            try:
                r.days_to_earnings = float((date.fromisoformat(r.next_earnings) - today).days)
            except ValueError:
                r.next_earnings = None
        rescore(r, cfg)

    return results


def _prefetch_gex(pairs: list[tuple[str, float]], cfg: Config) -> dict[str, GexInfo | None]:
    """Fetch net GEX for (ticker, spot) pairs in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: dict[str, GexInfo | None] = {}
    workers = max(1, int(cfg.get("gex.max_workers", 4)))
    print(f"[screen] fetching dealer GEX for {len(pairs)} tickers…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_gex, t, s, cfg): t for t, s in pairs}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                out[ticker] = fut.result()
            except Exception:  # noqa: BLE001
                out[ticker] = None
    n_neg = sum(1 for g in out.values() if g is not None and g.net_gex < 0)
    n_ok  = sum(1 for g in out.values() if g is not None)
    print(f"[screen] GEX resolved for {n_ok}/{len(pairs)} tickers ({n_neg} negative gamma)")
    return out


def _prefetch_earnings(tickers: list[str], cfg: Config) -> dict[str, str | None]:
    """Fetch next-earnings dates in parallel (cached on disk per ticker)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: dict[str, str | None] = {}
    workers = max(1, int(cfg.get("earnings.max_workers", 4)))
    print(f"[screen] fetching earnings dates for {len(tickers)} tickers…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_next_earnings, t, cfg): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                out[ticker] = fut.result()
            except Exception:  # noqa: BLE001
                out[ticker] = None
    n_ok = sum(1 for v in out.values() if v)
    print(f"[screen] earnings dates resolved for {n_ok}/{len(tickers)} tickers")
    return out


def rank(results: list[Result], direction: str) -> list[Result]:
    """Ranked copy of results by score in the given direction.

    direction: "support", "resistance", "both", or "combined".
    """
    def key(r: Result) -> float:
        if direction == "combined":
            return r.combined_score
        s = r.support.score    if r.support    else 0.0
        t = r.resistance.score if r.resistance else 0.0
        if direction == "support":    return s
        if direction == "resistance": return t
        return max(s, t)  # "both"

    ranked = [r for r in results if key(r) > 0]
    ranked.sort(key=key, reverse=True)
    return ranked
