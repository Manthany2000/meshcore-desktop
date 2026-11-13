"""What we know about the mesh, saved per node in SQLite. Knows nothing of Qt or
BLE: frames go in through apply(), changes come out through listeners."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as P

CLOCK_SANE = 1_700_000_000  # late 2023: a node never told the time reads less
COPY_CLAIM_S = 30  # how old a logged packet can be and still belong to a new message
NO_SCOPE = "*"  # a channel's scope: floods with no region at all
REPEAT_WINDOW_S = 120  # how long a flood can still be going round

from .desktop import data_dir

DATA_DIR = data_dir()

INCOMING, SENDING, SENT, WAITING, DELIVERED, NO_ACK, FAILED = (
    "incoming", "sending", "sent", "waiting", "delivered", "no_ack", "failed")


def chan_conv(idx: int) -> str:
    return f"ch:{idx}"


def dm_conv(prefix: bytes) -> str:
    return f"dm:{prefix[:P.KEY_PREFIX].hex()}"


@dataclass
class Repeater:
    hash: str  # hex
    direct: bool = False  # it was the last hop of a copy our node heard
    snr: float | None = None  # best of those copies


@dataclass
class Message:
    id: int
    conv: str
    outgoing: bool
    sender: str
    text: str
    wire_time: int  # the timestamp inside the packet
    at: float  # local epoch it arrived or was sent
    status: str = INCOMING
    hops: int = -1  # -1 = direct route
    snr: float | None = None
    repeats: int = 0  # relayed copies our node heard
    repeaters: dict[str, Repeater] = field(default_factory=dict)
    path: list[str] | None = None  # hop hashes of the copy the node acted on
    path_size: int = 1
    ack: int = 0
    trip_ms: int = 0
    deadline: float = 0
    live: bool = False  # still collecting repeats this session
    payload_key: bytes = b""

    @property
    def is_channel(self) -> bool:
        return self.conv.startswith("ch:")

    @property
    def channel(self) -> int:
        return int(self.conv[3:]) if self.is_channel else -1

    @property
    def dm_prefix(self) -> bytes:
        return bytes.fromhex(self.conv[3:]) if not self.is_channel else b""


@dataclass
class Channel:
    idx: int
    name: str
    secret: bytes = b""  # never logged or shown


class Model:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.listeners: list = []  # fn(event: str, obj)
        self.db: sqlite3.Connection | None = None
        self.reset()

    def reset(self):
        self.self_info = None
        self.device_info = None
        self.battery_mv = 0
        self.clock_offset = 0  # node clock minus ours, seconds
        self.contacts: dict[bytes, P.Contact] = {}  # by 6-byte prefix
        self.heard: dict[bytes, float] = {}  # prefix -> local time heard live
        self.channels: dict[int, Channel] = {}
        self.messages: list[Message] = []
        self.by_id: dict[int, Message] = {}
        self.last_read: dict[str, float] = {}
        self.raw_log: deque = deque(maxlen=64)  # [packet, at, claimed]
        self.next_temp = -1
        self.packets: deque = deque(maxlen=2000)  # (at, RawPacket), this session only
        self.saved_names: dict[bytes, str] = {}  # every node this companion has ever had, from the database
        self.first_sync = True
        self.found_away: list[P.Contact] = []  # in the node's contact list but new to us
        self.last_snr: dict[bytes, float] = {}  # prefix -> SNR of its latest message
        self.flood_always: set[bytes] = set()  # contact prefixes whose DMs always flood
        # Channel slot -> (channel name, scope): "" the node's default, NO_SCOPE, or a region.
        # Kept here because the node has no per-channel scope; it applies while the name matches.
        self.channel_scopes: dict[int, tuple[str, str]] = {}
        self.default_scope: str | None = None  # the node's, "" for none; None: not known

    # --- plumbing -----------------------------------------------------------

    def emit(self, event: str, obj=None):
        for fn in list(self.listeners):
            fn(event, obj)

    def open_for_node(self, key: bytes):
        """Messages are kept per node, so a second node doesn't mix in."""
        if self.db:
            self.db.close()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / f"node-{key[:4].hex()}.db")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY, conv TEXT, outgoing INT, sender TEXT, text TEXT,
              wire_time INT, at REAL, status TEXT, hops INT, snr REAL, repeats INT,
              repeaters TEXT, path TEXT, path_size INT, ack INT, trip_ms INT);
            CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv, at);
            CREATE TABLE IF NOT EXISTS channels (idx INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS contacts (key TEXT PRIMARY KEY, name TEXT, type INT);
            CREATE TABLE IF NOT EXISTS reads (conv TEXT PRIMARY KEY, at REAL);
            CREATE TABLE IF NOT EXISTS flood_always (prefix TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS channel_scopes (idx INTEGER PRIMARY KEY, name TEXT, scope TEXT);
            CREATE TABLE IF NOT EXISTS repeater_stats (prefix TEXT, at REAL, batt_mv INT, noise_floor INT, last_rssi INT);
            CREATE INDEX IF NOT EXISTS repeater_stats_key ON repeater_stats(prefix, at);
        """)
        self.messages.clear()
        self.by_id.clear()
        for row in self.db.execute("SELECT * FROM messages ORDER BY at"):
            m = Message(id=row[0], conv=row[1], outgoing=bool(row[2]), sender=row[3], text=row[4],
                        wire_time=row[5], at=row[6], status=row[7], hops=row[8], snr=row[9],
                        repeats=row[10], path=json.loads(row[12]) if row[12] else None,
                        path_size=row[13] or 1, ack=row[14] or 0, trip_ms=row[15] or 0)
            for r in json.loads(row[11] or "[]"):
                m.repeaters[r["hash"]] = Repeater(r["hash"], r["direct"], r["snr"])
            if m.status in (SENDING, WAITING):
                m.status = NO_ACK if m.status == WAITING else FAILED
            self.messages.append(m)
            self.by_id[m.id] = m
        for idx, name in self.db.execute("SELECT idx, name FROM channels"):
            self.channels.setdefault(idx, Channel(idx, name))
        # Names for DMs restored before the node's contact list arrives.
        self.saved_names = {bytes.fromhex(k): n for k, n, _ in self.db.execute("SELECT * FROM contacts")}
        self.first_sync = not self.saved_names  # everything looks new the first time: that isn't news
        self.found_away = []
        self.last_read = dict(self.db.execute("SELECT conv, at FROM reads"))
        self.flood_always = {bytes.fromhex(k) for k, in self.db.execute("SELECT prefix FROM flood_always")}
        self.channel_scopes = {i: (n, sc) for i, n, sc in self.db.execute("SELECT * FROM channel_scopes")}
        self.emit("reload")

    def take_found_away(self) -> list:
        found, self.found_away = self.found_away, []
        return found

    def _save(self, m: Message):
        if not self.db:
            return
        reps = json.dumps([r.__dict__ for r in m.repeaters.values()])
        vals = (m.conv, int(m.outgoing), m.sender, m.text, m.wire_time, m.at, m.status, m.hops, m.snr,
                m.repeats, reps, json.dumps(m.path) if m.path is not None else None, m.path_size, m.ack, m.trip_ms)
        if m.id < 0:
            cur = self.db.execute("INSERT INTO messages (conv, outgoing, sender, text, wire_time, at, status, hops,"
                                  " snr, repeats, repeaters, path, path_size, ack, trip_ms)"
                                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            del self.by_id[m.id]
            m.id = cur.lastrowid
            self.by_id[m.id] = m
        else:
            self.db.execute("UPDATE messages SET conv=?, outgoing=?, sender=?, text=?, wire_time=?, at=?, status=?,"
                            " hops=?, snr=?, repeats=?, repeaters=?, path=?, path_size=?, ack=?, trip_ms=?"
                            " WHERE id=?", vals + (m.id,))
        self.db.commit()

    def _changed(self, m: Message):
        self._save(m)
        self.emit("message_changed", m)

    def _new_message(self, m: Message):
        self.messages.append(m)
        self.by_id[m.id] = m
        self._save(m)
        self.emit("message_added", m)

    def _temp_id(self) -> int:
        self.next_temp -= 1
        return self.next_temp

    # --- queries ------------------------------------------------------------

    def node_now(self) -> int:
        return int(time.time()) + self.clock_offset

    def conversation(self, conv: str) -> list[Message]:
        return [m for m in self.messages if m.conv == conv]

    def unread(self, conv: str) -> int:
        since = self.last_read.get(conv, 0)
        return sum(1 for m in self.messages if m.conv == conv and not m.outgoing and m.at > since)

    def mark_read(self, conv: str):
        self.last_read[conv] = time.time()
        if self.db:
            self.db.execute("INSERT OR REPLACE INTO reads VALUES (?, ?)", (conv, self.last_read[conv]))
            self.db.commit()
        self.emit("unread", conv)

    def always_floods(self, prefix: bytes) -> bool:
        return prefix[:P.KEY_PREFIX] in self.flood_always

    def channel_scope(self, idx: int) -> str:
        """The scope messages on this channel go out with: "" for the node's
        default, NO_SCOPE for none, or a region name."""
        ch = self.channels.get(idx)
        saved = self.channel_scopes.get(idx)
        return saved[1] if ch and saved and saved[0] == ch.name else ""

    def set_channel_scope(self, idx: int, scope: str):
        ch = self.channels.get(idx)
        if not ch:
            return
        scope = scope if scope == NO_SCOPE else P.region_name(scope)
        if scope:
            self.channel_scopes[idx] = (ch.name, scope)
        else:
            self.channel_scopes.pop(idx, None)
        if self.db:
            if scope:
                self.db.execute("INSERT OR REPLACE INTO channel_scopes VALUES (?,?,?)", (idx, ch.name, scope))
            else:
                self.db.execute("DELETE FROM channel_scopes WHERE idx = ?", (idx,))
            self.db.commit()
        self.emit("channels")

    def set_always_flood(self, prefix: bytes, on: bool):
        """Marks a contact so every DM to them floods instead of using a stored path."""
        prefix = prefix[:P.KEY_PREFIX]
        (self.flood_always.add if on else self.flood_always.discard)(prefix)
        if self.db:
            if on:
                self.db.execute("INSERT OR IGNORE INTO flood_always VALUES (?)", (prefix.hex(),))
            else:
                self.db.execute("DELETE FROM flood_always WHERE prefix = ?", (prefix.hex(),))
            self.db.commit()
        self.emit("flood_always", prefix)

    def dm_convs(self) -> list[str]:
        """DM conversations, most recent first."""
        last: dict[str, float] = {}
        for m in self.messages:
            if not m.is_channel:
                last[m.conv] = m.at
        return sorted(last, key=last.get, reverse=True)

    def contact_name(self, prefix: bytes) -> str:
        c = self.contacts.get(prefix[:P.KEY_PREFIX])
        if c and c.name:
            return c.name
        n = getattr(self, "saved_names", {}).get(prefix[:P.KEY_PREFIX])
        return n or prefix[:3].hex()

    def conv_title(self, conv: str) -> str:
        if conv.startswith("ch:"):
            ch = self.channels.get(int(conv[3:]))
            return ch.name if ch and ch.name else f"Channel {conv[3:]}"
        return self.contact_name(bytes.fromhex(conv[3:]))

    def hop_names(self, h: str) -> list[str]:
        """Who a hop hash might be. Repeaters first: they're the ones that relay.
        One-byte hashes collide, so there can be several."""
        hb = bytes.fromhex(h)
        cs = [c for c in self.contacts.values() if c.key.startswith(hb)]
        cs.sort(key=lambda c: c.type != P.ADV_REPEATER)
        return [c.name or c.key[:3].hex() for c in cs]

    def distance_km(self, c: P.Contact) -> float | None:
        s = self.self_info
        if not s or not (s.lat or s.lon) or not (c.lat or c.lon):
            return None
        la1, la2 = math.radians(s.lat), math.radians(c.lat)
        dla, dlo = la2 - la1, math.radians(c.lon - s.lon)
        a = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
        return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def contact_age(self, c: P.Contact) -> float | None:
        if c.prefix in self.heard:
            return time.time() - self.heard[c.prefix]
        if c.last_mod:  # by the node's clock
            return max(0, self.node_now() - c.last_mod)
        return None

    # --- repeats ------------------------------------------------------------

    def _packet_is(self, p: P.RawPacket, m: Message) -> bool:
        """Is this logged packet a copy of this message? A channel packet is
        decrypted and compared, which is exact. A DM can't be decrypted here, but
        names its two ends, and its copies share a payload."""
        if m.is_channel:
            if p.payload_type != P.PAYLOAD_GRP_TXT:
                return False
            ch = self.channels.get(m.channel)
            if not ch or not ch.secret:
                return False
            got = P.decrypt_group(ch.secret, p.payload)
            return got is not None and got[0] == m.wire_time and got[1] == f"{m.sender}: {m.text}"
        if p.payload_type != P.PAYLOAD_TXT_MSG or len(p.payload) < 2:
            return False
        if m.payload_key:
            return p.payload == m.payload_key
        me = self.self_info.key[0] if self.self_info else -1
        them = m.dm_prefix[0]
        dest, src = (them, me) if m.outgoing else (me, them)
        return p.payload[0] == dest and p.payload[1] == src

    def _add_copy(self, m: Message, p: P.RawPacket):
        """A flood's path lists every repeater so far, in order. A direct route's
        lists those still to come, which says nothing about who relayed it."""
        if not m.payload_key:
            m.payload_key = p.payload
        if p.flood and p.hops == 0:
            return  # the sender's own transmission, not a repeat
        m.repeats += 1
        if not p.flood:
            return
        hops = p.path.hops()
        for i, h in enumerate(hops):
            r = m.repeaters.setdefault(h.hex(), Repeater(h.hex()))
            if i == len(hops) - 1 and (not r.direct or (r.snr is not None and p.snr > r.snr)):
                r.direct, r.snr = True, p.snr

    def _claim_copies(self, m: Message):
        """A new message collects the copies logged before its frame got here. The
        first with its hop count is the copy the node acted on: that's its path."""
        now = time.time()
        for e in self.raw_log:
            p, at, claimed = e
            if claimed or now - at > COPY_CLAIM_S or not self._packet_is(p, m):
                continue
            e[2] = True
            if m.path is None and (not p.flood if m.hops < 0 else (p.flood and p.hops == m.hops)):
                m.path, m.path_size = [h.hex() for h in p.path.hops()], p.path.hash_size
            self._add_copy(m, p)

    def _on_raw(self, p: P.RawPacket):
        now = time.time()
        self.packets.append((now, p))
        self.emit("packet", p)
        for m in reversed(self.messages):
            if not m.live or now - m.at > REPEAT_WINDOW_S:
                continue
            if self._packet_is(p, m):
                self._add_copy(m, p)
                self._changed(m)
                return
        # Probably a message whose frame hasn't reached us yet.
        self.raw_log.append([p, now, False])

    # --- frames in ----------------------------------------------------------

    def apply(self, f: P.Frame, live: bool = True):
        k = f.kind
        if k == "self_info":
            new_node = not self.self_info or self.self_info.key != f.key
            self.self_info = f
            if new_node:
                self.open_for_node(f.key)
            self.emit("node")
        elif k == "device_info":
            self.device_info = f
            self.emit("node")
        elif k == "time":
            self.clock_offset = f.time - int(time.time())
        elif k == "battery":
            self.battery_mv = f.mv
            self.emit("node")
        elif k == "contact":
            c = f.contact
            # A node the radio doesn't keep comes back as "new" at every advert, so check our own record too.
            known = c.prefix in self.contacts or c.prefix in self.saved_names
            if not known and not f.is_new and not self.first_sync:
                self.found_away.append(c)
            self.contacts[c.prefix] = c
            self.saved_names[c.prefix] = c.name
            if f.is_new:
                self.heard[c.prefix] = time.time()
            if self.db:
                self.db.execute("INSERT OR REPLACE INTO contacts VALUES (?,?,?)", (c.prefix.hex(), c.name, c.type))
                if f.is_new:
                    self.db.commit()
            self.emit("contact", c)
            if f.is_new and not known:
                self.emit("new_contact", c)
        elif k == "contacts_end":
            self.first_sync = False
            if self.db:
                self.db.commit()
            self.emit("contacts")
        elif k == "advert":
            p = f.key[:P.KEY_PREFIX]
            if p in self.contacts:
                self.heard[p] = time.time()
                self.emit("contact", self.contacts[p])
        elif k == "channel_info":
            if f.name or f.secret:
                self.channels[f.idx] = Channel(f.idx, f.name, f.secret)
                if self.db:
                    self.db.execute("INSERT OR REPLACE INTO channels VALUES (?,?)", (f.idx, f.name))
                    self.db.commit()
            else:
                self.channels.pop(f.idx, None)
            self.emit("channels")
        elif k == "default_scope":
            self.default_scope = f.name
            self.emit("default_scope")
        elif k == "log_rx":
            self._on_raw(f.raw)
        elif k == "confirmed":
            for m in self.messages:
                # A late ack still counts, and the same ack can come more than once.
                if m.outgoing and m.ack == f.ack and m.status in (WAITING, NO_ACK):
                    m.status, m.trip_ms = DELIVERED, f.trip_ms
                    self._changed(m)
        elif k == "message":
            self._add_incoming(f.msg, live)

    def _add_incoming(self, t: P.TextMessage, live: bool):
        if t.txt_type not in (P.TXT_PLAIN, P.TXT_SIGNED):
            return  # replies from a remote console aren't conversation
        conv = chan_conv(t.channel) if t.is_channel else dm_conv(t.key)
        sender = t.who if t.is_channel else self.contact_name(t.key)
        at = time.time()
        # Backlog: the sender's timestamp says when it really happened, if their
        # clock is anywhere near right.
        now = self.node_now()
        if not live and t.timestamp > CLOCK_SANE and 0 < now - t.timestamp < 30 * 86400:
            at -= now - t.timestamp
        m = Message(self._temp_id(), conv, False, sender, t.text, t.timestamp, at,
                    hops=t.hops, snr=t.snr, live=live)
        if live:
            self._claim_copies(m)
        self._new_message(m)
        # A message proves its sender is around.
        if not t.is_channel:
            self.heard[t.key[:P.KEY_PREFIX]] = at
            if t.snr is not None:
                self.last_snr[t.key[:P.KEY_PREFIX]] = t.snr
        else:
            for c in self.contacts.values():
                if c.name == t.who:
                    self.heard[c.prefix] = at
                    if t.snr is not None:
                        self.last_snr[c.prefix] = t.snr
                    break

    # --- repeater history ---------------------------------------------------

    def record_repeater(self, prefix: bytes, stats: dict):
        if self.db:
            self.db.execute("INSERT INTO repeater_stats VALUES (?,?,?,?,?)", (
                prefix.hex(), time.time(), stats["batt_mv"], stats["noise_floor"], stats["last_rssi"]))
            self.db.commit()

    def repeater_history(self, prefix: bytes, limit: int = 500) -> list[tuple]:
        """(at, batt_mv, noise_floor, last_rssi), oldest first."""
        if not self.db:
            return []
        rows = self.db.execute("SELECT at, batt_mv, noise_floor, last_rssi FROM repeater_stats WHERE prefix=?"
                               " ORDER BY at DESC LIMIT ?", (prefix.hex(), limit)).fetchall()
        return rows[::-1]

    # --- outgoing -----------------------------------------------------------

    def add_outgoing(self, conv: str, text: str) -> Message:
        name = self.self_info.name if self.self_info else "me"
        m = Message(self._temp_id(), conv, True, name, text, self.node_now(), time.time(), status=SENDING, live=True)
        self._new_message(m)
        return m

    def send_result(self, m: Message, f: P.Frame | None):
        """The node's reply to the send command."""
        if f is None or f.kind == "err":
            m.status = FAILED
        elif f.kind == "sent":  # a DM: on air, waiting for the recipient
            m.status, m.ack = WAITING, f.ack
            m.deadline = time.time() + f.timeout_ms / 1000 + 5
        else:
            m.status = SENT  # all a channel message ever gets
        self._changed(m)

    def tick(self):
        now = time.time()
        for m in self.messages:
            if m.status == WAITING and now > m.deadline:
                m.status = NO_ACK
                self._changed(m)
