#!/usr/bin/env python3
"""Userspace driver + CLI for Plugwise Stick / Circle+ / Circle (stdlib only).

Wire format (both directions):
    \\x05\\x05\\x03\\x03  <ASCII-hex body>  <CRC16-XMODEM of body, 4 hex>  \\r\\n
Request body:  <msg id 4 hex> <payload>
Response body: <msg id 4 hex> <seq 4 hex> <payload>
"""
import argparse
import os
import select
import struct
import sys
import termios
import time
from datetime import datetime, timedelta, timezone

HEADER = b"\x05\x05\x03\x03"
FOOTER = b"\r\n"
DEFAULT_PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A801VBMD-if00-port0"
PULSES_PER_KW_SECOND = 468.9385193

# Stick ack codes (message 0000)
ACK_SUCCESS = "00C1"
ACK_ERROR = "00C2"
ACK_TIMEOUT = "00E1"
ACK_RELAY_ON = "00D8"
ACK_RELAY_OFF = "00DE"
ACK_NAMES = {
    "00C1": "success", "00C2": "error", "00E1": "node did not respond",
    "00D8": "relay on", "00DE": "relay off", "00D7": "clock set",
    "00DD": "joining disabled", "00D6": "joining enabled", "00F9": "node joined",
}
NODE_TYPES = {0: "Stick", 1: "Circle+", 2: "Circle", 3: "Switch", 5: "Sense",
              6: "Scan", 8: "Stealth"}


class PlugwiseError(Exception):
    pass


def crc16(data: bytes) -> str:
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
    v = int(s, 16)
    bits = len(s) * 4
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def norm_mac(mac: str) -> str:
    mac = mac.replace(":", "").replace("-", "").upper()
    if len(mac) != 16 or any(c not in "0123456789ABCDEF" for c in mac):
        raise PlugwiseError(f"invalid MAC {mac!r} (need 16 hex digits)")
    return mac


class NodeTimeout(PlugwiseError):
    pass


class Frame:
    def __init__(self, msg_id: str, seq: str, payload: str):
        self.id, self.seq, self.payload = msg_id, seq, payload

    def __repr__(self):
        return f"<{self.id} seq={self.seq} {self.payload}>"


class Stick:
    def __init__(self, port=DEFAULT_PORT, debug=False):
        self.debug = debug
        if not os.path.exists(port):
            port = "/dev/ttyUSB0"
        try:
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            raise PlugwiseError(
                f"no permission for {port}; add yourself to its group "
                f"(e.g. 'sudo usermod -aG uucp $USER' and re-login)")
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0                                          # iflag
        attrs[1] = 0                                          # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag 8N1
        attrs[3] = 0                                          # lflag (raw)
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.buf = b""
        self.pending = []  # frames read but not yet consumed

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- low level -------------------------------------------------------
    def _log(self, *a):
        if self.debug:
            print("[dbg]", *a, file=sys.stderr)

    def send(self, msg_id: str, payload: str = ""):
        body = (msg_id + payload).encode()
        frame = HEADER + body + crc16(body).encode() + FOOTER
        self._log(">>", frame)
        os.write(self.fd, frame)

    def _read_frames(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            chunk = os.read(self.fd, 4096)
            self.buf += chunk
        while True:
            end = self.buf.find(FOOTER)
            if end < 0:
                break
            line, self.buf = self.buf[:end], self.buf[end + 2:]
            start = line.find(HEADER)
            if start < 0:
                if line.strip():
                    self._log("<< (noise)", line)
                continue
            body = line[start + 4:]
            self._log("<<", body)
            if len(body) < 12:
                continue
            data, crc = body[:-4], body[-4:].decode(errors="replace")
            if crc16(data) != crc:
                self._log("   CRC mismatch, dropped")
                continue
            s = data.decode()
            self.pending.append(Frame(s[0:4], s[4:8], s[8:]))

    def wait_for(self, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            for i, f in enumerate(self.pending):
                if pred(f):
                    return self.pending.pop(i)
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._read_frames(min(left, 0.2))

    def request(self, msg_id, payload="", reply_id=None, timeout=5.0, retries=3):
        """Like _request, but retry when the node doesn't answer (mesh may be
        re-routing, e.g. after a relaying Circle lost power)."""
        for attempt in range(retries + 1):
            try:
                return self._request(msg_id, payload, reply_id, timeout)
            except NodeTimeout:
                if attempt == retries:
                    raise
                self._log(f"node timeout, retry {attempt + 1}/{retries}")
                time.sleep(2 * (attempt + 1))

    def _request(self, msg_id, payload="", reply_id=None, timeout=5.0):
        """Send a request, wait for the stick ack, then (optionally) the reply."""
        self.pending.clear()
        self.send(msg_id, payload)
        ack = self.wait_for(lambda f: f.id == "0000", timeout)
        if ack is None:
            raise PlugwiseError(f"no ack from stick for {msg_id}")
        code = ack.payload[:4]
        if code == ACK_ERROR:
            raise PlugwiseError(f"stick reported error for {msg_id}")
        if reply_id is None:
            return ack
        # Some acks arrive as final result (e.g. relay); 00E1 = node timeout
        if code == ACK_TIMEOUT:
            raise NodeTimeout("node did not respond (offline / not paired?)")
        seq = ack.seq
        while True:
            f = self.wait_for(lambda f: f.seq == seq, timeout)
            if f is None:
                raise PlugwiseError(f"timeout waiting for {reply_id}")
            if f.id == reply_id:
                return f
            if f.id == "0000" and f.payload[:4] == ACK_TIMEOUT:
                raise NodeTimeout("node did not respond (offline / not paired?)")

    # ---- high level ------------------------------------------------------
    def init(self):
        r = self.request("000A", reply_id="0011")
        p = r.payload
        return {
            "stick_mac": p[0:16],
            "network_online": p[18:20] == "01",
            # extended PAN id; Circle+ MAC is this with first byte set to 00
            "network_id": p[20:36],
            "circle_plus_mac": "00" + p[22:36],
            "short_pan_id": p[36:40],
        }

    def ping(self, mac):
        r = self.request("000D", norm_mac(mac), reply_id="000E")
        p = r.payload
        return {"rssi_in": sint(p[16:18]), "rssi_out": sint(p[18:20]),
                "ping_ms": int(p[20:24], 16)}

    def info(self, mac):
        r = self.request("0023", norm_mac(mac), reply_id="0024")
        p = r.payload
        year, month, minutes = int(p[16:18], 16), int(p[18:20], 16), int(p[20:24], 16)
        try:
            clock = (datetime(2000 + year, max(month, 1), 1, tzinfo=timezone.utc)
                     + timedelta(minutes=minutes))
        except ValueError:
            clock = None
        fw = int(p[48:56], 16)
        return {
            "mac": p[0:16],
            "clock_utc": clock.isoformat() if clock else None,
            "last_log_address": (int(p[24:32], 16) - 278528) // 32,
            "relay": "on" if p[32:34] == "01" else "off",
            "hz": {133: 50, 197: 60}.get(int(p[34:36], 16), int(p[34:36], 16)),
            "hw_version": f"{p[36:40]}-{p[40:44]}-{p[44:48]}",
            "fw_date": datetime.fromtimestamp(fw, timezone.utc).date().isoformat(),
            "node_type": NODE_TYPES.get(int(p[56:58], 16), p[56:58]),
        }

    def calibration(self, mac):
        r = self.request("0026", norm_mac(mac), reply_id="0027")
        p = r.payload
        return {"gain_a": hexfloat(p[16:24]), "gain_b": hexfloat(p[24:32]),
                "off_tot": hexfloat(p[32:40]), "off_noise": hexfloat(p[40:48])}

    def power(self, mac, cal=None):
        cal = cal or self.calibration(mac)
        r = self.request("0012", norm_mac(mac), reply_id="0013")
        p = r.payload
        p1, p8 = sint(p[16:20]), sint(p[20:24])

        def correct(pulses, secs):
            if pulses in (0, -1):
                return 0.0
            v = pulses / secs + cal["off_noise"]
            return secs * (v * v * cal["gain_b"] + v * cal["gain_a"]) + cal["off_tot"]

        w1 = correct(p1, 1) / PULSES_PER_KW_SECOND * 1000
        w8 = correct(p8, 8) / 8 / PULSES_PER_KW_SECOND * 1000
        return {"watts_1s": round(max(w1, 0), 2), "watts_8s": round(max(w8, 0), 2),
                "pulses_1s": p1, "pulses_8s": p8}

    def switch(self, mac, on: bool, retries=3):
        for attempt in range(retries + 1):
            ack = self.request("0017", norm_mac(mac) + ("01" if on else "00"))
            code = ack.payload[:4]
            if code == ACK_SUCCESS:  # result follows in a second ack
                f = self.wait_for(lambda f: f.id == "0000" and f.seq == ack.seq
                                  and f.payload[:4] != ACK_SUCCESS, 5)
                code = f.payload[:4] if f else ACK_TIMEOUT
            if code != ACK_TIMEOUT:
                break
            self._log(f"node timeout, retry {attempt + 1}/{retries}")
            time.sleep(2 * (attempt + 1))
        if code == ACK_TIMEOUT:
            raise NodeTimeout("node did not respond")
        if code not in (ACK_RELAY_ON, ACK_RELAY_OFF, ACK_SUCCESS):
            raise PlugwiseError(f"unexpected ack {code} ({ACK_NAMES.get(code, '?')})")
        return "on" if code == ACK_RELAY_ON else "off" if code == ACK_RELAY_OFF else "?"

    def allow_joining(self, enable: bool):
        return self.request("0008", "01" if enable else "00")

    def listen(self, seconds, ids=None):
        """Yield unsolicited frames (optionally filtered by msg id)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            f = self.wait_for(lambda f: ids is None or f.id in ids,
                              min(1.0, deadline - time.monotonic()))
            if f:
                yield f

    def accept_node(self, mac):
        self.send("0007", "01" + norm_mac(mac))

    def remove_node(self, circle_plus_mac, mac):
        return self.request("001C", norm_mac(circle_plus_mac) + norm_mac(mac),
                            reply_id="001D")

    def scan(self, circle_plus_mac):
        """List nodes registered in the Circle+ (64 slots)."""
        nodes = []
        for idx in range(64):
            r = self.request("0018", norm_mac(circle_plus_mac) + f"{idx:02X}",
                             reply_id="0019")
            node = r.payload[16:32]
            if node not in ("FFFFFFFFFFFFFFFF", "0000000000000000"):
                nodes.append((idx, node))
        return nodes


# ---- CLI -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Plugwise Stick/Circle driver")
    ap.add_argument("-p", "--port", default=DEFAULT_PORT)
    ap.add_argument("-d", "--debug", action="store_true", help="dump raw frames")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stick", help="initialise stick and show network info")
    sub.add_parser("scan", help="list nodes registered in the Circle+")
    for name, h in [("info", "node info"), ("ping", "ping node"),
                    ("cal", "calibration data"), ("power", "current power usage"),
                    ("on", "switch relay on"), ("off", "switch relay off")]:
        sp = sub.add_parser(name, help=h)
        sp.add_argument("mac")
    j = sub.add_parser("join", help="allow joining and accept announcing nodes")
    j.add_argument("mac", nargs="*", help="only accept these MACs (default: any)")
    j.add_argument("-t", "--time", type=float, default=120)
    sub.add_parser("listen", help="print all unsolicited frames").add_argument(
        "-t", "--time", type=float, default=60)
    w = sub.add_parser("watch", help="poll power every N seconds")
    w.add_argument("mac", nargs="+")
    w.add_argument("-i", "--interval", type=float, default=2.0)
    a = ap.parse_args()

    try:
        with Stick(a.port, a.debug) as s:
            st = s.init()
            if a.cmd == "stick":
                show(st)
            elif a.cmd == "scan":
                if not st["network_online"]:
                    raise PlugwiseError("network offline")
                print(f"Circle+  {st['circle_plus_mac']}")
                for idx, mac in s.scan(st["circle_plus_mac"]):
                    print(f"slot {idx:2d} {mac}")
            elif a.cmd == "info":
                show(s.info(a.mac))
            elif a.cmd == "ping":
                show(s.ping(a.mac))
            elif a.cmd == "cal":
                show(s.calibration(a.mac))
            elif a.cmd == "power":
                show(s.power(a.mac))
            elif a.cmd in ("on", "off"):
                print(s.switch(a.mac, a.cmd == "on"))
            elif a.cmd == "listen":
                for f in s.listen(a.time):
                    print(time.strftime("%H:%M:%S"), f, flush=True)
            elif a.cmd == "join":
                want = {norm_mac(m) for m in a.mac}
                s.allow_joining(True)
                print(f"joining enabled for {a.time:.0f}s, waiting for announcements...")
                try:
                    for f in s.listen(a.time, ids={"0006"}):
                        mac = f.payload[:16]
                        print(time.strftime("%H:%M:%S"), "node announced:", mac, flush=True)
                        if not want or mac in want:
                            s.accept_node(mac)
                            print("  -> accepted, waiting for it to come online",
                                  flush=True)
                            # keep joining open while the node associates
                            deadline = time.monotonic() + 180
                            while time.monotonic() < deadline:
                                time.sleep(5)
                                try:
                                    s.ping(mac)
                                    print(time.strftime("%H:%M:%S"), "  -> online",
                                          flush=True)
                                    break
                                except PlugwiseError:
                                    pass
                            else:
                                print("  -> still not responding", flush=True)
                            want.discard(mac)
                            if a.mac and not want:
                                break
                finally:
                    s.allow_joining(False)
                    print("joining disabled")
            elif a.cmd == "watch":
                cals = {m: s.calibration(m) for m in a.mac}
                while True:
                    line = []
                    for m in a.mac:
                        try:
                            line.append(f"{m[-6:]}: {s.power(m, cals[m])['watts_8s']:8.2f} W")
                        except PlugwiseError as e:
                            line.append(f"{m[-6:]}: {e}")
                    print(time.strftime("%H:%M:%S"), " | ".join(line), flush=True)
                    time.sleep(a.interval)
    except PlugwiseError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


def show(d):
    for k, v in d.items():
        print(f"{k:18} {v}")


if __name__ == "__main__":
    main()
