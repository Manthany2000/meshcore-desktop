"""A pretend companion node, speaking the same frames as the real one, for
running the app with no radio: channel chatter with repeats, DMs with acks."""
from __future__ import annotations

import asyncio
import hashlib
import random
import struct
import time

from . import protocol as P

PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


def _key(name: str) -> bytes:
    return hashlib.sha256(name.encode()).digest()


CONTACTS = [  # name, type, lat, lon, hops
    ("Ridge Repeater", P.ADV_REPEATER, 42.36, -71.20, 0),
    ("Hilltop RPT", P.ADV_REPEATER, 42.40, -71.10, 1),
    ("Water Tower", P.ADV_REPEATER, 42.30, -71.30, 2),
    ("Library Roof", P.ADV_REPEATER, 42.33, -71.05, 1),
    ("alice", P.ADV_CHAT, 42.35, -71.15, 1),
    ("bob-t1000", P.ADV_CHAT, 42.38, -71.00, 2),
    ("Kestrel", P.ADV_CHAT, 0, 0, -1),
    ("NHbot", P.ADV_CHAT, 42.90, -71.40, 3),
    ("Town Room", P.ADV_ROOM, 42.34, -71.12, 1),
]

CHATTER = ["anyone on tonight?", "test from the car", "good copy here, 3 hops", "@[{me}] you're coming in clear",
           "new repeater up on the ridge", "weather's turning, signal is great", "ping", "73 all"]


class SimTransport:
    def __init__(self):
        self.on_frame = lambda b: None
        self.on_disconnect = lambda: None
        self.name = "SimNode"
        self.key = _key("sim-self")
        self.clock = int(time.time()) - 3600  # a bit behind, so the app sets it
        self.queue: list[bytes] = []
        self.contacts = [(n, _key(n), t, la, lo, h) for n, t, la, lo, h in CONTACTS]
        self.repeaters = [c for c in self.contacts if c[2] == P.ADV_REPEATER]
        self.channels = {0: ("Public", PUBLIC_SECRET), 1: ("#test", hashlib.sha256(b"#test").digest()[:16])}
        self.task = None
        self.admins: set[bytes] = set()  # repeaters that have us on their admin list
        self.repeater_cfg: dict[bytes, dict] = {}
        now = int(time.time())
        # Queued while no app was connected: the old frame format.
        self.queue.append(self._chan_msg(0, "alice", "morning all", now - 1800, v3=False))
        self.queue.append(self._dm(self.contacts[5][1], "you around later?", now - 900, v3=False))

    async def connect(self):
        await asyncio.sleep(0.3)
        self.task = asyncio.create_task(self._chatter())

    async def close(self):
        if self.task:
            self.task.cancel()

    def _push(self, b: bytes, delay: float = 0.0):
        asyncio.get_running_loop().call_later(delay, self.on_frame, b)

    # --- frames out ---------------------------------------------------------

    def _contact_frame(self, c, code=P.RESP_CONTACT) -> bytes:
        name, key, typ, lat, lon, hops = c
        path_len = 0xFF if hops < 0 else hops
        path = bytes(r[1][0] for r in self.repeaters[:max(hops, 0)]).ljust(64, b"\0")
        return (bytes([code]) + key + bytes([typ, 0, path_len]) + path + name.encode().ljust(32, b"\0")
                + struct.pack("<IiiI", self.clock - 600, int(lat * 1e6), int(lon * 1e6), self.clock - 300))

    def _chan_msg(self, ch, who, text, ts, hops=1, snr=6.5, v3=True) -> bytes:
        body = struct.pack("<BBBI", ch, hops, 0, ts) + f"{who}: {text}".encode()
        return (bytes([P.RESP_CHANNEL_MSG_V3, int(snr * 4) & 255, 0, 0]) if v3 else bytes([P.RESP_CHANNEL_MSG])) + body

    def _dm(self, key, text, ts, hops=2, snr=4.0, v3=True, txt=0) -> bytes:
        body = key[:6] + struct.pack("<BBI", hops, txt, ts) + text.encode()
        return (bytes([P.RESP_CONTACT_MSG_V3, int(snr * 4) & 255, 0, 0]) if v3 else bytes([P.RESP_CONTACT_MSG])) + body

    def _log_rx(self, ptype, path: list[bytes], payload: bytes, snr=None) -> bytes:
        snr = snr if snr is not None else random.uniform(-8, 10)
        header = (ptype << 2) | 1  # flood
        return (bytes([P.PUSH_LOG_RX, int(snr * 4) & 255, random.randint(-120, -60) & 255, header, len(path)])
                + b"".join(path) + payload)

    def _copies(self, ptype, payload, first_hops=0, n=None):
        """Schedules relayed copies of one packet, as repeaters pass it on."""
        n = n if n is not None else random.randint(1, 4)
        t = 0.0
        for _ in range(n):
            t += random.uniform(0.4, 2.5)
            k = random.randint(max(first_hops, 1), 3)
            path = [r[1][:1] for r in random.sample(self.repeaters, k)]
            self._push(self._log_rx(ptype, path, payload), t)

    async def _chatter(self):
        me = self.name
        while True:
            await asyncio.sleep(random.uniform(10, 20))
            sender = random.choice([c for c in self.contacts if c[2] == P.ADV_CHAT])
            if random.random() < 0.75:
                ch = random.choice(list(self.channels))
                text = random.choice(CHATTER).format(me=me)
                ts = self.clock_now()
                payload = P.encrypt_group(self.channels[ch][1], ts, sender[0], text)
                hops = random.randint(1, 2)
                path = [r[1][:1] for r in random.sample(self.repeaters, hops)]
                self._push(self._log_rx(P.PAYLOAD_GRP_TXT, path, payload, 7.0))  # the copy the node acts on
                self.queue.append(self._chan_msg(ch, sender[0], text, ts, hops))
                self._push(bytes([P.PUSH_MSG_WAITING]), 0.05)
                self._copies(P.PAYLOAD_GRP_TXT, payload, hops)
            else:
                self.queue.append(self._dm(sender[1], random.choice(["hey", "got your msg", "what's your antenna?"]),
                                           self.clock_now()))
                self._push(bytes([P.PUSH_MSG_WAITING]))
            if random.random() < 0.3:
                self._push(bytes([P.PUSH_ADVERT]) + random.choice(self.contacts)[1])

    def clock_now(self):
        return self.clock + int(time.monotonic() - self._t0) if hasattr(self, "_t0") else self.clock

    # --- a pretend repeater: password "admin" or "guest". Library Roof is out of range.

    def _repeater(self, key_or_prefix: bytes):
        c = next((c for c in self.repeaters if c[1].startswith(key_or_prefix)), None)
        return None if c is None or c[0] == "Library Roof" else c

    def _login(self, b: bytes):
        key, pw = b[1:33], b[33:].decode()
        self._push(struct.pack("<BBII", P.RESP_SENT, 0, 1, 500))
        if not self._repeater(key):
            return
        if pw == "admin" or (not pw and key[:6] in self.admins):
            self.admins.add(key[:6])
            self._push(bytes([P.PUSH_LOGIN_SUCCESS, 1]) + key[:6] + struct.pack("<IBB", self.clock_now(), 3, 8), 0.4)
        elif pw == "guest":
            self._push(bytes([P.PUSH_LOGIN_SUCCESS, 0]) + key[:6] + struct.pack("<IBB", self.clock_now(), 1, 8), 0.4)
        else:
            self._push(bytes([P.PUSH_LOGIN_FAIL, 0]) + key[:6], 0.4)

    @staticmethod
    def _set(cfg: dict, key: str, value: str) -> str:
        if key == "advert.interval":
            if not 0 <= int(value) <= 240:
                return "Error: interval range is 60-240 minutes"
            value = str(int(value) // 2 * 2)  # the repeater stores it in units of 2 minutes
        cfg[key] = value
        return f"OK - repeat is now {value.upper()}" if key == "repeat" else "OK"

    def _status(self, b: bytes):
        key = b[1:33]
        self._push(struct.pack("<BBII", P.RESP_SENT, 0, 2, 500))
        if self._repeater(key):
            self._push(bytes([P.PUSH_STATUS_RESPONSE, 0]) + key[:6] + struct.pack(
                "<HHhhIIIIIIIIHhHHII", 3987, 1, -112, -71, 48213, 21877, 1842, 412345, 18000, 3877, 40000, 8213,
                2, 26, 15, 930, 5120, 3), 0.4)

    def _cli(self, b: bytes):
        prefix, cmd = b[7:13], b[13:].decode()
        self._push(struct.pack("<BBII", P.RESP_SENT, 0, 0, 500))
        if not self._repeater(prefix) or prefix not in self.admins:
            return  # a real repeater ignores commands from anyone but an admin
        cfg = self.repeater_cfg.setdefault(prefix, {
            "name": self._repeater(prefix)[0], "repeat": "on", "tx": "22", "advert.interval": "120",
            "flood.advert.interval": "12", "flood.max": "64", "lat": "42.36", "lon": "-71.2", "owner.info": ""})
        parts = cmd.split(" ", 2)
        if parts[0] == "get" and len(parts) == 2 and parts[1] in cfg:
            reply = f"> {cfg[parts[1]]}"
        elif parts[0] == "set" and len(parts) == 3 and parts[1] in cfg:
            reply = self._set(cfg, parts[1], parts[2])
        else:
            reply = {"ver": "v1.11.0 (Build: 05-Nov-2026)", "clock": "12:00 - 8/11/2026 UTC", "board": "Heltec V3",
                     "neighbors": "A1B2C3D4:42:26\n99F0E1AA:310:-8",
                     "advert": "OK - Advert sent"}.get(cmd, "Unknown command")
        self.queue.append(self._dm(prefix, reply, self.clock_now(), txt=P.TXT_CLI))
        self._push(bytes([P.PUSH_MSG_WAITING]), 0.5)

    # --- commands in --------------------------------------------------------

    async def write(self, b: bytes):
        await asyncio.sleep(0.01)
        c = b[0]
        out = self._push
        if c == P.CMD_APP_START:
            self._t0 = time.monotonic()
            out(bytes([P.RESP_SELF_INFO, 1, 22, 22]) + self.key + struct.pack("<ii", 42356000, -71100000)
                + bytes(4) + struct.pack("<IIBB", 910525, 62500, 7, 5) + self.name.encode())
        elif c == P.CMD_DEVICE_QUERY:
            out(bytes([P.RESP_DEVICE_INFO, 14, 175, 8]) + bytes(4) + b"05 Nov 2026".ljust(12, b"\0")
                + b"Simulated".ljust(40, b"\0") + b"sim-1.0".ljust(20, b"\0"))
        elif c == P.CMD_GET_DEVICE_TIME:
            out(struct.pack("<BI", P.RESP_CURR_TIME, self.clock_now()))
        elif c == P.CMD_SET_DEVICE_TIME:
            self.clock = struct.unpack_from("<I", b, 1)[0]
            self._t0 = time.monotonic()
            out(bytes([P.RESP_OK]))
        elif c == P.CMD_GET_BATT_AND_STORAGE:
            out(bytes([P.RESP_BATT]) + struct.pack("<H", random.randint(3900, 4100)) + bytes(8))
        elif c == P.CMD_GET_CHANNEL:
            i = b[1]
            name, sec = self.channels.get(i, ("", bytes(16)))
            out(bytes([P.RESP_CHANNEL_INFO, i]) + name.encode().ljust(32, b"\0") + sec)
        elif c == P.CMD_SET_CHANNEL:
            name = P._cstr(b[2:34])
            if name:
                self.channels[b[1]] = (name, b[34:50])
            else:
                self.channels.pop(b[1], None)
            out(bytes([P.RESP_OK]))
        elif c == P.CMD_GET_CONTACTS:
            out(struct.pack("<BI", P.RESP_CONTACTS_START, len(self.contacts)))
            for i, ct in enumerate(self.contacts):
                out(self._contact_frame(ct), 0.005 * i)
            out(struct.pack("<BI", P.RESP_END_OF_CONTACTS, self.clock_now()), 0.005 * len(self.contacts) + 0.01)
        elif c == P.CMD_SYNC_NEXT_MESSAGE:
            out(self.queue.pop(0) if self.queue else bytes([P.RESP_NO_MORE_MESSAGES]))
        elif c == P.CMD_SEND_CHANNEL_TXT_MSG:
            ch, ts = b[2], struct.unpack_from("<I", b, 3)[0]
            text = b[7:].decode()
            if ch not in self.channels:
                out(bytes([P.RESP_ERR, 2]))
                return
            out(bytes([P.RESP_OK]))
            self._copies(P.PAYLOAD_GRP_TXT, P.encrypt_group(self.channels[ch][1], ts, self.name, text))
        elif c == P.CMD_SEND_LOGIN:
            self._login(b)
        elif c == P.CMD_SEND_STATUS_REQ:
            self._status(b)
        elif c == P.CMD_SEND_TXT_MSG and b[1] == P.TXT_CLI:
            self._cli(b)
        elif c == P.CMD_SEND_TXT_MSG:
            dest = b[7:13]
            ack = random.getrandbits(32)
            out(struct.pack("<BBII", P.RESP_SENT, 1, ack, 6000))
            payload = bytes([dest[0], self.key[0]]) + random.randbytes(20)
            self._copies(P.PAYLOAD_TXT_MSG, payload, n=random.randint(0, 2))
            if random.random() < 0.8:
                out(struct.pack("<BII", P.PUSH_SEND_CONFIRMED, ack, random.randint(900, 4000)), random.uniform(1, 4))
                if random.random() < 0.5:
                    self.queue.append(self._dm(dest, "copy that 👍", self.clock_now()))
                    out(bytes([P.PUSH_MSG_WAITING]), 5)
        elif c in (P.CMD_SEND_SELF_ADVERT, P.CMD_RESET_PATH):
            out(bytes([P.RESP_OK]))
        else:
            out(bytes([P.RESP_ERR, 1]))
