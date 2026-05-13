"""
MO2 session authority checks via ``mobase.IOrganizer`` and Qt ``QFileInfo``.

Resolves ``7z.exe`` from :meth:`mobase.IOrganizer.basePath` (and ancestors). If MO2 does not
ship it, falls back to ``<plugin_root>/bin/7za.exe`` (``os.path.join`` + QFileInfo probe).
"""

from __future__ import annotations

import os
from pathlib import Path

import mobase
from PyQt6.QtCore import QFileInfo

from .hard_log import _hard_log, _plugin_root_dir

_TAG = "MO2_AUTHORITY"

# Relative to MO2 install / instance roots (same layout as upstream MO2).
_SEVEN_ZIP_RELATIVE: tuple[Path, ...] = (
    Path("dlls") / "7z.exe",
    Path("7z.exe"),
    Path("tools") / "7z.exe",
    Path("helper") / "7z.exe",
)


def _fi(path_str: str) -> QFileInfo:
    return QFileInfo(path_str)


def _resolve_bundled_7za() -> Path | None:
    """``<plugin_root>/bin/7za.exe`` when present (Qt QFileInfo + absolute path for logging)."""
    cand = os.path.join(str(_plugin_root_dir()), "bin", "7za.exe")
    fi = _fi(cand)
    if not fi.isFile():
        return None
    abs_path = str(Path(cand).resolve())
    _hard_log(f"[WepawnAI DIAG] SEVEN_ZIP_RESOLVED source=bundled path={abs_path!r}")
    return Path(abs_path)


def resolve_mo2_seven_zip_exe(organizer: mobase.IOrganizer | None) -> Path | None:
    """
    Resolve ``7z.exe`` under MO2 via :meth:`mobase.IOrganizer.basePath` and ancestors.

    If not found (or ``organizer`` is ``None`` after skipping MO2 search), use
    ``os.path.join(plugin_root, 'bin', '7za.exe')``. Does not read ``PATH``.
    """
    if organizer is None:
        return _resolve_bundled_7za()

    try:
        base_s = organizer.basePath()
    except Exception as exc:
        _hard_log(f"{_TAG} basePath() raised {type(exc).__name__}: {exc}")
        return _resolve_bundled_7za()

    s = str(base_s).strip() if base_s is not None else ""
    if not s:
        _hard_log(f"{_TAG} seven_zip_resolve basePath_empty=True")
        return _resolve_bundled_7za()

    roots: list[Path] = []
    seen: set[str] = set()
    try:
        b = Path(s)
        roots.append(b)
        for anc in b.parents:
            roots.append(anc)
            if len(roots) >= 14:
                break
    except Exception:
        roots = [Path(s)]

    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        for rel in _SEVEN_ZIP_RELATIVE:
            cand = root / rel
            try:
                cs = str(cand)
            except Exception:
                continue
            fi = _fi(cs)
            if fi.isFile():
                _hard_log(f"{_TAG} seven_zip_via_mobase_basePath={s!r} resolved={cs!r}")
                return Path(cs)
    _hard_log(
        f"{_TAG} seven_zip_via_mobase NOT_FOUND basePath={s!r} "
        f"roots_tried={len(roots)}"
    )
    return _resolve_bundled_7za()


def _probe_path(label: str, path_str: str) -> None:
    fi = _fi(path_str)
    exists = fi.exists()
    is_dir = fi.isDir()
    is_file = fi.isFile()
    readable = fi.isReadable() if exists else False
    _hard_log(
        f"{_TAG} PING {label} path={path_str!r} "
        f"exists={exists} isDir={is_dir} isFile={is_file} readable={readable}"
    )


def ping_mo2_api_authority(
    organizer: mobase.IOrganizer,
    *,
    archive_abs: str | None = None,
) -> None:
    """
    Call mobase path APIs and Qt QFileInfo probes; log results for sandbox / permission fact-checks.
    """
    # downloadsPath
    dp: str | None = None
    try:
        raw = organizer.downloadsPath()
        dp = str(raw).strip() if raw is not None else None
    except Exception as exc:
        _hard_log(f"{_TAG} PING downloadsPath() raised {type(exc).__name__}: {exc}")
    if dp:
        _probe_path("downloadsPath()", dp)
    else:
        _hard_log(f"{_TAG} PING downloadsPath missing_or_empty=True")

    # basePath
    bp: str | None = None
    try:
        raw = organizer.basePath()
        bp = str(raw).strip() if raw is not None else None
    except Exception as exc:
        _hard_log(f"{_TAG} PING basePath() raised {type(exc).__name__}: {exc}")
    if bp:
        _probe_path("basePath()", bp)
    else:
        _hard_log(f"{_TAG} PING basePath missing_or_empty=True")

    # plugin data path (instance or static, depending on MO2 build)
    pdp: str | None = None
    for gpd in (
        getattr(organizer, "getPluginDataPath", None),
        getattr(mobase.IOrganizer, "getPluginDataPath", None),
    ):
        if not callable(gpd):
            continue
        try:
            raw = gpd()
        except Exception as exc:
            _hard_log(f"{_TAG} PING getPluginDataPath() raised {type(exc).__name__}: {exc}")
            continue
        if raw is not None:
            pdp = str(raw).strip()
            break
    if pdp:
        _probe_path("getPluginDataPath()", pdp)
    else:
        _hard_log(f"{_TAG} PING getPluginDataPath missing_or_empty=True")

    # First installed mod folder via modList (native mod filesystem visibility)
    try:
        ml = organizer.modList()
        names = ml.allMods()
    except Exception as exc:
        _hard_log(f"{_TAG} PING modList/allMods raised {type(exc).__name__}: {exc}")
        names = ()
    if names:
        first = str(names[0]).strip()
        try:
            mod = ml.getMod(first)
        except Exception as exc:
            mod = None
            _hard_log(f"{_TAG} PING getMod({first!r}) raised {type(exc).__name__}: {exc}")
        if mod is not None:
            try:
                ap = mod.absolutePath()
                ap_s = str(ap).strip() if ap is not None else ""
            except Exception as exc:
                ap_s = ""
                _hard_log(f"{_TAG} PING mod.absolutePath() raised {type(exc).__name__}: {exc}")
            if ap_s:
                _probe_path(f"mod.absolutePath(first={first!r})", ap_s)
            else:
                _hard_log(f"{_TAG} PING mod.absolutePath empty for mod={first!r}")
        else:
            _hard_log(f"{_TAG} PING getMod returned None for first={first!r}")
    else:
        _hard_log(f"{_TAG} PING modList allMods empty (no probe mod)")

    if archive_abs:
        _probe_path("archive_abs(target)", archive_abs)

    sz = resolve_mo2_seven_zip_exe(organizer)
    _hard_log(f"{_TAG} PING seven_zip_executable_resolved={sz is not None}")
