"""
NYSE market calendar — holiday and early-close awareness for the morning brief.

Source: https://www.nyse.com/markets/hours-calendars (official yearly PDFs).
2026 dates verified against the ICE/NYSE 2026 Yearly Trading Calendar PDF; 2027
follows NYSE's published observance rules (Sat holiday -> observed Fri, Sun -> Mon).

Regular session (RTH) is 9:30 AM – 4:00 PM ET, Mon–Fri. Early closes halt at
1:00 PM ET. From 2026-12-06 the tape runs 23/5 around that core session — see
EXTENDED_HOURS_LIVE, trading_day() and session_phase() below.

To extend: when NYSE publishes the next year's calendar, add its full closures to
HOLIDAYS and its 1pm early closes to EARLY_CLOSES, then append the year to KNOWN_YEARS.
"""

from datetime import date, datetime, timedelta, timezone

MARKET_TZ = "America/New_York"

# --- 23/5 extended hours ----------------------------------------------------
#
# From 2026-12-06 the SIPs, Nasdaq and NYSE Arca run a 23-hour, five-day week:
#
#   21:00–04:00 ET   night session
#   04:00–09:30 ET   pre-market
#   09:30–16:00 ET   regular session (RTH) — auctions and official close
#   16:00–20:00 ET   post-market
#   20:00–21:00 ET   dark: maintenance and next-day corporate actions
#
# The whole 21:00 -> 20:00 window is ONE trading day, dated futures-style:
# prints between 21:00 and midnight ET belong to the NEXT calendar date, so
# Monday's session opens Sunday at 21:00 ET. The trading date therefore can no
# longer be read off the calendar even on the market's own clock — the same
# class of bug market_today() exists to fix, with the boundary at 21:00 ET
# instead of UTC midnight. Callers asking "which session is this?" want
# trading_day(); see its docstring.
#
# RTH, the opening and closing auctions and the official closing price are all
# unchanged, which is why grading still scores the 09:30–16:00 window only
# (see grade_outcomes.fetch_daily_bar).
#
# The week runs Sunday 21:00 ET -> Friday 20:00 ET, so Friday and Saturday
# nights are dark, as is the night leading into a holiday.
EXTENDED_HOURS_LIVE = date(2026, 12, 6)

NIGHT_OPEN_HOUR  = 21   # the next trading day's tape begins
NIGHT_CLOSE_HOUR = 4    # night session ends, pre-market begins
DAY_CLOSE_HOUR   = 20   # trading day ends; 20:00–21:00 is dark

RTH_OPEN_MIN    = 9 * 60 + 30
RTH_CLOSE_MIN   = 16 * 60
EARLY_CLOSE_MIN = 13 * 60


def _in_us_dst(d: date) -> bool:
    """US daylight saving: 2nd Sunday in March through 1st Sunday in November."""
    def _nth_sunday(year: int, month: int, n: int) -> date:
        first = date(year, month, 1)
        first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
        return first_sunday + timedelta(weeks=n - 1)
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def _market_now() -> datetime:
    """Current time on the market's own clock (US/Eastern)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(MARKET_TZ))
    except Exception:
        # No tz database in the image — fall back to the DST rule above.
        utc = datetime.now(timezone.utc)
        return utc + timedelta(hours=-4 if _in_us_dst(utc.date()) else -5)


def market_today() -> date:
    """Today's calendar date on the market's own clock (US/Eastern).

    Deliberately NOT date.today(). The deploy runs on UTC (Railway's default,
    which is why the scheduler has to name its timezone explicitly), and UTC
    rolls over at 8:00 PM ET. Any "what day is it?" question answered from the
    server clock is therefore wrong for the last four hours of every trading
    day: the dashboard reports a weekend closure while Friday evening is still
    the session in play, the options chain filters out the front expiry, and a
    brief generated in that window gets logged under tomorrow's date.

    This is the market's *calendar* date, which is not always the date the tape
    is writing to: under 23/5 the two diverge every evening from 21:00 ET.
    Callers that mean "which trading session is this?" want trading_day().
    Wall-clock timestamps (fetched_at, generated_at) stay on datetime.now().
    """
    return _market_now().date()


def extended_hours_live(d: date | None = None) -> bool:
    """Whether the 23/5 schedule is in effect on `d`."""
    return (d or market_today()) >= EXTENDED_HOURS_LIVE


def is_session_day(d: date) -> bool:
    """True when `d` is a trading date — not a weekend, not a full closure.

    Early closes are trading dates; only HOLIDAYS and weekends are not. Years
    outside KNOWN_YEARS fall back to the weekday rule, same caveat as
    market_status's calendar_known flag.
    """
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def next_session_day(after: date) -> date:
    """The first trading date strictly after `after`."""
    d = after + timedelta(days=1)
    for _ in range(10):   # longest run of consecutive US closures is < 10 days
        if is_session_day(d):
            return d
        d += timedelta(days=1)
    return d


def trading_day(now: datetime | None = None) -> date:
    """The trading date the tape is currently writing to.

    Under 23/5 the trading day rolls at 21:00 ET, not at midnight: a print at
    22:00 Monday is Tuesday's tape, and Monday's session opened Sunday at
    21:00. Reading the date off the calendar — even the market's own calendar —
    is therefore wrong for the last three hours of every evening, which is the
    same bug market_today() was written to fix one boundary earlier.

    The roll only happens when there is a session to roll into: Friday and
    Saturday nights and the night before a holiday are dark, and the trading
    date stays on the calendar date through them.

    Before EXTENDED_HOURS_LIVE this is exactly market_today(), so the change is
    inert until the schedule actually starts.
    """
    now = now or _market_now()
    today = now.date()
    if not extended_hours_live(today) or now.hour < NIGHT_OPEN_HOUR:
        return today
    nxt = today + timedelta(days=1)
    return nxt if is_session_day(nxt) else today


def session_phase(now: datetime | None = None) -> str:
    """Where the clock sits in the trading day.

    One of 'regular' | 'premarket' | 'postmarket' | 'night' | 'maintenance' |
    'closed'. Before EXTENDED_HOURS_LIVE only 'regular' and 'closed' are
    returned: without a 23/5 schedule there is no session to be in outside RTH.
    """
    now = now or _market_now()
    day = trading_day(now)
    if not is_session_day(day):
        return "closed"

    minute = now.hour * 60 + now.minute
    rth_close = EARLY_CLOSE_MIN if day.isoformat() in EARLY_CLOSES else RTH_CLOSE_MIN
    if RTH_OPEN_MIN <= minute < rth_close:
        return "regular"
    if not extended_hours_live(now.date()):
        return "closed"

    if minute >= NIGHT_OPEN_HOUR * 60:
        # trading_day() rolls the date only when a night session actually
        # opened, so an unrolled date here means this evening is dark.
        return "night" if day != now.date() else "closed"
    if minute < NIGHT_CLOSE_HOUR * 60:
        return "night"
    if minute < RTH_OPEN_MIN:
        return "premarket"
    if minute < DAY_CLOSE_HOUR * 60:
        # Whether the exchanges run a full post-market on 1pm early-close days
        # is not published yet; treated as a normal post-market until it is.
        return "postmarket"
    return "maintenance"


# yfinance's ten_day_average_volume window — the baseline the brief reads
# today's volume against.
VOLUME_BASELINE_SESSIONS = 10


def first_extended_session() -> date:
    """The first trading date run on the 23/5 schedule."""
    d = EXTENDED_HOURS_LIVE
    return d if is_session_day(d) else next_session_day(d)


def volume_baseline_note(d: date | None = None) -> str | None:
    """Warning for the stretch where average-volume comparisons straddle 23/5.

    Overnight prints count toward consolidated volume, so from the first 23/5
    session the daily figure steps up against a trailing average still made
    mostly of regular-hours-only days. For about two weeks "volume vs the
    10-day average" therefore reads high for a reason that has nothing to do
    with conviction — which is the exact inference the brief draws from it.

    None once the baseline has fully rolled over, and before launch.
    """
    d = d or trading_day()
    if not extended_hours_live(d):
        return None

    first = first_extended_session()
    # Cheap bail-out so this never walks a long date range in normal operation.
    if d > first + timedelta(days=40):
        return None

    # A brief written on the launch weekend previews the first 23/5 session, so
    # count from that session rather than reporting a meaningless zero.
    anchor = max(d, first)
    elapsed = sum(1 for n in range((anchor - first).days + 1)
                  if is_session_day(first + timedelta(days=n)))
    if elapsed > VOLUME_BASELINE_SESSIONS:
        return None
    return (
        f"Volume baselines are in transition: {elapsed} of the trailing "
        f"{VOLUME_BASELINE_SESSIONS} sessions have run on the 23/5 schedule, which "
        f"counts overnight prints toward consolidated volume. Today's volume is "
        f"therefore measured against an average built mostly from regular-hours-only "
        f"days, and will read high for that reason alone. Do not treat volume as "
        f"confirming or questioning a move until the baseline has rolled over."
    )


# Full market closures (holidays): ISO date -> name
HOLIDAYS = {
    # 2025
    "2025-01-01": "New Year's Day",
    "2025-01-09": "National Day of Mourning (Pres. Carter)",
    "2025-01-20": "Martin Luther King Jr. Day",
    "2025-02-17": "Washington's Birthday",
    "2025-04-18": "Good Friday",
    "2025-05-26": "Memorial Day",
    "2025-06-19": "Juneteenth",
    "2025-07-04": "Independence Day",
    "2025-09-01": "Labor Day",
    "2025-11-27": "Thanksgiving Day",
    "2025-12-25": "Christmas Day",
    # 2026  (verified against NYSE 2026 PDF)
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    # 2027
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}

# 1:00 PM ET early closes: ISO date -> name
EARLY_CLOSES = {
    # 2025
    "2025-07-03": "Day before Independence Day",
    "2025-11-28": "Day after Thanksgiving",
    "2025-12-24": "Christmas Eve",
    # 2026
    "2026-11-27": "Day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    # 2027
    "2027-11-26": "Day after Thanksgiving",
}

KNOWN_YEARS = (2025, 2026, 2027)

# FOMC policy statement / rate decision dates (final day of each 2-day meeting).
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#
# NOT sourced from FRED's bulk release-dates API: FRED release_id 101 ("FOMC Press
# Release") returns a release-date entry for essentially every single calendar day
# from Dec 2020 onward (a real-time-tracking artifact, not actual meeting dates) —
# querying it naively makes every brief report "FOMC today". Hardcoding the Fed's
# own published schedule avoids that entirely, same rationale as the NYSE holidays
# above. Update when the Fed publishes the next year's calendar.
FOMC_DATES = {
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
}


def upcoming_fomc_dates(start: date | None = None, days: int = 14) -> list[dict]:
    """FOMC policy decision dates within the next `days` days (inclusive of today)."""
    start = start or trading_day()
    out = []
    for offset in range(days + 1):
        d = start + timedelta(days=offset)
        if d.isoformat() in FOMC_DATES:
            out.append({"date": d.isoformat(), "event": "FOMC Press Release",
                       "days_away": offset})
    return out


def extended_hours_window(d: date) -> dict | None:
    """The 23/5 sessions wrapped around `d`'s regular session, or None when the
    schedule is not in effect (before launch, or a day with no session at all).

    `opens` is the previous session day's evening, since that is when `d`'s tape
    actually starts — the detail that makes a Sunday-evening "market closed"
    banner wrong once the schedule is live.
    """
    if not extended_hours_live(d) or not is_session_day(d):
        return None
    rth_close = "13:00" if d.isoformat() in EARLY_CLOSES else "16:00"
    return {
        "night":       "21:00–04:00 ET",
        "pre_market":  "04:00–09:30 ET",
        "regular":     f"09:30–{rth_close} ET",
        "post_market": f"{rth_close}–20:00 ET",
        "dark":        "20:00–21:00 ET",
        "tape_opens":  "21:00 ET the previous session day",
    }


def market_status(d: date | None = None) -> dict:
    """Return the current (or a given day's) NYSE session status.

    status is one of: 'open' | 'closed' | 'early_close', and describes the
    *regular* session — RTH and the official close are unchanged under 23/5, so
    a holiday is still a full closure and an early close still halts at 1pm ET.
    `extended_hours` carries the surrounding 23/5 windows once those are live.
    `calendar_known` is False for weekdays in years not yet in the table, so
    callers can flag the uncertainty.

    Defaults to trading_day(), not market_today(): from 21:00 ET the live tape
    belongs to the next session, and a dashboard asking "what is the market
    doing?" at 22:00 Sunday means Monday's night session, not the weekend.
    """
    d = d or trading_day()
    iso = d.isoformat()

    if d.weekday() >= 5:  # Saturday / Sunday
        base = {"date": iso, "status": "closed", "is_open": False,
                "reason": "Weekend", "close_time": None, "calendar_known": True}
    elif iso in HOLIDAYS:
        base = {"date": iso, "status": "closed", "is_open": False,
                "reason": HOLIDAYS[iso], "close_time": None, "calendar_known": True}
    elif iso in EARLY_CLOSES:
        base = {"date": iso, "status": "early_close", "is_open": True,
                "reason": EARLY_CLOSES[iso], "close_time": "13:00 ET",
                "calendar_known": True}
    else:
        base = {"date": iso, "status": "open", "is_open": True,
                "reason": None, "close_time": "16:00 ET",
                "calendar_known": d.year in KNOWN_YEARS}

    return {**base, "extended_hours": extended_hours_window(d)}


def upcoming_closures(start: date | None = None, days: int = 14) -> list[dict]:
    """Holidays and early closes within the next `days` days (inclusive of today)."""
    start = start or trading_day()
    out = []
    for offset in range(days + 1):
        d = start + timedelta(days=offset)
        iso = d.isoformat()
        if iso in HOLIDAYS:
            out.append({"date": iso, "event": f"{HOLIDAYS[iso]} — market closed",
                        "type": "closed", "days_away": offset})
        elif iso in EARLY_CLOSES:
            out.append({"date": iso, "event": f"{EARLY_CLOSES[iso]} — 1pm ET close",
                        "type": "early_close", "days_away": offset})
    return out


if __name__ == "__main__":
    import json
    print(json.dumps({
        "calendar_date": market_today().isoformat(),
        "trading_day": trading_day().isoformat(),
        "phase": session_phase(),
        "today": market_status(),
        "upcoming": upcoming_closures(days=30),
    }, indent=2))
