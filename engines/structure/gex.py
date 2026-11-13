"""Dealer gamma exposure (GEX) from listed option chains.

Estimates net dealer gamma using the standard naive convention — dealers are
long the calls and short the puts their customers hold:

    net GEX ($ per 1% move) = Σ_calls Γ·OI·100·S²·1%  −  Σ_puts Γ·OI·100·S²·1%

**Negative net GEX** = dealers are short gamma: their delta-hedging sells into
declines and buys into rallies, *amplifying* moves — the regime where price can
really run through a thin zone. **Positive** = hedging leans against the move
(dampening).

Providers, tried in order under "auto":
  massive  — Massive.com (ex-Polygon.io) paid chain snapshot: OPRA-wide,
             exchange-computed greeks + OI + day volume, 15-min delayed
             (irrelevant post-close). Used when an API key is configured
             (env MASSIVE_API_KEY / POLYGON_API_KEY). Raw snapshots are
             cached and shared with the universe liquidity filter.
  cboe     — free delayed quotes CDN, greeks included, CBOE-listed only.
  yfinance — chains without greeks; Black-Scholes gamma computed from each
             contract's implied volatility (r = 0; negligible at these tenors).

Results are cached per ticker/day so the daily job fetches each chain at most once.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config


@dataclass
class GexInfo:
    net_gex: float        # $ gamma per 1% move (calls − puts, naive dealer)
    n_expiries: int       # expiries included
    n_contracts: int      # contracts included after cleaning
    # Snapshot enrichments (Massive path only; None on CBOE/yfinance and on
    # pre-upgrade caches). Logged into the learning features at detection.
    atm_iv: float | None = None              # mean IV of the strikes nearest spot
    flip_price: float | None = None          # naive zero-gamma level (strike-cumulative)
    top_gamma_strikes: list | None = None    # biggest gamma walls, descending
    call_volume: float | None = None         # day volume, calls (nearest expiries)
    put_volume: float | None = None          # day volume, puts
    total_oi: float | None = None            # open interest, calls + puts

    @property
    def negative(self) -> bool:
        """True when dealers are net short gamma → hedging amplifies moves."""
        return self.net_gex < 0


# ── Pure math (testable without network) ────────────────────────────────────────

def _bs_gamma(spot: float, strikes, t_years: float, ivs) -> np.ndarray:
    """Black-Scholes gamma per contract (r = 0)."""
    strikes = np.asarray(strikes, dtype=float)
    ivs = np.asarray(ivs, dtype=float)
    sig_t = ivs * np.sqrt(t_years)
    d1 = (np.log(spot / strikes) + 0.5 * ivs * ivs * t_years) / sig_t
    pdf = np.exp(-0.5 * d1 * d1) / np.sqrt(2.0 * np.pi)
    return pdf / (spot * sig_t)


def _side_dollar_gamma(spot: float, df: pd.DataFrame, t_years: float) -> tuple[float, int]:
    """Total $ gamma per 1% move for one side (calls or puts) of a chain."""
    if df is None or df.empty:
        return 0.0, 0
    k = df["strike"].to_numpy(dtype=float)
    oi = df["openInterest"].fillna(0).to_numpy(dtype=float)
    iv = df["impliedVolatility"].fillna(0).to_numpy(dtype=float)

    ok = (oi > 0) & (iv > 0.01) & (iv < 5.0) & (k > 0)
    if not ok.any():
        return 0.0, 0

    gamma = _bs_gamma(spot, k[ok], t_years, iv[ok])
    dollars = gamma * oi[ok] * 100.0 * spot * spot * 0.01
    return float(np.nansum(dollars)), int(ok.sum())


def compute_net_gex(spot: float, chains: list[tuple[pd.DataFrame, pd.DataFrame, float]]) -> GexInfo | None:
    """chains: list of (calls_df, puts_df, t_years). Returns None if nothing usable."""
    if spot <= 0 or not chains:
        return None
    total, contracts, expiries = 0.0, 0, 0
    for calls, puts, t_years in chains:
        if t_years <= 0:
            continue
        c_sum, c_n = _side_dollar_gamma(spot, calls, t_years)
        p_sum, p_n = _side_dollar_gamma(spot, puts, t_years)
        if c_n + p_n == 0:
            continue
        total += c_sum - p_sum
        contracts += c_n + p_n
        expiries += 1
    if contracts == 0:
        return None
    return GexInfo(net_gex=round(total, 2), n_expiries=expiries, n_contracts=contracts)


# ── Fetch + cache ───────────────────────────────────────────────────────────────

def _cache_path(cfg: Config, ticker: str) -> Path:
    d = Path(cfg.get("data.cache_dir", "data")) / "gex"
    d.mkdir(parents=True, exist_ok=True)
    safe = ticker.replace("/", "_").replace("\\", "_")
    return d / f"{safe}.json"


def fetch_gex(ticker: str, spot: float, cfg: Config) -> GexInfo | None:
    """Net dealer GEX for a ticker, cached for gex.cache_hours. None on any failure
    (no options, rate limit, junk data) — callers treat None as neutral."""
    path = _cache_path(cfg, ticker)
    max_age_h = float(cfg.get("gex.cache_hours", 12))
    if path.exists() and (time.time() - path.stat().st_mtime) / 3600.0 <= max_age_h:
        try:
            d = json.loads(path.read_text())
            return GexInfo(
                d["net_gex"], d["n_expiries"], d["n_contracts"],
                # .get: caches written before the snapshot enrichments existed
                atm_iv=d.get("atm_iv"), flip_price=d.get("flip_price"),
                top_gamma_strikes=d.get("top_gamma_strikes"),
                call_volume=d.get("call_volume"), put_volume=d.get("put_volume"),
                total_oi=d.get("total_oi"),
            )
        except Exception:  # noqa: BLE001 - corrupt cache -> refetch
            pass

    retries = max(0, int(cfg.get("gex.retries", 2)))
    for attempt in range(retries + 1):
        try:
            info = _fetch_once(ticker, spot, cfg)
            if info is not None:
                from dataclasses import asdict
                path.write_text(json.dumps({**asdict(info), "ts": time.time()}))
            return info
        except Exception:  # noqa: BLE001 - GEX is a bonus signal; never fail the screen
            if attempt < retries:
                # Rate-limit friendly backoff (Yahoo 429s bursts from cloud IPs).
                time.sleep(1.5 * (attempt + 1))
    return None


def _fetch_once(ticker: str, spot: float, cfg: Config) -> GexInfo | None:
    """Try the configured provider(s) in order. "auto" = Massive (when an API
    key is configured), then CBOE, then yfinance.

    Massive ships OPRA-wide greeks/OI on the paid plan; CBOE's free CDN covers
    its own listings and is the keyless fallback (both reachable from datacenter
    IPs, where Yahoo blocks chain endpoints)."""
    provider = str(cfg.get("gex.provider", "auto")).lower()
    auto = ("massive", "cboe", "yfinance") if _massive_key(cfg) else ("cboe", "yfinance")
    order = {"auto": auto, "massive": ("massive",), "cboe": ("cboe",),
             "yfinance": ("yfinance",)}.get(provider, auto)

    fetchers = {"massive": _fetch_massive, "cboe": _fetch_cboe, "yfinance": _fetch_yfinance}
    for name in order:
        try:
            info = fetchers[name](ticker, spot, cfg)
        except Exception:  # noqa: BLE001 - fall through to the next provider
            info = None
        if info is not None:
            return info
    return None


# ── Massive.com (ex-Polygon.io) provider ────────────────────────────────────────

def _massive_key(cfg: Config) -> str | None:
    """API key from config or environment (MASSIVE_API_KEY / POLYGON_API_KEY —
    old Polygon keys keep working after the rebrand). None = provider disabled."""
    return (cfg.get("gex.massive_api_key")
            or os.environ.get("MASSIVE_API_KEY")
            or os.environ.get("POLYGON_API_KEY")
            or None)


def massive_net_gex(spot: float, contracts: list[dict], max_days: int, max_expiries: int,
                    today: date | None = None) -> GexInfo | None:
    """Net GEX + snapshot enrichments from trimmed Massive contracts (pure).

    Each contract dict: {"cp": "call"|"put", "exp": "YYYY-MM-DD", "strike":
    float|None, "gamma": float|None, "iv": float|None, "oi": float, "vol":
    float} — see _trim_contract. Contracts missing native gamma (deep ITM, or a
    plan without computed Greeks) fall back to Black-Scholes gamma from their IV
    — same math as the yfinance provider — so flip/walls still work without a
    Greeks entitlement, as long as IV is present. Truly missing gamma+IV still
    count toward flow/OI but not the gamma math.

    Enrichments, all over the same nearest-expiry window:
      atm_iv            — mean IV of the (≤4) contracts struck nearest spot
      flip_price        — naive zero-gamma level: where the strike-cumulative
                          net $gamma (ascending strikes) crosses zero
      top_gamma_strikes — the 3 strikes with the most gross gamma $ (walls)
      call/put volume, total OI — directional flow inputs"""
    if spot <= 0 or not contracts:
        return None
    today = today or datetime.now(timezone.utc).date()

    rows: list[dict] = []
    for c in contracts:
        cp, exp = c.get("cp"), c.get("exp") or ""
        if cp not in ("call", "put"):
            continue
        try:
            days = (date.fromisoformat(exp) - today).days
        except ValueError:
            continue
        if days < 1 or days > max_days:
            continue
        rows.append(c)
    if not rows:
        return None

    nearest = sorted({r["exp"] for r in rows})[:max(1, max_expiries)]
    sub = [r for r in rows if r["exp"] in nearest]

    # Net dealer gamma (calls − puts) and per-strike profiles.
    total = 0.0
    n_contracts = 0
    net_by_strike: dict[float, float] = {}    # signed $ gamma per strike (flip)
    gross_by_strike: dict[float, float] = {}  # |$ gamma| per strike (walls)
    for r in sub:
        gamma = float(r.get("gamma") or 0.0)
        strike, iv = r.get("strike"), r.get("iv")
        if gamma <= 0 and strike and iv and 0.01 < float(iv) < 5.0:
            # No native gamma (deep ITM, or the plan lacks computed Greeks) —
            # derive it from IV via Black-Scholes instead of dropping the contract.
            try:
                t_years = (date.fromisoformat(r["exp"]) - today).days / 365.0
                gamma = float(_bs_gamma(spot, [float(strike)], t_years, [float(iv)])[0])
            except Exception:  # noqa: BLE001 - malformed contract -> stays excluded below
                gamma = 0.0
        oi = float(r.get("oi") or 0.0)
        if gamma <= 0 or oi <= 0:
            continue
        dollars = gamma * oi * 100.0 * spot * spot * 0.01
        total += dollars if r["cp"] == "call" else -dollars
        n_contracts += 1
        strike = r.get("strike")
        if strike:
            k = float(strike)
            net_by_strike[k] = net_by_strike.get(k, 0.0) + (dollars if r["cp"] == "call" else -dollars)
            gross_by_strike[k] = gross_by_strike.get(k, 0.0) + dollars
    if n_contracts == 0:
        return None

    # ATM IV: mean over the contracts struck nearest spot with a sane IV.
    ivs = [(abs(float(r["strike"]) - spot), float(r["iv"]))
           for r in sub
           if r.get("strike") and 0.01 < float(r.get("iv") or 0.0) < 5.0]
    ivs.sort(key=lambda t: t[0])
    atm_iv = round(float(np.mean([iv for _, iv in ivs[:4]])), 4) if ivs else None

    # Gamma walls: top strikes by gross gamma dollars.
    walls = sorted(gross_by_strike, key=gross_by_strike.get, reverse=True)[:3] or None

    # Naive zero-gamma flip: walk strikes ascending, accumulate net $ gamma,
    # linearly interpolate where the running sum crosses zero. None if it never
    # crosses (uniformly positive/negative profile).
    flip = None
    if len(net_by_strike) >= 2:
        strikes = sorted(net_by_strike)
        cum = np.cumsum([net_by_strike[k] for k in strikes])
        for i in range(len(cum) - 1):
            a, b = cum[i], cum[i + 1]
            if a == 0 or (a < 0) == (b < 0):
                continue
            frac = abs(a) / (abs(a) + abs(b)) if (abs(a) + abs(b)) > 0 else 0.5
            flip = round(strikes[i] + frac * (strikes[i + 1] - strikes[i]), 4)
            break

    call_vol = sum(float(r.get("vol") or 0) for r in sub if r["cp"] == "call")
    put_vol = sum(float(r.get("vol") or 0) for r in sub if r["cp"] == "put")
    total_oi = sum(float(r.get("oi") or 0) for r in sub)

    return GexInfo(
        net_gex=round(total, 2), n_expiries=len(nearest), n_contracts=n_contracts,
        atm_iv=atm_iv, flip_price=flip, top_gamma_strikes=walls,
        call_volume=call_vol, put_volume=put_vol, total_oi=total_oi,
    )


def _trim_contract(raw: dict) -> dict | None:
    """Keep only what GEX, the enrichment features, and the liquidity filter
    need, so cached snapshots stay small."""
    details = raw.get("details") or {}
    cp = details.get("contract_type")
    exp = details.get("expiration_date")
    if cp not in ("call", "put") or not exp:
        return None
    greeks = raw.get("greeks") or {}
    day = raw.get("day") or {}
    trimmed = {
        "cp": cp,
        "exp": exp,
        "strike": details.get("strike_price"),
        "gamma": greeks.get("gamma"),
        "iv": raw.get("implied_volatility"),
        "oi": raw.get("open_interest") or 0,
        "vol": day.get("volume") or 0,
    }
    sym = details.get("ticker")          # "O:AAPL260717C00100000" — needed to stream AM
    if sym:                              # conditional: keeps pre-existing cache/test shape
        trimmed["sym"] = sym
    # Flow inputs (src/flow.py), conditional for the same reason as "sym".
    # px: the day's VWAP, so vol × px × 100 is the dollars actually traded; close /
    # last trade as fallbacks (no bid/ask on the Starter plan).
    trade = raw.get("last_trade") or {}
    px = day.get("vwap") or day.get("close") or trade.get("price")
    if px:
        trimmed["px"] = px
    if greeks.get("delta") is not None:
        trimmed["delta"] = greeks.get("delta")
    bar = _bar_date(day)
    if bar:
        trimmed["bar"] = bar
    return trimmed


def _bar_date(day: dict) -> str | None:
    """Market-tz session date the `day` aggregate belongs to ("YYYY-MM-DD").

    Massive keeps returning the PREVIOUS session's day bar for a contract that
    hasn't traded yet today: volume, close and VWAP all stale (the Options Flow
    Scanner measured ~44% of volume-bearing index-ETF contracts). `last_updated`
    (ns) is stamped at midnight ET of the bar's own session, which separates the
    two cases. None when absent — callers decide how permissive to be."""
    ns = day.get("last_updated")
    if not ns:
        return None
    try:
        dt = datetime.fromtimestamp(float(ns) / 1e9, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def read_snapshot_cache(cfg: Config, ticker: str) -> list[dict] | None:
    """Trimmed option snapshot from disk cache only (no network). The live monitor
    reuses what the daily screen already fetched. None if uncached/unreadable."""
    path = _massive_cache_path(cfg, ticker)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("contracts")
    except Exception:  # noqa: BLE001
        return None


def _massive_cache_path(cfg: Config, ticker: str) -> Path:
    d = Path(cfg.get("data.cache_dir", "data")) / "massive"
    d.mkdir(parents=True, exist_ok=True)
    safe = ticker.replace("/", "_").replace("\\", "_")
    return d / f"{safe}.json"


def snapshot_cache_ts(cfg: Config, ticker: str) -> float | None:
    """Epoch seconds the cached snapshot was fetched, or None if uncached."""
    path = _massive_cache_path(cfg, ticker)
    if not path.exists():
        return None
    try:
        return float(json.loads(path.read_text()).get("ts"))
    except Exception:  # noqa: BLE001
        return None


def _massive_snapshot(ticker: str, cfg: Config, force: bool = False) -> list[dict] | None:
    """Trimmed option-chain snapshot for a ticker (paginated fetch, disk-cached).

    One snapshot serves both dealer GEX and the universe options-liquidity
    filter, so a shared per-ticker cache (gex.cache_hours) means the daily job
    hits Massive at most once per name. Returns None when the provider is
    unconfigured or the fetch fails; [] when the ticker has no listed options.
    `force` skips the cache (src/flow.py: a pre-close snapshot isn't the day's flow)."""
    import requests

    key = _massive_key(cfg)
    if not key:
        return None

    path = _massive_cache_path(cfg, ticker)
    max_age_h = float(cfg.get("gex.cache_hours", 12))
    if not force and path.exists() and (time.time() - path.stat().st_mtime) / 3600.0 <= max_age_h:
        try:
            return json.loads(path.read_text())["contracts"]
        except Exception:  # noqa: BLE001 - corrupt cache -> refetch
            pass

    sym = ticker.replace("-", ".").upper()      # Yahoo BRK-B -> Massive BRK.B
    base = str(cfg.get("gex.massive_base_url", "https://api.massive.com")).rstrip("/")
    today = datetime.now(timezone.utc).date()
    lte = today + timedelta(days=int(cfg.get("gex.max_days", 45)))
    url = (f"{base}/v3/snapshot/options/{sym}?limit=250"
           f"&expiration_date.gte={today.isoformat()}&expiration_date.lte={lte.isoformat()}")
    headers = {"Authorization": f"Bearer {key}"}

    contracts: list[dict] = []
    for _ in range(40):                          # page cap: 40 x 250 = 10k contracts
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for raw in data.get("results") or []:
            t = _trim_contract(raw)
            if t is not None:
                contracts.append(t)
        url = data.get("next_url")
        if not url:
            break

    try:
        path.write_text(json.dumps({"contracts": contracts, "ts": time.time()}))
    except Exception:  # noqa: BLE001 - cache write is best-effort
        pass
    return contracts


def _fetch_massive(ticker: str, spot: float, cfg: Config) -> GexInfo | None:
    contracts = _massive_snapshot(ticker, cfg)
    if not contracts:
        return None
    return massive_net_gex(
        spot, contracts,
        max_days=int(cfg.get("gex.max_days", 45)),
        max_expiries=int(cfg.get("gex.max_expiries", 2)),
    )


def option_activity(contracts: list[dict], n_expiries: int,
                    today: date | None = None) -> tuple[int, int] | None:
    """(volume, open interest) summed over the nearest n expiries — pure.
    None when there's no usable expiry (no listed options)."""
    today = today or datetime.now(timezone.utc).date()
    live: dict[str, list[dict]] = {}
    for c in contracts:
        try:
            if (date.fromisoformat(c.get("exp") or "") - today).days >= 1:
                live.setdefault(c["exp"], []).append(c)
        except ValueError:
            continue
    if not live:
        return None
    nearest = sorted(live)[:max(1, n_expiries)]
    vol = sum(float(c.get("vol") or 0) for e in nearest for c in live[e])
    oi = sum(float(c.get("oi") or 0) for e in nearest for c in live[e])
    return int(vol), int(oi)


def fetch_option_activity(ticker: str, cfg: Config, n_expiries: int) -> tuple[int, int] | None:
    """Options (volume, OI) over the nearest expiries via Massive, for the
    universe liquidity filter. None = provider unavailable / fetch failed /
    no options — the caller falls back to yfinance chains."""
    try:
        contracts = _massive_snapshot(ticker, cfg)
    except Exception:  # noqa: BLE001
        return None
    if not contracts:
        return None
    return option_activity(contracts, n_expiries)


# CBOE wants a browser-ish UA; some CDN POPs 403 the default python-requests one.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def cboe_net_gex(spot: float, options: list[dict], max_days: int, max_expiries: int,
                 today=None) -> GexInfo | None:
    """Net GEX from CBOE contract dicts (pure — testable without network).

    Each dict carries an OCC symbol (`ROOT` + YYMMDD + C|P + 8-digit strike) plus
    exchange-computed `gamma` and `open_interest`, so no BS math is needed."""
    if spot <= 0 or not options:
        return None
    today = today or datetime.now(timezone.utc).date()

    by_expiry: dict[str, float] = {}          # yymmdd -> signed $ gamma
    counts: dict[str, int] = {}
    for o in options:
        sym = str(o.get("option") or "")
        if len(sym) < 16:                      # ROOT(1+) + 6 date + 1 C/P + 8 strike
            continue
        tail = sym[-15:]
        yymmdd, cp = tail[:6], tail[6]
        if cp not in ("C", "P"):
            continue
        try:
            days = (datetime.strptime(yymmdd, "%y%m%d").date() - today).days
        except ValueError:
            continue
        if days < 1 or days > max_days:
            continue
        gamma = float(o.get("gamma") or 0.0)
        oi = float(o.get("open_interest") or 0.0)
        if gamma <= 0 or oi <= 0:
            continue
        dollars = gamma * oi * 100.0 * spot * spot * 0.01
        by_expiry[yymmdd] = by_expiry.get(yymmdd, 0.0) + (dollars if cp == "C" else -dollars)
        counts[yymmdd] = counts.get(yymmdd, 0) + 1

    if not by_expiry:
        return None
    # Nearest N expiries only, matching the yfinance path's methodology.
    nearest = sorted(by_expiry)[:max(1, max_expiries)]
    total = sum(by_expiry[e] for e in nearest)
    n_contracts = sum(counts[e] for e in nearest)
    return GexInfo(net_gex=round(total, 2), n_expiries=len(nearest), n_contracts=n_contracts)


def _fetch_cboe(ticker: str, spot: float, cfg: Config) -> GexInfo | None:
    import requests

    sym = ticker.replace("-", ".").upper()     # Yahoo BRK-B -> CBOE/OCC BRK.B
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    return cboe_net_gex(
        spot,
        data.get("options") or [],
        max_days=int(cfg.get("gex.max_days", 45)),
        max_expiries=int(cfg.get("gex.max_expiries", 2)),
    )


def _fetch_yfinance(ticker: str, spot: float, cfg: Config) -> GexInfo | None:
    import yfinance as yf

    t = yf.Ticker(ticker)
    expiries = t.options
    if not expiries:
        return None

    max_days = int(cfg.get("gex.max_days", 45))
    max_expiries = int(cfg.get("gex.max_expiries", 2))
    today = datetime.now(timezone.utc).date()

    chains: list[tuple[pd.DataFrame, pd.DataFrame, float]] = []
    for exp in expiries:
        days = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if days < 1 or days > max_days:
            continue
        chain = t.option_chain(exp)
        chains.append((chain.calls, chain.puts, days / 365.0))
        if len(chains) >= max_expiries:
            break

    return compute_net_gex(spot, chains)
