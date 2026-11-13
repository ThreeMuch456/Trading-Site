"""
Trading Desk — unified aggregator (Phase 0 shell).

Six pages off one base template, rendered from `desk.sample_data.snapshot()`.
This is the mockup turned into a real, navigable Flask app — still fed by sample
data. Phase 1+ swaps `snapshot()` for live adapters (see BUILD-PLAN.md) without
touching the templates.

Run:   python app.py        # http://localhost:8000  (override with PORT)
"""

import os
import re
from pathlib import Path

from flask import Flask, render_template, request, g

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


@app.before_request
def _gate():
    """Optional shared-secret gate. Unset APP_TOKEN = fully open (right for local).
    Set it on a public deploy and every page requires the token as ?token=…, an
    X-App-Token header, or the cookie set on first query-param auth (so links stay
    clean afterwards). /healthz and /static stay open for health checks + assets."""
    token = os.environ.get("APP_TOKEN")
    if not token or request.endpoint in ("healthz", "static"):
        return None
    supplied = (request.args.get("token")
                or request.headers.get("X-App-Token", "")
                or request.cookies.get("desk_token", ""))
    if supplied != token:
        return render_template(
            "error.html", code=401,
            message="This desk is private. Append ?token=… to your link to sign in."), 401
    g.set_desk_cookie = request.args.get("token") == token
    return None


@app.errorhandler(404)
def _not_found(_e):
    return render_template("error.html", code=404, message="That page isn't part of the desk."), 404


@app.errorhandler(500)
def _server_error(_e):
    return render_template("error.html", code=500,
                           message="Something went wrong composing the desk. Try again shortly."), 500


@app.after_request
def _persist_token(resp):
    if getattr(g, "set_desk_cookie", False):
        resp.set_cookie("desk_token", os.environ.get("APP_TOKEN", ""),
                        httponly=True, samesite="Lax", secure=request.is_secure)
    return resp


@app.context_processor
def inject_globals():
    # Shared by every template: site name, nav, and the current snapshot.
    # get_snapshot() is cached (DESK_CACHE_TTL) and fails soft to sample data; the
    # try/except is a last resort so a context-processor error can't break even the
    # error page itself.
    try:
        snap = get_snapshot()
    except Exception:
        snap = {"mock": True, "degraded": {}, "meta": "",
                "session": {"phase": "—", "phase_class": "closed", "clock": "", "date_label": ""}}
    return {"site_name": SITE_NAME, "nav_items": NAV_ITEMS, "s": snap}


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


@app.route("/flow/all")
def flow_all():
    """All-ticker unusual activity (not the index ETFs). Live when FLOW_URL is set,
    else the sample rows — a fetch failure degrades to sample with a note."""
    rows, note = _drilldown(lambda src, cfg, to: src.fetch_sweeps(cfg, to),
                            fallback_key="all", fallback_default=[])
    return render_template("flow_all.html", rows=rows, flow_note=note)


@app.route("/flow/ticker/<sym>")
def flow_ticker(sym):
    sym = re.sub(r"[^A-Za-z0-9.\-]", "", sym).upper()[:10] or "SPY"
    data, note = _drilldown(lambda src, cfg, to: src.fetch_ticker(cfg, sym, to),
                            fallback_key="sample_ticker", fallback_default=None)
    if data and note:                       # sample fallback — label it with the asked symbol
        data = {**data, "sym": sym}
    return render_template("flow_ticker.html", tk=data, sym=sym, flow_note=note)


def _drilldown(call, fallback_key, fallback_default):
    """Run a live flow fetch when the source is configured; on absence or failure
    fall back to the sample snapshot's value. Returns (data, note)."""
    from desk import config
    from desk.sources import flow as flow_src
    if config.enabled("flow"):
        try:
            return call(flow_src, config.SOURCES["flow"], config.FETCH_TIMEOUT), None
        except Exception as exc:
            note = f"flow ({type(exc).__name__}) — showing sample"
            return (get_snapshot().get("flow", {}) or {}).get(fallback_key, fallback_default), note
    return (get_snapshot().get("flow", {}) or {}).get(fallback_key, fallback_default), None


@app.route("/structure")
def structure():
    return render_template("structure.html")


@app.route("/eod")
def eod():
    return render_template("eod.html")


@app.route("/healthz")
def healthz():
    s = get_snapshot()
    out = {
        "status": "ok",
        "live": s.get("live", False),
        "sources_degraded": s.get("degraded", {}),
    }
    # Surface the morning-brief generator's last-run state so a failed brief is
    # diagnosable from here rather than only from the deploy logs. The adapter can
    # only report "no brief generated yet" (the read side); the WRITE side's
    # stage-tagged error lives on the agent engine's run-state.
    try:
        from engines import agent as _agent
        rs = _agent._run_state
        out["agent_brief"] = {"status": rs.get("status"),
                              "detail": rs.get("error") or rs.get("message") or None,
                              "last_run": rs.get("last_run")}
    except Exception:
        pass
    return out, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
