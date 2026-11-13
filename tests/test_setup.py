import asyncio
import struct

import pytest

from meshcore_desktop import protocol as P
from meshcore_desktop import setup as S
from meshcore_desktop import sim
from meshcore_desktop.link import Session
from meshcore_desktop.model import Model


def test_encoders_match_firmware_layouts():
    # MyMesh.cpp: freq in kHz and bandwidth in Hz, both u32; then sf, cr.
    assert P.set_radio(910.525, 62.5, 7, 5) == struct.pack("<BIIBB", 11, 910525, 62500, 7, 5)
    assert P.set_radio(869.618, 62.5, 8, 8)[1:5] == struct.pack("<I", 869618)
    assert P.set_tx_power(-3) == bytes([12, 0xFD])
    assert P.set_latlon(42.3601, -71.0589) == struct.pack("<Bii", 14, 42360100, -71058900)
    assert P.set_pin(123456) == struct.pack("<BI", 37, 123456)
    assert P.set_other_params(1, 0, 1, 0) == bytes([38, 1, 0, 1, 0])
    assert P.set_path_hash_mode(1) == bytes([61, 0, 1])
    assert P.reboot() == b"\x13reboot"
    assert P.set_name("x" * 40) == b"\x08" + b"x" * 31


def test_is_fresh():
    t = sim.SimTransport(fresh=True)
    frames = []
    t.on_frame = frames.append

    async def go():
        await t.write(P.app_start())
        await asyncio.sleep(0.05)
    asyncio.run(go())
    si = P.decode(frames[0])
    assert S.is_fresh(si) and si.lat == 0 and si.freq == 869.618
    assert not S.is_fresh(P.decode(frames[0][:58] + b"kestrel-mobile"))
    assert S.is_fresh(P.decode(frames[0][:58] + si.key[:4].hex().lower().encode()))
    assert not S.is_fresh(None)


def test_parsers():
    assert S.parse_hashtags("#Local, emergency  #local;#x") == ["#local", "#emergency", "#x"]
    assert S.parse_latlon(" 42.3601, -71.0589 ") == (42.3601, -71.0589)
    assert S.parse_latlon("42.36 -71.05") == (42.36, -71.05)
    assert S.parse_latlon("") is None
    for bad in ("42.36", "95, 10", "a, b"):
        with pytest.raises(ValueError):
            S.parse_latlon(bad)


def test_presets():
    usa = S.find_preset(910.525, 62.5, 7, 5)
    assert usa.title == "Canada"  # first with those settings; Canada floods with 3-byte hashes
    assert S.find_preset(910.525, 62.5, 7, 5, 1).title == "USA"
    assert S.find_preset(869.618, 62.5, 8, 8).title == "EU/UK (Narrow)"
    assert S.find_preset(900.0, 125, 9, 5) is None
    assert len({p.title for p in S.PRESETS}) == len(S.PRESETS)


def test_setup_a_fresh_node(tmp_path):
    async def go():
        model = Model(tmp_path)
        t = sim.SimTransport(fresh=True)
        s = Session(model, t)
        await s.start()
        assert S.is_fresh(model.self_info)
        plan = S.Plan(name="kestrel-mobile", radio=(910.525, 62.5, 7, 5), hash_size=2, tx_power=20,
                      location=(42.3601, -71.0589), share_location=True, auto_add=False, pin=246810,
                      channels=[("#local", S.hashtag_secret("#local"))], advert=True, reboot=False)
        steps = []
        ok = await S.apply(s, plan, lambda text, ok=None, why="": steps.append((text, ok, why)))
        assert ok, steps
        assert all(ok for _t, ok, _w in steps if ok is not None)
        si = model.self_info  # read back from the node
        assert si.name == "kestrel-mobile" and not S.is_fresh(si)
        assert (si.freq, si.bw, si.sf, si.cr) == (910.525, 62.5, 7, 5)
        assert si.tx_power == 20 and si.loc_policy == 1 and si.manual_add & 1
        assert abs(si.lat - 42.3601) < 1e-6 and abs(si.lon + 71.0589) < 1e-6
        assert model.device_info.ble_pin == 246810 and model.device_info.path_hash_mode == 1
        assert "#local" in {c.name for c in model.channels.values()}
        assert len(plan.steps()) == 9
        await s.stop()
    asyncio.run(go())


def test_failed_step_is_reported_and_the_rest_still_sent(tmp_path):
    async def go():
        model = Model(tmp_path)
        t = sim.SimTransport(fresh=True)
        s = Session(model, t)
        await s.start()
        steps = []
        ok = await S.apply(s, S.Plan(name="n2", tx_power=40, advert=False),
                           lambda text, ok=None, why="": steps.append((text, ok, why)))
        assert not ok
        assert ("Setting transmit power to 40 dBm", False, "illegal argument") in steps
        assert model.self_info.name == "n2"
        await s.stop()
    asyncio.run(go())


def test_reboot_drops_the_link(tmp_path):
    async def go():
        model = Model(tmp_path)
        t = sim.SimTransport(fresh=True)
        states = []
        s = Session(model, t, lambda st, d="": states.append((st, d)))
        await s.start()
        await S.apply(s, S.Plan(advert=False, reboot=True))
        await asyncio.sleep(0.4)
        assert t.reboots == 1 and states[-1] == ("disconnected", "link dropped")
    asyncio.run(go())


def test_telemetry_byte_packs_two_bits_per_kind():
    assert S.telemetry_modes(0b100110) == {"base": 2, "location": 1, "environment": 2}
    assert S.telemetry_byte({"base": 2, "location": 1, "environment": 2}) == 0b100110
    # Kinds left out keep the old bits, and so do the two spare bits on top.
    assert S.telemetry_byte({"location": 0}, 0b11000110) == 0b11000010


def test_privacy_options_keep_the_others_as_they_were(tmp_path):
    async def go():
        model = Model(tmp_path)
        t = sim.SimTransport(fresh=True)
        s = Session(model, t)
        await s.start()
        await S.apply(s, S.Plan(share_location=True, auto_add=False, advert=False))
        plan = S.Plan(telemetry=S.telemetry_byte({"base": 2, "location": 0, "environment": 1}), multi_acks=True,
                      advert=False)
        assert any("Answer requests for readings" in x for x in plan.steps())
        assert await S.apply(s, plan)
        si = model.self_info
        assert S.telemetry_modes(si.telemetry) == {"base": 2, "location": 0, "environment": 1}
        assert si.multi_acks == 1
        assert si.loc_policy == 1 and si.manual_add & 1  # untouched by the second plan
        await s.stop()
    asyncio.run(go())
