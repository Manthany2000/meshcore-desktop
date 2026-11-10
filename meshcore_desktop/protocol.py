"""MeshCore companion protocol: one binary frame per BLE write or notification,
first byte is the code. Layouts from examples/companion_radio/MyMesh.cpp
(firmware protocol v14), ported from the CYD MeshCore app's decoder."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # we write
TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # it notifies

PASSWORD_MAX = 15  # the node sends at most this many bytes of a login password
TEXT_MAX = 160  # MAX_TEXT_LEN; for channels it includes "name: ", and the node cuts the rest
KEY_PREFIX = 6

# Commands, app to node.
CMD_APP_START = 1
CMD_SEND_TXT_MSG = 2
CMD_SEND_CHANNEL_TXT_MSG = 3
CMD_GET_CONTACTS = 4
CMD_GET_DEVICE_TIME = 5
CMD_SET_DEVICE_TIME = 6
CMD_SEND_SELF_ADVERT = 7
CMD_SYNC_NEXT_MESSAGE = 10
CMD_RESET_PATH = 13
CMD_GET_BATT_AND_STORAGE = 20
CMD_DEVICE_QUERY = 22
CMD_SEND_LOGIN = 26
CMD_SEND_STATUS_REQ = 27
CMD_GET_CONTACT_BY_KEY = 30
CMD_GET_CHANNEL = 31
CMD_SET_CHANNEL = 32

# Replies and pushes, node to app.
RESP_OK = 0
RESP_ERR = 1
RESP_CONTACTS_START = 2
RESP_CONTACT = 3
RESP_END_OF_CONTACTS = 4
RESP_SELF_INFO = 5
RESP_SENT = 6
RESP_CONTACT_MSG = 7
RESP_CHANNEL_MSG = 8
RESP_CURR_TIME = 9
RESP_NO_MORE_MESSAGES = 10
RESP_BATT = 12
RESP_DEVICE_INFO = 13
RESP_CONTACT_MSG_V3 = 16
RESP_CHANNEL_MSG_V3 = 17
RESP_CHANNEL_INFO = 18
PUSH_ADVERT = 0x80
PUSH_PATH_UPDATED = 0x81
PUSH_SEND_CONFIRMED = 0x82
PUSH_MSG_WAITING = 0x83
PUSH_LOGIN_SUCCESS = 0x85
PUSH_LOGIN_FAIL = 0x86
PUSH_STATUS_RESPONSE = 0x87
PUSH_LOG_RX = 0x88
PUSH_NEW_ADVERT = 0x8A

ADV_CHAT, ADV_REPEATER, ADV_ROOM, ADV_SENSOR = 1, 2, 3, 4
TXT_PLAIN, TXT_CLI, TXT_SIGNED = 0, 1, 2
PAYLOAD_TXT_MSG, PAYLOAD_GRP_TXT = 2, 5

ERR_NAMES = {1: "unsupported", 2: "not found", 3: "table full", 4: "bad state", 5: "file error", 6: "illegal argument"}


def batt_percent(mv: int) -> int:
    """The scale the node's own screen uses: straight line, 3.0 V to 4.2 V."""
    return max(0, min(100, (mv - 3000) * 100 // 1200))


def _u32(b: bytes, at: int) -> int:
    return struct.unpack_from("<I", b, at)[0]


def _i32(b: bytes, at: int) -> int:
    return struct.unpack_from("<i", b, at)[0]


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


@dataclass
class HopList:
    """Each hop is the first hash_size bytes of a repeater's key."""
    hash_size: int = 1
    data: bytes = b""

    def hops(self) -> list[bytes]:
        n = self.hash_size
        return [self.data[i:i + n] for i in range(0, len(self.data) - n + 1, n)]


def hops_from_path_len(path_len: int) -> int:
    """Low 6 bits count the hops, top 2 give the hash size. 0xFF: direct/unknown."""
    return -1 if path_len == 0xFF else path_len & 63


def _path(path_len: int, src: bytes) -> HopList:
    size = (path_len >> 6) + 1
    return HopList(size, bytes(src[:(path_len & 63) * size]))


@dataclass
class RawPacket:
    """One packet the node's radio heard, from PUSH_LOG_RX."""
    payload_type: int
    flood: bool
    hops: int
    path: HopList
    payload: bytes
    snr: float
    rssi: int

    @property
    def payload_key(self) -> bytes:
        return self.payload  # identical in every copy of one packet


@dataclass
class Contact:
    key: bytes  # full 32-byte public key
    type: int
    hops: int  # -1 = no known path
    path: HopList
    name: str
    last_advert: int
    lat: float
    lon: float
    last_mod: int
    flags: int = 0  # low bit: favourite; the rest are telemetry permissions

    @property
    def prefix(self) -> bytes:
        return self.key[:KEY_PREFIX]


@dataclass
class TextMessage:
    is_channel: bool
    channel: int = 0
    key: bytes = b""  # DMs: sender's 6-byte prefix
    txt_type: int = TXT_PLAIN
    hops: int = -1
    snr: float | None = None
    timestamp: int = 0  # by the sender's clock
    who: str = ""  # channel messages: the name the text started with
    text: str = ""


@dataclass
class Frame:
    code: int
    kind: str  # see decode()
    data: dict = field(default_factory=dict)

    def __getattr__(self, k):
        try:
            return self.data[k]
        except KeyError:
            raise AttributeError(k) from None


def _decode_log_rx(b: bytes) -> RawPacket | None:
    """code, snr*4, rssi, then the packet as it came off the air: header,
    [4 bytes of transport codes], path_len, path, payload."""
    if len(b) < 5:
        return None
    snr = struct.unpack_from("b", b, 1)[0] / 4.0
    rssi = struct.unpack_from("b", b, 2)[0]
    header = b[3]
    route = header & 3
    ptype = (header >> 2) & 15
    at = 4 + (4 if route in (0, 3) else 0)
    if at >= len(b):
        return None
    path_len = b[at]
    at += 1
    if path_len >> 6 == 3:
        return None
    n = (path_len & 63) * ((path_len >> 6) + 1)
    if at + n >= len(b):
        return None
    return RawPacket(ptype, route in (0, 1), path_len & 63, _path(path_len, b[at:at + n]), bytes(b[at + n:]), snr, rssi)


# struct RepeaterStats in examples/simple_repeater/MyMesh.h. Older firmware
# sends a shorter one, so missing fields read as zero.
_STATS = struct.Struct("<HHhhIIIIIIIIHhHHII")
_STATS_FIELDS = ("batt_mv", "tx_queue", "noise_floor", "last_rssi", "recv", "sent", "air_secs", "up_secs",
                 "sent_flood", "sent_direct", "recv_flood", "recv_direct", "errors", "last_snr",
                 "direct_dups", "flood_dups", "rx_air_secs", "recv_errors")


def _decode_status(b: bytes) -> dict:
    s = dict(zip(_STATS_FIELDS, _STATS.unpack(bytes(b[:_STATS.size]).ljust(_STATS.size, b"\0"))))
    s["last_snr"] /= 4.0
    return s


CONTACT_LEN = 148


def _decode_contact(b: bytes) -> Contact:
    """code, key[32], type, flags, out_path_len, out_path[64], name[32],
    last_advert, lat, lon, lastmod"""
    pl = b[35]
    return Contact(
        key=bytes(b[1:33]), type=b[33], hops=hops_from_path_len(pl),
        path=_path(pl, b[36:100]) if pl != 0xFF else HopList(),
        name=_cstr(b[100:132]), last_advert=_u32(b, 132),
        lat=_i32(b, 136) / 1e6, lon=_i32(b, 140) / 1e6, last_mod=_u32(b, 144), flags=b[34])


def _decode_contact_msg(b: bytes, snr) -> TextMessage | None:
    """key[6], path_len, txt_type, timestamp, [author prefix if signed], text"""
    if len(b) < 12:
        return None
    t = b[7]
    at = 16 if t == TXT_SIGNED else 12
    if len(b) < at:
        return None
    return TextMessage(False, key=bytes(b[:6]), hops=hops_from_path_len(b[6]), txt_type=t,
                       timestamp=_u32(b, 8), text=_cstr(b[at:]), snr=snr)


def _decode_channel_msg(b: bytes, snr) -> TextMessage | None:
    """channel, path_len, txt_type, timestamp, "sender: text" """
    if len(b) < 7:
        return None
    whole = _cstr(b[7:])
    who, sep, text = whole.partition(": ")
    if not sep or len(who) > 31:
        who, text = "", whole
    return TextMessage(True, channel=b[0], hops=hops_from_path_len(b[1]), txt_type=b[2],
                       timestamp=_u32(b, 3), who=who, text=text, snr=snr)


def decode(b: bytes) -> Frame | None:
    """None if it's too short for its code. Unknown codes give kind 'other'."""
    if not b:
        return None
    c = b[0]
    n = len(b)
    F = lambda kind, **d: Frame(c, kind, d)  # noqa: E731
    if c == RESP_OK:
        return F("ok", value=_u32(b, 1) if n >= 5 else 0)
    if c == RESP_ERR:
        return F("err", err=b[1] if n > 1 else 0)
    if c == RESP_CONTACTS_START:
        return F("contacts_start", count=_u32(b, 1)) if n >= 5 else None
    if c in (RESP_CONTACT, PUSH_NEW_ADVERT):
        return F("contact", contact=_decode_contact(b), is_new=c == PUSH_NEW_ADVERT) if n >= CONTACT_LEN else None
    if c == RESP_END_OF_CONTACTS:
        return F("contacts_end", since=_u32(b, 1) if n >= 5 else 0)
    if c == RESP_SELF_INFO:
        # code, type, tx, max_tx, key[32], lat, lon, multi_acks, advert_policy,
        # telemetry, manual_add, freq, bw, sf, cr, name
        if n < 58:
            return None
        return F("self_info", key=bytes(b[4:36]), tx_power=b[2], max_tx=b[3],
                 lat=_i32(b, 36) / 1e6, lon=_i32(b, 40) / 1e6,
                 freq=_u32(b, 48) / 1000, bw=_u32(b, 52) / 1000, sf=b[56], cr=b[57], name=_cstr(b[58:]))
    if c == RESP_CURR_TIME:
        return F("time", time=_u32(b, 1)) if n >= 5 else None
    if c == RESP_NO_MORE_MESSAGES:
        return F("no_more")
    if c == RESP_BATT:
        return F("battery", mv=b[1] | b[2] << 8) if n >= 3 else None
    if c == RESP_DEVICE_INFO:
        # code, ver, max_contacts/2, max_channels, ble_pin, date[12], maker[40], version[20]
        if n < 2:
            return None
        return F("device_info", protocol=b[1], max_contacts=b[2] * 2 if n > 2 else 0,
                 max_channels=b[3] if n > 3 else 8, build=_cstr(b[8:20]) if n >= 20 else "",
                 model=_cstr(b[20:60]) if n >= 60 else "", fw=_cstr(b[60:80]) if n >= 61 else "")
    if c in (RESP_CONTACT_MSG, RESP_CHANNEL_MSG, RESP_CONTACT_MSG_V3, RESP_CHANNEL_MSG_V3):
        # V3 adds snr*4 and 2 reserved bytes. Messages the node queued while no
        # app was connected stay in the old format.
        v3 = c in (RESP_CONTACT_MSG_V3, RESP_CHANNEL_MSG_V3)
        if v3 and n < 4:
            return None
        snr = struct.unpack_from("b", b, 1)[0] / 4.0 if v3 else None
        body = b[4:] if v3 else b[1:]
        dec = _decode_channel_msg if c in (RESP_CHANNEL_MSG, RESP_CHANNEL_MSG_V3) else _decode_contact_msg
        m = dec(body, snr)
        return F("message", msg=m) if m else None
    if c == RESP_CHANNEL_INFO:
        if n < 34:
            return None
        secret = bytes(b[34:50]) if n >= 50 else b""
        return F("channel_info", idx=b[1], name=_cstr(b[2:34]), secret=secret if any(secret) else b"")
    if c == RESP_SENT:
        return F("sent", flood=bool(b[1]), ack=_u32(b, 2), timeout_ms=_u32(b, 6)) if n >= 10 else None
    if c == PUSH_SEND_CONFIRMED:
        return F("confirmed", ack=_u32(b, 1), trip_ms=_u32(b, 5)) if n >= 9 else None
    if c == PUSH_MSG_WAITING:
        return F("msg_waiting")
    if c == PUSH_ADVERT:
        return F("advert", key=bytes(b[1:33])) if n >= 7 else None
    if c == PUSH_PATH_UPDATED:
        return F("path_updated", key=bytes(b[1:33])) if n >= 33 else None
    if c == PUSH_LOGIN_SUCCESS:
        # code, is_admin, key[6], then on current firmware: server time, acl, fw level.
        # Old repeaters send only the prefix and can't say whether we're admin.
        if n < 8:
            return None
        return F("login_ok", prefix=bytes(b[2:8]), is_admin=bool(b[1]) if n >= 12 else None)
    if c == PUSH_LOGIN_FAIL:
        return F("login_fail", prefix=bytes(b[2:8])) if n >= 8 else None
    if c == PUSH_STATUS_RESPONSE:
        return F("status", prefix=bytes(b[2:8]), stats=_decode_status(b[8:])) if n > 8 else None
    if c == PUSH_LOG_RX:
        r = _decode_log_rx(b)
        return F("log_rx", raw=r) if r else F("other")  # odd packets on the air aren't an error
    return F("other")


# --- encoders ---------------------------------------------------------------

def app_start(name: str = "MeshCore Desktop") -> bytes:
    return bytes([CMD_APP_START]) + bytes(7) + name.encode()


def device_query() -> bytes:
    return bytes([CMD_DEVICE_QUERY, 3])  # the protocol we speak: asks for V3 message frames


def get_channel(idx: int) -> bytes:
    return bytes([CMD_GET_CHANNEL, idx])


def set_channel(idx: int, name: str, secret: bytes) -> bytes:
    return bytes([CMD_SET_CHANNEL, idx]) + name.encode()[:31].ljust(32, b"\0") + secret[:16].ljust(16, b"\0")


def send_channel(channel: int, ts: int, text: str) -> bytes:
    return struct.pack("<BBBI", CMD_SEND_CHANNEL_TXT_MSG, TXT_PLAIN, channel, ts) + text.encode()


def send_direct(prefix: bytes, ts: int, text: str, attempt: int = 0) -> bytes:
    return struct.pack("<BBBI", CMD_SEND_TXT_MSG, TXT_PLAIN, attempt, ts) + prefix[:6] + text.encode()


def send_cli(prefix: bytes, ts: int, text: str) -> bytes:
    """A command for a repeater we're logged in to. The reply comes back as a DM of type TXT_CLI."""
    return struct.pack("<BBBI", CMD_SEND_TXT_MSG, TXT_CLI, 0, ts) + prefix[:6] + text.encode()


def login(key: bytes, password: str) -> bytes:
    return bytes([CMD_SEND_LOGIN]) + key[:32] + password.encode()[:PASSWORD_MAX]


def status_req(key: bytes) -> bytes:
    return bytes([CMD_SEND_STATUS_REQ]) + key[:32]


def set_time(ts: int) -> bytes:
    return struct.pack("<BI", CMD_SET_DEVICE_TIME, ts)


def send_advert(flood: bool) -> bytes:
    return bytes([CMD_SEND_SELF_ADVERT, 1 if flood else 0])


def reset_path(key: bytes) -> bytes:
    return bytes([CMD_RESET_PATH]) + key[:32]


def get_contact(key: bytes) -> bytes:
    return bytes([CMD_GET_CONTACT_BY_KEY]) + key[:32]


def simple(cmd: int) -> bytes:
    return bytes([cmd])


# --- channel packets ----------------------------------------------------------

def decrypt_group(secret: bytes, payload: bytes) -> tuple[int, str] | None:
    """A GRP_TXT payload: channel hash, 2-byte MAC, AES-128-ECB blocks of
    timestamp, type byte, "sender: text". None if it isn't whole blocks."""
    cipher = payload[3:]
    if not secret or not cipher or len(cipher) % 16:
        return None
    d = Cipher(algorithms.AES(secret[:16]), modes.ECB()).decryptor()
    plain = d.update(cipher) + d.finalize()
    return _u32(plain, 0), _cstr(plain[5:])


def encrypt_group(secret: bytes, ts: int, who: str, text: str) -> bytes:
    """The inverse, for the simulator and tests. Channel hash and MAC are fake."""
    plain = struct.pack("<IB", ts, 0) + f"{who}: {text}".encode()
    plain += bytes(-len(plain) % 16)
    e = Cipher(algorithms.AES(secret[:16]), modes.ECB()).encryptor()
    return b"\x11\x22\x33" + e.update(plain) + e.finalize()
