"""
Background DFS over Nexus prerequisites (stage 2): requirements valley-slice parse + OR wait.

OR branches block on :attr:`wait_event` (60s) until UI calls :meth:`apply_or_choice` or timeout.
"""

from __future__ import annotations

import heapq
import html
import re
from threading import Event
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..ai.nexus_scraper import (
    build_nexus_mod_page_url,
    fetch_nexus_mod_page_html,
)
from ..nexus.dependencies import (
    NexusDependencyLink,
    nexus_links_from_requirements_bundle,
    nexus_mod_url,
    slice_nexus_requirements_html_chunk,
)
from ..utils.core_dependencies import get_core_requirements_bundle
from ..utils.hard_log import _hard_log, python_hard_log
from ..utils.mo2_nemesis_launch import FNIS_NEXUS_ID, NEMESIS_NEXUS_ID

_MAX_RE_DEPS = 28
# Nexus 선행 DFS: 과도한 전이·무관 모드 연쇄 방지 (루트 depth=0).
_MAX_DEPENDENCY_RECURSION_DEPTH = 6

_DIAG_XP32_MAX_SKEL_ID = 1988
_FNIS_NEMESIS_IDS: frozenset[int] = frozenset({FNIS_NEXUS_ID, NEMESIS_NEXUS_ID})
_TD_REQUIRE_MOD_ID = re.compile(
    r'class="table-require-name".*?nexusmods\.com/.*?/mods/(\d+)',
    re.IGNORECASE | re.DOTALL,
)
_ROW_REQUIRE_NAME_NOTES = re.compile(
    r'<td[^>]*\bclass\s*=\s*["\'][^"\']*table-require-name[^"\']*["\'][^>]*>'
    r"[\s\S]*?nexusmods\.com/[^\"'>\s]+/mods/(\d+)"
    r"[\s\S]*?</td>\s*"
    r'<td[^>]*\bclass\s*=\s*["\'][^"\']*table-require-notes[^"\']*["\'][^>]*>([\s\S]*?)</td>',
    re.IGNORECASE,
)
_NOTE_OR_HINT = re.compile(
    r"(pick\s+one|choose\s+one|either|alternative\s+to|alternative|one\s+of|\bor\b|\ub610\ub294|택\s*1|다음\s*중)",
    re.IGNORECASE,
)
# ``alternative to Some Mod Name`` — 대상 모드명 추출(첫 줄만).
_NOTE_ALTERNATIVE_TO = re.compile(
    r"\balternative\s+to\b\s*[:\-–—]?\s*(.+)$",
    re.IGNORECASE,
)

python_VR_AE_FILTER = re.compile(
    r"\bVR\b|\bSKSEVR\b|\bAE\b.*\bVR\b", re.IGNORECASE
)
python_NOTE_VR_ONLY_RE = re.compile(
    r"\b(for\s+vr|vr\s+only|vr\s+version|sksevr|vr\s+users?\s+only)\b",
    re.IGNORECASE,
)


class GuideScanAborted(Exception):
    """User closed the alternative-selection modal or cancelled OR resolution."""


def _is_note_vr_only(note: str) -> bool:
    return bool(python_NOTE_VR_ONLY_RE.search(note or ""))


# Nexus ``<title>`` patterns: ``… at <Game> Nexus`` and generic hub titles per domain.
_NEXUS_AT_MARKER_BY_DOMAIN: dict[str, str] = {
    "skyrimspecialedition": " at Skyrim Special Edition Nexus",
    "skyrim": " at Skyrim Nexus",
    "fallout4": " at Fallout 4 Nexus",
    "newvegas": " at Fallout New Vegas Nexus",
    "fallout3": " at Fallout 3 Nexus",
    "oblivion": " at Oblivion Nexus",
    "morrowind": " at Morrowind Nexus",
    "enderal": " at Enderal Nexus",
    "enderalse": " at Enderal Special Edition Nexus",
    "cyberpunk2077": " at Cyberpunk 2077 Nexus",
    "starfield": " at Starfield Nexus",
}
_NEXUS_GENERIC_TITLE_BY_DOMAIN: dict[str, str] = {
    "skyrimspecialedition": "Skyrim Special Edition Nexus - Mods and Community",
    "skyrim": "Skyrim Nexus - Mods and Community",
    "fallout4": "Fallout 4 Nexus - Mods and Community",
    "newvegas": "Fallout New Vegas Nexus - Mods and Community",
    "fallout3": "Fallout 3 Nexus - Mods and Community",
    "oblivion": "Oblivion Nexus - Mods and Community",
    "morrowind": "Morrowind Nexus - Mods and Community",
    "enderal": "Enderal Nexus - Mods and Community",
    "enderalse": "Enderal Special Edition Nexus - Mods and Community",
    "cyberpunk2077": "Cyberpunk 2077 Nexus - Mods and Community",
    "starfield": "Starfield Nexus - Mods and Community",
}


def _note_suggests_or_alternatives(note: str) -> bool:
    return bool(note and _NOTE_OR_HINT.search(note.strip()))


def _alternative_to_target_phrase(note: str) -> str | None:
    """Note 첫 줄에서 ``alternative to …`` 뒤의 대상 모드명/구문."""
    plain = (note or "").strip()
    if not plain:
        return None
    first = plain.replace("\r", "\n").split("\n", 1)[0].strip()
    m = _NOTE_ALTERNATIVE_TO.search(first)
    if not m:
        return None
    raw = m.group(1).strip()
    raw = re.sub(r"\s+", " ", raw)
    raw = raw.strip(" \t.,;:\"'")
    # 괄호 앞까지만 (예: FNIS (old))
    raw = re.split(r"\s*[\(\[\{]", raw, maxsplit=1)[0].strip()
    return raw if len(raw) >= 2 else None


def _link_name_matches_target_phrase(link_name: str, target: str) -> bool:
    a = (link_name or "").casefold().strip()
    b = (target or "").casefold().strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    aw = a.split()[0] if a.split() else ""
    bw = b.split()[0] if b.split() else ""
    if len(aw) >= 3 and len(bw) >= 3 and (aw in b or bw in a):
        return True
    return False


def _find_link_for_alternative_to_target(
    links: list[NexusDependencyLink],
    target: str,
    *,
    exclude_mod_id: int,
) -> NexusDependencyLink | None:
    partial: NexusDependencyLink | None = None
    for lk in links:
        try:
            mid = int(lk.mod_id)
        except (TypeError, ValueError):
            continue
        if mid == int(exclude_mod_id):
            continue
        name = str(lk.name or "").strip()
        if name.casefold().strip() == target.casefold().strip():
            return lk
        if _link_name_matches_target_phrase(name, target):
            partial = partial or lk
    return partial


def _initial_requirement_groups(
    links: list[NexusDependencyLink],
) -> tuple[dict[str, list[NexusDependencyLink]], list[str]]:
    groups: dict[str, list[NexusDependencyLink]] = {}
    order_keys: list[str] = []
    for link in links:
        note = (link.note or "").strip()
        if _note_suggests_or_alternatives(note):
            key = f"or::{note.casefold()}"
        else:
            key = f"m::{link.mod_id}"
        if key not in groups:
            groups[key] = []
            order_keys.append(key)
        groups[key].append(link)
    return groups, order_keys


def _merge_mandatory_keys_to_alternative_to_or(
    groups: dict[str, list[NexusDependencyLink]],
    order_keys: list[str],
    keys_to_merge: set[str],
    member_mod_ids: list[int],
    order_index: dict[int, int],
) -> None:
    """여러 ``m::`` 그룹을 하나의 ``or::alternative_to:…`` 그룹으로 합친다."""
    positions: list[int] = []
    buf: list[NexusDependencyLink] = []
    for k in keys_to_merge:
        positions.append(order_keys.index(k))
        buf.extend(groups[k])
    buf.sort(key=lambda lk: order_index.get(int(lk.mod_id), 10**9))
    seen: set[int] = set()
    merged: list[NexusDependencyLink] = []
    for lk in buf:
        mid = int(lk.mod_id)
        if mid not in seen:
            seen.add(mid)
            merged.append(lk)
    insert_at = min(positions)
    for k in sorted(keys_to_merge, key=lambda kk: order_keys.index(kk), reverse=True):
        del groups[k]
        order_keys.remove(k)
    tag = "_".join(str(x) for x in sorted(seen))
    new_key = f"or::alternative_to:{tag}"
    order_keys.insert(insert_at, new_key)
    groups[new_key] = merged
    _hard_log(
        f"[OR DIAG] alternative_to: mandatory 병합 → {new_key!r} "
        f"mod_ids={[int(x.mod_id) for x in merged]}"
    )


def _apply_alternative_to_pairing(
    groups: dict[str, list[NexusDependencyLink]],
    order_keys: list[str],
    links: list[NexusDependencyLink],
) -> None:
    """
    Note에 ``alternative to X`` 가 있으면, 이름이 X와 맞는 다른 행과 같은 OR 그룹으로 묶는다.

    서로 다른 ``m::`` 단독 그룹끼리만 병합한다(기존 다항 OR 그룹은 건드리지 않음).
    """
    pairs: list[tuple[int, int]] = []
    for lk in links:
        tgt = _alternative_to_target_phrase(lk.note or "")
        if not tgt:
            continue
        try:
            src_mid = int(lk.mod_id)
        except (TypeError, ValueError):
            continue
        other = _find_link_for_alternative_to_target(links, tgt, exclude_mod_id=src_mid)
        if other is None:
            _hard_log(
                f"[OR DIAG] alternative_to: 대상 모드 미매칭 phrase={tgt!r} "
                f"(note on mod_id={src_mid})"
            )
            continue
        try:
            dst_mid = int(other.mod_id)
        except (TypeError, ValueError):
            continue
        if src_mid == dst_mid:
            continue
        pairs.append((src_mid, dst_mid))
        _hard_log(
            f"[OR DIAG] alternative_to: 엣지 mod_id {src_mid} ↔ {dst_mid} (phrase={tgt!r})"
        )

    if not pairs:
        return

    parent: dict[int, int] = {}

    def ufind(x: int) -> int:
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = ufind(parent[x])
        return parent[x]

    def uunion(a: int, b: int) -> None:
        ra, rb = ufind(a), ufind(b)
        if ra != rb:
            parent[rb] = ra

    involved: set[int] = set()
    for a, b in pairs:
        uunion(a, b)
        involved.add(a)
        involved.add(b)

    for mid in involved:
        ufind(mid)

    comp_members: dict[int, list[int]] = {}
    for mid in involved:
        r = ufind(mid)
        comp_members.setdefault(r, []).append(mid)

    order_index = {int(lk.mod_id): i for i, lk in enumerate(links)}

    for _root, mids_raw in comp_members.items():
        mids = list(dict.fromkeys(mids_raw))
        if len(mids) < 2:
            continue

        keys_set: set[str] = set()
        ok = True
        for mid in mids:
            hit_key: str | None = None
            for k, lst in groups.items():
                if any(int(x.mod_id) == mid for x in lst):
                    hit_key = k
                    break
            if hit_key is None:
                ok = False
                break
            keys_set.add(hit_key)
        if not ok:
            continue
        if len(keys_set) < 2:
            continue

        def _merge_eligible_key(k: str) -> bool:
            if k.startswith("m::"):
                return True
            # 노트만 OR 힌트고 행이 1개뿐인 ``or::`` (곧 mandatory 취급)도 페어 병합 허용
            return k.startswith("or::") and len(groups[k]) == 1

        if not all(_merge_eligible_key(k) for k in keys_set):
            _hard_log(
                f"[OR DIAG] alternative_to: 컴포넌트 {sorted(mids)} 스킵 "
                f"(다항 OR 그룹 등 병합 불가 keys={sorted(keys_set)!r})"
            )
            continue
        _merge_mandatory_keys_to_alternative_to_or(
            groups, order_keys, keys_set, mids, order_index
        )


def _partition_links_heuristic(links: list[NexusDependencyLink]) -> list[tuple[str, Any]]:
    if not links:
        return []
    groups, order_keys = _initial_requirement_groups(links)
    _apply_alternative_to_pairing(groups, order_keys, links)
    _log_fnis_nemesis_groups(groups, order_keys)
    out: list[tuple[str, Any]] = []
    for key in order_keys:
        g = groups[key]
        if key.startswith("m::"):
            out.append(("mandatory", g[0].mod_id))
        elif len(g) >= 2:
            opts = [
                {
                    "label": x.name,
                    "id": int(x.mod_id),
                    "note": (x.note or "").strip(),
                }
                for x in g
            ]
            out.append(("or", opts))
        else:
            out.append(("mandatory", g[0].mod_id))
    return out


def _log_fnis_nemesis_groups(
    groups: dict[str, list[NexusDependencyLink]],
    order_keys: list[str],
) -> None:
    """
    FNIS(3038) / Nemesis(60033)가 같은 Requirements Note(OR 힌트)로 묶였는지 로그.

    OR 그룹은 노트에 pick one / either / or / 또는 등이 있을 때만 ``or::…`` 키로 합쳐지며,
    그룹 크기 ≥2일 때 ``or_branch_signal``이 발동한다.
    """
    present: set[int] = set()
    for lst in groups.values():
        for lk in lst:
            try:
                mid = int(lk.mod_id)
            except (TypeError, ValueError):
                continue
            if mid in _FNIS_NEMESIS_IDS:
                present.add(mid)
    if not present:
        return

    _hard_log(
        f"[OR DIAG] FNIS({FNIS_NEXUS_ID})/NEMESIS({NEMESIS_NEXUS_ID}) "
        f"중 이번 requirements 목록에 등장: {sorted(present)}"
    )
    for key in order_keys:
        lst = groups[key]
        try:
            mids = [int(x.mod_id) for x in lst]
        except (TypeError, ValueError):
            continue
        touch = sorted(_FNIS_NEMESIS_IDS.intersection(mids))
        if not touch:
            continue
        is_or_key = key.startswith("or::")
        branch_ready = is_or_key and len(lst) >= 2
        role = "OR 그룹(or_branch 후보)" if branch_ready else "mandatory 단독(m:: 또는 OR단독 1건)"
        _hard_log(
            f"[OR DIAG]   그룹 key={key!r} n={len(lst)} ids={mids} "
            f"FNIS/NEMESIS포함={touch} → {role}"
        )

    dual_or_keys = [
        key
        for key in order_keys
        if key.startswith("or::")
        and len(groups[key]) >= 2
        and _FNIS_NEMESIS_IDS.issubset({int(x.mod_id) for x in groups[key]})
    ]
    if dual_or_keys:
        _hard_log(
            f"[OR DIAG] FNIS+NEMESIS가 **동일 OR 노트 그룹**에 함께 있음 → "
            f"or_branch_signal 발동 예정 keys={dual_or_keys!r}"
        )
    elif present == _FNIS_NEMESIS_IDS:
        _hard_log(
            "[OR DIAG] 두 ID 모두 있으나 **한 OR 그룹에 같이 묶이지 않음** "
            "(Notes가 다르거나 OR 힌트 없음) → 각각 mandatory로 따로 스캔됨"
        )
    elif present:
        _hard_log(
            f"[OR DIAG] FNIS/NEMESIS 중 일부만 목록에 있음 ids={sorted(present)}"
        )


def _strip_tags_entities(raw: str) -> str:
    """Strip HTML tags and decode entities (stdlib only)."""
    t = re.sub(r"<[^<]+?>", "", raw)
    return html.unescape(t)


def _clean_require_note_html(raw_note: str) -> str:
    note = re.sub(r"<br\s*/?>", " ", raw_note or "", flags=re.IGNORECASE)
    note = re.sub(r"<[^<]+?>", "", note)
    return html.unescape(note).strip()


def _split_nexus_page_title(page_html: str) -> tuple[str | None, str | None]:
    """Return ``(raw_title, cleaned_mod_name)`` from ``<title>``; cleaned is None if unusable."""
    m = re.search(r"<title[^>]*>\s*([^<]{1,400})\s*</title>", page_html, re.I)
    if not m:
        return None, None
    raw_full = html.unescape(m.group(1)).strip()
    clean = raw_full
    clean = re.sub(r"\s+at\s+.+$", "", clean, flags=re.I).strip()
    clean = re.sub(r"\s*\|\s*Nexus Mods.*$", "", clean, flags=re.I).strip()
    if len(clean) < 2:
        return raw_full, None
    return raw_full, clean[:240]


def _extract_mod_title_from_nexus_html(page_html: str) -> str | None:
    """Best-effort mod display name from a Nexus mod page HTML (e.g. ``<title>``)."""
    _, cleaned = _split_nexus_page_title(page_html)
    return cleaned


def _title_without_nexus_suffix_pipe(raw_title: str) -> str:
    t = (raw_title or "").strip()
    return re.sub(r"\s*\|\s*Nexus Mods.*$", "", t, flags=re.I).strip()


def _is_external_nexus_listing_signal(
    raw_title: str | None,
    extracted_clean: str | None,
    game_domain: str,
) -> bool:
    """True if the page does not look like a normal Nexus mod listing (external tool / hub / scrape fail)."""
    if extracted_clean is None:
        return True
    if not (raw_title and str(raw_title).strip()):
        return True
    raw_cmp = _title_without_nexus_suffix_pipe(str(raw_title))
    dom = (game_domain or "").strip().lower() or "skyrimspecialedition"
    gen = _NEXUS_GENERIC_TITLE_BY_DOMAIN.get(dom)
    if gen and raw_cmp.casefold() == gen.casefold():
        return True
    marker = _NEXUS_AT_MARKER_BY_DOMAIN.get(dom)
    if marker:
        if marker.casefold() not in raw_cmp.casefold():
            return True
    else:
        if not re.search(r"\s+at\s+.+\s+nexus", raw_cmp, re.I):
            return True
    return False


def _parse_nexus_requirements_table(
    page_html: str,
    *,
    parent_mod_id: int,
    game_domain: str,
) -> list[NexusDependencyLink]:
    """Pure re + slicing: harvest mod ids from ``table-require-name`` cells (no bs4)."""
    chunk = slice_nexus_requirements_html_chunk(page_html)
    if not chunk:
        _hard_log(
            "[SCAN DIAG] \uacc4\uace1 \uc2ac\ub77c\uc774\uc2f1: \uad6c\uac04 \uc5c6\uc74c "
            "(Nexus requirements \ubbf8\ubc1c\uacac)"
        )
        return []
    _hard_log(
        f"[SCAN DIAG] \uacc4\uace1 \uc2ac\ub77c\uc774\uc2f1 \uc131\uacf5. \uad6c\uac04 \uae38\uc774: {len(chunk)}"
    )

    seen: set[int] = set()
    out: list[NexusDependencyLink] = []
    dom = (game_domain or "").strip().lower() or "skyrimspecialedition"

    row_matches = list(_ROW_REQUIRE_NAME_NOTES.finditer(chunk))
    if row_matches:
        for m in row_matches:
            mid = int(m.group(1))
            if mid <= 0 or mid == int(parent_mod_id) or mid in seen:
                continue
            seen.add(mid)
            raw_note = m.group(2) or ""
            note = _clean_require_note_html(raw_note)
            python_hard_log(
                f"[SCAN DBG] \ucd94\ucd9c \uc644\ub8cc - ID: {mid}, Note: '{note}'"
            )
            raw_span = m.group(0)
            name_cell_end = raw_span.lower().find("</td>")
            name_part = raw_span[:name_cell_end] if name_cell_end >= 0 else raw_span
            cleaned = " ".join(_strip_tags_entities(name_part).split())
            name = (cleaned[:240].strip() or f"Mod {mid}")[:200]
            url = nexus_mod_url(dom, mid)
            out.append(
                NexusDependencyLink(name=name, mod_id=mid, url=url, note=note or None)
            )
            if len(out) >= _MAX_RE_DEPS:
                break
        return out

    for m in _TD_REQUIRE_MOD_ID.finditer(chunk):
        raw_span = m.group(0)
        mid = int(m.group(1))
        if mid <= 0 or mid == int(parent_mod_id) or mid in seen:
            continue
        seen.add(mid)
        python_hard_log(f"[SCAN DBG] \ucd94\ucd9c \uc644\ub8cc - ID: {mid}, Note: ''")
        cleaned = " ".join(_strip_tags_entities(raw_span).split())
        name = (cleaned[:240].strip() or f"Mod {mid}")[:200]
        url = nexus_mod_url(dom, mid)
        out.append(NexusDependencyLink(name=name, mod_id=mid, url=url, note=None))
        if len(out) >= _MAX_RE_DEPS:
            break

    return out


def _kahn_toposort_install_order(
    prereqs_map: dict[int, Any],
    dfs_post_order_ids: list[int],
) -> list[int]:
    """
    Each mod ``u`` depends on direct prerequisites ``prereqs_map[u]`` (edges ``p -> u``).
    Among zero-in-degree choices, prefer earlier positions in ``dfs_post_order_ids`` (DFS post-order tie).
    """
    nodes = list(dict.fromkeys(int(x) for x in dfs_post_order_ids))
    node_set = set(nodes)
    tie_rank = {mid: i for i, mid in enumerate(dfs_post_order_ids)}
    adj: dict[int, list[int]] = {n: [] for n in node_set}
    in_deg = {n: 0 for n in node_set}
    for u in node_set:
        raw = prereqs_map.get(u, [])
        if not isinstance(raw, list):
            continue
        for p in raw:
            if isinstance(p, dict):
                try:
                    pi = int(p.get("id"))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    pi = int(p)
                except (TypeError, ValueError):
                    continue
            if pi not in node_set or pi == u:
                continue
            adj[pi].append(u)
            in_deg[u] += 1
    for pi in adj:
        adj[pi].sort()
    heap: list[tuple[int, int]] = []
    for n in node_set:
        if in_deg[n] == 0:
            heapq.heappush(heap, (tie_rank.get(n, 10**9), n))
    order: list[int] = []
    while heap:
        _, u = heapq.heappop(heap)
        order.append(u)
        for v in adj[u]:
            in_deg[v] -= 1
            if in_deg[v] == 0:
                heapq.heappush(heap, (tie_rank.get(v, 10**9), v))
    if len(order) < len(node_set):
        placed = set(order)
        for n in nodes:
            if n not in placed:
                order.append(n)
    return order


def fetch_requirements_links_for_mod(
    *,
    game_domain: str,
    mod_id: int,
    scrape_timeout: float,
) -> tuple[list[NexusDependencyLink], str | None, bool]:
    """
    Nexus 모드 페이지에서 Requirements 표만 수집(캐시 우선). DFS·가이드 없이 단독 사용 가능.

    Returns:
        (dependency_links, page_title_clean_or_none, is_external_listing)
    """
    from ..utils.cache_manager import get_wepawn_cache

    dom_lc = (game_domain or "").strip().lower() or "skyrimspecialedition"
    mid = int(mod_id)
    links: list[NexusDependencyLink] = []
    title_clean: str | None = None
    is_ext: bool = False

    core_bundle = get_core_requirements_bundle(dom_lc, mid)
    if core_bundle is not None:
        _hard_log(
            f"[CORE_DEP DBG] HIT core_dependencies.json - ID: {mid} / domain: {dom_lc}"
        )
        links, title_clean, is_ext = nexus_links_from_requirements_bundle(
            core_bundle, dom_lc
        )
        if not links:
            _hard_log(
                "[SCAN DIAG] re/\uacc4\uace1 \uc218\uc9d1 0\uac74 "
                "(\ud3ed\ubc31 \uc5c6\uc74c, \ube48 \ubaa9\ub85d)"
            )
        return links, title_clean, is_ext

    cache = get_wepawn_cache()
    bundle = cache.get_requirements_bundle(dom_lc, mid)
    if bundle is not None:
        _hard_log(
            f"[CACHE DBG] HIT! 넥서스 호출 우회 완료 - ID: {mid} / 유형: requirements"
        )
        links, title_clean, is_ext = nexus_links_from_requirements_bundle(bundle, dom_lc)
        if not links:
            _hard_log(
                "[SCAN DIAG] re/\uacc4\uace1 \uc218\uc9d1 0\uac74 "
                "(\ud3ed\ubc31 \uc5c6\uc74c, \ube48 \ubaa9\ub85d)"
            )
        return links, title_clean, is_ext

    _hard_log(f"[CACHE DBG] MISS! 네트워크 스크랩 진행 - ID: {mid}")
    url = build_nexus_mod_page_url(dom_lc, mid, tab="requirements")
    if url:
        fetched = fetch_nexus_mod_page_html(url, timeout=float(scrape_timeout))
        if fetched is not None:
            st, page_html = fetched
            if st == 200 and (page_html or "").strip():
                raw_title, extracted_clean = _split_nexus_page_title(page_html)
                is_ext = _is_external_nexus_listing_signal(
                    raw_title, extracted_clean, dom_lc
                )
                if is_ext:
                    _hard_log(
                        f"[SCAN DIAG] 외부 프로그램 판별: id={mid} title={raw_title!r}"
                    )
                elif extracted_clean:
                    title_clean = extracted_clean.strip()[:240]
                links = _parse_nexus_requirements_table(
                    page_html,
                    parent_mod_id=mid,
                    game_domain=dom_lc,
                )
                _hard_log(
                    f"[REQ_TAB] guide_scan_worker fetch_url={url!r} "
                    f"parsed_link_count={len(links)} "
                    f"mod_ids={[lk.mod_id for lk in links]} "
                    f"ussep_nexus_266={any(lk.mod_id == 266 for lk in links)}"
                )
                cache.set_requirements_bundle(
                    dom_lc,
                    mid,
                    rows=[
                        {
                            "name": lk.name,
                            "mod_id": lk.mod_id,
                            "note": lk.note or "",
                        }
                        for lk in links
                    ],
                    title_clean=extracted_clean.strip()[:240]
                    if extracted_clean
                    else None,
                    is_external=is_ext,
                )
    if not links:
        _hard_log(
            "[SCAN DIAG] re/\uacc4\uace1 \uc218\uc9d1 0\uac74 "
            "(\ud3ed\ubc31 \uc5c6\uc74c, \ube48 \ubaa9\ub85d)"
        )
    return links, title_clean, is_ext


class ScanWorker(QThread):
    """QThread worker: recursive prerequisite scan, page HTML valley parse + OR gate."""

    progress_signal = pyqtSignal(str, int)
    or_branch_signal = pyqtSignal(object)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str, bool)

    def __init__(
        self,
        *,
        target_mod_id: int,
        visited_mods: dict[int, Any],
        install_queue: list[dict[str, Any]],
        game_domain: str,
        scrape_timeout: float = 45.0,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._target_mod_id = int(target_mod_id)
        self._visited = visited_mods
        self._visited_ids: set[int] = set()
        for _k in self._visited.keys():
            try:
                self._visited_ids.add(int(_k))
            except (TypeError, ValueError):
                continue
        self._install_queue = install_queue
        self._mod_name_cache: dict[int, str] = {}
        self._mod_external_flags: dict[int, bool] = {}
        self._game_domain = (game_domain or "").strip().lower() or "skyrimspecialedition"
        self._scrape_timeout = float(scrape_timeout)
        self.wait_event = Event()
        self._or_chosen_index: int = 0
        self._or_abort_requested: bool = False

    def apply_or_choice(self, option_index: int) -> None:
        """UI: 0-based index into the last ``or_branch_signal`` ``options`` list; resumes the worker."""
        self._or_chosen_index = max(0, int(option_index))
        self.wait_event.set()

    def abort_or_branch(self) -> None:
        """UI: user dismissed OR selection — unblock the worker without choosing a default."""
        self._or_abort_requested = True
        self.wait_event.set()

    def run(self) -> None:
        try:
            self._recursive_scan(self._target_mod_id, depth=0)
            tie_ids = [
                int(it["id"])
                for it in self._install_queue
                if isinstance(it, dict) and it.get("id") is not None
            ]
            by_id: dict[int, dict[str, Any]] = {}
            for it in self._install_queue:
                if isinstance(it, dict) and it.get("id") is not None:
                    by_id[int(it["id"])] = it
            sorted_ids = _kahn_toposort_install_order(self._visited, tie_ids)
            self._install_queue.clear()
            self._install_queue.extend(by_id[i] for i in sorted_ids if i in by_id)
            snap = list(self._install_queue)
            parts: list[str] = []
            for i, it in enumerate(snap):
                if isinstance(it, dict):
                    parts.append(
                        f"{i}:id={it.get('id')!s},name={str(it.get('name') or '')!r}"
                        f",ext={it.get('is_external')!s}"
                    )
                else:
                    parts.append(f"{i}:{it!r}")
            _hard_log(
                "[SCAN DIAG] install_queue finished_ok order (count="
                f"{len(snap)}): "
                + " ; ".join(parts)
            )
            graph_snap: dict[int, list[dict[str, Any]]] = {}
            for mid, ch in self._visited.items():
                try:
                    ik = int(mid)
                except (TypeError, ValueError):
                    continue
                if isinstance(ch, list):
                    kids: list[dict[str, Any]] = []
                    for x in ch:
                        if isinstance(x, dict) and x.get("id") is not None:
                            try:
                                cid = int(x["id"])
                            except (TypeError, ValueError):
                                continue
                            if cid <= 0:
                                continue
                            kids.append(
                                {
                                    "id": cid,
                                    "note": str(x.get("note") or "").strip(),
                                }
                            )
                        else:
                            try:
                                cid = int(x)
                            except (TypeError, ValueError):
                                continue
                            if cid > 0:
                                kids.append({"id": cid, "note": ""})
                    graph_snap[ik] = kids
                else:
                    graph_snap[ik] = []
            self.finished_ok.emit({"queue": snap, "graph": graph_snap})
        except GuideScanAborted:
            self.failed.emit("대안 모드 선택이 취소되어 의존성 분석을 중단했습니다.", False)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}", False)

    def _recursive_scan(self, mod_id: int, depth: int = 0) -> None:
        try:
            mid = int(mod_id)
        except (TypeError, ValueError):
            return
        if mid in self._visited_ids:
            return
        if depth > _MAX_DEPENDENCY_RECURSION_DEPTH:
            return

        self._visited_ids.add(mid)
        self._visited[mid] = []

        _hard_log(
            f"[SCAN DIAG] \uc9c4\uc785: ID {mid} (Depth: {depth}, Queue: {len(self._install_queue)})"
        )

        mod_name = self._resolve_mod_name(mid)
        self.progress_signal.emit(mod_name, mid)

        children = self._collect_prereq_child_ids(mid, depth)
        children_kept: list[dict[str, Any]] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            child_id = int(child["id"])
            note = str(child.get("note", "") or "").strip()
            if self._keep_prereq_child_after_vr_ae_filter(
                child_id, mid, title="", note=note
            ):
                children_kept.append(child)
        self._visited[mid] = children_kept
        for child in children_kept:
            try:
                cid = int(child["id"])
            except (TypeError, ValueError):
                continue
            self._recursive_scan(cid, depth + 1)

        name = (self._resolve_mod_name(mid) or "").strip() or f"Mod {mid}"
        is_ext = bool(self._mod_external_flags.get(mid, True))
        self._install_queue.append(
            {
                "id": mid,
                "name": name[:500],
                "is_external": is_ext,
            }
        )

    def _resolve_mod_name(self, mod_id: int) -> str:
        mid = int(mod_id)
        cached = self._mod_name_cache.get(mid)
        if cached and str(cached).strip():
            return str(cached).strip()[:500]
        return f"Mod {mid}"

    def _keep_prereq_child_after_vr_ae_filter(
        self,
        child_id: int,
        parent_mod_id: int,
        title: str,
        note: str = "",
    ) -> bool:
        """Exclude VR/AE-only style listings from nested scans; never drop target or its direct deps."""
        if int(child_id) == int(self._target_mod_id):
            return True
        if int(parent_mod_id) == int(self._target_mod_id):
            return True
        name = (title or "").strip() or self._resolve_mod_name(child_id)
        if python_VR_AE_FILTER.search(name) or _is_note_vr_only(note):
            _hard_log(
                f"[SCAN DBG] \ub8f8 \uae30\ubc18 \uac00\uc9c0\uce58\uae30 \ubc1c\ub3d9! "
                f"ID: {child_id} / name: {name} / Note: '{note}'"
            )
            return False
        return True

    def _partition_scraped_links(
        self, links: list[NexusDependencyLink]
    ) -> list[tuple[str, Any]]:
        """Split into mandatory mod ids and OR option groups (heuristic from shared notes)."""
        return _partition_links_heuristic(links)

    def _collect_prereq_child_ids(self, mod_id: int, depth: int) -> list[dict[str, Any]]:
        _hard_log(f"[SCAN DIAG] _collect_prereq_child_ids 진입: mod_id={mod_id}")
        _hard_log(f"[SCAN DIAG] Analyzing Depth {depth}: Mod {mod_id}")
        self.progress_signal.emit(
            f"\ubaa8\ub4dc ID [{mod_id}]\uc758 \uc758\uc874\uc131 \ubd84\uc11d \uc911...",
            mod_id,
        )

        links: list[NexusDependencyLink] = []
        try:
            links, title_clean, is_ext = fetch_requirements_links_for_mod(
                game_domain=self._game_domain,
                mod_id=mod_id,
                scrape_timeout=self._scrape_timeout,
            )
            self._mod_external_flags[int(mod_id)] = is_ext
            if is_ext:
                _hard_log(f"[SCAN DIAG] 외부 프로그램 판별(통합): id={mod_id}")
            elif title_clean:
                self._mod_name_cache[int(mod_id)] = title_clean
        except Exception as e:
            _hard_log(f"[SCAN DIAG] \uc2a4\ud06c\ub798\ud551 \uc911 \uc608\uc678 \ubc1c\uc0dd: {e}")
            raise

        _hard_log(f"[SCAN DIAG] scrape 결과: {len(links)}건")

        if mod_id == _DIAG_XP32_MAX_SKEL_ID:
            _hard_log(
                f"[REQ1988] XP32 Extended Skeleton (nexus {mod_id}) requirements 파싱 "
                f"— 총 {len(links)}행"
            )
            for lk in links:
                _hard_log(
                    f"[REQ1988]   dep mod_id={lk.mod_id} name={lk.name!r} "
                    f"note={((lk.note or '')[:400])!r}"
                )

        self.progress_signal.emit(
            f"\ubaa8\ub4dc ID [{mod_id}] \uc758\uc874\uc131 \uc218\uc9d1 \uc644\ub8cc ({len(links)}\uac74)",
            mod_id,
        )

        id_to_name: dict[int, str] = {}
        id_to_note: dict[int, str] = {}
        for link in links:
            try:
                mid = int(link.mod_id)
            except (TypeError, ValueError):
                continue
            nm = (link.name or "").strip()
            if nm and mid not in id_to_name:
                id_to_name[mid] = nm
            id_to_note[mid] = (link.note or "").strip()

        result_children: list[dict[str, Any]] = []
        for kind, payload in self._partition_scraped_links(links):
            if kind == "mandatory":
                cid = int(payload)
                result_children.append(
                    {"id": cid, "note": id_to_note.get(cid, "")}
                )
                continue
            options: list[dict[str, Any]] = payload
            if not options:
                continue
            self.wait_event.clear()
            self._or_chosen_index = 0
            self._or_abort_requested = False
            opt_ids = [
                int(o["id"])
                for o in options
                if isinstance(o, dict) and o.get("id") is not None
            ]
            if _FNIS_NEMESIS_IDS.intersection(opt_ids):
                _hard_log(
                    f"[OR DIAG] or_branch_signal **발동** (FNIS/NEMESIS 옵션 포함): "
                    f"option_ids={opt_ids} labels="
                    f"{[str(o.get('label') or '')[:72] for o in options if isinstance(o, dict)]}"
                )
            self.or_branch_signal.emit(options)
            if not self.wait_event.wait(timeout=60):
                _hard_log("[SCAN DIAG] User response timeout - Defaulting to option 0")
                idx_choice = 0
            elif self._or_abort_requested:
                raise GuideScanAborted()
            else:
                idx_choice = min(self._or_chosen_index, len(options) - 1)
            chosen = options[idx_choice]
            chosen_id = int(chosen["id"])
            result_children.append(
                {
                    "id": chosen_id,
                    "note": str(chosen.get("note") or "").strip(),
                }
            )

        return result_children
