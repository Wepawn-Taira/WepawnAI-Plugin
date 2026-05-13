"""
Managed-game context for script extender loader names and paths.

Maps ``managedGame().gameNexusName()`` (Nexus domain style) to the expected
loader executable basename (e.g. ``skse64_loader.exe``, ``f4se_loader.exe``).
"""

from __future__ import annotations

import os

import mobase

# Normalized like ``load_order_manager._loot_game_id_for_organizer`` domain keys.
_NEXUS_DOMAIN_TO_SCRIPT_LOADER: dict[str, str] = {
    "skyrimspecialedition": "skse64_loader.exe",
    "skyrim": "skse_loader.exe",
    "enderalspecialedition": "skse64_loader.exe",
    "enderal": "skse_loader.exe",
    "fallout4": "f4se_loader.exe",
    "falloutnewvegas": "nvse_loader.exe",
    "fallout3": "fose_loader.exe",
    "oblivion": "obse_loader.exe",
}

# Nexus mod IDs for manual script-extender download pages (guide flow).
_NEXUS_DOMAIN_TO_SCRIPT_EXTENDER_MOD_ID: dict[str, int] = {
    "fallout4": 42147,
    "skyrimspecialedition": 30379,
    "enderalspecialedition": 30379,
}
_DEFAULT_SCRIPT_EXTENDER_NEXUS_MOD_ID = 30379


def _normalized_nexus_domain(organizer: mobase.IOrganizer | None) -> str:
    if organizer is None:
        return ""
    try:
        mg = organizer.managedGame()
    except Exception:
        mg = None
    if mg is None:
        return ""
    try:
        raw = mg.gameNexusName()
        return str(raw or "").strip().casefold().replace(" ", "").replace("_", "")
    except Exception:
        return ""


def script_extender_loader_basename_for_organizer(
    organizer: mobase.IOrganizer | None,
) -> str:
    """Loader exe basename for the current managed game (default: SSE SKSE64)."""
    dom = _normalized_nexus_domain(organizer)
    if dom in _NEXUS_DOMAIN_TO_SCRIPT_LOADER:
        return _NEXUS_DOMAIN_TO_SCRIPT_LOADER[dom]
    return "skse64_loader.exe"


def script_extender_nexus_mod_id_for_organizer(
    organizer: mobase.IOrganizer | None,
) -> int:
    """
    Nexus mod ID for the script extender manual-download page for the managed game
    (e.g. SKSE64 SSE 30379, F4SE 42147).
    """
    dom = _normalized_nexus_domain(organizer)
    return _NEXUS_DOMAIN_TO_SCRIPT_EXTENDER_MOD_ID.get(
        dom, _DEFAULT_SCRIPT_EXTENDER_NEXUS_MOD_ID
    )


# Games where we prepend the script extender to the guide prereq list when missing.
_SCRIPT_EXTENDER_INJECT_DOMAINS: frozenset[str] = frozenset(
    {"fallout4", "skyrimspecialedition", "enderalspecialedition"}
)


def should_inject_script_extender_prereq(organizer: mobase.IOrganizer | None) -> bool:
    """True for FO4 / SSE / Enderal SE when the guide should always offer SE first."""
    return _normalized_nexus_domain(organizer) in _SCRIPT_EXTENDER_INJECT_DOMAINS


def script_extender_prereq_label_for_organizer(
    organizer: mobase.IOrganizer | None,
) -> str:
    """Display label for the mandatory script-extender step (matches Nexus listing short names)."""
    dom = _normalized_nexus_domain(organizer)
    if dom == "fallout4":
        return "F4SE"
    if dom in ("skyrimspecialedition", "enderalspecialedition"):
        return "SKSE64"
    return "Script Extender"


def game_folder_short_label_for_organizer(organizer: mobase.IOrganizer | None) -> str:
    """Short game name for UI strings (game root folder, path errors)."""
    dom = _normalized_nexus_domain(organizer)
    if dom == "fallout4":
        return "Fallout 4"
    if dom in ("skyrimspecialedition", "enderalspecialedition"):
        return "Skyrim"
    if dom == "skyrim":
        return "Skyrim"
    if dom == "enderal":
        return "Enderal"
    return "game"


def loader_path_exists(
    game_dir: str | None, basename: str
) -> tuple[str, bool]:
    """Absolute path to ``basename`` under game root and ``os.path.exists`` (OSError-safe)."""
    if not (game_dir or "").strip() or not (basename or "").strip():
        return ("", False)
    root = os.path.normpath(str(game_dir).strip())
    exe_path = os.path.normpath(os.path.join(root, basename))
    try:
        ok = os.path.exists(exe_path)
    except OSError:
        ok = False
    return (exe_path, ok)


def script_extender_loader_absolute_path(
    organizer: mobase.IOrganizer | None,
    game_dir: str | None,
) -> tuple[str, str]:
    """
    Returns ``(absolute_path, basename)`` for the script extender loader in the game root.
    ``game_dir`` should be MO2 managed game directory (e.g. from ``IOrganizer``).
    """
    base = script_extender_loader_basename_for_organizer(organizer)
    g = (game_dir or "").strip() or None
    abs_path, _ = loader_path_exists(g, base)
    if abs_path:
        return (abs_path, base)
    if g:
        return (os.path.normpath(os.path.join(os.path.normpath(g), base)), base)
    return ("", base)
