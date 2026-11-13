"""The node setup wizard: name, radio, position, contacts, channels and PIN, a
review of what will change, then the changes sent one by one with a tick or
a cross for each. Offered by itself when a newly flashed node connects."""
from __future__ import annotations

import asyncio
import html
import json
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QRadioButton, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import protocol as P
from . import setup as S
from . import theme

log = logging.getLogger("setup")

CUSTOM = "Custom"


def _title(text: str) -> QLabel:
    lab = QLabel(text)
    f = lab.font()
    f.setPointSize(f.pointSize() + 5)
    f.setBold(True)
    lab.setFont(f)
    return lab


def _err() -> QLabel:
    lab = QLabel("", wordWrap=True)
    lab.setStyleSheet(f"color: {theme.current.danger};")
    return lab


def _note(text: str, hint: bool = False) -> QLabel:
    return QLabel(text, objectName="hint" if hint else "dim", wordWrap=True, textFormat=Qt.RichText)


class PositionMap(QWidget):
    """A map to click the node's spot on. The web view only loads when the
    page is first shown, so the wizard opens quickly."""

    def __init__(self, on_pick):
        super().__init__()
        self.on_pick = on_pick  # fn(lat, lon)
        self.view = None
        self.ready = False
        self.pending: list[str] = []
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.setMinimumHeight(240)

    def start(self, pos, others: list[dict], fallback):
        if self.view:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from .mapview import WEB, _Page
        self.view = QWebEngineView(self)  # made before the profile, so it goes first on the way out
        # OpenStreetMap's tile policy asks apps to identify themselves.
        self.profile = QWebEngineProfile("meshcore-pick", self)
        self.profile.setHttpUserAgent("MeshCore-Desktop/0.1 (Qt WebEngine)")
        page = _Page(self.profile, self.view, self._message)
        page.settings().setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.view.setPage(page)
        self.view.loadFinished.connect(self._loaded)
        self.lay.addWidget(self.view)
        self.js(f"setTheme({json.dumps(theme.current.dark)}, {json.dumps(theme.current.accent)})")
        if others:
            self.js(f"setOthers({json.dumps(others)})")
        if pos:
            self.js(f"setPin({pos[0]}, {pos[1]}, 13)")
        elif others:
            self.js(f"lookBounds({json.dumps([[o['lat'], o['lon']] for o in others])})")
        elif fallback:
            self.js(f"look({fallback[0]}, {fallback[1]}, 10)")
        self.view.setUrl(QUrl.fromLocalFile(str(WEB / "pick.html")))

    def _loaded(self, ok):
        self.ready = ok
        for code in self.pending:
            self.view.page().runJavaScript(code)
        self.pending = []

    def js(self, code: str):
        if self.ready:
            self.view.page().runJavaScript(code)
        else:
            self.pending.append(code)

    def _message(self, what: str, arg: str):
        if what == "pick":
            lat, lon = (float(x) for x in arg.split(","))
            self.on_pick(lat, lon)

    def show_pos(self, pos):
        if self.view:
            self.js(f"setPin({pos[0]}, {pos[1]}, 0)" if pos else "clearPin()")


class SetupWizard(QDialog):
    def __init__(self, win, session, over_usb: bool):
        super().__init__(win)
        self.win, self.session, self.over_usb = win, session, over_usb
        model = session.model
        self.si = model.self_info
        self.di = model.device_info
        self.fresh = S.is_fresh(self.si)
        self.setWindowTitle("Set up node")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(560, 600)
        self.running = False

        lay = QVBoxLayout(self)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)
        self.pages.addWidget(self._name_page())
        self.pages.addWidget(self._radio_page())
        self.pages.addWidget(self._position_page())
        self.pages.addWidget(self._channels_page())
        self.pages.addWidget(self._review_page())
        self.pages.addWidget(self._apply_page())

        row = QHBoxLayout()
        self.step_label = QLabel("", objectName="dim")
        row.addWidget(self.step_label, 1)
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(lambda: self._go(-1))
        self.next_btn = QPushButton("Next", objectName="primary")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._next)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        for b in (self.cancel_btn, self.back_btn, self.next_btn):
            row.addWidget(b)
        lay.addLayout(row)
        self._show(0)

    # --- pages --------------------------------------------------------------

    def _page(self, title: str, intro: str = "") -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 4, 4, 4)
        v.addWidget(_title(title))
        if intro:
            v.addWidget(_note(intro))
        return w, v

    def _name_page(self):
        intro = ("This node still has the name it got when it was flashed, so it looks new. A few questions "
                 "and it's ready for the mesh. Everything here can be changed again later from ⋯ → Set up node."
                 if self.fresh else "Go through the node's settings. Anything you leave alone stays as it is.")
        w, v = self._page("Name", intro)
        f = QFormLayout()
        self.name = QLineEdit("" if self.fresh else self.si.name)
        self.name.setPlaceholderText("What others see, like ‘kestrel-mobile’")
        self.name.setMaxLength(P.NAME_MAX)
        self.name.textChanged.connect(self._name_check)
        f.addRow("Node name", self.name)
        v.addLayout(f)
        self.name_err = _err()
        v.addWidget(self.name_err)
        v.addWidget(_note("It goes out in the node's adverts and in front of every channel message you send. "
                          f"Up to {P.NAME_MAX} bytes."))
        if self.fresh:
            v.addWidget(_note(f"Now: <b>{html.escape(self.si.name)}</b> (the first bytes of its key)."))
        v.addStretch(1)
        return w

    def _radio_page(self):
        w, v = self._page("Radio", "Every node in a mesh has to use the same settings, or they can't hear each other. "
                                   "Pick your region's preset, or ask your local mesh what they use.")
        f = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItems([p.title for p in S.PRESETS] + [CUSTOM])
        self.preset.currentTextChanged.connect(self._preset_picked)
        f.addRow("Region", self.preset)
        self.freq = QDoubleSpinBox(decimals=3, minimum=150, maximum=2500, singleStep=0.025, suffix=" MHz")
        self.bw = QComboBox()
        for b in S.BANDWIDTHS:
            self.bw.addItem(f"{b:g} kHz", b)
        self.sf = QSpinBox(minimum=5, maximum=12, prefix="SF")
        self.cr = QSpinBox(minimum=5, maximum=8, prefix="4/")
        self.hash = QComboBox()
        for n in (1, 2, 3):
            self.hash.addItem(f"{n} byte{'s' * (n > 1)}", n)
        self.tx = QSpinBox(minimum=-9, maximum=max(self.si.max_tx, 1), suffix=" dBm")
        f.addRow("Frequency", self.freq)
        f.addRow("Bandwidth", self.bw)
        f.addRow("Spreading factor", self.sf)
        f.addRow("Coding rate", self.cr)
        f.addRow("Hop hash size", self.hash)
        f.addRow("Transmit power", self.tx)
        v.addLayout(f)
        v.addWidget(_note("Hop hash size is how many bytes of each repeater's key a flood carries in its path. "
                          "Bigger meshes use 2 or 3 so repeaters don't get mixed up."))
        v.addWidget(_note("Check what your country allows before raising the power: the most this board does is "
                          f"{self.si.max_tx} dBm, and some boards have an amplifier after the radio.", hint=True))
        v.addStretch(1)
        # The node's current settings, under the preset that matches them.
        self.cur_hash = (self.di.path_hash_mode + 1) if self.di and self.di.protocol >= 10 else 1
        self._set_radio(self.si.freq, self.si.bw, self.si.sf, self.si.cr, self.cur_hash)
        self.tx.setValue(self.si.tx_power)
        for wdg in (self.freq, self.sf, self.cr):
            wdg.valueChanged.connect(self._radio_edited)
        for wdg in (self.bw, self.hash):
            wdg.currentIndexChanged.connect(self._radio_edited)
        self._radio_edited()
        return w

    def _position_page(self):
        w, v = self._page("Position and contacts", "Click the map where the node is. It puts you on other people's "
                                                   "maps, so a rough spot is fine.")
        self.map = PositionMap(self._picked)
        v.addWidget(self.map, 1)
        row = QHBoxLayout()
        has_pos = self.si.lat or self.si.lon
        self.pos = QLineEdit(f"{self.si.lat:.6f}, {self.si.lon:.6f}" if has_pos else "")
        self.pos.setPlaceholderText("or type lat, lon  (like 42.3601, -71.0589)")
        self.pos.textEdited.connect(self._typed_pos)
        row.addWidget(QLabel("Position"))
        row.addWidget(self.pos, 1)
        clear = QPushButton("Clear")
        clear.setToolTip("No position: the node isn't put on maps")
        clear.clicked.connect(lambda: (self.pos.clear(), self._typed_pos("")))
        row.addWidget(clear)
        v.addLayout(row)
        self.pos_err = _err()
        v.addWidget(self.pos_err)
        self.share = QCheckBox("Share the position in adverts")
        self.share.setChecked(self.si.loc_policy != 0)
        self.share.setToolTip("Off keeps it on the node only")
        self.share.clicked.connect(lambda: setattr(self, "_share_touched", True))
        v.addWidget(self.share)
        self.auto_add = QCheckBox("Add contacts automatically when their adverts are heard")
        self.auto_add.setToolTip("Off: only nodes you add yourself. On is easiest on a new node.")
        self.auto_add.setChecked(not (self.si.manual_add & 1))
        v.addWidget(self.auto_add)
        return w

    def _start_map(self):
        ok, pos = self._pos()
        others = [{"lat": c.lat, "lon": c.lon, "name": c.name} for c in self.session.model.contacts.values()
                  if c.lat or c.lon]
        fallback = None
        try:
            last = str(self.win.settings.value("last_position", "") or "")
            if last:
                fallback = tuple(float(x) for x in last.split(","))
        except (ValueError, TypeError):
            fallback = None
        self.map.start(pos if ok else None, others, fallback)

    def _picked(self, lat: float, lon: float):
        self.pos.setText(f"{lat:.6f}, {lon:.6f}")
        self.pos_err.setText("")
        if self.fresh and not self._share_touched:
            self.share.setChecked(True)  # picking a spot on a new node means wanting to be on the map

    _share_touched = False

    def _typed_pos(self, _t):
        ok, pos = self._pos()
        if ok:
            self.map.show_pos(pos)

    def _channels_page(self):
        w, v = self._page("Channels and Bluetooth")
        have = [c.name for _i, c in sorted(self.session.model.channels.items())]
        v.addWidget(_note("On the node now: " + (", ".join(f"<b>{html.escape(n)}</b>" for n in have) or "none")))
        f = QFormLayout()
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("#local, #emergency")
        f.addRow("Join hashtag channels", self.tags)
        v.addLayout(f)
        v.addWidget(_note("A #channel's key comes from its name, so everyone who joins the same name talks together. "
                          "Private channels: + beside Chats once you're set up."))
        self.scope = QLineEdit(self.session.model.default_scope or "")
        self.scope.setPlaceholderText("none: floods go everywhere")
        self.scope.setMaxLength(P.SCOPE_NAME_MAX - 1)
        self.scope_err = _err()
        if self.session.scopes and self.session.model.default_scope is not None:
            f2 = QFormLayout()
            f2.addRow("Flood scope (region)", self.scope)
            v.addLayout(f2)
            v.addWidget(_note("If your mesh's repeaters use regions, the one this node's floods stay in, like ‘usa’ "
                              "or ‘boston’. Channels can have their own: right-click one in Chats."))
            v.addWidget(self.scope_err)
        v.addSpacing(10)
        v.addWidget(QLabel("Bluetooth PIN"))
        self.pin_default = QRadioButton("Firmware default: a new random PIN on the node's screen, or 123456 without one")
        self.pin_fixed = QRadioButton("Always this PIN:")
        self.pin = QLineEdit()
        self.pin.setInputMask("999999")
        self.pin.setMaximumWidth(110)
        grp = QButtonGroup(self)
        grp.addButton(self.pin_default)
        grp.addButton(self.pin_fixed)
        cur_pin = self.di.ble_pin if self.di else 0
        self.pin_fixed.setChecked(bool(cur_pin))
        self.pin_default.setChecked(not cur_pin)
        if cur_pin:
            self.pin.setText(f"{cur_pin:06d}")
        self.pin.textEdited.connect(lambda _t: self.pin_fixed.setChecked(True))
        v.addWidget(self.pin_default)
        row = QHBoxLayout()
        row.addWidget(self.pin_fixed)
        row.addWidget(self.pin)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(_note("A fixed PIN makes pairing from a phone or another computer easier. The node starts using "
                          "a new PIN when it restarts." + (" You're on USB, so this doesn't affect the app now."
                                                          if self.over_usb else "")))
        self.pin_err = _err()
        v.addWidget(self.pin_err)
        v.addStretch(1)
        return w

    def _review_page(self):
        w, v = self._page("Review", "These are sent to the node when you press Apply.")
        self.review = QLabel("", wordWrap=True, textFormat=Qt.RichText)
        self.review.setAlignment(Qt.AlignTop)
        v.addWidget(self.review)
        self.advert = QCheckBox("Send a flood advert when done, so the mesh hears about the node")
        self.advert.setChecked(True)
        self.advert.toggled.connect(self._fill_review)
        self.reboot = QCheckBox("Restart the node afterwards (needed for a new Bluetooth PIN)")
        self.reboot.toggled.connect(self._fill_review)
        v.addWidget(self.advert)
        v.addWidget(self.reboot)
        self.radio_warn = _note("The radio settings change. Nodes on the old settings won't hear this one any more.",
                                hint=True)
        v.addWidget(self.radio_warn)
        v.addStretch(1)
        return w

    def _apply_page(self):
        w, v = self._page("Setting up")
        self.log = QListWidget()
        self.log.setSelectionMode(QListWidget.NoSelection)
        v.addWidget(self.log, 1)
        self.result = QLabel("", wordWrap=True)
        v.addWidget(self.result)
        return w

    # --- radio fields ---------------------------------------------------------

    def _set_radio(self, freq, bw, sf, cr, hash_size):
        self.freq.setValue(freq)
        i = min(range(len(S.BANDWIDTHS)), key=lambda k: abs(S.BANDWIDTHS[k] - bw))
        self.bw.setCurrentIndex(i)
        self.sf.setValue(sf)
        self.cr.setValue(cr)
        self.hash.setCurrentIndex(max(0, min(2, hash_size - 1)))

    def _radio(self) -> tuple[float, float, int, int]:
        return round(self.freq.value(), 3), self.bw.currentData(), self.sf.value(), self.cr.value()

    def _preset_picked(self, title: str):
        p = next((p for p in S.PRESETS if p.title == title), None)
        if p and not self._editing:
            self._editing = True
            self._set_radio(p.freq, p.bw, p.sf, p.cr, p.hash_size)
            self._editing = False

    _editing = False

    def _radio_edited(self, *_):
        if self._editing:
            return
        cur = self.preset.currentText()
        p = next((p for p in S.PRESETS if p.title == cur), None)
        r = self._radio()
        if p and p.matches(*r) and p.hash_size == self.hash.currentData():
            return  # still the picked preset; several share settings
        m = S.find_preset(*r, self.hash.currentData())
        self._editing = True
        self.preset.setCurrentText(m.title if m and m.hash_size == self.hash.currentData() else CUSTOM)
        self._editing = False

    # --- checks and the plan ----------------------------------------------------

    def _name_check(self, *_) -> bool:
        n = self.name.text().strip()
        bad = ""
        if not n:
            bad = "Give it a name." if self.fresh else ""
        elif len(n.encode()) > P.NAME_MAX:
            bad = f"That's {len(n.encode())} bytes; the node keeps {P.NAME_MAX}."
        elif n.upper() == self.si.key[:4].hex().upper():
            bad = "That's the default name; pick your own." if self.fresh else ""
        self.name_err.setText(bad)
        return not bad and (bool(n) or not self.fresh)

    def _pos(self):
        """(ok, position or None)"""
        try:
            p = S.parse_latlon(self.pos.text())
            self.pos_err.setText("")
            return True, p
        except ValueError as e:
            self.pos_err.setText(str(e) if "Enter" in str(e) or "must" in str(e) else "That isn't a position.")
            return False, None

    def _pin(self):
        if self.pin_default.isChecked():
            return True, 0
        t = self.pin.text().strip()
        if len(t) == 6 and t.isdigit() and int(t) >= 100000:
            self.pin_err.setText("")
            return True, int(t)
        self.pin_err.setText("The PIN needs six digits, not starting with 0.")
        return False, None

    def plan(self) -> S.Plan:
        si, p = self.si, S.Plan()
        n = self.name.text().strip()
        if n and n != si.name:
            p.name = n
        r = self._radio()
        if not (abs(r[0] - si.freq) < 0.0005 and abs(r[1] - si.bw) < 0.05 and (r[2], r[3]) == (si.sf, si.cr)):
            p.radio = r
        if self.hash.currentData() != self.cur_hash:
            p.hash_size = self.hash.currentData()
        if self.tx.value() != si.tx_power:
            p.tx_power = self.tx.value()
        _ok, pos = self._pos()
        if pos and (abs(pos[0] - si.lat) > 1e-6 or abs(pos[1] - si.lon) > 1e-6):
            p.location = pos
        elif pos is None and (si.lat or si.lon) and not self.pos.text().strip():
            p.location = (0.0, 0.0)  # cleared
        if self.share.isChecked() != (si.loc_policy != 0):
            p.share_location = self.share.isChecked()
        if self.auto_add.isChecked() != (not (si.manual_add & 1)):
            p.auto_add = self.auto_add.isChecked()
        cur_scope = self.session.model.default_scope
        new_scope = P.region_name(self.scope.text())
        if cur_scope is not None and new_scope != cur_scope and not P.region_problem(new_scope or "x"):
            p.default_scope = new_scope
        have = {c.name.lower() for c in self.session.model.channels.values()}
        p.channels = [(t, S.hashtag_secret(t)) for t in S.parse_hashtags(self.tags.text()) if t not in have]
        _ok, pin = self._pin()
        if pin is not None and pin != (self.di.ble_pin if self.di else 0):
            p.pin = pin
        p.advert = self.advert.isChecked()
        p.reboot = self.reboot.isChecked()
        return p

    def _fill_review(self, *_):
        p = self.plan()
        steps = p.steps()
        changes = [s for s in steps if not s.startswith(("Send a flood", "Restart"))]
        if not changes:
            steps.insert(0, "Nothing to change on the node")
        self.review.setText("<ul style='margin-left:-20px'>" + "".join(f"<li style='margin-bottom:4px'>{html.escape(s)}</li>"
                                                                     for s in steps) + "</ul>")
        self.radio_warn.setVisible(p.radio is not None and not self.fresh)
        self.next_btn.setEnabled(bool(steps) and bool(changes or p.advert or p.reboot))

    # --- navigation -------------------------------------------------------------

    def _show(self, i: int):
        self.pages.setCurrentIndex(i)
        last_input = self.pages.count() - 2
        self.back_btn.setVisible(0 < i <= last_input)
        self.step_label.setText(f"Step {i + 1} of {last_input}" if i < last_input else "")
        self.next_btn.setText("Apply" if i == last_input else "Next")
        self.next_btn.setEnabled(True)
        if i == last_input:
            if self.plan().pin is not None and not self.reboot.isChecked() and not self._reboot_touched:
                self.reboot.setChecked(True)
                self._reboot_touched = True
            self._fill_review()
        if i == 0:
            self.name.setFocus()
        if i == 2:
            self._start_map()

    _reboot_touched = False

    def _go(self, d: int):
        self._show(self.pages.currentIndex() + d)

    def _next(self):
        i = self.pages.currentIndex()
        if i == self.pages.count() - 1:
            self.accept()
            return
        if i == 0 and not self._name_check():
            if not self.name_err.text():
                self.name_err.setText("Give it a name.")
            self.name.setFocus()
            return
        if i == 2 and not self._pos()[0]:
            self.pos.setFocus()
            return
        if i == 3 and not self._pin()[0]:
            self.pin.setFocus()
            return
        if i == 3 and self.scope.text().strip() and (problem := P.region_problem(self.scope.text())):
            self.scope_err.setText(problem)
            self.scope.setFocus()
            return
        if i == 3:
            self.scope_err.setText("")
        if i == self.pages.count() - 2:
            asyncio.ensure_future(self._apply())
            return
        self._go(1)

    def reject(self):
        if not self.running:
            super().reject()

    async def _apply(self):
        plan = self.plan()
        self._show(self.pages.count() - 1)
        self.running = True
        for b in (self.back_btn, self.next_btn, self.cancel_btn):
            b.setVisible(False)
        rows: dict[str, QListWidgetItem] = {}

        def progress(text, ok=None, why=""):
            it = rows.get(text)
            if it is None:
                it = rows[text] = QListWidgetItem()
                self.log.addItem(it)
            mark = "…" if ok is None else ("✓" if ok else "✗")
            it.setText(f"{mark}  {text}" + (f" — {why}" if why else ""))
            if ok is False:
                it.setForeground(QColor(theme.current.danger))
            elif ok:
                it.setForeground(QColor(theme.current.ok))
            self.log.scrollToItem(it)

        if plan.location and plan.location != (0.0, 0.0):
            # Where the map opens next time, for the next node.
            self.win.settings.setValue("last_position", f"{plan.location[0]:.6f},{plan.location[1]:.6f}")
        try:
            ok = await S.apply(self.session, plan, progress)
        except Exception as e:
            log.exception("setup")
            ok = False
            self.result.setText(f"Stopped: {e}")
        else:
            if ok:
                self.result.setText("Done. " + ("The node is restarting; the app reconnects by itself."
                                                if plan.reboot else "The node is ready."))
            else:
                self.result.setText("Some steps didn't work; the rest were sent. Open ⋯ → Set up node to try again.")
        self.running = False
        self.next_btn.setText("Close")
        self.next_btn.setEnabled(True)
        self.next_btn.setVisible(True)
        self.win.toast("Node set up" if ok else "Node setup: some steps failed", not ok)
