"""The app's look: one set of colours for light and one for dark, a Qt palette and
a stylesheet built from them. Follows the system's light or dark setting."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QLinearGradient, QPainter, QPalette, QPen, QPixmap
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


LIGHT = Colors(False, "#e8e9e5", "#f6f6f3", "#dfe0db", "#b9bbb3", "#1d1f1c", "#5d6159",
               "#3f7420", "#ffffff", "#d9e6cc", "#b3261e", "#3f7420", "#9a6200", "#3f7420")
DARK = Colors(True, "#181a18", "#1f2220", "#2a2e2b", "#3c413d", "#dddfd6", "#8e948a",
              "#9cc76a", "#141a0e", "#2e3b24", "#e5675c", "#9cc76a", "#e0a841", "#9cc76a")

# name: (light mode, dark mode), each what the accent changes.
# One flat colour each: accent2 is kept equal to accent, so nothing draws a gradient.
ACCENTS = {
    "Signal green": ({}, {}),
    "Amber": (dict(accent="#8a5600", accent2="#8a5600", on_accent="#ffffff", accent_soft="#efe0c4"),
              dict(accent="#e3a53c", accent2="#e3a53c", on_accent="#1d1405", accent_soft="#463620")),
    "Radio red": (dict(accent="#a32b22", accent2="#a32b22", on_accent="#ffffff", accent_soft="#f0d6d2"),
                  dict(accent="#e8705f", accent2="#e8705f", on_accent="#1e0b08", accent_soft="#4a2622")),
    "Slate blue": (dict(accent="#2f5b8a", accent2="#2f5b8a", on_accent="#ffffff", accent_soft="#d5e0ec"),
                   dict(accent="#86ade0", accent2="#86ade0", on_accent="#0b1522", accent_soft="#26364a")),
}
MONO = "monospace"  # the system's fixed-width family, set in apply(): Qt takes one name, not a list

current = LIGHT
accent_name = "Signal green"
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
    grad = f"background: {c.accent};"
    grad_hi = f"background: {QColor(c.accent).lighter(110).name() if c.dark else QColor(c.accent).darker(112).name()};"
    return f"""
    QToolTip {{ background: {c.raised}; color: {c.text}; border: 1px solid {c.border}; padding: 4px 6px; }}
    QSplitter::handle {{ background: {c.border}; }}
    QSplitter#tiles::handle {{ background: transparent; }}
    QLabel#dim {{ color: {c.dim}; }}

    QToolBar {{ background: {c.surface}; border: none; border-bottom: 1px solid {c.border}; padding: 6px 8px; spacing: 4px; }}
    QToolBar::separator {{ background: {c.border}; width: 1px; margin: 4px 8px; }}
    QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 3px;
                            padding: 4px 10px; min-height: 18px; }}
    QToolBar QToolButton:hover {{ background: {c.raised}; border-color: {c.border}; }}
    QToolBar QToolButton:pressed, QToolBar QToolButton:checked {{ background: {c.accent_soft}; border-color: {c.border}; }}
    QToolBar QToolButton:disabled {{ color: {c.dim}; background: transparent; border-color: transparent; }}
    QToolBar QToolButton#primary:enabled {{ {grad} color: {c.on_accent}; border: 1px solid {c.accent}; font-weight: 600; }}
    QToolBar QToolButton#primary:enabled:hover {{ {grad_hi} }}
    QLabel#brand {{ font-weight: 700; font-size: 14px; letter-spacing: 1px; color: {c.text}; }}
    QLabel#pill {{ border: 1px solid {c.border}; border-radius: 2px; padding: 0 8px; background: transparent;
                   color: {c.dim}; font-family: {MONO}; font-size: 12px; }}
    QLabel#pill[tone="ok"] {{ border-color: {c.ok}; color: {c.ok}; }}
    QLabel#pill[tone="busy"] {{ color: {c.warn}; }}

    QTabWidget#sidebar {{ background: {c.surface}; }}
    QTabWidget::pane {{ border: none; border-top: 1px solid {c.border}; }}
    QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
    QTabBar::tab {{ background: transparent; color: {c.dim}; padding: 6px 12px 4px 12px; margin: 4px 2px 0 2px;
                    border: none; border-bottom: 2px solid transparent; border-radius: 0; min-height: 16px; }}
    QTabBar::tab:hover {{ color: {c.text}; }}
    QTabBar::tab:selected {{ color: {c.text}; border-bottom-color: {c.accent}; font-weight: 600; }}

    QWidget#sidebar {{ background: {c.surface}; }}
    QListWidget, QTreeWidget {{ background: {c.surface}; border: none; outline: 0; padding: 4px; }}
    QListWidget::item {{ padding: 6px 8px; border-radius: 0; color: {c.text}; }}
    QListWidget::item:hover {{ background: {c.raised}; }}
    QListWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; border-left: 3px solid {c.accent}; }}
    QListWidget::item:disabled {{ background: transparent; color: {c.dim}; }}
    QTreeWidget::item {{ padding: 4px 2px; }}
    QTreeWidget::item:hover {{ background: {c.raised}; }}
    QTreeWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QHeaderView {{ background: {c.surface}; }}
    QHeaderView::section {{ background: {c.surface}; color: {c.dim}; border: none;
                            border-bottom: 1px solid {c.border}; padding: 5px 4px; }}
    QListView#conversation {{ background: {c.bg}; border: none; }}
    QTextBrowser#details {{ background: {c.surface}; border: none; padding: 4px 10px 10px 10px; }}

    QLineEdit, QPlainTextEdit, QComboBox {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 2px;
        padding: 6px 10px; selection-background-color: {c.accent}; selection-color: {c.on_accent}; }}
    QLineEdit#round {{ border-radius: 2px; padding: 6px 10px; min-height: 18px; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {c.accent}; }}
    QLineEdit:disabled, QComboBox:disabled {{ background: {c.bg}; color: {c.dim}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: url({(HERE / "icons/chevron.png").as_posix()}); width: 10px; height: 10px; }}
    QComboBox QAbstractItemView {{ background: {c.surface}; border: 1px solid {c.border}; outline: 0;
                                   selection-background-color: {c.accent_soft}; selection-color: {c.text}; }}

    QPushButton, QToolButton#plain {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 3px;
        padding: 5px 14px; min-height: 18px; }}
    QPushButton:hover, QToolButton#plain:hover {{ border-color: {c.dim}; }}
    QPushButton:pressed, QToolButton#plain:pressed {{ background: {c.accent_soft}; }}
    QPushButton:disabled, QToolButton#plain:disabled {{ color: {c.dim}; background: transparent; border-color: {c.raised}; }}
    QPushButton#primary {{ {grad} color: {c.on_accent}; border: 1px solid {c.accent}; font-weight: 600; }}
    QPushButton#primary:hover {{ {grad_hi} }}
    QPushButton#primary:pressed {{ background: {c.accent}; }}
    QPushButton#primary:disabled {{ background: {c.raised}; color: {c.dim}; border-color: {c.raised}; font-weight: 500; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    QToolButton#convtitle {{ background: transparent; border: none; border-radius: 2px; padding: 2px 6px; }}
    QToolButton#convtitle:hover {{ background: {c.raised}; }}
    QToolButton#convtitle:pressed, QToolButton#convtitle:open {{ background: {c.accent_soft}; }}

    QCheckBox#chip {{ background: transparent; border: 1px solid transparent; border-bottom: 2px solid transparent;
                      border-radius: 0; padding: 3px 8px; min-height: 18px; color: {c.dim}; }}
    QCheckBox#chip:hover {{ color: {c.text}; }}
    QCheckBox#chip:checked {{ color: {c.text}; border-bottom-color: {c.accent}; font-weight: 600; }}
    QCheckBox#chip::indicator {{ width: 0; height: 0; border: none; image: none; background: transparent; margin: 0; }}
    QCheckBox#chip {{ spacing: 0; }}
    QToolBar QToolButton#more {{ border-radius: 3px; padding: 4px 4px; min-width: 22px; font-size: 16px; font-weight: 700; }}
    QFrame#detailsBox {{ background: {c.surface}; }}
    QLabel#hint {{ background: {c.surface}; color: {c.text}; border: none; border-left: 3px solid {c.accent};
                   border-radius: 0; padding: 6px 10px; }}

    QFrame#tile {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 3px; }}
    QLabel#big {{ font-size: 24px; font-weight: 600; font-family: {MONO}; color: {c.text}; }}
    QLabel#hero {{ font-size: 28px; font-weight: 700; color: {c.text}; }}
    QLabel#toast {{ background: {c.raised}; color: {c.text}; border: 1px solid {c.border}; border-left: 3px solid {c.accent};
                    border-radius: 2px; padding: 7px 14px; }}

    QGroupBox {{ background: {c.surface}; border: 1px solid {c.border}; border-radius: 3px;
                 margin-top: 22px; padding: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 4px; top: 2px; color: {c.text}; font-weight: 600; }}

    QMenu {{ background: {c.surface}; border: 1px solid {c.border}; padding: 3px; }}
    QMenu::item {{ padding: 5px 22px 5px 12px; border-radius: 0; }}
    QMenu::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QMenu::separator {{ height: 1px; background: {c.border}; margin: 4px 6px; }}

    QStatusBar {{ background: {c.surface}; color: {c.dim}; border-top: 1px solid {c.border}; }}
    QStatusBar::item {{ border: none; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle {{ background: {c.border}; border-radius: 0; min-height: 28px; min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {c.dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


# Added after the main sheet, so these win.
compact = False
COMPACT = """
    QToolBar { padding: 2px 6px; spacing: 3px; }
    QToolBar QToolButton { padding: 2px 8px; min-height: 16px; }
    QToolBar QToolButton#more { padding: 2px 4px; }
    QPushButton, QToolButton#plain { padding: 2px 10px; min-height: 16px; }
    QLineEdit, QPlainTextEdit, QComboBox { padding: 3px 8px; }
    QLineEdit#round { padding: 2px 8px; min-height: 16px; }
    QCheckBox#chip { padding: 1px 6px; min-height: 15px; }
    QTabBar::tab { padding: 3px 9px 2px 9px; margin: 2px 2px 0 2px; min-height: 14px; }
    QListWidget::item { padding: 3px 6px; }
    QTreeWidget::item { padding: 1px 2px; }
    QHeaderView::section { padding: 2px 4px; }
    QGroupBox { margin-top: 16px; padding: 4px; }
    QMenu::item { padding: 3px 18px 3px 10px; }
    QLabel#big { font-size: 19px; }
    QLabel#hero { font-size: 22px; }
"""


def accent_gradient(rect: QRectF) -> QLinearGradient:
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    g.setColorAt(0, QColor(current.accent))
    g.setColorAt(1, QColor(current.accent2))
    return g


def draw_avatar(p: QPainter, rect: QRectF, name: str, channel: bool = False):
    """A small squared tag with the name's first letter, muted. Channels get a # on the accent."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    if channel:
        p.setBrush(QColor(current.accent_soft))
        ink = QColor(current.accent if not current.dark else current.text)
    else:
        hue = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 360
        p.setBrush(QColor.fromHsl(hue, 60, 70 if current.dark else 205))
        ink = QColor.fromHsl(hue, 90, 215 if current.dark else 60)
    p.drawRoundedRect(rect, 3, 3)
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
    p.drawRoundedRect(box, s * 0.08, s * 0.08)
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
    global current, accent_name, MONO
    MONO = f"'{QFontDatabase.systemFont(QFontDatabase.FixedFont).family()}'"
    if accent in ACCENTS:
        accent_name = accent
    current = replace(DARK if dark else LIGHT, **ACCENTS[accent_name][dark])
    app.setStyle("Fusion")  # the one built-in style that honours all of this on Linux and Windows
    app.setPalette(_palette(current))
    app.setStyleSheet(_sheet(current) + (COMPACT if compact else ""))
    for fn in list(on_change):
        fn()


def set_compact(app: QApplication, on: bool):
    global compact
    compact = on
    apply(app, current.dark)


def follow_system(app: QApplication, accent: str = "Signal green"):
    """Must run before apply() changes the palette it reads."""
    scheme = app.styleHints().colorScheme()
    dark = scheme == Qt.ColorScheme.Dark if scheme != Qt.ColorScheme.Unknown else (
        app.palette().color(QPalette.Window).lightness() < 128)
    apply(app, dark, accent)

    def changed(s):
        if s != Qt.ColorScheme.Unknown and (s == Qt.ColorScheme.Dark) != current.dark:
            apply(app, s == Qt.ColorScheme.Dark)
    app.styleHints().colorSchemeChanged.connect(changed)
