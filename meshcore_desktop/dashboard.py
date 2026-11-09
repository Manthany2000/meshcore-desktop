"""The Dashboard and Network tabs: what the mesh has been doing, from what the
node has told us. Packets are counted for this session; messages come from the
saved history."""
from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGridLayout, QLabel, QPlainTextEdit, QToolTip, QVBoxLayout, QWidget)

from . import protocol as P
from . import theme
from .model import Model
from .widgets import BarChart

DAY = 86400
PACKET_NAMES = {0: "Request", 1: "Response", 2: "Direct msg", 3: "Ack", 4: "Advert", 5: "Channel msg", 6: "Channel data",
                7: "Anon request", 8: "Path", 9: "Trace"}


def summary(model: Model, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    ages = [model.contact_age(c) for c in model.contacts.values()]
    return {
        "heard": sum(a is not None and a < DAY for a in ages),
        "repeaters": sum(c.type == P.ADV_REPEATER for c in model.contacts.values()),
        "packets": len(model.packets),
        "messages": sum(now - m.at < DAY for m in model.messages),
    }


def busiest_repeaters(model: Model, limit: int = 8) -> list[tuple[str, int]]:
    """By hop hash, so repeaters sharing a one-byte hash count together."""
    counts = Counter(h for m in model.messages for h in m.repeaters)
    out = []
    for h, n in counts.most_common(limit):
        names = model.hop_names(h)
        out.append((names[0] if names else h, n))
    return out


def channel_activity(model: Model, now: float | None = None) -> list[tuple[str, int]]:
    now = now if now is not None else time.time()
    counts = Counter(m.conv for m in model.messages if m.is_channel and now - m.at < 7 * DAY)
    return [(model.conv_title(c), n) for c, n in counts.most_common(8)]


def packet_line(at: float, p: P.RawPacket) -> str:
    kind = PACKET_NAMES.get(p.payload_type, f"Type {p.payload_type}")
    hops = "direct" if not p.flood else f"{p.hops} hop{'s' * (p.hops != 1)}"
    path = " → ".join(h.hex() for h in p.path.hops()) if p.flood else ""
    return f"{time.strftime('%H:%M:%S', time.localtime(at))}  {kind:<12} {hops:<7} {p.snr:>5.1f} dB  {path}".rstrip()


class Dashboard(QWidget):
    def __init__(self, model: Model):
        super().__init__()
        self.model = model
        grid = QGridLayout(self)
        grid.setContentsMargins(16, 12, 16, 12)
        grid.setSpacing(10)
        self.tiles: dict[str, QLabel] = {}
        for i, (key, caption) in enumerate((("heard", "Nodes heard, 24 h"), ("repeaters", "Repeaters known"),
                                            ("messages", "Messages, 24 h"), ("packets", "Packets this session"))):
            tile = QFrame(objectName="tile")
            v = QVBoxLayout(tile)
            big = QLabel("0", objectName="big")
            self.tiles[key] = big
            v.addWidget(big)
            v.addWidget(QLabel(caption, objectName="dim"))
            grid.addWidget(tile, 0, i)
        self.busiest = BarChart("Repeaters appear here once they relay messages you can hear.", "messages relayed")
        self.channels = BarChart("No channel messages in the last week.", "messages")
        grid.addWidget(self._card("Busiest repeaters", self.busiest), 1, 0, 1, 2)
        grid.addWidget(self._card("Channel activity, 7 days", self.channels), 1, 2, 1, 2)
        self.feed = QPlainTextEdit(readOnly=True)
        self.feed.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.feed.setMaximumBlockCount(300)
        self.feed.setPlaceholderText("Packets your node hears will scroll by here.")
        self.feed.setLineWrapMode(QPlainTextEdit.NoWrap)
        grid.addWidget(self._card("Live packets", self.feed), 2, 0, 1, 4)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 1)
        self.timer = QTimer(self, interval=3000, timeout=self.refresh)

    def _card(self, title: str, inner: QWidget) -> QFrame:
        card = QFrame(objectName="tile")
        v = QVBoxLayout(card)
        head = QLabel(title)
        f = head.font()
        f.setBold(True)
        head.setFont(f)
        v.addWidget(head)
        v.addWidget(inner, 1)
        return card

    def refresh(self):
        for k, v in summary(self.model).items():
            self.tiles[k].setText(f"{v:,}")
        self.busiest.set_rows(busiest_repeaters(self.model))
        self.channels.set_rows(channel_activity(self.model))

    def add_packet(self, p: P.RawPacket):
        self.feed.appendPlainText(packet_line(time.time(), p))

    def showEvent(self, e):
        self.refresh()
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)


ME = "me"


def mesh_links(model: Model) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Node weights and links among repeaters and us, from message and contact
    paths. Hops are hash prefixes, so a shared hash shows as one node."""
    weight: Counter = Counter()
    links: Counter = Counter()

    def walk(hops: list[str], n: int = 1):
        for a, b in zip(hops, hops[1:]):
            links[tuple(sorted((a, b)))] += n
        for h in hops:
            weight[h] += n

    for m in model.messages:
        if m.path:
            walk(m.path + [ME])
    for c in model.contacts.values():
        if c.hops > 0:
            walk([ME] + [h.hex() for h in c.path.hops()])
        elif c.hops == 0 and c.type == P.ADV_REPEATER:
            walk([ME, c.key[:1].hex()])
    return dict(weight), dict(links)


class NetworkGraph(QWidget):
    """Who relays through whom, laid out by a small force simulation."""
    MAX_NODES = 150

    def __init__(self, model: Model):
        super().__init__()
        self.model = model
        self.pos: dict[str, list[float]] = {}
        self.weight: dict[str, int] = {}
        self.links: dict[tuple[str, str], int] = {}
        self.heat = 0.0
        self.hover: str | None = None
        self.setMouseTracking(True)
        self.sim = QTimer(self, interval=33, timeout=self._step)
        self.poll = QTimer(self, interval=5000, timeout=self.rebuild)

    def rebuild(self):
        weight, links = mesh_links(self.model)
        keep = {ME} | {h for h, _ in Counter(weight).most_common(self.MAX_NODES - 1)}
        weight = {h: n for h, n in weight.items() if h in keep}
        links = {k: n for k, n in links.items() if k[0] in keep and k[1] in keep}
        fresh = set(weight) != set(self.pos)
        self.weight, self.links = weight, links
        for h in weight:
            self.pos.setdefault(h, [random.uniform(-.6, .6), random.uniform(-.6, .6)])
        for h in set(self.pos) - set(weight):
            del self.pos[h]
        if fresh:
            self.heat = 1.0
        if self.heat >= .02:
            self.sim.start()
        self.update()

    def _step(self):
        """Nodes push apart, links pull together, and the whole thing cools."""
        names = list(self.pos)
        if not names or self.heat < .02:
            self.sim.stop()
            return
        k = 1.6 / math.sqrt(len(names))
        force = defaultdict(lambda: [0.0, 0.0])
        for i, a in enumerate(names):
            pa = self.pos[a]
            for b in names[i + 1:]:
                pb = self.pos[b]
                dx, dy = pa[0] - pb[0], pa[1] - pb[1]
                d2 = dx * dx + dy * dy + 1e-4
                f = k * k / d2
                force[a][0] += dx * f
                force[a][1] += dy * f
                force[b][0] -= dx * f
                force[b][1] -= dy * f
        for (a, b) in self.links:
            pa, pb = self.pos[a], self.pos[b]
            dx, dy = pa[0] - pb[0], pa[1] - pb[1]
            d = math.hypot(dx, dy) + 1e-4
            f = d / k
            force[a][0] -= dx * f
            force[a][1] -= dy * f
            force[b][0] += dx * f
            force[b][1] += dy * f
        limit = .07 * self.heat
        for n in names:
            fx, fy = force[n]
            size = math.hypot(fx, fy) + 1e-6
            step = min(size * .02, limit)
            self.pos[n][0] = max(-1, min(1, self.pos[n][0] + fx / size * step - self.pos[n][0] * .01))
            self.pos[n][1] = max(-1, min(1, self.pos[n][1] + fy / size * step - self.pos[n][1] * .01))
        self.heat *= .975
        self.update()

    def _screen(self, h: str) -> QPointF:
        x, y = self.pos[h]
        return QPointF(self.width() / 2 + x * (self.width() / 2 - 70), self.height() / 2 + y * (self.height() / 2 - 50))

    def _radius(self, h: str) -> float:
        return 12 if h == ME else 5 + min(9, math.sqrt(self.weight.get(h, 1)))

    def _name(self, h: str) -> str:
        if h == ME:
            return self.model.self_info.name if self.model.self_info else "You"
        names = self.model.hop_names(h)
        return names[0] if names else h

    def _near(self, h: str) -> set[str]:
        return {h} | {b if a == h else a for a, b in self.links if h in (a, b)}

    def paintEvent(self, _e):
        t = theme.current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if len(self.pos) < 2:
            p.setPen(QColor(t.dim))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                       "The network appears here as your node learns routes and hears relayed messages.")
            return
        near = self._near(self.hover) if self.hover else None
        top = max(self.links.values(), default=1)
        for (a, b), n in self.links.items():
            on = near is None or (a == self.hover or b == self.hover)
            c = QColor(t.accent if on and near else t.dim)
            c.setAlpha(190 if on and near else 90 if on else 25)
            p.setPen(QPen(c, 1 + 3 * n / top))
            p.drawLine(self._screen(a), self._screen(b))
        labelled = {h for h, _ in Counter(self.weight).most_common(14)} | {ME}
        for h in self.pos:
            pt, r = self._screen(h), self._radius(h)
            fill = QColor(t.warn if h == ME else t.accent)
            if near is not None and h not in near:
                fill.setAlpha(50)
            p.setPen(QPen(QColor(t.bg), 2))
            p.setBrush(fill)
            p.drawEllipse(pt, r, r)
            if h in labelled or (near is not None and h in near):
                if near is None or h in near:
                    p.setPen(QColor(t.text))
                    p.drawText(pt + QPointF(r + 4, 4), self._name(h))
        f = QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(QColor(t.dim))
        p.drawText(14, 22, "Line thickness: how often a link was used. Dot size: how often a repeater appears. Hover to trace.")

    def mouseMoveEvent(self, e):
        at = e.position()
        hit = next((h for h in self.pos if math.dist((at.x(), at.y()), (self._screen(h).x(), self._screen(h).y()))
                    <= self._radius(h) + 4), None)
        if hit != self.hover:
            self.hover = hit
            self.update()
        if hit:
            ways = len(self._near(hit)) - 1
            tag = "" if hit == ME else f" ({hit})"
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"{self._name(hit)}{tag}\nSeen on {self.weight.get(hit, 0)} paths, {ways} link{'s' * (ways != 1)}", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    def showEvent(self, e):
        self.rebuild()
        self.poll.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.poll.stop()
        self.sim.stop()
        super().hideEvent(e)
