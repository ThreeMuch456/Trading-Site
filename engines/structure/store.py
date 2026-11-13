"""Persist screen results to disk so the web layer is stateless.

The screen job writes (atomically); the web reads, and reloads whenever the file
changes (by mtime). This decouples the heavy screen from request serving, lets
results survive restarts, and lets several readers share one result set on a
Railway volume.
"""
from __future__ import annotations

import json
import pickle
import time
from pathlib import Path

from .config import Config


def _dir(cfg: Config) -> Path:
    d = Path(cfg.get("data.cache_dir", "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def results_path(cfg: Config) -> Path:
    return _dir(cfg) / "screen_results.pkl"


def meta_path(cfg: Config) -> Path:
    return _dir(cfg) / "screen_meta.json"


def base_path(cfg: Config) -> Path:
    return _dir(cfg) / "screen_base.pkl"


def save_base(cfg: Config, results: list) -> Path:
    """Snapshot the EOD screen's output as the re-price pass's fixed input.

    Written only by the screen job. The pre-open pass (src/reprice.py) reads this
    rather than the display store because it also *writes* the display store: a
    morning that ranks nothing would otherwise destroy its own input, leaving
    /ticker pages 404 and the board empty until the next 17:00 run."""
    bp = base_path(cfg)
    tmp = bp.with_suffix(".pkl.tmp")
    with tmp.open("wb") as fh:
        pickle.dump(results, fh)
    tmp.replace(bp)
    return bp


def load_base(cfg: Config) -> list | None:
    """The EOD snapshot, or None when there isn't one yet (pre-first-run, or a
    store written before save_base existed — callers fall back to load())."""
    bp = base_path(cfg)
    if not bp.exists():
        return None
    try:
        with bp.open("rb") as fh:
            return pickle.load(fh)
    except Exception:  # noqa: BLE001 - corrupt/partial -> treat as absent
        return None


def save(cfg: Config, results: list, last_run: str, next_run: str,
         extra: dict | None = None) -> Path:
    """Atomically persist results + a small metadata sidecar.

    The pickle is written to a temp file then renamed so a concurrent reader
    never sees a half-written file. `extra` is merged into the meta sidecar
    (e.g. GEX coverage) so /healthz can report run diagnostics."""
    rp = results_path(cfg)
    tmp = rp.with_suffix(".pkl.tmp")
    with tmp.open("wb") as fh:
        pickle.dump(results, fh)
    tmp.replace(rp)  # atomic on the same filesystem

    meta = {
        "last_run": last_run,
        "next_run": next_run,
        "count": len(results),
        "ts": time.time(),
        **(extra or {}),
    }
    meta_path(cfg).write_text(json.dumps(meta))
    return rp


def load_meta(cfg: Config) -> dict:
    """The meta sidecar alone, fresh from disk ({} if missing/garbled). Cheap —
    lets /healthz see post-save updates (update_meta) that don't bump the
    results pickle's mtime."""
    try:
        return json.loads(meta_path(cfg).read_text())
    except Exception:  # noqa: BLE001
        return {}


def update_meta(cfg: Config, **fields) -> None:
    """Merge fields into the meta sidecar without touching the results pickle.
    Best-effort — used by the job to append post-save diagnostics (e.g. whether
    the learning log write succeeded) so /healthz can report them."""
    meta = load_meta(cfg)
    meta.update(fields)
    meta_path(cfg).write_text(json.dumps(meta))


def load(cfg: Config) -> tuple[list | None, dict | None]:
    """Return (results, meta), or (None, None) if there's nothing usable yet."""
    rp = results_path(cfg)
    if not rp.exists():
        return None, None
    try:
        with rp.open("rb") as fh:
            results = pickle.load(fh)
    except Exception:  # noqa: BLE001 - corrupt/partial file -> treat as no data
        return None, None

    meta: dict = {}
    try:
        meta = json.loads(meta_path(cfg).read_text())
    except Exception:  # noqa: BLE001 - missing/garbled meta is non-fatal
        pass
    return results, meta


def mtime(cfg: Config) -> float:
    rp = results_path(cfg)
    return rp.stat().st_mtime if rp.exists() else 0.0
