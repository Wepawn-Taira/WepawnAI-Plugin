"""MO2 profile discovery and UI helpers for onboarding."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import mobase

from ..nexus.dependencies import NEXUS_REST_V1, NexusAPIError, _nexus_get_json
from ..utils.hard_log import _hard_log

_SKIP_PROFILE_SCAN_NAMES: frozenset[str] = frozenset(
    {
        "System Volume Information",
        "$RECYCLE.BIN",
        "FOUND.000",
        "Recovery",
    }
)


def validate_nexus_api_key(
    api_key: str,
    *,
    application: str,
    timeout: float = 20.0,
) -> bool:
    key = (api_key or "").strip()
    if not key:
        return False
    url = f"{NEXUS_REST_V1}/games.json"
    try:
        data = _nexus_get_json(url, api_key=key, application=application, timeout=timeout)
    except (NexusAPIError, OSError, TypeError, ValueError) as exc:
        _hard_log(f"[ONBOARD] Nexus validate failed: {exc!r}")
        return False
    return data is not None


def list_profile_names(
    organizer: mobase.IOrganizer,
) -> tuple[list[str], bool]:
    """
    Returns (names, api_success).

    ``api_success`` is True only when ``profileNames()`` returned at least one name.
    On failure or empty API result, the parent of ``profilePath()`` is scanned; then
    ``api_success`` is False.
    """
    names_fn = getattr(organizer, "profileNames", None)
    out: list[str] = []
    if names_fn is not None and callable(names_fn):
        try:
            raw = names_fn()
            if raw is not None:
                seq = list(raw) if isinstance(raw, (list, tuple)) else list(raw)
                out = sorted(
                    {str(x).strip() for x in seq if str(x).strip()},
                    key=str.casefold,
                )
        except Exception as exc:
            _hard_log(f"[PROFILE] profileNames() failed: {exc!r}")
            out = []
    if out:
        api_success = True
        _hard_log(f"[PROFILE] 탐색 방법: {'API' if api_success else 'Folder Scan'}")
        return out, api_success
    scanned = _profiles_from_folder_scan(organizer)
    api_success = False
    _hard_log(f"[PROFILE] 탐색 방법: {'API' if api_success else 'Folder Scan'}")
    return scanned, api_success


def _profiles_from_folder_scan(organizer: mobase.IOrganizer) -> list[str]:
    try:
        pp = organizer.profilePath()
    except Exception:
        return []
    if not (pp or "").strip():
        return []
    try:
        cur = Path(str(pp).strip()).resolve()
    except OSError:
        return []
    parent = cur.parent
    if not parent.is_dir():
        return []
    out: list[str] = []
    try:
        entries = os.listdir(parent)
    except OSError:
        return []
    for name in entries:
        if not name or name.startswith("."):
            continue
        if name in _SKIP_PROFILE_SCAN_NAMES:
            continue
        p = parent / name
        try:
            if not p.is_dir():
                continue
        except OSError:
            continue
        if sys.platform == "win32":
            try:
                st = os.stat(p)
                attrs = int(getattr(st, "st_file_attributes", 0))
                if attrs & 0x2:
                    continue
            except (OSError, ValueError):
                pass
        out.append(name)
    return sorted(set(out), key=str.casefold)


def try_show_profile_manager(
    organizer: mobase.IOrganizer,
    parent: Any | None,
) -> bool:
    fn = getattr(organizer, "showProfileManager", None)
    if fn is None or not callable(fn):
        _hard_log("[ONBOARD] showProfileManager not available on IOrganizer")
        return False
    try:
        if parent is not None:
            fn(parent)
        else:
            fn()
        return True
    except TypeError:
        try:
            fn()
            return True
        except Exception as exc:
            _hard_log(f"[ONBOARD] showProfileManager failed: {exc!r}")
            return False
    except Exception as exc:
        _hard_log(f"[ONBOARD] showProfileManager failed: {exc!r}")
        return False
