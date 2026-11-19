"""Options-Flow-Scanner adapter — short-dated index flow.

Maps three existing endpoints into the Flow page + the Intraday flow skew:
  /api/index-etfs/waves  -> fresh one-sided waves (banners) + intraday skew
  /api/index-etfs/eod    -> late-day positioning chips
  /api/index-etfs        -> session skew by ticker (aggregated notable flow)

Defensive: option_type may be 'call'/'put' or 'C'/'P'; a ticker with no fresh
flow simply doesn't appear in waves. All numeric fields are formatted here so the
templates stay dumb.
"""

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from . import http_get_json


def collect(cfg: dict, timeout: float) -> dict:
    base, tok, hdr = cfg["url"], cfg.get("token", ""), cfg.get("header", "Authorization")
    waves = http_get_json(base + "/api/index-etfs/waves", tok, hdr, timeout)
    eod = http_get_json(base + "/api/index-etfs/eod", tok, hdr, timeout)
    alerts = http_get_json(base + "/api/index-etfs", tok, hdr, timeout)

    from .. import config
    order = config.TICKERS

    wave_banners = [_wave(w) for w in waves if w.get("is_surge")]
    intraday_skew = _skew_from_waves(waves, order)
    session_skew = _session_skew(alerts, order)
    eod_chips = [_eod_chip(e) for e in (eod.get("tickers") or [])]
    session_date = _today_et().isoformat()
    label = _session_label(session_date)

    # Full index-ETF parity with the standalone page: every ranked entry (the
    # #1/#2… cards — the old page shows them all, not a top 25) and the raw
    # call-vs-put volume rollup, per expiry bucket so the 0DTE / 1DTE filters
    # can swap it, all from the same /api/index-etfs list the skew aggregates.
    now = datetime.now(timezone.utc)
    ranked = [_entry(a, now) for a in
              sorted(alerts, key=lambda x: (x.get("score") or 0), reverse=True)]
    rollup = _rollup(alerts, order)
    rollups = {
        "all": rollup,
        "0dte": _rollup([a for a in alerts if a.get("bucket") == "0dte"], order),
        "1dte": _rollup([a for a in alerts if a.get("bucket") == "1dte"], order),
    }
    new_count = sum(1 for a in alerts if a.get("is_new"))

    # How many far-OTM lottery tickets the page holds back (the old page's
    # "Far OTM · N hidden"). Only a count — they stay out of every read here,
    # exactly as on the old page by default. Guarded: a miss just omits it.
    try:
        wide = http_get_json(base + "/api/index-etfs?far_otm=1", tok, hdr, timeout)
        far_otm_hidden = sum(1 for a in (wide or []) if a.get("far_otm"))
    except Exception:
        far_otm_hidden = None

    # EOD predictiveness learning loop (flow engine Phase A–D): how well the day's
    # directional flow called the realized ETF move, plus the learned metric +
    # threshold. Guarded — a learning hiccup must never degrade the live Flow page.
    try:
        predict = _predict(http_get_json(base + "/api/flow-learning", tok, hdr, timeout))
    except Exception:
        predict = {"headline": "", "n_sessions": 0, "rows": []}

    surge_count = len(wave_banners)
    fresh_premium = sum(w.get("premium") or 0 for w in waves if w.get("is_surge"))
    # A ticker only "leans" when its EOD flow is a real one-sided wave — the old
    # page shows no lean badge otherwise (e.g. SPY at 48.6% calls).
    call_leans = sum(1 for e in (eod.get("tickers") or [])
                     if e.get("is_wave") and e.get("direction") == "call")
    eod_total = len(eod.get("tickers") or [])

    return {
        "flow": {"session_label": label, "session_date": session_date,
                 "waves": wave_banners, "window_minutes": _window_minutes(waves),
                 "session_skew": session_skew, "rollup": rollup, "rollups": rollups,
                 "ranked": ranked, "new_count": new_count,
                 "far_otm_hidden": far_otm_hidden,
                 "eod": eod_chips, "eod_meta": _eod_meta(eod, session_date)},
        "intraday_skew": intraday_skew,
        "predict": predict,
        "card": {"surge_count": surge_count, "fresh_premium": _usd(fresh_premium),
                 "eod_lean": f"Calls {call_leans}/{eod_total}" if eod_total else "—"},
    }


# Display labels for the predictiveness table.
_HORIZON_LABEL = {"to_close": "→ close", "open_close": "open→close", "next_day": "→ next day"}
_METRIC_LABEL = {"score_weighted_skew": "score-wtd skew", "net_premium_skew": "premium skew"}


def _dir_class(v, up_at) -> str:
    """up/down/flat colour class for a signed value against an 'up' threshold."""
    if v is None:
        return "flat"
    return "up" if v >= up_at else ("down" if v < 0 else "flat")


def _predict(rep: dict) -> dict:
    """Map the flow engine's /api/flow-learning report into the EOD-page table."""
    rows = []
    for b in (rep.get("buckets") or []):
        t = b.get("tuning")
        hit = b.get("hit_rate")
        corr = b.get("skew_corr")
        if t:
            metric = _METRIC_LABEL.get(t.get("metric"), t.get("metric") or "—")
            threshold = f"{t['threshold']:.2f}" if t.get("threshold") is not None else "—"
            edge = f"{t['edge']:+.0f}%" if t.get("edge") is not None else "—"
            sig = bool(t.get("significant"))
            edge_class = _dir_class(t.get("edge"), 0.01)
        else:
            metric = "—" if b.get("ready") else "accumulating"
            threshold, edge, sig, edge_class = "—", "—", False, "flat"
        rows.append({
            "cutoff": b.get("cutoff", "—"),
            "horizon": _HORIZON_LABEL.get(b.get("horizon"), b.get("horizon") or "—"),
            "n": b.get("n_directional") or 0,
            "hit": f"{hit:.0f}%" if isinstance(hit, (int, float)) else "—",
            "hit_class": _dir_class(hit, 55),
            "corr": f"{corr:+.2f}" if isinstance(corr, (int, float)) else "—",
            "metric": metric, "threshold": threshold,
            "edge": edge, "edge_class": edge_class, "sig": sig,
        })
    return {"headline": rep.get("headline", ""),
            "n_sessions": rep.get("n_sessions", 0), "rows": rows}


def merge(s: dict, c: dict) -> None:
    s["flow"].update(c["flow"])
    if c.get("predict"):
        s.setdefault("eod", {})["flow_predict"] = c["predict"]
    if c["intraday_skew"]:
        s["intraday"]["skew"] = c["intraday_skew"]
    # Keep the Overview flow snapshot card honest.
    for card in s.get("overview_cards", []):
        if card.get("href") != "flow":
            continue
        for st in card.get("stats", []):
            if st.get("l") == "Active waves":
                st["v"], st["badge_after"] = str(c["card"]["surge_count"]), ("call", "call")
            elif st.get("l") == "Fresh premium":
                st["v"] = c["card"]["fresh_premium"]
            elif st.get("l") == "EOD lean":
                st["v"] = c["card"]["eod_lean"]


# ---- helpers ----

def _is_call(ot) -> bool:
    return str(ot).lower() in ("call", "c")


_ET = ZoneInfo("America/New_York")


def _today_et():
    """The page's session date: always today on the ET clock (the flow engine's
    _session_window), never rolled back — unlike the EOD window."""
    return datetime.now(_ET).date()


def _parse(iso):
    """An API timestamp as an aware datetime. The flow store keeps them naive
    UTC (SQLAlchemy drops the zone), so a naive value is UTC — reading it as
    local time put every card five hours off."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _hhmm(iso, fmt: str = "%H:%M") -> str:
    dt = _parse(iso)
    return dt.astimezone(_ET).strftime(fmt) if dt else ""


def _ago(iso, now: datetime) -> str:
    dt = _parse(iso)
    if not dt:
        return ""
    mins = max(0, int((now - dt).total_seconds() // 60))
    return f"{mins}m" if mins < 60 else f"{mins // 60}h"


# Flag label -> the style class the old page colours it with. Used only when the
# API does not send `flag_classes` (older payloads / fixtures).
_FLAG_CLASS = (("WHALE", "whale"), ("BIG", "big"), ("VOL/OI", "unusual"),
               ("BURST", "burst"), ("@ASK", "buy"), ("@BID", "sell"),
               ("NO OI", "newoi"), ("VOL≫OI", "newoi"), ("FAR OTM", "farotm"),
               ("NEW", "new"))


def _flag_class(label: str) -> str:
    up = label.upper()
    return next((cls for key, cls in _FLAG_CLASS if key in up), "")


def _flags(a: dict) -> list:
    labels = [str(f) for f in (a.get("flags") or [])]
    classes = a.get("flag_classes")
    if not (isinstance(classes, list) and len(classes) == len(labels)):
        classes = [_flag_class(f) for f in labels]
    out = [{"cls": c, "label": escape(f)} for c, f in zip(classes, labels)]
    if a.get("is_new") and not any(f["cls"] == "new" for f in out):
        out.append({"cls": "new", "label": "NEW"})
    return out


def _entry(a: dict, now: datetime | None = None) -> dict:
    """One ranked index-ETF flow entry → a card row, mirroring the standalone's
    _sweep_card: flags in their colours, the wave / EOD tags that tie a card to
    the banners above it, Px @ flag, and times on the ET clock."""
    now = now or datetime.now(timezone.utc)
    call = _is_call(a.get("option_type"))
    try:
        score = max(0, min(100, int(round(float(a.get("score") or 0)))))
    except (TypeError, ValueError):
        score = 0
    voi, iv, dl = a.get("vol_oi_ratio"), a.get("implied_volatility"), a.get("delta")
    und = a.get("underlying_price")
    hits = a.get("hits") or 1
    ask = a.get("ask")
    surge, eod = a.get("surge"), a.get("eod")
    return {
        "sym": a.get("ticker", "—"),
        "bucket": a.get("bucket") or "",
        "px": f"${ask:.2f}" if isinstance(ask, (int, float)) and ask > 0 else None,
        "px_age": _ago(a.get("detected_at"), now),
        "surge": "call" if surge == "call" else ("put" if surge == "put" else None),
        "eod": "call" if eod == "call" else ("put" if eod == "put" else None),
        "cp": "C" if call else "P", "cp_class": "call" if call else "put",
        "strike": _g(a.get("strike")), "expiry": escape(str(a.get("expiry", "—"))),
        # Full dollars on the card, as the scanner's page prints it.
        "premium": (f"${a['estimated_premium']:,.0f}"
                    if isinstance(a.get("estimated_premium"), (int, float)) else "—"),
        "score": score,
        "score_color": "var(--green)" if score >= 70 else ("var(--amber)" if score >= 45 else "var(--blue)"),
        "flags": _flags(a),
        "is_new": bool(a.get("is_new")),
        "voi": f"{voi:.1f}×" if isinstance(voi, (int, float)) else "—",
        "vol": _n(a.get("volume")),
        "fresh": f"+{_n(a.get('volume_delta'))}" if a.get("volume_delta") else None,
        "oi": _n(a.get("open_interest")) if a.get("open_interest") else "—",
        "iv": f"{iv * 100:.0f}%" if isinstance(iv, (int, float)) else "—",
        "delta": f"{dl:+.2f}" if isinstance(dl, (int, float)) else "—",
        "underlying": f"${und:.2f}" if isinstance(und, (int, float)) else "—",
        "reflagged": f"{hits}× since {_hhmm(a.get('first_seen'))} ET" if (hits and hits > 1) else None,
        "time": _hhmm(a.get("detected_at")),
    }


def _rollup(alerts: list, order: list) -> list:
    """Per-ticker raw call vs put volume across the notable entries this session,
    plus the fresh (15m) call/put split — the standalone's 'Ticker Rollup'.

    The fresh line counts only entries flagged inside the window (`is_new`), as
    the old page does. Summing every entry's last-scan delta credited contracts
    flagged hours earlier to the last 15 minutes. Tickers with no entries are
    left out rather than shown as an empty 50/50 card."""
    agg: dict = {}
    for a in alerts:
        t = a.get("ticker")
        if t is None:
            continue
        d = agg.setdefault(t, {"c": 0, "p": 0, "cd": 0, "pd": 0, "n": 0})
        vol = a.get("volume") or 0
        dl = (a.get("volume_delta") or 0) if a.get("is_new") else 0
        if _is_call(a.get("option_type")):
            d["c"] += vol
            d["cd"] += dl
        else:
            d["p"] += vol
            d["pd"] += dl
        d["n"] += 1
    out = []
    for sym in list(order) + sorted(set(agg) - set(order)):
        d = agg.get(sym)
        if not d:
            continue
        tot = d["c"] + d["p"]
        share = d["c"] / tot * 100 if tot else 50.0
        out.append({
            "sym": sym, "call_vol": _n(d["c"]), "put_vol": _n(d["p"]),
            "call_pct": int(round(share)), "call_pct_txt": f"{share:.1f}%" if tot else "—",
            "contracts": d["n"],
            "fresh": f"+{_n(d['cd'])}C / +{_n(d['pd'])}P" if (d["cd"] + d["pd"]) else None,
            "fresh_c": f"+{_n(d['cd'])}C", "fresh_p": f"+{_n(d['pd'])}P",
        })
    return out


def _wave(w: dict) -> dict:
    call_share = w.get("call_share")
    call_pct = int(round(call_share * 100)) if isinstance(call_share, (int, float)) else 50
    side = w.get("direction", "call")
    dom = call_pct if side == "call" else 100 - call_pct
    top = w.get("top_contract")
    lead = ""
    if top:
        cp = "C" if _is_call(top.get("option_type")) else "P"
        lead = f"led by ${_g(top.get('strike'))}{cp} exp {escape(str(top.get('expiry', '')))} (+{_n(top.get('volume_delta'))})"
    mins = w.get("window_minutes", 15)
    return {
        "sym": w.get("ticker", "—"), "side": side,
        "call_n": _n(w.get("call_fresh")), "put_n": _n(w.get("put_fresh")),
        "skew_pct": dom, "call_pct": call_pct, "lead": lead,
        "meta": f"{_usd(w.get('premium'))} fresh · {w.get('contracts', 0)} contract"
                f"{'s' if (w.get('contracts') or 0) != 1 else ''} · last {mins}m",
    }


def _skew_from_waves(waves: list, order: list) -> list:
    by = {w.get("ticker"): w for w in waves}
    out = []
    for sym in order:
        w = by.get(sym)
        if not w or not isinstance(w.get("call_share"), (int, float)):
            out.append({"sym": sym, "call_pct": 50, "label": "—"})
            continue
        cp = int(round(w["call_share"] * 100))
        out.append({"sym": sym, "call_pct": cp,
                    "label": f"{cp}% calls" if cp >= 50 else f"{100 - cp}% puts"})
    return out


def _session_skew(alerts: list, order: list) -> list:
    agg = {}
    for a in alerts:
        t = a.get("ticker")
        if t is None:
            continue
        c, p = agg.setdefault(t, [0, 0])
        v = a.get("volume") or 0
        if _is_call(a.get("option_type")):
            agg[t][0] = c + v
        else:
            agg[t][1] = p + v
    out = []
    for sym in order:
        c, p = agg.get(sym, [0, 0])
        tot = c + p
        if tot <= 0:
            out.append({"sym": sym, "call_pct": 50, "label": "—"})
            continue
        cp = int(round(c / tot * 100))
        out.append({"sym": sym, "call_pct": cp,
                    "label": f"{cp}% calls" if cp >= 50 else f"{100 - cp}% puts"})
    return out


def _eod_chip(e: dict) -> dict:
    """One ticker's late-day positioning, as the old page draws it: call and put
    contracts, % calls, prints, premium and the leading contract. The lean badge
    is only set when the flow is a real one-sided wave (`is_wave`) — otherwise
    the ticker is shown without a lean, never with the larger side by default."""
    # From the raw volumes when present (the API's call_share is pre-rounded,
    # which can shift the displayed decimal off the scanner page's own).
    cv, pv = e.get("call_volume"), e.get("put_volume")
    call_share = e.get("call_share")
    if isinstance(cv, (int, float)) and isinstance(pv, (int, float)) and cv + pv > 0:
        share = cv / (cv + pv) * 100
    else:
        share = call_share * 100 if isinstance(call_share, (int, float)) else 50.0
    side = e.get("direction", "call")
    top = e.get("top_contract") or {}
    led = ""
    if top:
        cp = "C" if _is_call(top.get("option_type")) else "P"
        led = f"led by ${_g(top.get('strike'))}{cp} exp {escape(str(top.get('expiry', '')))}"
    prints = e.get("contracts") or 0
    detail = (f"{share:.1f}% calls · {prints} print{'s' if prints != 1 else ''} · "
              f"{_usd(e.get('premium'))}")
    is_wave = bool(e.get("is_wave"))
    return {"sym": e.get("ticker", "—"),
            "lean": side if is_wave else None, "is_wave": is_wave,
            "call_vol": _n(e.get("call_volume")), "put_vol": _n(e.get("put_volume")),
            "call_pct": int(round(share)), "detail": detail, "led": led}


def _eod_meta(eod: dict, session_date: str) -> dict:
    """The EOD section's header: which window it covers and whether it is the
    prior session's read (mornings) or the one about to carry into tomorrow."""
    d = eod.get("session_date")
    start = _parse(eod.get("window_start"))
    start_txt = start.astimezone(_ET).strftime("%I:%M %p").lstrip("0") if start else "3:00 PM"
    prior = bool(d) and d != session_date
    return {"session": escape(str(d or "")), "start": start_txt, "prior": prior,
            "when": "prior session · read on today" if prior else "next session"}


def _window_minutes(waves: list) -> int:
    for w in waves or []:
        if isinstance(w.get("window_minutes"), int):
            return w["window_minutes"]
    return 15


def _session_label(session_date: str) -> str:
    try:
        d = datetime.fromisoformat(session_date)
        return f"0DTE / 1DTE · session of {d.strftime('%a %b')} {d.day}"
    except (TypeError, ValueError):
        return "0DTE / 1DTE"


def fetch_sweeps(cfg: dict, timeout: float) -> list:
    """All-ticker unusual activity (/api/sweeps, index ETFs excluded) → the
    'All Tickers' flow table."""
    data = http_get_json(cfg["url"] + "/api/sweeps", cfg.get("token", ""),
                         cfg.get("header", "Authorization"), timeout)
    rows = []
    for a in (data or [])[:50]:
        call = _is_call(a.get("option_type"))
        voi = a.get("vol_oi_ratio")
        score = a.get("score")
        rows.append({
            "sym": a.get("ticker", "—"),
            "cp": "C" if call else "P", "cp_class": "call" if call else "put",
            "strike": _g(a.get("strike")), "expiry": escape(str(a.get("expiry", "—"))),
            "premium": _usd(a.get("estimated_premium")), "vol": _n(a.get("volume")),
            "voi": f"{voi:.1f}" if isinstance(voi, (int, float)) else "—",
            "side": escape(str(a.get("side") or "—")),
            "score": f"{score:.0f}" if isinstance(score, (int, float)) else "—",
            "flags": [escape(str(f)) for f in (a.get("flags") or [])],
            "is_new": bool(a.get("is_new")),
        })
    return rows


def fetch_ticker(cfg: dict, sym: str, timeout: float) -> dict:
    """One name's call-vs-put picture (/api/ticker/<sym>): overall lean, the
    term structure (by expiry), and strike clustering."""
    data = http_get_json(cfg["url"] + f"/api/ticker/{sym}", cfg.get("token", ""),
                         cfg.get("header", "Authorization"), timeout)
    tot = data.get("totals") or {}
    cs = tot.get("call_share")
    lean = tot.get("lean")

    def _vrow(r: dict) -> dict:
        c, p = r.get("call_volume") or 0, r.get("put_volume") or 0
        t = c + p
        return {"call_vol": _n(c), "put_vol": _n(p),
                "call_pct": int(round(c / t * 100)) if t else 50,
                "premium": _usd(r.get("premium"))}

    by_expiry = []
    for r in data.get("by_expiry", []) or []:
        d = _vrow(r)
        top = f"${_g(r.get('top_strike'))}{'C' if _is_call(r.get('top_type')) else 'P'}" if r.get("top_strike") else "—"
        d.update(expiry=escape(str(r.get("expiry", "—"))), top=top)
        by_expiry.append(d)
    by_strike = []
    for r in (data.get("by_strike") or [])[:15]:
        d = _vrow(r)
        d.update(strike=_g(r.get("strike")))
        by_strike.append(d)

    return {
        "sym": data.get("ticker", sym),
        "underlying": _g(data.get("underlying")),
        "as_of": escape(str(data.get("as_of") or "")),
        "call_pct": int(round(cs * 100)) if isinstance(cs, (int, float)) else 50,
        "lean": lean or "balanced",
        "lean_class": "call" if lean == "call" else ("put" if lean == "put" else "flat"),
        "call_vol": _n(tot.get("call_volume")), "put_vol": _n(tot.get("put_volume")),
        "premium": _usd(tot.get("premium")),
        "by_expiry": by_expiry, "by_strike": by_strike,
    }


def _usd(x) -> str:
    if not isinstance(x, (int, float)) or x == 0:
        return "$0"
    if abs(x) >= 1e6:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.0f}"


def _n(x) -> str:
    return f"{int(x):,}" if isinstance(x, (int, float)) else "0"


def _g(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else "—"
