"""Node settings: everything the setup wizard sets, plus who the node shares
readings with, on tabs that can be opened at any time. Fields start as the node
has them; Apply sends only what was changed, one step at a time."""
from __future__ import annotations

import asyncio
import html
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

from . import setup as S
from .setup_ui import ApplyLog, NodeForm, _note

log = logging.getLogger("settings")

ABOUT = 4  # the About tab's index

TELEMETRY_LABELS = {"base": "Battery and basic readings", "location": "Location",
                    "environment": "Environment sensors"}


class NodeSettings(NodeForm):
    def __init__(self, win, session, over_usb: bool):
        super().__init__(win, session, over_usb)
        self.setWindowTitle("Node settings")
        self.resize(600, 640)
        lay = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._general_tab(), "General")
        self.tabs.addTab(self._radio_tab(), "Radio")
        self.tabs.addTab(self._privacy_tab(), "Privacy")
        self.tabs.addTab(self._bluetooth_tab(), "Bluetooth and scope")
        self.tabs.addTab(self._about_tab(), "About")
        self.tabs.currentChanged.connect(self._tab_shown)
        lay.addWidget(self.tabs, 1)

        self.log = ApplyLog()
        self.log.setMaximumHeight(150)
        self.log.hide()
        lay.addWidget(self.log)
        self.radio_warn = _note("The radio settings change. Nodes on the old settings won't hear this one any more.",
                                hint=True)
        lay.addWidget(self.radio_warn)
        self.advert = QCheckBox("Send a flood advert afterwards, so the mesh hears the new name or position")
        self.advert.toggled.connect(lambda _on: setattr(self, "_advert_touched", True))
        lay.addWidget(self.advert)

        row = QHBoxLayout()
        self.summary = QLabel("", objectName="dim")
        row.addWidget(self.summary, 1)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setToolTip("Put the fields back to what the node has")
        self.revert_btn.clicked.connect(self._revert)
        self.apply_btn = QPushButton("Apply", objectName="primary")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(lambda: asyncio.ensure_future(self._apply()))
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        for b in (self.revert_btn, self.apply_btn, self.close_btn):
            row.addWidget(b)
        lay.addLayout(row)
        QShortcut(QKeySequence.Save, self, activated=self.apply_btn.click)

        self._load()
        self._watch()
        self._edited()
        QTimer.singleShot(0, self._start_map)  # General shows first

    # --- tabs -------------------------------------------------------------------

    @staticmethod
    def _tab() -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 10, 8, 4)
        return w, v

    def _general_tab(self):
        w, v = self._tab()
        self._name_fields(v)
        v.addSpacing(6)
        v.addWidget(_note("Click the map where the node is, or type its position. A rough spot is fine."))
        self._position_fields(v)
        return w

    def _radio_tab(self):
        w, v = self._tab()
        v.addWidget(_note("Every node in a mesh has to use the same settings, or they can't hear each other."))
        self._radio_fields(v)
        v.addStretch(1)
        return w

    def _privacy_tab(self):
        w, v = self._tab()
        v.addWidget(_note("Other nodes can ask this one for its readings (telemetry). Choose who it answers, for "
                          "each kind. “Only contacts I allow” means contacts you've given permission in their "
                          "settings, in an app that has it."))
        f = QFormLayout()
        self.telem: dict[str, QComboBox] = {}
        for k in S.TELEMETRY_KINDS:
            c = QComboBox()
            for mode, label in S.TELEMETRY_MODES.items():
                c.addItem(label, mode)
            self.telem[k] = c
            f.addRow(TELEMETRY_LABELS[k], c)
        v.addLayout(f)
        v.addSpacing(10)
        self.multi_acks = QCheckBox("Send an extra acknowledgement for each direct message received")
        self.multi_acks.setToolTip("A second copy of each ack makes delivery ticks more reliable on busy meshes, "
                                   "at the cost of a little more airtime")
        v.addWidget(self.multi_acks)
        v.addWidget(_note("Sharing the node's position on the map and adding contacts automatically are on the "
                          "General tab."))
        v.addStretch(1)
        return w

    def _bluetooth_tab(self):
        w, v = self._tab()
        self._scope_fields(v)
        if self.scope_shown:
            v.addSpacing(10)
        self._pin_fields(v)
        v.addStretch(1)
        return w

    def _about_tab(self):
        w, lay = self._tab()
        si, di, model = self.si, self.di, self.session.model
        f = QFormLayout()

        def row(label, value):
            lab = QLabel(html.escape(str(value)) if value not in (None, "") else "—")
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            f.addRow(label, lab)
        if di:
            row("Board", di.model)
            row("Firmware", " ".join(x for x in (di.fw, f"({di.build})" if di.build else "") if x))
            row("Protocol", f"v{di.protocol}")
            row("Contacts", f"{len(model.contacts)} of {di.max_contacts}" if di.max_contacts else len(model.contacts))
            row("Channel slots", f"{len(model.channels)} of {di.max_channels}")
        row("Battery", f"{model.battery_mv / 1000:.2f} V" if model.battery_mv else "")
        row("Max transmit power", f"{si.max_tx} dBm")
        lay.addLayout(f)
        lay.addSpacing(8)
        lay.addWidget(QLabel("Public key"))
        key = QLabel(si.key.hex(), wordWrap=True)
        key.setTextInteractionFlags(Qt.TextSelectableByMouse)
        key.setStyleSheet("font-family: monospace;")
        lay.addWidget(key)
        btns = QHBoxLayout()
        copy = QPushButton("Copy key")
        copy.clicked.connect(lambda: (QGuiApplication.clipboard().setText(si.key.hex()),
                                      self.win.toast("Public key copied")))
        btns.addWidget(copy)
        self.restart_btn = QPushButton("Restart node…")
        self.restart_btn.setToolTip("The app reconnects by itself once it's back")
        self.restart_btn.clicked.connect(self._restart)
        btns.addWidget(self.restart_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addStretch(1)
        return w

    def _fill_about(self):
        """Rebuilt from the node's latest details."""
        cur = self.tabs.currentIndex()
        old = self.tabs.widget(ABOUT)
        self.tabs.removeTab(ABOUT)
        old.deleteLater()
        self.tabs.insertTab(ABOUT, self._about_tab(), "About")
        self.tabs.setCurrentIndex(cur)

    def _tab_shown(self, i: int):
        if i == 0:
            self._start_map()

    # --- values -------------------------------------------------------------------

    def _load(self):
        super()._load()
        modes = S.telemetry_modes(self.si.telemetry)
        for k, c in self.telem.items():
            i = c.findData(modes[k])
            if i < 0:
                c.addItem(f"Mode {modes[k]}", modes[k])
                i = c.count() - 1
            c.setCurrentIndex(i)
        self.multi_acks.setChecked(bool(self.si.multi_acks))

    def _watch(self):
        """Recount the changes whenever a field is edited."""
        for wdg in (self.name, self.pos, self.pin, self.scope):
            wdg.textChanged.connect(self._edited)
        for wdg in (self.freq, self.sf, self.cr, self.tx):
            wdg.valueChanged.connect(self._edited)
        for wdg in (self.bw, self.hash, self.preset, *self.telem.values()):
            wdg.currentIndexChanged.connect(self._edited)
        for wdg in (self.share, self.auto_add, self.multi_acks, self.pin_default, self.pin_fixed):
            wdg.toggled.connect(self._edited)

    def plan(self) -> S.Plan:
        p = super().plan()
        telem = S.telemetry_byte({k: c.currentData() for k, c in self.telem.items()}, self.si.telemetry)
        if telem != self.si.telemetry:
            p.telemetry = telem
        if self.multi_acks.isChecked() != bool(self.si.multi_acks):
            p.multi_acks = self.multi_acks.isChecked()
        p.advert = self.advert.isChecked()
        return p

    _advert_touched = False

    def _edited(self, *_):
        if not hasattr(self, "summary"):
            return  # still building
        p = self.plan()
        changes = [s for s in p.steps() if not s.startswith("Send a flood")]
        if not self._advert_touched:
            # A new name or position only reaches others in an advert.
            self.advert.blockSignals(True)
            self.advert.setChecked(p.name is not None or p.location is not None or p.share_location is not None)
            self.advert.blockSignals(False)
        ready = self._ready()
        self.summary.setText("No changes" if not changes else
                             f"{len(changes)} change{'s' * (len(changes) > 1)} to send")
        self.summary.setToolTip("\n".join(changes))
        self.radio_warn.setVisible(p.radio is not None or p.hash_size is not None)
        self.apply_btn.setEnabled(bool(changes) and ready and not self.running)
        self.revert_btn.setEnabled(bool(changes) and not self.running)
        if not ready:
            self.summary.setText(self.summary.text() + " · not connected")

    def _ready(self) -> bool:
        return bool(self.session and self.session.connected and self.win.state == "ready")

    def _revert(self):
        self._load()
        self._advert_touched = False
        self._edited()

    def link_changed(self, session, state: str):
        """The main window's link came or went. Edits stay; once it's back they're
        compared with what the node has then."""
        if session:
            self.session = session
        if state == "ready" and session and session.model.self_info:
            self._baseline()
            self._fill_about()
        self._edited()

    # --- sending ------------------------------------------------------------------

    def _check(self) -> bool:
        if not self._name_check():
            self.tabs.setCurrentIndex(0)
            self.name.setFocus()
            return False
        if not self._pos()[0]:
            self.tabs.setCurrentIndex(0)
            self.pos.setFocus()
            return False
        if not self._pin()[0]:
            self.tabs.setCurrentIndex(3)
            self.pin.setFocus()
            return False
        if not self._scope_check():
            self.tabs.setCurrentIndex(3)
            self.scope.setFocus()
            return False
        return True

    def _ask(self, title: str, text: str, yes: str, no: str = "Cancel") -> bool:
        box = QMessageBox(QMessageBox.Question, title, text, QMessageBox.NoButton, self)
        go = box.addButton(yes, QMessageBox.AcceptRole)
        box.addButton(no, QMessageBox.RejectRole)
        box.setDefaultButton(go)
        box.exec()
        return box.clickedButton() is go

    async def _apply(self):
        if self.running or not self._ready() or not self._check():
            return
        plan = self.plan()
        if plan.radio is not None or plan.hash_size is not None:
            if not self._ask("Change the radio?",
                             "Nodes still on the old settings won't hear this one any more, and it won't hear them."
                             "<br><br>Only do this if your local mesh uses the new settings.", "Change radio"):
                return
        if plan.pin is not None:
            plan.reboot = self._ask("New Bluetooth PIN",
                                    "The node only starts using the new PIN after a restart. Restart it once the "
                                    "settings are sent?" + ("" if self.over_usb else
                                                            "<br><br>Your computer may ask for the new PIN when "
                                                            "it reconnects."),
                                    "Restart afterwards", "Not now")
        self.running = True
        for wdg in (self.tabs, self.advert, self.apply_btn, self.revert_btn):
            wdg.setEnabled(False)
        self.log.start_over()
        self.log.show()
        try:
            ok = await self._send(plan, self.log)
        except Exception as e:
            log.exception("settings")
            self.log.progress(f"Stopped: {e}", False)
            ok = False
        self.running = False
        for wdg in (self.tabs, self.advert):
            wdg.setEnabled(True)
        self.win.toast("Node settings saved" if ok else "Node settings: some steps failed", not ok)
        if plan.reboot:
            self.log.progress("The node is restarting; the app reconnects by itself", True)
        elif self.session.model.self_info:
            # The node's new settings, read back, are the new starting point. Fields
            # whose step failed go back to what the node kept.
            self._baseline()
            self._load()
            self._fill_about()
        self._advert_touched = False
        self._edited()

    def _restart(self):
        if not self._ready():
            return
        if self._ask("Restart node?", "The link drops while it restarts, and the app reconnects by itself.",
                     "Restart"):
            asyncio.ensure_future(self.session.reboot())
            self.win.toast("Node restarting")

    def reject(self):
        if self.running:
            return
        if self.apply_btn.isEnabled() and not self._ask("Unsent changes",
                                                        "Close without sending your changes to the node?",
                                                        "Discard changes", "Keep editing"):
            return
        super().reject()
