"""The Repeater tab: log in to a repeater over the mesh, read its stats, and run
its CLI. Passwords are only ever in the password box and the login frame. They
aren't saved or logged, and a repeater that has us as admin takes a blank login."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QBoxLayout, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import protocol as P
from . import theme
from .live import LivePage
from .widgets import BatteryGauge, LineChart, place_pairs

NARROW = 600  # below this the status and settings grids go to one column

log = logging.getLogger("admin")

CONFIRM = ("reboot", "clkreboot", "poweroff", "shutdown", "start ota", "password ", "tempradio ",
           "set prv.key", "set guest.password", "set name", "set repeat", "set radio", "set tx", "clear stats",
           "neighbor.remove")
# Left out of the history, and masked in the console's echo of the command.
SECRET = ("password ", "set guest.password ", "set prv.key ")

READ_PRESETS = ["ver", "board", "clock", "neighbors", "powersaving", "get name", "get role", "get radio", "get tx",
                "get repeat", "get advert.interval", "get flood.advert.interval", "get flood.max",
                "get lat", "get lon", "get owner.info", "get flood.max.unscoped", "region", "region home",
                "region default"]
ACTION_PRESETS = ["clock sync", "advert", "advert.zerohop", "region save", "clear stats", "reboot"]

STAT_ROWS = [("Battery", "batt"), ("Uptime", "up"), ("Noise floor", "noise"), ("Last RSSI / SNR", "signal"),
             ("TX queue", "queue"), ("Airtime TX / RX", "air"), ("Packets RX / TX", "packets"),
             ("Flood RX / TX", "flood"), ("Direct RX / TX", "direct"), ("Dups flood / direct", "dups"),
             ("Errors / RX errors", "errors"), ("Updated", "at")]


@dataclass(frozen=True)
class Setting:
    """One value the Settings page edits, read and written with `get key` / `set key value`."""
    key: str
    label: str
    hint: str = ""
    lo: float | None = None  # with hi: it's a number in this range
    hi: float | None = None
    whole: bool = True
    choices: tuple[str, ...] = ()
    max_len: int = 0  # the longest text the repeater keeps
    may_be_empty: bool = False

    def problem(self, value: str) -> str | None:
        """Why this value can't be sent, or None. The repeater makes the finer checks."""
        if not value:
            return None if self.may_be_empty else f"{self.label} can't be empty."
        if self.lo is None:
            return None
        try:
            n = int(value) if self.whole else float(value)
        except ValueError:
            return f"{self.label} must be a {'whole ' if self.whole else ''}number."
        return None if self.lo <= n <= self.hi else f"{self.label} must be from {self.lo:g} to {self.hi:g}."


SETTINGS = [
    Setting("name", "Name", max_len=31),
    Setting("repeat", "Repeating", "Off stops this repeater relaying packets", choices=("on", "off")),
    Setting("tx", "TX power (dBm)", "Stay within your radio's and your region's limits", -9, 30),
    Setting("flood.max", "Max flood hops", "Floods that have gone further aren't relayed", 0, 64),
    Setting("flood.max.unscoped", "Max hops, unscoped", "The same for floods with no region scope. "
            "A low value like 3 keeps others' unscoped chatter local (firmware 1.10+)", 0, 64),
    Setting("advert.interval", "Local advert (min)", "0 = off. Otherwise even minutes, up to 240", 0, 240),
    Setting("flood.advert.interval", "Flood advert (hours)", "0 = off, or 3 to 168", 0, 168),
    Setting("lat", "Latitude", "Decimal degrees", -90, 90, whole=False),
    Setting("lon", "Longitude", "Decimal degrees", -180, 180, whole=False),
    Setting("owner.info", "Owner info", "| starts a new line", max_len=119, may_be_empty=True),
]


def needs_confirm(cmd: str) -> bool:
    return cmd.startswith(CONFIRM)


def masked(cmd: str) -> str:
    for s in SECRET:
        if cmd.startswith(s):
            return s + "••••••"
    return cmd


def uptime(secs: int) -> str:
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


def stat_text(s: dict, now: float | None = None) -> dict[str, str]:
    """What the stats grid shows, by STAT_ROWS key."""
    up = s["up_secs"]
    pct = f" ({100 * s['air_secs'] / up:.1f}% TX)" if up else ""
    batt = f"{s['batt_mv'] / 1000:.2f} V ({P.batt_percent(s['batt_mv'])}%)" if s["batt_mv"] else "no reading"
    return {
        "batt": batt, "up": uptime(up), "noise": f"{s['noise_floor']} dBm",
        "signal": f"{s['last_rssi']} dBm / {s['last_snr']:.1f} dB", "queue": str(s["tx_queue"]),
        "air": f"{s['air_secs']} s / {s['rx_air_secs']} s{pct}", "packets": f"{s['recv']} / {s['sent']}",
        "flood": f"{s['recv_flood']} / {s['sent_flood']}", "direct": f"{s['recv_direct']} / {s['sent_direct']}",
        "dups": f"{s['flood_dups']} / {s['direct_dups']}", "errors": f"{s['errors']} / {s['recv_errors']}",
        "at": time.strftime("%H:%M:%S", time.localtime(now)),
    }


@dataclass
class Remote:
    """What this session knows about one repeater."""
    logged_in: bool = False
    is_admin: bool | None = None  # None: an old repeater that doesn't say
    stats: dict[str, str] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)  # as last read from the repeater
    pct: int | None = None  # battery, from the latest status
    console: str = ""
    regions: list[P.Region] | None = None  # as last read; None: not read yet
    region_default: str | None = None  # its default scope, "" for none
    regions_unsaved: bool = False  # changed since the last `region save`


class HistoryLine(QLineEdit):
    """Up and Down walk through earlier commands."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.history: list[str] = []
        self.pos = 0

    def remember(self, cmd: str):
        if not self.history or self.history[-1] != cmd:
            self.history.append(cmd)
        self.pos = len(self.history)

    def event(self, e):
        if e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Up, Qt.Key_Down):
            step = -1 if e.key() == Qt.Key_Up else 1
            self.pos = max(0, min(len(self.history), self.pos + step))
            self.setText(self.history[self.pos] if self.pos < len(self.history) else "")
            return True
        return super().event(e)


class RepeaterPanel(QWidget):
    fill_height = True  # sized to its tile; its tab pages scroll inside instead

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.contact: P.Contact | None = None
        self.remotes: dict[bytes, Remote] = {}
        self.busy = False
        self.link_ready = False
        self._build()
        self.fit_width(1000)
        self._sync()

    # --- layout --------------------------------------------------------------

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        self.title = QLabel("No repeater selected")
        f = self.title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        self.title.setFont(f)
        self.subtitle = QLabel("In Contacts, right-click a repeater and choose Manage repeater.", objectName="dim")
        self.subtitle.setWordWrap(True)
        v.addWidget(self.title)
        v.addWidget(self.subtitle)

        self.password = QLineEdit(placeholderText="Password (blank if you're already its admin)")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setMaxLength(P.PASSWORD_MAX)
        self.password.returnPressed.connect(self.login)
        self.login_btn = QPushButton("Log in", objectName="primary")
        self.login_btn.clicked.connect(self.login)
        self.route_btn = QPushButton("Reset route")
        self.route_btn.setToolTip("Forget the saved route, so the next request finds one by flood")
        self.route_btn.clicked.connect(self.reset_route)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        # One button for both adverts, so the login row fits a narrow tile.
        self.advert_btn = QToolButton(text="Advert ▾", objectName="plain")
        self.advert_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.advert_btn.setPopupMode(QToolButton.InstantPopup)
        self.advert_btn.setToolTip("Make the repeater announce itself")
        am = QMenu(self.advert_btn)
        am.addAction("Zero-hop (nodes that hear it directly)", lambda: self.send_advert(False))
        am.addAction("Flood (whole mesh, uses more airtime)", lambda: self.send_advert(True))
        self.advert_btn.setMenu(am)
        self.top = QGridLayout()  # placed by fit_width: wide, the buttons sit beside the field
        self.top.setColumnStretch(0, 1)
        v.addLayout(self.top)

        box = QWidget()
        grid = self.status_grid = QGridLayout(box)
        self.values: dict[str, QLabel] = {}
        self.status_pairs = []
        for label, key in STAT_ROWS:
            name = QLabel(label, objectName="dim")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.values[key] = value
            self.status_pairs.append((name, value))
        self.status_btn = QPushButton("Refresh")
        self.status_btn.clicked.connect(self.refresh)
        self.gauge = BatteryGauge()
        status_page = QWidget()
        sv = QVBoxLayout(status_page)
        sv.addWidget(box)
        sv.addStretch(1)
        self.narrow = None

        self.console =QPlainTextEdit(readOnly=True)
        self.console.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.console.setMaximumBlockCount(2000)
        console_page = QWidget()
        cv = QVBoxLayout(console_page)
        cv.addWidget(self.console, 1)

        row = QHBoxLayout()
        self.presets = QToolButton(text="Commands ▾", objectName="plain")
        self.presets.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.presets)
        for cmd in READ_PRESETS:
            menu.addAction(cmd, lambda c=cmd: self.run_command(c))
        menu.addSeparator()
        for cmd in ACTION_PRESETS:
            menu.addAction(cmd, lambda c=cmd: self.run_command(c))
        self.presets.setMenu(menu)
        self.command = HistoryLine(placeholderText="CLI command, e.g. get radio")
        self.command.setMaxLength(P.TEXT_MAX)
        self.command.returnPressed.connect(self.send_typed)
        self.send_btn = QPushButton("Run")
        self.send_btn.clicked.connect(self.send_typed)
        row.addWidget(self.presets)
        row.addWidget(self.command, 1)
        row.addWidget(self.send_btn)
        cv.addLayout(row)

        # Everything below the login row is a tab, so the panel fits its tile: pages
        # taller than the room they get scroll inside the tab, not the whole panel.
        self.inner = QTabWidget()
        self.inner.setMinimumHeight(90)
        self.inner.addTab(self._scrolling(status_page), "Status")
        self.inner.addTab(console_page, "Console")
        self.inner.addTab(self._scrolling(self._settings_page()), "Settings")
        self.inner.addTab(self._scrolling(self._regions_page()), "Regions")
        self.inner.addTab(self._history_page(), "History")
        self.live = LivePage(self)
        self.inner.addTab(self.live, "Live")
        v.addWidget(self.inner, 1)

    @staticmethod
    def _scrolling(page: QWidget) -> QScrollArea:
        s = QScrollArea(frameShape=QFrame.NoFrame, widgetResizable=True)
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        s.setWidget(page)
        return s

    def _history_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self.batt_chart = LineChart("Battery", "V", lambda x: f"{x:.2f}")
        self.noise_chart = LineChart("Noise floor", "dBm", lambda x: f"{x:.0f}")
        v.addWidget(self.batt_chart, 1)
        v.addWidget(self.noise_chart, 1)
        return page

    def _load_history(self, c: P.Contact):
        rows = self.win.model.repeater_history(c.prefix)
        self.batt_chart.set_points([(r[0], r[1] / 1000) for r in rows if r[1]])
        self.noise_chart.set_points([(r[0], r[2]) for r in rows])

    def _settings_page(self) -> QWidget:
        page = QWidget()
        grid = self.settings_grid = QGridLayout(page)
        self.fields: dict[str, QLineEdit | QComboBox] = {}
        self.settings_pairs = []
        for s in SETTINGS:
            if s.choices:
                w = QComboBox()
                w.addItems(s.choices)
                w.setCurrentIndex(-1)
            else:
                w = QLineEdit()
                if s.max_len:
                    w.setMaxLength(s.max_len)
            w.setToolTip(s.hint)
            self.fields[s.key] = w
            self.settings_pairs.append((QLabel(s.label, objectName="dim"), w))
        row = self.settings_row = QBoxLayout(QBoxLayout.LeftToRight)
        self.note = QLabel("Read the current values first.", objectName="dim", wordWrap=True)
        self.read_btn = QPushButton("Read from repeater")
        self.read_btn.clicked.connect(self.read_settings)
        self.apply_btn = QPushButton("Apply changes", objectName="primary")
        self.apply_btn.clicked.connect(self.apply_settings)
        row.addWidget(self.note, 1)
        row.addWidget(self.read_btn)
        row.addWidget(self.apply_btn)
        return page

    def _regions_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        intro = QLabel("Regions decide which scoped floods this repeater relays: one scoped to a region it allows floods "
                       "for goes on, others stop here. <b>*</b> is floods with no scope. Changes work straight away but "
                       "are lost on a restart until you save them.", objectName="dim", wordWrap=True)
        v.addWidget(intro)
        self.region_tree = QTreeWidget()
        self.region_tree.setHeaderLabels(["Region", "Floods", ""])
        self.region_tree.setRootIsDecorated(True)
        self.region_tree.itemSelectionChanged.connect(self._sync)
        self.region_tree.setMinimumHeight(170)
        v.addWidget(self.region_tree, 1)
        self.region_info = QLabel("", objectName="dim", wordWrap=True)
        v.addWidget(self.region_info)
        row = QHBoxLayout()
        self.region_read_btn = QPushButton("Read")
        self.region_read_btn.setToolTip("Read the region list and default scope from the repeater")
        self.region_read_btn.clicked.connect(self.read_regions)
        self.region_add_btn = QPushButton("Add…")
        self.region_add_btn.clicked.connect(self.add_region)
        self.region_flood_btn = QPushButton("Deny floods")
        self.region_flood_btn.clicked.connect(self.toggle_region_flood)
        self.region_more = QToolButton(text="More ▾", objectName="plain")
        self.region_more.setPopupMode(QToolButton.InstantPopup)
        rm = QMenu(self.region_more)
        self.region_home_act = rm.addAction("Make it the home region", lambda: self._region_cmd("home"))
        self.region_def_act = rm.addAction("Make it the default scope", lambda: self._region_cmd("default"))
        self.region_nodef_act = rm.addAction("Clear the default scope", self.clear_region_default)
        rm.addSeparator()
        self.region_rm_act = rm.addAction("Remove region", self.remove_region)
        self.region_more.setMenu(rm)
        self.region_save_btn = QPushButton("Save", objectName="primary")
        self.region_save_btn.setToolTip("region save: keep the regions after a restart")
        self.region_save_btn.clicked.connect(self.save_regions)
        for b in (self.region_read_btn, self.region_add_btn, self.region_flood_btn, self.region_more):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.region_save_btn)
        v.addLayout(row)
        self.region_note = QLabel("Read the regions first.", objectName="dim", wordWrap=True)
        v.addWidget(self.region_note)
        return page

    def fit_width(self, w: int):
        """Called by its panel with the width it has: two columns of values, or one."""
        narrow = w < NARROW
        if narrow == self.narrow:
            return
        self.narrow = narrow
        cols = 1 if narrow else 2
        t = self.top
        for x in (self.password, self.login_btn, self.route_btn, self.state, self.advert_btn):
            t.removeWidget(x)
        if narrow:  # the field, then its buttons, then the status text
            t.addWidget(self.password, 0, 0, 1, 4)
            t.addWidget(self.login_btn, 1, 1)
            t.addWidget(self.route_btn, 1, 2)
            t.addWidget(self.advert_btn, 1, 3)
            t.addWidget(self.state, 2, 0, 1, 4)
        else:
            t.addWidget(self.password, 0, 0)
            t.addWidget(self.login_btn, 0, 1)
            t.addWidget(self.route_btn, 0, 2)
            t.addWidget(self.advert_btn, 0, 3)
            t.addWidget(self.state, 1, 0, 1, 4)
        self.settings_row.setDirection(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        g = self.status_grid
        g.removeWidget(self.gauge)
        g.removeWidget(self.status_btn)
        rows = place_pairs(g, self.status_pairs, cols)
        if narrow:  # the gauge goes under the values, beside Refresh
            g.addWidget(self.gauge, rows, 0, Qt.AlignLeft)
            g.addWidget(self.status_btn, rows, 1, Qt.AlignRight | Qt.AlignBottom)
        else:  # its own column on the right, Refresh under it
            g.addWidget(self.gauge, 0, 4, rows - 1, 1, Qt.AlignCenter)
            g.addWidget(self.status_btn, rows - 1, 4, Qt.AlignCenter)
        sg = self.settings_grid
        sg.removeItem(self.settings_row)
        for r in range(len(SETTINGS) + 3):
            sg.setRowStretch(r, 0)
        rows = place_pairs(sg, self.settings_pairs, cols)
        sg.setRowStretch(rows, 1)
        sg.addLayout(self.settings_row, rows + 1, 0, 1, 4)

    # --- state ---------------------------------------------------------------

    @property
    def remote(self) -> Remote | None:
        return self.remotes.setdefault(self.contact.prefix, Remote()) if self.contact else None

    def _sync(self):
        r = self.remote
        usable = self.link_ready and r is not None and not self.busy
        self.password.setEnabled(usable)
        self.login_btn.setEnabled(usable)
        self.route_btn.setEnabled(usable)
        self.status_btn.setEnabled(usable and r.logged_in)
        cli = usable and r.logged_in and r.is_admin is not False
        self.command.setEnabled(cli)
        self.send_btn.setEnabled(cli)
        self.presets.setEnabled(cli)
        self.advert_btn.setEnabled(cli)
        self.read_btn.setEnabled(cli)
        self.apply_btn.setEnabled(cli and bool(r.settings))
        self.live.set_usable(self.link_ready and r is not None and r.logged_in and r.is_admin is not False)
        for key, w in self.fields.items():
            w.setEnabled(cli and key in r.settings)
        sel = self._region_selected()
        have = cli and r.regions is not None
        self.region_read_btn.setEnabled(cli)
        self.region_add_btn.setEnabled(have)
        self.region_flood_btn.setEnabled(have and sel is not None)
        if sel is not None:
            self.region_flood_btn.setText("Deny floods" if sel.flood else "Allow floods")
        self.region_more.setEnabled(have)
        self.region_home_act.setEnabled(sel is not None and sel.name != "*" and not sel.home)
        self.region_def_act.setEnabled(sel is not None and sel.name != "*" and sel.name != (r.region_default if r else None))
        self.region_nodef_act.setEnabled(bool(r and r.region_default))
        self.region_rm_act.setEnabled(sel is not None and sel.name != "*")
        self.region_save_btn.setEnabled(have)

    def _showing(self, c: P.Contact) -> bool:
        return self.contact is not None and self.contact.prefix == c.prefix

    def _say(self, text: str, bad: bool = False):
        self.state.setStyleSheet(f"color:{theme.current.danger}" if bad else "")
        self.state.setText(text)

    def _print(self, text: str):
        r = self.remote
        r.console += text + "\n"
        self.console.appendPlainText(text)

    def _route(self, c: P.Contact) -> str:
        if c.hops < 0:
            return "no saved route, will flood"
        return "direct neighbour" if c.hops == 0 else f"{c.hops} hops via " + " → ".join(h.hex() for h in c.path.hops())

    def _show(self):
        c, r = self.contact, self.remote
        self.title.setText(c.name or c.key[:3].hex())
        self.subtitle.setText(f"{c.key[:6].hex()} · {self._route(c)}")
        self.console.setPlainText(r.console.rstrip("\n"))
        self.console.moveCursor(self.console.textCursor().MoveOperation.End)
        for key, label in self.values.items():
            label.setText(r.stats.get(key, "—"))
        for s in SETTINGS:
            self._fill(s)
        self._fill_regions()
        self.gauge.set_percent(r.pct)
        self._load_history(c)
        self._login_state()
        self.live.show_contact(c)
        self._sync()

    def live_changed(self, running: bool):
        """A dot on the tabs: polling goes on from any screen, and shouldn't go unnoticed."""
        dot = " ●" if running else ""
        self.inner.setTabText(self.inner.indexOf(self.live), "Live" + dot)
        self.win.center.setTabText(self.win.center.indexOf(self), "Repeater" + dot)
        self._sync()

    def _login_state(self):
        r = self.remote
        if not r.logged_in:
            self._say("Not logged in.")
        elif r.is_admin is False:
            self._say("Logged in as guest. Status works, but the repeater only takes CLI commands from an admin.")
        else:
            self._say("Logged in as admin.")

    def open(self, c: P.Contact):
        self.contact = c
        self._show()
        self.password.setFocus()

    def link_changed(self, state: str):
        self.link_ready = state == "ready"
        if not self.link_ready:
            self.live.stop("Stopped: the link to your node dropped.")
        if state == "disconnected":
            # The next connect may well be a different companion node.
            consoles = {k: v.console for k, v in self.remotes.items()}
            self.remotes = {k: Remote(console=v) for k, v in consoles.items()}
            if self.contact:
                self._show()
        self._sync()

    def late_reply(self, m: P.TextMessage):
        r = self.remotes.get(m.key)
        if r is None:
            return
        line = f"(late reply) {m.text}"
        if self.contact and self.contact.prefix == m.key:
            self._print(line)
        else:
            r.console += line + "\n"

    # --- actions -------------------------------------------------------------

    def _task(self, coro):
        """One request at a time, with the controls off until it ends."""
        self.busy = True  # now, not when the task first runs: a second click could get in between
        self._sync()

        async def go():
            try:
                await coro
            except Exception as e:
                log.exception("repeater request")
                self._say(f"Error: {e}", True)
            finally:
                self.busy = False
                self._sync()
        return asyncio.ensure_future(go())

    def _failure(self, f: P.Frame | None) -> str:
        if f is None:
            return "No reply. The repeater may be out of range, or its saved route stale: try Reset route."
        return f"The node refused: {P.ERR_NAMES.get(f.err, f.err)}."

    def login(self):
        if not self.login_btn.isEnabled():
            return
        c, password = self.contact, self.password.text()
        self.password.clear()
        self._say("Logging in…")
        self._task(self._login(c, password))

    async def _login(self, c: P.Contact, password: str):
        f = await self.win.session.login(c.key, password)
        r = self.remotes.setdefault(c.prefix, Remote())
        if f is None or f.kind == "err":
            self._report(c, self._failure(f), True)
        elif f.kind == "login_fail":
            r.logged_in = False
            self._report(c, "Wrong password, or a blank login and this node isn't on its admin list.", True)
        else:
            r.logged_in, r.is_admin = True, f.is_admin
            if self._showing(c):
                self._login_state()
                self._sync()
                await self._status(c)

    def _report(self, c: P.Contact, text: str, bad: bool = False):
        if self._showing(c):
            self._say(text, bad)

    def refresh(self):
        c = self.contact
        self._say("Asking for status…")
        self._task(self._status(c))

    async def _status(self, c: P.Contact):
        f = await self.win.session.repeater_status(c.key)
        if f is None or f.kind == "err":
            self._report(c, self._failure(f), True)
            return
        self.apply_status(c, f)

    def apply_status(self, c: P.Contact, f: P.Frame, record: bool = True):
        """Shows a status reply. Live polls only sometimes record one, or they'd swamp the History."""
        r = self.remotes.setdefault(c.prefix, Remote())
        r.stats = stat_text(f.stats)
        r.pct = P.batt_percent(f.stats["batt_mv"]) if f.stats["batt_mv"] else None
        if record:
            self.win.model.record_repeater(c.prefix, f.stats)
        if self._showing(c):
            self.gauge.set_percent(r.pct)
            self._load_history(c)
            for key, label in self.values.items():
                label.setText(r.stats.get(key, "—"))
            self._login_state()

    def reset_route(self):
        c = self.contact
        self._say("Resetting route…")
        self._task(self._reset(c))

    async def _reset(self, c: P.Contact):
        f = await self.win.session.reset_path(c.key)
        self.contact = self.win.model.contacts.get(c.prefix, c) if self._showing(c) else self.contact
        if self._showing(c):
            self._show()
            self._say("Route reset." if f and f.kind != "err" else "Couldn't reset the route.",
                      not f or f.kind == "err")

    def send_typed(self):
        if self.send_btn.isEnabled() and self.command.text().strip():
            if self.run_command(self.command.text().strip()):
                self.command.clear()

    def run_command(self, cmd: str) -> bool:
        if len(cmd.encode()) > P.TEXT_MAX:
            self._say(f"Commands can be at most {P.TEXT_MAX} bytes.", True)
            return False
        if needs_confirm(cmd) and QMessageBox.question(
                self, "Run on the repeater?",
                f"Run “{masked(cmd)}” on {self.contact.name}?\n\n"
                "A remote repeater may be hard to reach if this goes wrong.") != QMessageBox.Yes:
            return False
        if masked(cmd) == cmd:
            self.command.remember(cmd)
        self._task(self._run(self.contact, cmd))
        return True

    def send_advert(self, flood: bool):
        c, kind = self.contact, "Flood" if flood else "Zero-hop"
        self._say(f"Asking the repeater to send a {kind.lower()} advert…")
        self._task(self._advert(c, "advert" if flood else "advert.zerohop", kind))

    async def _advert(self, c: P.Contact, cmd: str, kind: str):
        self._log(c, f"> {cmd}")
        f = await self.win.session.cli(c.key, cmd)
        if f is None or f.kind == "err":
            self._log(c, "(no reply: it may or may not have sent one)")
            self._report(c, f"No reply, so the advert may or may not have gone out. {self._failure(f)}", True)
            return
        self._log(c, f.msg.text)
        ok = f.msg.text.startswith("OK")
        if ok:
            self.win.toast(f"{c.name}: {kind.lower()} advert sent")
        self._report(c, f"{kind} advert sent." if ok else f"The repeater said: {f.msg.text}", not ok)

    def _log(self, c: P.Contact, line: str):
        if self._showing(c):
            self._print(line)
        else:
            self.remotes.setdefault(c.prefix, Remote()).console += line + "\n"

    async def _run(self, c: P.Contact, cmd: str):
        self._log(c, f"> {masked(cmd)}")
        f = await self.win.session.cli(c.key, cmd)
        self._log(c, (f.msg.text if f and f.kind == "message"
                      else "(no reply: it may or may not have run)" if f is None else f"(failed: {self._failure(f)})"))

    # --- settings ------------------------------------------------------------

    def _value(self, s: Setting) -> str:
        w = self.fields[s.key]
        return w.currentText() if s.choices else w.text().strip()

    def _fill(self, s: Setting):
        """Puts the last value read from the repeater in its field."""
        w, v = self.fields[s.key], self.remote.settings.get(s.key, "")
        if s.choices:
            w.setCurrentIndex(w.findText(v))
        else:
            w.setText(v)

    async def _read_one(self, c: P.Contact, s: Setting) -> bool:
        """False if the repeater didn't answer. A key it doesn't know just stays blank."""
        f = await self.win.session.cli(c.key, f"get {s.key}")
        if f is None or f.kind == "err":
            return False
        r = self.remotes.setdefault(c.prefix, Remote())
        if f.msg.text.startswith("> ") or f.msg.text == ">":
            r.settings[s.key] = f.msg.text[2:]
        else:
            r.settings.pop(s.key, None)
        if self._showing(c):
            self._fill(s)
        return True

    def read_settings(self):
        self._task(self._read_all(self.contact))

    async def _read_all(self, c: P.Contact):
        for i, s in enumerate(SETTINGS, 1):
            self._report_note(c, f"Reading {i} of {len(SETTINGS)}: {s.label}…")
            if not await self._read_one(c, s):
                # Replies carry no tag, so a late one could land on the next field. Stop here.
                self._report_note(c, f"No reply reading {s.label}. Stopped; try again.", True)
                return
        self._report_note(c, "Up to date. Change what you like, then Apply.")

    def _report_note(self, c: P.Contact, text: str, bad: bool = False):
        if self._showing(c):
            self.note.setStyleSheet(f"color:{theme.current.danger}" if bad else "")
            self.note.setText(text)

    def apply_settings(self):
        r = self.remote
        changes = [(s, r.settings[s.key], self._value(s)) for s in SETTINGS
                   if s.key in r.settings and self._value(s) != r.settings[s.key]]
        if not changes:
            self._report_note(self.contact, "Nothing has changed.")
            return
        for s, _old, new in changes:
            if problem := s.problem(new):
                self._report_note(self.contact, problem, True)
                return
        lines = "\n".join(f"{s.label}: {old or '(empty)'} → {new or '(empty)'}" for s, old, new in changes)
        if QMessageBox.question(self, "Apply to the repeater?",
                                f"Change these on {self.contact.name}?\n\n{lines}") == QMessageBox.Yes:
            self._task(self._apply(self.contact, changes))

    async def _apply(self, c: P.Contact, changes: list):
        refused = []
        for s, _old, new in changes:
            cmd = f"set {s.key} {new}"
            self._report_note(c, f"Setting {s.label}…")
            self._log(c, f"> {cmd}")
            f = await self.win.session.cli(c.key, cmd)
            if f is None or f.kind == "err":
                self._log(c, "(no reply: it may or may not have applied)")
                self._report_note(c, f"No reply setting {s.label}: it may or may not have applied. "
                                  "Stopped; read again to check.", True)
                return
            self._log(c, f.msg.text)
            if not f.msg.text.startswith("OK"):
                refused.append(f"{s.label}: {f.msg.text}")
            elif not await self._read_one(c, s):  # shows what it actually stored
                self._report_note(c, f"{s.label} was set, but reading it back got no reply.", True)
                return
        self._report_note(c, "  ".join(refused) if refused else "Applied.", bool(refused))

    # --- regions ---------------------------------------------------------------

    def _region_selected(self) -> P.Region | None:
        it = self.region_tree.currentItem() if self.region_tree.selectedItems() else None
        return it.data(0, Qt.UserRole) if it else None

    def _fill_regions(self):
        r = self.remote
        keep = self._region_selected()
        self.region_tree.clear()
        if r is None or r.regions is None:
            self.region_info.setText("")
            return
        items: dict[str, QTreeWidgetItem] = {}
        for reg in r.regions:
            marks = []
            if reg.home:
                marks.append("home")
            if reg.name == r.region_default:
                marks.append("default scope")
            label = "* (no scope)" if reg.name == "*" else reg.name
            it = QTreeWidgetItem([label, "allowed" if reg.flood else "denied", ", ".join(marks)])
            it.setData(0, Qt.UserRole, reg)
            if not reg.flood:
                it.setForeground(1, self.palette().placeholderText())
            parent = items.get(reg.parent) if reg.parent else None
            (parent.addChild(it) if parent else self.region_tree.addTopLevelItem(it))
            items[reg.name] = it
            if keep and keep.name == reg.name:
                self.region_tree.setCurrentItem(it)
        self.region_tree.expandAll()
        self.region_tree.resizeColumnToContents(0)
        home = next((x.name for x in r.regions if x.home), None)
        self.region_info.setText(f"Home region: {home or 'none'} · Default scope: "
                                 f"{r.region_default or 'none'}" + ("  ·  <b>unsaved changes</b>" if r.regions_unsaved else ""))
        self.region_info.setTextFormat(Qt.RichText)

    def _region_note(self, c: P.Contact, text: str, bad: bool = False):
        if self._showing(c):
            self.region_note.setStyleSheet(f"color:{theme.current.danger}" if bad else "")
            self.region_note.setText(text)

    async def _region_run(self, c: P.Contact, cmd: str) -> str | None:
        """Runs a region command, logged in the console. Its reply, or None."""
        self._log(c, f"> {cmd}")
        f = await self.win.session.cli(c.key, cmd)
        if f is None or f.kind != "message":
            self._log(c, "(no reply)" if f is None else f"(failed: {self._failure(f)})")
            self._region_note(c, f"No reply to “{cmd}”. {self._failure(f)}", True)
            return None
        self._log(c, f.msg.text)
        return f.msg.text

    async def _read_regions(self, c: P.Contact) -> bool:
        self._region_note(c, "Reading regions…")
        tree = await self._region_run(c, "region")
        if tree is None:
            return False
        regions = P.parse_regions(tree)
        if not regions:
            self._region_note(c, f"This repeater didn't list any regions (it said “{tree[:60]}”). "
                              "It may need firmware 1.10 or later.", True)
            return False
        d = await self._region_run(c, "region default")
        r = self.remotes.setdefault(c.prefix, Remote())
        r.regions = regions
        if d is not None and "default scope is" in d:
            name = d.rsplit(" ", 1)[-1].strip()
            r.region_default = "" if name == "<null>" else name.lstrip("#")
        if self._showing(c):
            self._fill_regions()
        full = len(tree) < 150  # replies stop at 160 bytes
        self._region_note(c, "Up to date." if full else
                          "Up to date, but the list may be cut short: replies hold 160 characters. "
                          "Use the console's region get <name> for the rest.")
        return True

    def read_regions(self):
        self._task(self._read_regions(self.contact))

    async def _change_regions(self, c: P.Contact, cmd: str, done: str):
        reply = await self._region_run(c, cmd)
        if reply is None:
            return
        bad = reply.startswith("Err")
        if not bad:
            self.remotes.setdefault(c.prefix, Remote()).regions_unsaved = True
        if await self._read_regions(c):
            self._region_note(c, f"The repeater said: {reply}" if bad else done + " Save to keep it after a restart.", bad)

    def add_region(self):
        r, sel = self.remote, self._region_selected()
        d = QDialog(self)
        d.setWindowTitle("Add region")
        f = QFormLayout(d)
        name = QLineEdit()
        name.setPlaceholderText("e.g. boston")
        name.setMaxLength(P.SCOPE_NAME_MAX - 1)
        parent = QComboBox()
        for reg in r.regions or []:
            parent.addItem("* (top level)" if reg.name == "*" else reg.name, reg.name)
        if sel:
            parent.setCurrentIndex(max(parent.findData(sel.name), 0))
        err = QLabel("", wordWrap=True)
        err.setStyleSheet(f"color:{theme.current.danger}")
        f.addRow("Name", name)
        f.addRow("Inside", parent)
        f.addRow(QLabel("Floods scoped to it are allowed. A region inside another counts as part of it.",
                        objectName="dim", wordWrap=True))
        f.addRow(err)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        f.addRow(bb)
        bb.rejected.connect(d.reject)

        def ok():
            if problem := P.region_problem(name.text()):
                err.setText(problem)
                return
            if any(x.name == P.region_name(name.text()) for x in r.regions or []):
                err.setText("It already has that region.")
                return
            d.accept()
        bb.accepted.connect(ok)
        if d.exec():
            n, p = P.region_name(name.text()), parent.currentData()
            cmd = f"region put {n}" + (f" {p}" if p and p != "*" else "")
            self._task(self._change_regions(self.contact, cmd, f"Added {n}."))

    def toggle_region_flood(self):
        sel = self._region_selected()
        if sel is None:
            return
        if sel.flood and sel.name == "*" and QMessageBox.question(
                self, "Deny unscoped floods?",
                f"Stop {self.contact.name} relaying floods that have no scope? Most nodes send those today, so "
                "this cuts off anyone who hasn't set a scope. (Max hops, unscoped in Settings is a gentler way.)"
        ) != QMessageBox.Yes:
            return
        op = "denyf" if sel.flood else "allowf"
        self._task(self._change_regions(self.contact, f"region {op} {sel.name}",
                                        f"{'Denied' if sel.flood else 'Allowed'} floods for {sel.name}."))

    def _region_cmd(self, what: str):
        sel = self._region_selected()
        if sel is None:
            return
        done = f"{sel.name} is now the home region." if what == "home" else f"{sel.name} is now the default scope."
        self._task(self._change_regions(self.contact, f"region {what} {sel.name}", done))

    def clear_region_default(self):
        self._task(self._change_regions(self.contact, "region default <null>", "Cleared the default scope."))

    def remove_region(self):
        sel = self._region_selected()
        if sel is None:
            return
        if QMessageBox.question(self, "Remove region", f"Remove {sel.name} from {self.contact.name}?") != QMessageBox.Yes:
            return
        self._task(self._change_regions(self.contact, f"region remove {sel.name}", f"Removed {sel.name}."))

    def save_regions(self):
        self._task(self._save_regions(self.contact))

    async def _save_regions(self, c: P.Contact):
        reply = await self._region_run(c, "region save")
        if reply is None:
            return
        ok = reply.startswith("OK")
        if ok:
            self.remotes.setdefault(c.prefix, Remote()).regions_unsaved = False
            if self._showing(c):
                self._fill_regions()
        self._region_note(c, "Saved on the repeater." if ok else f"The repeater said: {reply}", not ok)
