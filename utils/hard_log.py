"""
Append-only physical log: ``wepawn_debug.log`` (see :mod:`wepawn_storage` for directory).

After :func:`wepawn_storage.configure_wepawn_data_dir_from_organizer` in ``plugin.init``,
the file is usually **``{organizer.basePath()}/WepawnAI/wepawn_debug.log``**.
If that folder is not writable, storage falls back to the plugin directory; each
:func:`_hard_log` call also retries the plugin path if the primary append raises ``OSError``.

Used for diagnostics that must not go through MO2/Qt console paths (``print``, ``qInfo``,
``mobase.logMessage``, etc.).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from .wepawn_storage import wepawn_data_dir

_LOCK = threading.Lock()
_LOG_FILENAME = "wepawn_debug.log"


def _plugin_root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _hard_log(message: str) -> None:
    """Append one UTF-8 line with UTC ISO timestamp to ``wepawn_debug.log``."""
    primary = wepawn_data_dir() / _LOG_FILENAME
    fallback = _plugin_root_dir() / _LOG_FILENAME
    paths: list[Path] = [primary]
    try:
        if primary.resolve() != fallback.resolve():
            paths.append(fallback)
    except OSError:
        paths.append(fallback)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    line = f"{ts} {message}\n"
    last_exc: OSError | None = None
    for path in paths:
        try:
            with _LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fp:
                    fp.write(line)
            return
        except OSError as exc:
            last_exc = exc
    try:
        print(
            f"[WepawnAI _hard_log OSError] {last_exc!r} message={message[:500]!r}",
            flush=True,
        )
    except Exception:
        pass


# Explicit alias for diagnostics / user-facing log instructions (same sink as ``_hard_log``).
python_hard_log = _hard_log
