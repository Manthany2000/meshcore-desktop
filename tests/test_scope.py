import asyncio
import hashlib

from meshcore_desktop import protocol as P
from meshcore_desktop import setup as S
from meshcore_desktop import sim
from meshcore_desktop.link import Session
from meshcore_desktop.model import NO_SCOPE, Model, chan_conv, dm_conv

KEY = sim._key("Ridge Repeater")


def test_region_key_and_frames():
    # TransportKeyStore::getAutoKeyFor: sha256 of "#name", with or without the '#' typed.
    k = hashlib.sha256(b"#boston").digest()[:16]
    assert P.region_key("boston") == P.region_key("#boston") == P.region_key(" boston ") == k
    assert P.region_key("Boston") != k  # names are case-sensitive
    # CMD_SET_FLOOD_SCOPE_KEY: 54, 0, key[16]; 54, 0 resets; 54, 1 is unscoped.
    assert P.set_flood_scope("boston") == bytes([54, 0]) + k
    assert P.set_flood_scope("") == bytes([54, 0])
    assert P.set_flood_scope(None) == bytes([54, 1])
    # CMD_SET_DEFAULT_FLOOD_SCOPE: 63, name[31], key[16]; 63 alone clears.
    f = P.set_default_scope("#boston")
    assert len(f) == 48 and f[:8] == b"\x3fboston\0" and f[32:] == k
    assert P.set_default_scope("") == bytes([63])
    got = P.decode(bytes([28]) + b"usa".ljust(31, b"\0") + P.region_key("usa"))
    assert got.kind == "default_scope" and got.name == "usa" and got.key == P.region_key("usa")
    assert P.decode(bytes([28])).name == ""


def test_region_names():
    assert P.region_problem("boston") is None
    assert P.region_problem("#north-shore$2") is None
    assert P.region_problem("Zürich") is None
    assert P.region_problem("") and P.region_problem("#")
    assert "' '" in P.region_problem("new york")
    assert P.region_problem("a.b") and P.region_problem("x" * 30)


def test_parse_regions():
    text = "* F\n usa F\n  ma^ F\n   boston\n  ny F\n eu"
    rs = P.parse_regions(text)
    assert [(r.name, r.depth, r.flood, r.home, r.parent) for r in rs] == [
        ("*", 0, True, False, None), ("usa", 1, True, False, "*"), ("ma", 2, True, True, "usa"),
        ("boston", 3, False, False, "ma"), ("ny", 2, True, False, "usa"), ("eu", 1, False, False, "*")]
    assert P.parse_regions("Err - ??") == [] and P.parse_regions("Unknown command") == []
    assert P.parse_regions("") == []


def _session(tmp_path):
    model = Model(tmp_path)
    t = sim.SimTransport()
    return Session(model, t), t, model


def test_channel_scopes_are_sent_before_each_message(tmp_path):
    async def go():
        s, t, model = _session(tmp_path)
        await s.start()
        assert s.scopes and s.unscoped and model.default_scope == ""
        assert t.send_scope == b""  # reset on connect
        model.set_channel_scope(1, "#boston")
        assert model.channel_scope(1) == "boston" and model.channel_scope(0) == ""
        await s.send(chan_conv(1), "scoped")
        await s.send(chan_conv(0), "default")
        model.set_channel_scope(0, NO_SCOPE)
        await s.send(chan_conv(0), "unscoped")
        await s.send(chan_conv(1), "scoped again")
        assert t.sent_scopes == [P.region_key("boston"), b"", None, P.region_key("boston")]
        # A DM goes back to the node's default.
        await s.send(dm_conv(sim._key("alice")), "hi")
        assert t.send_scope == b""
        # The node's default, saved on it.
        assert (await s.set_default_scope("ma")).kind == "ok"
        assert model.default_scope == "ma" and t.default_scope == ("ma", P.region_key("ma"))
        await s.send(chan_conv(1), "boston still")
        model.set_channel_scope(1, "")
        await s.send(chan_conv(1), "now ma")
        assert t.sent_scopes[-2:] == [P.region_key("boston"), P.region_key("ma")]
        await s.stop()
    asyncio.run(go())


def test_channel_scope_kept_per_node_and_dropped_with_the_channel(tmp_path):
    async def go():
        s, t, model = _session(tmp_path)
        await s.start()
        model.set_channel_scope(1, "boston")
        await s.stop()
        model2 = Model(tmp_path)
        s2 = Session(model2, sim.SimTransport())
        await s2.start()
        assert model2.channel_scope(1) == "boston"
        # Another channel in the same slot doesn't inherit it.
        await s2.set_channel(1, "#other", S.hashtag_secret("#other"))
        assert model2.channel_scope(1) == ""
        await s2.stop()
    asyncio.run(go())


def test_old_firmware_without_scopes(tmp_path):
    async def go():
        s, t, model = _session(tmp_path)
        await s.start()
        s.protocol = 7
        model.set_channel_scope(1, "boston")
        m = await s.send(chan_conv(1), "won't go")
        assert m.status == "failed" and t.sent_scopes == []  # not sent unscoped by mistake
        model.set_channel_scope(1, "")
        m = await s.send(chan_conv(1), "goes")
        assert m.status == "sent"
        await s.stop()
    asyncio.run(go())


def test_repeater_regions(tmp_path):
    async def go():
        s, t, model = _session(tmp_path)
        await s.start()
        assert (await s.login(KEY, "admin")).kind == "login_ok"

        async def cli(cmd):
            return (await s.cli(KEY, cmd)).msg.text
        rs = P.parse_regions(await cli("region"))
        assert [r.name for r in rs] == ["*", "usa", "ma", "boston"] and rs[2].home
        assert (await cli("region put salem ma")).startswith("OK")
        assert await cli("region denyf boston") == "OK"
        assert await cli("region default ma") == " default scope is now ma"
        rs = {r.name: r for r in P.parse_regions(await cli("region"))}
        assert rs["salem"].parent == "ma" and not rs["boston"].flood
        assert await cli("region remove ma") == "Err - not empty"
        assert await cli("region save") == "OK"
        assert await cli("get flood.max.unscoped") == "> 64"
        await s.stop()
    asyncio.run(go())
