"""Setting up a node: spotting one that's just been flashed, the community's
radio presets, and applying a chosen setup to the node one command at a time."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from . import protocol as P


@dataclass(frozen=True)
class Preset:
    title: str
    freq: float  # MHz
    bw: float  # kHz
    sf: int
    cr: int
    hash_size: int = 1  # bytes per hop in floods

    def matches(self, freq: float, bw: float, sf: int, cr: int) -> bool:
        return abs(self.freq - freq) < 0.0005 and abs(self.bw - bw) < 0.05 and (self.sf, self.cr) == (sf, cr)


# The suggested settings the MeshCore apps offer, from api.meshcore.nz/api/v1/config
# on 12 Nov 2026. The radio must match the local mesh, so these are a starting point.
PRESETS = [
    Preset("Australia", 915.800, 250, 10, 5, 2),
    Preset("Australia (Narrow)", 916.575, 62.5, 7, 7, 2),
    Preset("Australia (Mid)", 915.075, 125, 9, 5, 2),
    Preset("Australia: SA, WA", 923.125, 62.5, 8, 8, 2),
    Preset("Australia: QLD", 923.125, 62.5, 8, 5, 2),
    Preset("Brazil", 923.125, 62.5, 8, 8),
    Preset("Canada", 910.525, 62.5, 7, 5, 3),
    Preset("Costa Rica", 910.525, 125, 11, 5),
    Preset("EU/UK (Narrow)", 869.618, 62.5, 8, 8),
    Preset("EU/UK (Deprecated)", 869.525, 250, 11, 5),
    Preset("Czech Republic (Narrow)", 869.432, 62.5, 7, 5, 2),
    Preset("EU 433MHz (Long Range)", 433.650, 250, 11, 5),
    Preset("EU 433MHz (Narrow)", 433.650, 62.5, 8, 8),
    Preset("Hungary", 869.618, 62.5, 7, 5, 2),
    Preset("Netherlands", 869.618, 62.5, 7, 5),
    Preset("Netherlands (Limburg)", 869.618, 62.5, 8, 8, 2),
    Preset("New Zealand (Narrow)", 917.375, 62.5, 7, 5, 2),
    Preset("New Zealand (Gisborne)", 917.375, 250, 11, 5),
    Preset("Portugal 433", 433.375, 62.5, 9, 6),
    Preset("Portugal 868", 869.618, 62.5, 7, 6),
    Preset("Slovakia", 869.618, 62.5, 7, 5, 2),
    Preset("Switzerland", 869.618, 62.5, 8, 8),
    Preset("Thailand", 921.000, 62.5, 7, 5),
    Preset("USA", 910.525, 62.5, 7, 5),
    Preset("USA - Southern California", 927.875, 62.5, 7, 5, 3),
    Preset("Vietnam (Narrow)", 920.250, 62.5, 8, 5),
    Preset("Vietnam (Deprecated)", 920.250, 250, 11, 5),
]

BANDWIDTHS = [7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500]  # kHz, what SX126x radios do


def find_preset(freq: float, bw: float, sf: int, cr: int, hash_size: int | None = None) -> Preset | None:
    """The first preset with these settings; several regions share some."""
    hits = [p for p in PRESETS if p.matches(freq, bw, sf, cr)]
    exact = [p for p in hits if hash_size is None or p.hash_size == hash_size]
    return (exact or hits or [None])[0]


# Who the node answers telemetry requests from, one setting per kind of reading,
# two bits each in the telemetry byte: base (battery) in bits 0-1, location in
# 2-3, environment sensors in 4-5. (TELEM_MODE_* in the companion firmware.)
TELEMETRY_KINDS = ("base", "location", "environment")
TELEMETRY_MODES = {0: "Nobody", 1: "Only contacts I allow", 2: "Anyone"}


def telemetry_modes(byte: int) -> dict[str, int]:
    return {k: (byte >> (2 * i)) & 3 for i, k in enumerate(TELEMETRY_KINDS)}


def telemetry_byte(modes: dict[str, int], old: int = 0) -> int:
    """Packs the modes back, keeping the old byte's bits for any kind not given."""
    out = old & ~0x3F
    for i, k in enumerate(TELEMETRY_KINDS):
        out |= (modes.get(k, (old >> (2 * i)) & 3) & 3) << (2 * i)
    return out


def is_fresh(self_info) -> bool:
    """A node nobody has set up yet still has the name the firmware gave it on
    first boot: the first four bytes of its key in hex."""
    if not self_info:
        return False
    name = self_info.name.strip()
    return name.upper() == self_info.key[:4].hex().upper() or name == "NONAME"


def hashtag_secret(name: str) -> bytes:
    """A '#name' channel's key is derived from its name, so typing it joins it."""
    return hashlib.sha256(name.encode()).digest()[:16]


def parse_hashtags(text: str) -> list[str]:
    """'#local, #emergency foo' -> ['#local', '#emergency', '#foo']"""
    out = []
    for w in re.split(r"[\s,;]+", text.strip()):
        w = w.strip()
        if not w:
            continue
        w = "#" + w.lstrip("#").lower()
        if len(w) > 1 and w not in out:
            out.append(w[:31])
    return out


def parse_latlon(text: str) -> tuple[float, float] | None:
    """'42.3601, -71.0589' (as copied from most maps). Raises ValueError if it
    isn't a position; blank gives None."""
    text = text.strip()
    if not text:
        return None
    parts = [p for p in re.split(r"[\s,;]+", text) if p]
    if len(parts) != 2:
        raise ValueError("Enter latitude and longitude, like 42.3601, -71.0589")
    lat, lon = float(parts[0]), float(parts[1])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Latitude must be within ±90 and longitude within ±180")
    return lat, lon


@dataclass
class Plan:
    """What the setup wizard collected. None means leave it as it is."""
    name: str | None = None
    radio: tuple[float, float, int, int] | None = None  # freq MHz, bw kHz, sf, cr
    hash_size: int | None = None
    tx_power: int | None = None
    location: tuple[float, float] | None = None
    share_location: bool | None = None
    auto_add: bool | None = None
    telemetry: int | None = None  # the packed byte; see telemetry_byte()
    multi_acks: bool | None = None
    pin: int | None = None  # 0: the firmware's default
    channels: list[tuple[str, bytes]] = field(default_factory=list)  # to add
    default_scope: str | None = None  # a region, or "" to clear it
    advert: bool = True  # flood an advert when done, so the mesh learns the node
    reboot: bool = False

    def steps(self) -> list[str]:
        """What will be done, in order, for the review page."""
        out = []
        if self.name is not None:
            out.append(f"Name the node “{self.name}”")
        if self.location is not None:
            out.append(f"Set its position to {self.location[0]:.5f}, {self.location[1]:.5f}")
        if self.share_location is not None or self.auto_add is not None:
            bits = []
            if self.share_location is not None:
                bits.append("share its position in adverts" if self.share_location else "keep its position out of adverts")
            if self.auto_add is not None:
                bits.append("add contacts as they're heard" if self.auto_add else "add contacts only by hand")
            out.append(bits[0][0].upper() + "; ".join(bits)[1:])
        if self.telemetry is not None:
            modes = telemetry_modes(self.telemetry)
            out.append("Answer requests for readings — " + ", ".join(
                f"{k}: {TELEMETRY_MODES.get(m, f'mode {m}').lower()}" for k, m in modes.items()))
        if self.multi_acks is not None:
            out.append("Send an extra ack for each direct message" if self.multi_acks else "Send one ack per direct message")
        for name, _s in self.channels:
            out.append(f"Join channel {name}")
        if self.default_scope is not None:
            out.append(f"Keep its floods to region #{self.default_scope}" if self.default_scope
                       else "Clear its default flood scope")
        if self.tx_power is not None:
            out.append(f"Transmit at {self.tx_power} dBm")
        if self.hash_size is not None:
            out.append(f"Use {self.hash_size}-byte hop hashes in floods")
        if self.radio is not None:
            f, bw, sf, cr = self.radio
            out.append(f"Tune the radio to {f:.3f} MHz · BW {bw:g} kHz · SF{sf} · CR{cr}")
        if self.pin is not None:
            out.append("Use the default Bluetooth PIN" if self.pin == 0 else f"Set the Bluetooth PIN to {self.pin:06d}")
        if self.advert:
            out.append("Send a flood advert so the mesh hears about the node")
        if self.reboot:
            out.append("Restart the node")
        return out


def _ok(f) -> bool:
    return f is not None and f.kind != "err"


def _why(f) -> str:
    if f is None:
        return "no answer"
    return P.ERR_NAMES.get(f.err, f"error {f.err}") if f.kind == "err" else ""


async def apply(session, plan: Plan, progress=lambda text, ok=None, why="": None) -> bool:
    """Sends the plan to the node. progress(text) when a step starts, then
    progress(text, ok, why) when it's done. Carries on past a failed step and
    returns whether they all worked. The radio goes last but for the PIN and the
    advert, so the advert goes out on the new settings."""
    model = session.model
    si = model.self_info
    all_ok = True

    async def step(text, coro):
        nonlocal all_ok
        progress(text)
        try:
            f = await coro
        except Exception as e:
            all_ok = False
            progress(text, False, str(e) or e.__class__.__name__)
            return
        ok = _ok(f)
        all_ok &= ok
        progress(text, ok, _why(f))

    if plan.name is not None:
        await step(f"Naming it “{plan.name}”", session.request(P.set_name(plan.name), {"ok"}))
    if plan.location is not None:
        await step("Setting its position", session.request(P.set_latlon(*plan.location), {"ok"}))
    if any(x is not None for x in (plan.share_location, plan.auto_add, plan.telemetry, plan.multi_acks)):
        # One command sets all four, so the ones we don't change go back as they were.
        manual = si.manual_add if si else 0
        if plan.auto_add is not None:
            manual = (manual & ~1) | (0 if plan.auto_add else 1)
        policy = si.loc_policy if si else 0
        if plan.share_location is not None:
            policy = 1 if plan.share_location else 0
        telemetry = plan.telemetry if plan.telemetry is not None else (si.telemetry if si else 0)
        multi = int(plan.multi_acks) if plan.multi_acks is not None else (si.multi_acks if si else 0)
        await step("Setting contact, position and privacy options",
                   session.request(P.set_other_params(manual, telemetry, policy, multi), {"ok"}))
    for name, secret in plan.channels:
        free = [i for i in range(session.max_channels) if i not in model.channels]
        if not free:
            progress(f"Joining {name}", False, "every channel slot is in use")
            all_ok = False
            continue
        await step(f"Joining {name}", session.set_channel(free[0], name, secret))
    if plan.default_scope is not None:
        await step("Setting the default flood scope", session.set_default_scope(plan.default_scope))
    if plan.tx_power is not None:
        await step(f"Setting transmit power to {plan.tx_power} dBm", session.request(P.set_tx_power(plan.tx_power), {"ok"}))
    if plan.hash_size is not None:
        await step(f"Using {plan.hash_size}-byte hop hashes",
                   session.request(P.set_path_hash_mode(plan.hash_size - 1), {"ok"}))
    if plan.radio is not None:
        await step("Tuning the radio", session.request(P.set_radio(*plan.radio), {"ok"}))
    if plan.pin is not None:
        await step("Setting the Bluetooth PIN", session.request(P.set_pin(plan.pin), {"ok"}))
    # Read back what the node now says about itself, for the toolbar.
    await session.request(P.app_start(), {"self_info"})
    await session.request(P.device_query(), {"device_info"})
    if plan.advert:
        await step("Sending a flood advert", session.advert(True))
    if plan.reboot:
        progress("Restarting the node")
        await session.reboot()
        progress("Restarting the node", True)
    return all_ok
