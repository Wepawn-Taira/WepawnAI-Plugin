"""
Modal dialog: pick one Nexus prerequisite from mutually exclusive (OR) options.
"""

from __future__ import annotations

from typing import Any, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCommandLinkButton,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class AlternativeSelectorDialog(QDialog):
    """
    Application-modal dialog listing OR alternatives as large command-link buttons.

    ``options`` items are dicts with ``label``, ``id`` (nexus mod id), optional ``note``.
    """

    def __init__(
        self,
        parent: QWidget | None,
        options: Sequence[dict[str, Any]],
        *,
        window_title: str = "사전 요구 (대안 선택)",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(440)
        self._selected_index: int | None = None

        root = QVBoxLayout(self)
        hint = QLabel(
            "서로 바꿔 쓸 수 있는 사전 모드입니다. 설치할 <b>하나</b>만 선택하세요."
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(hint)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll.setMaximumHeight(420)

        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(4, 4, 4, 4)

        for i, raw in enumerate(options):
            if not isinstance(raw, dict):
                continue
            try:
                nid = int(raw.get("id"))
            except (TypeError, ValueError):
                nid = 0
            label = str(raw.get("label") or "").strip() or (f"Mod {nid}" if nid > 0 else f"옵션 {i + 1}")
            note = str(raw.get("note") or "").strip()
            desc = note if note else (f"Nexus Mod ID: {nid}" if nid > 0 else "")

            btn = QCommandLinkButton(label, inner)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            if desc:
                btn.setDescription(desc)
            # Long titles/descriptions: command link style + left alignment
            btn.setStyleSheet(
                "QCommandLinkButton { text-align: left; padding: 8px; }\n"
                "QCommandLinkButton::description { text-align: left; }"
            )
            btn.clicked.connect(lambda _checked=False, idx=i: self._accept_at(idx))
            inner_lay.addWidget(btn)

        inner_lay.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        box = QDialogButtonBox()
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        box.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        root.addWidget(box)

    def selected_index(self) -> int | None:
        return self._selected_index

    def _accept_at(self, idx: int) -> None:
        self._selected_index = int(idx)
        self.accept()
