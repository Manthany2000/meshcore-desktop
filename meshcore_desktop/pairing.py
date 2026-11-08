"""Bluetooth pairing with our own PIN prompt, per platform. Each backend has
device_path(address), is_paired(path), pair(path, ask_pin), forget(path) and
PairingError; "path" is whatever that backend names a device by."""
import sys

if sys.platform.startswith("linux"):
    from .bluez_pair import PairingError, device_path, forget, is_paired, pair  # noqa: F401
elif sys.platform == "win32":
    from .win_pair import PairingError, device_path, forget, is_paired, pair  # noqa: F401
else:
    # Elsewhere (macOS) the OS pairs on first use and asks for the PIN itself.
    class PairingError(Exception):
        name = ""

    def device_path(address: str) -> str:
        return address

    async def is_paired(path: str) -> bool:
        return True

    async def pair(path: str, ask_pin):
        pass

    async def forget(path: str):
        pass
