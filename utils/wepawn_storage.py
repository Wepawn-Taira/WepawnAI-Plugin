"""
Writable directory for WepawnAI persistence (``wepawn_config.json``, ``wepawn_cache.json``,
``wepawn_debug.log``).

Plugins under ``Program Files`` are not user-writable without elevation. After
:func:`configure_wepawn_data_dir_from_organizer` runs (from :meth:`WepawnAIPlugin.init`),
these files live under ``{organizer.basePath()}/WepawnAI`` (including ``wepawn_debug.log``).
If ``basePath()`` is empty or raises, or ``{basePath}/WepawnAI`` is not writable
(mkdir/append probe fails — e.g. locked-down AppData), :func:`wepawn_data_dir` falls back
to the plugin install directory (``plugins/WepawnAI/``).
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_override_dir: Path | None = None

_SUBDIR_NAME = "WepawnAI"


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _probe_data_dir_writable(target: Path) -> bool:
    """``mkdir`` + 짧은 파일 쓰기/삭제로 실제 쓰기 가능 여부 확인."""
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".wepawn_writable_probe"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except OSError:
            pass
        return True
    except OSError:
        return False


def wepawn_data_dir() -> Path:
    """Directory for mutable wepawn JSON/log files."""
    with _lock:
        if _override_dir is not None:
            return _override_dir
    return _plugin_root()


def configure_wepawn_data_dir_from_organizer(organizer: Any) -> None:
    """Set :func:`wepawn_data_dir` from ``organizer.basePath()``; migrate legacy plugin-root files."""
    global _override_dir
    candidate: Path | None = None
    try:
        raw = organizer.basePath()
        if raw is not None:
            s = str(raw).strip()
            if s:
                candidate = (Path(s) / _SUBDIR_NAME).resolve()
    except Exception:
        candidate = None

    plugin_root = _plugin_root()
    dest: Path | None = None
    fallback_from_instance = False
    if candidate is not None:
        if _probe_data_dir_writable(candidate):
            dest = candidate
        else:
            fallback_from_instance = True
            dest = None

    with _lock:
        prev = _override_dir
        _override_dir = dest
        effective = _override_dir

    if fallback_from_instance and candidate is not None:
        try:
            from .hard_log import _hard_log

            _hard_log(
                "[WEPAWN_STORAGE] instance data dir not writable — "
                f"fallback to plugin root. tried={str(candidate)!r}"
            )
        except Exception:
            pass

    if effective is not None and effective != plugin_root:
        if prev != effective:
            _try_migrate_from_plugin_root(plugin_root, effective)

    if prev != effective:
        _reset_wepawn_cache_singleton()


def _try_migrate_from_plugin_root(plugin_root: Path, dest: Path) -> None:
    for name in ("wepawn_config.json", "wepawn_cache.json", "wepawn_debug.log"):
        src = plugin_root / name
        dst = dest / name
        try:
            if dst.exists():
                continue
            if src.is_file():
                shutil.copy2(src, dst)
        except OSError:
            pass


def _reset_wepawn_cache_singleton() -> None:
    try:
        from .cache_manager import WepawnPersistentCache

        with WepawnPersistentCache._singleton_lock:
            WepawnPersistentCache._instance = None
    except Exception:
        pass
