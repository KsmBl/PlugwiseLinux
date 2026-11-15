# SPDX-License-Identifier: GPL-3.0-or-later
"""Plugwise USB protocol: framing, CRC, message ids and payload codecs.

Pure functions only (no I/O), so everything here is unit-testable.

Wire format (both directions):
    \\x05\\x05\\x03\\x03  <ASCII-hex body>  <CRC16-XMODEM of body, 4 hex>  \\r\\n
Request body:  <msg id 4 hex> <payload>
Response body: <msg id 4 hex> <seq 4 hex> <payload>

Multi-byte numbers are big-endian hex. All clocks are handled in UTC.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import PlugwiseError

HEADER = b"\x05\x05\x03\x03"
FOOTER = b"\r\n"
PULSES_PER_KW_SECOND = 468.9385193
LOG_ADDR_OFFSET = 278528  # first energy-log memory address
EMPTY_MACS = ("FFFFFFFFFFFFFFFF", "0000000000000000")


class Msg:
    """Message identifiers."""
    ACK = "0000"
    NODE_ANNOUNCE = "0006"      # unjoined node asks to join (unsolicited)
    NODE_ACCEPT = "0007"
    ALLOW_JOINING = "0008"
    STICK_INIT = "000A"
    PING = "000D"
    PING_REPLY = "000E"
    STICK_INIT_REPLY = "0011"
    POWER = "0012"
    POWER_REPLY = "0013"
    CLOCK_SET = "0016"
    RELAY = "0017"
    TABLE_ENTRY = "0018"
    TABLE_ENTRY_REPLY = "0019"
    NODE_REMOVE = "001C"
    NODE_REMOVE_REPLY = "001D"
    INFO = "0023"
    INFO_REPLY = "0024"
    CALIBRATION = "0026"
    CALIBRATION_REPLY = "0027"
    RTC_SET = "0028"            # Circle+ real-time clock
    RTC_GET = "0029"
    RTC_REPLY = "003A"
    CLOCK_GET = "003E"
    CLOCK_REPLY = "003F"


class Ack:
    """Status codes carried in message 0000."""
    SUCCESS = "00C1"
    ERROR = "00C2"
    TIMEOUT = "00E1"            # node did not answer
    RELAY_ON = "00D8"
    RELAY_OFF = "00DE"
    CLOCK_SET = "00D7"
    RTC_SET = "00DF"
    JOINING_ENABLED = "00D9"
    JOINING_DISABLED = "00DD"
    NAMES = {
        "00C1": "success", "00C2": "error", "00E1": "node did not respond",
        "00D8": "relay on", "00DE": "relay off", "00D7": "clock set",
        "00DF": "real-time clock set", "00D9": "joining enabled",
        "00DD": "joining disabled",
    }


NODE_TYPES = {0: "Stick", 1: "Circle+", 2: "Circle", 3: "Switch", 5: "Sense",
              6: "Scan", 8: "Stealth", 9: "Stealth+"}


# ---- basic codecs ----------------------------------------------------------
def crc16(data: bytes) -> str:
    """CRC16-XMODEM (poly 0x1021, init 0) as 4 uppercase hex chars."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def hexfloat(s: str) -> float:
    return struct.unpack(">f", bytes.fromhex(s))[0]


def sint(s: str) -> int:
    """Signed big-endian hex integer of len(s)*4 bits."""
    v = int(s, 16)
    bits = len(s) * 4
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def norm_mac(mac: str) -> str:
    m = mac.replace(":", "").replace("-", "").strip().upper()
    if len(m) != 16 or any(c not in "0123456789ABCDEF" for c in m):
        raise PlugwiseError(f"invalid MAC {mac!r} (need 16 hex digits)")
    return m


# ---- framing ---------------------------------------------------------------
@dataclass
class Frame:
    id: str
    seq: str
    payload: str

    def __str__(self):
        return f"<{self.id} seq={self.seq} {self.payload}>"


def build_frame(msg_id: str, payload: str = "") -> bytes:
    body = (msg_id + payload).encode()
    return HEADER + body + crc16(body).encode() + FOOTER


class FrameParser:
    """Incremental parser: feed() raw bytes, get back complete frames.

    Lines without header (the stick emits '# ...' debug text) and frames with
    a bad CRC are reported through on_noise and dropped.
    """

    def __init__(self, on_noise=None):
        self.buf = b""
        self.on_noise = on_noise or (lambda line, why: None)

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        frames = []
        while (end := self.buf.find(FOOTER)) >= 0:
            line, self.buf = self.buf[:end], self.buf[end + len(FOOTER):]
            start = line.find(HEADER)
            if start < 0:
                if line.strip():
                    self.on_noise(line, "noise")
                continue
            body = line[start + len(HEADER):]
            if len(body) < 12:
                self.on_noise(body, "short")
                continue
            data_part, crc = body[:-4], body[-4:]
            if crc16(data_part).encode() != crc:
                self.on_noise(body, "bad crc")
                continue
            s = data_part.decode("ascii", errors="replace")
            frames.append(Frame(s[0:4], s[4:8], s[8:]))
        return frames


# ---- typed results ---------------------------------------------------------
@dataclass
class StickInfo:
    mac: str
    network_online: bool
    network_id: str          # extended PAN id
    circle_plus_mac: str
    short_pan_id: str


@dataclass
class NodeInfo:
    mac: str
    clock: datetime | None   # node clock (UTC); minute resolution
    last_log_address: int
    relay_on: bool
    hz: int
    hw_version: str
    fw_date: str
    node_type: str


@dataclass
class Calibration:
    gain_a: float
    gain_b: float
    off_tot: float
    off_noise: float


@dataclass
class PowerReading:
    mac: str
    watts_1s: float
    watts_8s: float
    pulses_1s: int
    pulses_8s: int


@dataclass
class PingResult:
    mac: str
    rssi_in: int
    rssi_out: int
    ping_ms: int


@dataclass
class NodeClock:
    mac: str
    hour: int
    minute: int
    second: int
    weekday: int             # 0 = Monday


@dataclass
class RealTimeClock:
    mac: str
    time: datetime           # Circle+ RTC (UTC)


# ---- payload decoders (payload = everything after seq, CRC stripped) -------
def decode_stick_init(p: str) -> StickInfo:
    # mac(16) unknown(2) online(2) network_id(16) short_pan(4) unknown(2)
    return StickInfo(
        mac=p[0:16],
        network_online=p[18:20] == "01",
        network_id=p[20:36],
        circle_plus_mac="00" + p[22:36],
        short_pan_id=p[36:40],
    )


def decode_node_info(p: str) -> NodeInfo:
    # mac(16) year(2) month(2) minutes(4) logaddr(8) relay(2) hz(2)
    # hw(12) fw-unixtime(8) type(2)
    year, month, minutes = int(p[16:18], 16), int(p[18:20], 16), int(p[20:24], 16)
    try:
        clock = (datetime(2000 + year, month, 1, tzinfo=timezone.utc)
                 + timedelta(minutes=minutes))
    except ValueError:
        clock = None
    hz_raw = int(p[34:36], 16)
    return NodeInfo(
        mac=p[0:16],
        clock=clock,
        last_log_address=(int(p[24:32], 16) - LOG_ADDR_OFFSET) // 32,
        relay_on=p[32:34] == "01",
        hz={133: 50, 197: 60}.get(hz_raw, hz_raw),
        hw_version=f"{p[36:40]}-{p[40:44]}-{p[44:48]}",
        fw_date=datetime.fromtimestamp(int(p[48:56], 16), timezone.utc).date().isoformat(),
        node_type=NODE_TYPES.get(int(p[56:58], 16), f"unknown({p[56:58]})"),
    )


def decode_calibration(p: str) -> Calibration:
    return Calibration(hexfloat(p[16:24]), hexfloat(p[24:32]),
                       hexfloat(p[32:40]), hexfloat(p[40:48]))


def pulses_to_watts(pulses: int, seconds: int, cal: Calibration) -> float:
    if pulses in (0, -1):           # -1 (0xFFFF) = not available
        return 0.0
    v = pulses / seconds + cal.off_noise
    corrected = seconds * (v * v * cal.gain_b + v * cal.gain_a) + cal.off_tot
    return max(corrected / seconds / PULSES_PER_KW_SECOND * 1000, 0.0)


def decode_power(p: str, cal: Calibration) -> PowerReading:
    # mac(16) pulses_1s(4) pulses_8s(4) [totals ...]
    p1, p8 = sint(p[16:20]), sint(p[20:24])
    return PowerReading(p[0:16], round(pulses_to_watts(p1, 1, cal), 2),
                        round(pulses_to_watts(p8, 8, cal), 2), p1, p8)


def decode_ping(p: str) -> PingResult:
    return PingResult(p[0:16], sint(p[16:18]), sint(p[18:20]), int(p[20:24], 16))


def decode_table_entry(p: str) -> tuple[int, str | None]:
    """0019: circle+ mac(16) node mac(16) slot(2) -> (slot, mac or None)."""
    node = p[16:32]
    return int(p[32:34], 16), (None if node in EMPTY_MACS else node)


def decode_node_remove(p: str) -> tuple[str, bool]:
    """001D: circle+ mac(16) node mac(16) status(2)."""
    return p[16:32], p[32:34] == "01"


def decode_node_clock(p: str) -> NodeClock:
    # mac(16) hour(2) minute(2) second(2) weekday(2) ...
    return NodeClock(p[0:16], int(p[16:18], 16), int(p[18:20], 16),
                     int(p[20:22], 16), int(p[22:24], 16))


def decode_rtc(p: str) -> RealTimeClock:
    # mac(16) sec min hour (decimal digits) weekday(2) day month year (decimal)
    sec, mi, hr = int(p[16:18]), int(p[18:20]), int(p[20:22])
    day, mon, yr = int(p[24:26]), int(p[26:28]), int(p[28:30])
    return RealTimeClock(p[0:16], datetime(2000 + yr, mon, day, hr, mi, sec,
                                           tzinfo=timezone.utc))


# ---- payload encoders ------------------------------------------------------
def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def encode_clock_set(mac: str, dt: datetime) -> str:
    """0016 payload: mac, year/month/minutes-of-month, keep log address
    (FFFFFFFF), hour/min/sec, weekday."""
    dt = _utc(dt)
    month_minutes = (dt.day - 1) * 1440 + dt.hour * 60 + dt.minute
    return (norm_mac(mac)
            + f"{dt.year - 2000:02X}{dt.month:02X}{month_minutes:04X}"
            + "FFFFFFFF"
            + f"{dt.hour:02X}{dt.minute:02X}{dt.second:02X}"
            + f"{dt.weekday():02X}")


def encode_rtc_set(mac: str, dt: datetime) -> str:
    """0028 payload: mac, sec/min/hour, weekday, day/month/year (decimal)."""
    dt = _utc(dt)
    return (norm_mac(mac)
            + f"{dt.second:02d}{dt.minute:02d}{dt.hour:02d}"
            + f"{dt.weekday():02X}"
            + f"{dt.day:02d}{dt.month:02d}{dt.year - 2000:02d}")
