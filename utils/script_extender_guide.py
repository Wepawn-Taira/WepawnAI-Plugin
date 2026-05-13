"""
Facts for install-guide LLM: script extender (SKSE, F4SE, …) download link + safe version advice.

- If the MO2-reported exe version matches a maintainer-updated “latest patch” alias set for that
  Nexus game domain → short Korean note + official URL only (no specific extender build numbers).
- Otherwise → cautious Korean note: pick the download option that matches *their* game version.

Update ``latest_exe_aliases`` when Bethesda releases a new game patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class _ExtenderProfile:
    short_name: str
    official_url: str
    # Normalized exe version strings (digits joined by dots) treated as “on latest patch track”.
    latest_exe_aliases: frozenset[str]


def normalize_exe_version_string(version: str) -> str:
    """Normalize PE ``FileVersion`` / ``ProductVersion`` for set lookup."""
    raw = (version or "").strip()
    if not raw:
        return ""
    parts = re.findall(r"\d+", raw)
    return ".".join(parts) if parts else ""


# Nexus ``gameNexusName()`` domain (lowercase) → profile.
_EXTENDER_BY_DOMAIN: dict[str, _ExtenderProfile] = {
    "skyrimspecialedition": _ExtenderProfile(
        short_name="SKSE",
        official_url="https://skse.silverlock.org/",
        latest_exe_aliases=frozenset(
            {
                # AE 1.6.117 — add new tuples here after Bethesda patches.
                "1.6.117.0",
                "1.6.1170.0",
            }
        ),
    ),
    "skyrim": _ExtenderProfile(
        short_name="SKSE",
        official_url="https://skse.silverlock.org/",
        latest_exe_aliases=frozenset(
            {
                "1.9.32.0",
            }
        ),
    ),
    "fallout4": _ExtenderProfile(
        short_name="F4SE",
        official_url="https://f4se.silverlock.org/",
        # Keep empty until you maintain a canonical “latest FO4 exe” list; then always “cautious”.
        latest_exe_aliases=frozenset(),
    ),
}


@lru_cache(maxsize=32)
def _profile_for_domain(nexus_domain: str) -> _ExtenderProfile | None:
    key = (nexus_domain or "").strip().strip("/").casefold()
    return _EXTENDER_BY_DOMAIN.get(key)


def build_script_extender_fact_block(*, nexus_domain: str, exe_version: str) -> str:
    """
    Korean fact lines for the install-guide user prompt. Empty if this game has no mapped extender.
    """
    prof = _profile_for_domain(nexus_domain)
    if prof is None:
        return ""

    dom = (nexus_domain or "").strip() or "(알 수 없음)"
    norm = normalize_exe_version_string(exe_version)
    url = prof.official_url
    short = prof.short_name

    if not norm:
        return (
            f"[스크립트 확장 도구 안내] Nexus 게임 도메인: {dom}.\n"
            f"MO2에서 실행 파일 버전을 읽지 못했습니다. {short}는 공식 페이지에서 받습니다: {url}\n"
            f"다운로드 페이지에 여러 버전·옵션이 있으면, 본인 게임(스팀/게임스 기준)과 "
            f"맞는 항목을 골라야 합니다. 구체 빌드 번호는 단정하지 마세요."
        )

    if prof.latest_exe_aliases and norm in prof.latest_exe_aliases:
        return (
            f"[스크립트 확장 도구 안내] Nexus 게임 도메인: {dom}.\n"
            f"MO2가 읽은 실행 파일 버전(정규화): {norm} — 플러그인이 보유한 ‘최신 패치’ 목록과 일치합니다.\n"
            f"{short}는 공식 페이지에서 받으면 됩니다: {url}\n"
            f"사용자에게: 특정 {short} 빌드 번호를 지정하지 말고, 위 링크에서 받으라고만 짧게 한국어로 안내."
        )

    latest_hint = (
        ", ".join(sorted(prof.latest_exe_aliases)) if prof.latest_exe_aliases else "(목록 없음 — 항상 주의 안내)"
    )
    return (
        f"[스크립트 확장 도구 안내] Nexus 게임 도메인: {dom}.\n"
        f"MO2가 읽은 실행 파일 버전(정규화): {norm}.\n"
        f"플러그인이 추적하는 ‘최신 패치’ 버전 집합: {latest_hint}\n"
        f"위 집합과 일치하지 않거나 집합이 비어 있으므로, 게임이 최신 패치가 아닐 수 있습니다.\n"
        f"{short} 공식 페이지: {url}\n"
        f"사용자에게: 다운로드할 때 구버전/호환 구분·옵션을 잘 보고, 본인 게임 버전에 맞는 항목을 고르라고 "
        f"한국어로 안내. 구체 빌드 번호는 단정하지 말 것."
    )


__all__ = [
    "build_script_extender_fact_block",
    "normalize_exe_version_string",
]
