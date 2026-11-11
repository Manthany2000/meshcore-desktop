"""An SDR-style waterfall of the packets the node hears.

Across is SNR (weak on the left, strong on the right); down is time, newest at
the top. Each packet leaves a hot spot that cools and scrolls away. It isn't a
radio spectrum (the node only reports SNR per packet), but it reads like one:
a quiet mesh is dark blue, a busy one lights up.

Waterfall is the small strip in the toolbar. Double-click it for WaterfallWindow:
the same picture, bigger, with axes, a longer history and per-packet details.
Both draw from one shared packet history, so the big one opens already filled in.
"""
from __future__ import annotations

import math
import random
import time
from array import array
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QToolTip, QVBoxLayout, QWidget

from . import protocol as P
from . import theme
from .dashboard import PACKET_NAMES

SNR_LO, SNR_HI = -20.0, 15.0
ROW_S = 0.5  # seconds per pixel row
DECAY = 0.72  # a packet cools over a few rows: a short streak, like a real burst
STREAK = 12  # rows after which what's left of a packet is invisible
KEEP_S = 15 * 60  # packet history kept for the big window

# Classic SDR heat ramp: deep blue, blue, cyan, yellow, red, white.
RAMP = [(0.00, (6, 10, 32)), (0.18, (12, 32, 110)), (0.40, (20, 140, 210)),
        (0.62, (235, 225, 60)), (0.82, (235, 70, 30)), (1.00, (255, 245, 235))]


def _lut() -> list[int]:
    out = []
    for i in range(256):
        v = i / 255
        for (a, ca), (b, cb) in zip(RAMP, RAMP[1:]):
            if v <= b:
                t = (v - a) / (b - a)
                r, g, bl = (int(ca[k] + (cb[k] - ca[k]) * t) for k in range(3))
                out.append(0xFF000000 | r << 16 | g << 8 | bl)
                break
    return out


LUT = _lut()


@dataclass
class Heard:
    at: float
    p: P.RawPacket


class History:
    """Every packet heard in the last KEEP_S seconds, shared by both views."""

    def __init__(self):
        self.items: deque[Heard] = deque()

    def add(self, p: P.RawPacket) -> Heard:
        h = Heard(time.time(), p)
        self.items.append(h)
        while self.items and h.at - self.items[0].at > KEEP_S:
            self.items.popleft()
        return h

    def since(self, seconds: float) -> list[Heard]:
        cut = time.time() - seconds
        return [h for h in self.items if h.at >= cut]


def snr_x(snr: float, w: int) -> float:
    return (min(max(snr, SNR_LO), SNR_HI) - SNR_LO) / (SNR_HI - SNR_LO) * (w - 1)


def _deposit(energy: list[float], p: P.RawPacket, scale: float = 1.0):
    """Adds one packet's hot spot to a row. Stronger packets are hotter; floods
    spread a little wider (several copies, several SNRs)."""
    w = len(energy)
    x = snr_x(p.snr, w)
    peak = 0.55 + 0.45 * (x / max(1, w - 1))
    spread = (3.5 if p.flood else 2.5) * max(1.0, w / 200)  # the same blob reads the same size in a wider plot
    for i in range(max(0, int(x - 4 * spread)), min(w, int(x + 4 * spread) + 1)):
        energy[i] = min(1.0, energy[i] + scale * peak * math.exp(-((i - x) / spread) ** 2))


class Plot:
    """The scrolling picture itself: a QImage, one pixel row per ROW_S."""

    def __init__(self, w: int, h: int):
        self.img = QImage(max(1, w), max(1, h), QImage.Format_RGB32)
        w = self.img.width()
        # Some ready-made rows of background noise; copying one is much faster than drawing it.
        self.noise = [array("I", (LUT[int((random.random() * 0.13 + (0.06 if random.random() < 0.02 else 0)) * 255)]
                                  for _ in range(w))) for _ in range(24)]
        self.energy = [0.0] * w
        self.img.fill(LUT[0])

    def _write_row(self, y: int, energy: list[float] | None):
        row = array("I", random.choice(self.noise))
        if energy:
            for x, v in enumerate(energy):
                if v > 0.13:
                    row[x] = LUT[min(255, int(v * 255))]
        bpl = self.img.bytesPerLine()
        self.img.bits()[y * bpl:y * bpl + len(row) * 4] = row.tobytes()

    def blank(self):
        self.img.fill(LUT[0])
        self.energy = [0.0] * len(self.energy)

    def tick(self):
        """Scroll down a row and paint the newest one at the top."""
        h, bpl = self.img.height(), self.img.bytesPerLine()
        buf = self.img.bits()
        if h > 1:
            buf[bpl:bpl * h] = bytes(buf[0:bpl * (h - 1)])
        self._write_row(0, self.energy)
        self.energy = [v * DECAY for v in self.energy]

    def add(self, p: P.RawPacket):
        _deposit(self.energy, p)

    def backfill(self, history: History):
        """Draws what the history says the picture looks like now, for a new or resized plot."""
        w, h = self.img.width(), self.img.height()
        now = time.time()
        rows: dict[int, list[float]] = {}
        for item in history.since((h + STREAK) * ROW_S):
            age = int((now - item.at) / ROW_S)
            for k in range(STREAK):
                y = age - k  # rows above a packet's own row are the later, cooler ones
                if 0 <= y < h:
                    _deposit(rows.setdefault(y, [0.0] * w), item.p, DECAY ** k)
        for y in range(h):
            self._write_row(y, rows.get(y))
        # What's still cooling carries on in the next live rows.
        self.energy = [0.0] * w
        for item in history.since(STREAK * ROW_S):
            _deposit(self.energy, item.p, DECAY ** (int((now - item.at) / ROW_S) + 1))


def summary(history: History) -> str:
    last = history.since(60)
    if not last:
        return "Nothing heard in the last minute."
    snrs = [h.p.snr for h in last]
    n = len(last)
    return (f"Last minute: {n} packet{'s' * (n != 1)}, average SNR {sum(snrs) / n:.1f} dB, "
            f"best {max(snrs):.1f} dB.")


def describe(h: Heard) -> str:
    p = h.p
    kind = PACKET_NAMES.get(p.payload_type, f"Type {p.payload_type}")
    route = f"flood, {p.hops} hop{'s' * (p.hops != 1)}" if p.flood else "direct"
    lines = [f"{time.strftime('%H:%M:%S', time.localtime(h.at))}  {kind}",
             f"SNR {p.snr:.1f} dB · RSSI {p.rssi} dBm", route]
    if p.flood and p.hops:
        lines.append("Path: " + " → ".join(x.hex() for x in p.path.hops()))
    return "\n".join(lines)


class Waterfall(QWidget):
    """The small strip in the toolbar. Click: Dashboard. Double-click: the big window."""
    clicked = Signal()
    expand = Signal()

    def __init__(self, width: int = 200, height: int = 26):
        super().__init__()
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)
        self.history = History()
        self.plot = Plot(width - 2, height - 2)
        self.live = False
        self.big: WaterfallWindow | None = None
        self.timer = QTimer(self, interval=int(ROW_S * 1000), timeout=self._row)
        self.timer.start()
        # A single click waits out the double-click time, so a double-click doesn't also open the Dashboard.
        self.click_timer = QTimer(self, singleShot=True, timeout=self.clicked.emit)
        self._tip()

    def set_live(self, on: bool):
        """Connected: a faint noise floor moves. Not connected: it goes dark and still."""
        self.live = on
        if not on:
            self.plot.blank()
            self.update()
        if self.big:
            self.big.set_live(on)

    def add_packet(self, p: P.RawPacket):
        self.history.add(p)
        self.plot.add(p)
        if self.big:
            self.big.add_packet(p)
        self._tip()

    def open_big(self):
        if self.big is None:
            self.big = WaterfallWindow(self.window(), self.history)
            self.big.set_live(self.live)
        self.big.show()
        self.big.raise_()
        self.big.activateWindow()

    def _row(self):
        if not self.live:
            return
        self.plot.tick()
        self.update()
        if self.history.items and time.time() - self.history.items[-1].at > 60:
            self._tip()

    def _tip(self):
        rows = self.plot.img.height()
        self.setToolTip(f"Packets heard, newest at the top (last {rows * ROW_S:.0f} s).\n"
                        f"Across: SNR, {SNR_LO:.0f} dB on the left to +{SNR_HI:.0f} dB on the right.\n"
                        + summary(self.history) + "\nClick for the Dashboard, double-click to expand.")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.click_timer.start(QApplication.styleHints().mouseDoubleClickInterval())

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.click_timer.stop()
            self.expand.emit()

    def paintEvent(self, _e):
        p = QPainter(self)
        r = self.rect()
        p.drawImage(1, 1, self.plot.img)
        # A 0 dB tick at the top and bottom edges, so "left of the tick" reads as weak.
        x0 = 1 + snr_x(0, self.plot.img.width())
        p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        p.drawLine(int(x0), 1, int(x0), 3)
        p.drawLine(int(x0), r.height() - 4, int(x0), r.height() - 2)
        p.setPen(QColor(theme.current.border))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5))
        p.end()


class BigPlot(QWidget):
    """The plot in the big window: the picture plus SNR and time axes, and hover details."""
    LEFT, BOTTOM, TOP, RIGHT = 52, 26, 8, 12

    def __init__(self, history: History):
        super().__init__()
        self.history = history
        self.plot = Plot(1, 1)
        self.live = False
        self.setMouseTracking(True)
        self.setMinimumSize(360, 220)

    def area(self) -> QRect:
        return self.rect().adjusted(self.LEFT, self.TOP, -self.RIGHT, -self.BOTTOM)

    def resizeEvent(self, _e):
        a = self.area()
        self.plot = Plot(a.width(), a.height())
        if self.live:
            self.plot.backfill(self.history)

    def set_live(self, on: bool):
        self.live = on
        if on:
            self.plot.backfill(self.history)
        else:
            self.plot.blank()
        self.update()

    def tick(self):
        if self.live:
            self.plot.tick()
            self.update()

    def add(self, p: P.RawPacket):
        self.plot.add(p)

    def mouseMoveEvent(self, e):
        a = self.area()
        pos = e.position().toPoint()
        if not a.contains(pos):
            QToolTip.hideText()
            return
        w, now = self.plot.img.width(), time.time()
        best, near = None, 10.0  # pixels
        for h in self.history.since(a.height() * ROW_S):
            d = math.hypot(a.left() + snr_x(h.p.snr, w) - pos.x(), a.top() + (now - h.at) / ROW_S - pos.y())
            if d < near:
                best, near = h, d
        if best:
            QToolTip.showText(e.globalPosition().toPoint(), describe(best), self)
        else:
            snr = SNR_LO + (pos.x() - a.left()) / max(1, w - 1) * (SNR_HI - SNR_LO)
            ago = (pos.y() - a.top()) * ROW_S
            QToolTip.showText(e.globalPosition().toPoint(), f"{snr:+.1f} dB, {ago:.0f} s ago", self)

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        a = self.area()
        p.drawImage(a.topLeft(), self.plot.img)
        f = QFont(theme.MONO.strip("'"))
        f.setPixelSize(11)
        p.setFont(f)
        dim = QColor(t.dim)
        grid = QPen(QColor(255, 255, 255, 38), 1, Qt.DashLine)
        # SNR along the bottom, with faint lines up through the picture.
        for snr in range(int(SNR_LO), int(SNR_HI) + 1, 5):
            x = int(a.left() + snr_x(snr, a.width()))
            p.setPen(grid if snr else QPen(QColor(255, 255, 255, 80), 1))
            p.drawLine(x, a.top(), x, a.bottom())
            p.setPen(dim)
            p.drawLine(x, a.bottom() + 1, x, a.bottom() + 4)
            p.drawText(QRect(x - 30, a.bottom() + 5, 60, 16), Qt.AlignHCenter | Qt.AlignTop, f"{snr:+d}" if snr else "0 dB")
        # Time down the left side.
        span = a.height() * ROW_S
        step = next((s for s in (10, 15, 30, 60, 120, 300) if span / s <= 8), 600)
        for sec in range(0, int(span) + 1, step):
            y = int(a.top() + sec / ROW_S)
            if sec:
                p.setPen(grid)
                p.drawLine(a.left(), y, a.right(), y)
            p.setPen(dim)
            label = "now" if not sec else f"-{sec}s" if sec < 60 else f"-{sec // 60}m" + (f"{sec % 60:02d}" if sec % 60 else "")
            p.drawText(QRect(0, y - 8, self.LEFT - 6, 16), Qt.AlignRight | Qt.AlignVCenter, label)
        p.setPen(QColor(t.border))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(a).adjusted(-0.5, -0.5, 0.5, 0.5))
        p.end()


class WaterfallWindow(QDialog):
    """The waterfall, big: a window that stays open beside the app."""

    def __init__(self, parent, history: History):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Packet waterfall")
        self.resize(820, 520)
        self.history = history
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        self.stats = QLabel()
        head.addWidget(self.stats, 1)
        head.addWidget(QLabel("Across: SNR, weak to strong · Down: time, newest at the top · Hover a spot for its packet",
                              objectName="dim"))
        v.addLayout(head)
        self.plot = BigPlot(history)
        v.addWidget(self.plot, 1)
        self.timer = QTimer(self, interval=int(ROW_S * 1000), timeout=self._row)
        self.timer.start()
        self._stats()

    def set_live(self, on: bool):
        self.plot.set_live(on)

    def add_packet(self, p: P.RawPacket):
        self.plot.add(p)
        self._stats()

    def _row(self):
        self.plot.tick()
        if int(time.time() * 2) % 10 == 0:
            self._stats()

    def _stats(self):
        self.stats.setText(summary(self.history))
        self.stats.setStyleSheet(f"font-family: {theme.MONO};")
