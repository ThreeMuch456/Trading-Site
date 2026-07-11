"""
Trading Terminal — unified landing hub.

A standalone front door for the options-trading toolset. Each project remains its
own independently-deployed app; this hub just links out to them. Nothing here
imports or modifies the sibling projects.

Run:   python app.py
Visit: http://localhost:8000

Link targets are read from environment variables (see PROJECTS below); any that
are unset fall back to "#" and the card shows a "link not configured" hint.
"""

import os
from pathlib import Path

from flask import Flask, render_template_string

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

app = Flask(__name__)

SITE_NAME = os.environ.get("SITE_NAME", "Trading Terminal")

# ---------------------------------------------------------------------------
# Project registry
# ---------------------------------------------------------------------------
# `env` is the environment variable that supplies the deployed URL for each card.
# `status`: "live" (linkable) or "soon" (disabled placeholder — no link yet).
# `accent`: one of blue / green / amber / purple / red (maps to CSS vars).

PROJECTS = [
    {
        "num": "01",
        "name": "Morning Brief",
        "codename": "Trading-Agent",
        "category": "Pre-Market Gameplan",
        "blurb": "A synthesized pre-market brief: watchlist prices, overnight global "
                 "markets, macro calendar, and an AI-generated gameplan with a risk "
                 "rating for the session ahead.",
        "features": ["Market + options + macro synthesis",
                     "AI gameplan & risk rating",
                     "GEX levels · unusual flow · overnight globals"],
        "accent": "blue",
        "env": "MORNING_BRIEF_URL",
        "status": "live",
    },
    {
        "num": "02",
        "name": "Stock Deep Dive",
        "codename": "Stock-Analysis",
        "category": "Single-Stock Research",
        "blurb": "Long-horizon research on one ticker: fundamentals, multi-year price "
                 "history with news-driven move markers, analyst ratings, a quant "
                 "scorecard, and an AI deep-dive thesis.",
        "features": ["Fundamentals · beta · alpha vs SPY",
                     "Analyst ratings & price targets",
                     "Quant scorecard + AI thesis"],
        "accent": "purple",
        "env": "STOCK_DEEPDIVE_URL",
        "status": "live",
    },
    {
        "num": "03",
        "name": "SPY Power Hour",
        "codename": "SPY-Power-Hour",
        "category": "Intraday 0DTE Tracker",
        "blurb": "Tracks SPY / SPX spot and 0DTE call options across the 11:55a–1:15p "
                 "PT power-hour window, laddered by strike offset with a live Greeks "
                 "and P&L view.",
        "features": ["SPY & SPX 0DTE call ladders",
                     "11:55a PT entry benchmark",
                     "Live Greeks · intraday P&L"],
        "accent": "green",
        "env": "SPY_POWER_HOUR_URL",
        "status": "live",
    },
    {
        "num": "04",
        "name": "Thin-Zone Screener",
        "codename": "Structure-Identifier",
        "category": "Volume-Structure Screener",
        "blurb": "Builds volume-by-price profiles across a universe and flags low-volume "
                 "nodes near price — the 'air pockets' where price can travel fast, up "
                 "(thin resistance) or down (thin support).",
        "features": ["Volume-by-price / LVN detection",
                     "Thin support vs thin resistance",
                     "Watchlist · S&P 500 universe screens"],
        "accent": "amber",
        "env": "THIN_ZONE_URL",
        "status": "live",
    },
    {
        "num": "05",
        "name": "Options Flow Scanner",
        "codename": "Options-Flow-Scanner",
        "category": "Unusual Options Activity",
        "blurb": "Scans ~150 high-volume names for unusual options activity — large "
                 "premium and volume spikes vs. open interest. Undergoing work before "
                 "it joins the desk.",
        "features": ["Premium & vol/OI spike detection",
                     "Live alerts feed + watchlist P&L",
                     "In development — not yet wired in"],
        "accent": "red",
        "env": "OPTIONS_FLOW_URL",
        "status": "soon",
    },
]


def _resolve(project: dict) -> dict:
    url = os.environ.get(project["env"], "").strip() or "#"
    return {**project, "url": url, "configured": url != "#"}


@app.route("/")
def index():
    projects = [_resolve(p) for p in PROJECTS]
    live = sum(1 for p in projects if p["status"] == "live")
    return render_template_string(
        TEMPLATE, projects=projects, site_name=SITE_NAME,
        live_count=live, total_count=len(projects),
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


# ---------------------------------------------------------------------------
# Template — styled to match the Morning Brief (Trading-Agent) dashboard.
# ---------------------------------------------------------------------------

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ site_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0b0f1a;
    --surface:   #111827;
    --surface2:  #1a2235;
    --border:    #1f2d45;
    --text:      #d1dbe8;
    --muted:     #4b6080;
    --green:     #10d98a;
    --green-dim: #0a7a4e;
    --red:       #ff4466;
    --red-dim:   #7a1a28;
    --amber:     #f5a623;
    --amber-dim: #7a5010;
    --blue:      #4d9fff;
    --blue-dim:  #1a3d6e;
    --purple:    #a78bfa;
    --purple-dim:#3d2a6e;
    --font:      'JetBrains Mono', monospace;
    --header-h:  48px;
  }

  html, body { height: 100%; }
  body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    font-size: 13px;
    line-height: 1.6;
    min-height: 100%;
    display: flex;
    flex-direction: column;
  }

  /* ── Header ── */
  .header {
    background: #090d18;
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: var(--header-h);
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .header-left { display: flex; align-items: baseline; gap: 16px; }
  .header h1 {
    font-size: 0.9em;
    font-weight: 700;
    letter-spacing: 0.18em;
    color: var(--blue);
    text-transform: uppercase;
  }
  .header .meta { font-size: 0.72em; color: var(--muted); letter-spacing: 0.04em; }
  .header-right { display: flex; align-items: center; gap: 10px; }
  .pill {
    font-size: 0.66em; font-weight: 700; letter-spacing: 0.12em;
    padding: 3px 10px; border-radius: 3px; text-transform: uppercase;
    background: var(--green-dim); color: var(--green); border: 1px solid var(--green-dim);
  }

  /* ── Main ── */
  .main { flex: 1; max-width: 1180px; width: 100%; margin: 0 auto; padding: 32px 22px 56px; }

  /* ── Hero ── */
  .hero { margin-bottom: 30px; }
  .hero .eyebrow {
    font-size: 0.68em; font-weight: 700; letter-spacing: 0.22em;
    color: var(--muted); text-transform: uppercase; margin-bottom: 10px;
  }
  .hero h2 {
    font-size: 1.9em; font-weight: 700; letter-spacing: 0.01em;
    line-height: 1.25; color: var(--text); max-width: 760px;
  }
  .hero h2 .accent { color: var(--blue); }
  .hero p {
    margin-top: 12px; font-size: 0.92em; color: var(--muted);
    max-width: 680px; line-height: 1.7;
  }

  /* ── Section label ── */
  .section-label {
    display: flex; align-items: center; gap: 12px;
    font-size: 0.66em; font-weight: 700; letter-spacing: 0.18em;
    color: var(--muted); text-transform: uppercase; margin: 8px 0 14px;
  }
  .section-label::after {
    content: ""; flex: 1; height: 1px; background: var(--border);
  }

  /* ── Project grid ── */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
    gap: 14px;
  }

  .card {
    position: relative;
    display: flex;
    flex-direction: column;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    text-decoration: none;
    color: inherit;
    transition: border-color .18s, transform .18s, box-shadow .18s;
    border-top: 2px solid var(--accent);
  }
  .card.live:hover {
    transform: translateY(-3px);
    border-color: var(--accent);
    box-shadow: 0 10px 30px -12px var(--accent-shadow);
  }
  .card.soon { cursor: default; opacity: 0.62; }

  .card-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 13px 16px 0;
  }
  .card-num {
    font-size: 0.72em; font-weight: 700; letter-spacing: 0.12em;
    color: var(--accent);
  }
  .badge {
    font-size: 0.6em; font-weight: 700; letter-spacing: 0.14em;
    padding: 3px 9px; border-radius: 3px; text-transform: uppercase;
  }
  .badge.live { background: var(--green-dim); color: var(--green); }
  .badge.soon { background: var(--amber-dim); color: var(--amber); }

  .card-body { padding: 8px 16px 16px; flex: 1; display: flex; flex-direction: column; }
  .card h3 {
    font-size: 1.18em; font-weight: 700; letter-spacing: 0.02em;
    color: var(--text); margin: 6px 0 3px;
  }
  .card .cat {
    font-size: 0.68em; font-weight: 600; letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--accent); margin-bottom: 11px;
  }
  .card .blurb { font-size: 0.82em; color: var(--muted); line-height: 1.65; margin-bottom: 13px; }

  .feats { list-style: none; margin-top: auto; display: flex; flex-direction: column; gap: 5px; }
  .feats li {
    font-size: 0.74em; color: var(--text); display: flex; gap: 8px; align-items: flex-start;
  }
  .feats li::before {
    content: "▸"; color: var(--accent); font-weight: 700; flex-shrink: 0;
  }

  .card-foot {
    border-top: 1px solid var(--border);
    padding: 9px 16px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 0.7em; letter-spacing: 0.06em;
  }
  .card-foot .codename { color: var(--muted); }
  .card-foot .go { color: var(--accent); font-weight: 700; letter-spacing: 0.1em; }
  .card.live:hover .go .arr { transform: translateX(3px); }
  .go .arr { display: inline-block; transition: transform .18s; }
  .card-foot .unconfigured { color: var(--amber); font-weight: 600; }
  .card-foot .soon-note { color: var(--muted); }

  /* accent mapping */
  .accent-blue   { --accent: var(--blue);   --accent-shadow: rgba(77,159,255,0.5); }
  .accent-green  { --accent: var(--green);  --accent-shadow: rgba(16,217,138,0.5); }
  .accent-amber  { --accent: var(--amber);  --accent-shadow: rgba(245,166,35,0.5); }
  .accent-purple { --accent: var(--purple); --accent-shadow: rgba(167,139,250,0.5); }
  .accent-red    { --accent: var(--red);    --accent-shadow: rgba(255,68,102,0.5); }

  /* ── Footer ── */
  .footer {
    border-top: 1px solid var(--border);
    padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 8px;
    font-size: 0.7em; color: var(--muted); letter-spacing: 0.04em;
  }
  .footer .disclaimer { max-width: 720px; line-height: 1.6; }

  @media (max-width: 560px) {
    .hero h2 { font-size: 1.5em; }
    .grid { grid-template-columns: 1fr; }
    .header { padding: 0 16px; }
    .main { padding: 24px 16px 40px; }
  }
</style>
</head>
<body>

<!-- ── HEADER ── -->
<div class="header">
  <div class="header-left">
    <h1>{{ site_name }}</h1>
    <span class="meta">Options Desk</span>
  </div>
  <div class="header-right">
    <span class="pill">{{ live_count }} Live</span>
  </div>
</div>

<div class="main">

  <!-- ── HERO ── -->
  <div class="hero">
    <div class="eyebrow">Unified Options Trading Desk</div>
    <h2>One terminal for the whole workflow —
      <span class="accent">gameplan, research, structure, and flow.</span></h2>
    <p>A cohesive front door to a suite of purpose-built tools. Start the day with a
      synthesized brief, research a name in depth, track the intraday power hour, and
      screen for structural air pockets — each tool a click away.</p>
  </div>

  <!-- ── PROJECT GRID ── -->
  <div class="section-label">Tools</div>
  <div class="grid">
    {% for p in projects %}
      {% set live = p.status == 'live' %}
      <{{ 'a' if live and p.configured else 'div' }}
         class="card accent-{{ p.accent }} {{ p.status }}"
         {% if live and p.configured %}href="{{ p.url }}"{% endif %}>
        <div class="card-head">
          <span class="card-num">{{ p.num }}</span>
          <span class="badge {{ p.status }}">{{ 'Live' if live else 'Soon' }}</span>
        </div>
        <div class="card-body">
          <h3>{{ p.name }}</h3>
          <div class="cat">{{ p.category }}</div>
          <div class="blurb">{{ p.blurb }}</div>
          <ul class="feats">
            {% for f in p.features %}<li>{{ f }}</li>{% endfor %}
          </ul>
        </div>
        <div class="card-foot">
          <span class="codename">{{ p.codename }}</span>
          {% if not live %}
            <span class="soon-note">In development</span>
          {% elif p.configured %}
            <span class="go">Open <span class="arr">→</span></span>
          {% else %}
            <span class="unconfigured">Set {{ p.env }}</span>
          {% endif %}
        </div>
      </{{ 'a' if live and p.configured else 'div' }}>
    {% endfor %}
  </div>

</div>

<!-- ── FOOTER ── -->
<div class="footer">
  <span class="disclaimer">Research &amp; education only — not investment advice. Each
    tool links to its own independently-deployed service.</span>
  <span>{{ site_name }}</span>
</div>

</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False)
