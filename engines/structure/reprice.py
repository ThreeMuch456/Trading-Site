"""Pre-open re-price pass: refresh where price sits against yesterday's profile.

A volume profile is a slow-moving object — 60 days of hourly bars — and it does
not meaningfully change overnight. What changes is where price sits *relative* to
it. An overnight gap can move a name from "6% below its air pocket" to "sitting in
the mouth of it", and by 9:30 the 17:00 screen's geometry is ~17 hours stale.

So this pass rebuilds nothing. It loads the EOD snapshot (store.save_base),
replaces each profile's `current_price` with a fresh extended-hours quote, re-runs
zone detection and scoring against the *unchanged* volume histogram, and re-ranks
into the display store. One batched quote request replaces a full universe refetch.

Note what it can't do: the snapshot holds the EOD *ranked* set, so a name that
didn't qualify at 17:00 can't appear here no matter how it gaps. Re-pricing
re-sorts and prunes the overnight watchlist; it doesn't discover new names.

Two deliberate non-goals:

  1. It never touches the volume profile. See data.fetch_quotes — extended-hours
     prints are thin and often outside the regular-session range, so folding them
     into the histogram would manufacture LVNs out of hours when nobody traded.

  2. It never writes to the opportunity log. oplog's primary key is
     (run_date, ticker, type) with INSERT OR IGNORE, so a morning write would
     claim the day's row and silently suppress the 17:00 one — quietly redefining
     the learning dataset's entry_price from post-close to pre-market, mid
     calibration. The learning loop keeps exactly one EOD observation per trading
     day; this pass is display-only.

Carried over unchanged from the EOD run (and therefore mildly stale by design):
the flag, dealer GEX, ADV$, and RVOL. RVOL in particular is an EOD reading —
pre-market volume isn't comparable to a regular-session baseline, so re-deriving
it here would be worse than leaving it.
"""
from __future__ import annotations

from . import schedule, store
from .config import Config
from .data import fetch_quotes
from .detect import detect_thin_zones
from .screen import rank, rescore

# Meta keys owned by store.save() itself — everything else in the sidecar is a
# diagnostic written by the screen job (GEX coverage, learning counts) and must
# survive a re-price, since /healthz reads it.
_SAVE_OWNED = ("last_run", "next_run", "count", "ts")


def reprice(cfg: Config) -> list | None:
    """Re-price the persisted results against fresh extended-hours quotes and
    persist the re-ranked list. Returns the new ranked list, or None if there was
    nothing to do (no stored results, or no quotes came back)."""
    # Read the EOD snapshot, not the display store — this function writes the
    # display store, so reading it back would let a morning that ranks nothing
    # destroy its own input. Fall back for stores written before save_base existed.
    meta = store.load_meta(cfg)
    results = store.load_base(cfg)
    if results is None:
        results, meta = store.load(cfg)
        meta = meta or {}
    if not results:
        print("[reprice] no persisted results to re-price; skipping")
        return None

    quotes = fetch_quotes([r.ticker for r in results], cfg)
    if not quotes:
        # A provider hiccup shouldn't blank the dashboard — leave the EOD
        # results exactly as they are and wait for the next scheduled pass.
        print("[reprice] no quotes resolved (provider issue?); leaving results untouched")
        return None

    min_move = float(cfg.get("web.reprice_min_move_pct", 0.0))
    moved = out_of_range = 0

    for r in results:
        price = quotes.get(r.ticker)
        if price is None:
            continue
        previous = float(r.profile.current_price)
        if previous > 0 and abs(price - previous) / previous * 100.0 < min_move:
            continue

        # A gap clean out of the profile's price range means the histogram no
        # longer describes where price is. detect_thin_zones still behaves —
        # price_to_bin clamps to the edge bin while distances come off the true
        # price, so zones just read as far away — but it's worth counting.
        low, high = float(r.profile.bin_edges[0]), float(r.profile.bin_edges[-1])
        if not low <= price <= high:
            out_of_range += 1

        r.profile.current_price = price
        zones = detect_thin_zones(r.profile, cfg)
        r.support    = zones["support"]
        r.resistance = zones["resistance"]
        rescore(r, cfg)
        moved += 1

    ranked = rank(results, "combined")

    # store.save() rebuilds the sidecar from scratch, so carry the screen job's
    # diagnostics across explicitly. last_run/next_run keep pointing at the EOD
    # screen — this pass refreshed prices, it didn't re-screen.
    carried = {k: v for k, v in (meta or {}).items() if k not in _SAVE_OWNED}
    store.save(
        cfg, ranked,
        (meta or {}).get("last_run") or schedule.now_str(cfg),
        (meta or {}).get("next_run") or schedule.next_str(cfg),
        extra={
            **carried,
            "repriced_at":          schedule.now_str(cfg),
            "reprice_quotes":       len(quotes),
            "reprice_moved":        moved,
            "reprice_out_of_range": out_of_range,
        },
    )

    print(f"[reprice] re-priced {moved}/{len(results)} tickers "
          f"({out_of_range} gapped outside their profile range); "
          f"{len(ranked)} ranked")
    return ranked
