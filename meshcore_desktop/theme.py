"""The app's look: one set of colours for light and one for dark, a Qt palette and
a stylesheet built from them. Follows the system's light or dark setting."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from .desktop import HERE


@dataclass(frozen=True)
class Colors:
    dark: bool
    bg: str  # behind the conversation
    surface: str  # sidebars, cards
    raised: str  # hover, inputs on a card
    border: str
    text: str
    dim: str
    accent: str
    on_accent: str
    accent_soft: str  # selections, our own bubbles
    danger: str
    ok: str
    warn: str
    accent2: str  # where the accent gradient ends


LIGHT = Colors(False, "#f4f5f7", "#ffffff", "#eceef1", "#ced3db", "#1c2128", "#5c6573",
               "#0d7f74", "#ffffff", "#d0ece7", "#c9342b", "#1a8a4c", "#b67a06", "#0b6fa4")
DARK = Colors(True, "#14171b", "#1b1f25", "#262b33", "#3b4350", "#e6e9ee", "#9aa3b2",
              "#2dbdab", "#06201c", "#17423c", "#f0665c", "#4cc38a", "#f0b429", "#46a8e6")

# name: (light mode, dark mode), each what the accent changes.
ACCENTS = {
    "Teal": ({}, {}),
    "Violet": (dict(accent="#6d4fd1", accent2="#b0369c", on_accent="#ffffff", accent_soft="#e7e1fa"),
               dict(accent="#a78bfa", accent2="#e879c8", on_accent="#1a1033", accent_soft="#32285c")),
    "Blue": (dict(accent="#1f6fd1", accent2="#0a7c98", on_accent="#ffffff", accent_soft="#dbe9fb"),
             dict(accent="#5aa9f5", accent2="#36c5d9", on_accent="#06182b", accent_soft="#1b3a5c")),
    "Sunset": (dict(accent="#c2410c", accent2="#b91c5c", on_accent="#ffffff", accent_soft="#fbe3d6"),
               dict(accent="#fb923c", accent2="#f472b6", on_accent="#2a1204", accent_soft="#4a2a18")),
}

current = LIGHT
accent_name = "Teal"
on_change: list = []  # called after the look changes


def _palette(c: Colors) -> QPalette:
    p = QPalette()
    for role, value in {
        QPalette.Window: c.bg, QPalette.WindowText: c.text, QPalette.Base: c.surface, QPalette.AlternateBase: c.raised,
        QPalette.Text: c.text, QPalette.Button: c.surface, QPalette.ButtonText: c.text, QPalette.BrightText: c.text,
        QPalette.ToolTipBase: c.raised, QPalette.ToolTipText: c.text, QPalette.PlaceholderText: c.dim,
        QPalette.Highlight: c.accent, QPalette.HighlightedText: c.on_accent, QPalette.Link: c.accent,
        QPalette.LinkVisited: c.accent,
    }.items():
        p.setColor(role, QColor(value))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(c.dim))
    return p


def _sheet(c: Colors) -> str:
    grad = f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {c.accent}, stop:1 {c.accent2});"
    hi = QColor(c.accent).lighter(112).name(), QColor(c.accent2).lighter(112).name()
    grad_hi = f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {hi[0]}, stop:1 {hi[1]});"
    return f"""
    QToolTip {{ background: {c.raised}; color: {c.text}; border: 1px solid {c.border}; padding: 4px 6px; }}
    QSplitter::handle {{ background: {c.border}; }}
    QLabel#dim {{ color: {c.dim}; }}

    QToolBar {{ background: {c.surface}; border: none; border-bottom: 1px solid {c.border}; padding: 6px 8px; spacing: 4px; }}
    QToolBar::separator {{ background: {c.border}; width: 1px; margin: 4px 8px; }}
    QToolBar QToolButton {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 14px;
                            padding: 5px 14px; min-height: 18px; font-weight: 500; }}
    QToolBar QToolButton:hover {{ background: {c.raised}; border-color: {c.accent}; }}
    QToolBar QToolButton:pressed {{ background: {c.accent_soft}; }}
    QToolBar QToolButton:disabled {{ color: {c.dim}; background: transparent; border-color: {c.raised}; }}
    QToolBar QToolButton#primary:enabled {{ {grad} color: {c.on_accent}; border: 1px solid {c.accent}; font-weight: 600; }}
    QToolBar QToolButton#primary:enabled:hover {{ {grad_hi} }}
    QLabel#brand {{ font-weight: 700; font-size: 15px; color: {c.text}; }}
    QLabel#pill {{ border-radius: 13px; padding: 0 12px; background: {c.raised}; color: {c.dim}; }}
    QLabel#pill[tone="ok"] {{ background: {c.accent_soft}; color: {c.ok}; }}
    QLabel#pill[tone="busy"] {{ color: {c.warn}; }}

    QTabWidget#sidebar {{ background: {c.surface}; }}
    QTabWidget::pane {{ border: none; border-top: 1px solid {c.border}; }}
    QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
    QTabBar::tab {{ background: transparent; color: {c.dim}; padding: 5px 15px; margin: 7px 2px 7px 2px; border: none;
                    border-radius: 13px; min-height: 16px; font-weight: 500; }}
    QTabBar::tab:hover {{ color: {c.text}; background: {c.raised}; }}
    QTabBar::tab:selected {{ {grad} color: {c.on_accent}; font-weight: 600; }}

    QWidget#sidebar {{ background: {c.surface}; }}
    QListWidget, QTreeWidget {{ background: {c.surface}; border: none; outline: 0; padding: 4px; }}
    QListWidget::item {{ padding: 6px 8px; border-radius: 6px; color: {c.text}; }}
    QListWidget::item:hover {{ background: {c.raised}; }}
    QListWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QListWidget::item:disabled {{ background: transparent; color: {c.dim}; }}
    QTreeWidget::item {{ padding: 4px 2px; }}
    QTreeWidget::item:hover {{ background: {c.raised}; }}
    QTreeWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QHeaderView {{ background: {c.surface}; }}
    QHeaderView::section {{ background: {c.surface}; color: {c.dim}; border: none;
                            border-bottom: 1px solid {c.border}; padding: 5px 4px; }}
    QListView#conversation {{ background: {c.bg}; border: none; }}
    QTextBrowser#details {{ background: {c.surface}; border: none; padding: 10px; }}

    QLineEdit, QPlainTextEdit, QComboBox {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 10px;
        padding: 6px 10px; selection-background-color: {c.accent}; selection-color: {c.on_accent}; }}
    QLineEdit#round {{ border-radius: 15px; padding: 6px 14px; min-height: 18px; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {c.accent}; }}
    QLineEdit:disabled, QComboBox:disabled {{ background: {c.bg}; color: {c.dim}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url({(HERE / "icons/chevron.png").as_posix()}); width: 10px; height: 10px; }}
    QComboBox QAbstractItemView {{ background: {c.surface}; border: 1px solid {c.border}; outline: 0;
                                   selection-background-color: {c.accent_soft}; selection-color: {c.text}; }}

    QPushButton, QToolButton#plain {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 15px;
        padding: 6px 16px; min-height: 18px; font-weight: 500; }}
    QPushButton:hover, QToolButton#plain:hover {{ background: {c.raised}; border-color: {c.accent}; }}
    QPushButton:pressed, QToolButton#plain:pressed {{ background: {c.accent_soft}; }}
    QPushButton:disabled, QToolButton#plain:disabled {{ color: {c.dim}; background: transparent; border-color: {c.raised}; }}
    QPushButton#primary {{ {grad} color: {c.on_accent}; border: 1px solid {c.accent}; font-weight: 600; }}
    QPushButton#primary:hover {{ {grad_hi} }}
    QPushButton#primary:pressed {{ background: {c.accent}; }}
    QPushButton#primary:disabled {{ background: {c.raised}; color: {c.dim}; border-color: {c.raised}; font-weight: 500; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}

    QCheckBox#chip {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 13px;
                      padding: 3px 11px; min-height: 18px; color: {c.dim}; }}
    QCheckBox#chip:hover {{ border-color: {c.accent}; }}
    QCheckBox#chip:checked {{ background: {c.accent_soft}; border-color: {c.accent}; color: {c.text}; font-weight: 600; }}
    QCheckBox#chip::indicator {{ width: 0; height: 0; }}

    QFrame#tile {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 12px; }}
    QLabel#big {{ font-size: 26px; font-weight: 700; color: {c.text}; }}
    QLabel#hero {{ font-size: 30px; font-weight: 700; color: {c.text}; }}
    QLabel#toast {{ background: {c.surface}; color: {c.text}; border: 1px solid {c.accent}; border-radius: 17px;
                    padding: 8px 18px; font-weight: 600; }}

    QGroupBox {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 12px;
                 margin-top: 22px; padding: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 4px; top: 2px; color: {c.text}; font-weight: 600; }}

    QMenu {{ background: {c.surface}; border: 1px solid {c.border}; padding: 6px; }}
    QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 8px; }}
    QMenu::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QMenu::separator {{ height: 1px; background: {c.border}; margin: 4px 6px; }}

    QStatusBar {{ background: {c.surface}; color: {c.dim}; border-top: 1px solid {c.border}; }}
    QStatusBar::item {{ border: none; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle {{ background: {c.border}; border-radius: 3px; min-height: 28px; min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {c.dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


def accent_gradient(rect: QRectF) -> QLinearGradient:
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    g.setColorAt(0, QColor(current.accent))
    g.setColorAt(1, QColor(current.accent2))
    return g


def draw_avatar(p: QPainter, rect: QRectF, name: str, channel: bool = False):
    """A coloured disc with the name's first letter. Channels get a # on the accent."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    if channel:
        p.setBrush(accent_gradient(rect))
        ink = QColor(current.on_accent)
    else:
        hue = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 360
        p.setBrush(QColor.fromHsl(hue, 130, 100))
        ink = QColor("#ffffff")
    p.drawEllipse(rect)
    f = QFont(p.font())
    f.setBold(True)
    f.setPixelSize(int(rect.height() * 0.46))
    p.setFont(f)
    p.setPen(ink)
    p.drawText(rect, Qt.AlignCenter, "#" if channel else (name.strip()[:1].upper() or "?"))
    p.restore()


def logo(size: int = 26) -> QPixmap:
    """Three linked nodes on the accent: the mesh."""
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size * 2)
    box = QRectF(0, 0, s, s)
    p.setPen(Qt.NoPen)
    p.setBrush(accent_gradient(box))
    p.drawRoundedRect(box, s * 0.26, s * 0.26)
    nodes = [QPointF(s * 0.5, s * 0.26), QPointF(s * 0.25, s * 0.72), QPointF(s * 0.75, s * 0.72)]
    ink = QColor(current.on_accent)
    p.setPen(QPen(ink, s * 0.06))
    p.drawPolygon(nodes)
    p.setPen(Qt.NoPen)
    p.setBrush(ink)
    for n in nodes:
        p.drawEllipse(n, s * 0.1, s * 0.1)
    p.end()
    pm.setDevicePixelRatio(2)
    return pm


def set_tone(label, tone: str):
    """Changes a pill's colour. Qt only rereads a property's style on a re-polish."""
    label.setProperty("tone", tone)
    label.style().unpolish(label)
    label.style().polish(label)


def apply(app: QApplication, dark: bool, accent: str | None = None):
    global current, accent_name
    if accent in ACCENTS:
        accent_name = accent
    current = replace(DARK if dark else LIGHT, **ACCENTS[accent_name][dark])
    app.setStyle("Fusion")  # the one built-in style that honours all of this on Linux and Windows
    app.setPalette(_palette(current))
    app.setStyleSheet(_sheet(current))
    for fn in list(on_change):
        fn()


def follow_system(app: QApplication, accent: str = "Teal"):
    """Must run before apply() changes the palette it reads."""
    scheme = app.styleHints().colorScheme()
    dark = scheme == Qt.ColorScheme.Dark if scheme != Qt.ColorScheme.Unknown else (
        app.palette().color(QPalette.Window).lightness() < 128)
    apply(app, dark, accent)

    def changed(s):
        if s != Qt.ColorScheme.Unknown and (s == Qt.ColorScheme.Dark) != current.dark:
            apply(app, s == Qt.ColorScheme.Dark)
    app.styleHints().colorSchemeChanged.connect(changed)
