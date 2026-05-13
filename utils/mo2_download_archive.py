"""
Resolve an archive path for FOMOD inspection: MO2 download list selection, then API/folder fallbacks.
"""

from __future__ import annotations

import os
from pathlib import Path

import mobase
from PyQt6.QtCore import QModelIndex
from PyQt6.QtWidgets import QApplication, QTreeView

from ..ai.llm_client import _diag

_ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")


def _is_archive_path(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in _ARCHIVE_SUFFIXES


def _organizer_downloads_dir(organizer: mobase.IOrganizer) -> Path | None:
    for meth in ("downloadsPath",):
        fn = getattr(organizer, meth, None)
        if fn is None or not callable(fn):
            continue
        try:
            raw = fn()
        except Exception:
            continue
        if raw is None:
            continue
        try:
            d = Path(str(raw).strip()).expanduser()
            d = d.resolve()
        except Exception:
            continue
        if d.is_dir():
            return d
    paths_obj = getattr(organizer, "paths", None)
    if paths_obj is None or not callable(paths_obj):
        return None
    try:
        po = paths_obj()
    except Exception:
        return None
    if po is None:
        return None
    for attr in ("downloadsPath", "downloadPath", "downloads"):
        g = getattr(po, attr, None)
        if g is None or not callable(g):
            continue
        try:
            raw = g()
        except Exception:
            continue
        if raw is None:
            continue
        try:
            d = Path(str(raw).strip()).expanduser().resolve()
        except Exception:
            continue
        if d.is_dir():
            return d
    return None


def _proxy_to_source_row(index: QModelIndex) -> int:
    """Map a selection index through QSortFilterProxyModel (if any) to the source row index."""
    idx = index
    model = idx.model()
    while model is not None:
        map_to_src = getattr(model, "mapToSource", None)
        if map_to_src is None or not callable(map_to_src):
            break
        try:
            idx = map_to_src(idx)
        except Exception:
            return -1
        if not idx.isValid():
            return -1
        model = idx.model()
    return idx.row()


def _gui_selected_download_rows() -> list[int]:
    app = QApplication.instance()
    if app is None:
        return []
    main_window = None
    for w in app.topLevelWidgets():
        if w.objectName() == "MainWindow":
            main_window = w
            break
    if main_window is None:
        return []
    tree = main_window.findChild(QTreeView, "downloadView")
    if tree is None:
        return []
    sm = tree.selectionModel()
    if sm is None:
        return []
    rows: list[int] = []
    for idx in sm.selectedRows(0):
        r = _proxy_to_source_row(idx)
        if r >= 0:
            rows.append(r)
    return rows


def _download_path_for_row(dm: object, row: int) -> str | None:
    dp = getattr(dm, "downloadPath", None)
    if dp is None or not callable(dp):
        return None
    try:
        raw = dp(int(row))
    except Exception:
        return None
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _try_paths_from_download_manager(organizer: mobase.IOrganizer) -> tuple[str | None, str]:
    dm_get = getattr(organizer, "downloadManager", None)
    if dm_get is None or not callable(dm_get):
        return None, "no_download_manager"
    try:
        dm = dm_get()
    except Exception:
        return None, "download_manager_call_failed"
    if dm is None:
        return None, "download_manager_none"

    rows = _gui_selected_download_rows()
    if rows:
        for row in rows:
            path_s = _download_path_for_row(dm, row)
            if not path_s:
                _diag(f"FOMOD_TARGET download_row={row} downloadPath returned empty")
                continue
            p = Path(path_s)
            try:
                p = p.resolve()
            except Exception:
                pass
            if _is_archive_path(p):
                _diag(f"FOMOD_TARGET source=download_list_selection row={row}")
                return str(p), "download_list_selection"
            _diag(
                f"FOMOD_TARGET download_row={row} path={path_s!r} skipped "
                f"(not a .zip/.7z/.rar or missing)"
            )
        return None, "download_selection_not_archive"

    return None, "no_download_selection"


def _newest_archive_in_dir(d: Path) -> Path | None:
    best: Path | None = None
    best_mtime: float = -1.0
    try:
        with os.scandir(d) as it:
            for ent in it:
                if not ent.is_file():
                    continue
                suf = Path(ent.name).suffix.lower()
                if suf not in _ARCHIVE_SUFFIXES:
                    continue
                try:
                    st = ent.stat()
                except OSError:
                    continue
                if st.st_mtime >= best_mtime:
                    best_mtime = st.st_mtime
                    best = Path(ent.path)
    except OSError:
        return None
    return best


def resolve_target_archive_path(
    organizer: mobase.IOrganizer | None,
) -> tuple[str | None, str]:
    """
    Return ``(absolute_path, source_tag)``.

    Order: download tab selection + ``downloadPath(row)``; then newest archive in MO2 downloads folder.
    """
    if organizer is None:
        _diag("FOMOD_TARGET organizer=None")
        return None, "organizer_none"

    path, tag = _try_paths_from_download_manager(organizer)
    if path:
        _diag(f"FOMOD_TARGET resolved path={path!r} source={tag!r}")
        return path, tag
    if tag == "download_selection_not_archive":
        _diag("FOMOD_TARGET selection present but no .zip/.7z/.rar at those rows; not using folder fallback")
        return None, tag

    dl = _organizer_downloads_dir(organizer)
    if dl is None:
        _diag(f"FOMOD_TARGET fallback failed: could not resolve downloads directory ({tag})")
        return None, "no_downloads_dir"

    newest = _newest_archive_in_dir(dl)
    if newest is None:
        _diag(f"FOMOD_TARGET fallback: no archives under {dl!r}")
        return None, "downloads_folder_empty"

    try:
        resolved = str(newest.resolve())
    except Exception:
        resolved = str(newest)
    _diag(
        f"FOMOD_TARGET resolved path={resolved!r} source=newest_in_downloads_folder "
        f"(selection unavailable: {tag})"
    )
    return resolved, "newest_in_downloads_folder"
