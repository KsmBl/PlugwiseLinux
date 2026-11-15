# SPDX-License-Identifier: GPL-3.0-or-later
"""High-level, thread-safe API for a Plugwise Stick and its Circles.

Example:
    from plugwise_stick import Stick
    with Stick() as stick:
        net = stick.connect()
        stick.switch("000D6F0000D34169", on=True)
        print(stick.power("000D6F0000D34169").watts_8s)
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import protocol as P
from .cache import NodeCache
from .errors import NodeTimeout, PlugwiseError, StickError
from .protocol import Ack, Msg, norm_mac
from .transport import SerialTransport

log = logging.getLogger("plugwise_stick")


@dataclass
class NodeStatus:
    """Summary of one node, as returned by Stick.status()."""
    mac: str
    slot: int | None             # Circle+ table slot (None for the Circle+)
    online: bool
    node_type: str | None = None
    relay_on: bool | None = None
    watts: float | None = None
    clock: datetime | None = None
    error: str | None = None


@dataclass
class EnergyInterval:
    """Energy measured by a plug in one log interval (start, end]."""
    start: datetime
    end: datetime
    wh: float                    # consumed; production intervals are negative


@dataclass
class EnergyReport:
    """Result of Stick.energy()."""
    mac: str
    since: datetime              # requested window start
    intervals: list[EnergyInterval]
    consumed_wh: float
    produced_wh: float
    interval_minutes: int | None # detected log interval


@dataclass
class TableDiff:
    """Result of Stick.sync_nodes()."""
    nodes: list[tuple[int, str]]          # current table (slot, mac)
    added: list[tuple[int, str]]
    removed: list[tuple[int, str]]
    first_sync: bool                      # no previous copy to compare with


class Stick:
    def __init__(self, port: str | None = None, *, transport=None,
                 retries: int = 3, timeout: float = 5.0, retry_delay: float = 2.0,
                 node_cache: str | Path | None = None):
        """port: serial device (auto-detected if None).
        transport: any object with write/read/close (overrides port; for tests).
        retries: how often to retry when a node doesn't answer.
        node_cache: JSON file to persist the Circle+ node table in (reading
            it from the Circle+ takes 5-12 s). None = keep it in memory only.
        """
        self.transport = transport or SerialTransport(port)
        self._cache = NodeCache(node_cache) if node_cache else None
        self._table: list[tuple[int, str]] | None = None
        self.retries = retries
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.info_cache: P.StickInfo | None = None
        self._cal: dict[str, P.Calibration] = {}
        self._pending: list[P.Frame] = []
        self._parser = P.FrameParser(
            on_noise=lambda line, why: log.debug("<< (%s) %r", why, line))
        self._lock = threading.RLock()

    # ---- lifecycle ---------------------------------------------------------
    def close(self):
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- low level ---------------------------------------------------------
    def send(self, msg_id: str, payload: str = "") -> None:
        frame = P.build_frame(msg_id, payload)
        log.debug(">> %r", frame)
        self.transport.write(frame)

    def _pump(self, timeout: float) -> None:
        data = self.transport.read(timeout)
        if data:
            for f in self._parser.feed(data):
                log.debug("<< %s", f)
                self._pending.append(f)

    def wait_for(self, pred: Callable[[P.Frame], bool],
                 timeout: float) -> P.Frame | None:
        deadline = time.monotonic() + timeout
        while True:
            for i, f in enumerate(self._pending):
                if pred(f):
                    return self._pending.pop(i)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._pump(min(left, 0.2))

    def _request_once(self, msg_id: str, payload: str, reply_id: str | None,
                      final_acks: tuple[str, ...]) -> P.Frame:
        self._pending.clear()
        self.send(msg_id, payload)
        ack = self.wait_for(lambda f: f.id == Msg.ACK, self.timeout)
        if ack is None:
            raise StickError(f"no ack from stick for {msg_id}")
        code = ack.payload[:4]
        if code == Ack.ERROR:
            raise StickError(f"stick reported error for {msg_id}")
        if code == Ack.TIMEOUT:
            raise NodeTimeout("node did not respond")
        if reply_id is None and not final_acks:
            return ack
        if code in final_acks:
            return ack
        seq = ack.seq
        while True:
            f = self.wait_for(lambda f: f.seq == seq, self.timeout)
            if f is None:
                raise NodeTimeout(f"no reply to {msg_id}")
            if f.id == Msg.ACK:
                c = f.payload[:4]
                if c == Ack.TIMEOUT:
                    raise NodeTimeout("node did not respond")
                if c in final_acks:
                    return f
                continue
            if f.id == reply_id:
                return f

    def request(self, msg_id: str, payload: str = "", reply_id: str | None = None,
                *, final_acks: tuple[str, ...] = (), retries: int | None = None) -> P.Frame:
        """Send a request and return the reply frame (or the final ack).

        Retries on NodeTimeout: the mesh may be re-routing, e.g. after a
        Circle that relayed traffic lost power.
        """
        retries = self.retries if retries is None else retries
        with self._lock:
            for attempt in range(retries + 1):
                try:
                    return self._request_once(msg_id, payload, reply_id, final_acks)
                except NodeTimeout:
                    if attempt == retries:
                        raise
                    log.debug("node timeout, retry %d/%d", attempt + 1, retries)
                    time.sleep(self.retry_delay * (attempt + 1))
        raise AssertionError("unreachable")

    # ---- stick / network ---------------------------------------------------
    def connect(self) -> P.StickInfo:
        """Initialise the stick; returns network info. Call this first."""
        r = self.request(Msg.STICK_INIT, reply_id=Msg.STICK_INIT_REPLY, retries=0)
        self.info_cache = P.decode_stick_init(r.payload)
        return self.info_cache

    @property
    def circle_plus(self) -> str:
        if self.info_cache is None:
            self.connect()
        if not self.info_cache.network_online:
            raise PlugwiseError("Plugwise network is offline (no Circle+ paired)")
        return self.info_cache.circle_plus_mac

    def read_table(self) -> list[tuple[int, str]]:
        """Read the node table from the Circle+ itself (64 requests, slow)."""
        cp = self.circle_plus
        nodes = []
        for slot in range(64):
            r = self.request(Msg.TABLE_ENTRY, cp + f"{slot:02X}",
                             reply_id=Msg.TABLE_ENTRY_REPLY)
            _, mac = P.decode_table_entry(r.payload)
            if mac:
                nodes.append((slot, mac))
        return nodes

    def table(self, refresh: bool = False) -> list[tuple[int, str]]:
        """Nodes registered in the Circle+ as (slot, mac).

        Served from memory / the node cache when available; refresh=True
        (or a missing cache) reads it from the Circle+ and updates the cache.
        """
        if not refresh and (known := self._known_table()) is not None:
            return known
        self._set_table(self.read_table())
        return list(self._table)

    def _known_table(self) -> list[tuple[int, str]] | None:
        """Table from memory or cache, without touching the radio."""
        if self._table is None and self._cache:
            self._table = self._cache.load(self.circle_plus)
        return None if self._table is None else list(self._table)

    def _set_table(self, nodes: list[tuple[int, str]]) -> None:
        self._table = sorted(nodes)
        if self._cache:
            self._cache.save(self.circle_plus, self._table)

    def sync_nodes(self) -> TableDiff:
        """Re-read the node table from the Circle+ and update the cache.
        Returns what changed compared to the previously known table."""
        old = self._known_table()
        new = self.table(refresh=True)
        old_macs = {m for _, m in old or []}
        new_macs = {m for _, m in new}
        return TableDiff(
            nodes=new,
            added=[(s, m) for s, m in new if old is not None and m not in old_macs],
            removed=[(s, m) for s, m in old or [] if m not in new_macs],
            first_sync=old is None,
        )

    def nodes(self, refresh: bool = False) -> list[str]:
        """All known node MACs: Circle+ first, then the table."""
        return [self.circle_plus] + [m for _, m in self.table(refresh)]

    def remove(self, mac: str) -> bool:
        """Remove a node from the Circle+ table. Returns True on success."""
        mac = norm_mac(mac)
        if mac == self.circle_plus:
            raise PlugwiseError("cannot remove the Circle+ from its own table")
        r = self.request(Msg.NODE_REMOVE, self.circle_plus + mac,
                         reply_id=Msg.NODE_REMOVE_REPLY)
        removed_mac, ok = P.decode_node_remove(r.payload)
        self._cal.pop(mac, None)
        ok = ok and removed_mac == mac
        if ok and (known := self._known_table()) is not None:
            self._set_table([(s, m) for s, m in known if m != mac])
        return ok

    # ---- joining -----------------------------------------------------------
    def allow_joining(self, enable: bool) -> None:
        self.request(Msg.ALLOW_JOINING, "01" if enable else "00",
                     final_acks=(Ack.JOINING_ENABLED, Ack.JOINING_DISABLED,
                                 Ack.SUCCESS), retries=0)

    def accept(self, mac: str) -> None:
        self.send(Msg.NODE_ACCEPT, "01" + norm_mac(mac))

    def listen(self, seconds: float,
               ids: set[str] | None = None) -> Iterator[P.Frame]:
        """Yield unsolicited frames for `seconds` (optionally filtered)."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            with self._lock:
                f = self.wait_for(lambda f: ids is None or f.id in ids, min(1.0, left))
            if f:
                yield f

    def join(self, macs: list[str] | None = None, seconds: float = 120,
             online_timeout: float = 180,
             on_event: Callable[[str, str], None] = lambda ev, mac: None) -> list[str]:
        """Open the network, accept announcing nodes (only `macs` if given),
        and wait until each accepted node answers. Returns joined MACs.
        on_event(event, mac) gets 'announced', 'accepted', 'online', 'no-response'.
        """
        want = {norm_mac(m) for m in macs or []}
        joined = []
        self.allow_joining(True)
        try:
            for f in self.listen(seconds, ids={Msg.NODE_ANNOUNCE}):
                mac = f.payload[:16]
                on_event("announced", mac)
                if want and mac not in want:
                    continue
                self.accept(mac)
                on_event("accepted", mac)
                # keep joining open while the node associates (can take minutes)
                deadline = time.monotonic() + online_timeout
                while time.monotonic() < deadline:
                    time.sleep(5)
                    try:
                        self.ping(mac, retries=0)
                        on_event("online", mac)
                        joined.append(mac)
                        break
                    except NodeTimeout:
                        pass
                else:
                    on_event("no-response", mac)
                want.discard(mac)
                if macs and not want:
                    break
        finally:
            self.allow_joining(False)
        if joined:  # new nodes got slots we don't know: re-read the table
            self.table(refresh=True)
        return joined

    # ---- per node ----------------------------------------------------------
    def ping(self, mac: str, *, retries: int | None = None) -> P.PingResult:
        r = self.request(Msg.PING, norm_mac(mac), Msg.PING_REPLY, retries=retries)
        return P.decode_ping(r.payload)

    def info(self, mac: str, *, retries: int | None = None) -> P.NodeInfo:
        r = self.request(Msg.INFO, norm_mac(mac), Msg.INFO_REPLY, retries=retries)
        return P.decode_node_info(r.payload)

    def calibration(self, mac: str, *, refresh: bool = False,
                    retries: int | None = None) -> P.Calibration:
        mac = norm_mac(mac)
        if refresh or mac not in self._cal:
            r = self.request(Msg.CALIBRATION, mac, Msg.CALIBRATION_REPLY,
                             retries=retries)
            self._cal[mac] = P.decode_calibration(r.payload)
        return self._cal[mac]

    def power(self, mac: str, *, retries: int | None = None) -> P.PowerReading:
        mac = norm_mac(mac)
        cal = self.calibration(mac, retries=retries)
        r = self.request(Msg.POWER, mac, Msg.POWER_REPLY, retries=retries)
        return P.decode_power(r.payload, cal)

    def switch(self, mac: str, on: bool, *, retries: int | None = None) -> bool:
        """Switch the relay; returns the new relay state reported by the node."""
        r = self.request(Msg.RELAY, norm_mac(mac) + ("01" if on else "00"),
                         final_acks=(Ack.RELAY_ON, Ack.RELAY_OFF), retries=retries)
        return r.payload[:4] == Ack.RELAY_ON

    # ---- energy history ----------------------------------------------------
    def read_log(self, mac: str, index: int, *,
                 retries: int | None = None) -> list[P.LogEntry]:
        """Raw entries of one energy-log address (up to 4 records)."""
        r = self.request(Msg.ENERGY_LOG, norm_mac(mac) + P.log_address(index),
                         Msg.ENERGY_LOG_REPLY, retries=retries)
        return P.decode_energy_log(r.payload)[1]

    def energy(self, mac: str, minutes: float = 60, *,
               since: datetime | None = None) -> EnergyReport:
        """Energy history stored on the plug for the last `minutes`
        (or since `since`). Resolution = the plug's log interval (default
        60 min; see set_log_interval()). Intervals that end inside the window
        are included, so the first one may start slightly before it."""
        mac = norm_mac(mac)
        now = datetime.now(timezone.utc)
        since = since or now - timedelta(minutes=minutes)
        cal = self.calibration(mac)
        last = self.info(mac).last_log_address
        raw: list[P.LogEntry] = []
        for n in range(P.LOG_ADDRESSES):
            entries = self.read_log(mac, last - n)
            if not entries:
                if n == 0:
                    continue        # pointer may sit on a fresh, empty address
                break               # start of history
            raw += entries
            if min(e.end for e in entries) <= since:
                break
        # drop records outside the window and garbage from the future
        raw = sorted({(e.end, e.pulses) for e in raw
                      if since < e.end <= now + timedelta(minutes=2)})
        intervals, step_min = _to_intervals(raw, cal)
        return EnergyReport(
            mac, since, intervals,
            consumed_wh=round(sum(i.wh for i in intervals if i.wh > 0), 3),
            produced_wh=round(-sum(i.wh for i in intervals if i.wh < 0), 3),
            interval_minutes=step_min)

    def set_log_interval(self, mac: str, consumption_minutes: int,
                         production_minutes: int = 60) -> None:
        """How often the plug writes an energy-log record (default 60 min).
        Shorter intervals give finer history but the ring buffer
        (6016 x 4 records) covers less time: 1 min = ~16 days, 60 min = ~2.7 years."""
        self.request(Msg.LOG_INTERVAL,
                     P.encode_log_interval(mac, consumption_minutes, production_minutes),
                     final_acks=(Ack.LOG_INTERVAL_SET,))

    # ---- clocks ------------------------------------------------------------
    def get_clock(self, mac: str, *, retries: int | None = None) -> P.NodeClock:
        r = self.request(Msg.CLOCK_GET, norm_mac(mac), Msg.CLOCK_REPLY,
                         retries=retries)
        return P.decode_node_clock(r.payload)

    def set_clock(self, mac: str, dt: datetime | None = None, *,
                  retries: int | None = None) -> None:
        """Set a Circle's clock (default: now, UTC). Energy logs are kept."""
        dt = dt or datetime.now(timezone.utc)
        self.request(Msg.CLOCK_SET, P.encode_clock_set(mac, dt),
                     final_acks=(Ack.CLOCK_SET,), retries=retries)

    def get_rtc(self) -> P.RealTimeClock:
        """Read the Circle+ real-time clock (used for its schedules)."""
        r = self.request(Msg.RTC_GET, self.circle_plus, Msg.RTC_REPLY)
        return P.decode_rtc(r.payload)

    def set_rtc(self, dt: datetime | None = None) -> None:
        dt = dt or datetime.now(timezone.utc)
        self.request(Msg.RTC_SET, P.encode_rtc_set(self.circle_plus, dt),
                     final_acks=(Ack.RTC_SET,))

    def sync_clocks(self, macs: list[str] | None = None) -> dict[str, str | None]:
        """Set all node clocks + the Circle+ RTC to now (UTC).
        Returns {mac: None on success, or error text}."""
        result: dict[str, str | None] = {}
        for mac in macs or self.nodes():
            try:
                self.set_clock(mac, retries=1)
                result[mac] = None
            except PlugwiseError as e:
                result[mac] = str(e)
        if macs is None or self.circle_plus in macs:
            try:
                self.set_rtc()
            except PlugwiseError as e:
                result[self.circle_plus + " (rtc)"] = str(e)
        return result

    # ---- overview ----------------------------------------------------------
    def status(self, macs: list[str] | None = None, *,
               refresh: bool = False) -> list[NodeStatus]:
        """Online/relay/power summary for all (or the given) nodes.
        Offline nodes are tried only once, to keep this fast.
        refresh=True re-reads the node table from the Circle+ first."""
        slots = {}
        if macs is None:
            table = self.table(refresh)
            slots = {m: s for s, m in table}
            macs = [self.circle_plus] + [m for _, m in table]
        out = []
        for mac in map(norm_mac, macs):
            st = NodeStatus(mac=mac, slot=slots.get(mac), online=False)
            try:
                inf = self.info(mac, retries=0)
                st.online, st.node_type = True, inf.node_type
                st.relay_on, st.clock = inf.relay_on, inf.clock
                st.watts = self.power(mac, retries=1).watts_8s
            except PlugwiseError as e:
                st.error = str(e)
            out.append(st)
        return out


def _to_intervals(raw: list[tuple[datetime, int]],
                  cal: P.Calibration) -> tuple[list[EnergyInterval], int | None]:
    """Turn (end, pulses) records into intervals. Consumption (>= 0) and
    production (< 0) records are separate series; each record covers the time
    since the previous record of its series."""
    out: list[EnergyInterval] = []
    steps: list[float] = []
    for series in ([r for r in raw if r[1] >= 0], [r for r in raw if r[1] < 0]):
        diffs = [(b[0] - a[0]).total_seconds() for a, b in zip(series, series[1:])]
        steps += diffs
        default = min(diffs) if diffs else 3600.0
        for i, (end, pulses) in enumerate(series):
            secs = diffs[i - 1] if i > 0 else default
            # a gap (e.g. plug unpowered) shouldn't stretch the interval
            secs = min(secs, default) if default else secs
            wh = P.pulses_to_wh(pulses, secs, cal)
            out.append(EnergyInterval(end - timedelta(seconds=secs), end,
                                      round(wh if pulses >= 0 else -wh, 4)))
    out.sort(key=lambda i: (i.end, i.wh < 0))
    step = int(min(steps) // 60) if steps else None
    return out, step
