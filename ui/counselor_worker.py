"""
Background modding counselor: local Llama/Gemma (same endpoint as ID search) off the MO2 UI thread.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from PyQt6.QtCore import QThread, pyqtSignal

from ..ai.llm_client import LLMConnectionError, LLMParseError, _diag, complete_chat_plain_text
from .counselor_knowledge import COUNSELOR_KNOWLEDGE_EXTRA

_COUNSELOR_MAX_HISTORY_MESSAGES = 20  # 최근 10턴(user+assistant 쌍)

# 스카이림 SE 게임 실행 파일 버전(일반적으로 ProductVersion) → 권장 SKSE64 빌드 표기
_SKSE64_BY_GAME_EXE_RAW: dict[str, str] = {
    "1.6.1170.0": "SKSE64 2.2.6",
    "1.6.1130.0": "SKSE64 2.2.5",
    "1.6.659.0": "SKSE64 2.2.3",
    "1.5.97.0": "SKSE64 2.0.20 (LE 호환 버전)",
}

# 폴아웃4 게임 실행 파일 버전 → 권장 F4SE 빌드 표기
_F4SE_BY_GAME_EXE_RAW: dict[str, str] = {
    "1.10.984.0": "F4SE 0.7.2",
    "1.10.163.0": "F4SE 0.6.23",
}


def _expand_trailing_zero_aliases(raw: dict[str, str]) -> dict[str, str]:
    """같은 매핑에 대해 마지막 세그먼트 .0 유무 등 짧은 형태를 허용."""
    out = dict(raw)
    for k, v in raw.items():
        if k.endswith(".0"):
            short = k[:-2]
            out.setdefault(short, v)
    return out


_SKSE64_BY_GAME_EXE = _expand_trailing_zero_aliases(_SKSE64_BY_GAME_EXE_RAW)
_F4SE_BY_GAME_EXE = _expand_trailing_zero_aliases(_F4SE_BY_GAME_EXE_RAW)


def _lookup_in_version_map(game_version: str, mapping: dict[str, str]) -> str | None:
    v = (game_version or "").strip()
    if not v:
        return None
    if v in mapping:
        return mapping[v]
    # 마지막 .0만 제거해 한 번 더 시도 (예: 1.6.1170.0 vs 1.6.1170)
    if v.endswith(".0"):
        v2 = v[:-2]
        if v2 in mapping:
            return mapping[v2]
    return None


def lookup_script_extender_build(game_domain: str, game_version: str) -> str | None:
    """
    Nexus game domain + 게임 exe 버전 문자열로 권장 SKSE64 / F4SE 빌드 표기를 돌려준다.
    표에 없으면 None.
    """
    dom = (game_domain or "").strip().lower()
    if dom == "skyrimspecialedition":
        return _lookup_in_version_map(game_version, _SKSE64_BY_GAME_EXE)
    if dom == "fallout4":
        return _lookup_in_version_map(game_version, _F4SE_BY_GAME_EXE)
    return None


def format_counselor_reply_html_body(plain: str) -> str:
    """
    모델이 넣은 ``<b>``, ``<span>`` 등을 그대로 두고 줄바꿈만 ``<br/>``로 바꾼 뒤 ``_append_html``에 넣는다.
    (전체 이스케이프는 하지 않아 태그가 렌더된다.)
    """
    t = (plain or "").replace("\r\n", "\n")
    return t.replace("\n", "<br/>")


COUNSELOR_SYSTEM_PROMPT_STATIC = (
    "너는 스카이림 SE, 폴아웃4, MO2 전문 모딩 상담사야.\n"
    "반드시 다음 규칙을 따라줘.\n\n"
    "1. 항상 이전 대화 내용을 참고해서 맥락에 맞게 답해줘.\n"
    "2. 모드를 추천할 때는 반드시 구체적인 모드 이름을 말해줘. "
    "두루뭉술하게 '이런 종류의 모드를 찾아보세요' 식으로 답하지 마.\n"
    "3. 링크는 직접 주지 마. 단 모드 이름과 넥서스 검색 방법은 알려줘.\n"
    "4. 답변은 5문장 이내로 짧고 명확하게.\n"
    "5. 전문용어는 괄호 안에 쉬운 설명을 달아줘.\n"
    "6. 이모지, 마크다운, 번호 매기기 사용 금지.\n"
    "7. 사용자가 초보라고 하면 가장 대중적이고 안전한 모드를 먼저 추천해줘.\n"
    "8. 옷 물리, 헤어 물리 관련 질문 시 HDT-SMP를 우선 추천해줘.\n"
    "9. 애니메이션 관련 질문 시 Nemesis를 우선 추천해줘.\n"
    "10. 바디 관련 질문 시 CBBE(초보자 추천) 또는 BHUNP를 추천해줘.\n\n"
    "출력 가독성: 문장이 끝날 때마다 줄바꿈을 해줘. "
    "한 문단은 최대 2문장으로 제한해줘. "
    "문단과 문단 사이에는 빈 줄을 하나 넣어줘.\n\n"
    "HTML 출력(답변 본문): 답변에서 모드 이름이 나올 때는 반드시 "
    "<b>모드이름</b> 형태로 감싸줘. "
    "예: <b>SKSE64</b>, <b>HDT-SMP</b>, <b>SkyUI</b>. "
    "주의사항이나 경고성 내용은 "
    "<span style='color:#ef6c00'>내용</span> 으로 감싸줘. "
    "그 외 일반 텍스트는 태그 없이 평문으로 써줘.\n\n"
    "모딩과 관련 없는 질문이면 정중히 모딩 관련 질문만 답할 수 있다고 짧게 안내해.\n"
    "아래에 실행 환경(게임 버전·권장 SKSE64/F4SE) 정보가 이어지면 호환 질문에 그 정보를 반영하고, "
    "표에 없는 버전이면 공식 SKSE/F4SE 배포의 호환 표를 사용자가 직접 확인하도록 짧게 안내해."
) + COUNSELOR_KNOWLEDGE_EXTRA


def build_counselor_system_prompt(
    *,
    game_display_name: str = "",
    game_version: str = "",
    game_domain: str = "",
    guide_system_context: Mapping[str, Any] | None = None,
) -> str:
    """정적 규칙 + MO2에서 넘긴 게임 버전·매핑 힌트 + (선택) 가이드 설치 세션 컨텍스트."""
    parts: list[str] = [COUNSELOR_SYSTEM_PROMPT_STATIC.strip()]
    name = (game_display_name or "").strip()
    ver = (game_version or "").strip()
    dom = (game_domain or "").strip().lower()
    if name or ver:
        block_lines = ["실행 환경(MO2가 보고한 값, 답변에 활용 가능):"]
        if name:
            block_lines.append(f"- 게임 이름: {name}")
        if ver:
            block_lines.append(f"- 게임 버전(실행 파일 또는 MO2 버전 표시): {ver}")
        hint = lookup_script_extender_build(dom, ver)
        if hint:
            block_lines.append(f"- 위 게임 버전에 대응하는 권장 스크립트 확장 빌드(내부 참고 표): {hint}")
        else:
            if dom in ("skyrimspecialedition", "fallout4") and ver:
                block_lines.append(
                    "- 알려진 SKSE64/F4SE 매핑 표에 없는 버전이면, 공식 배포 페이지의 호환 표를 확인하라고 안내해."
                )
        parts.append("\n".join(block_lines))
    g = guide_system_context
    if isinstance(g, Mapping) and g:
        gm = str(g.get("main_mod") or "").strip()
        ct = str(g.get("current_target") or "").strip()
        done = g.get("completed_prereqs")
        rem = g.get("remaining_prereqs")
        if not isinstance(done, list):
            done = []
        if not isinstance(rem, list):
            rem = []
        done_s = ", ".join(str(x) for x in done) if done else "(없음)"
        rem_s = ", ".join(str(x) for x in rem) if rem else "(없음)"
        parts.append(
            "가이드 설치 모드(세션 컨텍스트 — 반드시 참고해 답변):\n"
            f"- 목표 메인 모드: {gm or '(이름 없음)'}\n"
            f"- 지금 안내 중인 사전 요구: {ct or '(없음)'}\n"
            f"- 이미 완료된 사전 요구: {done_s}\n"
            f"- 아직 남은 사전 요구(현재 항목 제외): {rem_s}\n"
            "위 순서와 설치 상태를 존중하고, 이미 완료된 항목을 다시 요구하지 마."
        )
    return "\n\n".join(parts)


class CounselorWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str, bool)

    def __init__(
        self,
        *,
        user_message: str,
        base_url: str,
        prior_messages: Sequence[Mapping[str, str]] | None = None,
        game_display_name: str = "",
        game_version: str = "",
        game_domain: str = "",
        guide_system_context: Mapping[str, Any] | None = None,
        request_timeout: float = 90.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._user_message = (user_message or "").strip()
        self._prior_messages: tuple[Mapping[str, str], ...] = tuple(prior_messages or ())
        self._base_url = base_url.rstrip("/")
        self._request_timeout = float(request_timeout)
        self._game_display_name = (game_display_name or "").strip()
        self._game_version = (game_version or "").strip()
        self._game_domain = (game_domain or "").strip().lower()
        self._guide_system_context: Mapping[str, Any] | None = (
            guide_system_context if isinstance(guide_system_context, Mapping) else None
        )

    def run(self) -> None:
        try:
            if not self._user_message:
                self.failed.emit("질문이 비어 있습니다.", False)
                return
            system_prompt = build_counselor_system_prompt(
                game_display_name=self._game_display_name,
                game_version=self._game_version,
                game_domain=self._game_domain,
                guide_system_context=self._guide_system_context,
            )
            ai_text, lat_ms = complete_chat_plain_text(
                base_url=self._base_url,
                system_prompt=system_prompt,
                user_prompt=self._user_message,
                prior_chat_messages=self._prior_messages,
                max_prior_messages=_COUNSELOR_MAX_HISTORY_MESSAGES,
                request_timeout=max(30.0, self._request_timeout - 5.0),
                max_tokens=2048,
                temperature=0.35,
            )
            _diag(f"COUNSELOR LLM ok latency_ms={lat_ms} chars={len(ai_text)}")
            self.finished_ok.emit(ai_text.strip())
        except LLMConnectionError as exc:
            self.failed.emit(str(exc), True)
        except LLMParseError as exc:
            self.failed.emit(str(exc), False)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)
