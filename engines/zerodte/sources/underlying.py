"""Underlying feed via yfinance — near-live 1-minute OHLCV.

This is the fast clock. It maintains the persistent 1-minute series the
two-clock rule needs: delayed option prints are always evaluated against the
bar matching their own timestamp, never against current spot.

yfinance caps 1-minute history at ~7 calendar days per request, so the
"20-day average volume by time of day" baseline is built from however many
days are actually available; the effective day count is reported, not hidden.
"""
import logging
from datetime import datetime, date, timedelta

from .. import config

log = logging.getLogger("underlying")


class UnderlyingError(Exception):
    pass


class NoSessionBars(UnderlyingError):
    """The feed answered, but carries no 1-minute bars for today's session.

    Before 09:30 this is the normal state, not a fault — it must not count
    toward the failure streak or trigger the delayed-bar fallback, or the
    backoff would already be minutes deep by the time the open arrives."""


class UnderlyingFeed:
    """Blocking yfinance calls — run via asyncio.to_thread."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.tod_volume_baseline: dict[str, float] = {}  # "HH:MM" -> avg volume
        self.tod_baseline_days = 0
        self.prior_close: float | None = None
        self.prior_day_bars: list[dict] = []  # ATR seed before today has 15 bars
        self.avg_daily_volume_20d: float | None = None

    def _yf(self):
        import yfinance as yf
        return yf.Ticker(self.ticker)

    def load_history(self):
        """Startup: prior close, 20d daily volume, time-of-day 1-min baseline."""
        tk = self._yf()
        daily = tk.history(period="1mo", interval="1d", auto_adjust=False)
        if daily is None or daily.empty:
            raise UnderlyingError(f"{self.ticker}: no daily history returned")
        today = datetime.now(config.ET).date()
        prior = daily[[d.date() < today for d in daily.index]]
        if not prior.empty:
            self.prior_close = float(prior["Close"].iloc[-1])
            tail = prior.tail(20)
            self.avg_daily_volume_20d = float(tail["Volume"].mean())

        m1 = tk.history(period="7d", interval="1m", auto_adjust=False)
        if m1 is None or m1.empty:
            log.warning("%s: no 7d 1-minute history; time-of-day volume "
                        "baseline unavailable", self.ticker)
            return
        m1 = m1.tz_convert(config.ET)
        sums: dict[str, list[float]] = {}
        days = set()
        for ts, row in m1.iterrows():
            if ts.date() >= today:
                continue
            days.add(ts.date())
            sums.setdefault(ts.strftime("%H:%M"), []).append(float(row["Volume"]))
        self.tod_volume_baseline = {k: sum(v) / len(v) for k, v in sums.items() if v}
        self.tod_baseline_days = len(days)
        if self.tod_baseline_days < 20:
            log.warning("%s: time-of-day volume baseline built from %d days "
                        "(20 requested; yfinance 1m history limit)",
                        self.ticker, self.tod_baseline_days)
        prior_days = sorted(days)
        if prior_days:
            last = prior_days[-1]
            self.prior_day_bars = [
                {"ts": ts.isoformat(), "o": float(r["Open"]), "h": float(r["High"]),
                 "l": float(r["Low"]), "c": float(r["Close"]), "v": float(r["Volume"])}
                for ts, r in m1.iterrows() if ts.date() == last]

    def poll(self) -> dict:
        """Today's 1-minute bars + spot. Raises on failure — caller fails loud.

        Asks for two days and filters to today rather than using period="1d",
        which is wrong at both ends of the session boundary: BEFORE the open it
        serves yesterday's session, which would be ingested as today's bars and
        leave spot sitting on yesterday's close; and the moment the session
        rolls at 09:30 it flips to an empty frame until Yahoo publishes the
        first bar. Filtering by date makes the date explicit instead of
        trusting the provider's window to mean what we want.
        """
        tk = self._yf()
        m1 = tk.history(period="2d", interval="1m", auto_adjust=False)
        if m1 is None or m1.empty:
            raise UnderlyingError(f"{self.ticker}: empty 1m response")
        m1 = m1.tz_convert(config.ET)
        today = datetime.now(config.ET).date()
        m1 = m1[[ts.date() == today for ts in m1.index]]
        if m1.empty:
            raise NoSessionBars(f"{self.ticker}: no 1m bars for {today} yet")
        bars = [{"ts": ts.isoformat(), "o": float(r["Open"]), "h": float(r["High"]),
                 "l": float(r["Low"]), "c": float(r["Close"]), "v": float(r["Volume"])}
                for ts, r in m1.iterrows()]
        last = m1.index[-1]
        return {
            "bars": bars,
            "spot": float(m1["Close"].iloc[-1]),
            "spot_ts": last.isoformat(),
            "session_high": float(m1["High"].max()),
            "session_low": float(m1["Low"].min()),
            "fetched_at": datetime.now(config.ET).isoformat(),
        }
