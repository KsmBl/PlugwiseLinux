# SPDX-License-Identifier: GPL-3.0-or-later
"""Friendly names for plugs, stored in ~/.config/plugwise/config.toml:

    port = "/dev/plugwise"          # optional

    [names]
    lamp = "000D6F0000D34169"
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from .errors import PlugwiseError
from .protocol import norm_mac

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "plugwise" / "config.toml"


class Config:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_path()
        self.port: str | None = None
        self.names: dict[str, str] = {}
        if self.path.exists():
            try:
                data = tomllib.loads(self.path.read_text())
            except tomllib.TOMLDecodeError as e:
                raise PlugwiseError(f"bad config {self.path}: {e}") from None
            self.port = data.get("port")
            self.names = {k: norm_mac(v) for k, v in data.get("names", {}).items()}

    def resolve(self, name_or_mac: str) -> str:
        """Name -> MAC. Also accepts a full MAC, or a unique MAC suffix
        (e.g. 'D34169') among named plugs."""
        if name_or_mac in self.names:
            return self.names[name_or_mac]
        try:
            return norm_mac(name_or_mac)
        except PlugwiseError:
            pass
        suffix = name_or_mac.upper()
        hits = {m for m in self.names.values() if m.endswith(suffix)}
        if len(hits) == 1:
            return hits.pop()
        raise PlugwiseError(f"unknown plug {name_or_mac!r} "
                            f"(known: {', '.join(self.names) or 'none'})")

    def name_of(self, mac: str) -> str | None:
        for n, m in self.names.items():
            if m == mac:
                return n
        return None

    def set_name(self, name: str, mac: str) -> None:
        if not NAME_RE.match(name):
            raise PlugwiseError("names may only contain letters, digits, - and _")
        mac = norm_mac(mac)
        # one name per plug
        self.names = {n: m for n, m in self.names.items() if m != mac}
        self.names[name] = mac
        self.save()

    def remove_name(self, name: str) -> None:
        if name not in self.names:
            raise PlugwiseError(f"no plug named {name!r}")
        del self.names[name]
        self.save()

    def save(self) -> None:
        lines = []
        if self.port:
            lines.append(f'port = "{self.port}"\n')
        lines.append("[names]")
        lines += [f'{n} = "{m}"' for n, m in sorted(self.names.items())]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(self.path)
