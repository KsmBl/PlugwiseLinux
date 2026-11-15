# PlugwiseLinux

Linux userspace driver for the Plugwise **Stick**, **Circle+** and **Circle**
smart plugs, meant to be embedded in smart-home software, plus a CLI.

- Pure Python 3.11+ standard library: no dependencies, no kernel module
  (the Stick is an FTDI serial device handled by the in-kernel `ftdi_sio`).
- Typed results (dataclasses), typed exceptions, thread-safe `Stick` object.
- Automatic retries when a plug doesn't answer (the mesh re-routes when a
  relaying Circle loses power).
- Unit tests run without hardware, against frames recorded from real devices.

## Setup

Your user needs access to the serial port (group `uucp` on Arch, `dialout` on
Debian/Ubuntu), then log out and back in:

    sudo usermod -aG uucp $USER

Run from the checkout with `./plugwise.py ...`, or install the `plugwise`
command with `pip install --user .` (or `pipx install .`).

## CLI

Plugs can be given by **name**, full MAC, or a unique MAC suffix of a named plug.

    plugwise status                  # all plugs: online, relay, watts, clock
    plugwise name <mac> <name>       # give a plug a name (plugwise names to list)
    plugwise on|off <plug>           # switch relay
    plugwise power <plug>            # current power usage (W)
    plugwise watch [<plug>...]       # live power every 2 s
    plugwise sync [<plug>...]        # set clocks (and Circle+ RTC) to now, UTC
    plugwise clock <plug>            # show a plug's clock
    plugwise info|ping|cal <plug>    # details
    plugwise scan                    # nodes registered in the Circle+
    plugwise join [<mac>...]         # pair new plugs
    plugwise remove <plug>           # remove a node from the Circle+ table
    plugwise stick                   # stick / network info
    plugwise listen                  # dump unsolicited frames

Global options: `--json` (machine-readable output, one JSON object per line),
`-d` (dump raw protocol frames), `-p <port>`, `-c <config>`.

Names live in `~/.config/plugwise/config.toml`:

    port = "/dev/plugwise"   # optional, auto-detected otherwise

    [names]
    grey = "000D6F0004465C94"
    white = "000D6F0000D34169"

## Library

```python
from plugwise_stick import Stick, NodeTimeout

with Stick() as stick:              # port auto-detected
    net = stick.connect()           # StickInfo (circle_plus_mac, network_online, ...)
    for st in stick.status():       # NodeStatus per node
        print(st.mac, st.online, st.relay_on, st.watts)
    stick.switch("000D6F0000D34169", on=False)
    try:
        print(stick.power("000D6F0000D34169").watts_8s)
    except NodeTimeout:
        print("plug offline")
    stick.sync_clocks()
```

One `Stick` should own the serial port; it serialises requests internally, so
it can be shared between threads.

## Tests

    python3 -m unittest discover -s tests

## Layout

    src/plugwise_stick/protocol.py   framing, CRC, message ids, payload codecs (pure)
    src/plugwise_stick/transport.py  raw termios serial port
    src/plugwise_stick/stick.py      high-level API
    src/plugwise_stick/config.py     plug names
    src/plugwise_stick/cli.py        command-line front-end
