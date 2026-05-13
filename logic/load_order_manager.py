"""
Guide-completion hook: run LOOT with ``--auto-sort`` via MO2 ``startApplication``.

LOOT resolution order:
1. ``{organizer.basePath()}/loot*/LOOT.exe`` (glob) and case fallback under ``loot*`` dirs
2. MO2 executables list entry titled LOOT / Loot
3. Windows: Uninstall registry ``InstallLocation`` / common install paths
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import mobase

from ..utils.hard_log import _hard_log
from ..utils.mo2_nemesis_launch import _executable_path_from_mo

_LOOT_EXECUTABLE_TITLES: tuple[str, ...] = ("LOOT", "Loot")
_LOOT_GLOB_PATTERNS: tuple[str, ...] = ("loot*/LOOT.exe", "loot*/loot.exe")

# LOOT ``--game`` values (case-sensitive per LOOT docs).
_NEXUS_DOMAIN_TO_LOOT_GAME: dict[str, str] = {
    "starfield": "Starfield",
    "morrowind": "Morrowind",
    "oblivion": "Oblivion",
    "oblivionremastered": "Oblivion Remastered",
    "skyrim": "Skyrim",
    "skyrimspecialedition": "Skyrim Special Edition",
    "skyrimvr": "Skyrim VR",
    "fallout3": "Fallout3",
    "falloutnv": "FalloutNV",
    "fallout4": "Fallout4",
    "fallout4vr": "Fallout4VR",
    "nehrim": "Nehrim",
    "enderal": "Enderal",
    "enderalforgottenstories": "Enderal",
    "enderalspecialedition": "Enderal Special Edition",
    "openmw": "OpenMW",
}

_GAME_NAME_TO_LOOT_GAME: dict[str, str] = {
    "skyrim special edition": "Skyrim Special Edition",
    "skyrim se": "Skyrim Special Edition",
    "skyrim: special edition": "Skyrim Special Edition",
    "the elder scrolls v: skyrim special edition": "Skyrim Special Edition",
    "skyrim": "Skyrim",
    "the elder scrolls v: skyrim": "Skyrim",
    "fallout 4": "Fallout4",
    "fallout4": "Fallout4",
    "fallout new vegas": "FalloutNV",
    "falloutnv": "FalloutNV",
    "fallout 3": "Fallout3",
    "fallout3": "Fallout3",
    "oblivion": "Oblivion",
    "morrowind": "Morrowind",
    "starfield": "Starfield",
}


def _loot_game_id_for_organizer(organizer: mobase.IOrganizer) -> str | None:
    try:
        mg = organizer.managedGame()
    except Exception:
        mg = None
    if mg is None:
        return None
    try:
        raw = mg.gameNexusName()
        dom = str(raw or "").strip().casefold().replace(" ", "").replace("_", "")
        if dom in _NEXUS_DOMAIN_TO_LOOT_GAME:
            return _NEXUS_DOMAIN_TO_LOOT_GAME[dom]
    except Exception:
        pass
    for meth in ("gameName", "gameShortName"):
        fn = getattr(mg, meth, None)
        if not callable(fn):
            continue
        try:
            g = str(fn() or "").strip().casefold()
        except Exception:
            continue
        if g in _GAME_NAME_TO_LOOT_GAME:
            return _GAME_NAME_TO_LOOT_GAME[g]
    return None


def _loot_game_install_path(organizer: mobase.IOrganizer) -> str | None:
    try:
        mg = organizer.managedGame()
    except Exception:
        mg = None
    if mg is None:
        return None
    try:
        gd = mg.gameDirectory()
        if gd is None:
            return None
        ap = gd.absolutePath()
        s = str(ap).strip() if ap is not None else ""
        return s if s else None
    except Exception:
        return None


def _loot_cli_kv(flag: str, value: str) -> str:
    """
    One LOOT/MO2 argv token. MO2 joins ``QStringList`` with spaces and passes a single
    ``CreateProcess`` command line, so values with spaces must be quoted or LOOT never
    sees a valid ``--game`` / ``--game-path`` (e.g. ``Skyrim Special Edition``).
    """
    v = str(value).strip()
    if not v:
        return flag
    if flag == "--game-path":
        try:
            v = Path(v).as_posix()
        except OSError:
            pass
    if any(c.isspace() for c in v) or '"' in v:
        esc = v.replace('"', '\\"')
        return f'{flag}="{esc}"'
    return f"{flag}={v}"


def loot_cli_arguments(organizer: mobase.IOrganizer) -> list[str]:
    """
    LOOT requires ``--game`` when using ``--auto-sort`` so it can sort, apply, and quit
    without leaving the main window open.
    """
    out: list[str] = []
    gid = _loot_game_id_for_organizer(organizer)
    if gid:
        out.append(_loot_cli_kv("--game", gid))
        gp = _loot_game_install_path(organizer)
        if gp:
            out.append(_loot_cli_kv("--game-path", gp))
    else:
        _hard_log(
            "[LOOT] could not map managedGame to LOOT --game; "
            "--auto-sort may keep the UI open"
        )
    out.append("--auto-sort")
    return out
_LOOT_EXE_NAMES: tuple[str, ...] = (
    "LOOT.exe",
    "loot.exe",
    "LOOT",
    "loot",
)


def _mo2_loot_under_basepath(organizer: mobase.IOrganizer) -> Path | None:
    """
    ``basePath()/loot*/LOOT.exe`` via :meth:`Path.glob`, then directory scan for names
    starting with ``loot`` (case-insensitive) so POSIX matches ``LOOT_*`` folders too.
    """
    try:
        raw = organizer.basePath()
    except Exception:
        return None
    if not (raw or "").strip():
        return None
    try:
        base = Path(str(raw).strip()).resolve()
    except OSError:
        return None
    if not base.is_dir():
        return None

    candidates: list[Path] = []
    for pattern in _LOOT_GLOB_PATTERNS:
        try:
            for p in base.glob(pattern):
                try:
                    if p.is_file():
                        candidates.append(p.resolve())
                except OSError:
                    continue
        except OSError:
            continue

    if not candidates:
        try:
            for sub in base.iterdir():
                try:
                    if not sub.is_dir():
                        continue
                except OSError:
                    continue
                if not sub.name.casefold().startswith("loot"):
                    continue
                for name in _LOOT_EXE_NAMES:
                    c = sub / name
                    try:
                        if c.is_file():
                            candidates.append(c.resolve())
                    except OSError:
                        continue
        except OSError:
            pass

    if not candidates:
        return None
    candidates.sort(key=lambda p: str(p).casefold())
    return candidates[0]


def _loot_from_mo2_executables(organizer: mobase.IOrganizer) -> Path | None:
    try:
        el = organizer.executablesList()
    except Exception:
        return None
    if el is None:
        return None
    get_bt = getattr(el, "getByTitle", None)
    if not callable(get_bt):
        return None
    for title in _LOOT_EXECUTABLE_TITLES:
        try:
            mo_exe = get_bt(title)
        except Exception:
            mo_exe = None
        path_s = _executable_path_from_mo(mo_exe)
        if path_s:
            try:
                p = Path(path_s).resolve()
            except OSError:
                continue
            if p.is_file():
                return p
    return None


def _loot_from_registry_win32() -> list[Path]:
    out: list[Path] = []
    if sys.platform != "win32":
        return out
    try:
        import winreg
    except ImportError:
        return out
    subkeys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    for hive in hives:
        for sub in subkeys:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    n_sub, _, _ = winreg.QueryInfoKey(k)
                    for i in range(min(n_sub, 512)):
                        try:
                            sk_name = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, sk_name) as h:
                                try:
                                    dn, _ = winreg.QueryValueEx(h, "DisplayName")
                                except OSError:
                                    continue
                                if "loot" not in str(dn).casefold():
                                    continue
                                try:
                                    loc, _ = winreg.QueryValueEx(h, "InstallLocation")
                                except OSError:
                                    continue
                                root = Path(str(loc).strip().strip('"'))
                                for name in _LOOT_EXE_NAMES:
                                    cand = root / name
                                    if cand.is_file():
                                        out.append(cand.resolve())
                                        break
                        except OSError:
                            continue
            except OSError:
                continue
    return out


def _loot_common_paths() -> list[Path]:
    out: list[Path] = []
    if sys.platform == "win32":
        out.extend(
            [
                Path(r"C:\Program Files\LOOT\LOOT.exe"),
                Path(r"C:\Program Files (x86)\LOOT\LOOT.exe"),
            ]
        )
        la = os.environ.get("LOCALAPPDATA", "").strip()
        if la:
            out.append(Path(la) / "Programs" / "LOOT" / "LOOT.exe")
    return out


def resolve_loot_executable(organizer: mobase.IOrganizer) -> str | None:
    """
    Return absolute path, or ``None`` if LOOT cannot be found.

    Priority: MO2 instance ``loot*/LOOT.exe`` → MO2 executables list → registry / defaults.
    """
    bundled = _mo2_loot_under_basepath(organizer)
    if bundled is not None:
        _hard_log(f"[LOOT] resolved: MO2 basePath loot* -> {bundled}")
        return str(bundled)

    mo2_exe = _loot_from_mo2_executables(organizer)
    if mo2_exe is not None:
        _hard_log(f"[LOOT] resolved: MO2 executables list -> {mo2_exe}")
        return str(mo2_exe)

    if sys.platform == "win32":
        for p in _loot_from_registry_win32():
            if p.is_file():
                _hard_log(f"[LOOT] resolved: registry -> {p}")
                return str(p)
        for p in _loot_common_paths():
            try:
                if p.is_file():
                    _hard_log(f"[LOOT] resolved: default path -> {p}")
                    return str(p.resolve())
            except OSError:
                continue

    return None


def is_loot_executable_registered(organizer: mobase.IOrganizer) -> bool:
    """True if LOOT can be launched (bundled, MO2 list, or fallback discovery)."""
    return resolve_loot_executable(organizer) is not None


def _coerce_handle(handle: Any) -> int | None:
    if handle is None:
        return None
    try:
        ih = int(handle)
    except (TypeError, ValueError):
        return None
    if ih in (0, -1):
        return None
    return ih


def _win32_wait_exit_code_and_close(handle_int: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    h = wintypes.HANDLE(handle_int)
    WAIT_OBJECT_0 = 0
    r = kernel32.WaitForSingleObject(h, 0xFFFFFFFF)
    if r != WAIT_OBJECT_0:
        _hard_log(f"[LOOT] WaitForSingleObject returned {r!r}")
    code = wintypes.DWORD(0)
    ok = bool(kernel32.GetExitCodeProcess(h, ctypes.byref(code)))
    kernel32.CloseHandle(h)
    if not ok:
        return None
    return int(code.value)


def _wait_exit_code(handle: Any, organizer: mobase.IOrganizer) -> int | None:
    ih = _coerce_handle(handle)
    if ih is None:
        return None
    if sys.platform == "win32":
        try:
            return _win32_wait_exit_code_and_close(ih)
        except Exception as exc:
            _hard_log(f"[LOOT] win32 wait failed: {exc!r}")
            return None
    wfa = getattr(organizer, "waitForApplication", None)
    if callable(wfa):
        try:
            ok = bool(wfa(handle, True))
            return 0 if ok else 1
        except TypeError:
            try:
                ok = bool(wfa(handle))
                return 0 if ok else 1
            except Exception as exc:
                _hard_log(f"[LOOT] waitForApplication: {exc!r}")
        except Exception as exc:
            _hard_log(f"[LOOT] waitForApplication: {exc!r}")
    return None


def start_loot_auto_sort_in_background(
    organizer: mobase.IOrganizer,
    on_finished: Callable[[int | None], None],
) -> bool:
    """
    Start LOOT with ``--auto-sort``, wait on a daemon thread, then call
    ``on_finished(exit_code)`` (``None`` if unknown). Returns False if launch failed.
    """
    exe = resolve_loot_executable(organizer)
    if not exe:
        _hard_log("[LOOT] no LOOT executable resolved")
        return False

    args = loot_cli_arguments(organizer)
    loot_command_args: list[str] = [exe, *args]
    _hard_log(f"[LOOT] 실행 시도: {loot_command_args!r}")

    sa = getattr(organizer, "startApplication", None)
    if not callable(sa):
        _hard_log("[LOOT] startApplication not available")
        return False
    try:
        try:
            handle = sa(exe, args, "", "", "", False)
        except TypeError:
            handle = sa(exe, args)
    except Exception as exc:
        _hard_log(f"[LOOT] startApplication raised: {exc!r}")
        return False

    if handle is None or (
        isinstance(handle, int) and handle in (0, -1)
    ):
        _hard_log("[LOOT] startApplication returned invalid handle")
        return False

    def _worker() -> None:
        code = _wait_exit_code(handle, organizer)
        try:
            on_finished(code)
        except Exception as exc:
            _hard_log(f"[LOOT] on_finished callback: {exc!r}")

    threading.Thread(target=_worker, name="WepawnAI-LOOT-wait", daemon=True).start()
    return True
