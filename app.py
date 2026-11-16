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

import socket as _socket
from datetime import datetime as _dt, timezone as _tz
_HOST = _socket.gethostname()
_STARTED_AT = _dt.now(_tz.utc).isoformat(timespec="seconds")


def _process_info() -> dict:
    """Which process answered, and how many web workers are actually running.

    The scheduler, the engine caches and the 0DTE live state all live in process
    memory and assume exactly ONE worker (see Procfile). With two, every scheduled
    job runs twice and each worker serves its own copy of that state. `workers`
    counts the processes sharing our parent (gunicorn's master), and the *_flag /
    env fields show where a wrong count is coming from. Linux-only detail (/proc);
    anywhere else it reports just the pid."""
    info = {"pid": os.getpid(), "host": _HOST, "started_at": _STARTED_AT}
    try:
        ppid = os.getppid()
        n = 0
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    # "pid (comm) state ppid ..." — comm may hold spaces, so cut after ')'
                    if int(f.read().rsplit(")", 1)[1].split()[1]) == ppid:
                        n += 1
            except Exception:
                continue
        info["workers"] = n
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            argv = [a for a in f.read().decode("utf-8", "replace").split("\0") if a]
        info["parent"] = os.path.basename(argv[1] if len(argv) > 1 and "python" in argv[0] else argv[0])
        for i, a in enumerate(argv):
            if a in ("--workers", "-w") and i + 1 < len(argv):
                info["workers_flag"] = argv[i + 1]
            elif a.startswith("--workers="):
                info["workers_flag"] = a.split("=", 1)[1]
        info["web_concurrency_env"] = os.environ.get("WEB_CONCURRENCY")
        info["gunicorn_cmd_args_env"] = bool(os.environ.get("GUNICORN_CMD_ARGS"))
    except Exception:
        pass
    return info

# One-time import of the standalone tools' learning history. A no-op unless files
# have been dropped into <data dir>/legacy/; runs before the scheduler so no
# engine job is writing while rows are merged. Never allowed to stop the boot.
try:
    from engines import legacy_import as _legacy_import
    _legacy_import.run_pending()
except Exception:
    import logging as _logging
    _logging.getLogger("desk").exception("legacy import failed")

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


@app.route("/api/learning.json")
def learning_json():
    """The learning scorecard: every loop's cohorts judged by one method in one
    vocabulary (engines/learning). ?loop=brief|structure|flow|zerodte narrows it.
    Gated like every page."""
    from engines import produce
    loop = request.args.get("loop", "")
    return produce("local://learning/api/scorecard.json"
                   + (f"?loop={loop}" if loop.isalpha() else ""))


@app.route("/healthz")
def healthz():
    s = get_snapshot()
    out = {
        "status": "ok",
        "live": s.get("live", False),
        "sources_degraded": s.get("degraded", {}),
        # Which process answered. The scheduler assumes exactly one: if repeated
        # calls show more than one pid/host, every scheduled job is running twice.
        "process": _process_info(),
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
    # Persistence diagnostic: the resolved data dir plus a live row count for each
    # engine's learning table, so you can confirm at a glance that the mounted
    # volume is catching writes — and, across a redeploy, that the counts survive
    # (the real proof the volume works, vs. an ephemeral image dir). Fully
    # defensive: a missing file/table or any error reports null and never breaks
    # the endpoint, since Railway gates routing on this returning 200.
    try:
        import sqlite3 as _sql
        from engines import config as _econf
        ddir = _econf.DATA_DIR
        storage = {
            "dir": ddir,
            "exists": os.path.isdir(ddir),
            "writable": os.access(ddir, os.W_OK),
        }
        # (label, sqlite filename under DATA_DIR, table) — the recursive-learning
        # record for each engine. agent: saved briefs + their grades; structure:
        # thin-zone opportunities + outcomes; zerodte: scored level touches; flow:
        # sweep history (first_seen/hits).
        _stores = [
            ("agent.sessions",          "agent.sqlite3",    "sessions"),
            ("agent.outcomes",          "agent.sqlite3",    "outcomes"),
            ("structure.opportunities", "opportunities.db", "opportunities"),
            ("structure.outcomes",      "opportunities.db", "outcomes"),
            ("zerodte.touches",         "zerodte.sqlite3",  "touches"),
            ("flow.sweep_alerts",       "flow.db",          "sweep_alerts"),
            ("flow.signals",            "flow_learning.db", "flow_signals"),
            ("flow.outcomes",           "flow_learning.db", "flow_outcomes"),
        ]
        # The desk's own journal. Its path can be overridden (DESK_DB_PATH), so take
        # it from the store; an absolute path survives the os.path.join below.
        try:
            from desk import store as _store
            _stores += [("desk.sessions", _store.DB_PATH, "daily_signals"),
                        ("desk.graded",   _store.DB_PATH, "graded_biases")]
            storage["desk_journal_on_volume"] = (
                os.path.abspath(_store.DB_PATH).startswith(os.path.abspath(ddir)))
        except Exception:
            pass
        learning = {}
        for label, fname, table in _stores:
            rows = None
            try:
                path = os.path.join(ddir, fname)
                if os.path.exists(path):
                    conn = _sql.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
                    try:
                        rows = conn.execute(
                            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    finally:
                        conn.close()
            except Exception:
                rows = None
            learning[label] = rows
        storage["learning_rows"] = learning
        out["storage"] = storage
        # One-time legacy history import (engines/legacy_import.py): what was
        # merged from the standalone tools' databases, or null if never run.
        try:
            from engines import legacy_import as _legacy
            out["legacy_import"] = _legacy.last_report()
        except Exception:
            pass
        # The learning scorecard in brief: per loop, how many session dates of
        # history it holds and how its headline cohorts are judged. Cached, so
        # this costs a dict lookup on all but one call every few minutes.
        try:
            from engines.learning import scorecard as _card
            out["learning"] = _card.summary(_card.cached())
        except Exception:
            pass
    except Exception:
        pass
    return out, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
