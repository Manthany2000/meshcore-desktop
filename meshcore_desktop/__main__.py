"""python -m meshcore_desktop [--sim | --address AA:BB:..] [--data DIR]"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

import qasync
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from . import theme
from .desktop import app_icon, settings
from .model import DATA_DIR, Model
from .ui import SIM, MainWindow

INSTANCE = "meshcore-desktop"


def already_running() -> bool:
    """Only one copy: the node takes one Bluetooth client at a time anyway.
    If one is open, ask it to come to the front and say so."""
    s = QLocalSocket()
    s.connectToServer(INSTANCE)
    if s.waitForConnected(300):
        s.write(b"show\n")
        s.waitForBytesWritten(300)
        s.disconnectFromServer()
        return True
    return False


def main():
    ap = argparse.ArgumentParser(prog="meshcore-desktop")
    ap.add_argument("--sim", action="store_true", help="connect to a simulated node")
    ap.add_argument("--address", help="connect to this node's BLE address at start")
    ap.add_argument("--data", type=Path, default=DATA_DIR, help="where messages are saved")
    ap.add_argument("-v", "--verbose", action="store_true")
    args, rest = ap.parse_known_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("bleak").setLevel(logging.INFO)

    if sys.platform == "win32":
        # Qt runs a Windows message loop on this thread, which is what bleak's
        # WinRT backend needs from an STA thread.
        from bleak.backends.winrt.util import allow_sta
        allow_sta()
    app = QApplication([sys.argv[0]] + rest)
    if already_running():
        print("MeshCore Desktop is already open; brought it to the front.")
        return
    app.setApplicationName("MeshCore Desktop")
    app.setDesktopFileName("meshcore-desktop")
    app.setWindowIcon(app_icon())
    theme.follow_system(app, settings().value("accent", "Signal green"))
    app.setQuitOnLastWindowClosed(False)  # the window's closeEvent disconnects, then quits
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = MainWindow(Model(args.data), SIM if args.sim else args.address)

    server = QLocalServer()
    QLocalServer.removeServer(INSTANCE)  # left behind if a previous copy crashed
    server.listen(INSTANCE)

    def raise_window():
        while server.hasPendingConnections():
            server.nextPendingConnection().deleteLater()
        win.showNormal()
        win.raise_()
        win.activateWindow()
    server.newConnection.connect(raise_window)

    win.show()
    quit_ = asyncio.Event()
    win.quit_event = quit_
    app.aboutToQuit.connect(quit_.set)

    async def run():
        await quit_.wait()
        # Stop whatever is still going (timers, the simulator) before the loop closes.
        me = asyncio.current_task()
        rest = [t for t in asyncio.all_tasks() if t is not me]
        for t in rest:
            t.cancel()
        await asyncio.gather(*rest, return_exceptions=True)

    with loop:
        loop.run_until_complete(run())
    server.close()


if __name__ == "__main__":
    main()
