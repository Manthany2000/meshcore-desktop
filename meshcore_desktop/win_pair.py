"""Pairing on Windows through WinRT custom pairing, so the PIN prompt is ours.
bleak's own pair() only does "just works", and the node wants its PIN typed in.
The winrt packages come with bleak on Windows."""
import asyncio
import logging

from winrt.windows.devices.bluetooth import BluetoothLEDevice
from winrt.windows.devices.enumeration import (DeviceInformation, DevicePairingKinds, DevicePairingProtectionLevel,
                                               DevicePairingResultStatus)

log = logging.getLogger("pair")

S = DevicePairingResultStatus
NICE = {S.AUTHENTICATION_FAILURE: "Wrong PIN, or the node refused it",
        S.AUTHENTICATION_TIMEOUT: "Timed out waiting for the PIN",
        S.PAIRING_CANCELED: "Pairing cancelled",
        S.REJECTED_BY_HANDLER: "Pairing cancelled",
        S.CONNECTION_REJECTED: "The node refused the connection",
        S.NO_SUPPORTED_PROFILES: "The node doesn't offer pairing",
        S.HARDWARE_FAILURE: "Bluetooth adapter error",
        S.NOT_READY_TO_PAIR: "Couldn't reach the node",
        S.TOO_MANY_CONNECTIONS: "Too many Bluetooth connections"}


class PairingError(Exception):
    def __init__(self, name: str, text: str):
        self.name = name
        super().__init__(text)


def device_path(address: str) -> str:
    return address


async def _info(address: str) -> DeviceInformation:
    dev = await BluetoothLEDevice.from_bluetooth_address_async(int(address.replace(":", ""), 16))
    if dev is None:
        raise PairingError("NotFound", "Windows hasn't seen the node yet: scan first")
    try:
        # A fresh object: the one on the device doesn't update its pairing state.
        return await DeviceInformation.create_from_id_async(dev.device_id)
    finally:
        dev.close()


async def is_paired(address: str) -> bool:
    try:
        return (await _info(address)).pairing.is_paired
    except Exception as e:
        log.debug("is_paired: %s", e)
        return False


async def pair(address: str, ask_pin):
    info = await _info(address)
    if info.pairing.is_paired:
        return
    loop = asyncio.get_running_loop()
    custom = info.pairing.custom

    def requested(_sender, args):
        # Called on a WinRT thread; the deferral holds Windows until we answer.
        if args.pairing_kind != DevicePairingKinds.PROVIDE_PIN:
            args.accept()
            return
        deferral = args.get_deferral()

        async def ask():
            try:
                pin = await ask_pin(address)
                if pin is not None:
                    args.accept_with_pin(f"{pin:06d}")
            except Exception:
                log.exception("PIN prompt")
            finally:
                deferral.complete()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ask()))

    token = custom.add_pairing_requested(requested)
    kinds = DevicePairingKinds.PROVIDE_PIN | DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.CONFIRM_PIN_MATCH
    try:
        for level in (DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION,
                      DevicePairingProtectionLevel.ENCRYPTION):
            result = await custom.pair_with_protection_level_async(kinds, level)
            if result.status != S.PROTECTION_LEVEL_COULD_NOT_BE_MET:
                break
    finally:
        custom.remove_pairing_requested(token)
    if result.status not in (S.PAIRED, S.ALREADY_PAIRED):
        raise PairingError(result.status.name, NICE.get(result.status, f"Pairing failed: {result.status.name}"))


async def forget(address: str):
    """Removes the bond, e.g. after the node was reflashed and lost its half."""
    info = await _info(address)
    if info.pairing.is_paired:
        await info.pairing.unpair_async()
