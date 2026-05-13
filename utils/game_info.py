"""
Resolve the MO2 ``managedGame()`` display name and main executable file version.

Uses ``binaryName()`` / ``gameDirectory()`` from ``mobase`` (no hardcoded game or .exe list).
Windows: ``ctypes`` + ``version.dll`` for ``ProductVersion`` / ``FileVersion``.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import mobase


def _diag(msg: str) -> None:
    print(f"[WepawnAI DIAG] {msg}", flush=True)


def _game_directory_path(game: object) -> Path | None:
    try:
        gh = game.gameDirectory()
    except Exception:
        return None
    if gh is None:
        return None
    try:
        ap = gh.absolutePath()
    except Exception:
        ap = str(gh)
    try:
        return Path(str(ap)).resolve()
    except Exception:
        return None


def _resolve_executable_path(organizer: mobase.IOrganizer | None) -> str | None:
    """
    Absolute path to the managed game's main binary, from MO2 APIs only (no game-specific fallbacks).
    """
    try:
        if organizer is None:
            return None
        game = organizer.managedGame()
        if game is None:
            return None
        root = _game_directory_path(game)
        if root is None or not root.is_dir():
            return None

        exe_name = ""
        for meth in ("binaryName", "executableName", "gameExecutable", "executableFileName"):
            fn = getattr(game, meth, None)
            if fn is None:
                continue
            try:
                val = fn() if callable(fn) else fn
            except Exception:
                continue
            if val:
                exe_name = str(val).strip()
                break

        if not exe_name:
            return None

        p = Path(exe_name)
        if p.is_file():
            return str(p.resolve())
        cand = root / exe_name
        if cand.is_file():
            return str(cand.resolve())
        return None
    except Exception:
        return None


def _windows_exe_file_version(exe_path: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        p = Path(exe_path)
        if not p.is_file():
            return ""
        abs_path = str(p.resolve())
        dll = ctypes.WinDLL("version", use_last_error=True)
        size = dll.GetFileVersionInfoSizeW(abs_path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(abs_path, 0, size, buf):
            return ""
        ulen = ctypes.c_uint(0)
        vptr = ctypes.c_void_p()
        if not dll.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(vptr), ctypes.byref(ulen)):
            return ""
        if ulen.value < 4:
            return ""
        trans = ctypes.cast(vptr, ctypes.POINTER(ctypes.c_uint32))[0]
        lang = int(trans) & 0xFFFF
        cp = (int(trans) >> 16) & 0xFFFF
        for key in ("ProductVersion", "FileVersion"):
            sk = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
            v2 = ctypes.c_void_p()
            u2 = ctypes.c_uint(0)
            if dll.VerQueryValueW(buf, sk, ctypes.byref(v2), ctypes.byref(u2)) and u2.value > 0:
                try:
                    s = ctypes.wstring_at(v2)
                except Exception:
                    continue
                if isinstance(s, str) and s.strip():
                    return s.strip()
        return ""
    except Exception:
        return ""


def get_current_game_info(organizer: mobase.IOrganizer | None) -> tuple[str, str]:
    """
    Return ``(game_name, executable_version_string)`` from ``managedGame()`` and the binary on disk.

    ``game_name`` uses ``gameName()`` then ``gameShortName()``. Version prefers PE resource, then
    ``game.gameVersion()``. On any failure, returns empty strings for the affected fields.
    """
    game_name = ""
    game_version = ""
    binary_path: str | None = None
    try:
        if organizer is None:
            _diag("GAME_INFO organizer=None → ('', '')")
            return ("", "")

        game = organizer.managedGame()
        if game is None:
            _diag("GAME_INFO managedGame() is None → ('', '')")
            return ("", "")

        for meth in ("gameName", "gameShortName"):
            fn = getattr(game, meth, None)
            if fn is None or not callable(fn):
                continue
            try:
                raw = fn()
                text = str(raw or "").strip()
                if text:
                    game_name = text
                    break
            except Exception:
                continue

        binary_path = _resolve_executable_path(organizer)
        if binary_path:
            game_version = _windows_exe_file_version(binary_path)

        if not game_version:
            try:
                gv = game.gameVersion()
                if gv is not None:
                    game_version = str(gv).strip()
            except Exception:
                pass

        _diag(
            f"GAME_INFO game_name={game_name!r} binary_path={binary_path!r} game_version={game_version!r}"
        )
        return (game_name, game_version)
    except Exception as exc:
        _diag(f"GAME_INFO exception {type(exc).__name__}: {exc}")
        return ("", "")


def get_skyrim_version(organizer: mobase.IOrganizer | None) -> str:
    """Backward-compatible alias: executable / MO2 version string only."""
    return get_current_game_info(organizer)[1]


def format_game_environment_block(organizer: mobase.IOrganizer | None) -> str:
    """
    Multi-line runtime summary for LLM prompts (managed game + executable version from MO2 only).
    Uses empty-field fallbacks so templates never break.
    """
    name, ver = get_current_game_info(organizer)
    gn = (name or "").strip()
    gv = (ver or "").strip()
    if not gn and not gv:
        return "게임 이름: (읽기 실패)\n실행 파일 버전: (읽기 실패)"
    if not gn:
        return f"게임 이름: (읽기 실패)\n실행 파일 버전: {gv}"
    if not gv:
        return f"게임 이름: {gn}\n실행 파일 버전: (읽기 실패)"
    return f"게임 이름: {gn}\n실행 파일 버전: {gv}"
