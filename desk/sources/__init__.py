"""Upstream source adapters. Each module exposes `collect(cfg, timeout) -> dict`
(pure mapping of upstream JSON into desk view-model pieces) and `merge(s, c)`
(overlay those pieces onto the snapshot). Keeping fetch+map pure and merge
separate lets fetch.py run the network calls in parallel, then merge on the main
thread — no concurrent mutation of the shared snapshot.
"""

import json
import urllib.request


def http_get_json(url: str, token: str = "", header: str = "Authorization", timeout: float = 4.0) -> dict:
    """GET a JSON document. Token (when set) is sent as the given header for
    X-App-Token-style gates, or as `Bearer` for Authorization."""
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "trading-desk/1.0"})
    if token:
        req.add_header(header, f"Bearer {token}" if header.lower() == "authorization" else token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
