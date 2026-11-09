"""The main views as tiled panels: buttons along the top open and close them,
each has its own close button, and dragging one by its title onto another swaps
the two. A panel too small for its contents scrolls, so a crowded layout can't
force the window wider than the screen."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QScrollArea, QSplitter, QToolButton, QVBoxLayout, QWidget)

from . import theme

DRAG_START = 8  # pixels, so a plain click on a title doesn't count


def columns(shown: list) -> tuple[list, list]:
    """Which panels go left and right: three leave the first one tall, four make a grid."""
    return (shown[::2], shown[1::2]) if len(shown) == 4 else (shown[:1], shown[1:])


def swapped(order: list[int], a: int, b: int) -> list[int]:
    return [b if x == a else a if x == b else x for x in order]


class Header(QWidget):
    """A panel's title bar. It tracks the drag itself, because a web view
    underneath would swallow Qt's drag and drop events."""

    def __init__(self, pane: "Pane", space: "Workspace"):
        super().__init__()
        self.pane, self.space = pane, space
        self.start: QPoint | None = None
        self.dragging = False
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag onto another panel to swap places")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.start = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self.start is None:
            return
        at = e.globalPosition().toPoint()
        if not self.dragging and (at - self.start).manhattanLength() >= DRAG_START:
            self.dragging = True
            self.setCursor(Qt.ClosedHandCursor)
        if self.dragging:
            self.space.drag_over(self.pane, at)

    def mouseReleaseEvent(self, e):
        if self.dragging:
            self.space.drop(self.pane, e.globalPosition().toPoint())
            self.setCursor(Qt.OpenHandCursor)
        self.start = None
        self.dragging = False


class Pane(QFrame):
    def __init__(self, widget: QWidget, title: str, close, space: "Workspace", index: int):
        super().__init__(objectName="tile")
        self.widget = widget
        self.index = index
        self.is_open = False
        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)
        header = Header(self, space)
        head = QHBoxLayout(header)
        head.setContentsMargins(14, 5, 6, 3)
        self.title = QLabel(title)
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)  # so the title drags
        x = QToolButton(text="×", objectName="plain", toolTip=f"Close {title}")
        x.setStyleSheet("padding: 0; min-width: 0; font-size: 16px; border-radius: 10px;")  # the theme's padding leaves no room
        x.setFixedSize(30, 26)
        x.clicked.connect(close)
        head.addWidget(self.title, 1)
        head.addWidget(x)
        self.head, self.close_btn = head, x
        v.addWidget(header)
        scroll = QScrollArea(frameShape=QFrame.NoFrame, widgetResizable=True)
        scroll.setWidget(widget)
        v.addWidget(scroll, 1)


class Workspace(QWidget):
    changed = Signal()  # panels opened, closed or swapped
    opened = Signal(QWidget)

    def __init__(self):
        super().__init__()
        self.panes: list[Pane] = []
        self.chips: list[QCheckBox] = []
        self.order: list[int] = []  # pane indexes, in the order they tile
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)
        self.bar = QHBoxLayout()
        self.bar.addStretch(1)
        v.addLayout(self.bar)
        self.root = QSplitter(Qt.Horizontal, handleWidth=6, childrenCollapsible=False)
        self.left = QSplitter(Qt.Vertical, handleWidth=6, childrenCollapsible=False)
        self.right = QSplitter(Qt.Vertical, handleWidth=6, childrenCollapsible=False)
        self.root.addWidget(self.left)
        self.root.addWidget(self.right)
        v.addWidget(self.root, 1)
        self.empty = QLabel("Everything is closed. Pick a view above.", objectName="dim", alignment=Qt.AlignCenter)
        v.addWidget(self.empty, 1)
        self.marker = QFrame(self)  # shows where a dragged panel would land
        self.marker.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.marker.hide()
        self._arrange()

    def addTab(self, widget: QWidget, title: str):
        i = len(self.panes)
        pane = Pane(widget, title, lambda: self.set_open(i, False), self, i)
        pane.hide()
        self.left.addWidget(pane)
        chip = QCheckBox(title, objectName="chip", toolTip="Open or close this panel")
        chip.clicked.connect(lambda on: self.set_open(i, on))
        self.bar.insertWidget(i, chip)
        self.panes.append(pane)
        self.chips.append(chip)
        self.order.append(i)

    def set_open(self, i: int, on: bool):
        pane = self.panes[i]
        self.chips[i].setChecked(on)
        if pane.is_open == on:
            return
        pane.is_open = on
        self._arrange()
        self.changed.emit()
        if on:
            self.opened.emit(pane.widget)

    def set_compact(self, on: bool):
        outer = 3 if on else 8
        self.layout().setContentsMargins(outer, outer, outer, outer)
        self.layout().setSpacing(3 if on else 8)
        for p in self.panes:
            p.head.setContentsMargins(*((8, 1, 3, 0) if on else (14, 5, 6, 3)))
            p.close_btn.setFixedSize(*((24, 20) if on else (30, 26)))
            lay = p.widget.layout()
            if lay is None:
                continue
            if not hasattr(p, "roomy"):  # what the view was built with, to go back to
                p.roomy = lay.contentsMargins(), lay.spacing()
            m, spacing = p.roomy
            lay.setContentsMargins(*((min(m.left(), 6), min(m.top(), 3), min(m.right(), 6), min(m.bottom(), 3)) if on else
                                     (m.left(), m.top(), m.right(), m.bottom())))
            lay.setSpacing(min(spacing, 4) if on else spacing)

    def _arrange(self):
        shown = [self.panes[i] for i in self.order if self.panes[i].is_open]
        for p in self.panes:
            p.setVisible(p.is_open)
        left, right = columns(shown)
        for column, panes in ((self.left, left), (self.right, right)):
            for pos, p in enumerate(panes):
                if column.indexOf(p) != pos:
                    column.insertWidget(pos, p)
            column.setVisible(bool(panes))
            column.setSizes([1000 if column.widget(k).isVisibleTo(column) else 0 for k in range(column.count())])
        self.root.setSizes([1000, 1000])
        self.root.setVisible(bool(shown))
        self.empty.setVisible(not shown)

    # --- swapping --------------------------------------------------------------

    def _target(self, source: Pane, at: QPoint) -> Pane | None:
        for p in self.panes:
            if p is not source and p.is_open and p.rect().contains(p.mapFromGlobal(at)):
                return p
        return None

    def drag_over(self, source: Pane, at: QPoint):
        p = self._target(source, at)
        if p is None:
            self.marker.hide()
            return
        c = QColor(theme.current.accent)
        self.marker.setStyleSheet(f"background: rgba({c.red()}, {c.green()}, {c.blue()}, 70);"
                                  f"border: 2px solid {theme.current.accent}; border-radius: 12px;")
        self.marker.setGeometry(QRect(p.mapTo(self, QPoint(0, 0)), p.size()))
        self.marker.show()
        self.marker.raise_()

    def drop(self, source: Pane, at: QPoint):
        self.marker.hide()
        p = self._target(source, at)
        if p is not None:
            self.order = swapped(self.order, source.index, p.index)
            self._arrange()
            self.changed.emit()

    def state(self) -> str:
        return ",".join(map(str, self.order))

    def restore(self, text: str):
        """Ignores anything that isn't a full ordering of the panels."""
        parts = text.split(",")
        if all(x.isdigit() for x in parts) and sorted(map(int, parts)) == list(range(len(self.panes))):
            self.order = [int(x) for x in parts]

    # --- what the rest of the app calls ---------------------------------------

    def is_open(self, widget: QWidget) -> bool:
        return self.panes[self.indexOf(widget)].is_open

    def open_indexes(self) -> list[int]:
        return [i for i, p in enumerate(self.panes) if p.is_open]

    # Opening is how the rest of the app "goes to" a view.
    def setCurrentIndex(self, i: int):
        self.set_open(i, True)

    def setCurrentWidget(self, widget: QWidget):
        self.set_open(self.indexOf(widget), True)

    def indexOf(self, widget: QWidget) -> int:
        return next(i for i, p in enumerate(self.panes) if p.widget is widget)

    def setTabText(self, i: int, text: str):
        self.chips[i].setText(text)
        self.panes[i].title.setText(text)
