"""
Three-tier classification for mod archive paths (Bethesda / MO2 context).

Red (.dll): version policy can block installs on explicit mismatch (stub PE parsing).
Yellow (.esp / .esm / .esl): warn-only.
Green (.dds, .nif, common sound formats): pass automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import PurePosixPath
from typing import Iterable, Mapping, Sequence


class ArchiveTier(Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"


class VersionCheckOutcome(Enum):
    """Result of comparing a native DLL against the managed game version (stub)."""

    OK = "ok"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TieredPath:
    """Single path inside an archive with its tier."""

    path: str
    tier: ArchiveTier


@dataclass
class ArchiveAnalysisResult:
    """Aggregated guidance for installers or AI agents."""

    entries: list[TieredPath] = field(default_factory=list)
    blocked: bool = False
    block_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_SOUND_EXTS: frozenset[str] = frozenset(
    {
        ".wav",
        ".xwm",
        ".fuz",
        ".ogg",
        ".mp3",
        ".opus",
        ".flac",
        ".aac",
    }
)

_GREEN_EXTS: frozenset[str] = frozenset({".dds", ".nif"}) | _SOUND_EXTS
_YELLOW_EXTS: frozenset[str] = frozenset({".esp", ".esm", ".esl"})
_RED_EXTS: frozenset[str] = frozenset({".dll"})


def classify_archive_path(relative_path: str) -> ArchiveTier:
    """
    Classify one archive member path by extension (case-insensitive, POSIX-style).
    Unknown extensions are treated as Yellow (conservative for Bethesda data files).
    """
    normalized = relative_path.replace("\\", "/").strip()
    if not normalized or normalized.endswith("/"):
        return ArchiveTier.GREEN
    ext = PurePosixPath(normalized).suffix.lower()
    if ext in _RED_EXTS:
        return ArchiveTier.RED
    if ext in _YELLOW_EXTS:
        return ArchiveTier.YELLOW
    if ext in _GREEN_EXTS:
        return ArchiveTier.GREEN
    return ArchiveTier.YELLOW


def stub_dll_version_check(
    relative_dll_path: str,
    game_version: str,
    dll_version_map: Mapping[str, str] | None = None,
) -> VersionCheckOutcome:
    """
    Stub DLL version analysis.

    Real builds should parse the PE resource or use game-specific rules. For now:

    - If ``dll_version_map`` maps the DLL **basename** (e.g. ``skse64_loader.dll``)
      to a version string, it is compared (case-sensitive) to ``game_version``.
    - Otherwise the outcome is :attr:`VersionCheckOutcome.UNKNOWN`.
    """
    base = os.path.basename(relative_dll_path.replace("\\", "/"))
    if dll_version_map and base in dll_version_map:
        declared = dll_version_map[base].strip()
        target = game_version.strip()
        if declared and target and declared != target:
            return VersionCheckOutcome.MISMATCH
        return VersionCheckOutcome.OK
    return VersionCheckOutcome.UNKNOWN


def analyze_mod_archive_contents(
    relative_paths: Iterable[str],
    *,
    game_version: str,
    dll_version_map: Mapping[str, str] | None = None,
    path_messages: Mapping[ArchiveTier, Mapping[str, str]] | None = None,
) -> ArchiveAnalysisResult:
    """
    Walk archive member paths and produce tier lists plus block/warn strings.

    ``path_messages`` may supply ``str.format`` templates per :class:`ArchiveTier`
    (``block`` / ``unknown`` for RED, ``warn`` for YELLOW) with placeholders
    ``path`` and ``game_version``. GREEN tier entries are recorded but do not emit
    messages here.
    """
    msgs = path_messages or {}
    red_tpl = msgs.get(ArchiveTier.RED, {}).get("block", "")
    red_unknown_tpl = msgs.get(ArchiveTier.RED, {}).get("unknown", "")
    yellow_tpl = msgs.get(ArchiveTier.YELLOW, {}).get("warn", "")

    result = ArchiveAnalysisResult()
    seen_dlls: set[str] = set()

    for raw in relative_paths:
        tier = classify_archive_path(raw)
        result.entries.append(TieredPath(path=raw, tier=tier))

        if tier is ArchiveTier.RED:
            if raw in seen_dlls:
                continue
            seen_dlls.add(raw)
            outcome = stub_dll_version_check(raw, game_version, dll_version_map)
            if outcome is VersionCheckOutcome.MISMATCH:
                result.blocked = True
                if red_tpl:
                    result.block_reasons.append(
                        red_tpl.format(path=raw, game_version=game_version)
                    )
            elif outcome is VersionCheckOutcome.UNKNOWN and red_unknown_tpl:
                result.warnings.append(
                    red_unknown_tpl.format(path=raw, game_version=game_version)
                )

        elif tier is ArchiveTier.YELLOW and yellow_tpl:
            result.warnings.append(yellow_tpl.format(path=raw, game_version=game_version))

    return result


def summarize_tiers(entries: Sequence[TieredPath]) -> dict[ArchiveTier, list[str]]:
    """Helper for UI: group paths by tier."""
    buckets: dict[ArchiveTier, list[str]] = {t: [] for t in ArchiveTier}
    for e in entries:
        buckets[e.tier].append(e.path)
    return buckets
