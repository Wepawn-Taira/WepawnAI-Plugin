"""
Read FOMOD metadata from mod archives without writing extracted files to disk.

- ``.zip``: ``zipfile`` (central directory + single-member read).
- ``.7z`` / ``.rar``: MO2 ``7z.exe`` or plugin ``bin/7za.exe`` with ``e … -so`` (stdout only).
"""

from __future__ import annotations

import codecs
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import mobase

from .hard_log import _hard_log
from .mo2_authority import resolve_mo2_seven_zip_exe

NO_FOMOD_MESSAGE = "이 아카이브에서 FOMOD(ModuleConfig.xml)를 찾지 못했습니다."

# 7-Zip / p7zip: RAR5·손상·형식 불일치 등으로 목록/추출이 안 될 때 stderr에 흔함.
def _seven_zip_stderr_unopenable_archive(text: str) -> bool:
    low = (text or "").casefold()
    return "cannot open" in low and "as archive" in low


_MODULE_XML = "moduleconfig.xml"
_INFO_XML = "info.xml"
_FOMOD_DIR = "fomod"

# Fallback inner paths if ``7z l`` parsing fails (wrong archive type, old 7z, etc.).
_SEVEN_ZIP_INNER_PATHS: tuple[str, ...] = (
    r"fomod\ModuleConfig.xml",
    "fomod/ModuleConfig.xml",
    r"fomod\info.xml",
    "fomod/info.xml",
)

_seven_zip_exe_cache: Path | None = None


def _qt_application_dir() -> Path | None:
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return None
    app = QApplication.instance()
    if app is None:
        return None
    try:
        d = Path(str(app.applicationDirPath())).resolve()
    except Exception:
        return None
    return d if d.is_dir() else None


def _iter_mo2_root_candidates() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            r = p.resolve()
        except Exception:
            r = p
        key = str(r)
        if key not in seen:
            seen.add(key)
            roots.append(r)

    try:
        exe = Path(sys.executable).resolve()
        _add(exe.parent)
        for anc in exe.parents:
            _add(anc)
            if len(roots) >= 24:
                break
    except Exception:
        pass

    qdir = _qt_application_dir()
    if qdir is not None:
        _add(qdir)
        for anc in qdir.parents:
            _add(anc)
            if len(roots) >= 32:
                break

    return roots


def _find_seven_zip_executable(organizer: mobase.IOrganizer | None = None) -> Path | None:
    """
    Resolve 7-Zip CLI: :func:`resolve_mo2_seven_zip_exe` (MO2 layout, then ``bin/7za.exe``),
    then MO2 / Qt app directory heuristics (no ``PATH``).
    """
    global _seven_zip_exe_cache
    via_mo = resolve_mo2_seven_zip_exe(organizer)
    if via_mo is not None:
        _seven_zip_exe_cache = via_mo
        return via_mo

    if _seven_zip_exe_cache is not None and _seven_zip_exe_cache.is_file():
        return _seven_zip_exe_cache

    rels = (
        Path("7z.exe"),
        Path("dlls") / "7z.exe",
        Path("tools") / "7z.exe",
        Path("helper") / "7z.exe",
    )
    for base in _iter_mo2_root_candidates():
        for rel in rels:
            cand = base / rel
            try:
                if cand.is_file():
                    _seven_zip_exe_cache = cand
                    _hard_log(f"FOMOD_7Z resolved seven_zip_exe fallback={str(cand)!r}")
                    return cand
            except OSError:
                continue

    _hard_log("FOMOD_7Z seven_zip_exe not found under MO2 / app directory candidates")
    return None


def _subprocess_run_kw() -> dict:
    kw: dict = {"capture_output": True}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kw


def _seven_zip_list_paths_slt(
    seven_zip: Path, archive: Path
) -> tuple[list[str], int, list[str], str]:
    """
    Run ``7z l -slt`` and collect ``Path = ...`` entries (archive-internal paths as 7-Zip reports them).

    Returns ``(paths, returncode, argv, stderr_plus_stdout_text)`` for RAR open-failure heuristics.
    """
    cmd = [str(seven_zip), "l", "-slt", str(archive)]
    try:
        completed = subprocess.run(
            cmd,
            timeout=120,
            check=False,
            **_subprocess_run_kw(),
        )
    except subprocess.TimeoutExpired as exc:
        err_b = exc.stderr
        if isinstance(err_b, bytes) and err_b.strip():
            err_s = err_b.decode("utf-8", errors="replace").strip()
        else:
            err_s = "timeout after 120s"
        _hard_log(f"FOMOD_7Z_EXEC_FAILED: stderr=({err_s})")
        out_part = exc.output
        raw_xml_path_lines: list[str] = []
        if isinstance(out_part, bytes) and out_part:
            partial = out_part.decode("utf-8", errors="replace")
            for line in partial.splitlines():
                if line.strip().startswith("Path = ") and ".xml" in line.casefold():
                    raw_xml_path_lines.append(line)
        _hard_log(f"FOMOD_RAW_XML_PATHS: {raw_xml_path_lines!r}")
        return [], -1, cmd, err_s

    out_b = completed.stdout or b""
    err_b = completed.stderr or b""
    err_s = err_b.decode("utf-8", errors="replace").strip()

    text = out_b.decode("utf-8", errors="replace")
    list_diag = err_s + "\n" + text

    raw_xml_path_lines: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("Path = ") and ".xml" in line.casefold():
            raw_xml_path_lines.append(line)

    exec_failed = completed.returncode != 0 or not out_b
    quiet_rar = (
        archive.suffix.lower() == ".rar"
        and exec_failed
        and _seven_zip_stderr_unopenable_archive(list_diag)
    )
    if exec_failed:
        if not quiet_rar:
            bits: list[str] = []
            if completed.returncode != 0:
                bits.append(f"returncode={completed.returncode}")
            if not out_b:
                bits.append("empty stdout")
            if err_s:
                bits.append(err_s)
            body = "; ".join(bits) if bits else "unknown failure"
            _hard_log(f"FOMOD_7Z_EXEC_FAILED: stderr=({body})")

    if not quiet_rar:
        _hard_log(f"FOMOD_RAW_XML_PATHS: {raw_xml_path_lines!r}")

    paths: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Path = "):
            paths.append(s[7:].strip())
    return paths, int(completed.returncode), cmd, list_diag


def _path_has_fomod_segment(parts: list[str]) -> bool:
    """True if any non-leaf segment is ``fomod`` (case-insensitive)."""
    return any(p.casefold() == _FOMOD_DIR.casefold() for p in parts[:-1])


def _fomod_xml_candidates_from_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """ModuleConfig / info paths anywhere under a ``fomod`` directory (any depth, case-insensitive)."""
    module: list[str] = []
    info: list[str] = []
    for raw in paths:
        norm = raw.replace("\\", "/").strip()
        if not norm or norm.endswith("/"):
            continue
        parts = norm.split("/")
        if len(parts) < 2:
            continue
        if not _path_has_fomod_segment(parts):
            continue
        leaf = parts[-1].casefold()
        if leaf == _MODULE_XML.casefold():
            module.append(raw)
        elif leaf == _INFO_XML.casefold():
            info.append(raw)
    return module, info


def _score_fomod_xml_path(raw: str) -> tuple[int, int]:
    """Higher first: prefers ``fomod`` segment, installer-ish dirs, shallower paths."""
    norm = raw.replace("\\", "/").strip().lower()
    score = 0
    if "/fomod/" in f"/{norm}/" or norm.startswith("fomod/"):
        score += 200
    if "fomod" in norm:
        score += 50
    if "installer" in norm or "wizard" in norm:
        score += 40
    depth = norm.count("/")
    return (score, -depth)


def _loose_xml_candidates_from_paths(
    paths: list[str],
    *,
    leaf: str,
) -> list[str]:
    """Any archive-internal path whose filename matches ``leaf`` (case-insensitive)."""
    want = leaf.casefold()
    hits: list[str] = []
    for raw in paths:
        norm = raw.replace("\\", "/").strip()
        if not norm or norm.endswith("/"):
            continue
        name = norm.split("/")[-1].casefold()
        if name == want:
            hits.append(raw)
    hits.sort(key=lambda r: _score_fomod_xml_path(r), reverse=True)
    return hits


def _seven_zip_list_paths_plain(seven_zip: Path, archive: Path) -> tuple[list[str], int, list[str]]:
    """
    Fallback: ``7z l`` without ``-slt`` (some archives / locales omit ``Path =`` lines).
    Greps stdout for plausible internal paths ending in ``.xml``.
    """
    cmd = [str(seven_zip), "l", str(archive)]
    try:
        completed = subprocess.run(
            cmd,
            timeout=120,
            check=False,
            **_subprocess_run_kw(),
        )
    except subprocess.TimeoutExpired as exc:
        err_b = exc.stderr
        if isinstance(err_b, bytes) and err_b.strip():
            err_s = err_b.decode("utf-8", errors="replace").strip()
        else:
            err_s = "timeout after 120s"
        _hard_log(f"FOMOD_7Z_LIST_PLAIN_EXEC_FAILED: command={cmd!r} stderr=({err_s})")
        return [], -1, cmd
    err_plain = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        out_plain_preview = (completed.stdout or b"").decode("utf-8", errors="replace")
        quiet_rar = archive.suffix.lower() == ".rar" and _seven_zip_stderr_unopenable_archive(
            err_plain + "\n" + out_plain_preview
        )
        if not quiet_rar:
            _hard_log(
                f"FOMOD_7Z_LIST_PLAIN_EXEC_FAILED: command={cmd!r} "
                f"returncode={completed.returncode} stderr=({err_plain or 'empty'})"
            )
    text = (completed.stdout or b"").decode("utf-8", errors="replace")
    paths: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        lc = line.casefold()
        if "moduleconfig.xml" not in lc and "info.xml" not in lc:
            continue
        # Typical ``7z l`` data row: date time attr size compressed Name
        # Name may contain spaces; capture everything after the compressed-size column.
        m_row = re.match(
            r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\d+\s+\d+\s+(.+)$",
            line,
        )
        if m_row:
            name = m_row.group(1).strip()
            nlc = name.casefold()
            if nlc.endswith("moduleconfig.xml") or nlc.endswith("info.xml"):
                paths.append(name)
                continue
        # Paths without spaces in the name (older / unusual list output).
        m = re.search(
            r"([\w./\\\-]+\.(?:[Mm]odule[Cc]onfig|[Ii]nfo)\.xml)\s*$",
            line,
        )
        if m:
            paths.append(m.group(1).strip())
            continue
        if lc.endswith("moduleconfig.xml") or lc.endswith("info.xml"):
            if not re.match(r"^\d{4}-\d{2}-\d{2}", line):
                paths.append(line)
    dedup: list[str] = []
    seen: set[str] = set()
    for p in paths:
        k = p.replace("\\", "/").casefold()
        if k not in seen:
            seen.add(k)
            dedup.append(p)
    return dedup, int(completed.returncode), cmd


def _extract_one_via_7z(seven_zip: Path, archive: Path, inner_path: str) -> tuple[bytes, list[str]]:
    cmd = [str(seven_zip), "e", str(archive), inner_path, "-so"]
    try:
        completed = subprocess.run(
            cmd,
            timeout=120,
            check=False,
            **_subprocess_run_kw(),
        )
    except subprocess.TimeoutExpired as exc:
        err_b = exc.stderr
        if isinstance(err_b, bytes) and err_b.strip():
            err_s = err_b.decode("utf-8", errors="replace").strip()
        else:
            err_s = "timeout after 120s"
        _hard_log(f"FOMOD_7Z_EXEC_FAILED: command={cmd!r} stderr=({err_s})")
        return b"", cmd
    stderr_preview = (completed.stderr or b"")[:512]
    try:
        err_s = stderr_preview.decode("utf-8", errors="replace")
    except Exception:
        err_s = repr(stderr_preview)
    out = completed.stdout or b""
    quiet_rar = (
        archive.suffix.lower() == ".rar"
        and completed.returncode != 0
        and _seven_zip_stderr_unopenable_archive(
            (completed.stderr or b"").decode("utf-8", errors="replace")
        )
    )
    if not quiet_rar:
        _hard_log(
            f"FOMOD_7Z command={cmd!r} returncode={completed.returncode} "
            f"stdout_bytes={len(out)} stderr_preview={err_s!r}"
        )
    return out, cmd


def decode_fomod_xml(raw_bytes: bytes) -> str:
    """
    7-Zip ``-so`` stdout 등에서 나온 FOMOD XML 바이트를 문자열로 만든다.
    BOM(UTF-16 LE/BE, UTF-8) 우선, 그다음 UTF-8 엄격 디코드, 실패 시 UTF-16 폴백.
    """
    if raw_bytes.startswith(codecs.BOM_UTF16_LE):
        return raw_bytes.decode("utf-16-le")
    if raw_bytes.startswith(codecs.BOM_UTF16_BE):
        return raw_bytes.decode("utf-16-be")
    if raw_bytes.startswith(codecs.BOM_UTF8):
        return raw_bytes.decode("utf-8-sig")
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("utf-16", errors="replace")


def _decode_7z_stdout(data: bytes) -> str:
    return decode_fomod_xml(data)


def _find_fomod_xml_member(member_names: list[str]) -> str | None:
    """Prefer ``ModuleConfig.xml`` under any ``fomod`` segment, then any path with that leaf name."""
    strict: list[str] = []
    for raw in member_names:
        norm = raw.replace("\\", "/").strip()
        if not norm or norm.endswith("/"):
            continue
        parts = norm.split("/")
        if len(parts) < 2:
            continue
        if parts[-1].casefold() != _MODULE_XML.casefold():
            continue
        if not _path_has_fomod_segment(parts):
            continue
        strict.append(raw)
    if strict:
        strict.sort(key=lambda r: _score_fomod_xml_path(r), reverse=True)
        return strict[0]
    loose = _loose_xml_candidates_from_paths(member_names, leaf=_MODULE_XML)
    return loose[0] if loose else None


def _find_fomod_info_member(member_names: list[str]) -> str | None:
    """Same as module lookup but for ``info.xml`` (ZIP fallback when no ModuleConfig)."""
    strict: list[str] = []
    for raw in member_names:
        norm = raw.replace("\\", "/").strip()
        if not norm or norm.endswith("/"):
            continue
        parts = norm.split("/")
        if len(parts) < 2:
            continue
        if parts[-1].casefold() != _INFO_XML.casefold():
            continue
        if not _path_has_fomod_segment(parts):
            continue
        strict.append(raw)
    if strict:
        strict.sort(key=lambda r: _score_fomod_xml_path(r), reverse=True)
        return strict[0]
    loose = _loose_xml_candidates_from_paths(member_names, leaf=_INFO_XML)
    return loose[0] if loose else None


def _decode_xml_bytes(data: bytes) -> str:
    last_exc: UnicodeDecodeError | None = None
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError as exc:
            last_exc = exc
            continue
    _hard_log(
        "FOMOD_ZIP_DECODE fallback=utf-8-replace "
        f"last_error={type(last_exc).__name__ if last_exc else 'unknown'}: {last_exc!s}"
    )
    return data.decode("utf-8", errors="replace")


def _extract_from_zip(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        member = _find_fomod_xml_member(names)
        if member is None:
            member = _find_fomod_info_member(names)
        if member is None:
            return NO_FOMOD_MESSAGE
        with zf.open(member, "r") as fp:
            raw = fp.read()
    return _decode_xml_bytes(raw)


def _extract_from_7z_or_rar(path: Path, organizer: mobase.IOrganizer | None = None) -> str:
    seven = _find_seven_zip_executable(organizer)
    if seven is None:
        return NO_FOMOD_MESSAGE

    listed, list_rc, list_cmd, list_diag = _seven_zip_list_paths_slt(seven, path)
    if (
        path.suffix.lower() == ".rar"
        and not listed
        and list_rc != 0
        and _seven_zip_stderr_unopenable_archive(list_diag)
    ):
        return NO_FOMOD_MESSAGE

    def _paths_contain_leaf(paths: list[str], leaf: str) -> bool:
        want = leaf.casefold()
        for p in paths:
            if p.replace("\\", "/").split("/")[-1].casefold() == want:
                return True
        return False

    if not listed or not _paths_contain_leaf(listed, _MODULE_XML):
        plain, plain_rc, plain_cmd = _seven_zip_list_paths_plain(seven, path)
        if plain:
            seen_k = {x.replace("\\", "/").casefold() for x in listed}
            added = 0
            for p in plain:
                k = p.replace("\\", "/").casefold()
                if k not in seen_k:
                    seen_k.add(k)
                    listed.append(p)
                    added += 1
            _hard_log(
                f"FOMOD_7Z_LIST_PLAIN merge command={plain_cmd!r} rc={plain_rc} "
                f"added={added} total_paths={len(listed)}"
            )

    mod_hits, info_hits = _fomod_xml_candidates_from_paths(listed)
    mod_loose = _loose_xml_candidates_from_paths(listed, leaf=_MODULE_XML)
    info_loose = _loose_xml_candidates_from_paths(listed, leaf=_INFO_XML)
    _hard_log(
        f"FOMOD_7Z_LIST command={list_cmd!r} returncode={list_rc} path_count={len(listed)} "
        f"moduleconfig_in_fomod={mod_hits!r} info_in_fomod={info_hits!r} "
        f"moduleconfig_loose={mod_loose[:6]!r} info_loose={info_loose[:4]!r}"
    )

    try_order: list[str] = []
    seen_try: set[str] = set()

    def _add_try(p: str) -> None:
        k = p.replace("\\", "/").casefold()
        if k not in seen_try:
            seen_try.add(k)
            try_order.append(p)

    for p in mod_hits:
        _add_try(p)
    for p in mod_loose:
        _add_try(p)
    for p in info_hits:
        _add_try(p)
    for p in info_loose:
        _add_try(p)

    last_cmd: list[str] | None = None
    for inner in try_order:
        raw, cmd = _extract_one_via_7z(seven, path, inner)
        last_cmd = cmd
        if raw and raw.strip():
            text = _decode_7z_stdout(raw)
            if text.strip():
                _hard_log(
                    f"FOMOD_7Z success inner_path={inner!r} (from list) "
                    f"xml_utf8_byte_length={len(raw)}"
                )
                return text

    for inner in _SEVEN_ZIP_INNER_PATHS:
        raw, cmd = _extract_one_via_7z(seven, path, inner)
        last_cmd = cmd
        if raw and raw.strip():
            text = _decode_7z_stdout(raw)
            if text.strip():
                _hard_log(f"FOMOD_7Z success inner_path={inner!r} (fallback) xml_utf8_byte_length={len(raw)}")
                return text

    _hard_log(f"FOMOD_7Z no usable stdout from archive; last_command={last_cmd!r}")
    return NO_FOMOD_MESSAGE


def extract_fomod_xml(
    file_path: str,
    *,
    organizer: mobase.IOrganizer | None = None,
) -> str:
    """
    Return FOMOD XML text (``ModuleConfig.xml`` or ``info.xml``), or :data:`NO_FOMOD_MESSAGE`
    when absent or when 7-Zip cannot extract. Does not write files to disk.

    Pass ``organizer`` so ``7z.exe`` is resolved via :meth:`mobase.IOrganizer.basePath` first.
    """
    p = Path(file_path)
    try:
        p = p.resolve()
    except Exception:
        p = Path(file_path)
    if not p.is_file():
        return f"파일을 찾을 수 없습니다: {file_path}"

    suf = p.suffix.lower()
    try:
        if suf == ".zip":
            return _extract_from_zip(p)
        if suf in (".7z", ".rar"):
            return _extract_from_7z_or_rar(p, organizer)
    except zipfile.BadZipFile as exc:
        _hard_log(f"FOMOD_ZIP BadZipFile: {exc}")
        return f"ZIP 파일이 손상되었거나 ZIP이 아닙니다: {exc}"
    except Exception as exc:
        exc_t = str(exc)
        if suf == ".rar" and _seven_zip_stderr_unopenable_archive(exc_t):
            return NO_FOMOD_MESSAGE
        _hard_log(f"FOMOD_EXTRACT {type(exc).__name__}: {exc}")
        return f"아카이브를 읽는 중 오류: {exc}"

    return f"지원하지 않는 아카이브 형식입니다: {suf}"
