"""Talking to the node: a BLE transport (or the simulator), and the session
that syncs and sends over it. The node answers one command at a time, with
pushes (codes 0x80 and up) mixed in at any moment."""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner

from . import pairing
from . import protocol as P
from .model import Model, Message

log = logging.getLogger("link")


async def scan(timeout: float = 6.0):
    """MeshCore nodes in range: [(address, name, rssi)], strongest first."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for dev, adv in found.values():
        name = adv.local_name or dev.name or ""
        uuids = [u.lower() for u in adv.service_uuids]
        if P.SERVICE_UUID in uuids or name.startswith("MeshCore"):
            out.append((dev.address, name or dev.address, adv.rssi))
    return sorted(out, key=lambda d: -d[2])


class BondError(ConnectionError):
    """We have a bond but the node wouldn't use it: it was probably reflashed
    or forgot us. Forgetting it here and pairing again fixes that."""


class BleTransport:
    def __init__(self, address: str, ask_pin=None):
        self.address = address
        self.ask_pin = ask_pin  # async fn(device_path) -> int | None
        self.client: BleakClient | None = None
        self.path = pairing.device_path(address)
        self.on_frame = lambda b: None
        self.on_disconnect = lambda: None

    async def connect(self):
        dev = await BleakScanner.find_device_by_address(self.address, timeout=10)
        if not dev:
            raise ConnectionError(f"{self.address} not found. Is it on, and not connected to a phone?")
        self.path = (dev.details or {}).get("path", self.path) if isinstance(dev.details, dict) else self.path
        # We pair ourselves, so the PIN prompt comes from this app.
        was_paired = await pairing.is_paired(self.path)
        if not was_paired:
            await pairing.pair(self.path, self.ask_pin)
        self.client = BleakClient(dev, disconnected_callback=lambda _c: self.on_disconnect(), timeout=30)
        try:
            await self.client.connect()
            await self.client.start_notify(P.TX_UUID, lambda _ch, data: self.on_frame(bytes(data)))
        except Exception as e:
            if was_paired:
                raise BondError(str(e) or e.__class__.__name__) from e
            raise

    async def forget(self):
        await pairing.forget(self.path)

    async def write(self, frame: bytes):
        await self.client.write_gatt_char(P.RX_UUID, frame, response=True)

    async def close(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()


class Session:
    """Drives a transport: sync on connect, then commands from the UI."""

    def __init__(self, model: Model, transport, on_state=lambda s, d="": None):
        self.model = model
        self.t = transport
        self.on_state = on_state
        self._waiter: tuple[set, asyncio.Future] | None = None
        self._admin_lock = asyncio.Lock()
        self._admin_waiter: tuple | None = None  # (fn(frame) -> bool, future)
        self.admin_min_wait = 6.0  # seconds; the node's estimate can be tight for a reply
        self._lock = asyncio.Lock()
        self._sync_wanted = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.connected = False
        self.max_channels = 8
        transport.on_frame = self._frame
        transport.on_disconnect = self._dropped

    # --- frames -------------------------------------------------------------

    def _frame(self, b: bytes):
        f = P.decode(b)
        if f is None:
            log.warning("bad frame: code %d, %d bytes", b[0] if b else -1, len(b))
            return
        if f.kind == "msg_waiting":
            self._sync_wanted.set()
        elif f.kind == "path_updated" and self.connected:
            # A flood reached them and the node learned a route: fetch it so the header shows it.
            self._tasks.append(asyncio.create_task(self.request(P.get_contact(f.key), {"contact"})))
        live = not self._syncing_backlog
        try:
            self.model.apply(f, live)
        except Exception:
            log.exception("applying %s", f.kind)
        if f.code < 0x80 and self._waiter:
            kinds, fut = self._waiter
            if (f.kind in kinds or f.kind == "err") and not fut.done():
                fut.set_result(f)
        self._admin_frame(f, live)

    def _admin_frame(self, f: P.Frame, live: bool):
        w = self._admin_waiter
        if w and not w[1].done() and w[0](f):
            w[1].set_result(f)
        elif live and f.kind == "message" and f.msg.txt_type == P.TXT_CLI and not f.msg.is_channel:
            self.model.emit("cli_late", f.msg)  # its command had already timed out

    _syncing_backlog = False

    async def request(self, frame: bytes, expect: set[str], timeout: float = 8.0) -> P.Frame | None:
        """Sends a command and waits for its answer: one of expect, or an error.
        None if nothing came."""
        async with self._lock:
            fut = asyncio.get_running_loop().create_future()
            self._waiter = (expect, fut)
            try:
                await self.t.write(frame)
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                log.warning("no answer to command %d", frame[0])
                return None
            finally:
                self._waiter = None

    # --- lifecycle ----------------------------------------------------------

    async def start(self):
        self.on_state("connecting", "")
        await self.t.connect()
        self.connected = True
        self.on_state("syncing", "node info")
        if not await self.request(P.app_start(), {"self_info"}):
            raise ConnectionError("node didn't answer")
        di = await self.request(P.device_query(), {"device_info"})
        if di and di.kind == "device_info":
            self.max_channels = di.max_channels or 8
        await self.request(P.simple(P.CMD_GET_DEVICE_TIME), {"time"})
        # Our clock is better than a node that lost power. It can only go forward.
        if self.model.node_now() < time.time() - 30:
            if await self.request(P.set_time(int(time.time())), {"ok"}):
                await self.request(P.simple(P.CMD_GET_DEVICE_TIME), {"time"})
        await self.request(P.simple(P.CMD_GET_BATT_AND_STORAGE), {"battery"})
        self.on_state("syncing", "channels")
        for i in range(self.max_channels):
            await self.request(P.get_channel(i), {"channel_info"})
        self.on_state("syncing", "contacts")
        await self.request(P.simple(P.CMD_GET_CONTACTS), {"contacts_end"}, timeout=60)
        self.on_state("syncing", "messages")
        await self._drain(backlog=True)
        self.on_state("ready", "")
        self._tasks = [asyncio.create_task(self._sync_loop()), asyncio.create_task(self._tick_loop())]

    async def _drain(self, backlog=False):
        self._syncing_backlog = backlog
        try:
            for _ in range(500):
                f = await self.request(P.simple(P.CMD_SYNC_NEXT_MESSAGE), {"message", "no_more"})
                if not f or f.kind != "message":
                    break
        finally:
            self._syncing_backlog = False

    async def _sync_loop(self):
        while self.connected:
            await self._sync_wanted.wait()
            self._sync_wanted.clear()
            await self._drain()

    async def _tick_loop(self):
        n = 0
        while self.connected:
            await asyncio.sleep(1)
            self.model.tick()
            n += 1
            if n % 60 == 0:
                await self.request(P.simple(P.CMD_GET_BATT_AND_STORAGE), {"battery"})

    def _dropped(self):
        if not self.connected:
            return
        self.connected = False
        for t in self._tasks:
            t.cancel()
        if self._waiter and not self._waiter[1].done():
            self._waiter[1].cancel()
        self.on_state("disconnected", "link dropped")

    async def stop(self):
        self.connected = False
        for t in self._tasks:
            t.cancel()
        await self.t.close()
        self.on_state("disconnected", "")

    # --- commands -----------------------------------------------------------

    async def send(self, conv: str, text: str) -> Message:
        m = self.model.add_outgoing(conv, text)
        await self._send(m)
        return m

    async def resend(self, m: Message):
        m.wire_time = self.model.node_now()
        m.status = "sending"
        m.repeats, m.repeaters = 0, {}
        m.at, m.live, m.payload_key = time.time(), True, b""
        self.model._changed(m)
        await self._send(m)

    async def _send(self, m: Message):
        if m.is_channel:
            frame = P.send_channel(m.channel, m.wire_time, m.text)
            f = await self.request(frame, {"ok"})
        else:
            c = self.model.contacts.get(m.dm_prefix)
            if c and self.model.always_floods(c.prefix):
                # The last reply will have taught the node a route; forget it so this one floods too.
                await self.reset_path(c.key)
            frame = P.send_direct(m.dm_prefix, m.wire_time, m.text)
            f = await self.request(frame, {"sent"})
        self.model.send_result(m, f)

    # --- repeater admin -----------------------------------------------------

    async def _admin(self, frame: bytes, done) -> P.Frame | None:
        """Sends a request to a remote node and waits for its answer. The node
        tracks one such request at a time, so they run one after another. Gives the
        answer, the node's "err" frame, or None if nothing came back in time."""
        async with self._admin_lock:
            # Set before sending, so a fast answer can't slip past.
            fut = asyncio.get_running_loop().create_future()
            self._admin_waiter = (done, fut)
            try:
                sent = await self.request(frame, {"sent"})
                if sent is None or sent.kind == "err":
                    return sent
                return await asyncio.wait_for(fut, max(self.admin_min_wait, sent.timeout_ms / 1000 + 3))
            except asyncio.TimeoutError:
                return None
            finally:
                self._admin_waiter = None

    async def login(self, key: bytes, password: str) -> P.Frame | None:
        prefix = key[:P.KEY_PREFIX]
        return await self._admin(P.login(key, password),
                                 lambda f: f.kind in ("login_ok", "login_fail") and f.prefix == prefix)

    async def repeater_status(self, key: bytes) -> P.Frame | None:
        prefix = key[:P.KEY_PREFIX]
        return await self._admin(P.status_req(key), lambda f: f.kind == "status" and f.prefix == prefix)

    async def cli(self, key: bytes, command: str) -> P.Frame | None:
        prefix = key[:P.KEY_PREFIX]
        return await self._admin(P.send_cli(prefix, self.model.node_now(), command),
                                 lambda f: f.kind == "message" and f.msg.txt_type == P.TXT_CLI
                                 and not f.msg.is_channel and f.msg.key == prefix)

    async def advert(self, flood: bool):
        return await self.request(P.send_advert(flood), {"ok"})

    async def reset_path(self, key: bytes):
        """Forgets the node's stored route to a contact, so the next message to
        them floods and the reply teaches the node a fresh one."""
        f = await self.request(P.reset_path(key), {"ok"})
        if f and f.kind == "ok":
            await self.request(P.get_contact(key), {"contact"})
        return f

    async def reboot(self):
        """The node restarts straight away and never answers. The link drops
        and the usual reconnect picks it up again."""
        async with self._lock:
            await self.t.write(P.reboot())

    async def set_channel(self, idx: int, name: str, secret: bytes):
        f = await self.request(P.set_channel(idx, name, secret), {"ok"})
        await self.request(P.get_channel(idx), {"channel_info"})
        return f
