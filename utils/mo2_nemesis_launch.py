"""
NEMESIS(Nexus 60033) 실행: MO2 실행 목록 → mods 재귀 → 수동 경로, 사후 안내, Pre-Check.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import mobase
from PyQt6.QtWidgets import QFileDialog, QMessageBox, QWidget

from .hard_log import _hard_log

PLUGIN_INTERNAL_NAME = "WepawnAI"
_PERSIST_NEMESIS_TIP = "nemesis_mo2_executable_tip_shown"

NEMESIS_NEXUS_ID = 60033
FNIS_NEXUS_ID = 3038

_MO2_EXECUTABLE_TITLES: tuple[str, ...] = (
    "Nemesis Unlimited Behavior Engine",
    "Nemesis Unlimited",
    "Nemesis",
    "NEMESIS",
)

_MODS_EXE_HINTS: tuple[str, ...] = (
    "nemesis unlimited behavior engine",
    "nemesis unlimited",
)


def _is_windows() -> bool:
    return sys.platform == "win32"


def mo2_running_as_admin() -> bool:
    """Windows: 현재 프로세스(MO2)가 상승된 토큰인지(대략적)."""
    if not _is_windows():
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _iter_process_exe_names_win32() -> Iterable[str]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snap == INVALID_HANDLE_VALUE or h_snap is None:
        return
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    if not kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
        kernel32.CloseHandle(h_snap)
        return
    while True:
        yield str(pe.szExeFile)
        if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
            break
    kernel32.CloseHandle(h_snap)


def nemesis_like_process_running() -> bool:
    """이미 Nemesis 계열 exe가 떠 있는지(중복 실행 방지)."""
    if not _is_windows():
        return False
    try:
        for exe in _iter_process_exe_names_win32():
            low = exe.casefold()
            if "nemesis" in low and low.endswith(".exe"):
                return True
    except Exception as exc:
        _hard_log(f"[NEMESIS] process enum failed: {exc}")
    return False


def _executable_path_from_mo(exe_obj: Any) -> str | None:
    if exe_obj is None:
        return None
    for attr in ("binaryFile", "binary", "binaryPath"):
        if hasattr(exe_obj, attr):
            try:
                raw = getattr(exe_obj, attr)()
            except TypeError:
                try:
                    raw = getattr(exe_obj, attr)
                except Exception:
                    continue
            except Exception:
                continue
            else:
                if raw is None:
                    continue
                s = str(raw).strip()
                if s:
                    return s
    if hasattr(exe_obj, "binaryInfo"):
        try:
            fi = exe_obj.binaryInfo()
            if fi is not None:
                s = str(fi.absoluteFilePath()).strip()
                if s:
                    return s
        except Exception:
            pass
    return None


def resolve_nemesis_from_mo2_executables(organizer: mobase.IOrganizer) -> Path | None:
    try:
        el = organizer.executablesList()
        if el is None:
            return None
        get_bt = getattr(el, "getByTitle", None)
        if get_bt is None or not callable(get_bt):
            return None
        for title in _MO2_EXECUTABLE_TITLES:
            try:
                mo_exe = get_bt(title)
            except Exception:
                mo_exe = None
            path_s = _executable_path_from_mo(mo_exe)
            if path_s:
                p = Path(path_s)
                if p.is_file():
                    _hard_log(f"[NEMESIS] resolved via MO2 list title={title!r} -> {p}")
                    return p
        return None
    except AttributeError:
        return None
    except Exception as exc:
        _hard_log(f"[NEMESIS] executablesList: {exc}")
        return None


def _find_nemesis_exe_under_mods(mods_root: Path, *, max_files: int = 8000) -> Path | None:
    if not mods_root.is_dir():
        return None
    seen = 0
    for p in mods_root.rglob("*.exe"):
        seen += 1
        if seen > max_files:
            break
        stem = p.stem.casefold()
        name = p.name.casefold()
        for hint in _MODS_EXE_HINTS:
            if hint in name or hint in stem:
                _hard_log(f"[NEMESIS] resolved via mods scan -> {p}")
                return p
        if "nemesis" in stem and "engine" in stem:
            _hard_log(f"[NEMESIS] resolved via mods scan (heuristic) -> {p}")
            return p
    return None


def resolve_nemesis_from_mods_folder(organizer: mobase.IOrganizer) -> Path | None:
    try:
        mp = organizer.modsPath()
    except Exception:
        mp = ""
    root = Path(str(mp or "").strip())
    return _find_nemesis_exe_under_mods(root)


def resolve_nemesis_executable(organizer: mobase.IOrganizer) -> Path | None:
    p = resolve_nemesis_from_mo2_executables(organizer)
    if p is not None:
        return p
    return resolve_nemesis_from_mods_folder(organizer)


def prompt_user_for_nemesis_exe(parent: QWidget, start_dir: str) -> Path | None:
    path, _ = QFileDialog.getOpenFileName(
        parent,
        "Nemesis 실행 파일 선택",
        start_dir,
        "실행 파일 (*.exe);;모든 파일 (*.*)",
    )
    s = (path or "").strip()
    return Path(s) if s else None


def launch_nemesis_with_organizer(
    organizer: mobase.IOrganizer,
    parent: QWidget,
) -> bool:
    """
    Pre-Check 후 ``startApplication``으로 실행. 성공 시 True.

    - 관리자 아님(Windows): 안내 후 False
    - 이미 Nemesis 프로세스: 안내 후 False
    - 경로 없음: 파일 대화상자
    """
    if _is_windows() and not mo2_running_as_admin():
        QMessageBox.warning(
            parent,
            "Nemesis",
            "MO2를 관리자 권한으로 실행해 주세요.",
        )
        return False

    if nemesis_like_process_running():
        QMessageBox.information(
            parent,
            "Nemesis",
            "Nemesis Unlimited Behavior Engine이 이미 실행 중인 것으로 보입니다.\n"
            "새 창을 띄우지 않았습니다.",
        )
        return False

    exe = resolve_nemesis_executable(organizer)
    if exe is None:
        try:
            sd = organizer.modsPath()
        except Exception:
            sd = ""
        exe = prompt_user_for_nemesis_exe(parent, str(sd or ""))
    if exe is None or not exe.is_file():
        QMessageBox.warning(parent, "Nemesis", "실행 파일을 찾지 못했습니다.")
        return False

    cwd = str(exe.parent.resolve())
    bin_s = str(exe.resolve())
    try:
        sa = getattr(organizer, "startApplication", None)
        if sa is None or not callable(sa):
            QMessageBox.warning(
                parent,
                "Nemesis",
                "MO2 API(startApplication)을 사용할 수 없습니다.",
            )
            return False
        try:
            handle = sa(bin_s, [], cwd, "", "", False)
        except TypeError:
            handle = sa(bin_s, [], cwd)
    except Exception as exc:
        _hard_log(f"[NEMESIS] startApplication failed: {exc}")
        QMessageBox.critical(
            parent,
            "Nemesis",
            f"실행에 실패했습니다.\n{type(exc).__name__}: {exc}",
        )
        return False

    if handle is None or (
        isinstance(handle, int) and (handle == 0 or handle == -1)
    ):
        QMessageBox.warning(parent, "Nemesis", "실행이 거부되었거나 핸들을 받지 못했습니다.")
        return False

    _hard_log(f"[NEMESIS] startApplication ok path={bin_s!r}")
    return True


def animation_hint_mod_names(organizer: mobase.IOrganizer | None) -> list[str]:
    """활성 모드 표시 이름 중 애니메이션 관련 힌트(목록 안내용)."""
    if organizer is None:
        return []
    kw = (
        "nemesis",
        "fnis",
        "animation",
        "animated",
        "idle",
        "idles",
        "behavior",
        "behaviour",
        "dodge",
        "movement",
        "jump",
        "sprint",
        "crouch",
        "combat",
        "horse",
        "tkuc",
        "ultimate combat",
    )
    ml = organizer.modList()
    flag = 0
    for attr in ("ACTIVE", "active"):
        if hasattr(mobase.ModState, attr):
            flag = int(getattr(mobase.ModState, attr))
            break
    if not flag:
        flag = 2
    out: list[str] = []
    try:
        for internal in ml.allModsByProfilePriority():
            try:
                st = ml.state(internal)
                if not (int(st) & flag):
                    continue
                dn = str(ml.displayName(internal) or "").strip()
            except Exception:
                continue
            if not dn:
                continue
            low = dn.casefold()
            if any(k in low for k in kw):
                out.append(dn)
    except Exception as exc:
        _hard_log(f"[NEMESIS] animation_hint_mod_names: {exc}")
    return sorted(set(out), key=str.casefold)


def nemesis_mo2_list_tip_needed(organizer: mobase.IOrganizer) -> bool:
    try:
        raw = organizer.persistent(PLUGIN_INTERNAL_NAME, _PERSIST_NEMESIS_TIP, False)
    except Exception:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("1", "true", "yes")
    return not bool(raw)


def mark_nemesis_mo2_list_tip_shown(organizer: mobase.IOrganizer) -> None:
    try:
        organizer.setPersistent(PLUGIN_INTERNAL_NAME, _PERSIST_NEMESIS_TIP, True, True)
    except Exception as exc:
        _hard_log(f"[NEMESIS] setPersistent tip: {exc}")
