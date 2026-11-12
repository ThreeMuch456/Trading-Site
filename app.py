"""
Trading Desk — unified aggregator (Phase 0 shell).

Six pages off one base template, rendered from `desk.sample_data.snapshot()`.
This is the mockup turned into a real, navigable Flask app — still fed by sample
data. Phase 1+ swaps `snapshot()` for live adapters (see BUILD-PLAN.md) without
touching the templates.

Run:   python app.py        # http://localhost:8000  (override with PORT)
"""

import os
from pathlib import Path

from flask import Flask, render_template

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

from desk.fetch import get_snapshot
from desk.schedule import start as start_scheduler

app = Flask(__name__)

# Optional cadence refresh (DESK_SCHEDULER=1). No-op otherwise. Started at import
# so it runs under gunicorn too; keep the service at one worker when enabled.
_scheduler = start_scheduler()

SITE_NAME = os.environ.get("SITE_NAME", "Trading Desk")

# (endpoint, nav label) — order is the nav order; `active` is derived from request.endpoint.
NAV_ITEMS = [
    ("index", "Overview"),
    ("gameplan", "Gameplan"),
    ("intraday", "Intraday"),
    ("flow", "Flow"),
    ("structure", "Structure"),
    ("eod", "EOD"),
]


@app.context_processor
def inject_globals():
    # Shared by every template: site name, nav, and the current snapshot.
    # get_snapshot() is cached (DESK_CACHE_TTL) and fails soft to sample data.
    return {"site_name": SITE_NAME, "nav_items": NAV_ITEMS, "s": get_snapshot()}


@app.route("/")
def index():
    return render_template("overview.html")


@app.route("/gameplan")
def gameplan():
    return render_template("gameplan.html")


@app.route("/intraday")
def intraday():
    return render_template("intraday.html")


@app.route("/flow")
def flow():
    return render_template("flow.html")


@app.route("/structure")
def structure():
    return render_template("structure.html")


@app.route("/eod")
def eod():
    return render_template("eod.html")


@app.route("/healthz")
def healthz():
    s = get_snapshot()
    return {
        "status": "ok",
        "live": s.get("live", False),
        "sources_degraded": s.get("degraded", {}),
    }, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
