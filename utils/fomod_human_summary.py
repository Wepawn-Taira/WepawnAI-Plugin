"""
Turn FOMOD ``ModuleConfig.xml`` text into a UI summary: Korean labels and hints only;
paths, mod option names, and descriptions stay as in the archive (often English).

When you add a locale switch, replace the fixed Korean strings with i18n lookups.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET


def _local_tag(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _elem_text_deep(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        parts.append(_elem_text_deep(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _clean_description(raw: str) -> str:
    t = re.sub(r"<[^>]+>", " ", raw or "")
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:500] + ("…" if len(t) > 500 else "")


_GROUP_TYPE_KO: dict[str, str] = {
    "selectexactlyone": "이 그룹에서 정확히 하나만 선택합니다.",
    "selectatleastone": "이 그룹에서 최소 하나 이상 선택합니다.",
    "selectany": "이 그룹에서 원하는 항목을 골라 설치할 수 있습니다.",
    "selectall": "이 그룹의 항목이 모두 설치 대상입니다.",
}


def _find_children_by_local(parent: ET.Element, local: str) -> list[ET.Element]:
    want = local.casefold()
    return [c for c in parent if _local_tag(c.tag).casefold() == want]


def fomod_xml_to_korean_summary(
    xml: str,
    *,
    max_plugins_shown: int = 72,
    max_required_entries: int = 24,
) -> str:
    """
    Parse ModuleConfig-style XML: Korean section labels and notes; mod paths/names/text unchanged.

    On parse failure, returns a short Korean message (no raw markup dump).
    """
    s = (xml or "").strip()
    if not s.startswith("<"):
        return s
    try:
        root = ET.fromstring(s)
    except ET.ParseError as exc:
        return (
            "설치 옵션 설정을 읽는 중 오류가 있어 요약을 만들지 못했습니다. "
            f"MO2 설치 창에서 옵션을 확인해 주세요. ({exc})"
        )

    lines: list[str] = []

    module_name = ""
    for el in root.iter():
        if _local_tag(el.tag).casefold() == "modulename":
            module_name = (el.text or "").strip()
            if module_name:
                lines.append(f"【설치 마법사 이름】 {module_name}")
            break

    for req in root.iter():
        if _local_tag(req.tag).casefold() != "requiredinstallfiles":
            continue
        entries: list[str] = []
        for child in req:
            ln = _local_tag(child.tag).casefold()
            src = (child.get("source") or child.get("name") or "").strip()
            if not src:
                continue
            if ln == "folder":
                entries.append(f"폴더: {src}")
            elif ln == "file":
                entries.append(f"파일: {src}")
        if entries:
            lines.append("")
            lines.append("【항상 함께 설치되는 항목】")
            shown = entries[:max_required_entries]
            lines.extend(f"  · {x}" for x in shown)
            if len(entries) > max_required_entries:
                lines.append(f"  · … 외 {len(entries) - max_required_entries}개")
        break

    plugin_shown = 0
    omitted_plugins = 0
    for ofg in root.iter():
        if _local_tag(ofg.tag).casefold() != "optionalfilegroups":
            continue
        for group in ofg:
            if _local_tag(group.tag).casefold() != "group":
                continue
            gname = (group.get("name") or "").strip() or "(이름 없는 옵션 그룹)"
            gtype_raw = (group.get("type") or "").replace(" ", "")
            type_key = gtype_raw.casefold()
            type_ko = _GROUP_TYPE_KO.get(
                type_key,
                f"선택 방식: {gtype_raw}" if gtype_raw else "선택 방식: 설치 창에서 확인",
            )
            lines.append("")
            lines.append(f"【선택 옵션 그룹】 {gname}")
            lines.append(f"  ({type_ko})")

            plugins_blocks = _find_children_by_local(group, "plugins")
            if not plugins_blocks:
                continue
            for plugins_el in plugins_blocks:
                for plugin in plugins_el:
                    if _local_tag(plugin.tag).casefold() != "plugin":
                        continue
                    if plugin_shown >= max_plugins_shown:
                        omitted_plugins += 1
                        continue
                    pname = (plugin.get("name") or "").strip() or "(이름 없는 옵션)"
                    desc = (plugin.get("description") or "").strip()
                    if not desc:
                        for ch in plugin:
                            if _local_tag(ch.tag).casefold() == "description":
                                desc = _clean_description(_elem_text_deep(ch))
                                break
                    else:
                        desc = _clean_description(desc)
                    plugin_shown += 1
                    lines.append(f"  · {pname}")
                    if desc:
                        lines.append(f"    └ {desc}")
        break

    if omitted_plugins:
        lines.append("")
        lines.append(
            f"※ 아래는 일부만 표시했습니다. "
            f"빠진 선택 항목 {omitted_plugins}개는 MO2 설치 창에서 확인하세요."
        )

    has_conditional = any(
        _local_tag(e.tag).casefold() == "conditionalfileinstalls" for e in root.iter()
    )
    if has_conditional:
        lines.append("")
        lines.append(
            "※ 조건부 설치 규칙이 있습니다. "
            "다른 모드나 게임 상태에 따라 달라질 수 있으니 설치 창 안내를 따르세요."
        )

    if len(lines) <= 1 and not module_name:
        return (
            "설치 옵션 구조를 요약으로 읽기 어렵습니다. "
            "MO2 설치 창의 그룹·설명을 보고 선택하면 됩니다."
        )

    return "\n".join(lines).strip()
