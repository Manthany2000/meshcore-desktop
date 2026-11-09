import asyncio
import struct

from meshcore_desktop import protocol as P
from meshcore_desktop import sim as S
from meshcore_desktop.admin import SETTINGS, masked, needs_confirm, stat_text
from meshcore_desktop.link import Session
from meshcore_desktop.model import Model, dm_conv

KEY = S._key("Ridge Repeater")


def test_encoders():
    f = P.login(KEY, "x" * 30)
    assert f[0] == 26 and f[1:33] == KEY and f[33:] == b"x" * 15
    assert P.status_req(KEY) == bytes([27]) + KEY
    f = P.send_cli(KEY, 5, "ver")
    assert f[:3] == bytes([2, P.TXT_CLI, 0]) and f[7:13] == KEY[:6] and f[13:] == b"ver"


def test_login_frames():
    new = P.decode(bytes([0x85, 1]) + KEY[:6] + struct.pack("<IBB", 9, 3, 8))
    assert new.kind == "login_ok" and new.is_admin is True and new.prefix == KEY[:6]
    guest = P.decode(bytes([0x85, 0]) + KEY[:6] + struct.pack("<IBB", 9, 1, 8))
    assert guest.is_admin is False
    assert P.decode(bytes([0x85, 0]) + KEY[:6]).is_admin is None  # an old repeater can't say
    assert P.decode(bytes([0x86, 0]) + KEY[:6]).kind == "login_fail"


def test_status_short_and_full():
    raw = struct.pack("<HHhhIIIIIIIIHhHHII", 3700, 2, -110, -80, 1, 2, 30, 300, 5, 6, 7, 8, 9, -10, 11, 12, 13, 14)
    assert len(raw) == 56  # sizeof(RepeaterStats)
    s = P.decode(bytes([0x87, 0]) + KEY[:6] + raw).stats
    assert (s["batt_mv"], s["noise_floor"], s["up_secs"], s["last_snr"], s["recv_errors"]) == (3700, -110, 300, -2.5, 14)
    assert "10.0% TX" in stat_text(s, 0)["air"]
    old = P.decode(bytes([0x87, 0]) + KEY[:6] + raw[:48]).stats
    assert old["rx_air_secs"] == 0 and old["flood_dups"] == 12


def test_console_safety():
    assert masked("password hunter2") == "password ••••••"
    assert masked("set guest.password abc") == "set guest.password ••••••"
    assert masked("get radio") == "get radio"
    assert needs_confirm("reboot") and needs_confirm("password x") and not needs_confirm("get name")


def _session(tmp_path):
    t = S.SimTransport()
    return Session(Model(tmp_path), t), t


def test_admin_flow(tmp_path):
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        assert (await s.login(KEY, "wrong")).kind == "login_fail"
        assert (await s.login(KEY, "")).kind == "login_fail"  # not on its list yet
        ok = await s.login(KEY, "admin")
        assert ok.kind == "login_ok" and ok.is_admin
        assert (await s.login(KEY, "")).is_admin  # now it knows us
        st = await s.repeater_status(KEY)
        assert st.stats["batt_mv"] == 3987
        r = await s.cli(KEY, "get name")
        assert r.msg.text == "> Ridge Repeater"
        # CLI replies aren't conversation.
        assert not s.model.conversation(dm_conv(KEY[:6]))
        await s.stop()
    asyncio.run(go())


def test_no_reply_and_late_reply(tmp_path):
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        s.admin_min_wait = 0.1
        far = S._key("Library Roof")
        t.admins.add(far[:6])
        assert await s.login(far, "admin") is None
        assert await s.cli(KEY, "ver") is None  # not logged in: ignored
        late = []
        s.model.listeners.append(lambda ev, o: ev == "cli_late" and late.append(o.text))
        # A reply after its command gave up still reaches the console.
        t.queue.append(t._dm(KEY[:6], "late one", t.clock_now(), txt=P.TXT_CLI))
        t._push(bytes([P.PUSH_MSG_WAITING]))
        await asyncio.sleep(0.2)
        assert late == ["late one"]
        await s.stop()
    asyncio.run(go())


def test_requests_take_turns(tmp_path):
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        t.admins.add(KEY[:6])
        a, b = await asyncio.gather(s.cli(KEY, "ver"), s.cli(KEY, "board"))
        assert a.msg.text.startswith("v1.11") and b.msg.text == "Heltec V3"
        await s.stop()
    asyncio.run(go())


def test_battery_percent():
    assert [P.batt_percent(v) for v in (2900, 3000, 3600, 4200, 4400)] == [0, 0, 50, 100, 100]
    raw = struct.pack("<H", 3987) + bytes(54)
    assert stat_text(P.decode(bytes([0x87, 0]) + KEY[:6] + raw).stats, 0)["batt"] == "3.99 V (82%)"


def test_setting_checks():
    by = {s.key: s for s in SETTINGS}
    assert by["tx"].problem("22") is None and "from" in by["tx"].problem("31") and "number" in by["tx"].problem("x")
    assert by["lat"].problem("42.5") is None and by["lat"].problem("91")
    assert by["name"].problem("") and by["owner.info"].problem("") is None


def test_get_and_set(tmp_path):
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        t.admins.add(KEY[:6])
        assert (await s.cli(KEY, "get tx")).msg.text == "> 22"
        assert (await s.cli(KEY, "set tx 20")).msg.text == "OK"
        assert (await s.cli(KEY, "get tx")).msg.text == "> 20"
        assert (await s.cli(KEY, "set advert.interval 999")).msg.text.startswith("Error")
        await s.stop()
    asyncio.run(go())


def test_map_details(tmp_path):
    from meshcore_desktop.mapview import contact_actions, contact_details, relay_counts, self_details
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        m = s.model
        c = m.contacts[S._key("Water Tower")[:6]]
        secs = {x["title"]: dict(x["rows"]) for x in contact_details(m, c, relay_counts(m), {"batt": "3.99 V (82%)"})}
        assert secs["Identity"]["Public key"] == c.key.hex() and secs["Identity"]["Type"] == "Repeater"
        assert secs["Location"]["Latitude"] == "42.300000" and secs["Reach"]["Route"] == "2 hops"
        assert secs["Reach"]["Distance"].endswith("km") and "Hop 2" in secs["Path"]
        assert secs["Repeater status"]["Battery"] == "3.99 V (82%)"
        assert [a[0] for a in contact_actions(c)][:2] == ["manage", "route"]
        me = {x["title"]: dict(x["rows"]) for x in self_details(m)}
        assert me["Radio"]["Frequency"] == "910.525 MHz" and me["Device"]["Firmware"] == "sim-1.0"
        await s.stop()
    asyncio.run(go())


def test_contact_flags():
    t = S.SimTransport()
    b = bytearray(t._contact_frame(t.contacts[0]))
    b[34] = 1
    assert P.decode(bytes(b)).contact.flags & 1


def test_repeater_adverts(tmp_path):
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        t.admins.add(KEY[:6])
        assert (await s.cli(KEY, "advert")).msg.text == "OK - Advert sent"
        assert (await s.cli(KEY, "advert.zerohop")).msg.text == "OK - zerohop advert sent"
        await s.stop()
    asyncio.run(go())


def test_signal_and_history(tmp_path):
    from meshcore_desktop.widgets import signal_level
    assert [signal_level(v) for v in (-20, -10, -5, 2, 9)] == [0, 1, 2, 3, 4]
    m = Model(tmp_path)
    m.open_for_node(KEY)
    m.record_repeater(KEY[:6], {"batt_mv": 3900, "noise_floor": -110, "last_rssi": -70})
    m.record_repeater(KEY[:6], {"batt_mv": 3850, "noise_floor": -105, "last_rssi": -71})
    assert [r[1] for r in m.repeater_history(KEY[:6])] == [3900, 3850]
    assert m.repeater_history(b"\0" * 6) == []


def test_dashboard_data(tmp_path):
    from meshcore_desktop.dashboard import ME, busiest_repeaters, channel_activity, mesh_links, packet_line, summary
    async def go():
        s, t = _session(tmp_path)
        await s.start()
        m = s.model
        out = m.add_outgoing("ch:0", "hello")
        m.apply(P.decode(t._log_rx(P.PAYLOAD_GRP_TXT, [KEY[:1]], P.encrypt_group(S.PUBLIC_SECRET, out.wire_time, "SimNode", "hello"), 3.0)))
        m._changed(out)
        got = summary(m)
        assert got["repeaters"] == 4 and got["packets"] == 1 and got["messages"] >= 2
        assert busiest_repeaters(m)[0] == ("Ridge Repeater", 1)
        assert ("Public", 2) in channel_activity(m)
        weight, links = mesh_links(m)
        assert ME in weight and tuple(sorted((ME, KEY[:1].hex()))) in links
        when, raw = m.packets[-1]
        assert "Channel msg" in packet_line(when, raw) and KEY[:1].hex() in packet_line(when, raw)
        await s.stop()
    asyncio.run(go())
