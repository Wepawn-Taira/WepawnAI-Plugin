"""
Nexus Mods API helpers tied to MO2: profile ``ModOrganizer.ini`` API key, download ``.meta`` mod IDs.

Dependency names are suitable for cross-checking against an active-mod list. No usernames are embedded.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import quote_plus

import mobase

from .hard_log import _hard_log
from ..nexus.dependencies import (
    NexusAPIError,
    NexusDependencyLink,
    _merge_unique_links,
    fetch_mod_file_dependencies,
)


def _diag(msg: str) -> None:
    line = f"[WepawnAI DIAG] {msg}"
    print(line, flush=True)
    _hard_log(line)


# (section, option) pairs seen in MO2 / Qt ``QSettings`` INI output.
_NEXUS_INI_KEY_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Settings", "nexus_api_key"),
    ("General", "nexus_api_key"),
    ("Nexus", "apikey"),
    ("Nexus", "apiKey"),
    ("Settings", "apikey"),
)


def _organizer_path_str(organizer: mobase.IOrganizer, *candidates: str) -> str | None:
    for name in candidates:
        fn = getattr(organizer, name, None)
        if fn is None or not callable(fn):
            continue
        try:
            raw = fn()
        except Exception:
            continue
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            return s
    prof = getattr(organizer, "profile", None)
    if prof is not None and callable(prof):
        try:
            pobj = prof()
        except Exception:
            pobj = None
        if pobj is not None:
            for meth in ("absolutePath", "path"):
                ap = getattr(pobj, meth, None)
                if ap is not None and callable(ap):
                    try:
                        raw = ap()
                    except Exception:
                        continue
                    if raw is not None:
                        s = str(raw).strip()
                        if s:
                            return s
    return None


def _modorganizer_ini_paths(organizer: mobase.IOrganizer) -> list[Path]:
    ordered: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            r = p.resolve()
        except Exception:
            r = p
        k = str(r)
        if k not in seen:
            seen.add(k)
            ordered.append(r)

    prof = _organizer_path_str(organizer, "profilePath")
    if prof:
        _add(Path(prof) / "ModOrganizer.ini")
    base = _organizer_path_str(organizer, "basePath")
    if base:
        _add(Path(base) / "ModOrganizer.ini")
    return ordered


def _read_ini_text(path: Path) -> str | None:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except OSError:
            return None
        except UnicodeDecodeError:
            continue
    return None


def _parse_nexus_api_key_from_ini_text(text: str) -> str | None:
    if not text or not text.strip():
        return None
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read_string(text)
    except configparser.Error:
        cfg = None
    if cfg is not None:
        for sec, opt in _NEXUS_INI_KEY_CANDIDATES:
            if not cfg.has_section(sec):
                continue
            if not cfg.has_option(sec, opt):
                continue
            raw = str(cfg.get(sec, opt, fallback="")).strip()
            if raw and not raw.startswith("@Variant(") and not raw.startswith("@ByteArray("):
                return raw
    m = re.search(
        r"(?im)^\s*nexus_api_key\s*=\s*(\S.*?)\s*$",
        text,
    )
    if m:
        val = m.group(1).strip().strip('"').strip("'")
        if val and not val.startswith("@Variant("):
            return val
    m2 = re.search(r"(?im)^\s*apikey\s*=\s*(\S.*?)\s*$", text)
    if m2:
        val = m2.group(1).strip().strip('"').strip("'")
        if val and not val.startswith("@Variant("):
            return val
    return None


def read_nexus_api_key_from_mo2(
    organizer: mobase.IOrganizer | None,
    *,
    fallback: str = "",
) -> str:
    """
    Read Nexus REST ``apikey`` from MO2 ``ModOrganizer.ini`` (profile, then instance).

    MO2 may store the key only in Windows Credential Manager / OAuth; in that case ``fallback``
    (plugin ``nexus_api_key`` from :meth:`mobase.IOrganizer.pluginSetting`) supplies the key.

    Returns a freshly resolved stripped string each call — do not stash this as a process-wide
    singleton if the user may update INI or plugin settings during the session.
    """
    fb = (fallback or "").strip()

    def _log_key_resolution(source: str, resolved: str) -> None:
        _diag(
            f"[WepawnAI DIAG] NEXUS_API_KEY source={source}, length={len(resolved)}"
        )

    if organizer is None:
        if fb:
            _log_key_resolution("fallback", fb)
            return fb
        _log_key_resolution("empty", "")
        return ""

    for ini in _modorganizer_ini_paths(organizer):
        if not ini.is_file():
            continue
        text = _read_ini_text(ini)
        if not text:
            continue
        key = _parse_nexus_api_key_from_ini_text(text)
        if key:
            k = key.strip()
            _log_key_resolution("ini", k)
            return k

    if fb:
        _log_key_resolution("fallback", fb)
        return fb
    _log_key_resolution("empty", "")
    return ""


def parse_download_meta_labels(archive_path: str | Path) -> tuple[str, str]:
    """
    Read ``modName`` and ``version`` from MO2 ``<archive>.meta`` (Qt INI).

    Returns ``(display_name, version_string)``; either may be empty if absent.
    """
    p = Path(archive_path)
    meta = Path(f"{p}.meta")
    if not meta.is_file():
        return "", ""
    text = _read_ini_text(meta)
    if not text:
        return "", ""

    def _grab(pattern: str) -> str:
        m = re.search(pattern, text, flags=re.I | re.M)
        if not m:
            return ""
        return str(m.group(1)).strip().strip('"').strip("'")

    mod_name = _grab(r"^\s*modName\s*=\s*(.+?)\s*$")
    if not mod_name:
        mod_name = _grab(r"^\s*name\s*=\s*(.+?)\s*$")
    version = _grab(r"^\s*version\s*=\s*(.+?)\s*$")
    return mod_name, version


def nexus_req_names_contain_skse_family(names: Sequence[str]) -> bool:
    """True if any dependency label looks like SKSE / Script Extender (API sanity check)."""
    for n in names:
        cf = str(n).casefold()
        if "skse" in cf or "script extender" in cf:
            return True
    return False


def _req_matches_any_active_mod(req: str, active_display_names: Sequence[str]) -> bool:
    r = str(req).casefold().strip()
    if len(r) < 2:
        return True
    for loc in active_display_names:
        l = str(loc).casefold().strip()
        if not l:
            continue
        if r in l or l in r:
            return True
    return False


def nexus_url_for_requirement_label(
    label: str,
    links: Sequence[NexusDependencyLink],
    *,
    game_domain: str,
) -> str:
    """
    Resolve a Nexus mod page URL for a dependency **display name** from API ``links``.

    Falls back to game-domain Nexus search when no API row matches.
    """
    dom = (game_domain or "").strip().strip("/").lower() or "skyrimspecialedition"
    req = str(label).strip()
    if not req:
        return f"https://www.nexusmods.com/{dom}/mods/"
    req_cf = req.casefold()
    for link in links:
        ln = str(link.name).strip().casefold()
        if ln == req_cf:
            return link.url
    for link in links:
        ln = str(link.name).strip().casefold()
        if not ln:
            continue
        if req_cf in ln or ln in req_cf:
            return link.url
    q = quote_plus(req, safe="")
    return f"https://www.nexusmods.com/{dom}/search/?gsearch={q}"


def find_unsatisfied_nexus_prereqs(
    nexus_req_names: Sequence[str],
    active_mod_display_names: Sequence[str],
) -> list[str]:
    """
    Compare Nexus-declared names to **all** active mod display names (profile order irrelevant).

    Uses case-insensitive substring match (either direction). Unmatched Nexus entries are missing.
    """
    reqs = [str(x).strip() for x in nexus_req_names if str(x).strip()]
    if not reqs:
        return []
    locals_ = [str(x).strip() for x in active_mod_display_names if str(x).strip()]
    return [req for req in reqs if not _req_matches_any_active_mod(req, locals_)]


# UI / LLM: do not treat empty ``names`` as “no requirements” when ``verified`` is False.
NEXUS_PREREQ_UNVERIFIED_BANNER = (
    "[경고: 넥서스 서버 통신 실패로 사전 모드를 확인할 수 없습니다]"
)
NEXUS_PREREQ_UNVERIFIED_DETAIL = (
    "의존성 검증 불가(네트워크 오류, API 키 없음, 또는 .meta에 modID 없음). "
    "빈 목록을 '사전 요구 없음'으로 해석하지 마라."
)


@dataclass(frozen=True)
class NexusDependencyFetchResult:
    """
    Nexus 사전 요구 이름 목록 + 검증 가능 여부.

    ``verified=False``인데 ``names``가 비어 있으면 **정상적인 '요구 없음'이 아니라**
    메타/API/네트워크로 **확인 불가** 상태다. 빈 배열로 위장하지 않는다.

    ``links``는 API가 돌려준 의존 모드별 Nexus URL(이름 매칭·채팅 링크용).

    ``reason_code`` 예: ``ok``, ``no_api_key``, ``bad_mod_id``, ``nexus_api_error``,
    ``nexus_http_401`` … ``nexus_http_429``, ``nexus_ssl_error``,
    UI/메타의 ``no_mod_id_meta`` 등.
    """

    names: tuple[str, ...]
    links: tuple[NexusDependencyLink, ...]
    verified: bool
    reason_code: str


def nexus_unverified_hint_korean(reason_code: str) -> str:
    """Short Korean hint for the chat UI when ``verified=False`` (empty string if unknown)."""
    rc = (reason_code or "").strip()
    hints: dict[str, str] = {
        "no_api_key": (
            "[진단] Nexus API 키가 비어 있습니다. Nexus 웹 → 계정 → API에서 발급한 **개인 API 키**를 "
            "WepawnAI 플러그인 설정의 «Nexus Mods API key»에 붙여 넣으세요. "
            "MO2에 Nexus 사이트만 로그인해 있어도 ModOrganizer.ini에는 키가 없을 수 있습니다."
        ),
        "nexus_http_401": (
            "[진단] API 키가 거부되었습니다(HTTP 401). 키를 다시 복사·저장했는지, 재발급이 필요한지 확인하세요."
        ),
        "nexus_http_403": (
            "[진단] 접근이 거부되었습니다(HTTP 403). 정책·계정 제한일 수 있습니다. 플러그인을 최신으로 유지하세요."
        ),
        "nexus_http_404": (
            "[진단] 해당 게임/모드 조합을 API가 찾지 못했습니다(HTTP 404). "
            "프로필 게임(SSE/AE)과 Nexus mod ID가 맞는지 확인하세요."
        ),
        "nexus_http_429": "[진단] Nexus API 요청 한도입니다(HTTP 429). 잠시 후 다시 시도하세요.",
        "nexus_ssl_error": (
            "[진단] SSL(HTTPS) 연결에 실패했습니다. 백신·프록시·기업망의 HTTPS 검사를 의심해 보세요."
        ),
        "no_mod_id_meta": (
            "[진단] Nexus mod ID를 찾지 못했습니다. 모드 목록에서 해당 모드를 선택하거나, "
            "Nexus에서 받은 원본 아카이브와 같은 이름의 `.meta`가 다운로드 폴더에 있는지 확인하세요."
        ),
    }
    if rc in hints:
        return hints[rc]
    if rc == "nexus_api_error" or rc.startswith("nexus_http_"):
        return (
            "[진단] Nexus HTTP/네트워크 오류입니다. MO2 로그의 "
            "[WepawnAI DIAG] NEXUS_FETCH 관련 줄을 확인하세요."
        )
    return ""


def _classify_nexus_api_failure(exc: NexusAPIError) -> str:
    code = getattr(exc, "status_code", None)
    if code == 401:
        return "nexus_http_401"
    if code == 403:
        return "nexus_http_403"
    if code == 404:
        return "nexus_http_404"
    if code == 429:
        return "nexus_http_429"
    low = str(exc).lower()
    if "ssl error" in low:
        return "nexus_ssl_error"
    return "nexus_api_error"


def log_nexus_req_pre_inject_for_llm(
    names: Sequence[str],
    *,
    verified: bool,
    reason_code: str = "ok",
) -> None:
    """DIAG immediately before prompt assembly / LLM."""
    sk = nexus_req_names_contain_skse_family(names)
    nl = list(names)
    _diag(
        f"INSTALL_GUIDE_NEXUS_REQ_PRE_INJECT verified={verified} reason_code={reason_code!r} "
        f"requirement_names={nl!r} count={len(nl)} skse_family_hit={sk}"
    )


def fetch_nexus_mod_dependency_names(
    mod_id: int,
    api_key: str,
    *,
    file_id: int = 0,
    game_domain: str = "skyrimspecialedition",
    application: str = "WepawnAI",
    timeout: float = 30.0,
) -> NexusDependencyFetchResult:
    """
    Nexus API에서 사전 요구 모드 **이름**을 가져온다.

    ``file_id``가 0이면 모드 JSON만으로 부족할 때 **파일 목록 → 대표 파일 JSON**으로 보강한다.

    API 응답과 **별도로** 공개 모드 페이지를 한 번 스크랩해 ``__NEXT_DATA__``·Requirements 인근 HTML의
    Nexus 모드 링크를 **항상 병합**한다. API만으로는 비어 있거나 빠진 항목이 많아 설치 전 가이드가 틀리는
    경우가 있어, 페이지의 Requirements와 합집합으로 맞춘다.

    네트워크 오류·키 없음·mod_id 무효 시 ``verified=False``이며 ``names``는 비어 있다.
    API 성공 후 페이지까지 봤는데 링크가 없으면 ``verified=True``, ``names=()`` 이다.
    """
    key = (api_key or "").strip()
    has_key = bool(key)
    if not has_key:
        _diag("NEXUS_FETCH verified=False reason=no_api_key")
        return NexusDependencyFetchResult((), (), False, "no_api_key")
    if mod_id <= 0:
        _diag("NEXUS_FETCH verified=False reason=bad_mod_id")
        return NexusDependencyFetchResult((), (), False, "bad_mod_id")
    try:
        links = fetch_mod_file_dependencies(
            key,
            game_domain,
            mod_id,
            int(file_id),
            application=application,
            timeout=timeout,
        )
    except NexusAPIError as exc:
        rcode = _classify_nexus_api_failure(exc)
        _diag(
            f"NEXUS_FETCH verified=False reason={rcode} status={getattr(exc, 'status_code', None)!r} "
            f"detail={exc!r}"
        )
        return NexusDependencyFetchResult((), (), False, rcode)
    tlinks = tuple(links)
    api_count = len(tlinks)
    scraped: list[NexusDependencyLink] = []
    try:
        from ..ai.nexus_scraper import scrape_nexus_mod_requirement_links

        scrape_timeout = min(10.0, max(5.0, float(timeout) * 0.25))
        scraped = scrape_nexus_mod_requirement_links(
            game_domain,
            mod_id,
            timeout=scrape_timeout,
            max_links=32,
        )
    except Exception as exc:
        _diag(f"NEXUS_DEPS_SCRAPE exception {type(exc).__name__}: {exc!r}")
        scraped = []
    if scraped:
        merged = _merge_unique_links(list(tlinks), scraped)
        tlinks = tuple(merged)
        _diag(
            f"NEXUS_FETCH merged_public_page api_links={api_count} scraped={len(scraped)} "
            f"merged_total={len(tlinks)}"
        )
    names = tuple(link.name for link in tlinks)
    _diag(f"NEXUS_FETCH verified=True reason=ok requirement_names={list(names)!r}")
    return NexusDependencyFetchResult(names, tlinks, True, "ok")


_MAX_NEXUS_SITE_MOD_ID = 2_000_000_000

# MO2 / Qt INI sidecar lines seen in the wild (case and quoting vary).
_META_MOD_ID_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*modID\s*=\s*[\"']?(\d+)[\"']?\s*$"),
    re.compile(r"(?im)^\s*modId\s*=\s*[\"']?(\d+)[\"']?\s*$"),
    re.compile(r"(?im)^\s*mod_id\s*=\s*[\"']?(\d+)[\"']?\s*$"),
    re.compile(r"(?im)^\s*modid\s*=\s*[\"']?(\d+)[\"']?\s*$"),
)

_META_FILE_ID_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*fileID\s*=\s*[\"']?(\d+)[\"']?\s*$"),
    re.compile(r"(?im)^\s*fileId\s*=\s*[\"']?(\d+)[\"']?\s*$"),
    re.compile(r"(?im)^\s*file_id\s*=\s*[\"']?(\d+)[\"']?\s*$"),
    re.compile(r"(?im)^\s*fileid\s*=\s*[\"']?(\d+)[\"']?\s*$"),
)


def mod_id_from_adjacent_meta(archive_path: str | Path) -> int | None:
    """
    Parse Nexus mod id from MO2 download sidecar ``<archive>.meta`` (Qt INI format).

    Accepts ``modID`` / ``modId`` / ``mod_id`` / ``modid`` and optional quotes around the number.
    """
    p = Path(archive_path)
    meta = Path(f"{p}.meta")
    if not meta.is_file():
        _diag(f"NEXUS_META no sidecar at {str(meta)!r}")
        return None
    text = _read_ini_text(meta)
    if not text:
        return None
    for rx in _META_MOD_ID_LINE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            mid = int(m.group(1), 10)
        except ValueError:
            continue
        if 0 < mid <= _MAX_NEXUS_SITE_MOD_ID:
            _diag(f"NEXUS_META mod_id={mid} from {str(meta)!r} (pattern={rx.pattern!r})")
            return mid
    _diag(f"NEXUS_META mod id not found in {str(meta)!r}")
    return None


def file_id_from_adjacent_meta(archive_path: str | Path) -> int | None:
    """Parse Nexus **file** id from ``<archive>.meta`` if present (MO2 download sidecar)."""
    p = Path(archive_path)
    meta = Path(f"{p}.meta")
    if not meta.is_file():
        return None
    text = _read_ini_text(meta)
    if not text:
        return None
    for rx in _META_FILE_ID_LINE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            fid = int(m.group(1), 10)
        except ValueError:
            continue
        # File IDs can exceed mod IDs; only reject non-positive / absurd values.
        if 0 < fid < 10**15:
            _diag(f"NEXUS_META file_id={fid} from {str(meta)!r} (pattern={rx.pattern!r})")
            return fid
    return None


def get_mod_dependencies(
    mod_id: int,
    api_key: str,
    *,
    file_id: int = 0,
    game_domain: str = "skyrimspecialedition",
    application: str = "WepawnAI",
    timeout: float = 30.0,
) -> list[str]:
    """
    Return Nexus-declared requirement mod names only (no verification flag).

    Prefer :func:`fetch_nexus_mod_dependency_names` when silent failure must be avoided.
    """
    r = fetch_nexus_mod_dependency_names(
        mod_id,
        api_key,
        file_id=file_id,
        game_domain=game_domain,
        application=application,
        timeout=timeout,
    )
    return list(r.names)
