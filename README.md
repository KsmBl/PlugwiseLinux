# PlugwiseLinux

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

**A Linux driver for Plugwise smart plugs: the USB Stick, Circle+ and Circle.**
Switch plugs on and off, read live power usage and manage the plug network.
Use it from the command line, from scripts (JSON output), or embed it as a
Python library in your own smart-home software.

```
$ plugwise status
NAME         MAC              TYPE     STATE   RELAY     POWER  CLOCK (UTC)
grey         000D6F0004465C94 Circle+  online  on       41.3 W  2026-11-15 16:11
white        000D6F0000D34169 Circle   online  off       0.0 W  2026-11-15 16:11

$ plugwise off white
off
```

- **No dependencies.** Only the Python 3.11+ standard library, with no
  pyserial and no compiled code.
- **No kernel module.** The Stick is an FTDI USB-serial adapter, which the
  in-kernel `ftdi_sio` driver already supports. This project speaks the
  Plugwise protocol on top of it, in userspace.
- **Built for integration.** Typed results, typed exceptions, a thread-safe API,
  automatic retries, and `--json` output on every command.
- **Fast.** The plug list is cached, so a full `status` takes about 0.6 s instead
  of 12 s.
- **Tested without hardware.** The tests run against radio frames recorded
  from real devices.

---

## Contents

- [Supported hardware](#supported-hardware)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Command reference](#command-reference)
- [Using it as a library](#using-it-as-a-library)
- [Integrating with other software](#integrating-with-other-software)
- [Pairing plugs](#pairing-plugs)
- [Troubleshooting](#troubleshooting)
- [Protocol notes](#protocol-notes)
- [Development](#development)
- [Limitations and roadmap](#limitations-and-roadmap)

## Supported hardware

| Device | Role | Status |
|---|---|---|
| **Stick** | USB ZigBee radio, the PC's gateway into the network | supported |
| **Circle+** | Smart plug that also coordinates the network (one per network) | supported |
| **Circle** | Smart plug, a member of the network | supported |
| Stealth / Stealth+ | Built-in variants of Circle / Circle+ | should work (same protocol), untested |
| Sense, Scan, Switch | Battery-powered sensors | not supported yet |

Tested with Circle/Circle+ firmware from 2011-06-27, on Arch Linux with Python 3.14.

## How it works

```
 ┌───────────── your PC ──────────────┐
 │ smart-home software / scripts      │
 │        │ Python API or CLI --json  │
 │  plugwise_stick (this project)     │
 │        │ /dev/ttyUSB0, 115200 8N1  │
 │  ftdi_sio (in-kernel driver)       │
 └────────┼───────────────────────────┘
          │ USB
     ┌────┴────┐   ZigBee mesh   ┌──────────┐        ┌────────┐
     │  Stick  │ ~~~~~~~~~~~~~~~ │ Circle+  │ ~~~~~~ │ Circle │ ...
     └─────────┘                 └──────────┘        └────────┘
                                 network coordinator,
                                 holds the plug list
```

Every Circle relays radio traffic for the others. If a Circle loses power,
messages are briefly re-routed. The driver retries automatically while
that happens.

## Installation

**1. Give your user access to the serial port.** The group is `uucp` on Arch
and `dialout` on Debian/Ubuntu/Fedora. Log out and back in afterwards:

```sh
sudo usermod -aG uucp $USER        # or: dialout
```

Optionally, add a udev rule that gives the Stick a stable name. The driver
looks for `/dev/plugwise` first:

```sh
# /etc/udev/rules.d/99-plugwise.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="plugwise", MODE="0660", GROUP="uucp"
```

```sh
sudo udevadm control --reload && sudo udevadm trigger
```

> The vendor/product ID `0403:6001` is the generic FTDI FT232R chip. If other
> FTDI adapters are plugged in, match on `ATTRS{serial}` too (see
> `udevadm info -a /dev/ttyUSB0`).

**2. Get the code.** Either run it straight from the checkout:

```sh
git clone https://github.com/KsmBl/PlugwiseLinux.git
cd PlugwiseLinux
./plugwise.py status
```

or install the `plugwise` command:

```sh
pipx install .            # or: pip install --user .
plugwise status
```

## Quick start

```sh
plugwise stick                       # is the stick there, and is the network online?
plugwise sync-nodes                  # read the plug list from the Circle+ (about 7 s, cached afterwards)
plugwise name 000D6F0004465C94 grey  # give your plugs names
plugwise name 000D6F0000D34169 white
plugwise sync-clocks                 # set the plug clocks (they reset after a power loss)
plugwise status                      # overview
plugwise off white                   # switch
plugwise watch                       # live power of all named plugs
```

The MAC address is printed on the label of every Plugwise device.

## Command reference

Wherever a command takes a `<plug>`, you can give a **name**, a **full MAC**, or
a **unique MAC ending** of a named plug (`d34169`).

| Command | What it does |
|---|---|
| `status [--rescan]` | All plugs: online or offline, relay, watts, clock. `--rescan` re-reads the plug list first |
| `on <plug>` / `off <plug>` | Switch the relay. Prints the state the plug reports back |
| `power <plug>` | Current power in watts (1 s and 8 s averages, calibrated) |
| `watch [<plug>...] [-i SEC]` | Print power every few seconds. Default: all named plugs |
| `info <plug>` | Type, firmware, hardware version, relay, mains frequency, clock |
| `ping <plug>` | Signal strength (RSSI in and out) and round-trip time |
| `cal <plug>` | The plug's calibration constants |
| `clock <plug>` | The plug's clock, plus the real-time clock for the Circle+ |
| `sync-clocks [<plug>...]` | Set clocks (and the Circle+ real-time clock) to now, in UTC. Alias: `sync` |
| `sync-nodes` | Re-read the plug list from the Circle+, update the cache, and show added and removed plugs, stale names and unnamed plugs. Alias: `scan` |
| `join [<mac>...] [-t SEC]` | Open the network and pair new plugs. See [Pairing plugs](#pairing-plugs) |
| `remove <plug> [--yes]` | Remove a plug from the Circle+ list. Asks for confirmation |
| `name <plug> <name>` | Name a plug. `name <name> --delete` removes a name |
| `names` | List names |
| `stick` | Stick MAC, network ID, Circle+ MAC |
| `listen [-t SEC]` | Print every unsolicited radio frame (debugging) |

Global options:

| Option | |
|---|---|
| `-j`, `--json` | Machine-readable output: one JSON object per line |
| `-d`, `--debug` | Show every raw protocol frame on stderr |
| `-p`, `--port PORT` | Serial port (default: `/dev/plugwise`, then any FTDI device, then `/dev/ttyUSB0`) |
| `-c`, `--config FILE` | Config file (default: `~/.config/plugwise/config.toml`) |

Exit status: 0 on success, 1 on error (for example when a plug is offline), 130 when interrupted.

### Files

| File | Contents |
|---|---|
| `~/.config/plugwise/config.toml` | Plug names and an optional serial port |
| `~/.cache/plugwise/nodes.json` | Cached copy of the Circle+ plug list (safe to delete) |

```toml
# ~/.config/plugwise/config.toml
port = "/dev/plugwise"     # optional

[names]
grey = "000D6F0004465C94"
white = "000D6F0000D34169"
```

Both paths follow `$XDG_CONFIG_HOME` and `$XDG_CACHE_HOME`.

## Using it as a library

```python
from plugwise_stick import Stick, NodeTimeout

with Stick(node_cache="/var/lib/myapp/plugwise-nodes.json") as stick:
    net = stick.connect()                 # always call first
    print("Circle+:", net.circle_plus_mac, "online:", net.network_online)

    for st in stick.status():             # one NodeStatus per plug
        print(st.mac, st.node_type, st.online, st.relay_on, st.watts)

    lamp = "000D6F0000D34169"
    stick.switch(lamp, on=True)           # returns the new relay state
    try:
        reading = stick.power(lamp)       # PowerReading
        print(f"{reading.watts_8s:.1f} W")
    except NodeTimeout:
        print("lamp is offline")

    stick.sync_clocks()                   # all plugs + Circle+ real-time clock
```

### API overview

| Method | Returns |
|---|---|
| `connect()` | `StickInfo`: stick MAC, `network_online`, `circle_plus_mac`, network IDs |
| `status(macs=None, refresh=False)` | `list[NodeStatus]`: online, type, relay, watts, clock per plug |
| `switch(mac, on)` | `bool`: new relay state |
| `power(mac)` | `PowerReading`: `watts_1s`, `watts_8s`, raw pulses |
| `info(mac)` | `NodeInfo`: type, firmware, hardware, relay, Hz, clock |
| `ping(mac)` | `PingResult`: `rssi_in`, `rssi_out`, `ping_ms` |
| `calibration(mac, refresh=False)` | `Calibration` (cached per plug) |
| `table(refresh=False)` / `nodes()` | Circle+ plug list, `(slot, mac)` pairs / MACs (cached) |
| `sync_nodes()` | `TableDiff`: `nodes`, `added`, `removed`, `first_sync` |
| `join(macs=None, seconds=120, on_event=cb)` | `list[str]` of joined MACs |
| `remove(mac)` | `bool` |
| `get_clock(mac)` / `set_clock(mac, dt=None)` | `NodeClock` / `None` |
| `get_rtc()` / `set_rtc(dt=None)` | Circle+ real-time clock |
| `sync_clocks(macs=None)` | `{mac: None or error text}` |
| `listen(seconds, ids=None)` | iterator of raw frames |

### Errors

```
PlugwiseError          base class for everything
├── StickError         the stick itself failed (no ack, error reply, port problem)
└── NodeTimeout        a plug did not answer (offline, unpowered, mesh re-routing)
```

### Behaviour worth knowing

- **One process per Stick.** Only one program should have the serial port
  open. Within that program, a single `Stick` object can be shared between
  threads, because requests are serialised internally.
- **Retries.** When a plug doesn't answer, requests are retried 3 times with
  increasing delays (2, 4, 6 s). You can change this with
  `Stick(retries=..., retry_delay=...)` or `retries=` on a single call.
  `status()` tries offline plugs only once, to stay fast.
- **Times are UTC.** All datetimes are timezone-aware UTC. `set_clock()` rejects
  naive datetimes.
- **Plug list cache.** Pass `node_cache=` to keep the Circle+ plug list between
  runs. Without it, the list is only kept in memory for the object's lifetime.
- **Testing without hardware.** `Stick(transport=...)` accepts any object with
  `write(bytes)`, `read(timeout) -> bytes` and `close()`. That's how the
  test-suite fakes a stick.

## Integrating with other software

Not using Python? Every command supports `--json`, which prints one JSON
object per line:

```sh
$ plugwise --json status
[{"mac": "000D6F0004465C94", "slot": null, "online": true, "node_type": "Circle+",
  "relay_on": true, "watts": 41.3, "clock": "2026-11-15T16:11:00+00:00", "error": null, "name": "grey"}, ...]

$ plugwise --json off white
{"mac": "000D6F0000D34169", "relay_on": false}

$ plugwise --json watch -i 10       # a stream, one object per sample
{"time": "2026-11-15T16:20:00.123+00:00", "watts": {"000D6F0004465C94": 41.3, "000D6F0000D34169": 0.0}}
```

Errors go to stderr, and a non-zero exit status signals failure. This works well
with Home Assistant's `command_line` integration, Node-RED's exec node, cron jobs
and shell scripts. For continuous use, prefer a long-running process (the
library, or `watch`), because every CLI call opens the port and initialises the
stick again.

## Pairing plugs

A new Circle, or one that was paired with another network, must join the
Circle+ network.

1. **Reset the Circle.** This is only needed if it was paired elsewhere. Unplug
   it, then plug in and out several times: in about 2 s, out about 2 s, in
   about 2 s, out about 2 s, in about 8 s, out, then plug it in for good. A
   reset Circle takes about 4 s to switch on.
2. **Join it:**
   ```sh
   plugwise join 000D6F0000D34169      # MAC from the label; omit it to accept any plug
   ```
   The network opens, accepts the Circle when it announces itself, and stays
   open until the Circle answers.

> **Be patient.** A newly accepted Circle can take several minutes, in
> practice sometimes 10 or more, to start answering. `join` waits up to 3 minutes
> per plug. If it reports `no-response`, the Circle usually comes online a
> little later anyway. Check with `plugwise ping <mac>`, and run
> `plugwise sync-nodes` once it answers.

To remove a plug from the network: `plugwise remove <plug>`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no permission for /dev/ttyUSB0` | Your user isn't in the port's group. See [Installation](#installation) and re-login |
| `no Plugwise stick found` | Stick not plugged in. Check `lsusb` for `0403:6001` |
| `node did not respond` right after switching another plug | The mesh is re-routing (the plug that relayed the traffic lost power). The driver retries. If it still fails, wait a few seconds |
| A plug is always `OFFLINE` in `status` | It's unpowered, out of range, or not paired with this Circle+. Check `sync-nodes` |
| Plug clocks show 2000-01-01 | Plugs reset their clock on power loss. Run `plugwise sync-clocks` |
| `status` lists plugs that no longer exist | Run `plugwise sync-nodes` |
| `network is offline` | The Stick isn't paired with a Circle+ |
| Strange behaviour | Run the command with `-d` to see every frame sent and received |

## Protocol notes

The Plugwise protocol isn't officially documented. This implementation is
based on community reverse-engineering and was verified against real
devices. Summary:

```
frame   = 05 05 03 03 | body (ASCII hex) | CRC16-XMODEM(body), 4 hex | \r\n
request = <msg id:4> <payload>
reply   = <msg id:4> <seq:4> <payload>
```

The stick acknowledges every request with `0000 <seq> <code>`. The actual reply
from the plug follows with the same sequence number.

| Request → Reply | Purpose |
|---|---|
| `000A` → `0011` | Initialise stick, network info |
| `000D` → `000E` | Ping |
| `0012` → `0013` | Power (pulse counters) |
| `0016` → ack `00D7` | Set clock |
| `0017` → ack `00D8`/`00DE` | Relay on / off |
| `0018` → `0019` | Read one Circle+ plug-list slot (0–63) |
| `001C` → `001D` | Remove plug from Circle+ |
| `0023` → `0024` | Node info |
| `0026` → `0027` | Calibration |
| `0028` → ack `00DF` | Set Circle+ real-time clock |
| `0029` → `003A` | Read Circle+ real-time clock |
| `003E` → `003F` | Read clock |
| `0008` → ack `00D9`/`00DD` | Allow / disallow joining |
| `0006` (unsolicited) | Unpaired plug announces itself |
| `0007` | Accept plug |

Ack codes: `00C1` success, `00C2` error, `00E1` plug did not answer.

Power: pulses are corrected with the plug's calibration
(`gain_a`, `gain_b`, `off_tot`, `off_noise`), then
`watts = corrected pulses per second / 468.9385193 × 1000`.

Note that the Circle+ real-time clock stores its digits in decimal
(`49` = 49 seconds), while everything else is plain hex.

## Development

```sh
python3 -m unittest discover -s tests -v     # 38 tests, about 0.05 s, no hardware needed
```

```
plugwise.py                     run the CLI from a checkout
src/plugwise_stick/
  protocol.py                   framing, CRC, message ids, payload codecs (pure, no I/O)
  transport.py                  raw termios serial port
  stick.py                      high-level, thread-safe API
  cache.py                      on-disk plug-list cache
  config.py                     plug names
  cli.py                        command-line front-end
tests/
  test_protocol.py              codecs, against frames recorded from real devices
  test_stick.py                 request logic, retries, cache, against a fake stick
```

New message types go in `protocol.py` (decoder plus a test with a recorded frame),
then as a method on `Stick`, then as a CLI command.

## Limitations and roadmap

- [ ] Read the plugs' stored hourly energy history (kWh)
- [ ] Background service exposing plugs via MQTT / Home Assistant discovery
- [ ] Switch schedules (on the PC, or the Circles' built-in schedules)
- [ ] Sense / Scan / Switch battery devices
- [ ] Power production (solar) counters

## License

[GPL-3.0-or-later](LICENSE). You may use, modify and sell software built
with this project, but anything you distribute that includes it must also be
released as open source under the GPL, with its full source code.

## Disclaimer

Not affiliated with or endorsed by Plugwise B.V. "Plugwise" is their trademark.
These devices switch mains voltage, so don't automate anything that could be
dangerous if it is switched unexpectedly. Use at your own risk.
