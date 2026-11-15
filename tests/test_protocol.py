# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol codec tests, using frames recorded from real hardware (2026-11-15)."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugwise_stick import protocol as P  # noqa: E402
from plugwise_stick.errors import PlugwiseError  # noqa: E402

CIRCLE_PLUS = "000D6F0004465C94"
CIRCLE = "000D6F0000D34169"
CAL = P.Calibration(1.0044513940811157, -9.768780273589073e-07,
                    0.08073636889457703, 0.0)


def parse(body: bytes) -> P.Frame:
    """Parse one recorded frame body (incl. CRC)."""
    frames = P.FrameParser().feed(P.HEADER + body + P.FOOTER)
    assert len(frames) == 1, frames
    return frames[0]


class TestFraming(unittest.TestCase):
    def test_crc_known_init_frame(self):
        self.assertEqual(P.crc16(b"000A"), "B43C")

    def test_build_ping_frame(self):
        self.assertEqual(P.build_frame("000D", CIRCLE),
                         b"\x05\x05\x03\x03000D000D6F0000D341695015\r\n")

    def test_build_remove_frame(self):
        self.assertEqual(P.build_frame("001C", CIRCLE_PLUS + CIRCLE),
                         b"\x05\x05\x03\x03001C000D6F0004465C94000D6F0000D34169B111\r\n")

    def test_parse_ack(self):
        f = parse(b"0000000100C1FEED")
        self.assertEqual((f.id, f.seq, f.payload), ("0000", "0001", "00C1"))

    def test_bad_crc_dropped(self):
        noise = []
        p = P.FrameParser(on_noise=lambda line, why: noise.append(why))
        self.assertEqual(p.feed(P.HEADER + b"0000000100C1FEEE" + P.FOOTER), [])
        self.assertEqual(noise, ["bad crc"])

    def test_debug_lines_and_split_reads(self):
        # stick debug output, then a frame delivered in two chunks
        p = P.FrameParser()
        self.assertEqual(p.feed(b"\x83# SENDING PING UNICAST\r\n\x05\x05\x03\x03"
                                b"000001E1"), [])
        frames = p.feed(b"00D8000D6F0004465C942C90\r\n")
        self.assertEqual(frames, [P.Frame("0000", "01E1", "00D8" + CIRCLE_PLUS)])


class TestDecoders(unittest.TestCase):
    def test_stick_init(self):
        f = parse(b"00110001000D6F0003C9E1C10101EB0D6F0004465C946FEBFF0766")
        info = P.decode_stick_init(f.payload)
        self.assertEqual(info.mac, "000D6F0003C9E1C1")
        self.assertTrue(info.network_online)
        self.assertEqual(info.circle_plus_mac, CIRCLE_PLUS)
        self.assertEqual(info.network_id, "EB0D6F0004465C94")
        self.assertEqual(info.short_pan_id, "6FEB")

    def test_node_info(self):
        f = parse(b"00240011000D6F0004465C94000100020004400001856539070073304E0843A9019B02")
        n = P.decode_node_info(f.payload)
        self.assertEqual(n.mac, CIRCLE_PLUS)
        self.assertEqual(n.clock, datetime(2000, 1, 1, 0, 2, tzinfo=timezone.utc))
        self.assertEqual(n.last_log_address, 0)
        self.assertTrue(n.relay_on)
        self.assertEqual(n.hz, 50)
        self.assertEqual(n.hw_version, "6539-0700-7330")
        self.assertEqual(n.fw_date, "2011-06-27")
        self.assertEqual(n.node_type, "Circle+")

    def test_calibration(self):
        f = parse(b"002700EE000D6F0004465C943F8091DDB5831D463DA5591C000000000F02")
        c = P.decode_calibration(f.payload)
        self.assertAlmostEqual(c.gain_a, CAL.gain_a)
        self.assertAlmostEqual(c.gain_b, CAL.gain_b)
        self.assertAlmostEqual(c.off_tot, CAL.off_tot)
        self.assertEqual(c.off_noise, 0.0)

    def test_power(self):
        # 1 pulse/1s, 4 pulses/8s -> values observed with this calibration
        r = P.decode_power(CIRCLE_PLUS + "0001" + "0004" + "00000000", CAL)
        self.assertEqual((r.watts_1s, r.watts_8s), (2.31, 1.09))
        self.assertEqual(P.decode_power(CIRCLE_PLUS + "0000FFFF", CAL).watts_8s, 0.0)

    def test_table_entries(self):
        f = parse(b"001900EC000D6F0004465C94FFFFFFFFFFFFFFFF021606")
        self.assertEqual(P.decode_table_entry(f.payload), (2, None))
        self.assertEqual(P.decode_table_entry(CIRCLE_PLUS + CIRCLE + "01"), (1, CIRCLE))

    def test_node_remove(self):
        f = parse(b"001D00F7000D6F0004465C94000D6F0000D3416901EEA9")
        self.assertEqual(P.decode_node_remove(f.payload), (CIRCLE, True))

    def test_clocks(self):
        # recorded payloads of 003F / 003A replies
        c = P.decode_node_clock("000D6F0004465C94002F310101457A")
        self.assertEqual((c.hour, c.minute, c.second, c.weekday), (0, 47, 49, 1))
        rtc = P.decode_rtc("000D6F0004465C9449470001010100")
        self.assertEqual(rtc.time, datetime(2000, 1, 1, 0, 47, 49, tzinfo=timezone.utc))

    def test_ping(self):
        r = P.decode_ping(CIRCLE_PLUS + "B7BA0045")
        self.assertEqual((r.rssi_in, r.rssi_out, r.ping_ms), (-73, -70, 69))


class TestEnergyLog(unittest.TestCase):
    # recorded 0049 payloads (2026-11-15)
    GREY = ("000D6F0004465C941A0B52800000088A" + "F" * 48 + "00044000")
    WHITE = ("000D6F0000D34169130B0011000000001A0B528000000000"
             + "F" * 32 + "00044000")

    def test_decode_grey_hourly_record(self):
        idx, entries = P.decode_energy_log(self.GREY)
        self.assertEqual(idx, 0)
        self.assertEqual(entries, [P.LogEntry(
            datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc), 2186)])

    def test_decode_skips_empty_keeps_old(self):
        _, entries = P.decode_energy_log(self.WHITE)
        self.assertEqual([e.end.year for e in entries], [2019, 2026])

    def test_log_address(self):
        self.assertEqual(P.log_address(0), "00044000")
        self.assertEqual(P.log_address(1), "00044020")
        self.assertEqual(P.log_address(-1), P.log_address(P.LOG_ADDRESSES - 1))

    def test_hour_energy(self):
        # 2186 pulses in one hour -> 1.3 Wh with the grey plug's calibration
        self.assertAlmostEqual(P.pulses_to_wh(2186, 3600, CAL), 1.3007, places=3)

    def test_power_hour_counter(self):
        r = P.decode_power("000D6F0004465C94000000040000020800000000000E", CAL)
        self.assertEqual(r.pulses_this_hour, 520)
        self.assertIsNotNone(r.wh_this_hour)

    def test_log_interval_payload(self):
        self.assertEqual(P.encode_log_interval(CIRCLE, 1, 60), CIRCLE + "0001003C")
        with self.assertRaises(PlugwiseError):
            P.encode_log_interval(CIRCLE, 0, 60)


class TestEncoders(unittest.TestCase):
    DT = datetime(2026, 11, 15, 15, 31, 40, tzinfo=timezone.utc)  # a Sunday

    def test_clock_set(self):
        minutes = 14 * 1440 + 15 * 60 + 31
        self.assertEqual(P.encode_clock_set(CIRCLE, self.DT),
                         CIRCLE + f"1A0B{minutes:04X}" + "FFFFFFFF" + "0F1F28" + "06")

    def test_rtc_set_roundtrip(self):
        payload = P.encode_rtc_set(CIRCLE_PLUS, self.DT)
        self.assertEqual(payload, CIRCLE_PLUS + "403115" + "06" + "151126")
        self.assertEqual(P.decode_rtc(payload).time, self.DT)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            P.encode_clock_set(CIRCLE, datetime(2026, 1, 1))

    def test_norm_mac(self):
        self.assertEqual(P.norm_mac("00:0d:6f:00:00:d3:41:69"), CIRCLE)
        with self.assertRaises(PlugwiseError):
            P.norm_mac("D34169")


if __name__ == "__main__":
    unittest.main()
