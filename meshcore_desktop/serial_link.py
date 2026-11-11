"""USB serial transport: the same companion frames as BLE, wrapped for a byte stream.
App to node: '<' + u16 length (little-endian) + frame. Node to app: '>' + u16 length + frame.
The node has to run a USB companion build (companion_radio_usb); a BLE build doesn't
listen for frames on its USB port."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading

import serial
from serial.tools import list_ports

log = logging.getLogger("serial")

PREFIX = "serial:"
BAUD = 115200
MAX_FRAME = 512  # real frames are under 200 bytes; anything bigger means we lost sync

# USB vendor IDs of the chips MeshCore boards use, so they can be listed first.
KNOWN_VIDS = {0x303A: "ESP32", 0x10C4: "CP210x", 0x1A86: "CH340", 0x0403: "FTDI", 0x239A: "nRF52",
              0x2886: "Seeed", 0x2E8A: "RP2040"}


def is_serial(address: str | None) -> bool:
    return bool(address) and address.startswith(PREFIX)


def port_of(address: str) -> str:
    return address[len(PREFIX):]


def ports():
    """USB serial ports: [(address, label, known_chip)], likely MeshCore boards first."""
    out = []
    for p in list_ports.comports():
        if p.vid is None:  # built-in UARTs (/dev/ttyS*), Bluetooth serial and the like
            continue
        chip = KNOWN_VIDS.get(p.vid)
        desc = p.product or p.description or "USB serial"
        if desc == "n/a":
            desc = "USB serial"
        out.append((PREFIX + p.device, f"{desc}  ({p.device})", chip is not None))
    return sorted(out, key=lambda d: (not d[2], d[0]))


class Deframer:
    """Pulls '>'-framed frames out of a byte stream, skipping anything else
    (boot messages, debug prints) until it finds a frame start again."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b">")
            if start < 0:
                self.buf.clear()
                return out
            del self.buf[:start]
            if len(self.buf) < 3:
                return out
            n = self.buf[1] | self.buf[2] << 8
            if n == 0 or n > MAX_FRAME:
                del self.buf[:1]  # not a real frame start
                continue
            if len(self.buf) < 3 + n:
                return out
            out.append(bytes(self.buf[3:3 + n]))
            del self.buf[:3 + n]


def frame(b: bytes) -> bytes:
    return b"<" + len(b).to_bytes(2, "little") + b


class SerialTransport:
    def __init__(self, address: str):
        self.address = address
        self.port = port_of(address)
        self.ser: serial.Serial | None = None
        self.on_frame = lambda b: None
        self.on_disconnect = lambda: None
        self._thread: threading.Thread | None = None
        self._closing = False

    async def connect(self):
        loop = asyncio.get_running_loop()
        try:
            self.ser = await loop.run_in_executor(None, self._open)
        except serial.SerialException as e:
            raise ConnectionError(self._explain(e)) from e
        self._closing = False
        # Boards that reset when the port opens print a boot banner first; let it pass.
        await asyncio.sleep(0.3)
        self.ser.reset_input_buffer()
        self._thread = threading.Thread(target=self._reader, args=(loop,), daemon=True, name="serial-rx")
        self._thread.start()

    def _open(self) -> serial.Serial:
        return serial.Serial(self.port, BAUD, timeout=0.2, write_timeout=2)

    def _explain(self, e: Exception) -> str:
        s = str(e)
        if "ermission" in s or "Access is denied" in s:
            if sys.platform == "linux":
                return (f"No permission to open {self.port}. Add yourself to the dialout group "
                        "(sudo usermod -aG dialout $USER), then log out and back in.")
            return f"{self.port} is busy or not allowed. Is another program using it?"
        if "busy" in s.lower() or "Resource temporarily unavailable" in s:
            return f"{self.port} is in use by another program."
        if "No such file" in s or "FileNotFound" in s or "could not open port" in s:
            return f"{self.port} not found. Is the node plugged in?"
        return s

    def _reader(self, loop: asyncio.AbstractEventLoop):
        d = Deframer()
        ser = self.ser
        while not self._closing:
            try:
                data = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not self._closing:
                    log.warning("serial read: %s", e)
                    loop.call_soon_threadsafe(self.on_disconnect)
                return
            if data:
                for f in d.feed(data):
                    loop.call_soon_threadsafe(self.on_frame, f)

    async def write(self, b: bytes):
        if not self.ser:
            raise ConnectionError("not connected")
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._write, frame(b))
        except (serial.SerialException, OSError) as e:
            raise ConnectionError(f"serial write: {e}") from e

    def _write(self, data: bytes):
        self.ser.write(data)
        self.ser.flush()

    async def forget(self):
        pass  # nothing to forget: USB has no pairing

    async def close(self):
        self._closing = True
        ser, self.ser = self.ser, None
        if ser:
            if hasattr(ser, "cancel_read"):
                ser.cancel_read()  # wakes the reader thread out of its read
            await asyncio.get_running_loop().run_in_executor(None, ser.close)
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
