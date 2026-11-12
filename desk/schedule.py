"""Optional background scheduler.

Off by default (set DESK_SCHEDULER=1 to enable). When on, it refreshes the
composed snapshot on the session cadence so the first visitor isn't slow, the
heavy quant fetch happens off the request path, and the EOD snapshot + grades are
recorded to the desk journal even on a day nobody opens the page.

Run exactly one instance (one worker) — the sibling apps document the same.
"""

import logging
import os

log = logging.getLogger("desk.schedule")


def enabled() -> bool:
    return os.environ.get("DESK_SCHEDULER", "").lower() in ("1", "true", "yes")


def start():
    if not enabled():
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:                      # dependency not installed
        log.warning("DESK_SCHEDULER set but APScheduler unavailable: %s", exc)
        return None

    from . import fetch, store

    tz = os.environ.get("DESK_TZ", "America/New_York")
    sched = BackgroundScheduler(timezone=tz)

    def refresh(tag: str):
        try:
            s = fetch.get_snapshot(force=True)
            store.record_snapshot(s)
            grades = (s.get("eod") or {}).get("grades")
            if grades and "agent" not in (s.get("degraded") or {}):
                store.ingest_grades(grades)
            log.info("desk %s refresh ok (live=%s, degraded=%s)",
                     tag, s.get("live"), list((s.get("degraded") or {}).keys()))
        except Exception:
            log.exception("desk %s refresh failed", tag)

    wk = {"day_of_week": "mon-fri"}
    sched.add_job(lambda: refresh("gameplan"), CronTrigger(hour=9, minute=35, **wk))
    sched.add_job(lambda: refresh("intraday"), CronTrigger(hour="10-15", minute="0,15,30,45", **wk))
    sched.add_job(lambda: refresh("eod"), CronTrigger(hour=16, minute=15, **wk))
    sched.start()
    log.info("desk scheduler started (tz=%s)", tz)
    return sched
