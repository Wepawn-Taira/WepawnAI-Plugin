"""
Fetch public Nexus Mods mod page HTML (no API key) and extract description / requirements text.

Also extracts **dependency mod links** from ``__NEXT_DATA__`` and Requirements-adjacent HTML
when the REST API returns no structured requirements (no API key needed for GET).

Public page fetch uses **portable Playwright** only (``bin/portable_python/python.exe`` +
``tools/pw_scraper.py`` + ``PLAYWRIGHT_BROWSERS_PATH`` → ``bin/portable_python/ms-playwright``).
There is no curl fallback — failures return ``None`` upstream.

Uses stdlib (``re`` + ``json`` + ``subprocess``) and optionally **beautifulsoup4** for the
``Nexus requirements`` table (``table-require-name`` / ``table-require-notes``). Install in the
portable venv: ``pip install beautifulsoup4``.

Runs from a worker thread; short timeouts avoid blocking diagnosis.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..nexus.dependencies import NexusDependencyLink, nexus_mod_url
from ..utils.hard_log import _hard_log


def _plugin_root_dir() -> str:
    """WepawnAI plugin root (parent of ``ai/``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _playwright_portable_python_exe() -> str:
    return os.path.join(_plugin_root_dir(), "bin", "portable_python", "python.exe")


def _playwright_browser_bundle_dir() -> str:
    return os.path.join(_plugin_root_dir(), "bin", "portable_python", "ms-playwright")


def _pw_scraper_script_path() -> str:
    return os.path.join(_plugin_root_dir(), "tools", "pw_scraper.py")


def _summarize_pw_stderr(stderr_text: str) -> str:
    """Last non-empty stderr line, max 150 chars — for [BRIDGE_DIAG] PW_SCRAPER_FAILED."""
    if not stderr_text:
        return "Unknown PW Error"

    lines = [line.strip() for line in stderr_text.strip().split("\n") if line.strip()]
    if not lines:
        return "Unknown PW Error"

    core_error = lines[-1]
    return core_error[:150] + ("..." if len(core_error) > 150 else "")


def _fetch_nexus_page_html(url: str, *, timeout: float) -> str:
    """
    GET Nexus mod page HTML via portable Playwright only.

    Returns empty string if portable stack is missing, subprocess fails, stdout is empty,
    or Cloudflare interstitial — **no curl fallback**.
    """
    portable_python = _playwright_portable_python_exe()
    scraper_script = _pw_scraper_script_path()
    browser_path = _playwright_browser_bundle_dir()

    if not os.path.isfile(portable_python):
        _hard_log(f"[BRIDGE_DIAG] PW_SCRAPER_FAILED: portable python 없음 ({portable_python})")
        return ""
    if not os.path.isfile(scraper_script):
        _hard_log(f"[BRIDGE_DIAG] PW_SCRAPER_FAILED: pw_scraper.py 없음 ({scraper_script})")
        return ""
    if not os.path.isdir(browser_path):
        _hard_log(
            f"[BRIDGE_DIAG] PW_SCRAPER_FAILED: ms-playwright 없음 — "
            f"bin/portable_python/README_SETUP.txt ({browser_path})"
        )
        return ""

    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.normpath(os.path.abspath(browser_path))

    cwd_path = os.path.dirname(os.path.abspath(scraper_script))

    cmd = [portable_python, scraper_script, url]
    subprocess_timeout = max(45.0, float(timeout))
    run_kw: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "timeout": subprocess_timeout,
        "env": env,
        "cwd": cwd_path,
    }
    if sys.platform == "win32":
        # [VDS 긴급] 은닉 기동 해제 — CMD/방화벽·Defender 팝업 확인용 일회성. 원인 규명 후 복구.
        # run_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        pass

    _hard_log(f"[BRIDGE_DIAG] EXEC CMD: {cmd}")
    _hard_log(f"[BRIDGE_DIAG] ENV PATH: {env.get('PLAYWRIGHT_BROWSERS_PATH')}")

    try:
        result = subprocess.run(cmd, **run_kw)
    except subprocess.TimeoutExpired as e:
        _to = getattr(e, "stdout", None) or getattr(e, "output", None)
        _hard_log(f"[BRIDGE_DIAG] TIMEOUT STDERR: {e.stderr or 'None'}")
        _hard_log(f"[BRIDGE_DIAG] TIMEOUT STDOUT: {_to or 'None'}")
        _hard_log("[BRIDGE_DIAG] PW_SCRAPER_FAILED: subprocess timeout")
        return ""
    except OSError as e:
        _hard_log(f"[BRIDGE_DIAG] EXC STDERR: None")
        _hard_log(f"[BRIDGE_DIAG] EXC STDOUT: None")
        _hard_log(f"[BRIDGE_DIAG] PW_SCRAPER_FAILED: OSError: {e}")
        return ""
    except Exception as e:
        _se = getattr(e, "stderr", None)
        _so = getattr(e, "stdout", None) or getattr(e, "output", None)
        _hard_log(f"[BRIDGE_DIAG] EXC STDERR: {_se or 'None'}")
        _hard_log(f"[BRIDGE_DIAG] EXC STDOUT: {_so or 'None'}")
        _hard_log(f"[BRIDGE_DIAG] PW_SCRAPER_FAILED: {e!r}")
        return ""

    err = result.stderr or ""
    if err.strip():
        for line in err.splitlines():
            t = line.strip()
            if t:
                _hard_log(t)

    if result.returncode != 0:
        reason = _summarize_pw_stderr(err)
        _hard_log(f"[BRIDGE_DIAG] PW_SCRAPER_FAILED: {reason}")
        return ""

    html = result.stdout or ""
    if not html.strip():
        _hard_log("[BRIDGE_DIAG] PW_SCRAPER_FAILED: stdout empty")
        return ""

    title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
    title = title_match.group(1) if title_match else "NO TITLE"
    cf_bypass = "Just a moment" not in title
    _hard_log(f"[SCRAPE DIAG] source=playwright title={title}")
    _hard_log(f"[SCRAPE DIAG] CF_BYPASS={cf_bypass}")
    _hard_log(f"[SCRAPE DIAG] html 앞 500자: {html[:500]}")
    if not cf_bypass:
        _hard_log("[BRIDGE_DIAG] PW_SCRAPER_FAILED: Cloudflare interstitial (Just a moment)")
        return ""

    _hard_log(f"[PW_SCRAPER] ok len={len(html)}")
    return html


def _fetch_public_nexus_mod_page_html(
    game_domain: str,
    mod_id: int,
    *,
    timeout: float = 15.0,
    tab: str | None = None,
) -> str:
    url = f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}"
    if tab and str(tab).strip():
        url = f"{url}?tab={str(tab).strip()}"
        _hard_log(f"[REQ_TAB] playwright_target_url={url}")
    return _fetch_nexus_page_html(url, timeout=timeout)

_GATE_SUBSTRINGS = (
    "you must be logged in",
    "you need to be logged in",
    # Adult interstitial: logged-in Nexus session serves real mod HTML; do not hard-block on copy.
    # "nsfw" 제거 — 넥서스 페이지 UI 자체에 nsfw 관련 문자열이 포함되어 있음
)

_SOFT_BLOCK_SUBSTRINGS = (
    "cf-browser-verification",
    "attention required",
    "just a moment",
    "enable javascript",
)


def _diag(msg: str) -> None:
    line = f"[WepawnAI DIAG] {msg}"
    print(line, flush=True)
    _hard_log(line)


_MAX_SCRAPED_DEPS = 28
_REQ_KEY_HINT = (
    "require",
    "depend",
    "prereq",
    "masterlist",
    "relation",
    "nexusmod",
    "modrequire",
)


def _coerce_positive_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        i = int(str(val).strip(), 10)
    except (TypeError, ValueError):
        return None
    return i if 0 < i < 2_000_000_000 else None


def _mod_id_from_nexus_url(text: str) -> int | None:
    m = re.search(r"nexusmods\.com/[^/\"'\s]+/mods/(\d+)\b", text, flags=re.I)
    if not m:
        return None
    try:
        i = int(m.group(1), 10)
    except ValueError:
        return None
    return i if i > 0 else None


def fetch_nexus_mod_page_html(
    url: str,
    *,
    timeout: float = 5.0,
    tab: str | None = None,
) -> tuple[int, str] | None:
    """
    GET a Nexus mod page; return ``(http_status, html_utf8)`` or ``None`` on hard failure.

    Shared by context scraper and dependency-link harvester (no API key).
    Uses portable Playwright only (see ``_fetch_nexus_page_html``); returns ``None`` on failure.

    ``tab``: Nexus 쿼리 ``?tab=…`` (예: ``requirements``). URL에 이미 ``tab`` 이 있으면
    인자가 없을 때 그 값을 쓴다.
    """
    url = (url or "").strip()
    if not url:
        return None
    to = float(timeout) if float(timeout) > 0 else 5.0
    m = re.search(
        r"https?://(?:www\.)?nexusmods\.com/([^/\"'\s]+)/mods/(\d+)\b",
        url,
        re.I,
    )
    if m:
        dom = m.group(1).strip().lower()
        try:
            mid = int(m.group(2), 10)
        except ValueError:
            mid = 0
        if dom and mid > 0:
            parsed = urlparse(url)
            tab_from_url: str | None = None
            if parsed.query:
                tvals = parse_qs(parsed.query).get("tab") or []
                tab_from_url = str(tvals[0]).strip() if tvals and tvals[0] else None
            tab_eff = tab if tab is not None else tab_from_url
            html = _fetch_public_nexus_mod_page_html(
                dom, mid, timeout=to, tab=tab_eff
            )
            return (200, html) if html else None

    html = _fetch_nexus_page_html(url, timeout=to)
    return (200, html) if html else None


def _page_usable(html: str) -> bool:
    hl = html.lower()
    for g in _GATE_SUBSTRINGS:
        if g in hl:
            _hard_log(f"[PAGE GATE] 하드 게이트 실패: {g!r}")
            return False
    for g in _SOFT_BLOCK_SUBSTRINGS:
        if g in hl and len(html) < 25_000:
            _hard_log(f"[PAGE GATE] 소프트 게이트 실패: {g!r} len={len(html)}")
            return False
    return True


def _dict_to_dependency_link(
    d: Mapping[str, Any],
    game_domain: str,
    *,
    require_context: bool,
) -> NexusDependencyLink | None:
    """Build a link if ``d`` looks like a Nexus mod reference."""
    url = str(d.get("url") or d.get("href") or d.get("uri") or "").strip()
    mid = _coerce_positive_int(d.get("modId") or d.get("mod_id"))
    if mid is None and url:
        mid = _mod_id_from_nexus_url(url)
    if mid is None and require_context:
        mid = _coerce_positive_int(d.get("id"))
    if mid is None:
        return None

    name = (
        d.get("name")
        or d.get("title")
        or d.get("modName")
        or d.get("mod_name")
        or d.get("displayName")
    )
    name_str = str(name).strip() if name is not None else ""
    if len(name_str) < 2:
        name_str = f"Nexus mod {mid}"
    note = d.get("description") or d.get("notes")
    note_str = str(note).strip() if note is not None and str(note).strip() else None
    link_url = url if url and "nexusmods.com" in url.lower() else nexus_mod_url(game_domain, mid)
    return NexusDependencyLink(name=name_str, mod_id=mid, url=link_url, note=note_str)


def _walk_next_data_for_dep_links(
    obj: Any,
    game_domain: str,
    exclude_mod_id: int,
    out: dict[int, NexusDependencyLink],
    depth: int,
    in_req_tree: bool,
) -> None:
    if depth > 32 or len(out) >= _MAX_SCRAPED_DEPS:
        return
    if isinstance(obj, dict):
        if in_req_tree:
            link = _dict_to_dependency_link(obj, game_domain, require_context=True)
            if link is not None and link.mod_id != exclude_mod_id and link.mod_id not in out:
                out[link.mod_id] = link

        for k, v in obj.items():
            kn = str(k).lower()
            subtree = in_req_tree or any(h in kn for h in _REQ_KEY_HINT)
            if isinstance(v, dict):
                _walk_next_data_for_dep_links(v, game_domain, exclude_mod_id, out, depth + 1, subtree)
            elif isinstance(v, list):
                for item in v[:400]:
                    _walk_next_data_for_dep_links(item, game_domain, exclude_mod_id, out, depth + 1, subtree)
    elif isinstance(obj, list):
        for item in obj[:400]:
            _walk_next_data_for_dep_links(item, game_domain, exclude_mod_id, out, depth + 1, in_req_tree)


def _requirements_adjacent_slice(html: str) -> str:
    """Narrow HTML to regions likely to list mod requirements (reduces unrelated mod links)."""
    lower = html.lower()
    anchors: list[int] = []
    for needle in (
        "requirements",
        "dependencies",
        "prerequisites",
        "mods requiring",
        "this mod requires",
        "required mods",
        "nexus requirements",
    ):
        i = lower.find(needle)
        if i >= 0:
            anchors.append(i)
    if not anchors:
        return ""
    start = max(0, min(anchors) - 1_200)
    end = min(len(html), min(anchors) + 42_000)
    return html[start:end]


def _nexus_mod_urls_in_text(
    text: str,
    game_domain: str,
    exclude_mod_id: int,
) -> dict[int, NexusDependencyLink]:
    dom = (game_domain or "").strip().lower()
    found: dict[int, NexusDependencyLink] = {}
    if not text or not dom:
        return found
    pat = re.compile(
        rf'https?://(?:www\.)?nexusmods\.com/({re.escape(dom)})/mods/(\d+)\b',
        flags=re.I,
    )
    for m in pat.finditer(text):
        try:
            mid = int(m.group(2), 10)
        except ValueError:
            continue
        if mid <= 0 or mid == exclude_mod_id or mid in found:
            continue
        u = m.group(0).strip().rstrip(").,]'\"}>")
        found[mid] = NexusDependencyLink(
            name=f"Nexus mod {mid}",
            mod_id=mid,
            url=u if u.startswith("http") else nexus_mod_url(game_domain, mid),
            note="scraped_page",
        )
        if len(found) >= _MAX_SCRAPED_DEPS:
            break
    return found


def _parse_nexus_requirements_table_bs4(
    html: str,
    *,
    game_domain: str,
    exclude_mod_id: int,
) -> list[NexusDependencyLink]:
    """
    Strict **Nexus requirements** block: ``h2``/``h3`` with exact ``get_text(strip=True) == "Nexus requirements"``,
    then the immediate following ``<table>``; only ``td.table-require-name`` / ``td.table-require-notes`` rows.

    Returns ``[]`` if BeautifulSoup is missing, header/table not found, or no valid rows.
    """
    _hard_log(
        f"[PARSER DIAG] html len={len(html)}, has_header={'Nexus requirements' in html}"
    )
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        _hard_log("[PARSER DIAG] beautifulsoup4 미설치 — pip install beautifulsoup4")
        _hard_log("[PARSER DIAG] 필터링된 필수 모드 개수: 0")
        return []

    soup = BeautifulSoup(html, "html.parser")
    header = soup.find(
        lambda tag: tag.name in ("h2", "h3")
        and tag.get_text(strip=True) == "Nexus requirements"
    )
    if not header:
        _hard_log("[PARSER DIAG] 필터링된 필수 모드 개수: 0")
        return []

    table = header.find_next("table")
    if not table:
        _hard_log("[PARSER DIAG] 필터링된 필수 모드 개수: 0")
        return []

    cls_name = re.compile(r"table-require-name")
    cls_notes = re.compile(r"table-require-notes")
    parsed_items: list[NexusDependencyLink] = []

    for tr in table.find_all("tr"):
        name_td = tr.find("td", class_=cls_name)
        if not name_td:
            continue
        notes_td = tr.find("td", class_=cls_notes)
        name_text = name_td.get_text(strip=True)
        notes_text = notes_td.get_text(strip=True) if notes_td else ""

        mid: int | None = None
        url_out = ""
        for a in name_td.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            cand = _mod_id_from_nexus_url(href)
            if cand is not None and cand > 0:
                mid = cand
                url_out = href if href.startswith("http") else ""
                break
        if mid is None or mid <= 0 or mid == exclude_mod_id:
            continue

        if not name_text:
            name_text = f"Nexus mod {mid}"

        note_merged = notes_text.strip() or None
        link_url = (
            url_out
            if url_out and "nexusmods.com" in url_out.lower()
            else nexus_mod_url(game_domain, mid)
        )
        parsed_items.append(
            NexusDependencyLink(name=name_text, mod_id=mid, url=link_url, note=note_merged)
        )

    _hard_log(f"[PARSER DIAG] 필터링된 필수 모드 개수: {len(parsed_items)}")
    return parsed_items


def scrape_nexus_mod_requirement_links(
    game_domain: str,
    mod_id: int,
    *,
    timeout: float = 6.0,
    max_links: int = 24,
) -> list[NexusDependencyLink]:
    """
    Public mod page only: collect dependency-like Nexus mod links (no API key).

    Uses ``__NEXT_DATA__`` walk, Requirements-adjacent URL scan, and — when **beautifulsoup4**
    is installed — the ``Nexus requirements`` heading plus the following table
    (``table-require-name`` / ``table-require-notes``). Table rows override prior hits for the same mod id.
    """
    url = build_nexus_mod_page_url(game_domain, mod_id, tab="requirements")
    if not url:
        return []
    fetched = fetch_nexus_mod_page_html(url, timeout=float(timeout))
    if fetched is None:
        return []
    status, html = fetched
    if status != 200 or not html.strip():
        _diag(f"NEXUS_DEPS_SCRAPE bad status={status} url={url!r}")
        return []
    if not _page_usable(html):
        _diag("NEXUS_DEPS_SCRAPE page failed gate heuristic — skip link harvest")
        return []

    by_id: dict[int, NexusDependencyLink] = {}
    data = _extract_next_data(html)
    if isinstance(data, dict):
        _walk_next_data_for_dep_links(data, game_domain, int(mod_id), by_id, 0, False)

    region = _requirements_adjacent_slice(html)
    if region:
        by_id.update(_nexus_mod_urls_in_text(region, game_domain, int(mod_id)))

    # API-only installs often miss page deps; if harvest is thin, widen scan from "requirements".
    if len(by_id) <= 2:
        low = html.lower()
        idx = low.find("requirements")
        if idx < 0:
            idx = low.find("dependencies")
        if idx >= 0:
            chunk = html[idx : idx + 140_000]
            by_id.update(_nexus_mod_urls_in_text(chunk, game_domain, int(mod_id)))

    # DOM table (exact header + table-require-name) overrides / fills authoritative rows
    for link in _parse_nexus_requirements_table_bs4(
        html, game_domain=game_domain, exclude_mod_id=int(mod_id)
    ):
        by_id[link.mod_id] = link

    out = list(by_id.values())[: max(1, min(int(max_links), _MAX_SCRAPED_DEPS))]
    _diag(
        f"NEXUS_DEPS_SCRAPE mod_id={mod_id} domain={game_domain!r} "
        f"harvested_unique={len(by_id)} returning={len(out)}"
    )
    return out


def build_nexus_mod_page_url(
    game_domain: str,
    mod_id: int,
    *,
    tab: str | None = None,
) -> str:
    """``https://www.nexusmods.com/{game_domain}/mods/{id}`` (``game_domain`` from MO2 ``gameNexusName()``)."""
    dom = (game_domain or "").strip().strip("/").lower()
    if not dom or int(mod_id) <= 0:
        return ""
    base = f"https://www.nexusmods.com/{dom}/mods/{int(mod_id)}"
    t = (tab or "").strip()
    return f"{base}?tab={t}" if t else base


def _collapse_ws(s: str) -> str:
    return " ".join(s.split())


def _strip_html_to_text(html: str, max_chars: int) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _collapse_ws(t)
    return t[:max_chars] if max_chars > 0 else t


def _extract_next_data(html: str) -> dict[str, Any] | None:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<json>.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    raw = m.group("json").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _diag(f"NEXUS_SCRAPER __NEXT_DATA__ JSON decode failed: {exc}")
        return None


def _walk_key_hints(obj: Any, key_hints: tuple[str, ...], out: list[str], depth: int = 0) -> None:
    if depth > 28:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k).lower()
            if isinstance(v, str) and len(v.strip()) > 35:
                if any(h in ks for h in key_hints):
                    out.append(v.strip())
            if isinstance(v, (dict, list)):
                _walk_key_hints(v, key_hints, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:250]:
            _walk_key_hints(item, key_hints, out, depth + 1)


def _walk_requirement_structures(obj: Any, out: list[str], depth: int = 0) -> None:
    if depth > 28:
        return
    hints = ("requirement", "dependenc", "prereq", "needs", "masterlist")
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k).lower()
            if any(h in ks for h in hints):
                if isinstance(v, str) and len(v.strip()) > 8:
                    out.append(v.strip())
                elif isinstance(v, (list, dict)):
                    frag = json.dumps(v, ensure_ascii=False)
                    if len(frag) > 1200:
                        frag = frag[:1200] + "…"
                    out.append(frag)
            if isinstance(v, (dict, list)):
                _walk_requirement_structures(v, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:200]:
            _walk_requirement_structures(item, out, depth + 1)


def _merge_unique_chunks(chunks: list[str], max_chars: int) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for c in chunks:
        c = _collapse_ws(c.strip())
        if len(c) < 30:
            continue
        sig = c[:100]
        if sig in seen:
            continue
        seen.add(sig)
        parts.append(c)
    merged = "\n\n".join(parts)
    return merged[:max_chars] if max_chars > 0 else merged


def scrape_nexus_mod_context(url: str, *, timeout: float = 4.0, max_chars: int = 2000) -> str:
    """
    GET the mod page, parse ``__NEXT_DATA__`` when possible, else visible HTML text.
    Returns ``""`` on any failure (caller uses name-only fallback). ``timeout`` caps wait (typically 3–5s).
    """
    url = (url or "").strip()
    if not url:
        _diag("NEXUS_SCRAPER empty URL — FALLBACK name-only analysis")
        return ""

    to = float(timeout)
    if to <= 0:
        to = 4.0
    _diag(f"NEXUS_SCRAPER GET {url!r} timeout={to}s")

    fetched = fetch_nexus_mod_page_html(url, timeout=to)
    if fetched is None:
        return ""
    status, html = fetched
    if status != 200:
        _diag(f"NEXUS_SCRAPER HTTP status={status} url={url!r} — FALLBACK name-only analysis")
        return ""

    if not _page_usable(html):
        _diag("NEXUS_SCRAPER page failed gate heuristic — FALLBACK name-only analysis")
        return ""

    chunks: list[str] = []
    data = _extract_next_data(html)
    if data is not None:
        desc_hints = (
            "description",
            "longdescription",
            "shortdescription",
            "readme",
            "summary",
            "overview",
            "moddescription",
        )
        _walk_key_hints(data, desc_hints, chunks)
        _walk_requirement_structures(data, chunks)

    text = _merge_unique_chunks(chunks, max_chars)
    if len(text.strip()) < 80:
        text = _strip_html_to_text(html, max_chars + 400)
        text = _collapse_ws(text)[:max_chars]

    if not text.strip():
        _diag("NEXUS_SCRAPER no usable text after parse — FALLBACK name-only analysis")
        return ""

    final = text[:max_chars] if max_chars > 0 else text
    _diag(f"NEXUS_SCRAPER final cleaned text length={len(final)} (cap={max_chars})")
    return final
