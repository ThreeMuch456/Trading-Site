"""Build the ticker list to screen.

Sources:
  "watchlist" — hardcoded list from config.yaml (fast, for development).
  "sp500"     — S&P 500 members scraped from Wikipedia.
  "ndx100"    — Nasdaq-100 members scraped from Wikipedia.
  "filtered"  — sp500 + ndx100 combined, deduplicated, then filtered for
                common stock / high avg volume / live options chain.
                Filter results are cached to disk (see filter.cache_hours).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from .config import Config

_SP500_WIKI  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX100_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"

# Wikipedia 403s requests with no/!browser User-Agent, so set one explicitly.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ── Index scrapers ─────────────────────────────────────────────────────────────

def _read_wiki_tables(url: str):
    """Fetch a Wikipedia page with a browser UA and parse its tables."""
    import io

    import pandas as pd
    import requests

    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _load_sp500() -> list[str]:
    tables = _read_wiki_tables(_SP500_WIKI)
    df = tables[0]
    symbols = df["Symbol"].astype(str).str.strip()
    return [s.replace(".", "-") for s in symbols if s and s.lower() != "nan"]


def _load_ndx100() -> list[str]:
    tables = _read_wiki_tables(_NDX100_WIKI)
    for df in tables:
        lower = {str(c).lower(): c for c in df.columns}
        col = lower.get("ticker") or lower.get("symbol")
        if col is not None and len(df) > 50:
            return [
                s.replace(".", "-")
                for s in df[col].astype(str).str.strip()
                if s and s.lower() != "nan"
            ]
    return []


# ── Per-ticker filter ──────────────────────────────────────────────────────────

def _ticker_metrics(ticker: str, cfg: Config) -> dict | None:
    """Fetch the liquidity metrics for one ticker. Returns None on total failure.

    options_volume / options_oi are the calls+puts totals summed over the nearest
    filter.options_expiries expiries (spanning both a weekly and the front monthly
    smooths the weekly/monthly split). They are None when the chain fetch failed,
    so the pass/fail logic can distinguish "illiquid" from "provider hiccup".

    Options activity comes from the Massive snapshot when an API key is
    configured (one cached request per ticker, shared with GEX); the yfinance
    chain walk is the keyless fallback."""
    import yfinance as yf

    from .gex import fetch_option_activity

    n_exp = max(1, int(cfg.get("filter.options_expiries", 2)))

    try:
        t = yf.Ticker(ticker)

        # Prefer fast_info (one HTTP round-trip); fall back to full info.
        try:
            fi         = t.fast_info
            avg_vol    = float(getattr(fi, "three_month_average_volume", None) or 0)
            quote_type = str(getattr(fi, "quote_type", "") or "").upper()
        except Exception:
            info       = t.info
            avg_vol    = float(
                info.get("averageVolume3Month") or info.get("averageVolume") or 0
            )
            quote_type = str(info.get("quoteType") or "").upper()

        opt_volume = opt_oi = None
        has_options = False

        activity = fetch_option_activity(ticker, cfg, n_exp)
        if activity is not None:
            opt_volume, opt_oi = activity
            has_options = True
        else:
            exps = list(t.options or ())
            has_options = bool(exps)
            if exps:
                vol_sum = oi_sum = 0
                fetched = False
                for exp in exps[:n_exp]:
                    try:
                        chain = t.option_chain(exp)
                        vol_sum += int(
                            chain.calls["volume"].fillna(0).sum()
                            + chain.puts["volume"].fillna(0).sum()
                        )
                        oi_sum += int(
                            chain.calls["openInterest"].fillna(0).sum()
                            + chain.puts["openInterest"].fillna(0).sum()
                        )
                        fetched = True
                    except Exception:
                        continue
                if fetched:
                    opt_volume, opt_oi = vol_sum, oi_sum

        return {
            "quote_type": quote_type,
            "avg_volume": avg_vol,
            "has_options": has_options,
            "options_volume": opt_volume,
            "options_oi": opt_oi,
        }
    except Exception:
        return None


def _passes_filter(m: dict | None, cfg: Config) -> bool:
    """Pure pass/fail on fetched metrics — every liquidity gate lives here."""
    if m is None:
        return False

    min_vol   = int(cfg.get("filter.min_avg_volume", 1_000_000))
    min_opt_v = int(cfg.get("filter.min_options_volume", 0))
    min_oi    = int(cfg.get("filter.min_options_oi", 5_000))

    # Common stock only (ETFs, preferred shares, warrants, etc. are excluded).
    if m["quote_type"] and m["quote_type"] != "EQUITY":
        return False

    # High daily share volume.
    if m["avg_volume"] < min_vol:
        return False

    # Options chain must exist.
    if not m["has_options"]:
        return False

    # High daily options volume + open interest. Enforced only when the chain
    # fetch succeeded — a transient fetch failure doesn't penalise the ticker.
    if min_opt_v > 0 and m["options_volume"] is not None and m["options_volume"] < min_opt_v:
        return False
    if min_oi > 0 and m["options_oi"] is not None and m["options_oi"] < min_oi:
        return False

    return True


def _filter_one(ticker: str, cfg: Config) -> bool:
    """Return True if ticker passes all liquidity / type filters."""
    return _passes_filter(_ticker_metrics(ticker, cfg), cfg)


# ── Universe filter with disk cache ───────────────────────────────────────────

def _filter_signature(cfg: Config) -> dict:
    """Identity of the filter result — changing any of these invalidates the cache."""
    return {
        "min_avg_volume": int(cfg.get("filter.min_avg_volume", 1_000_000)),
        "min_options_volume": int(cfg.get("filter.min_options_volume", 0)),
        "min_options_oi": int(cfg.get("filter.min_options_oi", 5_000)),
        "options_expiries": int(cfg.get("filter.options_expiries", 2)),
    }


def _filter_universe(tickers: list[str], cfg: Config) -> list[str]:
    cache_path = Path(cfg.get("data.cache_dir", "data")) / "universe_filter_cache.json"
    max_age_h  = float(cfg.get("filter.cache_hours", 48))
    sig        = _filter_signature(cfg)

    if cache_path.exists():
        age_h = (time.time() - cache_path.stat().st_mtime) / 3600.0
        if age_h <= max_age_h:
            try:
                data   = json.loads(cache_path.read_text())
                cached = data.get("tickers", [])
                # Reuse only a non-empty result whose filter params still match.
                if cached and data.get("params") == sig:
                    print(f"[universe] loaded {len(cached)} tickers from filter cache "
                          f"({age_h:.1f}h old — max {max_age_h}h)")
                    return cached
            except Exception:
                pass

    print(
        f"[universe] filtering {len(tickers)} tickers  "
        f"(avg share vol ≥ {cfg.get('filter.min_avg_volume', 1_000_000):,}  "
        f"options vol ≥ {cfg.get('filter.min_options_volume', 0):,}  "
        f"options OI ≥ {cfg.get('filter.min_options_oi', 5_000):,})…"
    )
    max_workers = int(cfg.get("filter.max_workers", 20))
    passed: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_filter_one, t, cfg): t for t in tickers}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="filtering", disable=None):
            ticker = futures[fut]
            try:
                if fut.result():
                    passed.append(ticker)
            except Exception:
                pass

    passed.sort()

    if not passed:
        # Every check failed — almost certainly a transient provider problem
        # (rate limiting, outage), not a universe with zero liquid names. Don't
        # cache the empty result (it would blank the screener for cache_hours);
        # fall back to the unfiltered list so the screen still has work to do.
        print("[universe] filter returned 0 tickers (provider failure?); "
              "using unfiltered list and NOT caching")
        return tickers

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"tickers": passed, "ts": time.time(), "params": sig}))
    print(f"[universe] {len(passed)} / {len(tickers)} tickers passed filter")
    return passed


# ── Public entry point ─────────────────────────────────────────────────────────

def get_universe(cfg: Config) -> list[str]:
    source    = (cfg.get("universe.source") or "watchlist").lower()
    watchlist = list(cfg.get("universe.watchlist") or [])

    if source == "watchlist":
        return watchlist

    if source == "sp500":
        try:
            tickers = _load_sp500()
            if tickers:
                return tickers
        except Exception as exc:
            print(f"[universe] sp500 load failed ({exc}); falling back to watchlist")
        return watchlist

    if source == "ndx100":
        try:
            tickers = _load_ndx100()
            if tickers:
                return tickers
        except Exception as exc:
            print(f"[universe] ndx100 load failed ({exc}); falling back to watchlist")
        return watchlist

    if source == "filtered":
        raw: list[str] = []
        try:
            raw.extend(_load_sp500())
        except Exception as exc:
            print(f"[universe] sp500 scrape failed ({exc})")
        try:
            raw.extend(_load_ndx100())
        except Exception as exc:
            print(f"[universe] ndx100 scrape failed ({exc})")
        if not raw:
            print("[universe] both index scrapes failed; falling back to watchlist")
            return watchlist
        # Deduplicate while preserving first-seen order.
        seen: set[str] = set()
        deduped = [t for t in raw if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
        print(f"[universe] combined universe: {len(deduped)} unique tickers before filter")
        return _filter_universe(deduped, cfg)

    print(f"[universe] unknown source {source!r}; using watchlist")
    return watchlist
