"""Shared Massive API client — synchronous, process-wide, rate-limited.

Massive (https://massive.com) is a Polygon.io-compatible REST API; auth is a Bearer
token, the feed is 15-minute delayed on Starter/Developer tiers. This is the single
client every vendored engine fetches through, so the account's per-minute request
budget is governed by ONE shared sliding-window limiter — the central payoff of
merging the tools into one process (previously each service throttled blind to the
others and they collided on the same account quota).

Synchronous by design: the desk is a Flask + APScheduler process with sync workers,
so there is no event loop to host 0DTE's original async client. The rate limiter,
retry/backoff, entitlement probing and non-finite-float scrubbing are ported from
``0DTE-Assistant/app/sources/massive.py`` (the most battle-tested of the two upstream
clients); the OFS normalizers live with their own engine so field expectations can't
drift. This module owns transport + pagination + the naming-stable helpers
(stock snapshot, minute bars, trade prints, contract reference) only.
"""

import logging
import math
import threading
import time
from datetime import date, datetime, timezone

import requests

from . import config

log = logging.getLogger("engines.massive")


class MassiveError(RuntimeError):
    def __init__(self, msg, status: int | None = None):
        super().__init__(msg)
        self.status = status


class MassiveBudgetError(MassiveError):
    """A best-effort call was dropped to protect the critical path — not a source
    fault and not an entitlement verdict; the caller should degrade, not retry."""


def finite(v):
    """Coerce to a finite float, else None. Python's JSON parser ACCEPTS the
    non-standard NaN/Infinity literals, so a failed IV solve on an illiquid strike
    arrives as float('nan'); left alone it poisons every downstream aggregate and
    finally 500s a JSON response. A greek that is not a real number is a MISSING
    greek, which every engine already knows how to skip."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class _RateLimiter:
    """Sliding-window limiter shared by every Massive call in the process. The quota
    is per account, so the window spans all engines. Throttling is invisible from the
    outside — a starved call doesn't error, it arrives late — so the counters below
    make the distinction between "slow feed" and "budget saturated" observable."""

    def __init__(self, per_minute: int):
        self.per_minute = max(per_minute, 1)
        self._hits: list[float] = []
        self._lock = threading.Lock()
        self.requests = 0
        self.throttled = 0
        self.total_wait_s = 0.0
        self.max_wait_s = 0.0
        self.shed = 0

    def acquire(self, critical: bool = True) -> bool:
        """Take a slot. Critical calls (chain snapshots, expiry discovery) wait as
        long as needed. Best-effort calls (trade prints) may only use slots above the
        reserve and are DROPPED rather than queued once they would wait past the
        ceiling — a print landing two minutes late is worth less than the chain
        snapshot it delayed."""
        ceiling = self.per_minute if critical else max(
            self.per_minute - config.CRITICAL_RESERVE, 0)
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._hits = [h for h in self._hits if now - h < 60]
                if len(self._hits) < ceiling:
                    self._hits.append(now)
                    self.requests += 1
                    if waited > 0:
                        self.throttled += 1
                        self.total_wait_s += waited
                        self.max_wait_s = max(self.max_wait_s, waited)
                    return True
                if not critical and ceiling <= 0:
                    self.shed += 1
                    return False
                wait = 60 - (now - self._hits[0]) + 0.25
                if not critical and waited + wait > config.NONCRITICAL_MAX_WAIT_S:
                    self.shed += 1
                    return False
            waited += wait
            time.sleep(max(wait, 0.0))

    def stats(self) -> dict:
        return {
            "rpm_limit": self.per_minute,
            "critical_reserve": config.CRITICAL_RESERVE,
            "requests": self.requests,
            "throttled": self.throttled,
            "shed_best_effort": self.shed,
            "throttled_pct": round(100 * self.throttled / self.requests, 1) if self.requests else 0.0,
            "avg_wait_s": round(self.total_wait_s / self.throttled, 1) if self.throttled else 0.0,
            "max_wait_s": round(self.max_wait_s, 1),
        }


class MassiveClient:
    def __init__(self):
        self.base = config.MASSIVE_BASE_URL.rstrip("/")
        self.key = config.MASSIVE_API_KEY
        self._session = requests.Session()
        if self.key:
            self._session.headers.update({"Authorization": f"Bearer {self.key}",
                                          "Accept": "application/json"})
        self._limiter = _RateLimiter(config.MASSIVE_RPM)
        # Account-level entitlements (None = not yet probed). Probing once covers
        # every engine in the process.
        self.trades_entitled: bool | None = None
        self.aggs_entitled: bool | None = None
        self.aggs_403_streak = 0

    # ---- transport ----

    def rate_stats(self) -> dict:
        return self._limiter.stats()

    def _get(self, path: str, params: dict | None = None, critical: bool = True) -> dict:
        if not self.key:
            raise MassiveError("MASSIVE_API_KEY is not set", status=None)
        url = path if path.startswith("http") else f"{self.base}{path}"
        backoff = 2.0
        for attempt in range(4):
            if not self._limiter.acquire(critical=critical):
                raise MassiveBudgetError(
                    f"best-effort call to {path} shed to protect the critical path "
                    f"(request budget saturated)")
            try:
                r = self._session.get(url, params=params, timeout=config.HTTP_TIMEOUT)
            except requests.RequestException as e:
                if attempt == 3:
                    raise MassiveError(f"request to {path} failed: {e}") from e
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403, 404):
                raise MassiveError(f"HTTP {r.status_code} from {path}", status=r.status_code)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise MassiveError(f"HTTP {r.status_code} from {path} after retries",
                                       status=r.status_code)
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else backoff
                except ValueError:
                    delay = backoff
                time.sleep(min(delay, 30))
                backoff *= 2
                continue
            raise MassiveError(f"HTTP {r.status_code} from {path}: {r.text[:200]}",
                               status=r.status_code)
        raise MassiveError(f"exhausted retries for {path}")

    def get(self, path: str, params: dict | None = None, critical: bool = True) -> dict:
        """Public single-request GET (engines that page themselves use this)."""
        return self._get(path, params, critical=critical)

    def paginate(self, path: str, params: dict | None = None, max_pages: int = 20,
                 critical: bool = True):
        """Yield every ``results`` item across Massive's ``next_url`` pages. The
        cursor URL is absolute and already carries the query string, so params are
        sent only on the first page."""
        url = path
        for page in range(max_pages):
            data = self._get(url, params, critical=critical)
            params = None
            for item in (data.get("results") or []):
                yield item
            url = data.get("next_url")
            if not url:
                return
        log.warning("pagination truncated after %d pages for %s", max_pages, path)

    # ---- naming-stable helpers (shared across engines) ----

    @staticmethod
    def day_bar_date(ns) -> str | None:
        """ET trading date a `day` aggregate belongs to. Massive keeps serving the
        prior session's bar for contracts that have not traded yet today; this is how
        the two cases are separated (the bar's own ``last_updated`` ns timestamp)."""
        if not ns:
            return None
        try:
            dt = datetime.fromtimestamp(float(ns) / 1e9, timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
        return dt.astimezone(config.ET).strftime("%Y-%m-%d")

    def get_stock_snapshot(self, ticker: str) -> dict:
        """Normalized stock snapshot (price/volume) for one ticker."""
        body = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        snap = body.get("ticker") or body.get("results") or {}
        day = snap.get("day", {}) or {}
        prev = snap.get("prevDay", {}) or {}
        last_tr = snap.get("lastTrade", {}) or {}
        last_price = finite(last_tr.get("p")) or finite(day.get("c"))
        prev_close = finite(prev.get("c"))
        day_change_pct = round((last_price - prev_close) / prev_close * 100, 2) \
            if (last_price and prev_close) else None
        return {
            "symbol": snap.get("ticker"),
            "last_price": round(last_price, 2) if last_price else None,
            "prev_close": round(prev_close, 2) if prev_close else None,
            "day_change_pct": day_change_pct,
            "day_open": finite(day.get("o")), "day_close": finite(day.get("c")),
            "day_high": finite(day.get("h")), "day_low": finite(day.get("l")),
            "day_volume": int(day.get("v")) if day.get("v") else None,
        }

    def fetch_minute_bars(self, ticker: str, day: date) -> list[dict]:
        """1-minute stock aggregates (T-15 delayed). Stock aggs may be excluded from
        an options-only plan; callers treat MassiveError 401/403/404 as 'not entitled'
        and stop asking."""
        url = (f"/v2/aggs/ticker/{ticker}/range/1/minute/"
               f"{day.isoformat()}/{day.isoformat()}")
        data = self._get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
        out = []
        for r in (data.get("results") or []):
            t = r.get("t")
            if t is None or r.get("c") is None:
                continue
            dt = datetime.fromtimestamp(t / 1000, timezone.utc).astimezone(config.ET)
            bar = {"ts": dt.isoformat(), "o": finite(r.get("o")), "h": finite(r.get("h")),
                   "l": finite(r.get("l")), "c": finite(r.get("c")), "v": finite(r.get("v")) or 0.0}
            if bar["c"] is None:
                continue
            out.append(bar)
        return out

    def fetch_trades(self, option_ticker: str, since_ns: int, critical: bool = False) -> list[dict]:
        """Trade prints for one contract since a nanosecond timestamp. Best-effort by
        default so a routine print fetch never delays a chain snapshot."""
        params = {"timestamp.gte": since_ns, "limit": 1000, "sort": "timestamp", "order": "asc"}
        out = []
        for item in self.paginate(f"/v3/trades/{option_ticker}", params,
                                  max_pages=3, critical=critical):
            price, size = finite(item.get("price") or item.get("p")), finite(item.get("size") or item.get("s"))
            ts = item.get("sip_timestamp") or item.get("participant_timestamp") or item.get("timestamp") or item.get("t")
            if price is None or size is None or ts is None:
                continue
            out.append({"price": price, "size": size, "ts_ns": ts,
                        "conditions": item.get("conditions") or item.get("c")})
        return out

    def has_contracts_for_expiry(self, underlying: str, day: date) -> bool:
        data = self._get("/v3/reference/options/contracts", {
            "underlying_ticker": underlying, "expiration_date": day.isoformat(), "limit": 1})
        return bool(data.get("results"))

    def fetch_next_expiry(self, underlying: str, after: date) -> date | None:
        data = self._get("/v3/reference/options/contracts", {
            "underlying_ticker": underlying, "expiration_date.gt": after.isoformat(),
            "sort": "expiration_date", "order": "asc", "limit": 1})
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        exp = r.get("expiration_date") or (r.get("details") or {}).get("expiration_date")
        return date.fromisoformat(exp) if exp else None

    # ---- entitlement probes (one-shot, account-level) ----

    def probe_trades_entitlement(self, option_ticker: str, since_ns: int) -> bool | None:
        if self.trades_entitled is not None:
            return self.trades_entitled
        try:
            self.fetch_trades(option_ticker, since_ns, critical=True)
            self.trades_entitled = True
        except MassiveError as e:
            if e.status in (401, 403, 404):
                self.trades_entitled = False
            else:
                return None  # transient — leave undetermined
        return self.trades_entitled

    def probe_aggs_entitlement(self, ticker: str, day: date) -> bool | None:
        if self.aggs_entitled is not None:
            return self.aggs_entitled
        try:
            self.fetch_minute_bars(ticker, day)
        except MassiveError as e:
            if e.status == 401:
                self.aggs_entitled = False
                return False
            if e.status == 403:
                self.aggs_403_streak += 1
                if self.aggs_403_streak >= config.AGGS_403_VERDICT_STREAK:
                    self.aggs_entitled = False
                    return False
                return None
            return None
        except Exception:  # noqa: BLE001 — inconclusive, not a verdict
            return None
        self.aggs_403_streak = 0
        self.aggs_entitled = True
        return True


# ---- process-wide singleton (shared limiter lives here) ----

_client: MassiveClient | None = None
_client_lock = threading.Lock()


def client() -> MassiveClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = MassiveClient()
    return _client
