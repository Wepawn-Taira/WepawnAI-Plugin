"""
Background Nexus mod.json fetch + LLM requirements extraction from description text.

가이드 모드에서 사용자가 사전 모드용 넥서스 ID(숫자)를 입력한 경우에는
``ui.guide_step_worker.GuideStepWorker``만 사용하고, 이 워커로 전체 모드 정보를
출력하지 않는다(모드 소개·설치 주의·호환성·사전 요구 전체 목록 등).
"""

from __future__ import annotations

import datetime as dt
import html
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping, Sequence

from PyQt6.QtCore import QThread, pyqtSignal

from ..ai.llm_client import LLMConnectionError, LLMParseError, _diag, complete_chat_plain_text
from ..game_context import loader_path_exists
from ..i18n import llm_system_prompt_language_line, tr
from ..utils.hard_log import _hard_log
from ..nexus.dependencies import (
    NexusAPIError,
    NEXUS_REST_V1,
    _coerce_mod_id,
    _dependency_item_mappings,
    _files_list_root,
    _mod_json_root,
    _nexus_get_json,
    _pick_primary_file_id,
    _scrape_nexus_requirements_from_page,
    _strip_html_to_plain,
)


def _extract_version(root: Mapping[str, Any]) -> str:
    for k in ("version", "modVersion", "mod_version"):
        raw = root.get(k)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _api_domain_name(root: Mapping[str, Any]) -> str:
    dn = root.get("domain_name")
    if isinstance(dn, str) and dn.strip():
        return dn.strip().lower()
    game = root.get("game")
    if isinstance(game, dict):
        gdn = game.get("domain_name")
        if isinstance(gdn, str) and gdn.strip():
            return gdn.strip().lower()
    return ""


def _format_update_yyyy_mm_dd(ts: int | None) -> str:
    none = tr("id_search.version_none")
    if ts is None:
        return none
    try:
        d = dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return none
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


def _prereq_label_skip_tokens() -> frozenset[str]:
    return frozenset(
        x
        for x in (
            tr("id_search.prereq_empty_token").casefold(),
            tr("id_search.version_none").casefold(),
        )
        if x
    )


def _re_split_or() -> re.Pattern[str]:
    tok = tr("guide.or_label_token").strip()
    return re.compile(rf"\s*{re.escape(tok)}\s*", re.IGNORECASE)


def _looks_like_no_install_cautions(chunk: str) -> bool:
    t = (chunk or "").strip()
    if not t:
        return True
    compact = re.sub(r"\s+", "", t.lower())
    if len(t) < 120:
        for i in range(1, 7):
            phrase = tr(f"id_search.caution_compact_{i}")
            if phrase and phrase in compact:
                return True
    return False


def _parse_llm_sections(raw: str) -> tuple[str, str | None, str | None, str]:
    """Returns (intro_body, install_body_or_none, compat_body_or_none, prereq_slice_from_header)."""
    s = (raw or "").strip()
    if not s:
        return "", None, None, ""

    t_intro = tr("id_search.sec_intro")
    t_install = tr("id_search.sec_install")
    t_compat = tr("id_search.sec_compat")
    t_prereq = tr("id_search.sec_prereq")

    intro = ""
    if t_intro in s:
        a = s.index(t_intro) + len(t_intro)
        b = len(s)
        for tag in (t_install, t_compat, t_prereq):
            if tag in s[a:]:
                b = min(b, s.index(tag, a))
        intro = s[a:b].strip()

    inst: str | None = None
    if t_install in s:
        c = s.index(t_install) + len(t_install)
        d = len(s)
        for tag in (t_compat, t_prereq):
            if tag in s[c:]:
                d = min(d, s.index(tag, c))
        chunk = s[c:d].strip()
        if chunk and not _looks_like_no_install_cautions(chunk):
            inst = chunk

    compat: str | None = None
    if t_compat in s:
        e = s.index(t_compat) + len(t_compat)
        f = len(s)
        if t_prereq in s[e:]:
            f = s.index(t_prereq, e)
        cchunk = s[e:f].strip()
        if cchunk:
            compat = cchunk

    if t_prereq in s:
        prereq = s[s.index(t_prereq) :].strip()
    else:
        prereq = s
    return intro, inst, compat, prereq


def _any_mod_keywords(actives: Sequence[str], *substrings: str) -> bool:
    subs = [x.casefold() for x in substrings if x.strip()]
    if not subs:
        return False
    for a in actives:
        ac = a.casefold().strip()
        if not ac:
            continue
        for sub in subs:
            if sub in ac or ac in sub:
                return True
    return False


def _compat_line_satisfied(line: str, actives: Sequence[str]) -> bool:
    """Match MO2 active names (partial, case-insensitive) for body / FNIS·Nemesis / HDT-SMP·CBP style hints."""
    raw = (line or "").strip()
    if raw.startswith("-"):
        raw = raw[1:].strip()
    lc = raw.casefold()

    checks: list[bool] = []

    if "bhunp" in lc:
        checks.append(_any_mod_keywords(actives, "bhunp"))
    elif "cbbe" in lc or "caliente" in lc:
        checks.append(_any_mod_keywords(actives, "cbbe", "caliente"))
    elif "3bbb" in lc or "3ba" in lc or "3bau" in lc:
        checks.append(_any_mod_keywords(actives, "3bbb", "3ba", "3bau", "3baka"))
    elif re.search(r"(?:^|[^a-z])unp(?:[^a-z]|$)", lc) or " unp" in lc:
        checks.append(_any_mod_keywords(actives, "unp", "dimonized"))

    nem_alt = tr("id_search.compat_nemesis_alt")
    if "fnis" in lc or "nemesis" in lc or (nem_alt and nem_alt in raw):
        checks.append(_any_mod_keywords(actives, "fnis", "nemesis"))

    wants_smp = "hdt" in lc or "smp" in lc or "fsmp" in lc or "faster hdt" in lc
    wants_cbp = "cbp" in lc or "cbpc" in lc
    if wants_smp or wants_cbp:
        if tr("guide.or_separator") in raw and wants_smp and wants_cbp:
            checks.append(
                _any_mod_keywords(actives, "hdt", "smp", "fsmp", "faster")
                or _any_mod_keywords(actives, "cbp", "cbpc")
            )
        elif wants_cbp and not wants_smp:
            checks.append(_any_mod_keywords(actives, "cbp", "cbpc"))
        elif wants_smp:
            checks.append(_any_mod_keywords(actives, "hdt", "smp", "fsmp", "faster", "hdtsmp"))

    if not checks:
        return True
    return all(checks)


def _build_compat_section_html(compat_body: str | None, actives: Sequence[str]) -> str:
    if not (compat_body or "").strip():
        return ""
    items: list[str] = []
    for raw in compat_body.splitlines():
        t = raw.strip()
        if t.startswith("-"):
            items.append(t)
    if not items:
        return ""
    sec = html.escape(tr("id_search.sec_compat"), quote=False)
    lines_out: list[str] = [f"<br/><br/><b>{sec}</b><br/>"]
    for t in items:
        ok = _compat_line_satisfied(t, actives)
        sym = tr("id_search.compat_line_ok") if ok else tr("id_search.compat_line_need")
        safe = html.escape(t, quote=False)
        lines_out.append(f"{safe} {sym}<br/>")
    return "".join(lines_out)


def _format_install_caution_plain(block: str) -> str:
    """Ensure each non-empty line starts with '- '."""
    lines_out: list[str] = []
    for raw in block.splitlines():
        t = raw.strip()
        if not t:
            continue
        if not t.startswith("-"):
            t = f"- {t}"
        lines_out.append(t)
    return "\n".join(lines_out)


def _normalize_prereq_block(ai_text: str) -> str:
    """Ensure [사전 요구 모드] section with one bullet per line."""
    head = tr("id_search.sec_prereq")
    none_bullet = f"• {tr('id_search.prereq_empty_token')}"
    or_sep = tr("guide.or_separator")
    s = (ai_text or "").strip()
    if not s:
        return f"{head}\n{none_bullet}"

    if head in s:
        idx = s.index(head)
        rest = s[idx + len(head) :].lstrip()
        if not rest:
            return f"{head}\n{none_bullet}"
        lines_out: list[str] = [head]
        for raw in rest.splitlines():
            if not raw.strip():
                continue
            exp = raw.expandtabs(2)
            t = exp.strip()
            if not t:
                continue
            if t.startswith("•"):
                # 들여쓰기(2칸=한 단계)로 상·하위 사전요구 트리 표현 — strip 하지 않음
                lines_out.append(exp.rstrip())
            elif ("," in t or "，" in t) and or_sep not in t:
                for p in re.split(r"[,，]", t.replace("，", ",")):
                    p = p.strip()
                    if p:
                        lines_out.append(f"• {p}" if not p.startswith("•") else p)
            else:
                lines_out.append(t if t.startswith("•") else f"• {t}")
        if len(lines_out) == 1:
            lines_out.append(none_bullet)
        return "\n".join(lines_out)

    one = s.replace("，", ",")
    if "," in one and or_sep not in one:
        parts = [p.strip() for p in one.split(",") if p.strip()]
        if len(parts) > 1:
            return head + "\n" + "\n".join(f"• {p}" for p in parts)
    return f"{head}\n• {s}"


def _prereq_bullet_labels(prereq_plain: str) -> list[str]:
    """Lines under [사전 요구 모드] starting with • (body only)."""
    head = tr("id_search.sec_prereq")
    out: list[str] = []
    for raw in (prereq_plain or "").splitlines():
        if head in raw:
            continue
        t = raw.expandtabs(2).strip()
        if t.startswith("•"):
            body = t[1:].strip()
            if body:
                out.append(body)
    return out


_RE_PREREQ_NEXUS_SUFFIX = re.compile(r"\s*\[nexus:(\d+|\?)\]\s*$", re.IGNORECASE)
_RE_PREREQ_PRIORITY_SUFFIX = re.compile(r"\s*\[priority:([A-Za-z_]+)\]\s*$", re.IGNORECASE)
_RE_PREREQ_PRIORITY_ANYWHERE = re.compile(r"\[priority:[^\]]+\]", re.IGNORECASE)
# 긴 설명 본문은 "ENB 호환/트러블슈팅" 등으로 ENB를 언급하는 경우가 많아 제외한다.
_RE_ENB_WORD = re.compile(r"\benb\b", re.IGNORECASE)

# Nexus Requirements / Notes 스타일 분류(LLM이 본문·요약에서 판별해 태그)
PREREQ_PRIORITY_MANDATORY = "MANDATORY"
PREREQ_PRIORITY_MCM_ONLY = "MCM_ONLY"
PREREQ_PRIORITY_OPTIONAL = "OPTIONAL"
PREREQ_PRIORITY_ADVANCED = "ADVANCED"
_VALID_PREREQ_PRIORITIES: frozenset[str] = frozenset(
    {
        PREREQ_PRIORITY_MANDATORY,
        PREREQ_PRIORITY_MCM_ONLY,
        PREREQ_PRIORITY_OPTIONAL,
        PREREQ_PRIORITY_ADVANCED,
    }
)

_RE_REQ_BLOCK_NUMERIC_NEXUS = re.compile(r"\[nexus:(\d+)\]")


def _nexus_requirement_mod_ids_from_req_block(req_block: str | None) -> frozenset[int]:
    """API/스크랩으로 만든 ``req_block`` 줄에서 숫자 ``[nexus:ID]`` 만 수집 (탭 정식 행)."""
    if not (req_block or "").strip():
        return frozenset()
    ids: set[int] = set()
    for line in req_block.splitlines():
        for m in _RE_REQ_BLOCK_NUMERIC_NEXUS.finditer(line):
            try:
                i = int(m.group(1), 10)
            except ValueError:
                continue
            if i > 0:
                ids.add(i)
    return frozenset(ids)


def _coerce_priorities_for_nexus_tab_requirements(
    nodes: list[dict[str, Any]],
    nexus_tab_ids: frozenset[int],
) -> list[dict[str, Any]]:
    """
    Nexus Requirements 탭에 숫자 ID로 등록된 모드는 Notes 완화 문구와 무관하게 MANDATORY.
    LLM이 [priority:OPTIONAL] 등으로 내린 경우 덮어쓴다.
    """
    if not nexus_tab_ids:
        return nodes
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        kids_in = [x for x in (n.get("children") or []) if isinstance(x, dict)]
        kids_out = _coerce_priorities_for_nexus_tab_requirements(kids_in, nexus_tab_ids)
        nn = {**n, "children": kids_out}
        try:
            ni = int(nn.get("nexus_mod_id") or 0)
        except (TypeError, ValueError):
            ni = 0
        if ni > 0 and ni in nexus_tab_ids:
            prev = str(nn.get("priority") or "").strip().upper()
            if prev != PREREQ_PRIORITY_MANDATORY:
                _hard_log(
                    f"[PREREQ NEXUS_TAB] nexus_mod_id={ni} "
                    f"LLM priority {prev!r} -> MANDATORY (Requirements 탭 정식 ID)"
                )
            nn["priority"] = PREREQ_PRIORITY_MANDATORY
        out.append(normalize_prereq_item_dict(nn))
    return out


def split_prereq_nexus_suffix(line: str) -> tuple[str, int | None]:
    """
    ``•`` 줄 본문 끝의 ``[nexus:12345]`` 또는 ``[nexus:?]`` 를 분리한다.
    숫자면 mod id, ``?`` 또는 없으면 None.
    """
    s = (line or "").strip()
    m = _RE_PREREQ_NEXUS_SUFFIX.search(s)
    if not m:
        return s, None
    base = s[: m.start()].strip()
    g = m.group(1)
    if g == "?":
        return base, None
    try:
        return base, int(g, 10)
    except ValueError:
        return base, None


def _strip_priority_markers_from_label(label: str) -> str:
    """UI용 라벨에서 ``[priority:…]`` 제거(LLM이 태그 순서를 바꿔 넣은 경우)."""
    return _RE_PREREQ_PRIORITY_ANYWHERE.sub("", label or "").strip()


def _collect_nexus_mod_link_candidates(desc_html: str, desc_plain: str) -> list[tuple[int, str]]:
    """
    설명 HTML/평문에서 ``.../mods/{id}`` 링크와 앵커·주변 텍스트를 모은다.
    ``(mod_id, 비교용 힌트 문자열)`` — 같은 id가 여러 번 나올 수 있음.
    """
    pairs: list[tuple[int, str]] = []
    seen_pair: set[tuple[int, str]] = set()
    raw_html = desc_html or ""
    plain = desc_plain or ""

    def _add(mid: int, hint: str) -> None:
        t = re.sub(r"\s+", " ", (hint or "").strip())
        if len(t) < 1:
            return
        key = (mid, t[:500])
        if key in seen_pair:
            return
        seen_pair.add(key)
        pairs.append((mid, t[:500]))

    # BBCode: [url=.../mods/ID]Label[/url]
    for m in re.finditer(
        r"\[url\s*=\s*([^\]]*?/mods/(\d+)[^\]]*)\]\s*([\s\S]*?)\s*\[/url\]",
        raw_html,
        flags=re.IGNORECASE,
    ):
        inner = (m.group(3) or "").strip()
        hint = _strip_html_to_plain(inner, 600) if "<" in inner else inner
        _add(int(m.group(2)), hint)

    # HTML: <a href=".../mods/ID">...</a> (단순 한 단계)
    for m in re.finditer(
        r"<a\s[^>]*?\bhref\s*=\s*[\"']([^\"']*?/mods/(\d+)[^\"']*)[\"'][^>]*>([\s\S]*?)</a\s*>",
        raw_html,
        flags=re.IGNORECASE,
    ):
        inner = m.group(3) or ""
        hint = _strip_html_to_plain(inner, 600)
        _add(int(m.group(2)), hint)

    # 평문 URL + 앞뒤 맥락 (desc_plain)
    for m in re.finditer(
        r"(?:https?:)?//(?:www\.)?nexusmods\.com/[\w.-]+/mods/(\d+)",
        plain,
        flags=re.IGNORECASE,
    ):
        mid = int(m.group(1))
        lo = max(0, m.start() - 80)
        hi = min(len(plain), m.end() + 100)
        _add(mid, plain[lo:hi])

    # HTML 원문 속 URL 주변(태그 제거 후)
    for m in re.finditer(
        r"(?:https?:)?//(?:www\.)?nexusmods\.com/[\w.-]+/mods/(\d+)",
        raw_html,
        flags=re.IGNORECASE,
    ):
        mid = int(m.group(1))
        lo = max(0, m.start() - 60)
        hi = min(len(raw_html), m.end() + 100)
        chunk = raw_html[lo:hi]
        hint = _strip_html_to_plain(chunk, 600)
        _add(mid, hint)

    return pairs


def _best_mod_id_for_label_from_candidates(
    label: str,
    pairs: list[tuple[int, str]],
) -> int | None:
    """라벨과 가장 잘 맞는 ``mod_id`` (없으면 None)."""
    clean = _strip_priority_markers_from_label(str(label or "").strip()).casefold()
    if not clean:
        return None
    best_id: int | None = None
    best = 0.48
    for mid, hint in pairs:
        h = (hint or "").casefold()
        if not h:
            continue
        if clean == h or clean in h or h in clean:
            return mid
        r = SequenceMatcher(None, clean, h).ratio()
        first = clean.split()[0] if clean.split() else ""
        if len(first) > 2 and first in h:
            r = max(r, 0.86)
        if r > best:
            best = r
            best_id = mid
    return best_id


def _fill_prereq_tree_nexus_from_description(
    nodes: list[dict[str, Any]],
    pairs: list[tuple[int, str]],
) -> None:
    """``nexus_mod_id`` 가 비어 있는 노드만 설명 링크 후보로 채운다."""
    for n in nodes:
        if not isinstance(n, dict):
            continue
        raw_nid = n.get("nexus_mod_id")
        try:
            has_id = int(raw_nid) > 0 if raw_nid is not None else False
        except (TypeError, ValueError):
            has_id = False
        if not has_id:
            lab = str(n.get("label") or "").strip()
            guess = _best_mod_id_for_label_from_candidates(lab, pairs)
            if guess is not None:
                n["nexus_mod_id"] = int(guess)
        ch = n.get("children")
        if isinstance(ch, list):
            _fill_prereq_tree_nexus_from_description([x for x in ch if isinstance(x, dict)], pairs)


def _prereq_body_strings_from_tree(nodes: list[dict[str, Any]], depth: int = 0) -> list[str]:
    """트리를 ``_prereq_bullet_labels`` 와 동일한 본문 문자열(``•`` 제외) 목록으로 직렬화."""
    out: list[str] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        lab = str(n.get("label") or "").strip()
        if not lab:
            continue
        prio = str(n.get("priority") or PREREQ_PRIORITY_MANDATORY).upper()
        if prio not in _VALID_PREREQ_PRIORITIES:
            prio = PREREQ_PRIORITY_MANDATORY
        raw_nid = n.get("nexus_mod_id")
        try:
            ni = int(raw_nid) if raw_nid is not None else None
        except (TypeError, ValueError):
            ni = None
        nid_s = str(ni) if ni is not None and ni > 0 else "?"
        pad = "  " * depth
        line = f"{lab} [nexus:{nid_s}] [priority:{prio}]"
        out.append(f"{pad}{line}" if pad else line)
        ch = n.get("children")
        if isinstance(ch, list):
            out.extend(_prereq_body_strings_from_tree([x for x in ch if isinstance(x, dict)], depth + 1))
    return out


def split_prereq_line_meta(line: str) -> tuple[str, int | None, str]:
    """
    ``•`` 줄에서 ``[nexus:…]`` · ``[priority:…]`` 를 제거하고 라벨·ID·우선순위를 반환.
    태그 순서가 ``[priority]`` 앞/뒤로 섞여 있어도 처리한다.
    우선순위 없으면 ``MANDATORY``.
    """
    s = (line or "").strip()
    prio = PREREQ_PRIORITY_MANDATORY
    for m in re.finditer(r"\[priority:([A-Za-z_]+)\]", s, flags=re.IGNORECASE):
        raw_p = m.group(1).strip().upper()
        if raw_p in _VALID_PREREQ_PRIORITIES:
            prio = raw_p
    s = _RE_PREREQ_PRIORITY_ANYWHERE.sub("", s).strip()
    clean, nid = split_prereq_nexus_suffix(s)
    clean = _strip_priority_markers_from_label(clean)
    return clean, nid, prio


def normalize_prereq_item_dict(node: Any) -> dict[str, Any]:
    """``label`` / ``nexus_mod_id`` / ``children`` / ``priority`` 키를 갖는 사전요구 노드로 정규화."""
    if not isinstance(node, dict):
        return {
            "label": _strip_priority_markers_from_label(str(node).strip()),
            "nexus_mod_id": None,
            "children": [],
            "priority": PREREQ_PRIORITY_MANDATORY,
        }
    lab = _strip_priority_markers_from_label(str(node.get("label") or "").strip())
    nid = node.get("nexus_mod_id")
    if nid is not None and not isinstance(nid, int):
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            nid = None
    pr = str(node.get("priority") or PREREQ_PRIORITY_MANDATORY).strip().upper()
    if pr not in _VALID_PREREQ_PRIORITIES:
        pr = PREREQ_PRIORITY_MANDATORY
    ch = node.get("children")
    kids: list[dict[str, Any]] = []
    if isinstance(ch, list):
        for x in ch:
            if isinstance(x, dict):
                kids.append(normalize_prereq_item_dict(x))
    out: dict[str, Any] = {"label": lab, "nexus_mod_id": nid, "children": kids, "priority": pr}
    if isinstance(node, dict) and node.get("is_external") is True:
        out["is_external"] = True
    return out


def parse_prereq_plain_to_tree(prereq_plain: str) -> list[dict[str, Any]]:
    """
    ``[사전 요구 모드]`` 아래 ``•`` 줄을 읽어 부모–자식 트리를 만든다.
    줄 앞 공백 2칸당 하위 단계(0=최상위).
    """
    lines: list[tuple[int, str]] = []
    head = tr("id_search.sec_prereq")
    for raw in (prereq_plain or "").splitlines():
        if head in raw:
            continue
        if not raw.strip():
            continue
        exp = raw.expandtabs(2)
        lead = len(exp) - len(exp.lstrip(" "))
        depth = lead // 2
        t = exp.strip()
        if t.startswith("•"):
            body = t[1:].strip()
        elif t.startswith("-"):
            body = t[1:].strip()
        else:
            body = t
        if not (body or "").strip():
            continue
        lines.append((depth, body.strip()))

    if not lines:
        return []

    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for depth, body in lines:
        clean, nid, prio = split_prereq_line_meta(body)
        node = {"label": clean.strip(), "nexus_mod_id": nid, "children": [], "priority": prio}
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if not stack:
            roots.append(node)
        else:
            stack[-1][1].setdefault("children", []).append(node)
        stack.append((depth, node))
    return roots


def filter_prereq_tree_unsatisfied(
    nodes: list[dict[str, Any]],
    actives: Sequence[str],
    game_dir: str | None,
    active_nexus_ids: set[int] | frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    """이미 설치된 노드는 제외; 상위가 설치된 경우 하위만 승격."""
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        kids_in = n.get("children") if isinstance(n.get("children"), list) else []
        kids_out = filter_prereq_tree_unsatisfied(
            [x for x in kids_in if isinstance(x, dict)],
            actives,
            game_dir,
            active_nexus_ids=active_nexus_ids,
        )
        lab = str(n.get("label") or "").strip()
        raw_nid = n.get("nexus_mod_id")
        try:
            node_nexus_id = int(raw_nid) if raw_nid is not None else 0
        except (TypeError, ValueError):
            node_nexus_id = 0
        if _label_install_satisfied(
            lab,
            actives,
            game_dir,
            active_nexus_ids=active_nexus_ids,
            node_nexus_id=node_nexus_id,
        ):
            out.extend(kids_out)
            continue
        merged = normalize_prereq_item_dict({**n, "children": kids_out})
        out.append(merged)
    return out


def filter_prereq_tree_by_priority_set(
    nodes: list[dict[str, Any]],
    include: frozenset[str],
) -> list[dict[str, Any]]:
    """우선순위가 ``include`` 에 속하는 노드만 트리에 남긴다. 제외된 노드의 자식은 상위로 승격."""
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        prio = str(n.get("priority") or PREREQ_PRIORITY_MANDATORY).strip().upper()
        if prio not in _VALID_PREREQ_PRIORITIES:
            prio = PREREQ_PRIORITY_MANDATORY
        kids_in = [x for x in (n.get("children") or []) if isinstance(x, dict)]
        kids_f = filter_prereq_tree_by_priority_set(kids_in, include)
        if prio in include:
            out.append(normalize_prereq_item_dict({**n, "children": kids_f}))
        else:
            out.extend(kids_f)
    return out


def flatten_prereq_install_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """각 노드의 ``children``을 먼저(깊이 우선), 그다음 부모 순으로 평탄화 — 설치 순서."""
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kids = node.get("children") if isinstance(node.get("children"), list) else []
        if kids:
            out.extend(flatten_prereq_install_order([x for x in kids if isinstance(x, dict)]))
        lab = str(node.get("label") or "").strip()
        if not lab:
            continue
        nid = node.get("nexus_mod_id")
        if nid is not None and not isinstance(nid, int):
            try:
                nid = int(nid)
            except (TypeError, ValueError):
                nid = None
        pr = str(node.get("priority") or PREREQ_PRIORITY_MANDATORY).strip().upper()
        if pr not in _VALID_PREREQ_PRIORITIES:
            pr = PREREQ_PRIORITY_MANDATORY
        row: dict[str, Any] = {"label": lab, "nexus_mod_id": nid, "priority": pr}
        if node.get("is_external") is True:
            row["is_external"] = True
        out.append(row)
    return out


def build_pending_tree_from_flat_labels(usable: list[str]) -> list[dict[str, Any]]:
    """트리 파싱 결과가 비었을 때 평면 ``•`` 목록으로 폴백."""
    out: list[dict[str, Any]] = []
    for lab in usable:
        clean, nid, prio = split_prereq_line_meta(lab.strip())
        if not clean.strip():
            continue
        out.append(
            normalize_prereq_item_dict(
                {"label": clean, "nexus_mod_id": nid, "children": [], "priority": prio}
            )
        )
    return out


def _id_search_diag_log_req_block(req_block: str | None, *, main_mod_id: int) -> None:
    """사전 모드 LLM 입력(req_block)에 Nexus Notes(soft requirement 등)가 어떻게 들어가는지 로그."""
    if not (req_block or "").strip():
        _hard_log(f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} req_block_empty=1")
        return
    rb = req_block.strip()
    low = rb.casefold()
    soft_hint = any(
        s in low
        for s in (
            "not an absolute",
            "not a requirement",
            "not required",
            "soft requirement",
            "optional requirement",
            "recommended but",
        )
    )
    _hard_log(
        f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} req_block_chars={len(rb)} "
        f"looksmenu_in_block={'looksmenu' in low} "
        f"soft_req_wording_hint={soft_hint}"
    )
    cap = 8000
    if len(rb) > cap:
        _hard_log(
            f"[ID_SEARCH PREREQ_DIAG] req_block head {cap} chars:\n{rb[:cap]}\n…(truncated)"
        )
    else:
        _hard_log(f"[ID_SEARCH PREREQ_DIAG] req_block full:\n{rb}")


def _id_search_diag_dump_tree(
    nodes: list[dict[str, Any]], *, main_mod_id: int, phase: str
) -> None:
    lines: list[str] = []

    def walk(ns: list[dict[str, Any]], depth: int) -> None:
        for n in ns:
            if not isinstance(n, dict):
                continue
            lab = str(n.get("label") or "").strip()
            pr = str(n.get("priority") or "").strip().upper()
            nid = n.get("nexus_mod_id")
            ind = "  " * depth
            lines.append(f"{ind}- label={lab!r} nexus_mod_id={nid} priority={pr}")
            kids = [x for x in (n.get("children") or []) if isinstance(x, dict)]
            walk(kids, depth + 1)

    walk(nodes, 0)
    body = "\n".join(lines) if lines else "(empty tree)"
    _hard_log(
        f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} phase={phase!r} tree:\n{body}"
    )


def _id_search_diag_dump_flat_all(
    flat_all: list[dict[str, Any]], *, main_mod_id: int
) -> None:
    if not flat_all:
        _hard_log(f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} flat_all empty")
        return
    summary_parts: list[str] = []
    optional_rows: list[str] = []
    for x in flat_all:
        if not isinstance(x, dict):
            continue
        lab = str(x.get("label") or "").strip()
        pr = str(x.get("priority") or "").strip().upper()
        nid = x.get("nexus_mod_id")
        summary_parts.append(f"{lab!r}:{pr}:nexus={nid}")
        if pr == PREREQ_PRIORITY_OPTIONAL:
            optional_rows.append(f"  OPTIONAL label={lab!r} nexus_mod_id={nid}")
            if "looksmenu" in lab.casefold():
                _hard_log(
                    f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} "
                    "LooksMenu marked OPTIONAL in flat_all — "
                    "mandatory_tree(사전 모드)에서 제외됨"
                )
    _hard_log(
        f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} flat_all count={len(flat_all)} "
        f"summary={summary_parts}"
    )
    if optional_rows:
        _hard_log(
            f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} OPTIONAL entries:\n"
            + "\n".join(optional_rows)
        )


def _collect_tree_labels_casefold(nodes: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()

    def w(ns: list[dict[str, Any]]) -> None:
        for n in ns:
            if not isinstance(n, dict):
                continue
            lab = str(n.get("label") or "").strip().casefold()
            if lab:
                out.add(lab)
            w([x for x in (n.get("children") or []) if isinstance(x, dict)])

    w(nodes)
    return out


def _id_search_diag_looksmenu_mandatory_gap(
    req_block: str | None,
    mandatory_tree: list[dict[str, Any]],
    flat_all: list[dict[str, Any]],
    *,
    main_mod_id: int,
) -> None:
    if not (req_block or "").strip():
        return
    if "looksmenu" not in req_block.casefold():
        return
    mand_labs = _collect_tree_labels_casefold(mandatory_tree)
    if any("looksmenu" in lab for lab in mand_labs):
        _hard_log(
            f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} "
            "LooksMenu present in mandatory_tree OK"
        )
        return
    optional_lm = [
        (x.get("label"), x.get("nexus_mod_id"))
        for x in flat_all
        if isinstance(x, dict)
        and str(x.get("priority") or "").upper() == PREREQ_PRIORITY_OPTIONAL
        and "looksmenu" in str(x.get("label") or "").casefold()
    ]
    adv_lm = [
        (x.get("label"), x.get("nexus_mod_id"))
        for x in flat_all
        if isinstance(x, dict)
        and str(x.get("priority") or "").upper() == PREREQ_PRIORITY_ADVANCED
        and "looksmenu" in str(x.get("label") or "").casefold()
    ]
    _hard_log(
        f"[ID_SEARCH PREREQ_DIAG] mod_id={main_mod_id} "
        f"LooksMenu_in_req_block_but_absent_from_mandatory_tree "
        f"optional_flat_matches={optional_lm!r} advanced_flat_matches={adv_lm!r} "
        f"(사전 모드는 MANDATORY 만 pending_prereq_items 로 전달)"
    )


_SCRIPT_EXTENDER_SUBSTRS = ("skse", "f4se", "obse", "nvse")
_SE_LOADER_FILES = (
    "skse64_loader.exe",
    "skse_loader.exe",
    "f4se_loader.exe",
    "obse_loader.exe",
    "nvse_loader.exe",
)


def _label_mentions_script_extender(label: str) -> bool:
    c = (label or "").casefold()
    return any(s in c for s in _SCRIPT_EXTENDER_SUBSTRS)


def _script_extender_installed_via_loaders(game_dir: str | None) -> bool:
    if not (game_dir or "").strip():
        return False
    base = Path(str(game_dir).strip())
    if not base.is_dir():
        return False
    return any((base / name).is_file() for name in _SE_LOADER_FILES)


_NEXUS_MOD_SKSE64_SSE = 30379
_NEXUS_MOD_F4SE = 42147
SKSE_NEXUS_ID = _NEXUS_MOD_SKSE64_SSE
F4SE_NEXUS_ID = _NEXUS_MOD_F4SE
# Guide steps that use the script extender manual-install + MO2 registration flow.
SCRIPT_EXTENDER_GUIDE_NEXUS_IDS: frozenset[int] = frozenset(
    {_NEXUS_MOD_SKSE64_SSE, _NEXUS_MOD_F4SE}
)


def is_script_extender_guide_nexus_id(nexus_mod_id: int) -> bool:
    try:
        return int(nexus_mod_id) in SCRIPT_EXTENDER_GUIDE_NEXUS_IDS
    except (TypeError, ValueError):
        return False


def skse64_loader_path_and_exists(game_dir: str | None) -> tuple[str, bool]:
    """게임 루트에 ``skse64_loader.exe`` 존재 여부. ``os.path.exists`` + OSError 방어."""
    return loader_path_exists(game_dir, "skse64_loader.exe")


def script_extender_step_loader_installed_in_game_dir(
    *,
    nexus_mod_id: int,
    game_dir: str | None,
    loader_basename: str | None = None,
) -> bool:
    """
    가이드 큐의 스크립트 익스텐더 단계(SKSE64, F4SE 등)에서 게임 루트의 로더 존재 여부.
    ``loader_basename`` 은 :func:`game_context.script_extender_loader_basename_for_organizer` 권장.
    """
    if not is_script_extender_guide_nexus_id(nexus_mod_id):
        return False
    if not (game_dir or "").strip():
        return False
    base = Path(str(game_dir).strip())
    if not base.is_dir():
        return False
    basename = (loader_basename or "").strip() or "skse64_loader.exe"
    _, ok = loader_path_exists(game_dir, basename)
    return ok


def _any_partial_match(req: str, actives: Sequence[str]) -> bool:
    rc = (req or "").casefold().strip()
    if not rc:
        return False
    for mod in actives:
        mc = (mod or "").casefold().strip()
        if not mc:
            continue
        if rc in mc or mc in rc:
            return True
    return False


def _label_install_satisfied(
    label: str,
    actives: Sequence[str],
    game_dir: str | None,
    active_nexus_ids: set[int] | frozenset[int] | None = None,
    node_nexus_id: int = 0,
) -> bool:
    """OR-separated alternatives: satisfied if any alternative partially matches an active mod."""
    if node_nexus_id > 0 and active_nexus_ids is not None:
        return node_nexus_id in active_nexus_ids
    if _label_mentions_script_extender(label):
        return _script_extender_installed_via_loaders(game_dir)
    if tr("guide.or_separator") in label:
        for seg in _re_split_or().split(label):
            seg = re.sub(r"\([^)]*\)", "", seg).strip()
            if seg and _any_partial_match(seg, actives):
                return True
        return False
    return _any_partial_match(label, actives)


# ``chat_window`` 가 본문과 분리해 버튼 구간만 제거할 때 사용 (본문 HTML에 나오지 않는 토큰).
WEPAWN_GUIDE_PROMPT_FOOTER_MARKER = "<!--wepawn_guide_prompt_footer-->"


def _mod_metadata_mentions_enb(name: str, summary: str) -> bool:
    """제목·짧은 요약에 ENB가 **단어**로 등장할 때만 True (본문은 오탐이 잦음)."""
    hay = "\n".join(
        x for x in (name or "", summary or "") if (x or "").strip()
    )
    return bool(hay.strip() and _RE_ENB_WORD.search(hay))


def guide_prompt_footer_inline_html() -> str:
    """채팅 인라인 ``wepawn://guide-yes|no`` 링크 버튼(예/아니오)."""
    q = html.escape(tr("id_search.guide_footer_question"), quote=False)
    sty_yes = (
        "display:inline-block;margin:4px 0 0 0;padding:6px 16px;"
        "background:#1565c0;color:#ffffff;text-decoration:none;border-radius:4px;"
        "font-weight:600;"
    )
    sty_no = (
        "display:inline-block;margin:4px 0 0 0;padding:6px 16px;"
        "background:#546e7a;color:#ffffff;text-decoration:none;border-radius:4px;"
        "font-weight:600;"
    )
    hy = html.escape("wepawn://guide-yes", quote=True)
    hn = html.escape("wepawn://guide-no", quote=True)
    return (
        f'<p style="margin:8px 0 4px 0;"><b>{q}</b></p>'
        f'<p style="margin:0;">'
        f'<a href="{hy}" style="{sty_yes}">{html.escape(tr("id_search.guide_button_yes"), quote=False)}</a>'
        f"&nbsp;&nbsp;&nbsp;"
        f'<a href="{hn}" style="{sty_no}">{html.escape(tr("id_search.guide_button_no"), quote=False)}</a>'
        f"</p>"
    )


def _approx_llama_prompt_tokens(system_prompt: str, user_prompt: str) -> tuple[int, int, int, int]:
    """
    llama-server로 보내는 chat 페이로드( system + user 본문 ) 근사 토큰.

    UTF-8 바이트 ÷ 3 은 한·영 혼합 BPE 길이의 거친 추정치이며, 실제 토큰과 다를 수 있다.
    """
    sys_chars = len(system_prompt)
    usr_chars = len(user_prompt)
    combined = f"{system_prompt}\n{user_prompt}"
    u8 = len(combined.encode("utf-8"))
    approx = max(1, int((u8 + 2) // 3))
    return sys_chars, usr_chars, u8, approx


def prune_prereq_tree_by_optional_nexus_ids(
    nodes: list[dict[str, Any]],
    optional_nids: frozenset[int],
) -> list[dict[str, Any]]:
    """선택(optional)으로 분류된 ``nexus_mod_id`` 노드를 제거하고 자식은 위로 승격."""
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        kids_in = [x for x in (n.get("children") or []) if isinstance(x, dict)]
        kids_out = prune_prereq_tree_by_optional_nexus_ids(kids_in, optional_nids)
        raw_nid = n.get("nexus_mod_id")
        try:
            nid = int(raw_nid) if raw_nid is not None else 0
        except (TypeError, ValueError):
            nid = 0
        if nid > 0 and nid in optional_nids:
            out.extend(kids_out)
            continue
        out.append(normalize_prereq_item_dict({**n, "children": kids_out}))
    return out

_USER_PROMPT_BODY_BUDGET = 14_000
# llama user_prompt 안 [본문 설명]: 요약·requirements만으로도 충분해 본문은 짧게
_LLM_USER_BODY_DESC_MAX_CHARS = 500


def _raw_requirement_display_name(raw: Mapping[str, Any], mid: int | None) -> str:
    """``_normalize_dependency_item`` 과 동일한 이름 필드 우선순위 (mod_id 없음·오프사이트 대비)."""
    for key in (
        "name",
        "mod_name",
        "modName",
        "title",
        "display_name",
        "displayName",
        "label",
        "dependency_name",
        "dependencyName",
    ):
        v = raw.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return (
        tr("id_search.req_mod_fallback", mid=mid)
        if mid is not None
        else tr("id_search.req_unknown")
    )


def _raw_requirement_note(raw: Mapping[str, Any]) -> str:
    note = raw.get("description") or raw.get("notes")
    if note is None:
        return ""
    return str(note).strip()


def _fetch_primary_file_payload(
    game_domain: str,
    mod_id: int,
    api_key: str,
    application: str,
    *,
    timeout: float,
) -> Mapping[str, Any] | None:
    """
    ``files.json`` 으로 목록을 받아 대표 파일 ID를 고른 뒤 ``files/{id}.json`` 을 반환.

    ``fetch_mod_file_dependencies`` 와 동일한 대표 파일 선택(``category_id == 1`` 우선).
    """
    _hard_log(f"[REQ FALLBACK] 진입 mod_id={mod_id}")
    domain = (game_domain or "").strip().lower() or "skyrimspecialedition"
    files_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}/files.json"
    try:
        files_raw = _nexus_get_json(
            files_url,
            api_key=api_key,
            application=application,
            timeout=timeout,
        )
    except NexusAPIError as e:
        _hard_log(f"[REQ FALLBACK] 실패: {e}")
        return None
    files_map = _files_list_root(files_raw)
    _hard_log(f"[REQ FALLBACK] files 목록: {files_map}")
    if files_map is None:
        _hard_log(f"[REQ FALLBACK] 실패: files_map is None")
        return None
    auto_fid = _pick_primary_file_id(files_map)
    if auto_fid <= 0:
        _hard_log(f"[REQ FALLBACK] 실패: primary file_id 없음 (auto_fid={auto_fid})")
        return None
    file_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}/files/{auto_fid}.json"
    try:
        file_payload = _nexus_get_json(
            file_url,
            api_key=api_key,
            application=application,
            timeout=timeout,
        )
    except NexusAPIError as e:
        _hard_log(f"[REQ FALLBACK] 실패: {e}")
        return None
    if isinstance(file_payload, Mapping):
        _hard_log(
            f"[ID_SEARCH] requirements fallback: using files/{auto_fid}.json "
            f"(picked from files.json for mod {mod_id})"
        )
        return file_payload
    _hard_log(f"[REQ FALLBACK] 실패: file_payload가 Mapping 아님 ({type(file_payload)!r})")
    return None


def _build_nexus_requirements_llm_block(game_domain: str, api_payload: Mapping[str, Any]) -> str:
    """Nexus ``.../mods/{id}.json`` 전체 응답에서 requirements 노드를 LLM용 텍스트로 만든다.

    ``_dependency_item_mappings`` 는 최상위와 ``data`` 양쪽을 본다
    (``fetch_mod_file_dependencies`` 와 동일). 내부 ``data`` 만 넘기면
    루트에만 붙은 ``requirements`` 를 놓칠 수 있어 여기서는 **원본 payload** 를 쓴다.
    mod_id 가 없는 노드(오프사이트 등)는 ``[nexus:?]`` 로 남긴다.
    """
    _ = (game_domain or "").strip().lower() or "skyrimspecialedition"
    items = _dependency_item_mappings(api_payload)
    _hard_log(f"[DEBUG req items] {items!r}")
    if not items:
        return ""
    lines: list[str] = [tr("id_search.nexus_requirements_header")]
    seen_mod_ids: set[int] = set()
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        mid = _coerce_mod_id(raw.get("mod_id") if raw.get("mod_id") is not None else raw.get("modId"))
        if mid is not None:
            if mid in seen_mod_ids:
                continue
            seen_mod_ids.add(mid)
        name = _raw_requirement_display_name(raw, mid)
        note = _raw_requirement_note(raw)
        nid = str(mid) if mid is not None else "?"
        tail = f" - {note}" if note else ""
        lines.append(f"- {name} [nexus:{nid}]{tail}")
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


class IdSearchWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str, bool)
    guide_context_ready = pyqtSignal(object)

    def __init__(
        self,
        *,
        mod_id: int,
        api_key: str,
        game_domain: str,
        application: str,
        base_url: str,
        active_mod_display_names: Sequence[str] = (),
        active_nexus_ids: set[int] | frozenset[int] | None = None,
        game_directory: str = "",
        request_timeout: float = 120.0,
        skip_guide_footer: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mod_id = int(mod_id)
        self._api_key = api_key
        self._game_domain = (game_domain or "").strip().lower() or "skyrimspecialedition"
        self._application = application
        self._base_url = base_url.rstrip("/")
        self._request_timeout = float(request_timeout)
        self._skip_guide_footer = bool(skip_guide_footer)
        self._game_directory: str = (game_directory or "").strip()
        self._active_mods: tuple[str, ...] = tuple(
            str(x).strip() for x in active_mod_display_names if str(x).strip()
        )
        self._active_nexus_ids: frozenset[int] | None = (
            frozenset(active_nexus_ids) if active_nexus_ids is not None else None
        )

    def run(self) -> None:
        try:
            mod_url = f"{NEXUS_REST_V1}/games/{self._game_domain}/mods/{self._mod_id}.json"
            payload = _nexus_get_json(
                mod_url,
                api_key=self._api_key,
                application=self._application,
                timeout=min(60.0, self._request_timeout),
            )
            root = _mod_json_root(payload)
            if root is None:
                self.failed.emit(tr("id_search.error_mod_json"), False)
                return

            name = str(root.get("name") or "").strip() or tr(
                "id_search.req_mod_fallback", mid=self._mod_id
            )
            ver = _extract_version(root)
            ts: int | None = None
            for key in ("updated_timestamp", "updatedTimestamp", "updated_time"):
                raw = root.get(key)
                if raw is None:
                    continue
                try:
                    ts = int(raw)
                except (TypeError, ValueError):
                    continue
                break

            api_domain = _api_domain_name(root)
            mo_domain = self._game_domain.strip().lower()
            if api_domain and api_domain == mo_domain:
                line1 = (
                    '<span style="color:#2e7d32;">'
                    f"{html.escape(tr('id_search.compat_ok_game'), quote=False)}"
                    "</span>"
                )
            else:
                line1 = (
                    '<span style="color:#ef6c00;">'
                    f"{html.escape(tr('id_search.compat_other_game'), quote=False)}"
                    "</span>"
                )

            update_ymd = _format_update_yyyy_mm_dd(ts)
            stale_warning = ""
            if ts is not None:
                age_days = max(0.0, (time.time() - float(ts)) / 86400.0)
                if age_days > 730:
                    stale_warning = (
                        '<br/><span style="color:#ef6c00;">'
                        f"{html.escape(tr('id_search.compat_stale'), quote=False)}"
                        "</span>"
                    )

            summary = str(root.get("summary") or "").strip()
            desc_raw = root.get("description")
            desc_html = str(desc_raw or "")
            desc_plain = _strip_html_to_plain(desc_html, 20_000)
            _hard_log(
                "[ID_SEARCH] mod.json description: "
                f"mod_id={self._mod_id} raw_is_none={desc_raw is None} "
                f"html_len={len(desc_html)} html_empty={not desc_html.strip()} "
                f"plain_len={len(desc_plain)} plain_empty={not (desc_plain or '').strip()} "
                f"html_head={desc_html[:200]!r}"
            )

            if _mod_metadata_mentions_enb(name, summary):
                _hard_log(
                    f"[ID_SEARCH ENB] 가이드 대체 분기 mod_id={self._mod_id} name={name!r}"
                )
                e_name = html.escape(name, quote=False)
                vn = tr("id_search.version_none")
                e_ver = html.escape(ver or vn, quote=False)
                enb_url = tr("id_search.enb_youtube_url")
                safe_href = html.escape(enb_url, quote=True)
                enb_chunks: list[str] = [
                    line1,
                    "<br/><br/>",
                    f"<b>{html.escape(tr('id_search.info_heading'), quote=False)}</b><br/>",
                    f"{html.escape(tr('id_search.lbl_name'), quote=False)} {e_name}<br/>",
                    f"{html.escape(tr('id_search.lbl_mod_id'), quote=False)} {self._mod_id}<br/>",
                    f"{html.escape(tr('id_search.lbl_version'), quote=False)} {e_ver}<br/>",
                    f"{html.escape(tr('id_search.lbl_last_update'), quote=False)} "
                    f"{html.escape(update_ymd, quote=False)}",
                    stale_warning,
                    "<br/><br/>",
                    "<p>",
                    html.escape(
                        f"{tr('id_search.enb_notice_1')} {tr('id_search.enb_notice_2')}",
                        quote=False,
                    ),
                    "</p>",
                    f'<p><a href="{safe_href}">'
                    f"{html.escape(enb_url, quote=False)}"
                    "</a></p>",
                ]
                self.finished_ok.emit("".join(enb_chunks))
                return

            parts: list[str] = []
            tag_sum = tr("id_search.llm_tag_summary")
            tag_body = tr("id_search.llm_tag_body")
            if summary:
                parts.append(f"{tag_sum}\n{summary}")
            if desc_plain:
                desc_for_llm = (desc_plain or "").strip()[:_LLM_USER_BODY_DESC_MAX_CHARS]
                if desc_for_llm:
                    parts.append(f"{tag_body}\n{desc_for_llm}")
            combined = "\n\n".join(parts).strip()
            if not combined:
                combined = tr("id_search.llm_combined_empty")

            lang_instruction = llm_system_prompt_language_line()
            system_prompt = f"{tr('id_search.llm_system_prompt')}\n\n{lang_instruction}"
            _hard_log(
                f"[PROMPT] 시스템 프롬프트 끝에 언어 지시어 추가: {lang_instruction}"
            )
            req_block = _build_nexus_requirements_llm_block(self._game_domain, payload)
            if not req_block:
                # mod.json에 requirements 노드가 없으면 대표 파일 JSON 시도
                file_payload = _fetch_primary_file_payload(
                    self._game_domain,
                    self._mod_id,
                    self._api_key,
                    self._application,
                    timeout=min(60.0, self._request_timeout),
                )
                if file_payload is not None:
                    req_block = _build_nexus_requirements_llm_block(
                        self._game_domain, file_payload
                    )
            if not req_block:
                scraped = _scrape_nexus_requirements_from_page(
                    self._game_domain,
                    self._mod_id,
                )
                if scraped:
                    lines = [tr("id_search.nexus_requirements_header")]
                    for item in scraped:
                        note = f" - {item['note']}" if item.get("note") else ""
                        mid = item.get("mod_id") or "?"
                        lines.append(f"- {item['name']} [nexus:{mid}]{note}")
                    req_block = "\n".join(lines)
                    ids_s = [int(x.get("mod_id") or 0) for x in scraped]
                    _hard_log(
                        f"[REQ SCRAPE] 성공: {len(scraped)}개 항목 mod_ids={ids_s} "
                        f"ussep_266={266 in ids_s}"
                    )
                else:
                    _hard_log("[REQ SCRAPE] 결과 없음 (REQ_TAB 로그로 스크랩 단계 확인)")
            _id_search_diag_log_req_block(
                req_block if req_block else None, main_mod_id=self._mod_id
            )
            body_cap = _USER_PROMPT_BODY_BUDGET
            if req_block:
                body_cap = max(4000, _USER_PROMPT_BODY_BUDGET - len(req_block) - 2)
            combined_trimmed = combined[:body_cap]
            user_body = f"{req_block}\n\n{combined_trimmed}" if req_block else combined_trimmed
            user_prompt = f"{tr('id_search.llm_user_prompt')}{user_body}"
            # _diag 는 stdout(print)만 — wepawn_debug.log 는 _hard_log 로 동시 기록
            _diag("[DEBUG 테스트 - 여기까지 도달]")
            _hard_log("[DEBUG 테스트 - 여기까지 도달]")
            _up_prev = f"[DEBUG user_prompt 앞 1500자]\n{user_prompt[:1500]}"
            _diag(_up_prev)
            _hard_log(_up_prev)

            llm_timeout = max(30.0, self._request_timeout - 5.0)
            sc_tok, uc_tok, u8_tok, approx_total = _approx_llama_prompt_tokens(
                system_prompt, user_prompt
            )
            nexus_to = min(60.0, self._request_timeout)
            _hard_log(
                "[ID_SEARCH LLM] 프롬프트 근사 토큰(진단): "
                f"~{approx_total} (system {sc_tok}자 + user {uc_tok}자, utf8 {u8_tok}B, 휴리스틱 int(bytes/3)); "
                f"timeout LLM={llm_timeout}s, worker_request_timeout={self._request_timeout}s, "
                f"nexus mod.json/primary file={nexus_to}s"
            )

            ai_text, lat_ms = complete_chat_plain_text(
                base_url=self._base_url,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                request_timeout=llm_timeout,
                max_tokens=2048,
                temperature=0.35,
            )
            _diag(f"ID_SEARCH LLM ok latency_ms={lat_ms} chars={len(ai_text)}")

            intro_body, install_body, compat_body, prereq_slice = _parse_llm_sections(ai_text)
            prereq_plain = _normalize_prereq_block(prereq_slice)
            _hard_log(
                f"[ID_SEARCH PREREQ_DIAG] mod_id={self._mod_id} "
                f"llm_prereq_slice_chars={len(prereq_slice)} "
                f"prereq_plain_chars={len(prereq_plain)} "
                f"prereq_slice_head={prereq_slice[:2200]!r}"
            )
            _skip_labels = _prereq_label_skip_tokens()
            usable_preview = [
                x
                for x in _prereq_bullet_labels(prereq_plain)
                if x.strip() and x.strip().casefold() not in _skip_labels
            ]
            tree: list[dict[str, Any]] = parse_prereq_plain_to_tree(prereq_plain)
            if not tree and usable_preview:
                tree = build_pending_tree_from_flat_labels(usable_preview)
            tree = [normalize_prereq_item_dict(x) for x in tree]
            link_pairs = _collect_nexus_mod_link_candidates(
                str(desc_html or ""),
                str(desc_plain or ""),
            )
            if link_pairs:
                _fill_prereq_tree_nexus_from_description(tree, link_pairs)
                tree = [normalize_prereq_item_dict(x) for x in tree]
                _hard_log(f"[PREREQ URL FILL] link_candidates={len(link_pairs)}")
            nexus_tab_req_ids = _nexus_requirement_mod_ids_from_req_block(req_block)
            if nexus_tab_req_ids:
                _hard_log(
                    f"[PREREQ NEXUS_TAB] mod_id={self._mod_id} "
                    f"coerce_mandatory_for_nexus_ids={sorted(nexus_tab_req_ids)}"
                )
                tree = _coerce_priorities_for_nexus_tab_requirements(
                    tree, nexus_tab_req_ids
                )
            _id_search_diag_dump_tree(
                tree, main_mod_id=self._mod_id, phase="after_llm_parse_and_url_fill"
            )
            if tree:
                prereq_labels = _prereq_body_strings_from_tree(tree)
            else:
                prereq_labels = _prereq_bullet_labels(prereq_plain)
            compat_chunk = _build_compat_section_html(compat_body, self._active_mods)

            intro_display = (
                intro_body if intro_body.strip() else tr("id_search.intro_fallback")
            )
            intro_html = (
                f"<b>{html.escape(tr('id_search.sec_intro'), quote=False)}</b><br/>"
                + html.escape(intro_display, quote=False).replace("\n", "<br/>")
            )

            install_chunk = ""
            if install_body is not None and install_body.strip():
                install_fmt = _format_install_caution_plain(install_body)
                install_html = html.escape(install_fmt, quote=False).replace("\n", "<br/>")
                install_chunk = (
                    f"<br/><br/><b>{html.escape(tr('id_search.sec_install'), quote=False)}</b><br/>"
                    + install_html
                )

            e_name = html.escape(name, quote=False)
            e_ver = html.escape(ver or tr("id_search.version_none"), quote=False)

            chunks: list[str] = [
                line1,
                "<br/><br/>",
                f"<b>{html.escape(tr('id_search.info_heading'), quote=False)}</b><br/>",
                f"{html.escape(tr('id_search.lbl_name'), quote=False)} {e_name}<br/>",
                f"{html.escape(tr('id_search.lbl_mod_id'), quote=False)} {self._mod_id}<br/>",
                f"{html.escape(tr('id_search.lbl_version'), quote=False)} {e_ver}<br/>",
                f"{html.escape(tr('id_search.lbl_last_update'), quote=False)} "
                f"{html.escape(update_ymd, quote=False)}",
                stale_warning,
                "<br/><br/>",
                intro_html,
                install_chunk,
                compat_chunk,
            ]
            out_html = "".join(chunks)
            if not self._skip_guide_footer:
                usable = [
                    x
                    for x in prereq_labels
                    if x.strip() and x.strip().casefold() not in _skip_labels
                ]
                gdir = self._game_directory or None
                full_filtered = filter_prereq_tree_unsatisfied(
                    tree,
                    self._active_mods,
                    gdir,
                    active_nexus_ids=self._active_nexus_ids,
                )
                _id_search_diag_dump_tree(
                    full_filtered,
                    main_mod_id=self._mod_id,
                    phase="after_install_filter_active_mods",
                )
                flat_all = flatten_prereq_install_order(full_filtered)
                _id_search_diag_dump_flat_all(flat_all, main_mod_id=self._mod_id)
                optional_labels = [
                    str(x.get("label") or "").strip()
                    for x in flat_all
                    if str(x.get("priority") or "").upper() == PREREQ_PRIORITY_OPTIONAL
                    and str(x.get("label") or "").strip()
                ]
                advanced_labels = [
                    str(x.get("label") or "").strip()
                    for x in flat_all
                    if str(x.get("priority") or "").upper() == PREREQ_PRIORITY_ADVANCED
                    and str(x.get("label") or "").strip()
                ]
                mcm_flat = [
                    {
                        "label": str(x.get("label") or "").strip(),
                        "nexus_mod_id": x.get("nexus_mod_id"),
                        "priority": PREREQ_PRIORITY_MCM_ONLY,
                    }
                    for x in flat_all
                    if str(x.get("priority") or "").upper() == PREREQ_PRIORITY_MCM_ONLY
                    and str(x.get("label") or "").strip()
                ]
                prereq_classify_flat: list[dict[str, Any]] = []
                for x in flat_all:
                    if not isinstance(x, dict):
                        continue
                    raw_nid = x.get("nexus_mod_id")
                    try:
                        ni = int(raw_nid) if raw_nid is not None else 0
                    except (TypeError, ValueError):
                        ni = 0
                    if ni <= 0:
                        continue
                    lab = str(x.get("label") or "").strip()
                    if not lab:
                        continue
                    prereq_classify_flat.append(
                        {
                            "label": lab,
                            "nexus_mod_id": ni,
                            "priority": str(x.get("priority") or PREREQ_PRIORITY_MANDATORY).upper(),
                        }
                    )
                mandatory_tree = filter_prereq_tree_by_priority_set(
                    full_filtered,
                    frozenset({PREREQ_PRIORITY_MANDATORY}),
                )
                _id_search_diag_dump_tree(
                    mandatory_tree,
                    main_mod_id=self._mod_id,
                    phase="mandatory_only_for_dictionary_guide",
                )
                _hard_log(
                    f"[ID_SEARCH PREREQ_DIAG] mod_id={self._mod_id} "
                    f"optional_prereq_labels={optional_labels!r} "
                    f"advanced_prereq_labels={advanced_labels!r}"
                )
                _id_search_diag_looksmenu_mandatory_gap(
                    req_block,
                    mandatory_tree,
                    flat_all,
                    main_mod_id=self._mod_id,
                )
                pending_nid_rows: list[tuple[str, Any]] = []

                def _walk_pending(nodes: list[Any]) -> None:
                    for n in nodes:
                        if not isinstance(n, dict):
                            continue
                        pending_nid_rows.append(
                            (str(n.get("label") or "").strip()[:120], n.get("nexus_mod_id"))
                        )
                        ch = n.get("children")
                        if isinstance(ch, list):
                            _walk_pending([x for x in ch if isinstance(x, dict)])

                _walk_pending(mandatory_tree)
                _hard_log(
                    f"[PREREQ CLASSIFY DBG] main_mod_id={self._mod_id} "
                    f"usable_count={len(usable)} len(flat_all)={len(flat_all)}"
                )
                _hard_log(
                    f"[PREREQ CLASSIFY DBG] pending_prereq_items (label, nexus_mod_id)="
                    f"{pending_nid_rows!r}"
                )
                _hard_log(
                    f"[PREREQ CLASSIFY DBG] prereq_classify_flat={prereq_classify_flat!r} "
                    "(flat retained for future JIT; classify prompt removed)"
                )
                self.guide_context_ready.emit(
                    {
                        "main_mod_id": self._mod_id,
                        "main_mod_name": name,
                        "game_domain": self._game_domain,
                        "game_directory": (self._game_directory or "").strip(),
                        "pending_prereq_items": mandatory_tree,
                        "mcm_pending_flat": mcm_flat,
                        "optional_prereq_labels": optional_labels,
                        "advanced_prereq_labels": advanced_labels,
                        "prereq_classify_flat": prereq_classify_flat,
                    }
                )
                out_html += (
                    "<br/><br/>"
                    + WEPAWN_GUIDE_PROMPT_FOOTER_MARKER
                    + guide_prompt_footer_inline_html()
                )
            self.finished_ok.emit(out_html)

        except NexusAPIError as exc:
            self.failed.emit(str(exc), False)
        except LLMConnectionError as exc:
            self.failed.emit(str(exc), True)
        except LLMParseError as exc:
            self.failed.emit(str(exc), False)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)
