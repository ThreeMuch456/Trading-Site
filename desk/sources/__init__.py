"""Upstream source adapters. Each module exposes `collect(cfg, timeout) -> dict`
(pure mapping of upstream JSON into desk view-model pieces) and `merge(s, c)`
(overlay those pieces onto the snapshot). Keeping fetch+map pure and merge
separate lets fetch.py run the network calls in parallel, then merge on the main
thread — no concurrent mutation of the shared snapshot.
"""

import json
import urllib.request


def http_get_json(url: str, token: str = "", header: str = "Authorization", timeout: float = 4.0) -> dict:
    """Fetch a source JSON document by logical URL.

    Unified (single-deploy) desk: a ``local://<engine>/api/...`` URL is produced
    in-process by the vendored engine (see the ``engines`` package) rather than
    fetched over the network — the whole point of the full merge. Any other scheme
    falls through to a real HTTP GET, kept only for standalone/ad-hoc use; the merged
    deploy never takes that path. The function signature is unchanged so every source
    adapter and the test-suite (which monkeypatch this symbol) are untouched.
    """
    if url.startswith("local:"):
        from engines import produce
        return produce(url, token, header, timeout)
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "trading-desk/1.0"})
    if token:
        req.add_header(header, f"Bearer {token}" if header.lower() == "authorization" else token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
