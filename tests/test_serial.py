import asyncio
import os
import sys

import pytest

from meshcore_desktop import serial_link as SL
from meshcore_desktop import sim as S
from meshcore_desktop.link import Session
from meshcore_desktop.model import SENT, Model, chan_conv


def test_deframer_skips_noise_and_splits():
    d = SL.Deframer()
    a, b = b"\x05hello", b"\x0cxy"
    wire = b"boot: ok\r\n>" + len(a).to_bytes(2, "little") + a + b">" + len(b).to_bytes(2, "little") + b
    out = []
    for i in range(len(wire)):  # one byte at a time, the worst case
        out += d.feed(wire[i:i + 1])
    assert out == [a, b]
    assert d.feed(b">\xff\xff>" + b"\x01\x00Z") == [b"Z"]  # absurd length: resync on the next '>'


def test_frame_out():
    assert SL.frame(b"\x01\x02") == b"<\x02\x00\x01\x02"


def test_address_helpers():
    assert SL.is_serial("serial:/dev/ttyACM0") and SL.port_of("serial:COM3") == "COM3"
    assert not SL.is_serial("AA:BB:CC:DD:EE:FF") and not SL.is_serial("")


def test_missing_port_message():
    t = SL.SerialTransport("serial:/dev/does-not-exist-mc")
    with pytest.raises(ConnectionError, match="not found"):
        asyncio.run(t.connect())


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_session_over_pty(tmp_path):
    """The whole sync over a pseudo-terminal, with the simulator as the node on the other end."""
    import pty
    import tty

    async def go():
        master, slave = pty.openpty()
        tty.setraw(slave)
        node = S.SimTransport()
        node.on_frame = lambda b: os.write(master, b">" + len(b).to_bytes(2, "little") + b)
        await node.connect()
        loop = asyncio.get_running_loop()
        buf = bytearray()

        def readable():
            buf.extend(os.read(master, 4096))
            while len(buf) >= 3 and buf[0] == ord("<") and len(buf) >= 3 + (n := buf[1] | buf[2] << 8):
                f = bytes(buf[3:3 + n])
                del buf[:3 + n]
                asyncio.ensure_future(node.write(f))
        loop.add_reader(master, readable)

        model = Model(tmp_path)
        states = []
        t = SL.SerialTransport("serial:" + os.ttyname(slave))
        s = Session(model, t, lambda st, d="": states.append(st))
        try:
            await s.start()
            assert states[-1] == "ready"
            assert model.self_info.name == "SimNode"
            assert len(model.contacts) == len(S.CONTACTS)
            out = await s.send(chan_conv(0), "hello over usb")
            assert out.status == SENT
        finally:
            await s.stop()
            loop.remove_reader(master)
            await node.close()
            os.close(master)
            os.close(slave)
    asyncio.run(go())
