"""The decoupled screen job: run one full screen and persist the results.

This is the single place the heavy work happens. It's invoked by:
  - the web's in-process daily scheduler (local / single-service deploys), and
  - the CLI:  python main.py --screen-job   (e.g. an external cron worker).

The web never screens in the request path — it only reads what this job wrote
(see src/store.py), so serving stays fast and results survive restarts.
"""
from __future__ import annotations

from . import oplog, schedule, store, tune
from .config import Config
from .screen import Result, rank, screen_universe
from .universe import get_universe


def run_job(cfg: Config, force_refresh: bool = True) -> list[Result]:
    """Screen the universe, rank by combined score, and persist. Returns the
    displayed (agreement-filtered) list."""
    tickers = get_universe(cfg)
    results = screen_universe(tickers, cfg, force_refresh=force_refresh)

    if not results:
        # Every ticker failed outright — almost certainly a transient provider
        # problem (rate limiting / outage), not a genuinely empty universe. Don't
        # clobber the last good results with an empty set; leave them for the
        # next run. (Distinct from "ranked below is empty" — see below.)
        print("[job] screen produced 0 results (provider failure?); keeping previous results")
        return results

    # What's DISPLAYED (dashboard/CSV) is the agreement-filtered, combined-score-
    # sorted list — reuses screen.rank()'s existing >0 filter, which now also
    # reflects the component-agreement gate in _combined(). A quiet day where
    # nothing clears the bar is a legitimate empty list, not a failure: it gets
    # persisted (and displayed) as such, unlike the raw-fetch-failure case above.
    ranked = rank(results, "combined")

    # Optional −γ focus: drop names where dealers are KNOWN to be long gamma
    # (hedging dampens the very moves this screener hunts). Unknown gamma is
    # kept — a provider outage shouldn't blank the dashboard. Logging below
    # still sees every result, so /learning can test this filter's premise.
    if cfg.get("score.negative_gamma_only", False):
        before = len(ranked)
        ranked = [r for r in ranked
                  if getattr(r, "gex", None) is None or r.gex.net_gex < 0]
        print(f"[job] negative_gamma_only: kept {len(ranked)}/{before} (known +γ dropped)")

    # GEX coverage in the meta sidecar -> visible at /healthz and in the banner,
    # so "GEX column is all dashes" is diagnosable without shell access.
    gexes = [getattr(r, "gex", None) for r in ranked]
    n_gex = sum(1 for g in gexes if g is not None)
    n_neg = sum(1 for g in gexes if g is not None and g.net_gex < 0)
    store.save(cfg, ranked, schedule.now_str(cfg), schedule.next_str(cfg),
               extra={"gex_resolved": n_gex, "gex_negative": n_neg})
    # Fixed input for the pre-open re-price pass. Snapshotted here so that pass
    # can re-price from the EOD set every morning without eroding it (it writes
    # the display store, which it would otherwise also be reading from).
    store.save_base(cfg, ranked)

    # Self-calibration loop (best-effort — never let it break the screen job):
    # log EVERY detected component (including agreement-filtered-out setups, so
    # the tuner can later verify the filter itself), then label past ones whose
    # horizon has elapsed.
    if cfg.get("learn.enabled", True):
        try:
            # A partial screen (provider hiccup) must not be logged as a full
            # session — see oplog.run_coverage_shortfall. Labeling still runs.
            skipped = oplog.run_coverage_shortfall(cfg, results, n_universe=len(tickers))
            if skipped:
                print(f"[job] NOT logging this run's opportunities: {skipped}")
                n_logged = 0
            else:
                n_logged = oplog.log_opportunities(cfg, results)
            n_labeled = oplog.label_due(cfg)
            # Refresh the recommend-only tuning proposal (advisory; never applied).
            tune.write_report(cfg)
            # Live peak-of-day hand-off status: whether the /live monitor's
            # summary reached this run's log (the /learning live section had been
            # silently empty). Best-effort — a diagnostic, never fatal.
            live_status = None
            try:
                from . import live
                live_status = live.daily_summary_status(cfg)
            except Exception:  # noqa: BLE001
                pass
            store.update_meta(cfg, learn_logged=n_logged, learn_labeled=n_labeled,
                              learn_error=None, learn_skipped=skipped,
                              live_summary=live_status)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"[job] opportunity logging/labeling failed (non-fatal): {exc}")
            # Surface the failure at /healthz and /learning instead of only in
            # the deploy logs — a silently empty learning DB looks "intentional".
            try:
                store.update_meta(cfg, learn_error=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass

    return ranked
