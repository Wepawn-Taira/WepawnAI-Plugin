"""
가이드 모드: 사용자가 넥서스 모드 ID(숫자)를 입력했을 때만 짧은 안내 HTML을 만든다.
Nexus API·LLM·전체 모드 정보 출력 없음.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import QThread, pyqtSignal


def build_guide_step_message_html(_main_mod_name: str, current_prereq_name: str) -> str:
    p = html.escape((current_prereq_name or "").strip() or "(사전 모드)", quote=False)
    return (
        "<p><b>"
        f"{p}</b> 모드 페이지를 열었습니다.<br/>"
        "이 모드의 넥서스 페이지 <b>Requirements</b> 목록에서<br/>"
        "설치가 필요한 항목을 클릭한 뒤,<br/>"
        "열린 페이지 주소창 맨 끝 숫자를<br/>"
        "여기에 입력해 주세요.<br/>"
        "다음에도 <b>주소창 맨 끝 숫자</b>를 이 채팅에 입력해 주시면 됩니다.</p>"
    )


class GuideStepWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str, bool)

    def __init__(
        self,
        *,
        main_mod_name: str,
        current_prereq_name: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._main_mod_name = main_mod_name
        self._current_prereq_name = current_prereq_name

    def run(self) -> None:
        try:
            out = build_guide_step_message_html(self._main_mod_name, self._current_prereq_name)
            self.finished_ok.emit(out)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)
