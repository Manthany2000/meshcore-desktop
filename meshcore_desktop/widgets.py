"""Small painted pieces shared around the window: signal bars, battery, toasts,
the scan radar, the welcome animation, and the charts."""
from __future__ import annotations

import math
import random
import time
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QConicalGradient, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolTip,
                               QVBoxLayout, QWidget)

from . import theme


def signal_level(snr: float) -> int:
    """0 to 4 bars. LoRa still decodes well below 0 dB, so the scale reaches -15."""
    return sum(snr >= t for t in (-15, -8, -2, 5))


def _level_color(level: int) -> str:
    t = theme.current
    return t.ok if level >= 3 else t.warn if level == 2 else t.danger


def signal_html(snr: float) -> str:
    n = signal_level(snr)
    bars = "▂▄▆█"
    return (f'<span style="color:{_level_color(n)}">{bars[:n]}</span>'
            f'<span style="color:{theme.current.border}">{bars[n:]}</span> SNR {snr:.1f}')


def signal_icon(snr: float) -> QIcon:
    n = signal_level(snr)
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    for i in range(4):
        h = 8 + i * 6
        p.setBrush(QColor(_level_color(n) if i < n else theme.current.border))
        p.drawRoundedRect(QRectF(3 + i * 7, 28 - h, 5, h), 2, 2)
    p.end()
    return QIcon(pm)


def battery_color(pct: int) -> str:
    t = theme.current
    return t.ok if pct >= 50 else t.warn if pct >= 20 else t.danger


def battery_pixmap(pct: int) -> QPixmap:
    pm = QPixmap(60, 32)
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(2)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(theme.current.dim), 1.3))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(1, 2, 25, 12), 3, 3)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(theme.current.dim))
    p.drawRoundedRect(QRectF(27, 5.5, 2, 5), 1, 1)
    p.setBrush(QColor(battery_color(pct)))
    p.drawRoundedRect(QRectF(3, 4, max(2.0, 21 * pct / 100), 8), 1.5, 1.5)
    p.end()
    return pm


def fade_in(w: QWidget, ms: int = 170):
    """Not for a web view: Qt can't apply effects to one."""
    e = QGraphicsOpacityEffect(w)
    w.setGraphicsEffect(e)
    a = QPropertyAnimation(e, b"opacity", w)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve.OutCubic)
    a.finished.connect(lambda: w.setGraphicsEffect(None))
    a.start(QPropertyAnimation.DeleteWhenStopped)


class Toast(QLabel):
    """A short message that fades up at the bottom of the window."""

    def __init__(self, window: QWidget):
        super().__init__(window, objectName="toast")
        self.hide()
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity", self)
        self.anim.setDuration(180)
        self.anim.finished.connect(lambda: self.effect.opacity() < 0.05 and self.hide())
        self.timer = QTimer(self, singleShot=True, timeout=self._out)

    def show_text(self, text: str, bad: bool = False, ms: int = 3200):
        self.setStyleSheet(f"color:{theme.current.danger}" if bad else "")
        self.setText(text)
        self.adjustSize()
        w = self.parentWidget()
        self.move((w.width() - self.width()) // 2, w.height() - self.height() - 72)
        self.show()
        self.raise_()
        self._fade(1.0)
        self.timer.start(ms)

    def _out(self):
        self._fade(0.0)

    def _fade(self, to: float):
        self.anim.stop()
        self.anim.setStartValue(self.effect.opacity())
        self.anim.setEndValue(to)
        self.anim.start()


class Animated(QWidget):
    """Repaints about 30 times a second, only while it's on screen."""
    running = True

    def __init__(self):
        super().__init__()
        self.t0 = time.monotonic()
        self.ticker = QTimer(self, interval=33, timeout=self.update)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def showEvent(self, e):
        self.ticker.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.ticker.stop()
        super().hideEvent(e)


class Radar(Animated):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(120)
        self.blips: list[tuple[float, float]] = []

    def set_found(self, n: int):
        while len(self.blips) < min(n, 12):
            self.blips.append((random.uniform(0, 360), random.uniform(.3, .9)))

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = self.height() / 2 - 6
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(Qt.NoBrush)
        for k in (1, 2 / 3, 1 / 3):
            p.drawEllipse(c, r * k, r * k)
        p.drawLine(QPointF(c.x() - r, c.y()), QPointF(c.x() + r, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - r), QPointF(c.x(), c.y() + r))
        angle = (self.now() * 140) % 360
        if self.running:
            g = QConicalGradient(c, -angle)
            head, tail = QColor(t.accent), QColor(t.accent)
            head.setAlpha(170)
            tail.setAlpha(0)
            g.setColorAt(0, head)
            g.setColorAt(.28, tail)
            g.setColorAt(1, tail)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawEllipse(c, r, r)
        p.setPen(Qt.NoPen)
        for a, d in self.blips:
            age = ((angle - a) % 360) / 360 if self.running else .2
            col = QColor(t.accent2)
            col.setAlpha(int(255 * (1 - age * .7)))
            p.setBrush(col)
            rad = math.radians(a)
            p.drawEllipse(QPointF(c.x() + math.cos(rad) * r * d, c.y() - math.sin(rad) * r * d), 4, 4)


class MeshArt(Animated):
    """Drifting nodes with links between neighbours and pulses running along them."""

    def __init__(self, count: int = 26):
        super().__init__()
        rng = random.Random(7)  # the same picture each time
        self.nodes = [(rng.random(), rng.random(), rng.uniform(.15, .4), rng.uniform(0, 6.3)) for _ in range(count)]

    def _points(self) -> list[QPointF]:
        now = self.now()
        w, h = self.width(), self.height()
        return [QPointF((x + .035 * math.sin(now * s + ph)) * w, (y + .035 * math.cos(now * s * .8 + ph)) * h)
                for x, y, s, ph in self.nodes]

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pts = self._points()
        reach = max(self.width(), self.height()) * .24
        now = self.now()
        links = []
        for i, a in enumerate(pts):
            for j in range(i + 1, len(pts)):
                d = math.dist((a.x(), a.y()), (pts[j].x(), pts[j].y()))
                if d < reach:
                    links.append((a, pts[j], 1 - d / reach))
        for a, b, k in links:
            c = QColor(t.accent)
            c.setAlpha(int(90 * k))
            p.setPen(QPen(c, 1.2))
            p.drawLine(a, b)
        p.setPen(Qt.NoPen)
        for n, (a, b, _k) in enumerate(links):
            if n % 5 == 0:
                f = (now * .45 + n * .37) % 1
                c = QColor(t.accent2)
                c.setAlpha(int(230 * math.sin(f * math.pi)))
                p.setBrush(c)
                p.drawEllipse(a + (b - a) * f, 3, 3)
        for i, a in enumerate(pts):
            c = QColor(t.accent)
            c.setAlpha(200)
            p.setBrush(c)
            p.drawEllipse(a, 3.5 + 1.5 * math.sin(now * 1.6 + i), 3.5 + 1.5 * math.sin(now * 1.6 + i))


class Welcome(QWidget):
    """What the window shows before the first connection."""

    def __init__(self, on_connect: Callable, on_sim: Callable):
        super().__init__()
        self.art = MeshArt()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.art)
        over = QVBoxLayout(self.art)
        over.addStretch(1)
        self.mark = QLabel(alignment=Qt.AlignCenter)
        self.mark.setPixmap(theme.logo(72))
        title = QLabel("MeshCore Desktop", objectName="hero", alignment=Qt.AlignCenter)
        sub = QLabel("Chat, map and manage your mesh over Bluetooth", objectName="dim", alignment=Qt.AlignCenter)
        over.addWidget(self.mark)
        over.addWidget(title)
        over.addWidget(sub)
        row = QHBoxLayout()
        row.addStretch(1)
        go = QPushButton("Connect to a node", objectName="primary")
        go.clicked.connect(on_connect)
        sim = QPushButton("Try the simulator")
        sim.clicked.connect(on_sim)
        row.addWidget(go)
        row.addWidget(sim)
        row.addStretch(1)
        over.addSpacing(14)
        over.addLayout(row)
        over.addStretch(1)

    def retheme(self):
        self.mark.setPixmap(theme.logo(72))


class BatteryGauge(QWidget):
    """A ring that sweeps round to the battery level."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(QSize(96, 96))
        self.pct: int | None = None
        self.shown = 0.0
        self.anim = QVariantAnimation(self, duration=550, easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._step)

    def _step(self, v):
        self.shown = float(v)
        self.update()

    def set_percent(self, pct: int | None):
        self.pct = pct
        self.setToolTip("" if pct is None else f"Battery {pct}%, on a single Li-ion cell's 3.0 to 4.2 V scale")
        self.anim.stop()
        if pct is None:
            self.shown = 0.0
            self.update()
            return
        self.anim.setStartValue(self.shown)
        self.anim.setEndValue(float(pct))
        self.anim.start()

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        box = QRectF(9, 9, 78, 78)
        p.setPen(QPen(QColor(t.raised), 8, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(box, 225 * 16, -270 * 16)
        if self.pct is not None and self.shown > 0:
            p.setPen(QPen(QColor(battery_color(self.pct)), 8, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(box, 225 * 16, int(-270 * 16 * self.shown / 100))
        f = QFont(self.font())
        f.setBold(True)
        f.setPixelSize(19)
        p.setFont(f)
        p.setPen(QColor(t.text))
        p.drawText(self.rect().adjusted(0, -4, 0, -4), Qt.AlignCenter, "—" if self.pct is None else f"{round(self.shown)}%")
        f.setBold(False)
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(QColor(t.dim))
        p.drawText(QRectF(0, 62, 96, 16), Qt.AlignCenter, "Battery")


class BarChart(QWidget):
    """Horizontal bars for one measure, sorted by the caller, each labelled directly."""
    ROW = 26

    def __init__(self, empty: str, unit: str):
        super().__init__()
        self.empty, self.unit = empty, unit
        self.rows: list[tuple[str, int]] = []
        self.setMouseTracking(True)
        self.setMinimumHeight(self.ROW * 3)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_rows(self, rows: list[tuple[str, int]]):
        self.rows = rows
        self.update()

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.rows:
            p.setPen(QColor(t.dim))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.empty)
            return
        top = max(v for _n, v in self.rows) or 1
        label_w = min(170, self.width() // 3)
        end = self.width() - 46
        fm = p.fontMetrics()
        for i, (name, v) in enumerate(self.rows):
            y = i * self.ROW
            if y + self.ROW > self.height():
                break
            p.setPen(QColor(t.text))
            p.drawText(QRectF(0, y, label_w - 8, self.ROW), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(name, Qt.ElideRight, label_w - 8))
            w = max(4.0, (end - label_w) * v / top)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(QRectF(label_w, y + 8, w, 10), 4, 4)
            p.setPen(QColor(t.dim))
            p.drawText(QRectF(label_w + w + 6, y, 44, self.ROW), Qt.AlignVCenter | Qt.AlignLeft, str(v))

    def mouseMoveEvent(self, e):
        i = int(e.position().y() // self.ROW)
        if 0 <= i < len(self.rows):
            QToolTip.showText(e.globalPosition().toPoint(), f"{self.rows[i][0]}: {self.rows[i][1]} {self.unit}", self)
        else:
            QToolTip.hideText()


class LineChart(QWidget):
    """One measure over time, with a crosshair and readout on hover."""

    def __init__(self, title: str, unit: str, fmt: Callable[[float], str] = lambda v: f"{v:g}"):
        super().__init__()
        self.title, self.unit, self.fmt = title, unit, fmt
        self.points: list[tuple[float, float]] = []
        self.hover: int | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(110)

    def set_points(self, pts: list[tuple[float, float]]):
        self.points = pts
        self.hover = None
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(52, 26, max(10, self.width() - 62), max(10, self.height() - 48))

    def _xy(self, r: QRectF) -> list[QPointF]:
        ts, vs = [t for t, _ in self.points], [v for _, v in self.points]
        t0, t1 = min(ts), max(ts)
        lo, hi = min(vs), max(vs)
        if hi - lo < 1e-6:
            lo, hi = lo - 1, hi + 1
        self.range = lo, hi
        return [QPointF(r.left() + (t - t0) / ((t1 - t0) or 1) * r.width(), r.bottom() - (v - lo) / (hi - lo) * r.height())
                for t, v in self.points]

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(t.text))
        p.drawText(QRectF(0, 0, self.width(), 20), Qt.AlignLeft | Qt.AlignVCenter, f"{self.title} ({self.unit})")
        p.setFont(self.font())
        if len(self.points) < 2:
            p.setPen(QColor(t.dim))
            p.drawText(self.rect().adjusted(0, 20, 0, 0), Qt.AlignCenter | Qt.TextWordWrap,
                       "Each Refresh adds a point. Two are needed to draw a line.")
            return
        r = self._plot()
        xy = self._xy(r)
        lo, hi = self.range
        p.setPen(QPen(QColor(t.border), 1))
        for k in (0, .5, 1):
            y = r.bottom() - k * r.height()
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QColor(t.dim))
        for k in (0, .5, 1):
            y = r.bottom() - k * r.height()
            p.drawText(QRectF(0, y - 9, 46, 18), Qt.AlignRight | Qt.AlignVCenter, self.fmt(lo + (hi - lo) * k))
        for text, align in ((_when(self.points[0][0]), Qt.AlignLeft), (_when(self.points[-1][0]), Qt.AlignRight)):
            p.drawText(QRectF(r.left(), r.bottom() + 4, r.width(), 16), align | Qt.AlignVCenter, text)
        p.setPen(QPen(QColor(t.accent), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(xy)
        if self.hover is not None and self.hover < len(xy):
            h = xy[self.hover]
            p.setPen(QPen(QColor(t.dim), 1))
            p.drawLine(QPointF(h.x(), r.top()), QPointF(h.x(), r.bottom()))
            p.setPen(QPen(QColor(t.surface), 2))
            p.setBrush(QColor(t.accent))
            p.drawEllipse(h, 5, 5)

    def mouseMoveEvent(self, e):
        if len(self.points) < 2:
            return
        xy = self._xy(self._plot())
        x = e.position().x()
        self.hover = min(range(len(xy)), key=lambda i: abs(xy[i].x() - x))
        at, v = self.points[self.hover]
        QToolTip.showText(e.globalPosition().toPoint(), f"{_when(at)}\n{self.fmt(v)} {self.unit}", self)
        self.update()

    def leaveEvent(self, _e):
        self.hover = None
        self.update()


def _when(t: float) -> str:
    return time.strftime("%b %d %H:%M", time.localtime(t))
