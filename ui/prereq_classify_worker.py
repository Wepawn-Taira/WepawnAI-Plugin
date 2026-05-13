"""
사전 요구 모드별 Nexus mod.json + LLM으로 필수/선택 판별 (UI 스레드 밖에서 실행).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from PyQt6.QtCore import QThread, pyqtSignal

from ..ai.llm_client import LLMConnectionError, LLMParseError, complete_chat_plain_text
from ..nexus.dependencies import fetch_nexus_mod_record
from ..utils.hard_log import _hard_log

_CLASSIFY_SYSTEM_KO = '당신은 스카이림 모드 설치를 도와주는 전문가입니다.\n제공된 [분류 대상 사전 요구 항목]과 [본문 설명 800자]를 교차 검증하라.\n본문에 \'A 또는 B(OR)\'·대안 중 하나·특정 바디/툴 전용 등 조건이 명시되어 있으면,\n단일 필수 모드로 맹목 분류하지 말고 묶어서 통합 판단하라.\n\n초보 유저가 잘못된 모드를 설치하면 게임이 망가질 수 있어 정확성이 매우 중요합니다.\n각 모드 설명을 꼬미꼬미히 읽고, 해당 모드가 모든 유저에게 반드시 필요한지,\n아니면 특정 조건이나 취향에 따라 선택적으로 필요한지를 판단하세요.\n\n선택적이라면 어떤 상황에서 필요한지 한 줄로 명확히 설명하고, 그 내용을 JSON의 "reason" 필드에 적는다.\n필수 여부가 불분명하면 type을 MANDATORY로 분류한다.\n\'또는\'으로 이어진 대안 바디·도구 등은 type "OR" 과 options 배열로 한 요구로 묶는다.\n\n이번 사용자 메시지의 대상 항목 하나에 대해, 아래 스키마를 따르는 객체 단 하나만 원소로 하는 JSON 배열을 출력한다.\n'

_CLASSIFY_SCHEMA_BLOCK = 'You MUST respond ONLY with a valid JSON array. Do not include any explanations, markdown formatting, or conversational text. Use the following strict schema:\n\nSingle mode: {"type": "MANDATORY" | "OPTIONAL", "label": "모드명", "id": 12345}\n(optional string field "reason" for one-line rationale.)\n\nOR branch: {"type": "OR", "options": [{"label": "CBBE", "id": 198}, {"label": "TBD", "id": 20024}]}\n(optional string field "reason" on the OR object.)\n\nExample valid response:\n[{"type": "MANDATORY", "label": "Example", "id": 1, "reason": "All users need this."}]'

_CLASSIFY_SYSTEM = _CLASSIFY_SYSTEM_KO + "\n" + _CLASSIFY_SCHEMA_BLOCK


def _extract_classify_json_array(raw: str) -> tuple[list[Any], bool]:
    """
    Bracket-slice + json.loads; empty list on failure.
    Returns (json_data, used_fallback).
    """
    text = raw or ""
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end >= start:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return [], True
        if isinstance(parsed, list):
            return parsed, False
        return [], True
    return [], True


def _coerce_positive_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        i = int(val)
    except (TypeError, ValueError):
        return None
    return i if i > 0 else None


def _kind_reason_from_classify_json(
    json_data: list[Any],
    *,
    lab: str,
    nid: int,
) -> tuple[str, str, bool, list[Any] | None]:
    """Map strict-schema array (one object) to kind, reason, OR flag, options (JIT)."""
    if not json_data:
        return "MANDATORY", 'JSON 파싱 결과 없음 — 필수로 간주', False, None

    first: dict[str, Any] | None = None
    for el in json_data:
        if isinstance(el, dict):
            first = el
            break
    if first is None:
        return "MANDATORY", 'JSON 배열에 객체가 없음 — 필수로 간주', False, None

    t = str(first.get("type") or "").strip().upper()
    reason_raw = str(first.get("reason") or "").strip()

    if t == "OR":
        opts = first.get("options")
        if isinstance(opts, list) and opts:
            return (
                "MANDATORY",
                reason_raw or '본문 기준 대안 중 하나를 선택하면 됩니다.',
                True,
                opts,
            )
        return (
            "MANDATORY",
            reason_raw or 'OR options 비어 있음 — 필수로 간주',
            True,
            None,
        )

    if t in ("MANDATORY", "OPTIONAL"):
        jid = _coerce_positive_int(first.get("id"))
        if jid is not None and jid != nid:
            _hard_log(
                f"[CLASSIFY DIAG] JSON id={jid}과 가이드 라벨={lab!r} nexus_mod_id={nid} 불일치 — JSON type 기준으로 계속"
            )
        default_r = '필수로 간주' if t == "MANDATORY" else '선택 사항'
        return t, reason_raw or default_r, False, None

    return (
        "MANDATORY",
        reason_raw or f"인식할 수 없는 type={t!r} — 필수로 간주",
        False,
        None,
    )


class PrereqClassifyWorker(QThread):
    """``prereq_classify_flat`` 항목마다 mod.json + LLM으로 필수/선택 분류."""

    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str, bool)

    def __init__(
        self,
        *,
        items: list[Mapping[str, Any]],
        game_domain: str,
        api_key: str,
        application: str,
        base_url: str,
        request_timeout: float = 75.0,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._items = list(items)
        self._game_domain = game_domain
        self._api_key = api_key
        self._application = application
        self._base_url = base_url
        self._request_timeout = float(request_timeout)

    def run(self) -> None:
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        try:
            for raw in self._items:
                if not isinstance(raw, Mapping):
                    continue
                lab = str(raw.get("label") or "").strip()
                try:
                    nid = int(raw.get("nexus_mod_id"))
                except (TypeError, ValueError):
                    continue
                if nid <= 0 or nid in seen:
                    continue
                seen.add(nid)
                rec = fetch_nexus_mod_record(
                    self._api_key,
                    self._game_domain,
                    nid,
                    application=self._application,
                    timeout=min(30.0, self._request_timeout),
                )
                if rec is None:
                    out.append(
                        {
                            "label": lab,
                            "nexus_mod_id": nid,
                            "kind": "MANDATORY",
                            "reason": '모드 정보를 가져오지 못해 필수로 간주함',
                        }
                    )
                    continue
                desc_plain = (rec.description_plain or "").strip()
                desc_800 = desc_plain[:800]
                body = (
                    "[분류 대상 사전 요구 항목]\n"
                    f"가이드 라벨: {lab}\n"
                    f"Nexus mod id: {nid}\n"
                    f"API 표시 이름: {rec.name}\n"
                    f"요약: {(rec.summary or '').strip()}\n\n"
                    "[본문 설명 800자]\n"
                    f"{desc_800}"
                )
                user_prompt = (
                    "아래 [분류 대상 사전 요구 항목]과 [본문 설명 800자]를 함께 보고, "
                    "이 모드가 메인 모드 설치·사용 전에 필수인지 선택인지 판단해.\n\n"
                    f"{body}"
                )
                try:
                    ai_line, _lat = complete_chat_plain_text(
                        base_url=self._base_url,
                        system_prompt=_CLASSIFY_SYSTEM,
                        user_prompt=user_prompt,
                        request_timeout=max(25.0, self._request_timeout - 10.0),
                        max_tokens=384,
                        temperature=0.2,
                    )
                except (LLMConnectionError, LLMParseError) as exc:
                    out.append(
                        {
                            "label": lab,
                            "nexus_mod_id": nid,
                            "kind": "MANDATORY",
                            "reason": f"판단 실패: {exc}",
                        }
                    )
                    continue
                json_data, used_fallback = _extract_classify_json_array(ai_line)
                _hard_log(f"[CLASSIFY DIAG] 파싱된 JSON 길이: {len(json_data)}")
                if used_fallback:
                    _hard_log('[CLASSIFY DIAG] 폭백: JSON 배열 추출/디코드 실패 또는 비리스트')
                kind, reason, is_or, options = _kind_reason_from_classify_json(
                    json_data, lab=lab, nid=nid
                )
                entry: dict[str, Any] = {
                    "label": lab,
                    "nexus_mod_id": nid,
                    "kind": kind,
                    "reason": reason
                    or ('필수로 간주' if kind == "MANDATORY" else '선택 사항'),
                }
                if is_or and options:
                    entry["options"] = options
                out.append(entry)
            self.finished_ok.emit(out)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)
