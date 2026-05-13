"""
Scan MO2 active mods and keep only entries that match core framework / body keywords
(for compact LLM context). Names come from ``mobase`` at runtime only.
"""

from __future__ import annotations

import mobase

from ..ai.llm_client import _diag

# Substring match (case-insensitive) against display or internal folder name.
# Product / framework tokens only — no user-specific strings.
CORE_KEYWORDS: tuple[str, ...] = (
    "SKSE",
    "skse",
    "SkyUI",
    "CBBE",
    "UNP",
    "BHUNP",
    "RaceMenu",
    "Race Menu",
    "FNIS",
    "Nemesis",
    "Address Library",
    "PapyrusUtil",
    "USSEP",
    "Unofficial Patch",
    "BodySlide",
    "Outfit Studio",
    "3BBB",
    "HDT",
    "SMP",
    "XP32",
    "Skeleton",
    "DynDOLOD",
    "xLODGen",
    "SSE Engine Fixes",
    "Engine Fixes",
    "NET Script",
    "JContainers",
    "Mfg Fix",
    "MCM",
    "Mod Configuration Menu",
    "LOOT",
    "BethINI",
)

_MAX_CORE_MOD_NAMES = 96
_MAX_RENDER_CHARS = 3800


def _mod_state_active_flag() -> int:
    for attr in ("ACTIVE", "active"):
        if hasattr(mobase.ModState, attr):
            return int(getattr(mobase.ModState, attr))
    return 2


def _name_matches_core(display: str, internal: str) -> bool:
    d = display.casefold()
    i = internal.casefold()
    for kw in CORE_KEYWORDS:
        k = kw.casefold()
        if k in d or k in i:
            return True
    return False


def get_active_core_mods(organizer: mobase.IOrganizer | None) -> list[str]:
    """
    Active mods (``allModsByProfilePriority``, ``state & ACTIVE``) whose display or internal
    name matches :data:`CORE_KEYWORDS`. Returns display names in profile order, de-duplicated.
    """
    if organizer is None:
        _diag("MOD_SCAN filtered_mods=[] organizer=None")
        return []
    ml = organizer.modList()
    flag = _mod_state_active_flag()
    out: list[str] = []
    seen: set[str] = set()
    try:
        order = ml.allModsByProfilePriority()
    except Exception as exc:
        _diag(f"MOD_SCAN allModsByProfilePriority failed: {exc}")
        return []
    for internal in order:
        if internal is None:
            continue
        try:
            st = ml.state(internal)
        except Exception:
            continue
        try:
            if not (int(st) & flag):
                continue
        except Exception:
            continue
        try:
            disp = str(ml.displayName(internal)).strip()
        except Exception:
            disp = ""
        int_s = str(internal).strip()
        label = disp or int_s
        if not label:
            continue
        if not _name_matches_core(disp or label, int_s):
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= _MAX_CORE_MOD_NAMES:
            break
    _diag(f"MOD_SCAN filtered_mods={out!r} count={len(out)}")
    return out


def format_local_core_mods_block(names: list[str]) -> str:
    """Join for prompts; cap total characters to limit context size."""
    if not names:
        return ""
    lines = [f"- {n}" for n in names]
    text = "\n".join(lines)
    if len(text) <= _MAX_RENDER_CHARS:
        return text
    cut = _MAX_RENDER_CHARS - 40
    trimmed = text[:cut].rstrip()
    return f"{trimmed}\n… (truncated, {_MAX_CORE_MOD_NAMES} name cap / char cap)"
