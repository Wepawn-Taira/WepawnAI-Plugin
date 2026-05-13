"""
Best-effort Skyrim (SSE/AE) executable version checks from FOMOD ``ModuleConfig.xml``.

Only inspects ``<dependencies>...</dependencies>`` blocks to avoid matching unrelated ``version=`` attributes.
"""

from __future__ import annotations

import re


def _dependencies_xml_snippets(xml: str) -> list[str]:
    if not xml or not xml.strip():
        return []
    out: list[str] = []
    for m in re.finditer(r"<dependencies\b[^>]*>.*?</dependencies>", xml, flags=re.I | re.DOTALL):
        out.append(m.group(0))
    return out if out else [xml]


def _version_tokens_from_text(chunk: str) -> list[str]:
    """Collect plausible game version literals near game-related keywords."""
    found: list[str] = []
    for m in re.finditer(
        r"(?is)(?:skyrim|game)(?:.{0,120}?)(?:min_?version|version|gameversion)\s*=\s*[\"']([\d.]+)[\"']",
        chunk,
    ):
        found.append(m.group(1).strip())
    for m in re.finditer(
        r"(?is)<(?:[^:>]+:)?gamedependency\b[^>]*?(?:min_?version|minVersion|version)\s*=\s*['\"]([\d.]+)['\"]",
        chunk,
    ):
        found.append(m.group(1).strip())
    for m in re.finditer(
        r"(?is)type\s*=\s*['\"]Game['\"][^>]*?(?:min_?version|version)\s*=\s*['\"]([\d.]+)['\"]",
        chunk,
    ):
        found.append(m.group(1).strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for v in found:
        if v and v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered


def fomod_declared_minimum_game_versions(xml: str) -> list[str]:
    """Return ordered unique minimum-like version strings from dependency blocks only."""
    if not xml.lstrip().startswith("<"):
        return []
    acc: list[str] = []
    for chunk in _dependencies_xml_snippets(xml):
        acc.extend(_version_tokens_from_text(chunk))
    seen: set[str] = set()
    out: list[str] = []
    for v in acc:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _version_tuple(s: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", s.replace(",", "."))
    return tuple(int(x) for x in parts[:12]) if parts else ()


def _version_ge(a: str, b: str) -> bool:
    """True if ``a`` is greater than or equal to ``b`` (component-wise, zero-padded)."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    if not ta or not tb:
        return True
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return ta >= tb


def game_version_mismatch_fact_line(current_exe_version: str, fomod_xml: str) -> str:
    """
    Return a single Korean fact line if FOMOD declares a minimum game version above the current exe.

    Empty string if unknown, if current version unreadable, or if current satisfies all mins.
    """
    cur = (current_exe_version or "").strip()
    if not cur:
        return ""
    mins = fomod_declared_minimum_game_versions(fomod_xml)
    if not mins:
        return ""
    failed = [m for m in mins if not _version_ge(cur, m)]
    if not failed:
        return ""
    need = ", ".join(failed)
    return (
        f"[시스템 팩트체크] 설치 옵션 설정(의존성)에 따르면 최소 게임(실행 파일) 버전 요구가 {need} 인데, "
        f"현재 MO2가 읽은 실행 파일 버전은 {cur} 입니다. 요구가 현재 버전보다 높으면 호환되지 않을 수 있습니다."
    )
