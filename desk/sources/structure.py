"""Structure-Identifier adapter — the scored thin-zone watchlist.

Core: maps /api/recs.json into the Structure recs table (direction, thin zone +
distance, agreement components, combined score). Enrichment (both best-effort,
degrade to '—' when absent):
  - Rotation: OFS /api/rotation lists member tickers per sector, so ticker ->
    sector quadrant needs no hardcoded map.
  - Quant: Stock-Analysis /api/analyze?ai=0 -> the deterministic 0-100 overall.

Rotation reads the flow source's URL; quant reads the stock source's URL. Either
missing just leaves that column blank — the table always renders from recs.json.
"""

from . import http_get_json

STRUCT_QUANT_MAX = 6      # cap per-ticker quant calls (each is a heavy upstream request)
STRUCT_QUANT_TIMEOUT = 2.5  # tighter per-call budget so the fan-out stays within the fetch deadline

_ROT_LABEL = {"leading": "Leading", "improving": "Improving",
              "weakening": "Weakening", "lagging": "Lagging", "neutral": "Neutral"}


def collect(cfg: dict, timeout: float) -> dict:
    from .. import config
    payload = http_get_json(cfg["url"] + "/api/recs.json", cfg.get("token", ""),
                            cfg.get("header", "Authorization"), timeout)
    recs = payload.get("recs", []) or []

    quad = _rotation_map(config.SOURCES.get("flow", {}), timeout)
    quant = _quant_map(recs, config.SOURCES.get("stock", {}), timeout)

    rows = [_row(r, quad, quant) for r in recs]
    top = max((r.get("combined_score") or 0 for r in recs), default=0)
    firing = sum(1 for row in rows if row["live"] == "Firing")
    learning = _learning(cfg, timeout)
    return {"recs": rows, "as_of": payload.get("as_of"),
            "card": {"passing": len(rows), "top": f"{top:.2f}", "firing": firing},
            "learning": learning["summary"], "track_line": learning["track_line"]}


def merge(s: dict, c: dict) -> None:
    s["structure"]["recs"] = c["recs"]
    if c.get("as_of"):
        s["structure"]["as_of"] = f"Profiles {c['as_of']}"
    if c.get("learning"):
        s["structure"]["learning"] = c["learning"]
    tl = c.get("track_line")
    if tl:
        tr = s.setdefault("eod", {}).setdefault("track_record", {"lines": []})
        tr["lines"] = list(tr.get("lines") or []) + [tl]
    for card in s.get("overview_cards", []):
        if card.get("href") != "structure":
            continue
        for st in card.get("stats", []):
            if st.get("l") == "Passing gate":
                st["v"] = str(c["card"]["passing"])
            elif st.get("l") == "Top score":
                st["v"] = c["card"]["top"]
            elif st.get("l") == "Firing live":
                st["v"] = str(c["card"]["firing"])


# ---- learning (recursive) ----

def _learning(cfg: dict, timeout: float) -> dict:
    """Structure's own self-calibration: win-rate by setup type at the shortest
    horizon, plus a Track Record line. Best-effort — a miss returns no learning."""
    none = {"summary": None, "track_line": None}
    try:
        d = http_get_json(cfg["url"] + "/api/learning.json", cfg.get("token", ""),
                          cfg.get("header", "Authorization"), timeout)
    except Exception:
        return none
    if not d.get("available"):
        return none
    groups = d.get("groups") or []
    n_out = d.get("n_outcomes") or 0
    min_s = d.get("min_samples") or 50
    if not groups:
        return {"summary": {"horizon": None, "rows": [], "n_outcomes": n_out},
                "track_line": {"src": "Structure", "text": f"learning: {n_out} outcomes logged (building history)"}}
    h = sorted({g["horizon"] for g in groups if g.get("horizon") is not None})[0]
    rows = []
    for g in groups:
        if g.get("horizon") != h:
            continue
        wp, n = g.get("win_pct"), g.get("n") or 0
        rows.append({"type": str(g.get("type") or "").capitalize(),
                     "win_pct": f"{wp:.0f}%" if isinstance(wp, (int, float)) else "—",
                     "n": n, "enough": n >= min_s})
    best = max((r for r in rows if r["enough"]), key=lambda r: r["n"], default=None)
    if best:
        tl = {"src": "Structure", "text": f"{best['type'].lower()} setups win {best['win_pct']} at the {h}d horizon (n={best['n']})"}
    else:
        tl = {"src": "Structure", "text": f"learning: {n_out} outcomes logged (need {min_s}/group)"}
    return {"summary": {"horizon": h, "rows": rows, "n_outcomes": n_out}, "track_line": tl}


# ---- enrichment ----

def _rotation_map(fcfg: dict, timeout: float) -> dict:
    quad = {}
    if not fcfg.get("url"):
        return quad
    try:
        rot = http_get_json(fcfg["url"] + "/api/rotation", fcfg.get("token", ""),
                            fcfg.get("header", "Authorization"), timeout)
        for sec in rot.get("sectors", []) or []:
            q = (sec.get("quadrant") or "").lower()
            for t in sec.get("tickers", []) or []:
                sym = t.get("ticker")
                if sym:
                    quad[sym] = q
    except Exception:
        pass   # rotation is optional — a failure just blanks the column
    return quad


def _quant_map(recs: list, scfg: dict, timeout: float) -> dict:
    quant = {}
    if not scfg.get("url"):
        return quant
    qt = min(timeout, STRUCT_QUANT_TIMEOUT)
    for r in recs[:STRUCT_QUANT_MAX]:
        sym = r.get("ticker")
        if not sym:
            continue
        try:
            d = http_get_json(f"{scfg['url']}/api/analyze?ticker={sym}&ai=0",
                              scfg.get("token", ""), scfg.get("header", "Authorization"), qt)
            sc = (d.get("scorecard") or {}).get("overall")
            if isinstance(sc, (int, float)):
                quant[sym] = int(sc)
        except Exception:
            pass   # one name's quant failing never blocks the table
    return quant


# ---- row mapping ----

def _row(r: dict, quad: dict, quant: dict) -> dict:
    sym = r.get("ticker", "—")
    up = r.get("direction", "up") == "up"
    da = r.get("distance_pct")
    dist = f"{'+' if up else '-'}{abs(da):.1f}%" if isinstance(da, (int, float)) else "—"
    comp = r.get("components") or {}
    score = r.get("combined_score") or 0.0
    q = quad.get(sym, "")
    qv = quant.get(sym)
    live_on = r.get("live_activated")
    return {
        "sym": sym,
        "price": f"{r['price']:.2f}" if isinstance(r.get("price"), (int, float)) else "—",
        "dir": "▲ Up" if up else "▼ Down",
        "dir_class": "dir-up" if up else "dir-dn",
        "zone": "Thin resistance" if up else "Thin support",
        "dist": dist,
        "comp": {"z": bool(comp.get("zone")), "f": bool(comp.get("flag")),
                 "g": "amp" if comp.get("gex") else False,
                 "r": "amp" if comp.get("rvol") else False},
        "rotation": _ROT_LABEL.get(q, "—"),
        "rotation_class": f"q-{q}" if q in _ROT_LABEL else "q-neutral",
        "quant": str(qv) if qv is not None else "—",
        "quant_class": "up" if (qv is not None and qv >= 60) else ("down" if (qv is not None and qv < 45) else "flat"),
        "live": "Firing" if live_on else "Watch",
        "live_class": "live" if live_on else "watch",
        "score": f"{score:.2f}",
        "score_pct": int(round(score * 100)),
        "score_color": "var(--green-hot)" if score >= 0.75 else "var(--amber)",
        "score_class": "up" if score >= 0.75 else "amber",
    }
