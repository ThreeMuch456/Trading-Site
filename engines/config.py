"""Engine-layer configuration — shared by every vendored tool now running inside
the desk process. Kept separate from ``desk/config.py`` (the view/compose layer) so
the engines stay a self-contained, one-way dependency: desk -> engines, never back.

Everything is env-driven with safe defaults. The single most important variable is
``MASSIVE_API_KEY``: with it unset the engines cannot fetch, every source reports
itself unavailable, and the desk renders sample data (exactly the old no-URLs path).
"""

import os
from datetime import time as _time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "SPY,QQQ,IWM").split(",") if t.strip()]

# --- Massive API (Polygon-compatible, T-15 delayed on Starter/Developer tiers) ---
MASSIVE_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()
# Requests allowed per rolling minute, account-wide and process-wide. ONE shared
# limiter now governs every engine — the whole reason to merge into one process is
# a single, honest view of the account's request budget. Starter answers ~5/min.
MASSIVE_RPM = int(os.getenv("MASSIVE_RPM", "5"))
# Seconds per HTTP request to Massive before giving up.
HTTP_TIMEOUT = float(os.getenv("MASSIVE_HTTP_TIMEOUT", "20"))
# Slots held back from best-effort (non-critical) calls so a burst of print fetches
# can never delay a chain snapshot. With one shared budget across engines, default
# to holding back all but a couple of slots for the critical path.
CRITICAL_RESERVE = int(os.getenv("MASSIVE_CRITICAL_RESERVE", str(max(1, MASSIVE_RPM - 2))))
# A non-critical call that cannot get a slot this quickly is dropped for this cycle
# rather than queued — late prints are worth less than a timely chain snapshot.
NONCRITICAL_MAX_WAIT_S = float(os.getenv("MASSIVE_NONCRITICAL_MAX_WAIT_S", "10"))
# HTTP 403 from Massive means either "plan lacks this" or a transient quota refusal;
# require this many consecutive 403s on the stock-aggs probe before believing it.
AGGS_403_VERDICT_STREAK = int(os.getenv("AGGS_403_VERDICT_STREAK", "5"))

# --- Other provider keys (used by the agent/structure/stock engines) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()

# --- Session clock (ET) ---
SESSION_OPEN = _time(9, 30)
SESSION_CLOSE = _time(16, 0)

# --- Model constants ---
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.04"))

# --- Storage ---
# One Railway volume holds every engine's SQLite file. Each engine keeps its own
# schema in its own file under this directory (lowest-risk port — no cross-tool
# schema merge). DESK_DB_PATH (the compose-layer journal) lives here too.
#
# Resolution order: DESK_DATA_DIR (explicit) → RAILWAY_VOLUME_MOUNT_PATH (the
# mounted volume Railway auto-injects) → ./data (local dev). Honouring the Railway
# path directly means attaching a volume is enough to land EVERY engine's state on
# it — without it, flow/zerodte/agent would write to the ephemeral image dir while
# the vendored structure engine (which reads RAILWAY_VOLUME_MOUNT_PATH on its own)
# wrote to the volume, silently splitting the data across a redeploy boundary.
DATA_DIR = (os.getenv("DESK_DATA_DIR")
            or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
            or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))


def db_path(filename: str) -> str:
    """Absolute path to an engine's SQLite file inside the shared data dir."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, filename)


def has_massive() -> bool:
    return bool(MASSIVE_API_KEY)
