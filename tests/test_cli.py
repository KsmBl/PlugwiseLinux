# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI tests: every command runs against a fake stick and only sends the
messages it should."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from plugwise_stick import Stick, cli  # noqa: E402
from test_stick import CIRCLE, CP, OLD, FakeStick  # noqa: E402

# messages that change the network or a plug; commands may only send the
# ones they are about
MUTATING = {"0007", "0008", "0016", "0017", "001C", "0028", "0057"}
EXPECTED = {
    ("on", "white"): {"0017"},
    ("off", "white"): {"0017"},
    ("log-interval", "white", "1"): {"0057"},
    ("sync-clocks", "grey", "white"): {"0016", "0028"},
    ("remove", "--yes", OLD): {"001C"},
}
READ_ONLY = [("stick",), ("status",), ("sync-nodes",), ("info", "white"),
             ("ping", "white"), ("cal", "white"), ("power", "white"),
             ("clock", "grey"), ("energy", "white"), ("energy", "grey", "--hours", "2")]


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.config = d / "config.toml"
        self.config.write_text(f'[names]\ngrey = "{CP}"\nwhite = "{CIRCLE}"\n')
        self.env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(d / "cache")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *args):
        fake = FakeStick()
        make = lambda port=None, **kw: Stick(  # noqa: E731
            transport=fake, timeout=0.3, retry_delay=0, node_cache=kw.get("node_cache"))
        out = io.StringIO()
        with mock.patch.object(cli, "Stick", make), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["-c", str(self.config), *args])
        sent = {m for m, _ in fake.sent} & MUTATING
        return rc, sent, out.getvalue()

    def test_read_only_commands_change_nothing(self):
        for args in READ_ONLY:
            with self.subTest(args=args):
                rc, sent, _ = self.run_cli(*args)
                self.assertEqual(rc, 0)
                self.assertEqual(sent, set())

    def test_mutating_commands_send_only_their_message(self):
        for args, expected in EXPECTED.items():
            with self.subTest(args=args):
                rc, sent, _ = self.run_cli(*args)
                self.assertEqual(rc, 0)
                self.assertEqual(sent, expected)

    def test_remove_without_terminal_refuses(self):
        with mock.patch.object(sys, "stdin", io.StringIO("y\n")):
            rc, sent, _ = self.run_cli("remove", OLD)
        self.assertEqual(rc, 1)
        self.assertEqual(sent, set())

    def test_json_output(self):
        rc, _, out = self.run_cli("--json", "off", "white")
        self.assertEqual(json.loads(out), {"mac": CIRCLE, "relay_on": False})


if __name__ == "__main__":
    unittest.main()
