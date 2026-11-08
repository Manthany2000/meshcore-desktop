"""Pairing through BlueZ with our own agent, so the PIN prompt is ours rather
than whatever the desktop's Bluetooth applet does. BlueZ asks the agent that
belongs to the D-Bus connection which called Pair, so both happen here."""
# No `from __future__ import annotations`: dbus_fast reads the D-Bus signatures
# from the annotations.
import logging

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger("pair")

BLUEZ = "org.bluez"
AGENT_PATH = "/org/meshcore_desktop/agent"


def device_path(address: str, adapter: str = "hci0") -> str:
    return f"/org/bluez/{adapter}/dev_{address.upper().replace(':', '_')}"


class Agent(ServiceInterface):
    def __init__(self, ask_pin):
        super().__init__("org.bluez.Agent1")
        self.ask_pin = ask_pin  # async fn(device_path) -> int | None

    @method()
    def Release(self):
        pass

    @method()
    async def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        pin = await self.ask_pin(device)
        if pin is None:
            raise DBusError("org.bluez.Error.Rejected", "cancelled")
        return pin

    @method()
    async def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        pin = await self.ask_pin(device)
        if pin is None:
            raise DBusError("org.bluez.Error.Rejected", "cancelled")
        return str(pin)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        pass

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        pass  # numeric comparison: accept

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        pass

    @method()
    def Cancel(self):
        log.info("pairing cancelled by BlueZ")


async def _call(bus, path, iface, member, sig="", body=()):
    reply = await bus.call(Message(destination=BLUEZ, path=path, interface=iface, member=member,
                                   signature=sig, body=list(body)))
    if reply.message_type == MessageType.ERROR:
        raise PairingError(reply.error_name, reply.body[0] if reply.body else "")
    return reply.body


class PairingError(Exception):
    def __init__(self, name: str, text: str):
        self.name = name
        nice = {"org.bluez.Error.AuthenticationFailed": "Wrong PIN, or the node refused it",
                "org.bluez.Error.AuthenticationCanceled": "Pairing cancelled",
                "org.bluez.Error.AuthenticationRejected": "Pairing cancelled",
                "org.bluez.Error.ConnectionAttemptFailed": "Couldn't reach the node",
                "org.bluez.Error.AuthenticationTimeout": "Timed out waiting for the PIN",
                "org.freedesktop.DBus.Error.UnknownObject": "Bluetooth hasn't seen the node yet: scan first"}.get(name)
        super().__init__(nice or f"{name.rsplit('.', 1)[-1]}: {text}")


async def is_paired(path: str) -> bool:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        (v,) = await _call(bus, path, "org.freedesktop.DBus.Properties", "Get", "ss", ["org.bluez.Device1", "Paired"])
        return bool(v.value)
    except PairingError:
        return False
    finally:
        bus.disconnect()


async def pair(path: str, ask_pin):
    """Pairs and trusts the device, asking ask_pin for the PIN when the node
    wants one. The device must be known to BlueZ, i.e. seen in a scan."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = Agent(ask_pin)
    bus.export(AGENT_PATH, agent)
    try:
        await _call(bus, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", [AGENT_PATH, "KeyboardOnly"])
        try:
            await _call(bus, path, "org.freedesktop.DBus.Properties", "Set", "ssv",
                        ["org.bluez.Device1", "Trusted", Variant("b", True)])
            await _call(bus, path, "org.bluez.Device1", "Pair")
        except PairingError as e:
            if e.name != "org.bluez.Error.AlreadyExists":
                raise
        finally:
            await _call(bus, "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent", "o", [AGENT_PATH])
    finally:
        bus.disconnect()


async def forget(path: str):
    """Removes the bond, e.g. after the node was reflashed and lost its half."""
    adapter = path.rsplit("/", 1)[0]
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        await _call(bus, adapter, "org.bluez.Adapter1", "RemoveDevice", "o", [path])
    finally:
        bus.disconnect()
