"""Command-line front-end. Plugs can be given by name, full MAC, or a unique
MAC suffix of a named plug. Use --json for machine-readable output."""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from datetime import datetime, timezone

from .cache import default_path as cache_path
from .config import Config
from .errors import PlugwiseError
from .stick import Stick


def _plain(obj):
    if dataclasses.is_dataclass(obj):
        return {k: _plain(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


class Output:
    def __init__(self, as_json: bool):
        self.json = as_json

    def obj(self, data, human=None):
        if self.json:
            print(json.dumps(_plain(data)), flush=True)
        elif human is not None:
            print(human, flush=True)
        else:
            for k, v in _plain(data).items():
                print(f"{k:18} {v}")


ALIASES = {"scan": "sync-nodes", "sync": "sync-clocks"}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="plugwise", description=__doc__)
    ap.add_argument("-p", "--port", help="serial port (default: auto-detect)")
    ap.add_argument("-c", "--config", help="config file "
                    "(default: ~/.config/plugwise/config.toml)")
    ap.add_argument("-j", "--json", action="store_true", help="JSON output")
    ap.add_argument("-d", "--debug", action="store_true", help="dump raw frames")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("stick", help="stick and network info")
    sub.add_parser("sync-nodes", aliases=["scan"],
                   help="re-read the plug list from the Circle+, show changes")
    sub.add_parser("status", help="overview of all plugs").add_argument(
        "--rescan", action="store_true", help="re-read the plug list first")
    for name, h in [("info", "node info"), ("ping", "ping a plug"),
                    ("cal", "calibration data"), ("power", "current power (W)"),
                    ("on", "switch relay on"), ("off", "switch relay off"),
                    ("clock", "show a plug's clock")]:
        sub.add_parser(name, help=h).add_argument("plug")
    sp = sub.add_parser("sync-clocks", aliases=["sync"],
                        help="set plug clocks (+ Circle+ RTC) to now")
    sp.add_argument("plug", nargs="*", help="default: all")
    sp = sub.add_parser("remove", help="remove a node from the Circle+ table")
    sp.add_argument("plug")
    sp.add_argument("-y", "--yes", action="store_true", help="don't ask")

    sp = sub.add_parser("names", help="list plug names")
    sp = sub.add_parser("name", help="name a plug (or --delete a name)")
    sp.add_argument("plug", help="MAC (or existing name)")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--delete", action="store_true")

    sp = sub.add_parser("join", help="pair new plugs")
    sp.add_argument("plug", nargs="*", help="only accept these MACs")
    sp.add_argument("-t", "--time", type=float, default=120)
    sp = sub.add_parser("listen", help="print unsolicited frames")
    sp.add_argument("-t", "--time", type=float, default=60)
    sp = sub.add_parser("watch", help="poll power periodically")
    sp.add_argument("plug", nargs="*", help="default: all named plugs")
    sp.add_argument("-i", "--interval", type=float, default=2.0)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    a.cmd = ALIASES.get(a.cmd, a.cmd)
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.WARNING,
                        format="[%(levelname)s] %(message)s", stream=sys.stderr)
    out = Output(a.json)
    try:
        cfg = Config(a.config)
        label = lambda mac: cfg.name_of(mac) or mac  # noqa: E731

        # commands that don't need the stick
        if a.cmd == "names":
            out.obj(cfg.names, "\n".join(f"{n:16} {m}" for n, m in
                                        sorted(cfg.names.items())) or "(no names)")
            return 0
        if a.cmd == "name":
            if a.delete:
                cfg.remove_name(a.plug)
            elif not a.name:
                raise PlugwiseError("usage: plugwise name <mac> <name>")
            else:
                cfg.set_name(a.name, cfg.resolve(a.plug))
            out.obj(cfg.names, f"saved to {cfg.path}")
            return 0

        with Stick(a.port or cfg.port, node_cache=cache_path()) as s:
            net = s.connect()
            if a.cmd == "stick":
                out.obj(net)
            elif a.cmd == "sync-nodes":
                diff = s.sync_nodes()
                present = {s.circle_plus} | {m for _, m in diff.nodes}
                stale = {n: m for n, m in cfg.names.items() if m not in present}
                unnamed = [m for m in sorted(present) if not cfg.name_of(m)]
                lines = [f"Circle+  {s.circle_plus}  {cfg.name_of(s.circle_plus) or ''}"]
                lines += [f"slot {i:2d}  {m}  {cfg.name_of(m) or ''}"
                          for i, m in diff.nodes]
                if diff.first_sync:
                    lines.append("(first sync: plug list saved)")
                elif not (diff.added or diff.removed):
                    lines.append("no changes")
                lines += [f"+ added    slot {i:2d}  {m}" for i, m in diff.added]
                lines += [f"- removed  slot {i:2d}  {label(m)}" for i, m in diff.removed]
                lines += [f"! name '{n}' -> {m} is not in this network "
                          f"(plugwise name --delete {n})" for n, m in stale.items()]
                lines += [f"? unnamed  {m}  (plugwise name {m} <name>)" for m in unnamed]
                out.obj({"circle_plus": s.circle_plus,
                         "nodes": [{"slot": i, "mac": m, "name": cfg.name_of(m)}
                                   for i, m in diff.nodes],
                         "added": [m for _, m in diff.added],
                         "removed": [m for _, m in diff.removed],
                         "first_sync": diff.first_sync,
                         "stale_names": stale, "unnamed": unnamed},
                        "\n".join(lines))
            elif a.cmd == "status":
                rows = s.status(refresh=a.rescan)
                data = [{**_plain(r), "name": cfg.name_of(r.mac)} for r in rows]
                lines = [f"{'NAME':12} {'MAC':16} {'TYPE':8} {'STATE':7} "
                         f"{'RELAY':5} {'POWER':>9}  CLOCK (UTC)"]
                for r in rows:
                    lines.append(
                        f"{cfg.name_of(r.mac) or '-':12} {r.mac:16} "
                        f"{r.node_type or '?':8} {'online' if r.online else 'OFFLINE':7} "
                        f"{'' if r.relay_on is None else ('on' if r.relay_on else 'off'):5} "
                        f"{'' if r.watts is None else f'{r.watts:.1f} W':>9}  "
                        f"{r.clock.strftime('%Y-%m-%d %H:%M') if r.clock else ''}")
                out.obj(data, "\n".join(lines))
            elif a.cmd == "info":
                out.obj(s.info(cfg.resolve(a.plug)))
            elif a.cmd == "ping":
                out.obj(s.ping(cfg.resolve(a.plug)))
            elif a.cmd == "cal":
                out.obj(s.calibration(cfg.resolve(a.plug)))
            elif a.cmd == "power":
                out.obj(s.power(cfg.resolve(a.plug)))
            elif a.cmd in ("on", "off"):
                mac = cfg.resolve(a.plug)
                state = s.switch(mac, a.cmd == "on")
                out.obj({"mac": mac, "relay_on": state}, "on" if state else "off")
            elif a.cmd == "clock":
                mac = cfg.resolve(a.plug)
                c = s.get_clock(mac)
                inf = s.info(mac)
                d = {"mac": mac, "time": f"{c.hour:02d}:{c.minute:02d}:{c.second:02d}",
                     "weekday": c.weekday,
                     "date_time": inf.clock}
                if mac == s.circle_plus:
                    d["rtc"] = s.get_rtc().time
                out.obj(d)
            elif a.cmd == "sync-clocks":
                macs = [cfg.resolve(p) for p in a.plug] or None
                res = s.sync_clocks(macs)
                out.obj(res, "\n".join(f"{label(m):16} {'ok' if e is None else e}"
                                       for m, e in res.items()))
                return 0 if all(e is None for e in res.values()) else 1
            elif a.cmd == "remove":
                mac = cfg.resolve(a.plug)
                if not a.yes:
                    if not sys.stdin.isatty():
                        raise PlugwiseError("refusing to remove without a terminal "
                                            "to confirm; pass --yes")
                    if input(f"Remove {label(mac)} from the Circle+? [y/N] ").lower() != "y":
                        return 1
                ok = s.remove(mac)
                out.obj({"mac": mac, "removed": ok}, "removed" if ok else "not removed")
                return 0 if ok else 1
            elif a.cmd == "listen":
                for f in s.listen(a.time):
                    out.obj(f, f"{time.strftime('%H:%M:%S')} {f}")
            elif a.cmd == "join":
                def ev(event, mac):
                    out.obj({"event": event, "mac": mac},
                            f"{time.strftime('%H:%M:%S')} {event:12} {mac}")
                if not a.json:
                    print(f"joining enabled for {a.time:.0f}s ...", flush=True)
                s.join([cfg.resolve(p) for p in a.plug], a.time, on_event=ev)
            elif a.cmd == "watch":
                macs = [cfg.resolve(p) for p in a.plug] or list(cfg.names.values()) \
                    or s.nodes()
                while True:
                    ts = datetime.now(timezone.utc)
                    row = {}
                    for m in macs:
                        try:
                            row[m] = s.power(m, retries=0).watts_8s
                        except PlugwiseError:
                            row[m] = None
                    out.obj({"time": ts, "watts": row},
                            time.strftime("%H:%M:%S ") + " | ".join(
                                f"{label(m)}: " + ("offline" if w is None else f"{w:7.1f} W")
                                for m, w in row.items()))
                    time.sleep(a.interval)
    except PlugwiseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
