import asyncio
import struct
import time

from meshcore_desktop import protocol as P
from meshcore_desktop import sim as S
from meshcore_desktop.link import Session
from meshcore_desktop.model import DELIVERED, FAILED, SENT, WAITING, Model, chan_conv, dm_conv


def test_group_roundtrip():
    sec = S.PUBLIC_SECRET
    payload = P.encrypt_group(sec, 1234567890, "alice", "hello world")
    assert P.decrypt_group(sec, payload) == (1234567890, "alice: hello world")
    assert P.decrypt_group(sec, payload[:-1]) is None


def test_channel_msg_v3_and_old():
    t = S.SimTransport()
    f = P.decode(t._chan_msg(1, "bob", "hi: there", 1700000123, hops=2, snr=-3.25))
    m = f.msg
    assert (m.is_channel, m.channel, m.who, m.text, m.hops, m.snr, m.timestamp) == \
        (True, 1, "bob", "hi: there", 2, -3.25, 1700000123)
    old = P.decode(t._chan_msg(0, "x", "y", 5, v3=False)).msg
    assert old.snr is None and old.who == "x"


def test_dm_and_contact():
    t = S.SimTransport()
    key = S._key("alice")
    m = P.decode(t._dm(key, "yo", 99, hops=0xFF)).msg
    assert not m.is_channel and m.key == key[:6] and m.hops == -1 and m.text == "yo"
    c = P.decode(t._contact_frame(t.contacts[5])).contact
    assert c.name == "bob-t1000" and c.hops == 2 and len(c.path.hops()) == 2 and abs(c.lat - 42.38) < 1e-6


def test_log_rx_flood_with_transport_codes():
    # route 0 = flood with transport codes: 4 extra bytes before path_len
    header = (P.PAYLOAD_GRP_TXT << 2) | 0
    b = bytes([P.PUSH_LOG_RX, 20, 0xB0, header, 1, 2, 3, 4, 2, 0xAA, 0xBB]) + b"payload!"
    r = P.decode(b).raw
    assert r.flood and r.hops == 2 and [h.hex() for h in r.path.hops()] == ["aa", "bb"]
    assert r.payload == b"payload!" and r.snr == 5.0 and r.rssi == -80
    # two-byte hashes
    b = bytes([P.PUSH_LOG_RX, 0, 0, (P.PAYLOAD_TXT_MSG << 2) | 1, 0x42, 1, 2, 3, 4]) + b"pp"
    r = P.decode(b).raw
    assert r.hops == 2 and r.path.hash_size == 2 and [h.hex() for h in r.path.hops()] == ["0102", "0304"]


def test_encoders():
    assert P.send_channel(2, 0x01020304, "hi") == bytes([3, 0, 2, 4, 3, 2, 1]) + b"hi"
    f = P.send_direct(b"\x01\x02\x03\x04\x05\x06\x07", 5, "x")
    assert f[0] == 2 and f[7:13] == b"\x01\x02\x03\x04\x05\x06" and f[13:] == b"x"
    assert len(P.set_channel(1, "#test", bytes(16))) == 50


def _model_with_channel(tmp_path):
    m = Model(tmp_path)
    t = S.SimTransport()
    t._t0 = time.monotonic()
    m.apply(P.decode(bytes([P.RESP_SELF_INFO, 1, 22, 22]) + t.key + bytes(12) + struct.pack("<IIBB", 0, 0, 7, 5)
                     + b"Me"))
    m.apply(P.decode(bytes([P.RESP_CHANNEL_INFO, 0]) + b"Public".ljust(32, b"\0") + S.PUBLIC_SECRET))
    return m, t


def test_repeats_before_and_after_frame(tmp_path):
    m, t = _model_with_channel(tmp_path)
    ts = 1800000000
    payload = P.encrypt_group(S.PUBLIC_SECRET, ts, "alice", "hello")
    other = P.encrypt_group(S.PUBLIC_SECRET, ts, "alice", "different")
    # Heard before the message frame: the copy the node acted on (2 hops), and noise.
    m.apply(P.decode(t._log_rx(P.PAYLOAD_GRP_TXT, [b"\xaa", b"\xbb"], payload, 6.0)))
    m.apply(P.decode(t._log_rx(P.PAYLOAD_GRP_TXT, [b"\xcc"], other, 1.0)))
    m.apply(P.decode(t._chan_msg(0, "alice", "hello", ts, hops=2)))
    msg = m.conversation(chan_conv(0))[0]
    assert msg.path == ["aa", "bb"] and msg.repeats == 1
    # A later copy relayed further.
    m.apply(P.decode(t._log_rx(P.PAYLOAD_GRP_TXT, [b"\xaa", b"\xbb", b"\xdd"], payload, -2.0)))
    assert msg.repeats == 2 and set(msg.repeaters) == {"aa", "bb", "dd"}
    assert msg.repeaters["bb"].direct and msg.repeaters["dd"].direct and not msg.repeaters["aa"].direct
    # Persisted.
    m2 = Model(tmp_path)
    m2.open_for_node(t.key)
    again = m2.conversation(chan_conv(0))[0]
    assert again.repeats == 2 and again.path == ["aa", "bb"] and again.repeaters["dd"].snr == -2.0


def test_own_channel_message_repeats(tmp_path):
    m, t = _model_with_channel(tmp_path)
    out = m.add_outgoing(chan_conv(0), "testing 123")
    m.send_result(out, P.decode(bytes([P.RESP_OK])))
    assert out.status == SENT
    payload = P.encrypt_group(S.PUBLIC_SECRET, out.wire_time, "Me", "testing 123")
    m.apply(P.decode(t._log_rx(P.PAYLOAD_GRP_TXT, [b"\x11"], payload, 3.0)))
    m.apply(P.decode(t._log_rx(P.PAYLOAD_GRP_TXT, [b"\x11", b"\x22"], payload, 1.0)))
    assert out.repeats == 2 and out.repeaters["11"].direct and out.repeaters["22"].direct


def test_session_against_sim(tmp_path):
    async def go():
        model = Model(tmp_path)
        states = []
        s = Session(model, S.SimTransport(), lambda st, d="": states.append(st))
        await s.start()
        assert states[-1] == "ready"
        assert model.self_info.name == "SimNode"
        assert {c.name for c in model.channels.values()} == {"Public", "#test"}
        assert len(model.contacts) == len(S.CONTACTS)
        assert abs(model.node_now() - time.time()) < 3  # we set its clock
        assert len(model.conversation(chan_conv(0))) == 1  # backlog
        out = await s.send(chan_conv(0), "hello mesh")
        assert out.status == SENT
        alice = model.contacts[S._key("alice")[:6]]
        dm = await s.send(dm_conv(alice.prefix), "hi alice")
        assert dm.status in (WAITING, DELIVERED)
        await asyncio.sleep(4.5)
        assert out.repeats >= 1 and out.repeaters
        await s.stop()
    asyncio.run(go())


def test_always_flood_resets_path_before_each_dm(tmp_path):
    async def go():
        model = Model(tmp_path)
        t = S.SimTransport()
        s = Session(model, t)
        await s.start()
        alice = model.contacts[S._key("alice")[:6]]
        bob = model.contacts[S._key("bob-t1000")[:6]]
        model.set_always_flood(alice.prefix, True)
        sent = []
        write = t.write

        async def spy(b):
            if b[0] == P.CMD_RESET_PATH:
                sent.append(("reset", b[1:7]))
            elif b[0] == P.CMD_SEND_TXT_MSG:
                sent.append(("dm", b[7:13]))
            await write(b)
        t.write = spy
        for text in ("one", "two"):  # the first flood teaches a route; the second must still flood
            await s.send(dm_conv(alice.prefix), text)
        await s.send(dm_conv(bob.prefix), "routed")
        assert sent == [("reset", alice.prefix), ("dm", alice.prefix),
                        ("reset", alice.prefix), ("dm", alice.prefix), ("dm", bob.prefix)]
        await s.stop()
        # Kept per node across restarts, and can be turned off.
        again = Model(tmp_path)
        again.open_for_node(model.self_info.key)
        assert again.always_floods(alice.prefix) and not again.always_floods(bob.prefix)
        again.set_always_flood(alice.prefix, False)
        third = Model(tmp_path)
        third.open_for_node(model.self_info.key)
        assert not third.always_floods(alice.prefix)
    asyncio.run(go())


def test_failed_or_stuck_write_never_leaves_a_message_sending(tmp_path):
    async def go():
        model = Model(tmp_path)
        t = S.SimTransport()
        states = []
        s = Session(model, t, lambda st, d="": states.append((st, d)))
        await s.start()
        write = t.write

        async def broken(b):
            raise RuntimeError("GATT Protocol Error: Unlikely Error")
        t.write = broken
        m = await s.send(chan_conv(0), "won't go")
        assert m.status == FAILED and s.connected  # an error alone keeps the link

        async def stuck(b):
            await asyncio.sleep(3600)
        t.write = stuck
        s.write_timeout = 0.2
        m = await s.send(chan_conv(0), "hangs")
        assert m.status == FAILED
        assert not s.connected and states[-1] == ("disconnected", "node stopped responding")
        assert await s.request(P.simple(P.CMD_GET_DEVICE_TIME), {"time"}) is None  # no writes once down

        # A drop while a command waits for its answer fails that message too.
        (tmp_path / "b").mkdir()
        s2 = Session(Model(tmp_path / "b"), S.SimTransport())
        await s2.start()
        t2 = s2.t
        w2 = t2.write

        async def silent(b):  # sent, but the node never answers; then the link drops
            if b[0] == P.CMD_SEND_CHANNEL_TXT_MSG:
                asyncio.get_running_loop().call_later(0.1, s2._dropped)
                return
            await w2(b)
        t2.write = silent
        m = await s2.send(chan_conv(0), "dropped")
        assert m.status == FAILED
        await s.stop()
        await s2.stop()
    asyncio.run(go())
