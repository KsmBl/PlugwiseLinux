"""Stick request/response logic against a scripted fake transport."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugwise_stick import Config, NodeTimeout, PlugwiseError, Stick  # noqa: E402
from plugwise_stick import protocol as P  # noqa: E402

STICK = "000D6F0003C9E1C1"
CP = "000D6F0004465C94"
CIRCLE = "000D6F0000D34169"
OLD = "000D6F00045227D6"


def fr(body: str) -> bytes:
    """Build a response frame from a body without CRC."""
    return P.HEADER + body.encode() + P.crc16(body.encode()).encode() + P.FOOTER


class FakeStick:
    """Transport that answers requests like a stick with a Circle+, a Circle
    and one offline node in slot 0."""

    def __init__(self, relay_timeouts=0):
        self.seq = 0
        self.out = b""
        self.sent = []
        self.relay = {CP: True, CIRCLE: True}
        self.relay_timeouts = relay_timeouts   # first N relay requests time out

    def _seq(self):
        self.seq += 1
        return f"{self.seq:04X}"

    def write(self, data: bytes):
        body = data[len(P.HEADER):-6].decode()  # strip header, CRC, CRLF
        msg, payload = body[:4], body[4:]
        self.sent.append((msg, payload))
        s = self._seq()
        ack = lambda code: fr(f"0000{s}{code}")  # noqa: E731
        mac = payload[:16]
        if mac == OLD:
            self.out += ack("00C1") + ack("00E1")
            return
        if msg == "000A":
            self.out += ack("00C1") + fr(f"0011{s}{STICK}0101EB0D6F0004465C946FEBFF")
        elif msg == "0018":
            slot = int(payload[16:18], 16)
            node = {0: OLD, 1: CIRCLE}.get(slot, "FFFFFFFFFFFFFFFF")
            self.out += ack("00C1") + fr(f"0019{s}{CP}{node}{slot:02X}")
        elif msg == "0023":
            relay = "01" if self.relay[mac] else "00"
            typ = "01" if mac == CP else "02"
            self.out += ack("00C1") + fr(
                f"0024{s}{mac}1A0B5000" + "00044000" + relay + "85"
                + "653907007330" + "4E0843A9" + typ)
        elif msg == "0026":
            self.out += ack("00C1") + fr(
                f"0027{s}{mac}3F8091DDB5831D463DA5591C00000000")
        elif msg == "0012":
            self.out += ack("00C1") + fr(f"0013{s}{mac}00010004000000000000")
        elif msg == "0017":
            if self.relay_timeouts:
                self.relay_timeouts -= 1
                self.out += ack("00C1") + ack("00E1")
                return
            on = payload[16:18] == "01"
            self.relay[mac] = on
            self.out += ack("00C1") + fr(f"0000{s}{'00D8' if on else '00DE'}{mac}")
        elif msg == "0016":
            self.out += ack("00C1") + fr(f"0000{s}00D7{mac}")
        elif msg == "0028":
            self.out += ack("00C1") + fr(f"0000{s}00DF{mac}")
        elif msg == "001C":
            self.out += ack("00C1") + fr(f"001D{s}{payload}01")
        else:
            self.out += ack("00C2")

    def read(self, timeout):
        data, self.out = self.out, b""
        return data

    def close(self):
        pass


def make(**kw):
    fake = FakeStick(**kw)
    s = Stick(transport=fake, timeout=0.3, retry_delay=0)
    s.connect()
    return s, fake


class TestStick(unittest.TestCase):
    def test_connect(self):
        s, _ = make()
        self.assertEqual(s.circle_plus, CP)
        self.assertTrue(s.info_cache.network_online)

    def test_table_and_nodes(self):
        s, _ = make()
        self.assertEqual(s.table(), [(0, OLD), (1, CIRCLE)])
        self.assertEqual(s.nodes(), [CP, OLD, CIRCLE])

    def test_switch(self):
        s, fake = make()
        self.assertFalse(s.switch(CIRCLE, False))
        self.assertFalse(fake.relay[CIRCLE])
        self.assertTrue(s.switch(CIRCLE, True))

    def test_switch_retries_on_node_timeout(self):
        s, fake = make(relay_timeouts=2)
        self.assertTrue(s.switch(CP, True))
        self.assertEqual(sum(1 for m, _ in fake.sent if m == "0017"), 3)

    def test_switch_gives_up(self):
        s, _ = make(relay_timeouts=10)
        with self.assertRaises(NodeTimeout):
            s.switch(CP, True, retries=1)

    def test_offline_node(self):
        s, _ = make()
        with self.assertRaises(NodeTimeout):
            s.ping(OLD, retries=0)

    def test_power_caches_calibration(self):
        s, fake = make()
        self.assertEqual(s.power(CIRCLE).watts_8s, 1.09)
        s.power(CIRCLE)
        self.assertEqual(sum(1 for m, _ in fake.sent if m == "0026"), 1)

    def test_status(self):
        s, _ = make()
        rows = {r.mac: r for r in s.status()}
        self.assertTrue(rows[CP].online)
        self.assertEqual(rows[CP].node_type, "Circle+")
        self.assertEqual(rows[CIRCLE].node_type, "Circle")
        self.assertEqual(rows[CIRCLE].watts, 1.09)
        self.assertFalse(rows[OLD].online)
        self.assertEqual(rows[OLD].slot, 0)

    def test_sync_clocks(self):
        s, fake = make()
        res = s.sync_clocks()
        self.assertIsNone(res[CP])
        self.assertIsNone(res[CIRCLE])
        self.assertIsNotNone(res[OLD])
        self.assertIn(("0028",), [(m,) for m, _ in fake.sent])

    def test_set_clock_payload(self):
        s, fake = make()
        dt = datetime(2026, 11, 15, 15, 31, 40, tzinfo=timezone.utc)
        s.set_clock(CIRCLE, dt)
        self.assertEqual(fake.sent[-1], ("0016", P.encode_clock_set(CIRCLE, dt)))

    def test_remove(self):
        s, fake = make()
        self.assertTrue(s.remove(CIRCLE))
        self.assertEqual(fake.sent[-1], ("001C", CP + CIRCLE))
        with self.assertRaises(PlugwiseError):
            s.remove(CP)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_roundtrip(self):
        c = Config(self.path)
        c.set_name("master", CP)
        c.set_name("lamp", CIRCLE.lower())
        c2 = Config(self.path)
        self.assertEqual(c2.names, {"master": CP, "lamp": CIRCLE})
        self.assertEqual(c2.resolve("lamp"), CIRCLE)
        self.assertEqual(c2.resolve("d34169"), CIRCLE)      # unique suffix
        self.assertEqual(c2.resolve(CP), CP)
        self.assertEqual(c2.name_of(CIRCLE), "lamp")

    def test_rename_replaces_old_name(self):
        c = Config(self.path)
        c.set_name("a", CIRCLE)
        c.set_name("b", CIRCLE)
        self.assertEqual(c.names, {"b": CIRCLE})

    def test_errors(self):
        c = Config(self.path)
        with self.assertRaises(PlugwiseError):
            c.resolve("nope")
        with self.assertRaises(PlugwiseError):
            c.set_name("bad name", CIRCLE)
        with self.assertRaises(PlugwiseError):
            c.remove_name("nope")


if __name__ == "__main__":
    unittest.main()
