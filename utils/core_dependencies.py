"""
Read ``core_dependencies.json`` (plugin root) for pre-extracted requirement rows.

Shape matches ``wepawn_cache.json`` requirement entries: ``requirements[domain:mod_id]``
→ ``{ "data": { "rows", "title_clean", "is_external" }, "timestamp": ... }``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CORE_FILENAME = "core_dependencies.json"
_loaded_mtime: float | None = None
_loaded_root: dict[str, Any] | None = None


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_core_root() -> dict[str, Any]:
    global _loaded_mtime, _loaded_root
    path = _plugin_root() / _CORE_FILENAME
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _loaded_mtime = None
        _loaded_root = {"metadata": {}, "requirements": {}}
        return _loaded_root
    if _loaded_root is not None and _loaded_mtime == mtime:
        return _loaded_root
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        _loaded_root = {"metadata": {}, "requirements": {}}
        _loaded_mtime = mtime
        return _loaded_root
    if not (raw or "").strip():
        _loaded_root = {"metadata": {}, "requirements": {}}
        _loaded_mtime = mtime
        return _loaded_root
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _loaded_root = {"metadata": {}, "requirements": {}}
        _loaded_mtime = mtime
        return _loaded_root
    if not isinstance(data, dict):
        _loaded_root = {"metadata": {}, "requirements": {}}
        _loaded_mtime = mtime
        return _loaded_root
    meta = data.get("metadata")
    req = data.get("requirements")
    _loaded_root = {
        "metadata": meta if isinstance(meta, dict) else {},
        "requirements": req if isinstance(req, dict) else {},
    }
    _loaded_mtime = mtime
    return _loaded_root


def requirements_entry_key(domain: str, mod_id: int) -> str:
    return f"{(domain or '').strip().lower()}:{int(mod_id)}"


def get_core_requirements_bundle(domain: str, mod_id: int) -> dict[str, Any] | None:
    """
    Same logical shape as :meth:`WepawnPersistentCache.get_requirements_bundle`:
    ``rows`` (list), optional ``title_clean``, ``is_external``. No TTL.
    """
    root = _load_core_root()
    req = root.get("requirements")
    if not isinstance(req, dict):
        return None
    ent = req.get(requirements_entry_key(domain, mod_id))
    if not isinstance(ent, dict):
        return None
    data = ent.get("data")
    if not isinstance(data, dict):
        return None
    rows = data.get("rows")
    if not isinstance(rows, list):
        return None
    out = dict(data)
    out["rows"] = list(rows)
    return out
