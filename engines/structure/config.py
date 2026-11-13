"""Load and access the YAML configuration as nested dotted dicts."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _apply_data_dir_override(data: dict[str, Any]) -> None:
    """Rebase all persistent state onto a mounted volume when the platform
    provides one. Railway injects RAILWAY_VOLUME_MOUNT_PATH (e.g. "/data") into
    a service with an attached volume; DATA_DIR is the generic override and
    wins. Without this, the volume sits unused at its mount point while the app
    writes to the relative ./data inside the image — ephemeral, so bar caches,
    screen results, and the learning DB silently vanish on every deploy/restart."""
    root = os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if not root:
        return
    data.setdefault("data", {})["cache_dir"] = root
    learn = data.setdefault("learn", {})
    db = learn.get("db_path")
    if not db or not Path(db).is_absolute():
        learn["db_path"] = str(Path(root) / (Path(db).name if db else "opportunities.db"))
    print(f"[config] persistent data dir -> {root} (mounted volume)")


class Config:
    """Thin wrapper over the parsed YAML so callers can do cfg.get("profile.atr_mult")."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _apply_data_dir_override(data)
        return cls(data)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        node = self._data.get(name, {})
        return node if isinstance(node, dict) else {}

    @property
    def raw(self) -> dict[str, Any]:
        return self._data
