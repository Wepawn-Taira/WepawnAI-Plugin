"""
Fetch mod file dependencies from the Nexus Mods REST API (v1).

Requires a personal API key (``apikey`` header) and a descriptive ``User-Agent``.

Uses only the Python standard library (``urllib.request``, ``json``).

See: https://help.nexusmods.com/article/114-api-acceptable-use-policy
"""

from __future__ import annotations

import json
import platform
import ssl
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, MutableMapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..utils.core_dependencies import get_core_requirements_bundle
from ..utils.hard_log import _hard_log

NEXUS_REST_V1 = "https://api.nexusmods.com/v1"

_MAX_NEXUS_REQ_HTML_FALLBACK = 50_000


def slice_nexus_requirements_html_chunk(page_html: str) -> str:
    """
    ``Nexus requirements`` 이후 첫 ``</table>``(및 선택적 Off-site 표)만 잘라 낸다.

    ``Mods using this mod`` 등 파생 구간은 첫 번째 요구사항 테이블 묶음 밖으로 두지 않는다.
    """
    if not (page_html or "").strip():
        return ""
    raw = page_html
    low = raw.lower()
    start = low.find("nexus requirements")
    if start < 0:
        return ""
    first_end = low.find("</table>", start)
    if first_end < 0:
        _hard_log("[SCRAPE DBG] Nexus requirements: 첫 </table> 없음 — 빈 청크")
        return ""
    probe_hi = min(first_end + 500, len(low))
    probe_chunk = low[first_end:probe_hi]
    has_offsite = "off-site requirements" in probe_chunk
    if has_offsite:
        second_end = low.find("</table>", first_end + 8)
        if second_end < 0:
            end = min(len(raw), start + _MAX_NEXUS_REQ_HTML_FALLBACK)
            _hard_log(
                "[SCRAPE DBG] Off-site 헤더 인접이나 두 번째 </table> 없음 — "
                f"폴백 절단 (end={end})"
            )
        else:
            end = second_end + 8
    else:
        end = first_end + 8
    chunk = raw[start:end]
    tier = 2 if has_offsite else 1
    _hard_log(
        f"[SCRAPE DBG] 절단 {tier}단 / Off-site 테이블 탐지됨: {has_offsite} / "
        f"최종 청크 길이: {len(chunk)}"
    )
    return chunk


class NexusAPIError(RuntimeError):
    """Raised when Nexus returns a non-success response or malformed payload."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class NexusDependencyLink:
    """One dependency edge with a stable Nexus mod URL for the UI."""

    name: str
    mod_id: int
    url: str
    note: str | None = None


def nexus_mod_url(game_domain: str, mod_id: int) -> str:
    """Build a canonical Nexus mod page URL (HTTPS, no trailing slash)."""
    domain = game_domain.strip().lower()
    return f"https://www.nexusmods.com/{domain}/mods/{int(mod_id)}"


def nexus_links_from_requirements_bundle(
    bundle: Mapping[str, Any],
    game_domain: str,
) -> tuple[list[NexusDependencyLink], str | None, bool]:
    """Build :class:`NexusDependencyLink` list from a cache/core requirements ``data`` bundle."""
    dom_lc = (game_domain or "").strip().lower() or "skyrimspecialedition"
    is_ext = bool(bundle.get("is_external"))
    title_clean: str | None = None
    tc = bundle.get("title_clean")
    if isinstance(tc, str) and tc.strip():
        title_clean = tc.strip()[:240]
    links: list[NexusDependencyLink] = []
    for r in bundle.get("rows") or []:
        if not isinstance(r, dict):
            continue
        try:
            mid_r = int(r.get("mod_id") or 0)
        except (TypeError, ValueError):
            continue
        if mid_r <= 0:
            continue
        nm = str(r.get("name") or f"Mod {mid_r}").strip()
        note = str(r.get("note") or "").strip()
        links.append(
            NexusDependencyLink(
                name=(nm[:200] if nm else f"Mod {mid_r}")[:200],
                mod_id=mid_r,
                url=nexus_mod_url(dom_lc, mid_r),
                note=note or None,
            )
        )
    return links, title_clean, is_ext


def _coerce_mod_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_dependency_item(
    game_domain: str,
    raw: Mapping[str, Any],
) -> NexusDependencyLink | None:
    mid = _coerce_mod_id(raw.get("mod_id") if raw.get("mod_id") is not None else raw.get("modId"))
    if mid is None:
        return None
    name = str(
        raw.get("name")
        or raw.get("mod_name")
        or raw.get("modName")
        or raw.get("title")
        or raw.get("display_name")
        or raw.get("displayName")
        or raw.get("label")
        or raw.get("dependency_name")
        or raw.get("dependencyName")
        or f"Mod {mid}"
    )
    note = raw.get("description") or raw.get("notes")
    note_str = str(note) if note is not None else None
    url = str(raw.get("url") or "").strip()
    if not url:
        url = nexus_mod_url(game_domain, mid)
    return NexusDependencyLink(
        name=name,
        mod_id=mid,
        url=url,
        note=note_str,
    )


def _requirement_nodes(requirements: Any) -> list[Mapping[str, Any]]:
    """Collect Nexus dependency nodes from a mod ``requirements`` object (snake or camel case)."""
    if not isinstance(requirements, Mapping):
        return []
    nexus_block = requirements.get("nexus_requirements")
    if nexus_block is None:
        nexus_block = requirements.get("nexusRequirements")
    if not isinstance(nexus_block, Mapping):
        return []
    nodes = nexus_block.get("nodes")
    if not isinstance(nodes, list):
        return []
    out: list[Mapping[str, Any]] = []
    for n in nodes:
        if isinstance(n, Mapping):
            out.append(n)
    return out


def _dependency_item_mappings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Extract dependency-like dict lists from a mod or file API JSON object."""
    candidates: tuple[Mapping[str, Any], ...]
    data = payload.get("data")
    if isinstance(data, Mapping):
        candidates = (payload, data)
    else:
        candidates = (payload,)

    for block in candidates:
        items: list[Mapping[str, Any]] = []
        req_block = block.get("requirements")
        items.extend(_requirement_nodes(req_block))
        if not items:
            for key in ("nexus_requirements", "nexusRequirements"):
                nb = block.get(key)
                if isinstance(nb, Mapping):
                    nodes = nb.get("nodes")
                    if isinstance(nodes, list):
                        items = [x for x in nodes if isinstance(x, Mapping)]
                        break
        if not items:
            alt = block.get("dependencies")
            if isinstance(alt, list):
                items = [x for x in alt if isinstance(x, Mapping)]
        if items:
            return items
    return []


def _links_from_items(domain: str, items: list[Mapping[str, Any]]) -> list[NexusDependencyLink]:
    links: list[NexusDependencyLink] = []
    seen: set[int] = set()
    for raw in items:
        link = _normalize_dependency_item(domain, raw)
        if link is not None and link.mod_id not in seen:
            seen.add(link.mod_id)
            links.append(link)
    return links


def _merge_unique_links(
    base: list[NexusDependencyLink],
    extra: list[NexusDependencyLink],
) -> list[NexusDependencyLink]:
    seen = {l.mod_id for l in base}
    out = list(base)
    for l in extra:
        if l.mod_id not in seen:
            seen.add(l.mod_id)
            out.append(l)
    return out


def _files_list_root(raw: Any) -> Mapping[str, Any] | None:
    if isinstance(raw, Mapping) and isinstance(raw.get("files"), list):
        return raw
    if isinstance(raw, list):
        return {"files": raw}
    return None


def _pick_primary_file_id(files_root: Mapping[str, Any]) -> int:
    files = files_root.get("files")
    if not isinstance(files, list) or not files:
        return 0

    def fid(f: Mapping[str, Any]) -> int:
        raw = f.get("file_id") or f.get("id")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    candidates: list[Mapping[str, Any]] = [
        f for f in files if isinstance(f, Mapping) and fid(f) > 0
    ]
    if not candidates:
        return 0

    primaries = [f for f in candidates if int(f.get("category_id") or 0) == 1]
    pool = primaries or candidates
    best_id = fid(max(pool, key=fid))
    return best_id if best_id > 0 else 0


def _nexus_user_agent_header(application: str) -> str:
    """
    Nexus requires a descriptive User-Agent (app + platform + runtime), not a bare name.
    Non-ASCII characters in plugin settings are stripped to keep the header valid.
    """
    raw = (application or "").strip() or "WepawnAI/0.1.0"
    ascii_app = raw.encode("ascii", errors="ignore").decode("ascii").strip() or "WepawnAI/0.1.0"
    try:
        plat = f"{platform.system()} {platform.release()}; {platform.machine()}"
        plat_a = plat.encode("ascii", errors="ignore").decode("ascii").strip() or "unknown"
    except Exception:
        plat_a = "unknown"
    py_ver = platform.python_version()
    return f"{ascii_app} ({plat_a}) Python/{py_ver}"


def _nexus_get_json(url: str, *, api_key: str, application: str, timeout: float) -> Any:
    key = (api_key or "").strip()
    if not key:
        raise NexusAPIError("Missing Nexus API key")

    headers = {
        "apikey": key,
        "Accept": "application/json",
        "User-Agent": _nexus_user_agent_header(application),
    }
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read()
            status_code = getattr(response, "status", None) or response.getcode()
    except HTTPError as exc:
        fragment = exc.read().decode("utf-8", errors="replace")[:2000]
        raise NexusAPIError(
            f"HTTP {exc.code} from Nexus",
            status_code=exc.code,
            body=fragment,
        ) from exc
    except URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        raise NexusAPIError(f"Network error: {reason}") from exc
    except ssl.SSLError as exc:
        raise NexusAPIError(f"SSL error: {exc}", status_code=None, body=None) from exc

    if status_code != 200:
        text = raw_body.decode("utf-8", errors="replace")[:2000]
        raise NexusAPIError(
            f"HTTP {status_code} from Nexus",
            status_code=status_code,
            body=text,
        )

    try:
        return json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise NexusAPIError("Nexus response was not valid JSON") from exc


@dataclass(frozen=True)
class NexusModRecord:
    """Subset of Nexus mod JSON for install-guide UI (no heavy fields)."""

    name: str
    updated_timestamp: int | None
    summary: str
    description_plain: str


def _mod_json_root(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        inner = payload.get("data")
        if isinstance(inner, Mapping):
            return inner
        return payload
    return None


def _strip_html_to_plain(text: str, max_len: int) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text or "")
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if max_len > 0 and len(t) > max_len:
        return t[: max_len - 1].rstrip() + "…"
    return t


def fetch_nexus_mod_record(
    api_key: str,
    game_domain: str,
    mod_id: int,
    *,
    application: str = "WepawnAI",
    timeout: float = 18.0,
) -> NexusModRecord | None:
    """
    Fetch mod ``.json`` once for UI metadata (name, summary, description, updated time).

    Returns ``None`` on network/auth errors or unexpected shape.
    """
    if int(mod_id) <= 0:
        return None
    domain = (game_domain or "").strip().lower() or "skyrimspecialedition"
    from ..utils.cache_manager import get_wepawn_cache

    cache = get_wepawn_cache()
    cached = cache.get_metadata_data(domain, int(mod_id))
    if cached is not None:
        _hard_log(
            f"[CACHE DBG] HIT! 넥서스 호출 우회 완료 - ID: {mod_id} / 유형: metadata"
        )
        ts: int | None = None
        raw_ts = cached.get("updated_timestamp")
        if raw_ts is not None:
            try:
                ts = int(raw_ts)
            except (TypeError, ValueError):
                ts = None
        return NexusModRecord(
            name=str(cached.get("name") or "").strip() or f"Mod {mod_id}",
            updated_timestamp=ts,
            summary=str(cached.get("summary") or "").strip(),
            description_plain=str(cached.get("description_plain") or "").strip(),
        )

    _hard_log(f"[CACHE DBG] MISS! 네트워크 스크랩 진행 - ID: {mod_id}")
    mod_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}.json"
    try:
        payload = _nexus_get_json(mod_url, api_key=api_key, application=application, timeout=timeout)
    except NexusAPIError:
        return None
    root = _mod_json_root(payload)
    if root is None:
        return None
    name = str(root.get("name") or "").strip() or f"Mod {mod_id}"
    summary = str(root.get("summary") or "").strip()
    desc_html = str(root.get("description") or "")
    desc_plain = _strip_html_to_plain(desc_html, 900)
    ts: int | None = None
    for key in ("updated_timestamp", "updatedTimestamp", "updated_time"):
        raw = root.get(key)
        if raw is None:
            continue
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            continue
        break
    rec = NexusModRecord(
        name=name,
        updated_timestamp=ts,
        summary=summary,
        description_plain=desc_plain,
    )
    cache.set_metadata_data(
        domain,
        int(mod_id),
        {
            "name": rec.name,
            "updated_timestamp": rec.updated_timestamp,
            "summary": rec.summary,
            "description_plain": rec.description_plain,
        },
    )
    return rec


def fetch_mod_file_dependencies(
    api_key: str,
    game_domain: str,
    mod_id: int,
    file_id: int,
    *,
    application: str = "WepawnAI",
    timeout: float = 30.0,
) -> list[NexusDependencyLink]:
    """
    Return Nexus mod requirements from the mod JSON, optionally merged with a file JSON.

    If ``file_id`` > 0, also requests ``.../files/{file_id}.json`` and merges any file-level deps.

    If the mod payload has no dependency nodes and ``file_id`` is 0, lists mod files via
    ``.../files.json``, picks a primary (category_id == 1) file when possible, and merges deps
    from that file's JSON — some requirement data only appears on the file record.
    """
    domain = (game_domain or "").strip().lower() or "skyrimspecialedition"
    mod_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}.json"
    mod_payload = _nexus_get_json(mod_url, api_key=api_key, application=application, timeout=timeout)

    links: list[NexusDependencyLink] = []
    if isinstance(mod_payload, Mapping):
        links = _links_from_items(domain, _dependency_item_mappings(mod_payload))

    fid = int(file_id)
    if fid > 0:
        file_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}/files/{fid}.json"
        file_payload = _nexus_get_json(file_url, api_key=api_key, application=application, timeout=timeout)
        if isinstance(file_payload, Mapping):
            extra = _links_from_items(domain, _dependency_item_mappings(file_payload))
            links = _merge_unique_links(links, extra)

    if not links and fid == 0:
        files_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}/files.json"
        try:
            files_raw = _nexus_get_json(files_url, api_key=api_key, application=application, timeout=timeout)
        except NexusAPIError:
            files_raw = None
        files_map = _files_list_root(files_raw) if files_raw is not None else None
        if files_map:
            auto_fid = _pick_primary_file_id(files_map)
            if auto_fid > 0:
                file_url = f"{NEXUS_REST_V1}/games/{domain}/mods/{int(mod_id)}/files/{auto_fid}.json"
                try:
                    file_payload = _nexus_get_json(
                        file_url, api_key=api_key, application=application, timeout=timeout
                    )
                except NexusAPIError:
                    file_payload = None
                if isinstance(file_payload, Mapping):
                    extra = _links_from_items(domain, _dependency_item_mappings(file_payload))
                    links = _merge_unique_links(links, extra)

    return links


_REQ_SUBSTR = ("requirement", "depend", "prereq", "relation")


def _mod_id_from_nexus_page_href(href: str) -> int | None:
    m = re.search(r"nexusmods\.com/[^/\"'\s]+/mods/(\d+)\b", href or "", flags=re.I)
    if not m:
        return None
    try:
        i = int(m.group(1), 10)
    except ValueError:
        return None
    return i if 0 < i < 2_000_000_000 else None


def _strip_simple_tags(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(t.split()).strip()


def _extract_next_data_json(html: str) -> dict[str, Any] | None:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<json>.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    raw = m.group("json").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _walk_next_data_requirement_rows(
    obj: Any,
    under_req: bool,
    exclude_mod_id: int,
    out: list[MutableMapping[str, Any]],
    depth: int,
) -> None:
    if depth > 48 or len(out) >= 200:
        return
    if isinstance(obj, dict):
        key_hit = any(
            any(s in str(k).lower() for s in _REQ_SUBSTR) for k in obj.keys()
        )
        subtree = under_req or key_hit

        mid = obj.get("modId")
        if mid is None:
            mid = obj.get("mod_id")
        try:
            mid_i = int(mid) if mid is not None else 0
        except (TypeError, ValueError):
            mid_i = 0
        href = str(obj.get("url") or obj.get("href") or "").strip()
        if mid_i <= 0 and href:
            parsed = _mod_id_from_nexus_page_href(href)
            mid_i = parsed or 0
        name = obj.get("name") or obj.get("modName") or obj.get("title") or obj.get("mod_name")
        name_s = str(name).strip() if name is not None else ""
        note_raw = obj.get("notes") or obj.get("note") or obj.get("description") or ""
        note_s = str(note_raw).strip() if note_raw is not None else ""

        if (
            subtree
            and mid_i > 0
            and mid_i != exclude_mod_id
            and len(name_s) >= 1
            and ("nexusmods.com" in href.lower() or obj.get("modId") is not None or obj.get("mod_id") is not None)
        ):
            out.append({"name": name_s, "mod_id": mid_i, "note": note_s})

        for v in obj.values():
            _walk_next_data_requirement_rows(v, subtree, exclude_mod_id, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:600]:
            _walk_next_data_requirement_rows(item, under_req, exclude_mod_id, out, depth + 1)


def _parse_requirements_table_html(
    html: str,
    game_domain: str,
    exclude_mod_id: int,
) -> list[MutableMapping[str, Any]]:
    """Best-effort: Nexus requirements region에서 mod 링크·Notes 열 추출."""
    chunk = slice_nexus_requirements_html_chunk(html)
    if not chunk:
        low = html.lower()
        anchors: list[int] = []
        for needle in (
            'id="requirements"',
            "mods requiring",
            "required mods",
        ):
            i = low.find(needle)
            if i >= 0:
                anchors.append(i)
        if not anchors:
            return []
        start = max(0, min(anchors) - 800)
        end = start + 280_000
        mods_requiring_idx = low.find("mods requiring this file", start)
        if mods_requiring_idx > start:
            end = mods_requiring_idx
        chunk = html[start:end]
    dom = re.escape((game_domain or "").strip().lower())
    out: list[MutableMapping[str, Any]] = []
    # Row: mod link + optional second cell as note
    row_rx = re.compile(
        rf"""<tr[^>]*>[\s\S]*?
        <a[^>]+href=["']([^"']*nexusmods\.com/{dom}/mods/(\d+)[^"']*)["'][^>]*>([\s\S]*?)</a>
        [\s\S]*?</tr>""",
        re.I | re.VERBOSE,
    )
    for m in row_rx.finditer(chunk):
        href, mid_s, name_html = m.group(1), m.group(2), m.group(3)
        try:
            mid = int(mid_s, 10)
        except ValueError:
            continue
        if mid <= 0 or mid == exclude_mod_id:
            continue
        name = _strip_simple_tags(name_html)
        if not name:
            name = f"Mod {mid}"
        rest = m.group(0)
        note = ""
        td_after = re.search(
            r"</a>\s*</td>\s*<td[^>]*>([\s\S]*?)</td>",
            rest,
            re.I,
        )
        if td_after:
            note = _strip_simple_tags(td_after.group(1))
        out.append({"name": name, "mod_id": mid, "note": note})
        if len(out) >= 120:
            break
    return out


def _rows_from_requirements_bundle(
    bundle: Mapping[str, Any],
    *,
    parent_mod_id: int,
) -> list[dict[str, Any]]:
    mid_ex = int(parent_mod_id)
    rows_hit: list[dict[str, Any]] = []
    seen_hit: set[int] = set()
    for r in bundle.get("rows") or []:
        if not isinstance(r, dict):
            continue
        try:
            mid = int(r.get("mod_id") or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0 or mid == mid_ex or mid in seen_hit:
            continue
        seen_hit.add(mid)
        rows_hit.append(
            {
                "name": str(r.get("name") or f"Mod {mid}").strip(),
                "mod_id": mid,
                "note": str(r.get("note") or "").strip(),
            }
        )
    return rows_hit


def _req_tab_row_summary(rows: Sequence[Mapping[str, Any]], limit: int = 15) -> str:
    """Requirements 스크랩 진단용: mod_id=이름 미리보기."""
    parts: list[str] = []
    for r in list(rows)[:limit]:
        try:
            mid = int(r.get("mod_id") or 0)
        except (TypeError, ValueError):
            mid = 0
        nm = str(r.get("name") or "")[:46]
        parts.append(f"{mid}={nm!r}")
    tail = f" …(+{len(rows) - limit} more)" if len(rows) > limit else ""
    return "; ".join(parts) + tail


def _scrape_nexus_requirements_from_page(
    game_domain: str,
    mod_id: int,
    session: Any | None = None,
    *,
    timeout: float = 12.0,
) -> list[dict[str, Any]]:
    """
    공개 모드 페이지 HTML에서 Nexus Requirements 테이블(또는 동등 데이터)을 스크랩한다.

    반환: ``[{"name": str, "mod_id": int, "note": str}, ...]`` (API 키 불필요).

    HTTP는 ``ai.nexus_scraper._fetch_public_nexus_mod_page_html`` (Playwright 하위 프로세스)로 수행한다.
    ``session`` 인자는 하위 호환용으로 유지되며 사용하지 않는다.
    BeautifulSoup 미사용(플러그인은 표준 라이브러리만 가정).
    """
    from ..ai.nexus_scraper import _fetch_public_nexus_mod_page_html
    from ..utils.cache_manager import get_wepawn_cache

    dom = (game_domain or "").strip().lower() or "skyrimspecialedition"
    mid_ex = int(mod_id)
    core_bundle = get_core_requirements_bundle(dom, mid_ex)
    if core_bundle is not None:
        _hard_log(
            f"[CORE_DEP DBG] HIT core_dependencies.json - ID: {mid_ex} / domain: {dom}"
        )
        rows_hit = _rows_from_requirements_bundle(
            core_bundle, parent_mod_id=mid_ex
        )
        ids_hit = sorted(
            {
                int(r.get("mod_id") or 0)
                for r in rows_hit
                if int(r.get("mod_id") or 0) > 0
            }
        )
        _hard_log(
            f"[REQ_TAB] source=core_dependencies domain={dom} mod_id={mid_ex} "
            f"row_count={len(rows_hit)} mod_ids={ids_hit} "
            f"preview={_req_tab_row_summary(rows_hit)}"
        )
        return rows_hit

    cache = get_wepawn_cache()
    bundle = cache.get_requirements_bundle(dom, mid_ex)
    if bundle is not None:
        _hard_log(
            f"[CACHE DBG] HIT! 넥서스 호출 우회 완료 - ID: {mid_ex} / 유형: requirements"
        )
        rows_hit = _rows_from_requirements_bundle(bundle, parent_mod_id=mid_ex)
        ids_hit = sorted(
            {
                int(r.get("mod_id") or 0)
                for r in rows_hit
                if int(r.get("mod_id") or 0) > 0
            }
        )
        _hard_log(
            f"[REQ_TAB] source=wepawn_cache domain={dom} mod_id={mid_ex} "
            f"row_count={len(rows_hit)} mod_ids={ids_hit} "
            f"preview={_req_tab_row_summary(rows_hit)}"
        )
        return rows_hit

    _hard_log(
        f"[CACHE DBG] MISS! 네트워크 스크랩 진행 - ID: {mid_ex} / domain: {dom}"
    )
    _hard_log(
        f"[REQ_TAB] playwright will open ?tab=requirements "
        f"(mod {mid_ex}; USSEP SSE=nexus mod_id 266)"
    )
    html = _fetch_public_nexus_mod_page_html(
        dom, mid_ex, timeout=float(timeout), tab="requirements"
    )
    if not (html or "").strip():
        _hard_log(
            f"[REQ_TAB] empty_html domain={dom} mod_id={mid_ex} "
            "(Playwright 실패·빈 stdout·Cloudflare 등)"
        )
        return []

    low = html.casefold()
    has_next = "__next_data__" in low
    has_nexus_req = "nexus requirements" in low
    _hard_log(
        f"[REQ_TAB] html_len={len(html)} __NEXT_DATA__ present={has_next} "
        f'"nexus requirements" in html={has_nexus_req}'
    )

    rows_nd: list[MutableMapping[str, Any]] = []
    data = _extract_next_data_json(html)
    if isinstance(data, dict):
        _walk_next_data_requirement_rows(data, False, mid_ex, rows_nd, 0)
    table_fallback = False
    rows: list[MutableMapping[str, Any]] = list(rows_nd)
    if not rows:
        rows = _parse_requirements_table_html(html, dom, mid_ex)
        table_fallback = True
    _hard_log(
        f"[REQ_TAB] parse next_data_rows={len(rows_nd)} "
        f"table_html_fallback={table_fallback} "
        f"rows_pre_dedup={len(rows)} "
        f"preview_pre_dedup={_req_tab_row_summary(rows)}"
    )

    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        mid = int(r.get("mod_id") or 0)
        if mid <= 0 or mid == mid_ex or mid in seen:
            continue
        seen.add(mid)
        out.append(
            {
                "name": str(r.get("name") or f"Mod {mid}").strip(),
                "mod_id": mid,
                "note": str(r.get("note") or "").strip(),
            }
        )
    ids_out = sorted({int(r.get("mod_id") or 0) for r in out if int(r.get("mod_id") or 0) > 0})
    ussep = any(int(r.get("mod_id") or 0) == 266 for r in out)
    _hard_log(
        f"[REQ_TAB] final row_count={len(out)} mod_ids={ids_out} "
        f"ussep_nexus_266_in_list={ussep} "
        f"preview={_req_tab_row_summary(out)}"
    )
    cache.set_requirements_bundle(
        dom,
        mid_ex,
        rows=list(out),
        title_clean=None,
        is_external=None,
    )
    return out


def dependency_links_html(links: Sequence[NexusDependencyLink]) -> str:
    """Format dependency links as HTML suitable for :class:`PyQt6.QtWidgets.QTextBrowser`."""
    if not links:
        return ""
    parts: list[str] = ["<ul>"]
    for link in links:
        safe_name = (
            link.name.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        parts.append(f'<li><a href="{link.url}">{safe_name}</a> (mod {link.mod_id})</li>')
    parts.append("</ul>")
    return "".join(parts)
