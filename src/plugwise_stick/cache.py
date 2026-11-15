"""Persistent copy of the Circle+ node table.

Reading the table from the Circle+ takes 64 radio round trips (5-12 s), and it
only changes when nodes join or are removed, so it is cached on disk:

    ~/.cache/plugwise/nodes.json
    {"circle_plus": "000D6F...", "updated": "2026-...", "nodes": [[1, "000D6F..."]]}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def default_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "plugwise" / "nodes.json"


class NodeCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self, circle_plus: str) -> list[tuple[int, str]] | None:
        """Cached table for this Circle+, or None if missing/other network."""
        try:
            data = json.loads(self.path.read_text())
            if data.get("circle_plus") != circle_plus:
                return None
            return [(int(slot), str(mac)) for slot, mac in data["nodes"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def updated(self) -> datetime | None:
        try:
            return datetime.fromisoformat(json.loads(self.path.read_text())["updated"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, circle_plus: str, nodes: list[tuple[int, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "circle_plus": circle_plus,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "nodes": [[slot, mac] for slot, mac in nodes],
        }, indent=1) + "\n")
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
