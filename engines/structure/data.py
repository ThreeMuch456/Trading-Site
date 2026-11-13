"""Fetch OHLCV bars per ticker, with an on-disk cache.

The provider is an interface (`BarProvider`) so the rest of the codebase never
imports yfinance directly. Swap in Polygon / Tiingo / Alpaca later by writing a
new provider and returning it from `make_provider`.

Cached bars live as Parquet under cfg["data"]["cache_dir"]. A ticker is
refetched only when its cache is missing or older than cache_max_age_hours.

Also home to the next-earnings-date lookup (cached JSON per ticker) — earnings
inside a setup's evaluation window are the classic catalyst that both creates
and confounds air-pocket moves, so the screen surfaces them.
"""
from __future__ import annotations

import json
import math
import time
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .config import Config

# Expected columns on every bars DataFrame this module returns.
OHLCV = ["open", "high", "low", "close", "volume"]


class BarProvider(ABC):
    """Anything that can return normalized OHLCV bars for a ticker."""

    @abstractmethod
    def fetch(self, ticker: str, timeframe: str, lookback_days: int) -> pd.DataFrame:
        """Return a DataFrame indexed by timestamp with columns OHLCV.

        Returns an empty DataFrame (columns OHLCV) when no data is available.
        """


class YFinanceProvider(BarProvider):
    """Free prototype provider. Mind the intraday history limits:
    1m ~7d, 5m/15m/1h ~60-730d depending on Yahoo's mood.
    """

    def fetch(self, ticker: str, timeframe: str, lookback_days: int) -> pd.DataFrame:
        import yfinance as yf

        period = f"{int(lookback_days)}d"
        df = yf.download(
            ticker,
            period=period,
            interval=timeframe,
            # Split/dividend-adjusted prices: without this, any split inside the
            # lookback opens a huge artificial gap that wrecks the price range,
            # the bins, and every level derived from them (POC/VA/LVN). Adjusted
            # prices also match what TradingView's VPVR shows.
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        return _normalize(df)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a yfinance frame to lower-case OHLCV columns, dropping junk rows."""
    if df is None or df.empty:
        return pd.DataFrame(columns=OHLCV)

    # yfinance may return a MultiIndex on columns (e.g. for a single ticker in
    # newer versions). Flatten to the price field.
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns={c: c.lower() for c in df.columns})
    keep = [c for c in OHLCV if c in df.columns]
    df = df[keep].copy()

    df = df.dropna(subset=[c for c in ["high", "low", "close"] if c in df.columns])
    if "volume" in df.columns:
        df["volume"] = df["volume"].fillna(0.0)   # NaN volume -> 0, never NaN downstream
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def make_provider(cfg: Config) -> BarProvider:
    name = (cfg.get("data.provider") or "yfinance").lower()
    if name == "yfinance":
        return YFinanceProvider()
    raise ValueError(f"Unknown data provider: {name!r}")


class DataFeed:
    """Provider + disk cache. This is what the rest of the app uses."""

    def __init__(self, cfg: Config, provider: BarProvider | None = None):
        self.cfg = cfg
        self.provider = provider or make_provider(cfg)
        self.timeframe = cfg.get("data.timeframe", "1h")
        self.lookback_days = int(cfg.get("data.lookback_days", 60))
        self.cache_dir = Path(cfg.get("data.cache_dir", "data"))
        self.max_age_hours = float(cfg.get("data.cache_max_age_hours", 12))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, ticker: str) -> Path:
        # Pickle keeps the DatetimeIndex and dtypes intact with no extra deps
        # (parquet would need pyarrow/fastparquet installed).
        safe = ticker.replace("/", "_").replace("\\", "_")
        return self.cache_dir / f"{safe}_{self.timeframe}_{self.lookback_days}d.pkl"

    def _is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        return age_hours <= self.max_age_hours

    def get_bars(self, ticker: str, force_refresh: bool = False) -> pd.DataFrame:
        path = self._cache_path(ticker)

        if not force_refresh and self._is_fresh(path):
            try:
                return pd.read_pickle(path)
            except Exception:  # noqa: BLE001 - corrupt cache -> refetch
                pass

        df = self.provider.fetch(ticker, self.timeframe, self.lookback_days)
        if not df.empty:
            try:
                df.to_pickle(path)
            except Exception as exc:  # noqa: BLE001 - cache write shouldn't be fatal
                print(f"[data] cache write failed for {ticker} ({exc})")
        return df


# ── Extended-hours spot quotes ──────────────────────────────────────────────────

def _last_price(df: pd.DataFrame, ticker: str, single: bool) -> float | None:
    """Most recent finite close for `ticker` out of a (possibly MultiIndex) frame."""
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if ticker not in df.columns.get_level_values(0):
                return None
            series = df[ticker]["Close"]
        elif single:
            series = df["Close"]
        else:
            return None
        series = series.dropna()
        if series.empty:
            return None
        price = float(series.iloc[-1])
        return price if math.isfinite(price) and price > 0 else None
    except Exception:  # noqa: BLE001 - a malformed column set is just "no quote"
        return None


def fetch_quotes(tickers: list[str], cfg: Config) -> dict[str, float]:
    """Latest traded price per ticker, INCLUDING pre/post-market prints.

    Deliberately separate from the bar fetch above, and deliberately never cached
    into it. Extended-hours prints are thin and frequently outside the regular
    session's range, so folding them into the volume profile would widen its price
    range, redistribute every bin, and drag the LVN/HVN percentile thresholds —
    manufacturing "air pockets" out of hours when nobody was trading. The profile
    stays regular-session-only; this moves the *spot* against it and nothing else.

    Best-effort throughout: a ticker missing from the result simply keeps its
    previous price. Returns {ticker: price} for whatever resolved."""
    if not tickers:
        return {}
    import yfinance as yf

    out: dict[str, float] = {}
    chunk = max(1, int(cfg.get("web.reprice_chunk", 100)))

    for start in range(0, len(tickers), chunk):
        batch = tickers[start:start + chunk]
        try:
            df = yf.download(
                " ".join(batch),
                period="1d",
                interval="1m",
                prepost=True,        # the whole point: pre-market / after-hours prints
                auto_adjust=True,    # matches the adjusted bars the profile was built on
                progress=False,
                threads=False,
                group_by="ticker",
            )
        except Exception as exc:  # noqa: BLE001 - one bad batch shouldn't kill the pass
            print(f"[quotes] batch of {len(batch)} failed: {exc}")
            continue
        if df is None or df.empty:
            continue
        for ticker in batch:
            price = _last_price(df, ticker, single=len(batch) == 1)
            if price is not None:
                out[ticker] = price

    print(f"[quotes] resolved {len(out)}/{len(tickers)} extended-hours prices")
    return out


# ── Next earnings date ──────────────────────────────────────────────────────────

def _earnings_cache_path(cfg: Config, ticker: str) -> Path:
    d = Path(cfg.get("data.cache_dir", "data")) / "earnings"
    d.mkdir(parents=True, exist_ok=True)
    safe = ticker.replace("/", "_").replace("\\", "_")
    return d / f"{safe}.json"


def _coerce_dates(raw) -> list[date]:
    """Normalize whatever yfinance's calendar returns (list/scalar of
    date/datetime/Timestamp/str) into a list of dates."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[date] = []
    for it in items:
        try:
            if isinstance(it, datetime):
                out.append(it.date())
            elif isinstance(it, date):
                out.append(it)
            else:
                out.append(pd.Timestamp(it).date())
        except Exception:  # noqa: BLE001 - skip unparseable entries
            continue
    return out


def fetch_next_earnings(ticker: str, cfg: Config) -> str | None:
    """ISO date (YYYY-MM-DD) of the next scheduled earnings report, or None.

    Best-effort: cached for earnings.cache_hours (dates rarely move), one
    yfinance calendar call on a miss, and None on any failure — the screen
    never depends on it."""
    path = _earnings_cache_path(cfg, ticker)
    max_age_h = float(cfg.get("earnings.cache_hours", 48))
    if path.exists() and (time.time() - path.stat().st_mtime) / 3600.0 <= max_age_h:
        try:
            return json.loads(path.read_text()).get("date")
        except Exception:  # noqa: BLE001 - corrupt cache -> refetch
            pass

    found: str | None = None
    try:
        import yfinance as yf

        cal = yf.Ticker(ticker).calendar
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw is None and cal is not None and hasattr(cal, "loc"):
            try:  # older yfinance returned a DataFrame
                raw = list(cal.loc["Earnings Date"])
            except Exception:  # noqa: BLE001
                raw = None
        upcoming = sorted(d for d in _coerce_dates(raw) if d >= date.today())
        found = upcoming[0].isoformat() if upcoming else None
    except Exception:  # noqa: BLE001 - blocked endpoint / no calendar -> None
        return None    # don't cache hard failures; retry next run

    try:
        path.write_text(json.dumps({"date": found, "ts": time.time()}))
    except Exception:  # noqa: BLE001
        pass
    return found
