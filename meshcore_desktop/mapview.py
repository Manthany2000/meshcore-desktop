"""A map of the contacts that advertise a location: Leaflet and OpenStreetMap
tiles in a web view, fed from the model."""
from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtCore import QStringListModel
from PySide6.QtGui import QDesktopServices, QKeySequence, QPalette, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from . import protocol as P
from . import theme
from .admin import STAT_ROWS
from .desktop import short_date
from .model import CLOCK_SANE, Model, dm_conv

WEB = Path(__file__).parent / "web"
AGES = [("Any time", None), ("Last 24 h", 86400), ("Last 7 days", 7 * 86400), ("Last 30 days", 30 * 86400)]
STALE_S = 3 * 86400  # drawn faded


def _ago(sec):
    if sec is None:
        return "—"
    sec = int(sec)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if sec >= n:
            return f"{sec // n} {unit} ago"
    return "just now"


TYPE_NAMES = {P.ADV_CHAT: "Chat", P.ADV_REPEATER: "Repeater", P.ADV_ROOM: "Room", P.ADV_SENSOR: "Sensor"}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _stamp(t: float) -> str:
    """A date and time, or a word for a clock that was never set."""
    if t < CLOCK_SANE:
        return "clock not set" if t else "never"
    lt = time.localtime(t)
    return f"{short_date(lt)} {lt.tm_year}, {time.strftime('%H:%M', lt)}"


def _bearing(la1, lo1, la2, lo2) -> str:
    a1, a2, d = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    deg = math.degrees(math.atan2(math.sin(d) * math.cos(a2),
                                  math.cos(a1) * math.sin(a2) - math.sin(a1) * math.cos(a2) * math.cos(d))) % 360
    return f"{deg:.0f}° {COMPASS[int((deg + 22.5) // 45) % 8]}"


def _section(title: str, rows: list) -> dict:
    return {"title": title, "rows": [[k, str(v)] for k, v in rows if v not in (None, "")]}


def relay_counts(model: Model) -> Counter:
    """How many saved messages each hop hash helped relay."""
    return Counter(h for m in model.messages for h in m.repeaters)


def contact_details(model: Model, c: P.Contact, relays: Counter, stats: dict[str, str]) -> list[dict]:
    """Everything we know about a contact, for the hover card and side panel."""
    s = model.self_info
    age = model.contact_age(c)
    heard = "unknown" if age is None else _ago(age)
    if c.prefix in model.heard:
        heard += f" ({_stamp(model.heard[c.prefix])})"
    route = "No known route: floods" if c.hops < 0 else "Direct neighbour" if c.hops == 0 else f"{c.hops} hops"
    km = model.distance_km(c)
    out = [
        _section("Identity", [("Type", TYPE_NAMES.get(c.type, f"Unknown ({c.type})")),
                              ("Favourite", "Yes" if c.flags & 1 else None),
                              ("Public key", c.key.hex()), ("Hop hash", c.key[:1].hex())]),
        _section("Reach", [("Route", route), ("Last heard", heard), ("Last advert", _stamp(c.last_advert)),
                           ("Contact updated", _stamp(c.last_mod)),
                           ("Distance", f"{km:.1f} km" if km is not None else None),
                           ("Bearing", _bearing(s.lat, s.lon, c.lat, c.lon) if km is not None else None)]),
        _section("Location", [("Latitude", f"{c.lat:.6f}"), ("Longitude", f"{c.lon:.6f}")]),
    ]
    if c.hops > 0:
        rows = []
        for i, h in enumerate(c.path.hops(), 1):
            names = model.hop_names(h.hex())
            who = (names[0] + (f" (or {len(names) - 1} other{'s' * (len(names) > 2)})" if len(names) > 1 else "")) if names \
                else "unknown"
            rows.append((f"Hop {i}", f"{h.hex()} · {who}"))
        out.append(_section("Path", rows))
    conv = dm_conv(c.prefix)
    msgs = model.conversation(conv)
    relayed = sum(relays[c.key[:n].hex()] for n in (1, 2, 3)) if c.type == P.ADV_REPEATER else None
    out.append(_section("Activity", [
        ("Saved messages", len(msgs)), ("Last message", _stamp(msgs[-1].at) if msgs else None),
        ("Relayed messages", f"{relayed} (by hop hash, may include others)" if relayed else None)]))
    if stats:
        out.append(_section("Repeater status", [(label, stats.get(key)) for label, key in STAT_ROWS]))
    return [x for x in out if x["rows"]]


def self_details(model: Model) -> list[dict]:
    s, d = model.self_info, model.device_info
    mv = model.battery_mv
    out = [
        _section("Identity", [("Public key", s.key.hex()), ("Hop hash", s.key[:1].hex())]),
        _section("Radio", [("Frequency", f"{s.freq:.3f} MHz"), ("Bandwidth", f"{s.bw:g} kHz"),
                           ("Spreading factor", s.sf), ("Coding rate", s.cr),
                           ("TX power", f"{s.tx_power} dBm (max {s.max_tx})")]),
        _section("Location", [("Latitude", f"{s.lat:.6f}"), ("Longitude", f"{s.lon:.6f}")]),
        _section("Node", [("Battery", f"{mv / 1000:.2f} V ({P.batt_percent(mv)}%)" if mv else None),
                          ("Contacts", len(model.contacts)), ("Channels", len(model.channels))]),
    ]
    if d:
        out.append(_section("Device", [("Model", d.model), ("Firmware", d.fw), ("Build", d.build),
                                       ("Protocol", d.protocol), ("Max contacts", d.max_contacts),
                                       ("Max channels", d.max_channels)]))
    return [x for x in out if x["rows"]]


def contact_actions(c: P.Contact) -> list[list[str]]:
    acts = []
    if c.type == P.ADV_REPEATER:
        acts.append(["manage", "Manage repeater"])
    if c.type in (P.ADV_CHAT, P.ADV_ROOM):
        acts.append(["dm", "Message"])
    if c.hops > 0:
        acts.append(["route", "Show route"])
    acts += [["zoom", "Zoom here"], ["copy", "Copy public key"]]
    if c.hops >= 0:
        acts.append(["reset", "Reset route"])
    return acts


class _Page(QWebEnginePage):
    """The page's buttons say what they want as "meshcore:<what>:<arg>" on the
    console. Real links (the OSM attribution) open in the browser."""

    def __init__(self, profile, parent, on_message):
        super().__init__(profile, parent)
        self.on_message = on_message

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith("meshcore:"):
            _, what, arg = message.split(":", 2)
            self.on_message(what, arg)

    def acceptNavigationRequest(self, url, nav_type, is_main):
        if nav_type == QWebEnginePage.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main)


class MapView(QWidget):
    def __init__(self, model: Model, open_conv, action=lambda what, c: None, stats=lambda prefix: {}):
        super().__init__()
        self.model = model
        self.open_conv = open_conv
        self.action = action  # fn(what, contact): things only the window can do
        self.stats = stats  # fn(prefix) -> what the Repeater tab has read this session
        self.ready = False
        self.pending_focus: str | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 8, 16, 8)
        self.search = QLineEdit(placeholderText="Search nodes (name or key)…", clearButtonEnabled=True, objectName="round")
        self.search.setMinimumWidth(170)
        self.search.setToolTip("Matches names, or the start of a node's key: a hop hash like a3 finds that repeater.\n"
                               "Enter zooms to the results. Ctrl+F jumps here, Esc clears.")
        self.names = QStringListModel(self)
        comp = QCompleter(self.names, self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.activated.connect(lambda _t: QTimer.singleShot(0, self.go_to_search))
        self.search.setCompleter(comp)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.search.returnPressed.connect(self.go_to_search)
        bar.addWidget(self.search)
        QShortcut(QKeySequence.Find, self, activated=lambda: (self.search.setFocus(), self.search.selectAll()))
        QShortcut(QKeySequence(Qt.Key_Escape), self.search, activated=self.search.clear,
                  context=Qt.WidgetShortcut)
        self.types = {}
        for t, name in ((P.ADV_CHAT, "Chat"), (P.ADV_REPEATER, "Repeaters"), (P.ADV_ROOM, "Rooms"),
                        (P.ADV_SENSOR, "Sensors")):
            cb = QCheckBox(name, checked=True, objectName="chip")
            cb.toggled.connect(self.refresh)
            self.types[t] = cb
            bar.addWidget(cb)
        self.labels = QCheckBox("Names", checked=True, objectName="chip")
        self.labels.toggled.connect(self.refresh)
        bar.addWidget(self.labels)
        self.age = QComboBox()
        for name, _s in AGES:
            self.age.addItem(name)
        self.age.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.age)
        self.count = QLabel("", objectName="dim")
        self.count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.count.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # mustn't set the window's width
        self.setMinimumWidth(560)  # nor must this bar: the details panel needs that room on other tabs
        bar.addWidget(self.count, 1)
        fit = QPushButton("Fit all")
        fit.clicked.connect(lambda: self.ready and self.view.page().runJavaScript("fit()"))
        bar.addWidget(fit)
        lay.addLayout(bar)

        # OpenStreetMap's tile policy asks apps to identify themselves.
        self.profile = QWebEngineProfile("meshcore-map", self)
        self.profile.setHttpUserAgent("MeshCore-Desktop/0.1 (Qt WebEngine)")
        self.view = QWebEngineView()
        page = _Page(self.profile, self.view, self._link)
        page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.view.setPage(page)
        self.view.loadFinished.connect(self._loaded)
        self.view.setUrl(QUrl.fromLocalFile(str(WEB / "map.html")))
        lay.addWidget(self.view, 1)

        self._timer = QTimer(self, singleShot=True, interval=500, timeout=self.refresh)
        self._search_timer = QTimer(self, singleShot=True, interval=200, timeout=self.refresh)

    def _loaded(self, ok):
        self.ready = ok
        self.refresh()
        if self.pending_focus:
            self.focus(self.pending_focus)

    def _link(self, what: str, arg: str):
        if what == "copy" and arg == "self" and self.model.self_info:
            QApplication.clipboard().setText(self.model.self_info.key.hex())
            return
        try:
            c = self.model.contacts.get(bytes.fromhex(arg))
        except ValueError:
            return
        if c is None:
            return
        if what == "dm":
            self.open_conv(dm_conv(c.prefix))
        elif what == "copy":
            QApplication.clipboard().setText(c.key.hex())
        elif what in ("manage", "reset"):
            self.action(what, c)

    def schedule(self):
        """Contacts arrive in bursts: redraw once they settle."""
        self._timer.start()

    def _located(self, c):
        return bool(c.lat or c.lon)

    def _hop(self, h: bytes):
        """The likeliest node for a hop hash that has a location: repeaters first."""
        cs = [c for c in self.model.contacts.values() if c.key.startswith(h)]
        cs.sort(key=lambda c: c.type != P.ADV_REPEATER)
        return cs[0] if cs else None

    def _matches(self, c, q: str) -> bool:
        return q in (c.name or "").lower() or c.key.hex().startswith(q)

    def _query(self) -> str:
        return self.search.text().strip().lower()

    def refresh(self):
        if not self.ready:
            return
        names = sorted({c.name for c in self.model.contacts.values() if c.name}, key=str.lower)
        if names != self.names.stringList():
            self.names.setStringList(names)
        q = self._query()
        max_age = AGES[self.age.currentIndex()][1]
        nodes, total, unlocated = [], 0, []
        relays = relay_counts(self.model)
        for c in self.model.contacts.values():
            if q and not self._matches(c, q):
                continue
            if not self._located(c):
                unlocated.append(c.name or c.key[:3].hex())
                continue
            total += 1
            age = self.model.contact_age(c)
            # A search finds a node whatever the other filters say.
            if not q:
                cb = self.types.get(c.type)
                if cb is not None and not cb.isChecked():
                    continue
                if max_age and (age is None or age > max_age):
                    continue
            route = []
            for h in c.path.hops() if c.hops > 0 else []:
                hc = self._hop(h)
                route.append({"lat": hc.lat, "lon": hc.lon} if hc and self._located(hc) else None)
            sub = TYPE_NAMES.get(c.type, "Node") + " · " + ("heard " + _ago(age) if age is not None else "not heard yet")
            nodes.append({"key": c.prefix.hex(), "name": c.name or c.key[:3].hex(), "type": c.type,
                          "lat": c.lat, "lon": c.lon, "sub": sub, "stale": age is None or age > STALE_S, "route": route,
                          "sections": contact_details(self.model, c, relays, self.stats(c.prefix)),
                          "actions": contact_actions(c)})
        self.shown = [n["key"] for n in nodes]
        s = self.model.self_info
        if s and (s.lat or s.lon) and (not q or q in s.name.lower()):
            nodes.append({"key": "self", "self": True, "name": s.name, "lat": s.lat, "lon": s.lon, "sub": "Your node",
                          "sections": self_details(self.model), "actions": [["zoom", "Zoom here"], ["copy", "Copy public key"]]})
        dark = self.palette().color(QPalette.Base).lightness() < 128
        if q:
            msg = f"{len(self.shown)} match{'es' * (len(self.shown) != 1)}"
            if unlocated:
                few = ", ".join(unlocated[:3]) + (f" +{len(unlocated) - 3}" if len(unlocated) > 3 else "")
                msg += f" · no location: {few}"
            self.search.setStyleSheet("" if self.shown or unlocated else "background:#5a1d1d" if dark
                                      else "background:#ffe0e0")
        else:
            msg = f"{len(self.shown)} of {len(self.model.contacts)} shown"
            self.count.setToolTip(f"{total} of {len(self.model.contacts)} contacts have a location")
            self.search.setStyleSheet("")
        self.count.setText(msg)
        opts = {"labels": self.labels.isChecked(), "dark": dark, "colors": asdict(theme.current)}
        self.view.page().runJavaScript(f"setNodes({json.dumps(nodes)}, {json.dumps(opts)})")

    def go_to_search(self):
        """Enter: fly to the one match, or fit them all. An exact name wins."""
        self.refresh()
        q = self._query()
        if not q or not self.shown:
            return
        exact = [c.prefix.hex() for c in self.model.contacts.values()
                 if (c.name or "").lower() == q and c.prefix.hex() in self.shown]
        if exact or len(self.shown) == 1:
            self.focus((exact or self.shown)[0])
        else:
            self.view.page().runJavaScript("fit()")

    def focus(self, prefix_hex: str):
        if not self.ready:
            self.pending_focus = prefix_hex
            return
        self.pending_focus = None
        self.view.page().runJavaScript(f"focusNode({json.dumps(prefix_hex)})")
