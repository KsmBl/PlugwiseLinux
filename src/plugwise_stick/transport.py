"""Byte transports. The Stick only needs write(bytes) / read(timeout) / close()."""
from __future__ import annotations

import os
import select
import termios

from .errors import PlugwiseError, StickError

DEFAULT_PORTS = [
    "/dev/plugwise",
    "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A801VBMD-if00-port0",
    "/dev/ttyUSB0",
]


def find_port() -> str:
    by_id = "/dev/serial/by-id"
    candidates = list(DEFAULT_PORTS)
    if os.path.isdir(by_id):
        candidates[1:1] = sorted(os.path.join(by_id, n) for n in os.listdir(by_id)
                                 if "FTDI" in n)
    for p in candidates:
        if os.path.exists(p):
            return p
    raise StickError("no Plugwise stick found (is it plugged in?)")


class SerialTransport:
    """Raw 115200 8N1 serial port via termios (no pyserial needed)."""

    def __init__(self, port: str | None = None):
        self.port = port or find_port()
        try:
            self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            raise PlugwiseError(
                f"no permission for {self.port}; add yourself to its group "
                f"(e.g. 'sudo usermod -aG uucp $USER' and re-login)") from None
        except FileNotFoundError:
            raise StickError(f"{self.port} does not exist") from None
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read(self, timeout: float) -> bytes:
        r, _, _ = select.select([self.fd], [], [], max(timeout, 0))
        return os.read(self.fd, 4096) if r else b""

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
