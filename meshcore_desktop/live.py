"""The Repeater tab's Live page. A repeater can't stream its traffic over the mesh
(its packet log only reads out over USB), so this polls its counters and turns
them into rates, and lists the packets our own node heard it relay."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from . import protocol as P
from .dashboard import packet_line
from .widgets import LineChart

log = logging.getLogger("live")

INTERVALS = [("15 s", 15), ("30 s", 30), ("1 min", 60), ("2 min", 120), ("5 min", 300)]
DEFAULT_INTERVAL = 1
NEIGHBOUR_EVERY = 4  # polls: the list costs a second round trip
MAX_MISSES = 3  # in a row, then it stops
MAX_RUN_S = 3600  # nobody should flood the mesh with polls by forgetting a window
RECORD_EVERY_S = 600  # how often a poll also goes in the saved History
IDLE = "Asks the repeater for its counters over the mesh, which uses a little airtime. Keeps going on other screens until you press Stop, for up to an hour."


def rates(prev: dict, cur: dict) -> dict[str, float] | None:
    """Per-minute rates and airtime between two status replies, timed by the
    repeater's own uptime. None if it rebooted or its counters were cleared."""
    up = cur["up_secs"] - prev["up_secs"]
    if up <= 0 or cur["recv"] < prev["recv"] or cur["sent"] < prev["sent"]:
        return None

    def per_min(*keys: str) -> float:
        return max(0, sum(cur[k] - prev[k] for k in keys)) * 60 / up

    return {"rx": per_min("recv"), "tx": per_min("sent"), "flood": per_min("recv_flood"), "direct": per_min("recv_direct"),
            "dups": per_min("flood_dups", "direct_dups"), "errors": per_min("errors", "recv_errors"),
            "air_tx": 100 * max(0, cur["air_secs"] - prev["air_secs"]) / up,
            "air_rx": 100 * max(0, cur["rx_air_secs"] - prev["rx_air_secs"]) / up}


def parse_neighbors(text: str) -> list[tuple[str, int, float]]:
    """The `neighbors` reply: lines of hex-id:seconds-ago:SNR-times-4."""
    out = []
    for line in text.removeprefix("> ").splitlines():
        parts = line.strip().split(":")
        try:
            out.append((parts[0].lower(), int(parts[1]), int(parts[2]) / 4))
        except (IndexError, ValueError):
            continue  # "-none-", or a line cut off at the end of the reply
    return out


def relayed_by(p: P.RawPacket, key: bytes) -> bool:
    """Whether this repeater's hash is on the packet's path. Hashes are short,
    so another node sharing it counts too."""
    return p.flood and any(key.startswith(h) for h in p.path.hops())


def _ago(secs: int) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= n:
            return f"{secs // n} {unit} ago"
    return f"{secs} s ago"


class LivePage(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.contact: P.Contact | None = None
        self.run = 0  # bumped by each start and stop, which ends the poll loop of an earlier run
        self.running = False
        self.last: dict | None = None
        self.recorded = 0.0
        self.series: dict[str, deque] = {k: deque(maxlen=240) for k in ("rx", "tx", "air", "noise")}

        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.toggle_btn = QPushButton("Start", objectName="primary")
        self.toggle_btn.clicked.connect(self.toggle)
        self.interval = QComboBox()
        for name, _s in INTERVALS:
            self.interval.addItem(name)
        self.interval.setCurrentIndex(DEFAULT_INTERVAL)
        self.interval.setToolTip("How often to ask. Shorter uses more airtime, and every poll is a request and reply over LoRa.")
        self.state = QLabel(IDLE, objectName="dim")
        self.state.setWordWrap(True)
        row.addWidget(self.toggle_btn)
        row.addWidget(QLabel("Every", objectName="dim"))
        row.addWidget(self.interval)
        row.addWidget(self.state, 1)
        v.addLayout(row)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        body = QHBoxLayout()
        grid = QGridLayout()
        wait = "Needs two polls to draw a line."
        self.charts = {"rx": LineChart("Received", "packets/min", lambda x: f"{x:.1f}", wait),
                       "tx": LineChart("Sent", "packets/min", lambda x: f"{x:.1f}", wait),
                       "air": LineChart("TX airtime", "%", lambda x: f"{x:.1f}", wait),
                       "noise": LineChart("Noise floor", "dBm", lambda x: f"{x:.0f}", wait)}
        for i, chart in enumerate(self.charts.values()):
            grid.addWidget(chart, i // 2, i % 2)
        body.addLayout(grid, 3)

        side = QVBoxLayout()
        fixed = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        side.addWidget(QLabel("Heard directly by the repeater", objectName="dim"))
        self.neighbours = QPlainTextEdit(readOnly=True, font=fixed)
        self.neighbours.setMaximumHeight(120)
        self.neighbours.setLineWrapMode(QPlainTextEdit.NoWrap)
        side.addWidget(self.neighbours)
        self.feed_title = QLabel("", objectName="dim")
        self.feed_title.setToolTip("Only packets your own node heard. Paths use short hashes, so another node sharing this "
                                   "repeater's hash shows here too.")
        side.addWidget(self.feed_title)
        self.feed = QPlainTextEdit(readOnly=True, font=fixed)
        self.feed.setMaximumBlockCount(500)
        self.feed.setLineWrapMode(QPlainTextEdit.NoWrap)
        side.addWidget(self.feed, 1)
        body.addLayout(side, 2)
        v.addLayout(body, 1)
        self._reset()
        self.set_usable(False)

    def _reset(self):
        self.last = None
        self.count = 0
        for d in self.series.values():
            d.clear()
        for chart in self.charts.values():
            chart.set_points([])
        self.summary.clear()
        self.neighbours.clear()
        self.feed.clear()
        self._feed_label()

    def _feed_label(self):
        self.feed_title.setText(f"Packets you heard it relay: {self.count}")

    def set_usable(self, ok: bool):
        self.toggle_btn.setEnabled(ok or self.running)

    def show_contact(self, c: P.Contact):
        if self.contact is None or self.contact.prefix != c.prefix:
            self.stop("Stopped: you opened a different repeater.")
            self._reset()
        self.contact = c

    def toggle(self):
        if self.running:
            self.stop("Stopped.")
        else:
            self.start()

    def start(self):
        c = self.contact
        if c is None or self.running:
            return
        self.feed.clear()
        self.count = 0
        for at, p in self.panel.win.model.packets:
            self._add(at, p)
        self._feed_label()
        self.last = None  # a gap since the last run would read as one long average
        self.recorded = 0.0
        self.run += 1
        self.running = True
        self.toggle_btn.setText("Stop")
        self.state.setText("Asking the repeater…")
        self.panel.live_changed(True)
        asyncio.ensure_future(self._loop(c, self.run))

    def stop(self, why: str):
        if self.running:
            self.running = False
            self.run += 1
            self.toggle_btn.setText("Start")
            self.state.setText(why)
            self.panel.live_changed(False)
            if not self.isVisible():  # nobody is looking at the state line
                self.panel.win.toast(f"Live: {why}")

    def add_packet(self, p: P.RawPacket):
        if self.running:
            self._add(time.time(), p)
            self._feed_label()

    def _add(self, at: float, p: P.RawPacket):
        if self.contact and relayed_by(p, self.contact.key):
            self.count += 1
            self.feed.appendPlainText(packet_line(at, p))

    async def _loop(self, c: P.Contact, run: int):
        started, polls, misses = time.monotonic(), 0, 0
        try:
            while run == self.run:
                session = self.panel.win.session
                f = await session.repeater_status(c.key)
                if run != self.run:
                    return
                if f is None or f.kind == "err":
                    misses += 1
                    if misses >= MAX_MISSES:
                        self.stop(f"Stopped: no reply {MAX_MISSES} times in a row. Try Reset route, then Start.")
                        return
                    self.state.setText(f"No reply ({misses} of {MAX_MISSES}). Will keep trying.")
                else:
                    misses = 0
                    self._sample(c, f)
                    if polls % NEIGHBOUR_EVERY == 0:
                        n = await session.cli(c.key, "neighbors")
                        if run != self.run:
                            return
                        if n is not None and n.kind == "message":
                            self._show_neighbours(n.msg.text)
                polls += 1
                if time.monotonic() - started > MAX_RUN_S:
                    self.stop("Stopped after an hour, to keep the channel clear. Press Start to carry on.")
                    return
                await asyncio.sleep(INTERVALS[self.interval.currentIndex()][1])
        except Exception as e:
            log.exception("live poll")
            if run == self.run:
                self.stop(f"Stopped: {e}")

    def _sample(self, c: P.Contact, f: P.Frame):
        now, s = time.time(), f.stats
        record = now - self.recorded >= RECORD_EVERY_S
        if record:
            self.recorded = now
        self.panel.apply_status(c, f, record)
        self.series["noise"].append((now, s["noise_floor"]))
        r = rates(self.last, s) if self.last else None
        if self.last and r is None:
            self.state.setText("Its counters went back to zero: it rebooted, or they were cleared.")
        else:
            self.state.setText(time.strftime("Updated %H:%M:%S", time.localtime(now)))
        self.last = s
        if r:
            self.series["rx"].append((now, r["rx"]))
            self.series["tx"].append((now, r["tx"]))
            self.series["air"].append((now, r["air_tx"]))
            self.summary.setText(f"RX {r['rx']:.1f}/min · TX {r['tx']:.1f}/min · Flood {r['flood']:.1f} · Direct {r['direct']:.1f} · "
                                 f"Duplicates {r['dups']:.1f} · Errors {r['errors']:.1f} · "
                                 f"Airtime TX {r['air_tx']:.1f}% RX {r['air_rx']:.1f}% · Queue {s['tx_queue']}")
        else:
            self.summary.setText("Rates appear after the next poll.")
        for key, chart in self.charts.items():
            chart.set_points(list(self.series[key]))

    def _show_neighbours(self, text: str):
        model = self.panel.win.model
        rows = []
        for h, secs, snr in parse_neighbors(text):
            names = model.hop_names(h)
            rows.append(f"{(names[0] if names else h)[:18]:<18} {snr:>5.1f} dB  {_ago(secs)}")
        self.neighbours.setPlainText("\n".join(rows) or "None heard yet.")
