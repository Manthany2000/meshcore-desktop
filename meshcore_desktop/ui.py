"""The window: chats and contacts on the left, the conversation in the middle,
details of the selected message on the right."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import secrets
import time

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QLinearGradient, QPainter, QPalette, QTextDocument, QTextOption, QFontMetrics
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter, QStyle, QStyledItemDelegate,
                               QTabWidget, QTextBrowser, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import protocol as P
from . import theme
from .admin import RepeaterPanel
from .desktop import notify, settings, short_date
from .pairing import PairingError
from .link import BleTransport, BondError, Session, scan
from .model import (DELIVERED, FAILED, INCOMING, NO_ACK, SENDING, SENT, WAITING, Message, Model, chan_conv, dm_conv)

log = logging.getLogger("ui")

TYPE_NAMES = {P.ADV_CHAT: "Chat", P.ADV_REPEATER: "Repeater", P.ADV_ROOM: "Room", P.ADV_SENSOR: "Sensor"}
MENTION = re.compile(r"@\[([^\]]{1,32})\]")
SIM = "sim"


def ago(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 60:
        return "now"
    if sec < 3600:
        return f"{sec // 60} min"
    if sec < 86400:
        return f"{sec // 3600} h"
    return f"{sec // 86400} d"


def when(t: float, full: bool = False) -> str:
    lt, now = time.localtime(t), time.localtime()
    if lt[:3] == now[:3] and not full:
        return time.strftime("%H:%M", lt)
    return f"{short_date(lt)} {time.strftime('%H:%M', lt)}"


def name_color(name: str, dark: bool) -> str:
    h = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 360
    c = QColor.fromHsl(h, 150, 170 if dark else 90)
    return c.name()


def text_limit(model: Model, conv: str) -> int:
    """The node cuts a channel message to 160 bytes including "name: "."""
    if conv.startswith("ch:"):
        name = model.self_info.name if model.self_info else ""
        return P.TEXT_MAX - len(name.encode()) - 2
    return P.TEXT_MAX


# --- the message list ----------------------------------------------------------

class MessageList(QAbstractListModel):
    def __init__(self, model: Model):
        super().__init__()
        self.model = model
        self.conv = ""
        self.msgs: list[Message] = []

    def set_conv(self, conv: str):
        self.beginResetModel()
        self.conv = conv
        self.msgs = self.model.conversation(conv)[-1000:]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.msgs)

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        m = self.msgs[idx.row()]
        if role == Qt.UserRole:
            return m
        if role == Qt.DisplayRole:
            return m.text
        return None

    def added(self, m: Message):
        if m.conv != self.conv:
            return
        # Backlog can arrive out of order: keep the list sorted by time.
        row = len(self.msgs)
        while row > 0 and self.msgs[row - 1].at > m.at:
            row -= 1
        self.beginInsertRows(QModelIndex(), row, row)
        self.msgs.insert(row, m)
        self.endInsertRows()

    def changed(self, m: Message):
        for i, x in enumerate(self.msgs):
            if x is m:
                ix = self.index(i)
                self.dataChanged.emit(ix, ix)
                return

    def reload(self):
        self.set_conv(self.conv)


class Bubble(QStyledItemDelegate):
    PAD, GAP, RADIUS = 11, 4, 13

    def __init__(self, win: "MainWindow", view: QListView):
        super().__init__(view)
        self.win = win
        self.view = view
        self.cache: dict = {}

    def _dark(self):
        return self.view.palette().color(QPalette.Base).lightness() < 128

    def _html(self, m: Message) -> str:
        dark = self._dark()
        dim = theme.current.dim
        me = self.win.model.self_info.name if self.win.model.self_info else ""
        parts = []
        if not m.outgoing and m.is_channel:
            parts.append(f'<div style="font-weight:600; color:{name_color(m.sender, dark)}">{html.escape(m.sender)}</div>')

        # @[Name] mentions in the name's colour; ours stand out.
        out, at = [], 0
        for mo in MENTION.finditer(m.text):
            out.append(html.escape(m.text[at:mo.start()]))
            who = mo.group(1)
            col = theme.current.warn if who == me else name_color(who, dark)
            out.append(f'<b style="color:{col}">@{html.escape(who)}</b>')
            at = mo.end()
        out.append(html.escape(m.text[at:]))
        parts.append(f"<div>{''.join(out).replace(chr(10), '<br>')}</div>")

        foot = [when(m.at)]
        if not m.outgoing:
            foot.append("direct" if m.hops < 0 else ("0 hops" if m.hops == 0 else f"{m.hops} hop{'s' * (m.hops != 1)}"))
            if m.snr is not None:
                foot.append(f"SNR {m.snr:.1f}")
        if m.repeats:
            foot.append(f"↻ {m.repeats} repeat{'s' * (m.repeats != 1)}")
        st = {SENDING: "… sending", SENT: "✓ sent", WAITING: "⏳ waiting for ack",
              DELIVERED: f"✓✓ delivered{f' {m.trip_ms / 1000:.1f}s' if m.trip_ms else ''}",
              NO_ACK: "✗ no ack", FAILED: "✗ failed"}.get(m.status, "")
        if st:
            t = theme.current
            col = {DELIVERED: t.ok, NO_ACK: t.danger, FAILED: t.danger}.get(m.status, dim)
            foot.append(f'<span style="color:{col}">{st}</span>')
        parts.append(f'<div style="color:{dim}; font-size:small">{" · ".join(foot)}</div>')
        return "".join(parts)

    def _doc(self, m: Message, maxw: int) -> QTextDocument:
        h = self._html(m)
        key = (m.id, maxw, h)
        doc = self.cache.get(key)
        if doc:
            return doc
        doc = QTextDocument()
        doc.setDefaultFont(self.view.font())
        doc.setDocumentMargin(0)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(opt)
        doc.setHtml(h)
        doc.setTextWidth(maxw)
        doc.setTextWidth(min(maxw, doc.idealWidth() + 1))
        if len(self.cache) > 3000:
            self.cache.clear()
        self.cache[key] = doc
        return doc

    AVATAR = 30

    def _has_avatar(self, m: Message) -> bool:
        return m.is_channel and not m.outgoing

    def _maxw(self):
        return max(160, int(self.view.viewport().width() * 0.68) - 2 * self.PAD)

    def sizeHint(self, option, idx):
        m = idx.data(Qt.UserRole)
        doc = self._doc(m, self._maxw())
        return QSize(self.view.viewport().width() - 4, int(doc.size().height()) + 2 * self.PAD + 2 * self.GAP)

    def paint(self, p: QPainter, option, idx):
        m: Message = idx.data(Qt.UserRole)
        doc = self._doc(m, self._maxw())
        w, h = doc.size().width() + 2 * self.PAD, doc.size().height() + 2 * self.PAD
        r = option.rect
        x = r.right() - w - 16 if m.outgoing else r.left() + 16 + (self.AVATAR + 8 if self._has_avatar(m) else 0)
        rect = QRectF(x, r.top() + self.GAP, w, h)
        t = theme.current
        bg = QColor(t.accent_soft if m.outgoing else t.surface)
        me = self.win.model.self_info.name if self.win.model.self_info else None
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if self._has_avatar(m):
            theme.draw_avatar(p, QRectF(r.left() + 16, rect.top(), self.AVATAR, self.AVATAR), m.sender)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70 if t.dark else 26))  # a soft edge underneath
        p.drawRoundedRect(rect.translated(0, 1.5), self.RADIUS, self.RADIUS)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            p.setPen(QColor(t.accent))
        elif me and not m.outgoing and f"@[{me}]" in m.text:
            p.setPen(QColor(t.warn))
        elif m.outgoing or t.dark:
            p.setPen(Qt.NoPen)
        else:
            p.setPen(QColor(t.border))  # white on near-white needs an edge
        if m.outgoing:
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0, bg)
            grad.setColorAt(1, bg.lighter(125) if t.dark else bg.darker(107))
            p.setBrush(grad)
        else:
            p.setBrush(bg)
        p.drawRoundedRect(rect, self.RADIUS, self.RADIUS)
        p.translate(rect.left() + self.PAD, rect.top() + self.PAD)
        doc.drawContents(p)
        p.restore()


class ChatRow(QStyledItemDelegate):
    """A row in the chat list: avatar, name and unread badge, or a section heading."""

    def sizeHint(self, option, idx):
        return QSize(option.rect.width(), 46 if idx.data(Qt.UserRole) else 30)

    def paint(self, p: QPainter, option, idx):
        conv, title, unread = idx.data(Qt.UserRole), idx.data(Qt.DisplayRole), idx.data(Qt.UserRole + 1) or 0
        t = theme.current
        r = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(option.font)
        if not conv:
            f.setBold(True)
            f.setPixelSize(11)
            p.setFont(f)
            p.setPen(QColor(t.dim))
            p.drawText(r.adjusted(12, 8, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, title)
            p.restore()
            return
        row = r.adjusted(2, 2, -2, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(QColor(t.accent))
            p.setBrush(QColor(t.accent_soft))
            p.drawRoundedRect(row, 11, 11)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.raised))
            p.drawRoundedRect(row, 11, 11)
        d = 30
        theme.draw_avatar(p, QRectF(row.left() + 7, row.center().y() - d / 2, d, d), title, conv.startswith("ch:"))
        right = row.right() - 10
        if unread:
            label = str(unread) if unread < 100 else "99+"
            bf = QFont(f)
            bf.setBold(True)
            bf.setPixelSize(11)
            w = max(20, QFontMetrics(bf).horizontalAdvance(label) + 12)
            badge = QRectF(right - w, row.center().y() - 10, w, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(theme.accent_gradient(badge))
            p.drawRoundedRect(badge, 10, 10)
            p.setFont(bf)
            p.setPen(QColor(t.on_accent))
            p.drawText(badge, Qt.AlignCenter, label)
            right = badge.left() - 6
        f.setBold(bool(unread))
        p.setFont(f)
        p.setPen(QColor(t.text))
        text = QRectF(row.left() + 7 + d + 9, row.top(), right - (row.left() + 16 + d), row.height())
        p.drawText(text, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(f).elidedText(title, Qt.ElideRight, int(text.width())))
        p.restore()


# --- dialogs ---------------------------------------------------------------------

class ConnectDialog(QDialog):
    def __init__(self, parent, last: str):
        super().__init__(parent)
        self.setWindowTitle("Connect to a MeshCore node")
        self.resize(420, 320)
        self.choice = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Nodes in range running MeshCore companion firmware.\n"
                             "The first time, you're asked for the PIN shown on the node's screen."))
        self.list = QListWidget()
        lay.addWidget(self.list)
        if last and last != SIM:
            self._add(last, f"Last used ({last})")
        self._add(SIM, "Simulated node (no radio)")
        row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(lambda: asyncio.ensure_future(self.do_scan()))
        row.addWidget(self.scan_btn)
        self.status = QLabel("")
        row.addWidget(self.status, 1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Connect")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _i: self._ok())
        lay.addWidget(bb)
        QTimer.singleShot(0, lambda: asyncio.ensure_future(self.do_scan()))

    def _add(self, addr, label):
        it = QListWidgetItem(label)
        it.setData(Qt.UserRole, addr)
        self.list.addItem(it)
        if self.list.count() == 1:
            self.list.setCurrentRow(0)

    async def do_scan(self):
        self.scan_btn.setEnabled(False)
        self.status.setText("Scanning…")
        try:
            found = await scan()
        except Exception as e:
            self.status.setText(f"Scan failed: {e}")
            self.scan_btn.setEnabled(True)
            return
        have = {self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())}
        for addr, name, rssi in found:
            if addr in have:
                for i in range(self.list.count()):
                    if self.list.item(i).data(Qt.UserRole) == addr:
                        self.list.item(i).setText(f"{name}  ({addr}, {rssi} dBm)")
            else:
                self._add(addr, f"{name}  ({addr}, {rssi} dBm)")
        self.status.setText(f"{len(found)} found" if found else "None found. Is a phone connected to it?")
        self.scan_btn.setEnabled(True)

    def _ok(self):
        it = self.list.currentItem()
        if it:
            self.choice = it.data(Qt.UserRole)
            self.accept()


class PinDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Bluetooth PIN")
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Enter the 6-digit PIN shown on the node's screen."))
        self.edit = QLineEdit()
        self.edit.setInputMask("999999")
        self.edit.setAlignment(Qt.AlignCenter)
        f = self.edit.font()
        f.setPointSize(f.pointSize() + 8)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 6)
        self.edit.setFont(f)
        lay.addWidget(self.edit)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Pair")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)
        self.edit.textChanged.connect(lambda _t: self.bb.button(QDialogButtonBox.Ok).setEnabled(self.pin() is not None))
        self.bb.button(QDialogButtonBox.Ok).setEnabled(False)
        self.edit.setFocus()

    def pin(self) -> int | None:
        t = self.edit.text().strip()
        return int(t) if len(t) == 6 and t.isdigit() else None


class ChannelDialog(QDialog):
    """A '#name' channel's key comes from its name, so anyone who types the
    same name joins it. A private one needs its key shared."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add channel")
        f = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText("#hashtag, or a private channel's name")
        self.key = QLineEdit()
        self.key.setPlaceholderText("32 hex digits; blank = derived from #name, or random")
        f.addRow("Name", self.name)
        f.addRow("Key", self.key)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.result_ = None

    def _ok(self):
        name = self.name.text().strip()
        k = self.key.text().strip().replace(" ", "")
        if not name:
            return
        try:
            if k:
                secret = bytes.fromhex(k)
                assert len(secret) == 16
            elif name.startswith("#"):
                secret = hashlib.sha256(name.encode()).digest()[:16]
            else:
                secret = secrets.token_bytes(16)
        except Exception:
            QMessageBox.warning(self, "Key", "The key must be 32 hex digits.")
            return
        self.result_ = (name, secret)
        self.accept()


# --- main window -------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, model: Model, autoconnect: str | None = None):
        super().__init__()
        self.model = model
        self.session: Session | None = None
        self.settings = settings()
        self.address = autoconnect or self.settings.value("address", "")
        self.want_connected = False
        self.conv = ""
        self.reply_to: Message | None = None
        self.setWindowTitle("MeshCore Desktop")
        self.resize(1250, 780)
        self._build()
        model.listeners.append(self._on_model)
        self._refresh_timer = QTimer(self, singleShot=True, interval=250, timeout=self._refresh_lists)
        self._age_timer = QTimer(self, interval=30000, timeout=self._refresh_contacts)
        self._age_timer.start()
        self.set_state("disconnected", "")
        if autoconnect:
            QTimer.singleShot(100, lambda: self.connect_to(autoconnect))

    # --- layout ----------------------------------------------------------------

    def _build(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        mark = QLabel()
        mark.setPixmap(theme.logo())
        mark.setContentsMargins(2, 0, 6, 0)
        tb.addWidget(mark)
        brand = QLabel("MeshCore", objectName="brand")
        brand.setContentsMargins(0, 0, 10, 0)
        tb.addWidget(brand)
        self.connect_act = QAction("Connect", self, triggered=self.choose_device)
        self.disconnect_act = QAction("Disconnect", self, triggered=self.disconnect)
        tb.addAction(self.connect_act)
        tb.widgetForAction(self.connect_act).setObjectName("primary")
        tb.addAction(self.disconnect_act)
        self.unpair_act = QAction("Unpair", self, triggered=self.forget_pairing)
        self.unpair_act.setToolTip("Forget this computer's Bluetooth pairing with the node")
        tb.addAction(self.unpair_act)
        tb.addSeparator()
        adv = QToolButton()
        adv.setText("Advert ▾")
        adv.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        adv.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(adv)
        m.addAction("Zero-hop (neighbours only)", lambda: self.run(self.session.advert(False), "Advert sent"))
        m.addAction("Flood (whole mesh)", lambda: self.run(self.session.advert(True), "Flood advert sent"))
        adv.setMenu(m)
        self.advert_btn = adv
        tb.addWidget(adv)
        tb.addSeparator()
        self.node_label = QLabel("", objectName="dim")
        tb.addWidget(self.node_label)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        self.state_label = QLabel("", objectName="pill")
        self.state_label.setFixedHeight(26)
        tb.addWidget(self.state_label)

        split = QSplitter()
        split.setHandleWidth(1)
        self.setCentralWidget(split)

        # Left: chats and contacts.
        self.tabs = QTabWidget(objectName="sidebar")
        self.tabs.setAttribute(Qt.WA_StyledBackground)
        self.tabs.setMinimumWidth(330)
        self.tabs.setDocumentMode(True)
        self.chats = QListWidget()
        self.chats.setItemDelegate(ChatRow(self.chats))
        self.chats.setMouseTracking(True)
        self.chats.currentItemChanged.connect(self._chat_selected)
        self.chats.setContextMenuPolicy(Qt.CustomContextMenu)
        self.chats.customContextMenuRequested.connect(self._chat_menu)
        chats_w = QWidget()
        cl = QVBoxLayout(chats_w)
        cl.setContentsMargins(0, 0, 0, 8)
        chats_w.setObjectName("sidebar")
        cl.addWidget(self.chats)
        add_ch = QPushButton("+  Add channel")
        add_ch.clicked.connect(self.add_channel)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(8, 0, 8, 0)
        wrap.addWidget(add_ch)
        cl.addLayout(wrap)
        self.tabs.addTab(chats_w, "Chats")

        cw = QWidget()
        cv = QVBoxLayout(cw)
        cv.setContentsMargins(8, 8, 8, 0)
        cw.setObjectName("sidebar")
        self.contact_filter = QLineEdit(placeholderText="Filter contacts", objectName="round")
        self.contact_filter.textChanged.connect(self._refresh_contacts)
        cv.addWidget(self.contact_filter)
        self.contacts = QTreeWidget()
        self.contacts.setHeaderLabels(["Name", "Type", "Hops", "Heard", "km"])
        self.contacts.setAllColumnsShowFocus(True)
        self.contacts.setRootIsDecorated(False)
        self.contacts.setSortingEnabled(True)
        self.contacts.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.contacts.sortByColumn(3, Qt.AscendingOrder)
        self.contacts.header().setSectionResizeMode(0, QHeaderView.Stretch)
        # Just wide enough for their contents, so the name gets the rest.
        fm = self.contacts.fontMetrics()
        for i, sample in enumerate(("Repeater", "flood", "30 min ▾", "99.9"), 1):
            self.contacts.header().setSectionResizeMode(i, QHeaderView.Fixed)
            self.contacts.header().resizeSection(i, fm.horizontalAdvance(sample) + 12)
        self.contacts.itemDoubleClicked.connect(lambda it, _c: self.open_conv(it.data(0, Qt.UserRole)))
        self.contacts.setContextMenuPolicy(Qt.CustomContextMenu)
        self.contacts.customContextMenuRequested.connect(self._contact_menu)
        cv.addWidget(self.contacts)
        self.tabs.addTab(cw, "Contacts")
        split.addWidget(self.tabs)

        # Middle: the conversation.
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(0)
        self.title = QLabel("")
        f = self.title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setContentsMargins(16, 12, 16, 0)
        self.subtitle = QLabel("", objectName="dim")
        self.subtitle.setContentsMargins(16, 0, 16, 8)
        mv.addWidget(self.title)
        mv.addWidget(self.subtitle)
        self.list_model = MessageList(self.model)
        self.view = QListView(objectName="conversation")
        self.view.setModel(self.list_model)
        self.view.setItemDelegate(Bubble(self, self.view))
        self.view.setResizeMode(QListView.Adjust)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._msg_menu)
        self.view.doubleClicked.connect(lambda ix: self.reply(ix.data(Qt.UserRole)))
        self.view.selectionModel().currentChanged.connect(lambda ix, _p: self._show_details(ix.data(Qt.UserRole)))
        self.list_model.rowsAboutToBeInserted.connect(self._remember_bottom)
        self.list_model.rowsInserted.connect(self._stick_bottom)
        mv.addWidget(self.view, 1)

        self.reply_bar = QFrame()
        rb = QHBoxLayout(self.reply_bar)
        rb.setContentsMargins(10, 2, 10, 2)
        self.reply_label = QLabel("")
        rb.addWidget(self.reply_label, 1)
        x = QToolButton(text="✕")
        x.clicked.connect(self.cancel_reply)
        rb.addWidget(x)
        self.reply_bar.hide()
        mv.addWidget(self.reply_bar)

        comp = QHBoxLayout()
        comp.setContentsMargins(16, 8, 16, 12)
        comp.setSpacing(8)
        self.input = QLineEdit(placeholderText="Message", objectName="round")
        self.input.returnPressed.connect(self.send)
        self.input.textChanged.connect(self._count)
        self.counter = QLabel("", objectName="dim")
        self.send_btn = QPushButton("Send", objectName="primary")
        self.send_btn.clicked.connect(self.send)
        comp.addWidget(self.input, 1)
        comp.addWidget(self.counter)
        comp.addWidget(self.send_btn)
        mv.addLayout(comp)
        self.center = QTabWidget()
        self.center.setDocumentMode(True)
        self.center.addTab(mid, "Messages")
        from .mapview import MapView
        self.map = MapView(self.model, self.open_conv, self._map_action, self._repeater_stats)
        self.center.addTab(self.map, "Map")
        self.admin = RepeaterPanel(self)
        self.center.addTab(self.admin, "Repeater")
        self.center.currentChanged.connect(self._center_tab)
        split.addWidget(self.center)

        # Right: the selected message's path and repeaters.
        self.details = QTextBrowser(objectName="details")
        self.details.setOpenLinks(False)
        self.details.setMinimumWidth(250)
        split.addWidget(self.details)
        split.setSizes([340, 620, 290])
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)

        QAction("Reply", self, shortcut=QKeySequence("Ctrl+R"), triggered=self._reply_current)
        esc = QAction(self, shortcut=QKeySequence("Escape"), triggered=self.cancel_reply)
        self.addAction(esc)

        self._show_details(None)

    # --- connection ---------------------------------------------------------------

    def run(self, coro, done_msg: str | None = None):
        """Runs a command on the session, reporting failure in the status bar."""
        async def go():
            try:
                r = await coro
                if done_msg:
                    ok = r is not None and getattr(r, "kind", "") != "err"
                    self.statusBar().showMessage(done_msg if ok else f"{done_msg}: failed", 5000)
            except Exception as e:
                log.exception("command")
                self.statusBar().showMessage(f"Error: {e}", 8000)
        return asyncio.ensure_future(go())

    def choose_device(self):
        d = ConnectDialog(self, self.address)
        if d.exec() and d.choice:
            self.connect_to(d.choice)

    def connect_to(self, address: str):
        self.address = address
        self.settings.setValue("address", address)
        self.want_connected = True
        asyncio.ensure_future(self._connect())

    async def _connect(self):
        if self.session:
            await self.session.stop()
        if self.address == SIM:
            from .sim import SimTransport
            t = SimTransport()
        else:
            t = BleTransport(self.address, self.ask_pin)
        self.session = Session(self.model, t, self.set_state)
        try:
            await self.session.start()
        except Exception as e:
            log.exception("connect")
            try:
                await t.close()
            except Exception:
                pass
            self.session.connected = False
            if isinstance(e, (PairingError, BondError)):
                # Retrying on a timer would just keep asking for the PIN.
                self.want_connected = False
                self.set_state("disconnected", str(e))
                await self._pairing_failed(t, e)
            else:
                self.set_state("disconnected", str(e) or e.__class__.__name__)

    async def ask_pin(self, _device_path: str) -> int | None:
        """Pairing wants the PIN the node shows on its screen."""
        self.state_label.setText("Pairing: enter the PIN…")
        self.showNormal()
        self.activateWindow()
        d = PinDialog(self)
        fut = asyncio.get_running_loop().create_future()
        d.finished.connect(lambda r: fut.done() or fut.set_result(d.pin() if r else None))
        d.open()
        pin = await fut
        self.state_label.setText("Pairing…" if pin is not None else "Pairing cancelled")
        return pin

    async def _pairing_failed(self, t, e):
        if isinstance(e, BondError):
            q = ("This computer is paired with the node, but the node wouldn't use that pairing. "
                 "That usually means it was reflashed or reset.\n\nForget the old pairing and pair again?")
        elif "cancel" in str(e).lower():
            return
        else:
            q = f"Pairing failed: {e}\n\nTry again? Check the PIN on the node's screen; it can change each time."
        box = QMessageBox(QMessageBox.Warning, "Pairing", q, QMessageBox.Yes | QMessageBox.No, self)
        fut = asyncio.get_running_loop().create_future()
        box.finished.connect(lambda _r: fut.done() or fut.set_result(box.result() == QMessageBox.Yes))
        box.open()
        if not await fut:
            return
        if isinstance(e, BondError):
            try:
                await t.forget()
            except Exception as fe:
                log.warning("forget: %s", fe)
        self.want_connected = True
        await self._connect()

    def forget_pairing(self):
        if not self.address or self.address == SIM:
            return
        if QMessageBox.question(self, "Forget pairing", f"Remove the Bluetooth pairing with {self.address}? "
                                "You'll be asked for the PIN on the next connect.") != QMessageBox.Yes:
            return

        async def go():
            if self.session:
                self.want_connected = False
                await self.session.stop()
            try:
                await BleTransport(self.address).forget()
                self.statusBar().showMessage("Pairing removed", 5000)
            except Exception as e:
                self.statusBar().showMessage(f"Couldn't remove pairing: {e}", 8000)
        asyncio.ensure_future(go())

    def disconnect(self):
        self.want_connected = False
        if self.session:
            asyncio.ensure_future(self.session.stop())

    def set_state(self, state: str, detail: str):
        self.state = state
        text = {"connecting": "Connecting…", "syncing": f"Syncing {detail}…", "ready": "Connected",
                "disconnected": "Disconnected" + (f": {detail}" if detail else "")}[state]
        self.state_label.setText("● " + text)
        theme.set_tone(self.state_label, {"ready": "ok", "disconnected": "off"}.get(state, "busy"))
        ready = state == "ready"
        self.connect_act.setEnabled(state == "disconnected")
        self.disconnect_act.setEnabled(state != "disconnected")
        self.unpair_act.setEnabled(bool(self.address) and self.address != SIM)
        self.advert_btn.setEnabled(ready)
        self.admin.link_changed(state)
        self._count()
        if state == "disconnected" and self.want_connected and detail:
            # Dropped or failed: keep trying while the user wants a link.
            QTimer.singleShot(10000, lambda: self.want_connected and self.state == "disconnected"
                              and asyncio.ensure_future(self._connect()))
            self.state_label.setText(text + " — retrying in 10 s")
        if ready:
            self._refresh_lists()
            self._refresh_contacts()

    # --- model events ---------------------------------------------------------------

    def _on_model(self, ev: str, obj):
        if ev == "message_added":
            self.list_model.added(obj)
            if obj.conv == self.conv and self.isActiveWindow():
                self.model.mark_read(self.conv)
            elif not obj.outgoing and obj.live:
                self._notify(obj)
            self._refresh_timer.start()
        elif ev == "message_changed":
            self.list_model.changed(obj)
            cur = self.view.currentIndex().data(Qt.UserRole)
            if cur is obj:
                self._show_details(obj)
        elif ev == "reload":
            self.list_model.reload()
            self._refresh_timer.start()
        elif ev in ("channels", "unread"):
            self._refresh_timer.start()
        elif ev == "contacts":
            self._refresh_contacts()
            self._refresh_timer.start()
            self.map.schedule()
        elif ev == "contact":
            if self.state == "ready":
                self._contacts_timer()
                self.map.schedule()
        elif ev == "cli_late":
            self.admin.late_reply(obj)
        elif ev == "node":
            self._node_label()
            self.map.schedule()

    def _contacts_timer(self):
        if not hasattr(self, "_ct"):
            self._ct = QTimer(self, singleShot=True, interval=1000, timeout=self._refresh_contacts)
        self._ct.start()

    def _notify(self, m: Message):
        me = self.model.self_info.name if self.model.self_info else ""
        mentioned = me and f"@[{me}]" in m.text
        if not m.is_channel or mentioned:
            if not self.isActiveWindow():
                # A desktop notification, no tray icon.
                title = f"{m.sender} in {self.model.conv_title(m.conv)}" if m.is_channel else m.sender
                notify(self, title, m.text)
            QApplication.alert(self)

    def _node_label(self):
        s = self.model.self_info
        if not s:
            self.node_label.setText("")
            return
        bits = [f"<b style='color:{theme.current.text}'>{html.escape(s.name)}</b>", f"{s.freq:.3f} MHz SF{s.sf} BW{s.bw:g}"]
        if self.model.battery_mv:
            bits.append(f"{P.batt_percent(self.model.battery_mv)}% · {self.model.battery_mv / 1000:.2f} V")
        di = self.model.device_info
        if di and di.fw:
            bits.append(html.escape(di.fw))
        self.node_label.setText(" &nbsp;·&nbsp; ".join(bits))

    # --- left side ------------------------------------------------------------------

    def _refresh_lists(self):
        convs = [chan_conv(i) for i in sorted(self.model.channels)]
        dms = self.model.dm_convs()
        if self.conv.startswith("dm:") and self.conv not in dms:
            dms.insert(0, self.conv)
        self.chats.blockSignals(True)
        self.chats.clear()
        sel = None

        def header(t):
            it = QListWidgetItem(t)
            it.setFlags(Qt.NoItemFlags)
            f = it.font()
            f.setBold(True)
            f.setPointSize(max(7, f.pointSize() - 1))
            it.setFont(f)
            self.chats.addItem(it)

        header("CHANNELS")
        for c in convs + ["—"] + dms:
            if c == "—":
                header("DIRECT MESSAGES")
                continue
            n = self.model.unread(c) if c != self.conv else 0
            title = self.model.conv_title(c)
            it = QListWidgetItem(title)
            it.setData(Qt.UserRole, c)
            it.setData(Qt.UserRole + 1, n)
            self.chats.addItem(it)
            if c == self.conv:
                sel = it
        if sel:
            self.chats.setCurrentItem(sel)
        self.chats.blockSignals(False)
        if not self.conv and convs:
            self.open_conv(convs[0])
        self._update_title()

    def _chat_selected(self, it, _prev):
        if it and it.data(Qt.UserRole):
            self.open_conv(it.data(Qt.UserRole))

    def _refresh_contacts(self):
        q = self.contact_filter.text().lower()
        self.contacts.setSortingEnabled(False)
        self.contacts.clear()
        for c in self.model.contacts.values():
            if q and q not in c.name.lower():
                continue
            age = self.model.contact_age(c)
            km = self.model.distance_km(c)
            it = SortItem([c.name or c.key[:3].hex(), TYPE_NAMES.get(c.type, "?"),
                           "flood" if c.hops < 0 else str(c.hops), ago(age), f"{km:.1f}" if km is not None else ""])
            it.setData(0, Qt.UserRole, dm_conv(c.prefix))
            it.setData(0, Qt.UserRole + 1, c)
            it.sort_keys = {2: c.hops if c.hops >= 0 else 99, 3: age if age is not None else 1e12,
                            4: km if km is not None else 1e9}
            it.setToolTip(0, f"{c.key.hex()}\n" + (f"Route: {' → '.join(h.hex() for h in c.path.hops())}"
                                                  if c.hops > 0 else ""))
            self.contacts.addTopLevelItem(it)
        self.contacts.setSortingEnabled(True)
        self.tabs.setTabText(1, f"Contacts ({len(self.model.contacts)})")

    def _contact_menu(self, pos: QPoint):
        it = self.contacts.itemAt(pos)
        if not it:
            return
        c: P.Contact = it.data(0, Qt.UserRole + 1)
        m = QMenu(self)
        if c.type == P.ADV_REPEATER:
            m.addAction("Manage repeater…", lambda: self.manage_repeater(c))
        m.addAction("Message", lambda: self.open_conv(dm_conv(c.prefix)))
        if c.lat or c.lon:
            m.addAction("Show on map", lambda: self.show_on_map(c))
        m.addAction("Reset route (rediscover by flood)",
                    lambda: self.run(self.session.reset_path(c.key), f"Route reset for {c.name}"))
        m.addAction("Copy public key", lambda: QApplication.clipboard().setText(c.key.hex()))
        m.exec(self.contacts.viewport().mapToGlobal(pos))

    def _chat_menu(self, pos: QPoint):
        it = self.chats.itemAt(pos)
        conv = it.data(Qt.UserRole) if it else None
        if not conv or not conv.startswith("ch:"):
            return
        idx = int(conv[3:])
        m = QMenu(self)
        m.addAction("Mark read", lambda: self.model.mark_read(conv))
        m.addAction("Remove channel from node", lambda: self.remove_channel(idx))
        m.exec(self.chats.viewport().mapToGlobal(pos))

    def add_channel(self):
        if not self.session or not self.session.connected:
            QMessageBox.information(self, "Add channel", "Connect to a node first.")
            return
        free = [i for i in range(self.session.max_channels) if i not in self.model.channels]
        if not free:
            QMessageBox.warning(self, "Add channel", "Every channel slot on the node is in use.")
            return
        d = ChannelDialog(self)
        if d.exec() and d.result_:
            name, secret = d.result_
            self.run(self.session.set_channel(free[0], name, secret), f"Added {name}")

    def remove_channel(self, idx: int):
        name = self.model.conv_title(chan_conv(idx))
        if QMessageBox.question(self, "Remove channel", f"Remove {name} from the node? Saved messages stay here.") \
                != QMessageBox.Yes:
            return
        self.run(self.session.set_channel(idx, "", bytes(16)), f"Removed {name}")

    # --- conversation -----------------------------------------------------------------

    def _center_tab(self, i: int):
        self.details.setVisible(i == 0)
        if i == 1:
            self.map.refresh()

    def show_on_map(self, c: P.Contact):
        self.center.setCurrentIndex(1)
        self.map.focus(c.prefix.hex())

    def _map_action(self, what: str, c: P.Contact):
        if what == "manage":
            self.manage_repeater(c)
        elif what == "reset" and self.session and self.state == "ready":
            self.run(self.session.reset_path(c.key), f"Route reset for {c.name}")

    def _repeater_stats(self, prefix: bytes) -> dict[str, str]:
        r = self.admin.remotes.get(prefix)
        return r.stats if r else {}

    def manage_repeater(self, c: P.Contact):
        self.center.setCurrentWidget(self.admin)
        self.admin.open(c)

    def open_conv(self, conv: str):
        self.center.setCurrentIndex(0)
        if conv == self.conv:
            return
        self.conv = conv
        self.cancel_reply()
        self.list_model.set_conv(conv)
        self.view.scrollToBottom()
        self.model.mark_read(conv)
        self._update_title()
        self._show_details(None)
        self._count()
        self._refresh_lists()
        self.input.setFocus()

    def _update_title(self):
        if not self.conv:
            self.title.setText("")
            self.subtitle.setText("Connect to a node to start.")
            return
        self.title.setText(self.model.conv_title(self.conv))
        if self.conv.startswith("ch:"):
            ch = self.model.channels.get(int(self.conv[3:]))
            self.subtitle.setText(f"Channel {self.conv[3:]}" + ("" if ch else " (not on this node)"))
        else:
            c = self.model.contacts.get(bytes.fromhex(self.conv[3:]))
            if c:
                route = "no known route: sends by flood" if c.hops < 0 else (
                    "direct neighbour" if c.hops == 0 else
                    f"{c.hops} hops via " + " → ".join(self._hop(h.hex()) for h in c.path.hops()))
                self.subtitle.setText(f"{TYPE_NAMES.get(c.type, '')} · {route} · heard {ago(self.model.contact_age(c))}"
                                      " ago")
            else:
                self.subtitle.setText("Not in the node's contacts")

    def _hop(self, h: str) -> str:
        names = self.model.hop_names(h)
        if not names:
            return h
        return names[0] + (f" (+{len(names) - 1}?)" if len(names) > 1 else "")

    def _remember_bottom(self, *_):
        sb = self.view.verticalScrollBar()
        self._at_bottom = sb.value() >= sb.maximum() - 30

    def _stick_bottom(self, *_):
        if getattr(self, "_at_bottom", True):
            QTimer.singleShot(0, self.view.scrollToBottom)

    def _count(self, *_):
        ready = getattr(self, "state", "") == "ready" and bool(self.conv)
        n = len(self.input.text().encode())
        lim = text_limit(self.model, self.conv) if self.conv else P.TEXT_MAX
        self.counter.setText(f"{n}/{lim}")
        self.counter.setStyleSheet(f"color:{theme.current.danger}" if n > lim else "")
        self.send_btn.setEnabled(ready and 0 < n <= lim)

    def send(self):
        text = self.input.text().strip()
        if not text or not self.send_btn.isEnabled():
            return
        self.input.clear()
        self.cancel_reply()
        self._at_bottom = True
        self.run(self.session.send(self.conv, text))

    def reply(self, m: Message | None):
        if not m:
            return
        if m.outgoing:
            self.input.setFocus()
            return
        self.reply_to = m
        snippet = m.text if len(m.text) < 60 else m.text[:57] + "…"
        self.reply_label.setText(f"↩ Replying to <b>{html.escape(m.sender)}</b>: {html.escape(snippet)}")
        self.reply_bar.show()
        tag = f"@[{m.sender}] "
        t = MENTION.sub("", self.input.text()).lstrip() if self.input.text().startswith("@[") else self.input.text()
        self.input.setText(tag + t)
        self.input.setFocus()
        self.input.end(False)

    def _reply_current(self):
        self.reply(self.view.currentIndex().data(Qt.UserRole))

    def cancel_reply(self):
        if self.reply_to and self.input.text().startswith(f"@[{self.reply_to.sender}] "):
            self.input.setText(self.input.text()[len(self.reply_to.sender) + 4:])
        self.reply_to = None
        self.reply_bar.hide()

    def _msg_menu(self, pos: QPoint):
        ix = self.view.indexAt(pos)
        m: Message = ix.data(Qt.UserRole) if ix.isValid() else None
        if not m:
            return
        self.view.setCurrentIndex(ix)
        menu = QMenu(self)
        if not m.outgoing:
            menu.addAction("Reply", lambda: self.reply(m))
            if m.is_channel:
                c = next((c for c in self.model.contacts.values() if c.name == m.sender), None)
                if c:
                    menu.addAction(f"Message {m.sender} directly", lambda: self.open_conv(dm_conv(c.prefix)))
        if m.outgoing and m.status in (NO_ACK, FAILED) and self.state == "ready":
            menu.addAction("Send again", lambda: self.run(self.session.resend(m)))
        menu.addAction("Copy text", lambda: QApplication.clipboard().setText(m.text))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _show_details(self, m: Message | None):
        if not m:
            self.details.setHtml("<p style='color:gray'>Select a message to see its path and the repeaters "
                                 "that relayed it.<br><br>Double-click or Ctrl+R to reply.</p>")
            return
        rows = [f"<h3>{'Sent' if m.outgoing else 'From ' + html.escape(m.sender)}</h3>",
                f"<p>{time.strftime('%a', time.localtime(m.at))} {short_date(time.localtime(m.at))}, "
                f"{time.strftime('%H:%M:%S', time.localtime(m.at))}"]
        if m.wire_time and not m.outgoing:
            skew = m.wire_time - (m.at + self.model.clock_offset)
            if abs(skew) > 120:
                rows.append(f"<br><small>Their clock said {when(m.wire_time, True)}"
                            "</small>")
        rows.append("</p>")
        if not m.outgoing:
            route = "Direct route" if m.hops < 0 else f"{m.hops} hop{'s' * (m.hops != 1)} (flood)"
            rows.append(f"<p><b>Route:</b> {route}" + (f"<br><b>SNR:</b> {m.snr:.2f} dB" if m.snr is not None else "")
                        + "</p>")
            if m.path is not None and m.path:
                hops = [html.escape(m.sender)] + [html.escape(self._hop(h)) for h in m.path] + ["you"]
                rows.append("<p><b>Path</b><br>" + "<br>↓ ".join(hops) + "</p>")
            elif m.hops > 0:
                rows.append("<p><small>Path not known: it was queued on the node while nothing was "
                            "connected.</small></p>")
        else:
            st = {SENDING: "Sending", SENT: "Sent (channel messages get no ack)", WAITING: "Waiting for ack",
                  DELIVERED: "Delivered", NO_ACK: "No ack came back", FAILED: "The node refused it"}[m.status]
            rows.append(f"<p><b>Status:</b> {st}" + (f" in {m.trip_ms / 1000:.1f} s" if m.trip_ms else "") + "</p>")
        rows.append(f"<p><b>Repeats heard:</b> {m.repeats}</p>")
        if m.repeaters:
            rows.append("<table cellspacing=0 cellpadding=3><tr><th align=left>Repeater</th><th>Heard by you</th></tr>")
            for r in sorted(m.repeaters.values(), key=lambda r: (not r.direct, -(r.snr or -99))):
                heard = f"SNR {r.snr:.1f}" if r.direct and r.snr is not None else ("yes" if r.direct else "")
                rows.append(f"<tr><td>{html.escape(self._hop(r.hash))} <small style='color:{theme.current.dim}'>{r.hash}</small>"
                            f"</td><td align=center>{heard}</td></tr>")
            rows.append("</table><p><small>“Heard by you” means your node received that repeater's "
                        "rebroadcast directly.</small></p>")
        elif m.live and m.is_channel:
            rows.append("<p><small>Listening for repeats…</small></p>")
        self.details.setHtml("".join(rows))

    def changeEvent(self, e):
        if e.type() == e.Type.ActivationChange and self.isActiveWindow() and self.conv:
            if self.model.unread(self.conv):
                self.model.mark_read(self.conv)
        super().changeEvent(e)

    def closeEvent(self, e):
        """Closing the window quits: let go of the node, so a phone or another
        app can connect, then end the program."""
        if getattr(self, "_closing", False):
            e.accept()
            return
        e.ignore()
        self._closing = True
        self.want_connected = False
        self.hide()

        async def shutdown():
            if self.session and self.session.connected:
                try:
                    await asyncio.wait_for(self.session.stop(), 4)
                except Exception as ex:
                    log.warning("disconnect on quit: %s", ex)
            # Let main() finish its loop rather than yanking it out from under us.
            if getattr(self, "quit_event", None):
                self.quit_event.set()
            else:
                QApplication.quit()
        asyncio.ensure_future(shutdown())


class SortItem(QTreeWidgetItem):
    """Sorts hops, ages and distances as numbers."""
    sort_keys: dict = {}

    def __lt__(self, other):
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if col in self.sort_keys:
            return self.sort_keys[col] < other.sort_keys.get(col, 0)
        return self.text(col).lower() < other.text(col).lower()
