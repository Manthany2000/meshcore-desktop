"""A small SDR-style waterfall of the packets the node hears, for the toolbar.

Across is SNR (weak on the left, strong on the right); down is time, newest at
the top. Each packet leaves a hot spot that cools and scrolls away. It isn't a
radio spectrum (the node only reports SNR per packet), but it reads like one:
a quiet mesh is dark blue, a busy one lights up.
"""
from __future__ import annotations

import math
import random
import time
from collections import deque

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import protocol as P
from . import theme

SNR_LO, SNR_HI = -20.0, 15.0
ROW_S = 0.5  # seconds per pixel row

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


class Waterfall(QWidget):
    clicked = Signal()

    def __init__(self, width: int = 200, height: int = 26):
        super().__init__()
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)
        self.img = QImage(width - 2, height - 2, QImage.Format_RGB32)
        self.img.fill(LUT[0])
        self.energy = [0.0] * self.img.width()  # what the next row will show
        self.recent: deque = deque()  # (time, snr) for the tooltip
        self.live = False
        self.timer = QTimer(self, interval=int(ROW_S * 1000), timeout=self._row)
        self.timer.start()
        self._tip()

    def set_live(self, on: bool):
        """Connected: a faint noise floor moves. Not connected: it goes dark and still."""
        self.live = on
        if not on:
            self.img.fill(LUT[0])
            self.energy = [0.0] * len(self.energy)
            self.update()

    def add_packet(self, p: P.RawPacket):
        now = time.time()
        self.recent.append((now, p.snr))
        w = len(self.energy)
        x = (min(max(p.snr, SNR_LO), SNR_HI) - SNR_LO) / (SNR_HI - SNR_LO) * (w - 1)
        # Stronger packets are hotter; floods spread a little wider (several copies, several SNRs).
        peak = 0.55 + 0.45 * (x / (w - 1))
        spread = 3.5 if p.flood else 2.5
        for i in range(max(0, int(x - 4 * spread)), min(w, int(x + 4 * spread) + 1)):
            self.energy[i] = min(1.0, self.energy[i] + peak * math.exp(-((i - x) / spread) ** 2))
        self._tip()

    def _row(self):
        if not self.live:
            return
        w, h = self.img.width(), self.img.height()
        # Scroll everything down a pixel, then paint the new top row.
        bpl = self.img.bytesPerLine()
        buf = self.img.bits()
        buf[bpl:bpl * h] = bytes(buf[0:bpl * (h - 1)])
        for x in range(w):
            noise = random.random() * 0.13 + (0.06 if random.random() < 0.02 else 0)
            v = max(noise, self.energy[x])
            self.img.setPixel(x, 0, LUT[min(255, int(v * 255))])
            self.energy[x] *= 0.72  # a packet cools over a few rows, a short streak like a real burst
        self.update()
        if self.recent and time.time() - self.recent[0][0] > 60:
            self._tip()

    def _tip(self):
        now = time.time()
        while self.recent and now - self.recent[0][0] > 60:
            self.recent.popleft()
        n = len(self.recent)
        rows = self.img.height()
        head = f"Packets heard, newest at the top (last {rows * ROW_S:.0f} s).\n" \
               f"Across: SNR, {SNR_LO:.0f} dB on the left to +{SNR_HI:.0f} dB on the right.\n"
        if n:
            best = max(s for _, s in self.recent)
            avg = sum(s for _, s in self.recent) / n
            head += f"Last minute: {n} packet{'s' * (n != 1)}, average SNR {avg:.1f} dB, best {best:.1f} dB."
        else:
            head += "Nothing heard in the last minute."
        self.setToolTip(head + "\nClick for the Dashboard.")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _e):
        p = QPainter(self)
        r = self.rect()
        p.drawImage(1, 1, self.img)
        # A 0 dB tick at the top and bottom edges, so "left of the tick" reads as weak.
        x0 = 1 + (0 - SNR_LO) / (SNR_HI - SNR_LO) * (self.img.width() - 1)
        p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        p.drawLine(int(x0), 1, int(x0), 3)
        p.drawLine(int(x0), r.height() - 4, int(x0), r.height() - 2)
        p.setPen(QColor(theme.current.border))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5))
        p.end()
