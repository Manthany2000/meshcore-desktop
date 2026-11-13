"""Picking a flood scope: the region a channel's messages, or the node's floods,
are kept to. Repeaters relay a scoped flood only if they allow its region, so a
scope keeps local chatter local."""
from __future__ import annotations

import html

from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QRadioButton,
                               QVBoxLayout)

from . import protocol as P
from . import theme
from .model import NO_SCOPE

ABOUT = ("Repeaters set up with regions only relay a scoped flood if they allow its region, so a scope keeps "
         "messages inside an area. Ask your local mesh which region names its repeaters use.")


def scope_text(scope: str, default: str | None = None) -> str:
    """How a channel's scope reads in a subtitle or menu."""
    if scope == NO_SCOPE:
        return "no scope"
    if scope:
        return f"#{scope}"
    return f"node default (#{default})" if default else "node default (none)"


class ScopeDialog(QDialog):
    """For a channel: the node's default, no scope, or a region. For the node's
    default: none, or a region. result_ is the scope as the model keeps it."""

    def __init__(self, parent, title: str, intro: str, current: str, node_default: str | None = None,
                 for_channel: bool = True, unscoped: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.result_: str | None = None
        self.for_channel = for_channel
        v = QVBoxLayout(self)
        lbl = QLabel(intro + "<br><br><span style='opacity:.7'>" + html.escape(ABOUT) + "</span>", wordWrap=True)
        lbl.setMinimumWidth(380)
        v.addWidget(lbl)
        grp = QButtonGroup(self)
        self.default = QRadioButton(
            f"The node's default scope ({'#' + node_default if node_default else 'none set'})"
            if for_channel else "No default: floods go out unscoped unless a channel has one")
        self.none = QRadioButton("No scope: every repeater may relay it")
        self.region = QRadioButton("Region:")
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. usa, ma, boston")
        self.name.setMaxLength(P.SCOPE_NAME_MAX)
        for b in (self.default, self.none, self.region):
            grp.addButton(b)
        v.addWidget(self.default)
        if for_channel:
            v.addWidget(self.none)
            self.none.setEnabled(unscoped)
            if not unscoped:
                self.none.setToolTip("Needs newer firmware on the node (protocol v12)")
        else:
            self.none.hide()
        row = QHBoxLayout()
        row.addWidget(self.region)
        row.addWidget(self.name, 1)
        v.addLayout(row)
        self.err = QLabel("", wordWrap=True)
        self.err.setStyleSheet(f"color:{theme.current.danger}")
        v.addWidget(self.err)
        if current == NO_SCOPE and for_channel:
            self.none.setChecked(True)
        elif current and current != NO_SCOPE:
            self.region.setChecked(True)
            self.name.setText(current)
        else:
            self.default.setChecked(True)
        self.name.textEdited.connect(lambda _t: self.region.setChecked(True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _ok(self):
        if self.region.isChecked():
            if problem := P.region_problem(self.name.text()):
                self.err.setText(problem)
                return
            self.result_ = P.region_name(self.name.text())
        else:
            self.result_ = NO_SCOPE if self.none.isChecked() else ""
        self.accept()
