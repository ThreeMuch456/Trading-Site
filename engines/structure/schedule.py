"""Market-timezone scheduling helpers, shared by the web and the screen job.

Centralised here so the daily-refresh time is computed identically whether the
in-process scheduler or an external cron drives the screen.

Two daily wakes:
  - the EOD screen        (web.daily_refresh_hour, default 17:00 ET) — the full
    rebuild, and the only one that feeds the learning log;
  - the pre-open re-price (web.reprice_hour/minute, default 09:15 ET) — see
    src/reprice.py; refreshes spot against the same profiles.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .config import Config

_FMT_RUN  = "%b %d, %Y · %I:%M %p %Z"
_FMT_NEXT = "%b %d · %I:%M %p %Z"


def market_tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.get("web.refresh_timezone", "America/New_York"))


# ── NYSE holiday calendar ───────────────────────────────────────────────────────
# Hand-rolled rather than pulling in pandas_market_calendars/exchange_calendars:
# the rules below are stable and this keeps the deploy dependency-light. Only
# *full* closures matter here — half days (1pm close) still open at 9:30 and are
# long shut by 17:00, so both scheduled runs behave correctly on them.


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` (Mon=0) of a month."""
    last = (date(year, 12, 31) if month == 12
            else date(year, month + 1, 1) - timedelta(days=1))
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """NYSE observance: Saturday -> preceding Friday, Sunday -> following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def market_holidays(year: int) -> frozenset[date]:
    """Full NYSE closures for `year`."""
    days: set[date] = set()

    # New Year's Day is the one exception to the observance rule: when Jan 1
    # falls on a Saturday the NYSE does *not* close the preceding Friday.
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        days.add(_observed(new_year))

    days.add(_nth_weekday(year, 1, 0, 3))          # MLK Jr. Day — 3rd Mon in Jan
    days.add(_nth_weekday(year, 2, 0, 3))          # Washington's Birthday — 3rd Mon in Feb
    days.add(_easter(year) - timedelta(days=2))    # Good Friday
    days.add(_last_weekday(year, 5, 0))            # Memorial Day — last Mon in May
    if year >= 2022:                               # Juneteenth — first NYSE closure 2022
        days.add(_observed(date(year, 6, 19)))
    days.add(_observed(date(year, 7, 4)))          # Independence Day
    days.add(_nth_weekday(year, 9, 0, 1))          # Labor Day — 1st Mon in Sep
    days.add(_nth_weekday(year, 11, 3, 4))         # Thanksgiving — 4th Thu in Nov
    days.add(_observed(date(year, 12, 25)))        # Christmas
    return frozenset(days)


def is_trading_day(d: date) -> bool:
    """Weekday and not a full NYSE closure.

    Ad-hoc closures (state funerals, hurricanes) aren't predictable and aren't
    modelled — a run on one of those wastes a fetch but breaks nothing."""
    return d.weekday() < 5 and d not in market_holidays(d.year)


# ── Next-run times ──────────────────────────────────────────────────────────────

def _next_at(tz: ZoneInfo, hour: int, minute: int = 0) -> datetime:
    """Next trading-day occurrence of hour:minute in `tz`.

    Wall-clock arithmetic on an aware datetime keeps the same local time across
    DST boundaries, which is what we want: 17:00 ET stays 17:00 ET."""
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while not is_trading_day(target.date()):
        target += timedelta(days=1)
    return target


def next_refresh(cfg: Config, tz: ZoneInfo | None = None) -> datetime:
    """Next trading-day occurrence of the configured refresh hour, in market tz.
    Skips weekends *and* market holidays — there's no new EOD data after either."""
    tz = tz or market_tz(cfg)
    return _next_at(tz, int(cfg.get("web.daily_refresh_hour", 17)))


def next_reprice(cfg: Config, tz: ZoneInfo | None = None) -> datetime:
    """Next trading-day pre-open re-price time (default 09:15 ET)."""
    tz = tz or market_tz(cfg)
    return _next_at(tz,
                    int(cfg.get("web.reprice_hour", 9)),
                    int(cfg.get("web.reprice_minute", 15)))


def now_str(cfg: Config, tz: ZoneInfo | None = None) -> str:
    tz = tz or market_tz(cfg)
    return datetime.now(tz).strftime(_FMT_RUN)


def next_str(cfg: Config, tz: ZoneInfo | None = None) -> str:
    tz = tz or market_tz(cfg)
    return next_refresh(cfg, tz).strftime(_FMT_NEXT)


def next_reprice_str(cfg: Config, tz: ZoneInfo | None = None) -> str:
    tz = tz or market_tz(cfg)
    return next_reprice(cfg, tz).strftime(_FMT_NEXT)
