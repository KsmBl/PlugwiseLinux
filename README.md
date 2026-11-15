# PlugwiseLinux

Userspace driver and CLI for Plugwise Stick / Circle+ / Circle on Linux.
Pure Python 3 standard library: no dependencies, no kernel module needed
(the Stick is an FTDI serial device handled by `ftdi_sio`).

## Setup

Your user needs access to the serial port (group `uucp` on Arch, `dialout` on Debian/Ubuntu):

    sudo usermod -aG uucp $USER   # then log out and back in

## Usage

    ./plugwise.py stick                    # stick / network status
    ./plugwise.py scan                     # nodes registered in the Circle+
    ./plugwise.py info|ping|cal <mac>      # node details
    ./plugwise.py power <mac>              # current power usage (W)
    ./plugwise.py on|off <mac>             # switch relay
    ./plugwise.py watch <mac> [<mac>...]   # live power every 2 s
    ./plugwise.py join [<mac>]             # pair new Circles
    ./plugwise.py listen                   # dump unsolicited frames

Add `-d` to dump raw protocol frames, `-p <port>` to choose a serial port.
