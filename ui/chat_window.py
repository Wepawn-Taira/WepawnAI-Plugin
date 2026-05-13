"""
Chat-style dockable window for WepawnAI, parented under Mod Organizer 2.
"""

from __future__ import annotations

import configparser
import html
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import mobase
from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QShowEvent, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..i18n import (
    LOCALE_COMBO_ENTRIES,
    normalize_ui_locale_code,
    persist_selected_language,
    resolve_initial_locale_code,
    set_locale,
    tr,
)
from ..nexus.dependencies import NexusAPIError, fetch_mod_file_dependencies
from ..ai.llm_client import _diag
from ..ai.nexus_scraper import build_nexus_mod_page_url
from ..utils.game_info import get_current_game_info
from ..utils.mo2_diagnostics import collect_mo2_physical_diagnostics_text
from ..utils.fomod_option_intel import (
    FOMOD_EXTRACT_ERROR_PREFIXES,
    fomod_extract_indicates_moduleconfig_wizard,
)
from ..utils.fomod_parser import NO_FOMOD_MESSAGE, extract_fomod_xml
from ..utils.hard_log import _hard_log, python_hard_log
from ..utils.mo2_nemesis_launch import (
    FNIS_NEXUS_ID,
    NEMESIS_NEXUS_ID,
    launch_nemesis_with_organizer,
)
from ..game_context import (
    game_folder_short_label_for_organizer,
    loader_path_exists,
    script_extender_loader_absolute_path,
    script_extender_loader_basename_for_organizer,
    script_extender_nexus_mod_id_for_organizer,
    script_extender_prereq_label_for_organizer,
    should_inject_script_extender_prereq,
)
from ..utils.nexus_api import read_nexus_api_key_from_mo2
from .components.alternative_dialog import AlternativeSelectorDialog
from .counselor_worker import CounselorWorker, format_counselor_reply_html_body
from .guide_scan_worker import ScanWorker
from .guide_step_worker import GuideStepWorker
from .id_search_worker import (
    IdSearchWorker,
    WEPAWN_GUIDE_PROMPT_FOOTER_MARKER,
    _label_install_satisfied,
    _re_split_or,
    _strip_priority_markers_from_label,
    filter_prereq_tree_unsatisfied,
    flatten_prereq_install_order,
    is_script_extender_guide_nexus_id,
    normalize_prereq_item_dict,
    script_extender_step_loader_installed_in_game_dir,
    split_prereq_line_meta,
    split_prereq_nexus_suffix,
)
from .tier_worker import TierAnalysisWorker

# Nexus site IDs are positive integers; reject garbage / overflow from bad metadata.
_MAX_NEXUS_MOD_ID = 2_000_000_000

def _main_mod_nexus_id_from_ctx(ctx: dict | None) -> int:
    if not isinstance(ctx, dict):
        return 0
    try:
        v = int(ctx.get("main_mod_id") or 0)
    except (TypeError, ValueError):
        return 0
    return v if 0 < v <= _MAX_NEXUS_MOD_ID else 0


def _prereq_tree_contains_nexus_id(nodes: list[Any], target: int) -> bool:
    for n in nodes:
        if not isinstance(n, dict):
            continue
        try:
            nid = int(n.get("nexus_mod_id") or 0)
        except (TypeError, ValueError):
            nid = 0
        if nid == target:
            return True
        ch = n.get("children")
        if isinstance(ch, list) and _prereq_tree_contains_nexus_id(
            [x for x in ch if isinstance(x, dict)], target
        ):
            return True
    return False


def inject_script_extender_prereq_if_missing(
    ctx: dict[str, Any],
    organizer: mobase.IOrganizer | None,
) -> None:
    """
    For SSE / Enderal SE / FO4, prepend the script extender (SKSE64 / F4SE) Nexus step
    when the dependency scan/LLM tree does not already include it.
    """
    if not isinstance(ctx, dict):
        return
    if not should_inject_script_extender_prereq(organizer):
        return
    se_id = script_extender_nexus_mod_id_for_organizer(organizer)
    if se_id <= 0:
        return
    try:
        main_mid = int(ctx.get("main_mod_id") or 0)
    except (TypeError, ValueError):
        main_mid = 0
    if main_mid == se_id:
        return
    raw = ctx.get("pending_prereq_items")
    if not isinstance(raw, list):
        return
    tree = [normalize_prereq_item_dict(x) for x in raw if isinstance(x, dict)]
    if _prereq_tree_contains_nexus_id(tree, se_id):
        _hard_log(
            f"[SE INJECT] skip: nexus {se_id} already present in pending_prereq_items"
        )
        return
    label = script_extender_prereq_label_for_organizer(organizer)
    head = normalize_prereq_item_dict(
        {
            "label": label,
            "nexus_mod_id": se_id,
            "children": [],
            "priority": "MANDATORY",
        }
    )
    ctx["pending_prereq_items"] = [head] + tree
    _hard_log(
        f"[SE INJECT] prepended {label!r} (nexus {se_id}) as first mandatory prereq"
    )


# VR-only Nexus mod IDs to strip from flat SE/AE guide scan results (e.g. VR Address Library).
python_VR_ID_BLOCKLIST = frozenset({58101})
_VR_ID_BLOCKLIST = python_VR_ID_BLOCKLIST

# 상담사 대화 히스토리 최대 길이 (10턴 = user+assistant 20개)
_COUNSELOR_HISTORY_MAX_MESSAGES = 20

_GUIDE_POSITIVE_PHRASES: tuple[str, ...] = (
    "응",
    "어",
    "예",
    "네",
    "그래",
    "좋아",
    "그러자",
    "yes",
    "ok",
    "오케이",
    "ㅇ",
    "ㅇㅇ",
    "고",
    "가자",
    "해줘",
    "진행",
)
_GUIDE_NEGATIVE_PHRASES: tuple[str, ...] = ("아니", "노", "괜찮", "됐어", "no")

_GUIDE_INSTALL_DONE_PHRASES: tuple[str, ...] = (
    "설치했어",
    "완료",
    "됐어",
    "다음",
    "넘어가",
    "했어",
    "설치됨",
    "완료됨",
    "done",
    "오케",
    "넘어",
    "다했어",
)


def _guide_text_has_install_done(text: str) -> bool:
    t = text or ""
    tl = t.casefold()
    for p in _GUIDE_INSTALL_DONE_PHRASES:
        if not p:
            continue
        if p.isascii():
            if p.casefold() in tl:
                return True
        elif p in t:
            return True
    return False


def _parse_or_guide_segments(raw_label: str) -> list[tuple[str, int | None]]:
    """Split a label on `` 또는 `` into (display name, optional nexus id) pairs."""
    base = _strip_priority_markers_from_label(str(raw_label or "").strip())
    if tr("guide.or_separator") not in base:
        return []
    parts = [p for p in _re_split_or().split(base) if p.strip()]
    segs: list[tuple[str, int | None]] = []
    for seg in parts:
        s = seg.strip()
        if not s:
            continue
        clean, nid, _ = split_prereq_line_meta(s)
        display = (clean or "").strip() or s
        nid_i: int | None = int(nid) if nid is not None and nid > 0 else None
        if nid_i is None:
            m_nid = re.search(r"\[nexus:(\d+)\]", s, flags=re.IGNORECASE)
            if m_nid:
                try:
                    nid_i = int(m_nid.group(1), 10)
                except ValueError:
                    nid_i = None
                if nid_i is not None:
                    display = re.sub(
                        r"\s*\[nexus:\d+\]\s*",
                        " ",
                        s,
                        flags=re.IGNORECASE,
                    )
                    display = _strip_priority_markers_from_label(display)
                    display = re.sub(r"\s+", " ", display).strip() or s
        segs.append((display, nid_i))
    return segs if len(segs) >= 2 else []


def _or_step_summary_title(segments: list[tuple[str, int | None]]) -> str:
    return " / ".join(d for d, _ in segments if d) or tr("guide.name_none")


def _build_or_alternatives_links_html(
    game_domain: str, segments: list[tuple[str, int | None]]
) -> str:
    pieces: list[str] = []
    for disp, mid in segments:
        safe_disp = html.escape(disp, quote=False)
        if mid is not None and mid > 0:
            mod_url = (
                build_nexus_mod_page_url(game_domain.strip(), mid) or ""
            ).strip()
            if not mod_url:
                _gd = (game_domain or "").strip().strip("/").lower()
                if _gd:
                    mod_url = f"https://www.nexusmods.com/{_gd}/mods/{mid}"
            if mod_url:
                _su = html.escape(mod_url, quote=True)
                _uv = html.escape(mod_url, quote=False)
                pieces.append(
                    '<span style="display:inline-flex;flex-direction:column;gap:3px;'
                    'vertical-align:top">'
                    f"<b>{safe_disp}</b>"
                    f'<a href="{_su}" style="color:#1565c0;">{_uv}</a></span>'
                )
                continue
        pieces.append(
            '<span style="display:inline-block;vertical-align:top">'
            f"<b>{safe_disp}</b></span>"
        )
    inner = "".join(pieces)
    _or_hint = tr("guide.or_alternatives_hint")
    return (
        '<div style="display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-start;'
        'margin:10px 0">'
        f"{inner}</div>"
        f"<p style='margin-top:6px'>{html.escape(_or_hint, quote=False)}</p>"
    )


def _guide_text_has_negative(text: str) -> bool:
    t = text or ""
    tl = t.casefold()
    for p in _GUIDE_NEGATIVE_PHRASES:
        if not p:
            continue
        if p.isascii():
            if p.casefold() in tl:
                return True
        elif p in t:
            return True
    return False


def _guide_text_has_positive(text: str) -> bool:
    t = text or ""
    tl = t.casefold()
    for p in _GUIDE_POSITIVE_PHRASES:
        if not p:
            continue
        if p.isascii():
            if p.casefold() in tl:
                return True
        elif p in t:
            return True
    return False


def _nexus_search_url_for_query(game_domain: str, query: str) -> str:
    dom = (game_domain or "").strip().strip("/").lower() or "skyrimspecialedition"
    enc = quote_plus(query.strip(), safe="")
    return f"https://www.nexusmods.com/{dom}/search/?gsearch={enc}"


def _nexus_mod_page_link_html_and_url(
    game_domain: str, nexus_mod_id_raw: object
) -> tuple[str, str]:
    """Resolve Nexus mod page URL and anchor HTML (same as guide ``download_from_link``)."""
    try:
        mid = int(nexus_mod_id_raw) if nexus_mod_id_raw is not None else 0
    except (TypeError, ValueError):
        mid = 0
    if mid <= 0:
        return ("", "")
    mod_url = (
        build_nexus_mod_page_url(game_domain.strip(), mid) or ""
    ).strip()
    if not mod_url:
        _gd = (game_domain or "").strip().strip("/").lower()
        if _gd:
            mod_url = f"https://www.nexusmods.com/{_gd}/mods/{mid}"
    if not mod_url:
        return ("", "")
    _su = html.escape(mod_url, quote=True)
    _uv = html.escape(mod_url, quote=False)
    link_html = f'<a href="{_su}" style="color:#1565c0;">{_uv}</a>'
    return (link_html, mod_url)


def _work_queue_index_for_nexus_id(
    work_queue: list[dict[str, Any]],
    mod_id: int,
) -> int | None:
    mid = int(mod_id)
    python_hard_log(
        f"[ROUTER DBG] work_queue_index 조회: child_id={mid} queue="
        f"{[x.get('nexus_mod_id') or x.get('id') for x in work_queue if isinstance(x, dict)]}"
    )
    for i, row in enumerate(work_queue):
        if not isinstance(row, dict):
            continue
        raw = row.get("nexus_mod_id")
        try:
            rid = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            rid = 0
        if rid == mid:
            return i
    return None


def _dedupe_guide_work_queue_by_nexus_id(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    ``nexus_mod_id`` 기준으로 앞선 항목만 남기고 중복을 제거한다.
    순서는 ``list(dict.fromkeys(nexus_ids))`` 와 동일하게 첫 등장 순을 유지한다.
    ID가 없거나 0 이하인 행은 건너뛰지 않는다.
    """
    seen: dict[int, None] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        raw = row.get("nexus_mod_id")
        try:
            nid = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            nid = 0
        if nid <= 0:
            out.append(row)
            continue
        if nid in seen:
            continue
        seen[nid] = None
        out.append(row)
    return out


def _coerce_scan_graph_node(x: object) -> dict[str, Any] | None:
    """Normalize one dependency edge to ``id`` + ``note`` dict."""
    if isinstance(x, dict):
        raw_id = x.get("id")
        if isinstance(raw_id, dict):
            return None
        try:
            i = int(raw_id)
        except (TypeError, ValueError):
            return None
        if i <= 0:
            return None
        return {"id": i, "note": str(x.get("note") or "").strip()}
    try:
        i = int(x)
    except (TypeError, ValueError):
        return None
    if i <= 0:
        return None
    return {"id": i, "note": ""}


def _coerce_scan_dependency_graph(
    raw: object,
) -> dict[int, list[dict[str, Any]]]:
    """Build SSOT graph: parent mod id -> list of id/note dicts. Legacy int lists migrate."""
    out: dict[int, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            ik = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, list):
            out[ik] = []
            continue
        kids: list[dict[str, Any]] = []
        for x in v:
            node = _coerce_scan_graph_node(x)
            if node is not None:
                kids.append(node)
        out[ik] = kids
    return out


def _rewrite_fnis_to_nemesis_dependency_graph(
    graph: dict[int, list[dict[str, Any]]],
) -> None:
    """스캔 그래프의 FNIS(3038) 노드를 NEMESIS(60033)로 합친다."""
    fnis_id = FNIS_NEXUS_ID
    nem_id = NEMESIS_NEXUS_ID
    if fnis_id in graph:
        moved = graph.pop(fnis_id)
        acc = list(graph.get(nem_id, []))
        seen = {int(x.get("id") or 0) for x in acc}
        for c in moved:
            cid = int(c.get("id") or 0)
            if cid and cid not in seen:
                seen.add(cid)
                acc.append(dict(c))
        graph[nem_id] = acc
    for k, kids in list(graph.items()):
        new_kids: list[dict[str, Any]] = []
        seen_row: set[int] = set()
        for c in kids:
            d = dict(c)
            cid = int(d.get("id") or 0)
            if cid == fnis_id:
                d["id"] = nem_id
                cid = nem_id
            if cid <= 0 or cid in seen_row:
                continue
            seen_row.add(cid)
            new_kids.append(d)
        graph[k] = new_kids


def _dependency_graph_child_ids(nodes: object) -> list[int]:
    """Extract prerequisite mod ids from a graph adjacency list (dict or legacy int)."""
    if not isinstance(nodes, list):
        return []
    ids: list[int] = []
    for x in nodes:
        coerced = _coerce_scan_graph_node(x)
        if coerced is not None:
            ids.append(int(coerced["id"]))
    return ids


def _read_game_version_for_counselor(organizer: mobase.IOrganizer | None) -> tuple[str, str]:
    """
    (게임 이름, 버전 문자열).
    ``get_current_game_info``의 실행 파일(PE) 버전을 우선하고, 비어 있으면
    ``managedGame().version().displayString()`` 을 사용한다.
    """
    name, exe_ver = get_current_game_info(organizer)
    name = (name or "").strip()
    v = (exe_ver or "").strip()
    if v:
        return (name, v)
    if organizer is None:
        return (name, "")
    try:
        mg = organizer.managedGame()
        if mg is None:
            return (name, "")
        ver_obj = getattr(mg, "version", None)
        if ver_obj is None:
            return (name, "")
        vo = ver_obj() if callable(ver_obj) else ver_obj
        if vo is None:
            return (name, "")
        disp = getattr(vo, "displayString", None)
        if disp is not None:
            s = disp() if callable(disp) else disp
            return (name, str(s or "").strip())
        return (name, "")
    except Exception:
        return (name, "")


def _nexus_id_from_meta_ini(mod: mobase.IModInterface) -> int | None:
    """Read ``modid`` from the mod folder's ``meta.ini`` if present."""
    path = Path(mod.absolutePath()) / "meta.ini"
    if not path.is_file():
        return None
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(path, encoding="utf-8")
    except configparser.Error:
        return None
    for section in cfg.sections():
        if not cfg.has_option(section, "modid"):
            continue
        try:
            value = int(str(cfg.get(section, "modid")).strip(), 10)
        except ValueError:
            continue
        if 0 < value <= _MAX_NEXUS_MOD_ID:
            return value
    return None


def _parse_nexus_id_scalar(raw: object) -> int:
    """Normalize MO2 / Shiboken ``nexusId()`` into a bounded positive int, or 0."""
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw if 0 < raw <= _MAX_NEXUS_MOD_ID else 0
    if isinstance(raw, float):
        if raw != raw:
            return 0
        i = int(raw)
        return i if 0 < i <= _MAX_NEXUS_MOD_ID else 0
    s = str(raw).strip()
    if not s or s.lower() in ("none", "null", "nan", "-1", "0"):
        return 0
    try:
        i = int(float(s))
    except (TypeError, ValueError):
        return 0
    return i if 0 < i <= _MAX_NEXUS_MOD_ID else 0


def _coerce_positive_nexus_id(mod: mobase.IModInterface) -> int:
    try:
        n = _parse_nexus_id_scalar(mod.nexusId())
    except Exception:
        n = 0
    if n > 0:
        return n
    meta = _nexus_id_from_meta_ini(mod)
    return meta if meta is not None and meta > 0 else 0


def _iter_mod_internal_names(ml: mobase.IModList):
    for method_name in ("allModsByProfilePriority", "allMods"):
        fn = getattr(ml, method_name, None)
        if fn is None or not callable(fn):
            continue
        try:
            raw = fn()
        except Exception:
            continue
        if not raw:
            continue
        for internal in raw:
            if internal is not None:
                yield internal
        return


def _entry_to_mod(ml: mobase.IModList, entry: object) -> mobase.IModInterface | None:
    """
    Resolve GUI / API selection entry to ``IModInterface``.

    MO2 2.5.2 often exposes **display names** from the mod list tree; ``getMod()`` expects the
    **internal (folder) name**, so we fall back to scanning ``allMods*`` when direct lookup fails.
    """
    if isinstance(entry, str):
        text = entry.strip()
        if not text:
            return None
        m = ml.getMod(text)
        if m is not None:
            return m
        for internal in _iter_mod_internal_names(ml):
            try:
                if ml.displayName(internal) == text:
                    return ml.getMod(internal)
            except Exception:
                continue
        tf = text.casefold()
        for internal in _iter_mod_internal_names(ml):
            try:
                if str(ml.displayName(internal)).strip().casefold() == tf:
                    return ml.getMod(internal)
            except Exception:
                continue
        return None
    # Shiboken wrappers may not pass isinstance(..., IModInterface); duck-type.
    if hasattr(entry, "nexusId") and hasattr(entry, "name") and hasattr(entry, "absolutePath"):
        return entry  # type: ignore[return-value]
    return None


def _gui_selected_mod_names_from_mod_tree() -> list[str]:
    """
    MO2 2.5.2: ``IModList.selectedMods()`` is absent. Read selection from the main mod list ``QTreeView``.
    """
    app = QApplication.instance()
    if app is None:
        return []

    main_window: QWidget | None = None
    for w in app.topLevelWidgets():
        if w.objectName() == "MainWindow":
            main_window = w
            break
    if main_window is None:
        return []

    tree = main_window.findChild(QTreeView, "modList")
    if tree is None:
        return []

    sm = tree.selectionModel()
    if sm is None:
        return []

    names: list[str] = []
    for idx in sm.selectedRows(0):
        data = idx.data(Qt.ItemDataRole.DisplayRole)
        if data is None:
            continue
        text = str(data).strip()
        if text:
            names.append(text)
    return names


def _selection_entries_from_modlist_api(ml: mobase.IModList) -> list[object] | None:
    """
    Returns a non-empty list from ``selectedMods()`` when that API exists and has a selection;
    ``None`` if the API is missing or unusable; ``[]`` if it exists but the selection is empty.
    """
    selected_accessor = getattr(ml, "selectedMods", None)
    if selected_accessor is None:
        return None

    raw_selected = selected_accessor() if callable(selected_accessor) else selected_accessor
    if raw_selected is None:
        return []

    try:
        return list(raw_selected)
    except TypeError:
        return []


def _mod_list_selection_entries(organizer: mobase.IOrganizer) -> list[object]:
    ml = organizer.modList()
    api_result = _selection_entries_from_modlist_api(ml)
    if api_result is not None and len(api_result) > 0:
        return api_result
    return list(_gui_selected_mod_names_from_mod_tree())


def _mod_state_active_flag() -> int:
    for attr in ("ACTIVE", "active"):
        if hasattr(mobase.ModState, attr):
            return int(getattr(mobase.ModState, attr))
    return 2


def _active_mod_display_names(organizer: mobase.IOrganizer) -> list[str]:
    ml = organizer.modList()
    flag = _mod_state_active_flag()
    names: list[str] = []
    for internal in ml.allModsByProfilePriority():
        try:
            st = ml.state(internal)
        except Exception:
            continue
        try:
            if int(st) & flag:
                names.append(ml.displayName(internal))
        except Exception:
            continue
    return names


def _active_mod_nexus_ids(organizer: mobase.IOrganizer) -> set[int]:
    ml = organizer.modList()
    flag = _mod_state_active_flag()
    ids: set[int] = set()
    for internal in ml.allModsByProfilePriority():
        try:
            st = ml.state(internal)
            if not (int(st) & flag):
                continue
            mod = ml.getMod(internal)
            if mod is None:
                continue
            nid = _coerce_positive_nexus_id(mod)
            if nid > 0:
                ids.add(nid)
        except Exception:
            continue
    return ids


_RE_MASTER_STATE_MISSING = re.compile(
    r"\bmaster\s+(.+?)(?:\.(?:esm|esp|esl))\b.*STATE_MISSING",
    re.I | re.DOTALL,
)
_RE_MASTER_NOT_IN_LIST = re.compile(
    r"required master not in MO2 plugin list\s*[—\-–]+\s*(?P<master>.+?)(?:\.(?:esm|esp|esl))\b",
    re.IGNORECASE,
)
_RE_PLUGIN_ROW_STATE_MISSING = re.compile(
    r"^\s*-\s+(.+?)(?:\.(?:esm|esp|esl))\s+\|\s+plugin\s+state=\s*STATE_MISSING\b",
    re.I | re.MULTILINE,
)


def extract_missing_master_filenames_from_mo2_context(mo2_text: str) -> list[str]:
    """
    Parse ``collect_mo2_physical_diagnostics_text`` output: master dependency issues,
    ``required master not in MO2 plugin list``, and plugin rows with ``STATE_MISSING``.

    Captured names omit ``.esp`` / ``.esm`` / ``.esl`` (used as Nexus search query text).
    """
    text = mo2_text or ""
    seen: set[str] = set()
    ordered: list[str] = []

    def _pull_name(m: re.Match) -> str:
        gd = m.groupdict()
        raw = gd["master"] if gd.get("master") is not None else m.group(1)
        return raw.strip().lower()

    for rx in (_RE_MASTER_STATE_MISSING, _RE_MASTER_NOT_IN_LIST, _RE_PLUGIN_ROW_STATE_MISSING):
        for m in rx.finditer(text):
            fn = _pull_name(m)
            if not fn:
                continue
            if fn in seen:
                continue
            seen.add(fn)
            ordered.append(fn)
    return ordered


def html_forced_nexus_master_search_links(filenames: list[str], nexus_game_domain: str) -> str:
    """Append block: ▶ [원본 찾기] + Nexus search links (``filenames`` = no ``.esp``/``.esm``/``.esl``)."""
    dom = (nexus_game_domain or "").strip().strip("/").lower() or "skyrimspecialedition"
    if not filenames:
        return ""
    parts: list[str] = ["<br/><br/>"]
    heading = html.escape(tr("chat.master_search_heading"), quote=False)
    suf = html.escape(tr("chat.master_search_link_suffix"), quote=False)
    for fn in filenames:
        enc = quote_plus(fn, safe="")
        url = f"https://www.nexusmods.com/{dom}/search/?gsearch={enc}"
        _diag(f"FORCED_NEXUS_SEARCH_SYNTH pure_mod_name={fn!r} url={url!r}")
        safe_href = html.escape(url, quote=True)
        safe_name = html.escape(fn, quote=False)
        parts.append(
            f'<b>▶ [{heading}]</b> <a href="{safe_href}" style="color:#1565c0;">'
            f"{safe_name} {suf}</a><br/>"
        )
    return "".join(parts)


def _tier_grade_span_only(tier: str) -> tuple[str, str]:
    """
    Colored grade word only (no '타입:' here — the header supplies that prefix once).
    Returns (html_fragment, diag_snippet).
    """
    t = str(tier).strip()
    if t == "Red":
        frag = (
            '<span style="color:red;font-weight:bold;">'
            f"{html.escape(tr('tiers.grade_warn'), quote=False)}</span>"
        )
        return frag, "타입:+경고(single_prefix)"
    if t == "Yellow":
        frag = (
            '<span style="color:orange;font-weight:bold;">'
            f"{html.escape(tr('tiers.grade_caution'), quote=False)}</span>"
        )
        return frag, "타입:+주의(single_prefix)"
    if t == "Green":
        frag = (
            '<span style="color:green;font-weight:bold;">'
            f"{html.escape(tr('tiers.grade_safe'), quote=False)}</span>"
        )
        return frag, "타입:+안전(single_prefix)"
    safe = html.escape(t, quote=False)
    frag = f'<span style="color:#1565c0;font-weight:bold;">{safe}</span>'
    return frag, frag


def _format_tier_reason_html(reason: str) -> str:
    """
    Escape model text; map ``Tier: Red``-style phrases to colored grade words only.
    (Nexus links are not parsed from AI output — added separately from MO2 diagnostics.)
    """
    s = str(reason or "")
    s = re.sub(r"(?i)\bTier\s*:\s*Red\b", "__WEPAWN_TRED__", s)
    s = re.sub(r"(?i)\bTier\s*:\s*Yellow\b", "__WEPAWN_TYEL__", s)
    s = re.sub(r"(?i)\bTier\s*:\s*Green\b", "__WEPAWN_TGRN__", s)
    tkw = tr("chat.tier_type_keyword").strip()
    if tkw:
        esc_t = re.escape(tkw)
        s = re.sub(rf"(?i){esc_t}\s*:\s*__WEPAWN_TRED__", "__WEPAWN_TRED__", s)
        s = re.sub(rf"(?i){esc_t}\s*:\s*__WEPAWN_TYEL__", "__WEPAWN_TYEL__", s)
        s = re.sub(rf"(?i){esc_t}\s*:\s*__WEPAWN_TGRN__", "__WEPAWN_TGRN__", s)
    s = html.escape(s, quote=False).replace("\n", "<br/>")
    s = s.replace(
        "__WEPAWN_TRED__",
        '<span style="color:red;font-weight:bold;">'
        f"{html.escape(tr('tiers.grade_warn'), quote=False)}</span>",
    )
    s = s.replace(
        "__WEPAWN_TYEL__",
        '<span style="color:orange;font-weight:bold;">'
        f"{html.escape(tr('tiers.grade_caution'), quote=False)}</span>",
    )
    s = s.replace(
        "__WEPAWN_TGRN__",
        '<span style="color:green;font-weight:bold;">'
        f"{html.escape(tr('tiers.grade_safe'), quote=False)}</span>",
    )
    return s


# QTextBrowser 인라인 «버튼»: anchorClicked에서 처리 (http(s)는 기본 외부 브라우저 동작 유지).
_NEMESIS_TRANSCRIPT_LAUNCH_URL = "wepawn://nemesis-launch"
_SKSE_TRANSCRIPT_OPEN_NEXUS = "wepawn://skse-open-nexus"
_SKSE_TRANSCRIPT_OPEN_GAME = "wepawn://skse-open-game-folder"
_SKSE_TRANSCRIPT_VERIFY = "wepawn://skse-verify"
_SKSE_TRANSCRIPT_MO2_EXEC = "wepawn://skse-mo2-exec-done"


class WepawnChatWindow(QWidget):
    """
    Chat UI: transcript (HTML), send/clear, tier analysis with MO2-backed search links.
    """

    _sig_mo2_mod_installed = pyqtSignal(str, int)
    _sig_mo2_download_complete = pyqtSignal(int)
    _sig_loot_sort_done = pyqtSignal(int)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        game_domain: str = "skyrimspecialedition",
        nexus_api_key_getter: Callable[[], str],
        game_version_getter: Callable[[], str],
        application_getter: Callable[[], str],
        organizer_getter: Callable[[], mobase.IOrganizer | None],
        llama_base_url_getter: Callable[[], str | None],
    ) -> None:
        super().__init__(parent)
        self._game_domain = game_domain
        self._nexus_api_key_getter = nexus_api_key_getter
        self._game_version_getter = game_version_getter
        self._application_getter = application_getter
        self._organizer_getter = organizer_getter
        self._llama_base_url_getter = llama_base_url_getter
        self._tier_worker: TierAnalysisWorker | None = None
        self._tier_btn: QPushButton | None = None
        self._tier_pending_mod_display: str = ""
        self._tier_pending_mo2_physical_context: str = ""
        self._id_search_worker: IdSearchWorker | None = None
        self._guide_step_worker: GuideStepWorker | None = None
        self._guide_scan_worker: ScanWorker | None = None
        self._guide_scan_status_range: tuple[int, int] | None = None
        self._id_search_btn: QPushButton | None = None
        self._nemesis_launch_inline_key: tuple[Any, ...] | None = None
        self._skse_manual_inline_key: tuple[Any, ...] | None = None
        self._skse_mo2_exec_inline_key: tuple[Any, ...] | None = None
        self._guide_skse_manual_step_active: bool = False
        self._guide_skse_mo2_exec_verify_pending: bool = False
        self._guide_prompt_after_search: bool = False
        self._guide_prompt_buttons_range: tuple[int, int] | None = None
        self._guide_skse_manual_buttons_range: tuple[int, int] | None = None
        self._guide_context: dict | None = None
        self._guide_awaiting_mod_id: bool = False
        self._guide_awaiting_install_signal: bool = False
        self._guide_mode_paused: bool = False
        self._guide_need_resume_confirm: bool = False
        self._counselor_from_guide_pause: bool = False
        self._guide_prereq_index: int = 0
        self._guide_work_queue: list[dict] = []
        self._guide_completed_prereqs: list[str] = []
        self._guide_phase: str = "mandatory"  # mandatory | mcm_prompt | mcm
        self._guide_mcm_queue: list[dict] = []
        self._guide_awaiting_mcm_prompt: bool = False
        self._guide_optional_labels: list[str] = []
        self._guide_advanced_labels: list[str] = []
        self._guide_awaiting_main_mod_nexus_id: int = 0
        self._loot_auto_sort_running: bool = False
        # ── Guide Stack (새 가이드 엔진) ──────────────
        self._guide_stack: list[dict] = []
        self._dependency_graph: dict[int, list[dict[str, Any]]] = {}
        # MO2 설치 직후 organizer 활성 목록이 아직 갱신되기 전, 라우터·인터셉트 검사용으로 주입.
        self._guide_installed_nexus_override: set[int] = set()
        # 각 프레임 구조:
        # {
        #   "mod_id": int,
        #   "mod_name": str,
        #   "pending": list[dict],   # 미설치 사전모드 목록
        #   "completed": list[str],  # 완료된 사전모드 label
        #   "current_index": int,    # 현재 처리 중인 pending 인덱스
        # }
        # Router intercept: {"_intercept": True, "saved_index", "target_label", "target_nexus_mod_id"}
        self._counselor_worker: CounselorWorker | None = None
        self._counselor_history: list[dict[str, str]] = []
        self._counselor_last_user_text: str = ""
        self._mo2_mod_installed_hooked: bool = False
        self._mo2_download_complete_hooked: bool = False

        self._sig_loot_sort_done.connect(
            self._on_loot_sort_finished, Qt.ConnectionType.QueuedConnection
        )

        org0 = self._organizer_getter()
        loc_code = resolve_initial_locale_code(org0)
        self._tr = set_locale(loc_code)
        _hard_log(f"[LANG] 기존 locale 시스템 확장 로드 완료: {loc_code}")

        self.setWindowTitle(self._tr.tr("chat.window_title"))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(520, 640)

        self._transcript = QTextBrowser(self)
        # 링크는 모두 anchorClicked에서 처리: http(s)는 OS 브라우저, wepawn:// 는 플러그인 액션.
        # (openExternalLinks=True 이면 wepawn 등이 QDesktopServices로만 가고 시그널이 안 올 수 있음)
        self._transcript.setOpenExternalLinks(False)
        self._transcript.setOpenLinks(False)
        self._transcript.anchorClicked.connect(self._on_transcript_anchor_clicked)
        self._transcript.setReadOnly(True)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText(self._tr.tr("chat.input_placeholder"))
        self._input.returnPressed.connect(self._on_send)

        self._send_btn = QPushButton(self._tr.tr("chat.send"), self)
        self._send_btn.clicked.connect(self._on_send)

        self._clear_btn = QPushButton(self._tr.tr("chat.clear"), self)
        self._clear_btn.clicked.connect(self._clear_transcript)

        self._tier_btn = QPushButton(self._tr.tr("chat.tier_stub"), self)
        self._tier_btn.setToolTip(self._tr.tr("chat.tier_stub_tip"))
        self._tier_btn.clicked.connect(self._on_tier_llm)

        id_search_btn = QPushButton(self._tr.tr("chat.id_search"), self)
        id_search_btn.setToolTip(self._tr.tr("chat.id_search_tip"))
        id_search_btn.clicked.connect(self._on_id_search)
        self._id_search_btn = id_search_btn

        try:
            gname, gver = get_current_game_info(org0)
        except Exception:
            gname, gver = "", ""
        dash = "—"
        self._game_info_label = QLabel(
            self._tr.tr(
                "chat.recognized_game_line",
                game=gname.strip() if gname else dash,
                version=gver.strip() if gver else dash,
            ),
            self,
        )
        self._game_info_label.setStyleSheet("color: gray; font-size: 11px;")

        self._guide_status_label = QLabel("")
        self._guide_status_label.setVisible(False)
        self._guide_status_label.setStyleSheet(
            "color: #00cc66; font-weight: bold; padding: 2px 6px;"
        )

        self._lang_combo = QComboBox(self)
        for opt_code, label in LOCALE_COMBO_ENTRIES:
            self._lang_combo.addItem(label, opt_code)
        self._lang_combo.setToolTip(self._tr.tr("chat.ui_language"))
        self._lang_combo.blockSignals(True)
        _lix = self._lang_combo.findData(loc_code)
        self._lang_combo.setCurrentIndex(max(0, _lix))
        self._lang_combo.blockSignals(False)
        self._lang_combo.currentTextChanged.connect(self.change_language)

        row = QHBoxLayout()
        row.addWidget(self._input, stretch=1)
        row.addWidget(self._send_btn)
        row.addWidget(self._clear_btn)
        row.addWidget(self._lang_combo)

        row2 = QHBoxLayout()
        row2.addWidget(self._tier_btn)
        row2.addWidget(id_search_btn)
        row2.addStretch(1)

        self._header_label = QLabel(self._tr.tr("plugin.display_name"), self)

        layout = QVBoxLayout()
        layout.addWidget(self._header_label)
        layout.addWidget(self._game_info_label)
        layout.addWidget(self._guide_status_label)
        layout.addWidget(self._transcript, stretch=1)
        layout.addLayout(row)
        layout.addLayout(row2)
        self.setLayout(layout)

        self._sig_mo2_mod_installed.connect(self._on_mo2_mod_installed_main_thread)
        self._sig_mo2_download_complete.connect(self._on_mo2_download_complete_main_thread)
        self._try_hook_mo2_mod_installed()
        self._try_hook_mo2_download_complete()

        self._append_system(self._tr.tr("chat.welcome"))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._try_hook_mo2_mod_installed()
        self._try_hook_mo2_download_complete()

    def _try_hook_mo2_mod_installed(self) -> None:
        if self._mo2_mod_installed_hooked:
            return
        org = self._organizer_getter()
        if org is None:
            return
        try:
            ml = org.modList()
        except Exception:
            return
        if ml is None:
            return
        reg = getattr(ml, "onModInstalled", None)
        if reg is None or not callable(reg):
            _hard_log("[HOOK DBG] modList().onModInstalled 사용 불가 (API 없음)")
            return
        try:
            reg(self._on_mo2_mod_installed)
        except Exception as exc:
            _hard_log(f"[HOOK DBG] onModInstalled 등록 실패: {type(exc).__name__}: {exc}")
            return
        self._mo2_mod_installed_hooked = True
        _hard_log("[HOOK DBG] MO2 modList().onModInstalled 등록 완료")

    def _try_hook_mo2_download_complete(self) -> None:
        if self._mo2_download_complete_hooked:
            return
        org = self._organizer_getter()
        if org is None:
            return
        try:
            dm = org.downloadManager()
        except Exception:
            return
        if dm is None:
            return
        if not hasattr(dm, "onDownloadComplete"):
            _hard_log("[HOOK DBG] downloadManager().onDownloadComplete 사용 불가 (API 없음)")
            return
        reg = getattr(dm, "onDownloadComplete", None)
        if reg is None or not callable(reg):
            _hard_log("[HOOK DBG] downloadManager().onDownloadComplete 사용 불가 (API 없음)")
            return
        try:
            reg(self._on_mo2_download_complete_native)
        except Exception as exc:
            _hard_log(
                f"[HOOK DBG] onDownloadComplete 등록 실패: {type(exc).__name__}: {exc}"
            )
            return
        self._mo2_download_complete_hooked = True
        _hard_log("[HOOK DBG] MO2 downloadManager().onDownloadComplete 등록 완료")

    def _on_mo2_download_complete_native(self, row: int) -> None:
        """MO2 may call from a non-GUI thread; UI work runs on the main thread via Qt signal."""
        try:
            idx = int(row)
        except (TypeError, ValueError):
            idx = -1
        _hard_log(
            f"[HOOK DBG] MO2 다운로드 완료 콜백 수신! download_index={idx}"
        )
        if idx < 0:
            return
        self._sig_mo2_download_complete.emit(idx)

    def _on_mo2_download_complete_main_thread(self, index: int) -> None:
        org = self._organizer_getter()
        if org is None:
            return
        try:
            dm = org.downloadManager()
        except Exception:
            dm = None
        if dm is None:
            return
        dp_get = getattr(dm, "downloadPath", None)
        if dp_get is None or not callable(dp_get):
            _hard_log("[DOWNLOAD UI] downloadManager().downloadPath unavailable")
            return
        try:
            raw_path = dp_get(int(index))
        except Exception as exc:
            _hard_log(f"[DOWNLOAD UI] downloadPath({index}) failed: {exc}")
            return
        path_s = str(raw_path or "").strip()
        if not path_s:
            _hard_log(f"[DOWNLOAD UI] downloadPath({index}) empty")
            return
        path_obj = Path(path_s)
        try:
            path_resolved = path_obj.resolve()
        except OSError:
            path_resolved = path_obj
        if not path_resolved.is_file():
            _hard_log(f"[DOWNLOAD UI] missing file: {path_s!r}")
            return
        _hard_log(
            f"[DOWNLOAD UI] index={index} path={str(path_resolved)!r} (fomod check)"
        )
        suf = path_resolved.suffix.lower()
        if suf not in (".zip", ".7z", ".rar"):
            self._append_system(
                self._tr.tr(
                    "chat.download_done_install",
                    filename=path_resolved.name,
                )
            )
            return
        xml_or_msg = extract_fomod_xml(str(path_resolved), organizer=org)
        if any(xml_or_msg.startswith(p) for p in FOMOD_EXTRACT_ERROR_PREFIXES):
            self._append_system(xml_or_msg)
            return
        if xml_or_msg == NO_FOMOD_MESSAGE:
            self._append_system(
                self._tr.tr(
                    "chat.download_done_manual",
                    filename=path_resolved.name,
                )
            )
            return
        if fomod_extract_indicates_moduleconfig_wizard(xml_or_msg):
            self._append_system(self._tr.tr("chat.fomod_wizard_user_hint"))

    def _on_mo2_mod_installed(self, mod: mobase.IModInterface) -> None:
        """MO2가 비-UI 스레드에서 호출할 수 있음. UI/라우터는 시그널로 메인 스레드에서 처리."""
        try:
            name = str(mod.name()) if mod is not None else ""
        except Exception:
            name = ""
        try:
            installed_nexus_id = _coerce_positive_nexus_id(mod) if mod is not None else 0
        except Exception:
            installed_nexus_id = 0
        _hard_log(
            f"[HOOK DBG] MO2 설치 콜백 수신! Name: {name}, NexusID: {installed_nexus_id}"
        )
        self._sig_mo2_mod_installed.emit(name, int(installed_nexus_id))

    def _try_activate_installed_mod(self, internal_name: str) -> None:
        """설치 직후 프로필에서 해당 모드 체크(IModList.setActive)."""
        org = self._organizer_getter()
        if org is None:
            return
        name = (internal_name or "").strip()
        if not name:
            return
        try:
            ml = org.modList()
        except Exception:
            return
        if ml is None:
            return
        sa = getattr(ml, "setActive", None)
        if sa is None or not callable(sa):
            return
        try:
            sa(name, True)
        except TypeError:
            try:
                sa([name], True)
            except Exception as exc:
                _hard_log(f"[MOD_INSTALL] setActive(TypeError fallback): {exc}")
        except Exception as exc:
            _hard_log(f"[MOD_INSTALL] setActive: {type(exc).__name__}: {exc}")

    def _guide_current_install_target_nexus_ids(self) -> frozenset[int]:
        """현재 설치 확인 대기 단계의 Nexus ID 집합(행의 nexus_mod_id 및 '또는' 후보)."""
        ctx = self._guide_context
        if ctx is None:
            return frozenset()
        pending = self._guide_pending_items()
        idx = self._guide_prereq_index
        if idx < 0 or idx >= len(pending):
            return frozenset()
        item = pending[idx]
        label = str(item.get("label") or "")
        ids: set[int] = set()
        raw_nid = item.get("nexus_mod_id")
        try:
            node_nid = int(raw_nid) if raw_nid is not None else 0
        except (TypeError, ValueError):
            node_nid = 0
        if node_nid > 0:
            ids.add(node_nid)
        for _disp, seg_nid in _parse_or_guide_segments(label):
            if seg_nid is not None and int(seg_nid) > 0:
                ids.add(int(seg_nid))
        return frozenset(ids)

    def _guide_reset_install_nexus_override(self) -> None:
        self._guide_installed_nexus_override.clear()

    def _guide_effective_active_nexus_ids(
        self, organizer: mobase.IOrganizer | None
    ) -> frozenset[int]:
        base = (
            frozenset(_active_mod_nexus_ids(organizer))
            if organizer is not None
            else frozenset()
        )
        return frozenset(base | frozenset(self._guide_installed_nexus_override))

    def _on_mo2_mod_installed_main_thread(self, name: str, installed_nexus_id: int) -> None:
        self._try_activate_installed_mod(name)
        eff_nexus = int(installed_nexus_id)
        if eff_nexus <= 0 and (name or "").strip():
            org = self._organizer_getter()
            if org is not None:
                try:
                    ml = org.modList()
                    m = ml.getMod(name.strip()) if ml is not None else None
                    if m is not None:
                        eff_nexus = int(_coerce_positive_nexus_id(m))
                except Exception as exc:
                    _hard_log(f"[LOOT/INSTALL] post-install nexus re-read failed: {exc!r}")
        exp_main = int(self._guide_awaiting_main_mod_nexus_id)
        _hard_log(
            f"[LOOT/INSTALL] mod_installed name={name!r} nexus_from_hook={installed_nexus_id} "
            f"nexus_effective={eff_nexus} awaiting_main_nexus={exp_main}"
        )
        if exp_main > 0 and eff_nexus != exp_main:
            _hard_log(
                f"[LOOT/INSTALL] skip all_installs_done+LOOT: nexus mismatch "
                f"(expected main {exp_main}, effective {eff_nexus})"
            )
        if (
            exp_main > 0
            and eff_nexus == exp_main
        ):
            self._guide_awaiting_main_mod_nexus_id = 0
            self._append_system(self._tr.tr("guide.all_installs_done"))
            self._guide_lamp_off()
            self._guide_try_loot_auto_sort_after_all_installs()
        if installed_nexus_id == NEMESIS_NEXUS_ID:
            self._sync_nemesis_launch_button(
                step_mod_id=NEMESIS_NEXUS_ID,
                or_segments=[],
                item={},
                allow_without_guide=True,
                after_mo2_install=True,
            )
        if (
            self._guide_skse_manual_step_active
            or self._guide_skse_mo2_exec_verify_pending
        ) and is_script_extender_guide_nexus_id(int(eff_nexus)):
            _hard_log(
                "[ROUTER DBG] Script extender manual/MO2 step: ignoring MO2 mod install "
                f"callback (nexus={eff_nexus})"
            )
            return
        if not self._guide_awaiting_install_signal or self._guide_context is None:
            return
        expected = self._guide_current_install_target_nexus_ids()
        if not expected:
            return
        if installed_nexus_id <= 0 or installed_nexus_id not in expected:
            _hard_log(
                f"[ROUTER DBG] 설치 콜백 NexusID 불일치·무시 "
                f"(installed={installed_nexus_id}, expected={sorted(expected)})"
            )
            return
        _hard_log(
            f"[ROUTER DBG] 타겟 일치 확인! 라우터 자동 전진 트리거 "
            f"(Target ID: {installed_nexus_id})"
        )
        if installed_nexus_id > 0:
            self._guide_installed_nexus_override.add(installed_nexus_id)
            _hard_log(
                f"[STATE DBG] 라우터 전진 전 상태 강제 갱신 완료: ID {installed_nexus_id} 추가됨"
            )
        self._guide_try_advance_after_install_claim(
            mo2_installed_nexus_id=installed_nexus_id
        )

    def _guide_try_loot_auto_sort_after_all_installs(self) -> None:
        _hard_log("[LOOT] _guide_try_loot_auto_sort_after_all_installs entered")
        if self._loot_auto_sort_running:
            _hard_log("[LOOT] skip: already running")
            return
        org = self._organizer_getter()
        if org is None:
            _hard_log("[LOOT] skip: organizer None")
            return
        from ..logic.load_order_manager import (
            is_loot_executable_registered,
            start_loot_auto_sort_in_background,
        )

        if not is_loot_executable_registered(org):
            _hard_log("[LOOT] skip: executable not resolved (see msg_loot_not_registered)")
            self._append_system(self._tr.tr("guide.msg_loot_not_registered"))
            return

        self._loot_auto_sort_running = True
        _busy = self._tr.tr("guide.msg_loot_sort_in_progress")
        self._append_system(_busy)
        self._guide_status_label.setText(_busy)
        self._guide_status_label.setVisible(True)

        def _on_thread_done(code: int | None) -> None:
            sc = int(code) if code is not None else -1
            # Wait runs on a Python thread; QTimer without a main-thread context never fires
            # there. Queued signal delivers the slot on the GUI thread.
            self._sig_loot_sort_done.emit(sc)

        if not start_loot_auto_sort_in_background(org, _on_thread_done):
            self._loot_auto_sort_running = False
            self._guide_lamp_off()

    def _on_loot_sort_finished(self, status_code: int) -> None:
        self._loot_auto_sort_running = False
        self._guide_lamp_off()
        _hard_log(f"[LOOT] 실행 결과 상태 코드: {status_code}")
        self._append_system(self._tr.tr("guide.msg_loot_sort_complete"))

    def _guide_lamp_on(self, mod_name: str) -> None:
        _hard_log("[LAMP] _guide_lamp_on 호출됨")
        self._guide_status_label.setText(
            self._tr.tr("guide.status_in_progress", name=mod_name)
        )
        self._guide_status_label.setVisible(True)

    def _guide_lamp_off(self) -> None:
        self._guide_status_label.setText("")
        self._guide_status_label.setVisible(False)

    def _guide_stack_try_advance(self) -> None:
        """
        현재 스택 프레임의 current_index를 증가.
        프레임 내 pending 소진 시 pop 후 상위 복귀.
        스택 비었으면 가이드 완료.
        """
        if not self._guide_stack:
            self._guide_lamp_off()
            return

        frame = self._guide_stack[-1]
        frame["current_index"] += 1

        # 현재 프레임 pending 소진 여부
        if frame["current_index"] >= len(frame["pending"]):
            # 현재 프레임 완료 → pop
            completed_mod = frame["mod_name"]
            self._guide_stack.pop()

            if not self._guide_stack:
                # 스택 비었으면 전체 완료
                self._guide_lamp_off()
                self._append_assistant(
                    self._tr.tr(
                        "guide.stack_all_prereqs_done",
                        mod=completed_mod,
                    )
                )
                return

            # 상위 프레임으로 복귀
            parent = self._guide_stack[-1]
            parent_pending = parent["pending"]
            parent_idx = parent["current_index"]

            if parent_idx < len(parent_pending):
                next_item = parent_pending[parent_idx]
                next_name = next_item.get("label", "")
                self._append_assistant(
                    self._tr.tr(
                        "guide.stack_prereq_done_next",
                        completed=completed_mod,
                        next=next_name,
                    )
                )
        else:
            # 같은 프레임 내 다음 항목
            next_item = frame["pending"][frame["current_index"]]
            next_name = next_item.get("label", "")
            self._append_assistant(
                self._tr.tr("guide.stack_next_only", next=next_name)
            )

    def _append_system(self, text: str) -> None:
        self._append_html(f"<p><i>{_esc(text)}</i></p>")

    def _begin_guide_scan_status_line(self) -> None:
        doc = self._transcript.document()
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.MoveOperation.End)
        start = cur.position()
        cur.insertHtml(
            f"<p><i>{_esc(self._tr.tr('guide.scan_analyzing_deps'))}</i></p>"
        )
        cur.insertHtml("<br/>")
        self._guide_scan_status_range = (start, cur.position())

    def _resolve_guide_scan_status_line(self, *, replace_with: str | None) -> None:
        r = self._guide_scan_status_range
        self._guide_scan_status_range = None
        if r is None:
            if replace_with is not None:
                self._append_system(replace_with)
            return
        start, end = r
        doc = self._transcript.document()
        n = max(0, doc.characterCount())
        if start < 0 or end < start or start > n:
            if replace_with is not None:
                self._append_system(replace_with)
            return
        cur = QTextCursor(doc)
        cur.setPosition(min(start, n))
        cur.setPosition(min(end, n), QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        if replace_with is not None:
            cur.insertHtml(f"<p><i>{_esc(replace_with)}</i></p>")
            cur.insertHtml("<br/>")

    def _append_user(self, text: str, *, counselor_spaced: bool = False) -> None:
        label = self._tr.tr("chat.you_label")
        lead = "<br/>" if counselor_spaced else ""
        self._append_html(f"{lead}<p><b>{_esc(label)}:</b> {_esc(text)}</p>")

    def _append_assistant(self, text: str) -> None:
        label = self._tr.tr("chat.wepawn_label")
        self._append_html(f"<p><b>{_esc(label)}:</b> {_esc(text)}</p>")

    def _append_html(self, html: str) -> None:
        self._transcript.moveCursor(QTextCursor.MoveOperation.End)
        self._transcript.insertHtml(html)
        self._transcript.insertHtml("<br/>")
        self._transcript.moveCursor(QTextCursor.MoveOperation.End)

    def _on_transcript_anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() == "wepawn":
            host = (url.host() or "").lower()
            if host == "guide-yes":
                self._on_guide_prompt_inline_yes()
                return
            if host == "guide-no":
                self._on_guide_prompt_inline_no()
                return
            if host == "nemesis-launch":
                self._on_nemesis_launch_clicked()
                return
            if host == "skse-open-nexus":
                self._on_skse_open_nexus_clicked()
                return
            if host == "skse-open-game-folder":
                self._on_skse_open_game_folder_clicked()
                return
            if host == "skse-verify":
                self._on_skse_verify_clicked()
                return
            if host == "skse-mo2-exec-done":
                self._on_skse_mo2_exec_confirm_clicked()
                return
        if url.scheme() in ("http", "https", "mailto") or url.isLocalFile():
            QDesktopServices.openUrl(url)
            return
        if url.isValid() and url.scheme():
            QDesktopServices.openUrl(url)

    def change_language(self, _display_text: str = "") -> None:
        code = self._lang_combo.currentData()
        if code is None:
            return
        self._apply_ui_language(str(code))

    def _apply_ui_language(self, code: str) -> None:
        c = normalize_ui_locale_code(code)
        if not c:
            return
        self._tr = set_locale(c)
        _hard_log(f"[LANG] 기존 locale 시스템 확장 로드 완료: {c}")
        persist_selected_language(c)
        self._refresh_ui_texts()
        self._lang_combo.blockSignals(True)
        idx = self._lang_combo.findData(c)
        if idx >= 0:
            self._lang_combo.setCurrentIndex(idx)
        self._lang_combo.blockSignals(False)

    def _refresh_ui_texts(self) -> None:
        self.setWindowTitle(self._tr.tr("chat.window_title"))
        self._input.setPlaceholderText(self._tr.tr("chat.input_placeholder"))
        self._send_btn.setText(self._tr.tr("chat.send"))
        self._clear_btn.setText(self._tr.tr("chat.clear"))
        if self._tier_btn is not None:
            self._tier_btn.setText(self._tr.tr("chat.tier_stub"))
            self._tier_btn.setToolTip(self._tr.tr("chat.tier_stub_tip"))
        if self._id_search_btn is not None:
            self._id_search_btn.setText(self._tr.tr("chat.id_search"))
            self._id_search_btn.setToolTip(self._tr.tr("chat.id_search_tip"))
        self._header_label.setText(self._tr.tr("plugin.display_name"))
        self._lang_combo.setToolTip(self._tr.tr("chat.ui_language"))
        org = self._organizer_getter()
        try:
            gname, gver = get_current_game_info(org)
        except Exception:
            gname, gver = "", ""
        dash = "—"
        self._game_info_label.setText(
            self._tr.tr(
                "chat.recognized_game_line",
                game=gname.strip() if gname else dash,
                version=gver.strip() if gver else dash,
            )
        )

    def _clear_transcript(self) -> None:
        self._guide_awaiting_main_mod_nexus_id = 0
        self._transcript.clear()
        self._nemesis_launch_clear_offer_state()
        self._guide_prompt_buttons_range = None
        self._guide_skse_manual_buttons_range = None
        self._guide_prompt_after_search = False
        self._guide_context = None
        self._guide_reset_install_nexus_override()
        self._guide_awaiting_mod_id = False
        self._guide_awaiting_install_signal = False
        self._guide_mode_paused = False
        self._guide_need_resume_confirm = False
        self._counselor_from_guide_pause = False
        self._guide_prereq_index = 0
        self._guide_work_queue = []
        self._guide_completed_prereqs = []
        self._guide_phase = "mandatory"
        self._guide_mcm_queue = []
        self._guide_awaiting_mcm_prompt = False
        self._guide_optional_labels = []
        self._guide_advanced_labels = []
        self._guide_stack.clear()
        self._dependency_graph = {}
        self._guide_lamp_off()
        self._guide_skse_manual_step_active = False
        self._guide_skse_mo2_exec_verify_pending = False
        self._counselor_worker = None
        self._guide_step_worker = None
        self._guide_scan_status_range = None
        self._counselor_history = []
        self._counselor_last_user_text = ""
        self._append_system(self._tr.tr("chat.welcome"))

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        if self._guide_need_resume_confirm:
            self._append_user(text, counselor_spaced=True)
            self._input.clear()
            if _guide_text_has_positive(text):
                self._guide_need_resume_confirm = False
                self._guide_mode_paused = False
                self._append_guide_step_message()
            else:
                self._append_system(self._tr.tr("guide.resume_counselor_yes"))
            return
        if self._guide_awaiting_mcm_prompt and self._guide_context is not None:
            self._append_user(text, counselor_spaced=True)
            self._input.clear()
            if _guide_text_has_positive(text):
                self._guide_awaiting_mcm_prompt = False
                self._guide_phase = "mcm"
                self._guide_work_queue = [dict(x) for x in self._guide_mcm_queue]
                self._guide_mcm_queue = []
                self._guide_prereq_index = 0
                self._guide_awaiting_mod_id = False
                self._guide_awaiting_install_signal = False
                self._append_guide_step_message()
                return
            if _guide_text_has_negative(text):
                self._guide_awaiting_mcm_prompt = False
                self._finish_guide_main_mod_ready()
                return
            self._append_system(self._tr.tr("guide.mcm_yes_no"))
            return
        if self._guide_awaiting_mod_id and self._guide_context is not None:
            self._append_user(text, counselor_spaced=True)
            self._input.clear()
            if text.isdigit():
                mod_id = int(text, 10)
                if mod_id <= 0 or mod_id > _MAX_NEXUS_MOD_ID:
                    self._append_system(self._tr.tr("chat.id_search_need_number"))
                    return
                self._start_guide_step_worker(mod_id)
                return
            self._guide_mode_paused = True
            self._start_counselor_worker(text, from_guide_pause=True)
            return
        if self._guide_awaiting_install_signal and self._guide_context is not None:
            self._append_user(text, counselor_spaced=True)
            self._input.clear()
            if _guide_text_has_install_done(text):
                self._guide_try_advance_after_install_claim()
                return
            self._guide_mode_paused = True
            self._start_counselor_worker(text, from_guide_pause=True)
            return
        if self._guide_prompt_after_search:
            _hard_log("[SEND DBG] guide_prompt_after_search 블록 진입")
            self._append_user(text)
            self._input.clear()
            if _guide_text_has_negative(text):
                self._remove_guide_prompt_buttons()
                self._finish_guide_prompt_decline()
                return
            if _guide_text_has_positive(text):
                self._remove_guide_prompt_buttons()
                self._finish_guide_prompt_accept()
                return
            self._append_system(self._tr.tr("guide.use_yes_no_buttons"))
            return
        if self._id_search_worker is not None and self._id_search_worker.isRunning():
            self._append_system(self._tr.tr("guide.busy_id_search"))
            return
        if self._guide_step_worker is not None and self._guide_step_worker.isRunning():
            self._append_system(self._tr.tr("guide.busy_generic"))
            return
        if self._guide_scan_worker is not None and self._guide_scan_worker.isRunning():
            self._append_system(self._tr.tr("guide.busy_prereq_scan"))
            return
        if self._counselor_worker is not None and self._counselor_worker.isRunning():
            self._append_system(self._tr.tr("guide.busy_counselor"))
            return
        self._append_user(text, counselor_spaced=True)
        self._input.clear()
        self._start_counselor_worker(text)

    def _start_counselor_worker(self, user_text: str, *, from_guide_pause: bool = False) -> None:
        base_url = self._llama_base_url_getter()
        if not base_url:
            self._append_system(self._tr.tr("ai.portable_server_unavailable"))
            return
        gname, gver = _read_game_version_for_counselor(self._organizer_getter())
        guide_ctx_payload: dict | None = None
        if self._guide_context is not None:
            guide_ctx_payload = self._guide_system_context_for_llm()
        worker = CounselorWorker(
            user_message=user_text,
            base_url=base_url,
            prior_messages=list(self._counselor_history),
            game_display_name=gname,
            game_version=gver,
            game_domain=self._game_domain,
            guide_system_context=guide_ctx_payload,
            request_timeout=90.0,
            parent=self,
        )
        self._counselor_last_user_text = user_text
        self._counselor_from_guide_pause = from_guide_pause
        self._counselor_worker = worker
        worker.finished_ok.connect(self._on_counselor_ok)
        worker.failed.connect(self._on_counselor_failed)
        worker.finished.connect(self._on_counselor_cleanup)
        worker.start()

    def _on_counselor_ok(self, reply: str) -> None:
        ut = (self._counselor_last_user_text or "").strip()
        if ut:
            self._counselor_history.append({"role": "user", "content": ut})
            self._counselor_history.append({"role": "assistant", "content": reply.strip()})
            if len(self._counselor_history) > _COUNSELOR_HISTORY_MAX_MESSAGES:
                self._counselor_history = self._counselor_history[-_COUNSELOR_HISTORY_MAX_MESSAGES:]
        self._counselor_last_user_text = ""
        label = self._tr.tr("chat.wepawn_label")
        body = format_counselor_reply_html_body(reply)
        self._append_html(f"<br/><p><b>{_esc(label)}:</b> {body}</p>")
        if self._counselor_from_guide_pause:
            self._counselor_from_guide_pause = False
            self._guide_need_resume_confirm = True
            self._append_system(
                self._tr.tr("guide.counselor_continue_question")
            )

    def _on_counselor_failed(self, message: str, connection: bool) -> None:
        self._counselor_last_user_text = ""
        self._counselor_from_guide_pause = False
        if connection:
            self._append_system(self._tr.tr("ai.llm_connection_error", detail=message))
        else:
            self._append_system(self._tr.tr("ai.llm_parse_error", detail=message))

    def _on_counselor_cleanup(self) -> None:
        w = self._counselor_worker
        self._counselor_worker = None
        if w is not None:
            w.deleteLater()

    def _append_plain_text_block(self, text: str) -> None:
        self._append_html(
            '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.45;margin:8px 0;">'
            f"{_esc(text)}</pre>"
        )

    def _on_id_search(self) -> None:
        if self._id_search_worker is not None and self._id_search_worker.isRunning():
            return
        if self._guide_step_worker is not None and self._guide_step_worker.isRunning():
            return
        if self._guide_scan_worker is not None and self._guide_scan_worker.isRunning():
            return
        self._guide_prompt_buttons_range = None
        self._guide_skse_manual_buttons_range = None
        self._guide_prompt_after_search = False
        self._nemesis_launch_clear_offer_state()
        self._guide_context = None
        self._guide_reset_install_nexus_override()
        self._guide_awaiting_main_mod_nexus_id = 0
        self._guide_awaiting_mod_id = False
        self._guide_awaiting_install_signal = False
        self._guide_skse_manual_step_active = False
        self._guide_skse_mo2_exec_verify_pending = False
        self._guide_mode_paused = False
        self._guide_need_resume_confirm = False
        self._guide_prereq_index = 0
        self._guide_work_queue = []
        self._guide_completed_prereqs = []
        self._guide_phase = "mandatory"
        self._guide_mcm_queue = []
        self._guide_awaiting_mcm_prompt = False
        self._guide_optional_labels = []
        self._guide_advanced_labels = []
        self._guide_step_worker = None
        mod_id, ok = QInputDialog.getInt(
            self,
            self._tr.tr("chat.id_search"),
            self._tr.tr("chat.nexus_dialog_mod_label"),
            1,
            1,
            _MAX_NEXUS_MOD_ID,
            1,
        )
        if not ok:
            return
        self._start_id_search_worker(mod_id)

    def _organizer_game_directory(self, organizer: mobase.IOrganizer | None) -> str:
        if organizer is None:
            return ""
        try:
            mg = organizer.managedGame()
            if mg is None:
                return ""
            gd = mg.gameDirectory()
            if gd is None:
                return ""
            ap = gd.absolutePath()
            return str(ap).strip() if ap is not None else ""
        except Exception:
            return ""

    def _on_id_search_guide_context(self, ctx: object) -> None:
        if isinstance(ctx, dict):
            ctx = dict(ctx)
            if not ctx.get("pending_prereq_items") and ctx.get("pending_prereq_labels"):
                labels = list(ctx.get("pending_prereq_labels") or [])
                ctx["pending_prereq_items"] = [
                    normalize_prereq_item_dict(
                        {
                            "label": lab,
                            "nexus_mod_id": nid,
                            "children": [],
                            "priority": pri,
                        }
                    )
                    for x in labels
                    if str(x).strip()
                    for lab, nid, pri in [split_prereq_line_meta(str(x))]
                ]
            raw = ctx.get("pending_prereq_items")
            if isinstance(raw, list):
                ctx["pending_prereq_items"] = [
                    normalize_prereq_item_dict(x) for x in raw if isinstance(x, dict)
                ]
            try:
                ctx_main = int(ctx.get("main_mod_id") or 0)
            except (TypeError, ValueError):
                ctx_main = 0
            if not self._id_search_worker_matches_guide_ctx_main_id(ctx_main):
                return
            self._dependency_graph = {}
            self._guide_reset_install_nexus_override()
            self._guide_awaiting_main_mod_nexus_id = 0
            inject_script_extender_prereq_if_missing(
                ctx, self._organizer_getter()
            )
            self._guide_context = ctx

    def _rebuild_guide_work_queue(self) -> None:
        ctx = self._guide_context
        if not isinstance(ctx, dict):
            self._guide_work_queue = []
            python_hard_log(
                f"[QUEUE DBG] work_queue={[x.get('nexus_mod_id') for x in self._guide_work_queue if isinstance(x, dict)]}"
            )
            return
        organizer = self._organizer_getter()
        active_names = _active_mod_display_names(organizer)
        active_nexus_ids = self._guide_effective_active_nexus_ids(organizer)
        gd = self._organizer_game_directory(organizer)
        game_dir: str | None = gd or None
        if not (gd or "").strip():
            game_dir = str(ctx.get("game_directory") or "").strip() or None
        raw = ctx.get("pending_prereq_items")
        if not isinstance(raw, list):
            self._guide_work_queue = []
            python_hard_log(
                f"[QUEUE DBG] work_queue={[x.get('nexus_mod_id') for x in self._guide_work_queue if isinstance(x, dict)]}"
            )
            return
        tree = [normalize_prereq_item_dict(x) for x in raw if isinstance(x, dict)]
        python_hard_log(
            f"[QUEUE DBG] pending_prereq_items pre_filter nexus_ids="
            f"{[t.get('nexus_mod_id') for t in tree if isinstance(t, dict)]}"
        )
        tree = filter_prereq_tree_unsatisfied(
            tree, active_names, game_dir, active_nexus_ids=active_nexus_ids
        )
        flat = flatten_prereq_install_order(tree)
        self._guide_work_queue = _dedupe_guide_work_queue_by_nexus_id(flat)
        python_hard_log(
            f"[QUEUE DBG] work_queue={[x.get('nexus_mod_id') for x in self._guide_work_queue if isinstance(x, dict)]}"
        )

    def _guide_system_context_for_llm(self) -> dict:
        ctx = self._guide_context
        if not isinstance(ctx, dict):
            return {}
        wq = self._guide_work_queue
        idx = self._guide_prereq_index
        if idx < 0 or idx >= len(wq):
            cur = ""
        else:
            cur = _strip_priority_markers_from_label(str(wq[idx].get("label") or "").strip())
        rest = [
            _strip_priority_markers_from_label(str(x.get("label") or "").strip())
            for x in wq[idx + 1 :]
            if _strip_priority_markers_from_label(str(x.get("label") or "").strip())
        ]
        return {
            "main_mod": str(ctx.get("main_mod_name") or "").strip(),
            "current_target": cur,
            "completed_prereqs": list(self._guide_completed_prereqs),
            "remaining_prereqs": rest,
            "guide_phase": str(self._guide_phase or ""),
            "awaiting_mcm_prompt": bool(self._guide_awaiting_mcm_prompt),
            "mcm_queue_remaining": len(self._guide_mcm_queue),
        }

    def _guide_pending_items(self) -> list[dict]:
        return list(self._guide_work_queue)

    def _append_optional_advanced_hints(self) -> None:
        opt = [str(x).strip() for x in (self._guide_optional_labels or []) if str(x).strip()]
        adv = [str(x).strip() for x in (self._guide_advanced_labels or []) if str(x).strip()]
        if not opt and not adv:
            return
        parts: list[str] = []
        if opt:
            safe = html.escape(", ".join(opt), quote=False)
            opt_plain = ", ".join(opt)
            parts.append(
                "<p><small>"
                f"{html.escape(self._tr.tr('guide.optional_skipped', list=opt_plain), quote=False)}"
                "</small></p>"
            )
        if adv:
            adv_plain = ", ".join(adv)
            parts.append(
                "<p><small>"
                f"{html.escape(self._tr.tr('guide.advanced_only', list=adv_plain), quote=False)}"
                "</small></p>"
            )
        self._append_html("".join(parts))

    def _finish_guide_main_mod_ready(self) -> None:
        ctx = self._guide_context
        mid = _main_mod_nexus_id_from_ctx(ctx)
        self._guide_awaiting_main_mod_nexus_id = mid
        if mid <= 0:
            _hard_log(
                "[LOOT/INSTALL] _finish_guide_main_mod_ready: main_mod_id missing in ctx; "
                "MO2 install hook will not match — LOOT auto-sort after main install disabled"
            )
        else:
            _hard_log(
                f"[LOOT/INSTALL] awaiting main mod MO2 install (nexus_id={mid}) "
                "for all_installs_done + LOOT"
            )
        main_name = str((ctx or {}).get("main_mod_name") or "").strip() or self._tr.tr(
            "guide.main_mod_fallback"
        )
        safe = html.escape(main_name, quote=False)
        self._append_html(
            "<p><b>"
            f"{html.escape(self._tr.tr('guide.finish_prereqs_title'), quote=False)}</b><br/>"
            f"{self._tr.tr('guide.finish_prereqs_body', name=safe)}</p>"
        )
        self._append_optional_advanced_hints()
        self._nemesis_launch_clear_offer_state()
        self._guide_context = None
        self._guide_reset_install_nexus_override()
        self._guide_work_queue = []
        self._guide_completed_prereqs = []
        self._guide_phase = "mandatory"
        self._guide_mcm_queue = []
        self._guide_awaiting_mcm_prompt = False
        self._guide_optional_labels = []
        self._guide_advanced_labels = []
        self._guide_stack.clear()
        self._dependency_graph = {}
        self._guide_lamp_off()
        self._guide_skse_manual_step_active = False
        self._guide_skse_mo2_exec_verify_pending = False

    def _nemesis_launch_clear_offer_state(self) -> None:
        self._nemesis_launch_inline_key = None
        self._skse_manual_inline_key = None
        self._skse_mo2_exec_inline_key = None
        self._guide_skse_manual_buttons_range = None

    def _append_nemesis_launch_inline(self) -> None:
        href = html.escape(_NEMESIS_TRANSCRIPT_LAUNCH_URL, quote=True)
        btn = (
            f'<a href="{href}" title="Nemesis Unlimited Behavior Engine (MO2 VFS)" '
            'style="display:inline-block;margin-top:6px;padding:6px 16px;'
            "background:#1565c0;color:#ffffff;text-decoration:none;border-radius:4px;"
            'font-weight:600;">'
            f"{html.escape(self._tr.tr('guide.nemesis_launch'), quote=False)}</a>"
        )
        self._append_html(
            f"<p style='margin-top:8px;line-height:1.6'>{btn}</p>"
        )

    def _sync_nemesis_launch_button(
        self,
        *,
        step_mod_id: int,
        or_segments: list,
        item: dict[str, Any],
        allow_without_guide: bool = False,
        after_mo2_install: bool = False,
    ) -> None:
        ext = bool(item.get("is_external"))
        if (
            step_mod_id != NEMESIS_NEXUS_ID
            or or_segments
            or ext
        ):
            self._nemesis_launch_clear_offer_state()
            return

        if self._guide_context is not None:
            pending = self._guide_pending_items()
            idx = self._guide_prereq_index
            if idx < 0 or idx >= len(pending):
                self._nemesis_launch_clear_offer_state()
                return
            cur = pending[idx]
            if bool(cur.get("is_external")):
                self._nemesis_launch_clear_offer_state()
                return
            if after_mo2_install:
                if NEMESIS_NEXUS_ID not in self._guide_current_install_target_nexus_ids():
                    self._nemesis_launch_clear_offer_state()
                    return
            else:
                try:
                    cur_id = int(cur.get("nexus_mod_id") or 0)
                except (TypeError, ValueError):
                    cur_id = 0
                cur_or = _parse_or_guide_segments(str(cur.get("label") or ""))
                if cur_id != NEMESIS_NEXUS_ID or cur_or:
                    self._nemesis_launch_clear_offer_state()
                    return
        elif not allow_without_guide:
            self._nemesis_launch_clear_offer_state()
            return
        if self._guide_context is not None:
            key: tuple[Any, ...] = (
                "guide",
                id(self._guide_context),
                self._guide_prereq_index,
            )
        else:
            key = ("noguide",)
        if self._nemesis_launch_inline_key == key:
            return
        self._nemesis_launch_inline_key = key
        self._append_nemesis_launch_inline()

    def _on_nemesis_launch_clicked(self) -> None:
        org = self._organizer_getter()
        if org is None:
            QMessageBox.warning(
                self,
                self._tr.tr("guide.nemesis_dialog_title"),
                self._tr.tr("guide.nemesis_no_organizer"),
            )
            return
        if not launch_nemesis_with_organizer(org, self):
            return
        self._append_system(self._tr.tr("guide.nemesis_instructions"))

    def _skse_manual_footer_html(self) -> str:
        """
        ID 검색 가이드 푸터(``guide_prompt_footer_inline_html``)와 동일한 링크·버튼 스타일.
        각 액션은 별도 ``<p>`` 행으로 분리한다.
        """
        sty = (
            "display:inline-block;margin:4px 0 0 0;padding:6px 16px;"
            "background:#1565c0;color:#ffffff;text-decoration:none;border-radius:4px;"
            "font-weight:600;"
        )
        org = self._organizer_getter()
        _game = html.escape(
            game_folder_short_label_for_organizer(org), quote=False
        )
        h1 = html.escape(_SKSE_TRANSCRIPT_OPEN_NEXUS, quote=True)
        h2 = html.escape(_SKSE_TRANSCRIPT_OPEN_GAME, quote=True)
        h3 = html.escape(_SKSE_TRANSCRIPT_VERIFY, quote=True)
        t1 = html.escape(self._tr.tr("guide.skse_open_nexus"), quote=False)
        t2 = html.escape(
            self._tr.tr("guide.skse_open_game_folder", game=_game), quote=False
        )
        t3 = html.escape(self._tr.tr("guide.skse_verify_manual"), quote=False)
        return (
            f'<p style="margin:8px 0 4px 0;"><a href="{h1}" style="{sty}">{t1}</a></p>'
            f'<p style="margin:4px 0 0 0;"><a href="{h2}" style="{sty}">{t2}</a></p>'
            f'<p style="margin:4px 0 0 0;"><a href="{h3}" style="{sty}">{t3}</a></p>'
        )

    def _append_skse_manual_action_footer(self) -> None:
        """본문과 분리: ``_on_id_search_ok`` 의 가이드 푸터 삽입 방식과 동일."""
        doc = self._transcript.document()
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.MoveOperation.End)
        start = cur.position()
        cur.insertHtml(self._skse_manual_footer_html())
        cur.insertHtml("<br/>")
        end = cur.position()
        self._guide_skse_manual_buttons_range = (start, end)

    def _sync_skse_manual_inline_buttons(
        self,
        *,
        step_mod_id: int,
        or_segments: list,
        item: dict[str, Any],
    ) -> None:
        ext = bool(item.get("is_external"))
        if (
            not is_script_extender_guide_nexus_id(step_mod_id)
            or or_segments
            or ext
        ):
            return
        if self._guide_context is not None:
            key: tuple[Any, ...] = (
                "skse",
                id(self._guide_context),
                self._guide_prereq_index,
            )
        else:
            key = ("skse", "noguide")
        if self._skse_manual_inline_key == key:
            return
        self._skse_manual_inline_key = key
        self._append_skse_manual_action_footer()

    def _guide_skse_try_verify_and_advance(self) -> None:
        ctx = self._guide_context
        if ctx is None or not self._guide_skse_manual_step_active:
            return
        organizer = self._organizer_getter()
        game_path = self._organizer_game_directory(organizer)
        if not (game_path or "").strip():
            game_path = str(ctx.get("game_directory") or "").strip()
        basename = script_extender_loader_basename_for_organizer(organizer)
        skse_exe_path, is_exists = loader_path_exists(
            game_path or None, basename
        )
        _hard_log(
            f"[SE VERIFY] 경로 확인: {skse_exe_path} / 결과: {is_exists} "
            f"(basename={basename})"
        )
        if is_exists:
            self._guide_skse_manual_step_active = False
            pending = self._guide_pending_items()
            idx = self._guide_prereq_index
            nid = 0
            if 0 <= idx < len(pending):
                try:
                    nid = int(pending[idx].get("nexus_mod_id") or 0)
                except (TypeError, ValueError):
                    nid = 0
            if nid <= 0:
                nid = script_extender_nexus_mod_id_for_organizer(organizer)
            self._guide_installed_nexus_override.add(nid)
            self._guide_skse_mo2_exec_verify_pending = True
            self._nemesis_launch_clear_offer_state()
            self._append_skse_mo2_exec_register_ui()
        else:
            self._append_system(
                self._tr.tr(
                    "guide.msg_skse_not_found",
                    loader=basename,
                )
            )

    def _append_skse_mo2_exec_register_ui(self) -> None:
        ctx = self._guide_context
        if ctx is None:
            return
        org = self._organizer_getter()
        game_path = self._organizer_game_directory(org)
        if not (game_path or "").strip():
            game_path = str(ctx.get("game_directory") or "").strip()
        abs_path, _bn = script_extender_loader_absolute_path(org, game_path or None)
        if self._guide_context is not None:
            key: tuple[Any, ...] = (
                "skse_mo2",
                id(self._guide_context),
                self._guide_prereq_index,
            )
        else:
            key = ("skse_mo2", "noguide")
        if self._skse_mo2_exec_inline_key == key:
            return
        self._skse_mo2_exec_inline_key = key
        safe_path = html.escape(abs_path or "", quote=False)
        guide = self._tr.tr("guide.msg_exec_register_guide")
        plab = self._tr.tr("guide.msg_exec_path_label")
        btn_h = html.escape(_SKSE_TRANSCRIPT_MO2_EXEC, quote=True)
        btn_t = html.escape(self._tr.tr("guide.exec_mo2_confirm"), quote=False)
        style = (
            "display:inline-block;margin-top:10px;padding:6px 16px;"
            "background:#1565c0;color:#ffffff;text-decoration:none;border-radius:4px;"
            "font-weight:600;"
        )
        btn = f'<a href="{btn_h}" style="{style}">{btn_t}</a>'
        self_check = html.escape(
            self._tr.tr("guide.msg_exec_self_check"), quote=False
        )
        self._append_html(
            "<p style='line-height:1.65'>"
            f"{guide}"
            f"</p><p style='margin-top:10px;font-size:13px'>{self_check}</p>"
            f"<p style='margin-top:8px'><b>{html.escape(plab, quote=False)}</b><br/>"
            f"<code style='font-size:13px;word-break:break-all'>{safe_path}</code></p>"
            f"<p style='margin-top:10px'>{btn}</p>"
        )

    def _guide_skse_try_mo2_exec_verify_and_advance(self) -> None:
        ctx = self._guide_context
        if ctx is None or not self._guide_skse_mo2_exec_verify_pending:
            return
        organizer = self._organizer_getter()
        if organizer is None:
            self._append_system(self._tr.tr("nexus.organizer_unavailable"))
            return
        pending = self._guide_pending_items()
        idx = self._guide_prereq_index
        if idx < 0 or idx >= len(pending):
            return
        try:
            nid = int(pending[idx].get("nexus_mod_id") or 0)
        except (TypeError, ValueError):
            nid = 0
        if nid <= 0:
            nid = script_extender_nexus_mod_id_for_organizer(organizer)
        self._guide_installed_nexus_override.add(nid)
        self._guide_skse_mo2_exec_verify_pending = False
        clean = _strip_priority_markers_from_label(
            str(pending[idx].get("label") or "").strip()
        )
        self._guide_advance_prereq_queue_after_success(clean)

    def _on_skse_mo2_exec_confirm_clicked(self) -> None:
        self._guide_skse_try_mo2_exec_verify_and_advance()

    def _on_skse_open_nexus_clicked(self) -> None:
        ctx = self._guide_context
        game_domain = (
            str(ctx.get("game_domain") or self._game_domain)
            if isinstance(ctx, dict)
            else str(self._game_domain)
        )
        org = self._organizer_getter()
        pending = self._guide_pending_items()
        gidx = self._guide_prereq_index
        mid = 0
        if 0 <= gidx < len(pending):
            try:
                mid = int(pending[gidx].get("nexus_mod_id") or 0)
            except (TypeError, ValueError):
                mid = 0
        if not is_script_extender_guide_nexus_id(mid):
            mid = script_extender_nexus_mod_id_for_organizer(org)
        mod_url = (
            build_nexus_mod_page_url(game_domain.strip(), mid) or ""
        ).strip()
        if not mod_url:
            _gd = (game_domain or "").strip().strip("/").lower()
            if _gd:
                mod_url = f"https://www.nexusmods.com/{_gd}/mods/{mid}"
        if mod_url:
            QDesktopServices.openUrl(QUrl(mod_url))

    def _on_skse_open_game_folder_clicked(self) -> None:
        org = self._organizer_getter()
        game_path = self._organizer_game_directory(org)
        ctx = self._guide_context
        if isinstance(ctx, dict) and not (game_path or "").strip():
            game_path = str(ctx.get("game_directory") or "").strip()
        game_path = (game_path or "").strip()
        if not game_path:
            _gl = game_folder_short_label_for_organizer(org)
            QMessageBox.warning(
                self,
                self._tr.tr("chat.window_title"),
                self._tr.tr("guide.msg_skse_game_path_unknown", game=_gl),
            )
            return
        try:
            os.startfile(game_path)
        except OSError as exc:
            _hard_log(f"[SE] os.startfile failed: {exc!r}")
            QMessageBox.warning(
                self,
                self._tr.tr("chat.window_title"),
                self._tr.tr("guide.msg_skse_open_folder_failed"),
            )

    def _on_skse_verify_clicked(self) -> None:
        self._guide_skse_try_verify_and_advance()

    def _append_guide_step_message(self) -> None:
        ctx = self._guide_context
        if ctx is None:
            self._nemesis_launch_clear_offer_state()
            return
        pending = self._guide_pending_items()
        idx = self._guide_prereq_index
        if idx < 0 or idx >= len(pending):
            self._nemesis_launch_clear_offer_state()
            return
        entry_idx = idx
        entry_flat = pending[entry_idx]
        name = _strip_priority_markers_from_label(
            str(entry_flat.get("label") or "").strip()
        ) or self._tr.tr("guide.name_none")
        nid = entry_flat.get("nexus_mod_id")
        python_hard_log(f"[ROUTER DBG] 진입 idx={entry_idx} name={name!r} nid={nid!r}")
        intercept_j: int | None = None
        intro = ""
        organizer = self._organizer_getter()
        _, game_version_str = _read_game_version_for_counselor(organizer)
        try:
            current_mod_id = int(entry_flat.get("nexus_mod_id") or 0)
        except (TypeError, ValueError):
            current_mod_id = 0
        child_nodes = self._dependency_graph.get(current_mod_id, [])
        child_ids = _dependency_graph_child_ids(child_nodes)
        _hard_log(
            f"[ROUTER DBG] SSOT 참조: {current_mod_id}의 실제 선행 모드 -> {child_ids}"
        )
        if (
            organizer is not None
            and isinstance(ctx, dict)
            and current_mod_id > 0
            and child_ids
        ):
            active_nexus_ids = self._guide_effective_active_nexus_ids(organizer)
            for cid in child_ids:
                if int(cid) in active_nexus_ids:
                    continue
                row_i = _work_queue_index_for_nexus_id(pending, cid)
                if row_i is None:
                    _hard_log(
                        f"[ROUTER DBG] child_id={cid} 미설치+큐 누락 (경고)"
                    )
                    continue
                j = row_i
                if j < entry_idx:
                    intercept_j = j
                    break
        _hard_log(f"[ROUTER DBG] intercept_j={intercept_j!r}")
        j = intercept_j
        if j is not None and j < entry_idx:
            saved_lab = _strip_priority_markers_from_label(
                str(pending[entry_idx].get("label") or "").strip()
            )
            need_lab = _strip_priority_markers_from_label(
                str(pending[j].get("label") or "").strip()
            )
            saved_nid = pending[entry_idx].get("nexus_mod_id")
            need_nid = pending[j].get("nexus_mod_id")
            self._guide_stack.append(
                {
                    "_intercept": True,
                    "saved_index": entry_idx,
                    "target_label": saved_lab,
                    "target_nexus_mod_id": saved_nid,
                }
            )
            _hard_log(
                f"[ROUTER DIAG] intercept hold {saved_lab!r} (id={saved_nid!r}) -> "
                f"guide_first {need_lab!r} (id={need_nid!r}) "
                f"(stack={len(self._guide_stack)})"
            )
            self._guide_prereq_index = j
            idx = j
            ss = html.escape(saved_lab or self._tr.tr("guide.name_none"), quote=False)
            sn = html.escape(need_lab or self._tr.tr("guide.name_none"), quote=False)
            intro = self._tr.tr("guide.intercept_need_first", saved=ss, need=sn)
        pfx = intro
        item = pending[idx]
        name = _strip_priority_markers_from_label(
            str(item.get("label") or "").strip()
        ) or self._tr.tr("guide.name_none")
        nid = item.get("nexus_mod_id")
        try:
            step_mod_id = int(item.get("nexus_mod_id") or 0)
        except (TypeError, ValueError):
            step_mod_id = 0
        step_child_nodes = self._dependency_graph.get(step_mod_id, [])
        step_child_ids = _dependency_graph_child_ids(step_child_nodes)
        deps_all_satisfied_for_hint = False
        if organizer is not None and step_mod_id > 0 and step_child_ids:
            _aids_hint = self._guide_effective_active_nexus_ids(organizer)
            deps_all_satisfied_for_hint = all(
                int(cid) in _aids_hint for cid in step_child_ids
            )
        _gv_disp = html.escape(
            (game_version_str or "").strip() or self._tr.tr("guide.version_unknown"),
            quote=False,
        )
        _files_hint_lines = [
            self._tr.tr("guide.files_tab_hint", version=_gv_disp),
        ]
        if not step_child_ids:
            _files_hint_lines.append(self._tr.tr("guide.files_no_prereq"))
        elif deps_all_satisfied_for_hint:
            _files_hint_lines.append(self._tr.tr("guide.files_prereq_ok"))
        _files_tab_hint_html = (
            "<p style='margin-top:6px'>" + "<br/>".join(_files_hint_lines) + "</p>"
        )
        game_domain = str(ctx.get("game_domain") or self._game_domain)
        gd_step = self._organizer_game_directory(organizer)
        game_dir_for_guide: str | None = (gd_step or "").strip() or None
        if not game_dir_for_guide:
            game_dir_for_guide = str(ctx.get("game_directory") or "").strip() or None
        n_total = len(pending)
        k = idx + 1
        safe_name = html.escape(name, quote=False)
        progress = self._tr.tr("guide.progress", total=n_total, k=k)
        first_mandatory = (
            self._guide_phase == "mandatory" and idx == 0 and not intro
        )

        or_segments = _parse_or_guide_segments(str(item.get("label") or ""))
        if not (
            is_script_extender_guide_nexus_id(step_mod_id)
            and not or_segments
            and not bool(item.get("is_external"))
        ):
            self._guide_skse_manual_step_active = False
            self._guide_skse_mo2_exec_verify_pending = False
        if (
            is_script_extender_guide_nexus_id(step_mod_id)
            and not or_segments
            and not bool(item.get("is_external"))
            and script_extender_step_loader_installed_in_game_dir(
                nexus_mod_id=step_mod_id,
                game_dir=game_dir_for_guide,
                loader_basename=script_extender_loader_basename_for_organizer(
                    organizer
                ),
            )
        ):
            _lb = script_extender_loader_basename_for_organizer(organizer)
            _hard_log(
                "[ROUTER DBG] Script extender skip gate: file_in_game_dir=True "
                f"loader={_lb!r} nid={step_mod_id}"
            )
            self._guide_installed_nexus_override.add(step_mod_id)
            self._append_html(
                self._tr.tr("guide.skse_skip", name=safe_name),
            )
            clean_skip = _strip_priority_markers_from_label(
                str(item.get("label") or "").strip()
            )
            self._guide_advance_prereq_queue_after_success(clean_skip)
            self._nemesis_launch_clear_offer_state()
            return
        if (
            is_script_extender_guide_nexus_id(step_mod_id)
            and not or_segments
            and not bool(item.get("is_external"))
        ):
            self._guide_awaiting_mod_id = False
            self._guide_awaiting_install_signal = True
            self._guide_skse_manual_step_active = True
            _ldr = html.escape(
                script_extender_loader_basename_for_organizer(organizer),
                quote=False,
            )
            body = self._tr.tr("guide.skse_manual_intro", loader=_ldr)
            skse_link_html, _ = _nexus_mod_page_link_html_and_url(
                game_domain, nid
            )
            skse_link_block = ""
            if skse_link_html:
                skse_link_block = (
                    self._tr.tr("guide.download_from_link") + f"{skse_link_html}<br/>"
                )
            if first_mandatory:
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.mandatory_count", n=n_total)
                    + self._tr.tr("guide.in_order")
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + skse_link_block
                    + body
                    + _files_tab_hint_html
                    + f"<br/>{progress}</p>"
                )
            else:
                self._append_html(
                    pfx
                    + self._tr.tr("guide.next_need_para", name=safe_name)
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + skse_link_block
                    + body
                    + _files_tab_hint_html
                    + f"<br/>{progress}</p>"
                )
            self._nemesis_launch_clear_offer_state()
            self._sync_skse_manual_inline_buttons(
                step_mod_id=step_mod_id,
                or_segments=or_segments,
                item=item,
            )
            return
        if or_segments and not bool(item.get("is_external")):
            summary = html.escape(_or_step_summary_title(or_segments), quote=False)
            or_links = _build_or_alternatives_links_html(game_domain, or_segments)
            self._guide_awaiting_mod_id = False
            self._guide_awaiting_install_signal = True
            if first_mandatory:
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.mandatory_count", n=n_total)
                    + self._tr.tr("guide.in_order")
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{summary}</b><br/>"
                    + self._tr.tr("guide.pick_or_link")
                    + f"{or_links}"
                    + f"{_files_tab_hint_html}<br/>"
                    + f"{progress}</p>"
                )
            else:
                self._append_html(
                    pfx
                    + self._tr.tr("guide.next_need_para", name=summary)
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{summary}</b><br/>"
                    + self._tr.tr("guide.pick_or_link")
                    + f"{or_links}"
                    + f"{_files_tab_hint_html}<br/>"
                    + f"{progress}</p>"
                )
            self._nemesis_launch_clear_offer_state()
            return

        link_html, mod_url = _nexus_mod_page_link_html_and_url(game_domain, nid)
        mid_for_link: int | None = None
        if nid is not None:
            try:
                _mid = int(nid)
                if _mid > 0:
                    mid_for_link = _mid
            except (TypeError, ValueError):
                mid_for_link = None

        if bool(item.get("is_external")) and nid is not None:
            try:
                mid_for_url = int(nid)
            except (TypeError, ValueError):
                mid_for_url = 0
            if mid_for_url > 0:
                mod_url = (
                    build_nexus_mod_page_url(game_domain.strip(), mid_for_url) or ""
                ).strip()
                if not mod_url:
                    dom = (game_domain or "").strip().strip("/").lower()
                    if dom:
                        mod_url = f"https://www.nexusmods.com/{dom}/mods/{mid_for_url}"
                safe_url = html.escape(mod_url, quote=True)
                url_visible = html.escape(mod_url, quote=False)
                self._guide_awaiting_mod_id = False
                self._guide_awaiting_install_signal = True
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.external_warn_name", name=safe_name)
                    + self._tr.tr("guide.external_maybe_program")
                    + self._tr.tr("guide.external_skip_if_have")
                    + self._tr.tr("guide.external_download_if_need")
                    + self._tr.tr("guide.link_label")
                    + f'<a href="{safe_url}" style="color:#1565c0;">{url_visible}</a><br/>'
                    f"{_files_tab_hint_html}<br/>"
                    f"{progress}</p>"
                )
                self._nemesis_launch_clear_offer_state()
                return
        if first_mandatory:
            if link_html:
                self._guide_awaiting_mod_id = False
                self._guide_awaiting_install_signal = True
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.mandatory_count", n=n_total)
                    + self._tr.tr("guide.in_order")
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + self._tr.tr("guide.download_from_link")
                    + f"{link_html}<br/>"
                    f"{_files_tab_hint_html}<br/>"
                    f"{progress}</p>"
                )
            elif nid is not None and mid_for_link is not None and not mod_url:
                self._guide_awaiting_mod_id = True
                self._guide_awaiting_install_signal = False
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.mandatory_count", n=n_total)
                    + self._tr.tr("guide.in_order")
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + self._tr.tr("guide.cannot_build_mod_url")
                    + self._tr.tr("guide.enter_nexus_mod_id")
                    + self._tr.tr("guide.url_tail_is_id")
                    + f"{progress}</p>"
                )
            elif nid is not None:
                self._guide_awaiting_mod_id = True
                self._guide_awaiting_install_signal = False
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.mandatory_count", n=n_total)
                    + self._tr.tr("guide.in_order")
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + self._tr.tr("guide.enter_nexus_mod_id")
                    + self._tr.tr("guide.url_tail_is_id")
                    + f"{progress}</p>"
                )
            else:
                url = _nexus_search_url_for_query(game_domain, name)
                safe_href = html.escape(url, quote=True)
                self._guide_awaiting_mod_id = False
                self._guide_awaiting_install_signal = True
                self._append_html(
                    pfx
                    + "<p>"
                    + self._tr.tr("guide.mandatory_count", n=n_total)
                    + self._tr.tr("guide.in_order")
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + self._tr.tr("guide.search_install_on_nexus", name=safe_name)
                    + f'<a href="{safe_href}" style="color:#1565c0;">'
                    + html.escape(self._tr.tr("guide.search_quick_link"), quote=False)
                    + "</a><br/>"
                    + self._tr.tr("guide.say_installed_when_done")
                    + f"{progress}</p>"
                )
            if (
                step_mod_id == NEMESIS_NEXUS_ID
                and not or_segments
                and not bool(item.get("is_external"))
            ):
                self._nemesis_launch_clear_offer_state()
            else:
                self._sync_nemesis_launch_button(
                    step_mod_id=step_mod_id,
                    or_segments=or_segments,
                    item=item,
                )
            return
        if nid is not None:
            if link_html:
                self._guide_awaiting_mod_id = False
                self._guide_awaiting_install_signal = True
                self._append_html(
                    pfx
                    + self._tr.tr("guide.next_need_para", name=safe_name)
                    + self._tr.tr("guide.step_k", k=k)
                    + f"<b>{safe_name}</b><br/>"
                    + self._tr.tr("guide.download_from_link")
                    + f"{link_html}<br/>"
                    f"{_files_tab_hint_html}<br/>"
                    f"{progress}</p>"
                )
            elif mid_for_link is not None and not mod_url:
                self._guide_awaiting_mod_id = True
                self._guide_awaiting_install_signal = False
                self._append_html(
                    pfx
                    + self._tr.tr("guide.next_need_short", name=safe_name)
                    + self._tr.tr("guide.cannot_build_mod_url")
                    + self._tr.tr("guide.enter_nexus_mod_id")
                    + self._tr.tr("guide.url_tail_is_id")
                    + f"{progress}</p>"
                )
            else:
                self._guide_awaiting_mod_id = True
                self._guide_awaiting_install_signal = False
                self._append_html(
                    pfx
                    + self._tr.tr("guide.next_need_short", name=safe_name)
                    + self._tr.tr("guide.enter_nexus_mod_id")
                    + self._tr.tr("guide.url_tail_is_id")
                    + f"{progress}</p>"
                )
        else:
            url = _nexus_search_url_for_query(game_domain, name)
            safe_href = html.escape(url, quote=True)
            self._guide_awaiting_mod_id = False
            self._guide_awaiting_install_signal = True
            self._append_html(
                pfx
                + self._tr.tr("guide.next_need_short", name=safe_name)
                + self._tr.tr("guide.search_install_on_nexus", name=safe_name)
                + f'<a href="{safe_href}" style="color:#1565c0;">'
                + html.escape(self._tr.tr("guide.search_quick_link"), quote=False)
                + "</a><br/>"
                + self._tr.tr("guide.say_installed_when_done")
                + f"{progress}</p>"
            )
        if (
            step_mod_id == NEMESIS_NEXUS_ID
            and not or_segments
            and not bool(item.get("is_external"))
        ):
            self._nemesis_launch_clear_offer_state()
        else:
            self._sync_nemesis_launch_button(
                step_mod_id=step_mod_id,
                or_segments=or_segments,
                item=item,
            )

    def _guide_scan_worker_matches_context(self, worker: ScanWorker | None) -> bool:
        """True if ``self._guide_context['main_mod_id']`` matches this scan worker target."""
        if worker is None:
            return False
        ctx = self._guide_context
        if not isinstance(ctx, dict):
            return False
        try:
            expected = int(ctx.get("main_mod_id") or 0)
            got = int(getattr(worker, "_target_mod_id", 0))
        except (TypeError, ValueError):
            return False
        if expected <= 0 or got <= 0:
            return False
        if expected != got:
            _hard_log(
                f"[GUIDE DBG] scan callback mismatch: context main_mod_id={expected} "
                f"ScanWorker target={got} → ignore"
            )
            return False
        return True

    def _id_search_worker_matches_guide_ctx_main_id(self, ctx_main_id: int) -> bool:
        """True if ``guide_context_ready`` main id matches the active IdSearchWorker."""
        w = self._id_search_worker
        if w is None:
            return True
        try:
            wid = int(w._mod_id)
        except (TypeError, ValueError):
            return False
        if ctx_main_id <= 0:
            return False
        if wid != ctx_main_id:
            _hard_log(
                f"[GUIDE DBG] guide_context_ready mismatch: ctx main_mod_id={ctx_main_id} "
                f"IdSearchWorker mod_id={wid} → ignore"
            )
            return False
        return True

    def _start_guide_scan_worker(self) -> None:
        ctx = self._guide_context
        if not isinstance(ctx, dict):
            self._append_system(self._tr.tr("guide.ctx_missing"))
            return
        if self._guide_scan_worker is not None and self._guide_scan_worker.isRunning():
            self._append_system(self._tr.tr("guide.scan_running"))
            return
        mid = int(ctx.get("main_mod_id") or 0)
        if mid <= 0:
            self._append_system(self._tr.tr("guide.no_main_mod_id"))
            return
        gd = str(ctx.get("game_domain") or self._game_domain or "").strip()
        visited: dict[int, Any] = {}
        install_queue: list[dict[str, Any]] = []
        worker = ScanWorker(
            target_mod_id=mid,
            visited_mods=visited,
            install_queue=install_queue,
            game_domain=gd,
            parent=self,
        )
        self._guide_scan_worker = worker
        self._begin_guide_scan_status_line()
        worker.or_branch_signal.connect(self._on_guide_scan_or_branch)
        worker.finished_ok.connect(self._on_guide_scan_finished)
        worker.failed.connect(self._on_guide_scan_failed)
        worker.finished.connect(self._on_guide_scan_cleanup)
        worker.start()

    def _on_guide_scan_or_branch(self, options: object) -> None:
        worker = self._guide_scan_worker
        if worker is None:
            return
        if not self._guide_scan_worker_matches_context(worker):
            worker.apply_or_choice(0)
            return
        if not isinstance(options, list) or not options:
            worker.apply_or_choice(0)
            return

        opt_ids: list[int] = []
        for o in options:
            if isinstance(o, dict) and o.get("id") is not None:
                try:
                    opt_ids.append(int(o["id"]))
                except (TypeError, ValueError):
                    continue
        if FNIS_NEXUS_ID in opt_ids and NEMESIS_NEXUS_ID in opt_ids:
            nem_idx: int | None = None
            for i, o in enumerate(options):
                if not isinstance(o, dict):
                    continue
                try:
                    if int(o.get("id") or 0) == NEMESIS_NEXUS_ID:
                        nem_idx = i
                        break
                except (TypeError, ValueError):
                    continue
            if nem_idx is None:
                worker.apply_or_choice(0)
                return
            discarded_ids: list[int] = []
            for j, o in enumerate(options):
                if j == nem_idx:
                    continue
                if not isinstance(o, dict):
                    continue
                try:
                    oid = int(o.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if oid > 0:
                    discarded_ids.append(oid)
            for d in discarded_ids:
                self._guide_installed_nexus_override.add(d)
            self._append_system(self._tr.tr("guide.fnis_use_nemesis"))
            _hard_log(
                f"[OR DIAG] FNIS/NEMESIS OR 자동 선택 → NEMESIS "
                f"(idx={nem_idx}, 폐기 id={discarded_ids})"
            )
            worker.apply_or_choice(nem_idx)
            return

        option_titles: list[str] = []
        for o in options:
            if isinstance(o, dict):
                lab = str(o.get("label") or "").strip() or f"Mod {o.get('id')}"
                option_titles.append(lab)
            else:
                option_titles.append(str(o))
        _hard_log(f"[UI DBG] 대안 선택 팝업 호출됨: {option_titles}")

        dlg = AlternativeSelectorDialog(self, options)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            worker.abort_or_branch()
            return
        idx = dlg.selected_index()
        if idx is None or idx < 0 or idx >= len(options):
            worker.abort_or_branch()
            return

        discarded_ids: list[int] = []
        for j, o in enumerate(options):
            if j == idx:
                continue
            if not isinstance(o, dict):
                continue
            try:
                oid = int(o.get("id"))
            except (TypeError, ValueError):
                continue
            if oid > 0:
                discarded_ids.append(oid)
        sel_raw = options[idx] if idx < len(options) else None
        try:
            sel_id = int(sel_raw.get("id")) if isinstance(sel_raw, dict) else 0
        except (TypeError, ValueError):
            sel_id = 0
        _hard_log(
            f"[UI DBG] 유저 선택 완료 - 채택: {sel_id} / "
            f"영구 폐기(블랙리스트): {discarded_ids}"
        )
        for d in discarded_ids:
            self._guide_installed_nexus_override.add(d)
        worker.apply_or_choice(idx)

    def _on_guide_scan_finished(self, payload: object) -> None:
        ctx = self._guide_context
        worker = self._guide_scan_worker
        if not isinstance(ctx, dict):
            self._resolve_guide_scan_status_line(replace_with=None)
            return
        if worker is None or not self._guide_scan_worker_matches_context(worker):
            self._resolve_guide_scan_status_line(replace_with=None)
            return
        self._resolve_guide_scan_status_line(
            replace_with=self._tr.tr("guide.scan_done")
        )
        main_id = int(ctx.get("main_mod_id") or 0)
        raw_q: list[Any] = []
        graph_raw: object = {}
        if isinstance(payload, dict):
            raw_q = list(payload.get("queue") or [])
            graph_raw = payload.get("graph") or {}
        elif isinstance(payload, list):
            raw_q = list(payload)
        self._dependency_graph = _coerce_scan_dependency_graph(graph_raw)
        raw_q = [
            x
            for x in raw_q
            if not (
                isinstance(x, dict) and int(x.get("id", 0)) in _VR_ID_BLOCKLIST
            )
        ]
        for vid in _VR_ID_BLOCKLIST:
            self._dependency_graph.pop(vid, None)
        for k in self._dependency_graph:
            self._dependency_graph[k] = [
                c
                for c in self._dependency_graph[k]
                if int(c.get("id", 0)) not in _VR_ID_BLOCKLIST
            ]
        _rewrite_fnis_to_nemesis_dependency_graph(self._dependency_graph)
        _hard_log(
            f"[ROUTER DBG] 글로벌 필터 발동! VR ID 차단: {_VR_ID_BLOCKLIST}"
        )
        rows: list[tuple[int, str, bool]] = []
        for item in raw_q:
            if isinstance(item, dict):
                raw_id = item.get("id")
                if isinstance(raw_id, dict):
                    continue
                try:
                    nid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if nid <= 0:
                    continue
                name = str(item.get("name") or "").strip() or f"Mod {nid}"
                ext = bool(item.get("is_external"))
                rows.append((nid, name, ext))
            else:
                try:
                    nid = int(item)
                except (TypeError, ValueError):
                    continue
                if nid <= 0:
                    continue
                rows.append((nid, f"Mod {nid}", False))
        prereq_rows = [
            (nid, name, ext)
            for nid, name, ext in rows
            if nid > 0 and nid != main_id
        ]
        fnis_swapped_queue = False
        new_prereq_rows: list[tuple[int, str, bool]] = []
        for nid, name, ext in prereq_rows:
            if nid == FNIS_NEXUS_ID:
                fnis_swapped_queue = True
                self._guide_installed_nexus_override.add(FNIS_NEXUS_ID)
                new_prereq_rows.append(
                    (
                        NEMESIS_NEXUS_ID,
                        "Nemesis Unlimited Behavior Engine",
                        ext,
                    )
                )
            else:
                new_prereq_rows.append((nid, name, ext))
        if fnis_swapped_queue:
            self._append_system(self._tr.tr("guide.fnis_use_nemesis"))
        prereq_rows = new_prereq_rows
        ctx["pending_prereq_items"] = [
            normalize_prereq_item_dict(
                {
                    "label": name,
                    "nexus_mod_id": nid,
                    "children": [],
                    "priority": "MANDATORY",
                    **({"is_external": True} if ext else {}),
                }
            )
            for nid, name, ext in prereq_rows
        ]
        inject_script_extender_prereq_if_missing(
            ctx, self._organizer_getter()
        )
        self._begin_guide_flow()

    def _on_guide_scan_failed(self, message: str, connection: bool) -> None:
        _ = connection
        self._resolve_guide_scan_status_line(replace_with=None)
        if not self._guide_scan_worker_matches_context(self._guide_scan_worker):
            return
        self._append_system(message or self._tr.tr("guide.scan_failed"))

    def _on_guide_scan_cleanup(self) -> None:
        self._resolve_guide_scan_status_line(replace_with=None)
        w = self._guide_scan_worker
        self._guide_scan_worker = None
        if w is not None:
            w.deleteLater()

    def _begin_guide_flow(self) -> None:
        _hard_log("[SEND DBG] _begin_guide_flow 진입")
        ctx = self._guide_context
        if ctx is None:
            self._append_system(self._tr.tr("guide.ctx_missing"))
            return
        self._guide_awaiting_main_mod_nexus_id = 0
        self._guide_completed_prereqs = []
        self._guide_phase = "mandatory"
        self._guide_mcm_queue = list(ctx.get("mcm_pending_flat") or [])
        self._guide_awaiting_mcm_prompt = False
        self._guide_optional_labels = list(ctx.get("optional_prereq_labels") or [])
        self._guide_advanced_labels = list(ctx.get("advanced_prereq_labels") or [])
        self._rebuild_guide_work_queue()
        pending = self._guide_pending_items()
        _hard_log(
            f"[LAMP DBG] _begin_guide_flow 진입 pending={len(pending)} "
            f"main_mod_id={ctx.get('main_mod_id')!r}"
        )
        main_name = str(ctx.get("main_mod_name") or "").strip() or self._tr.tr(
            "guide.name_none"
        )
        if not pending:
            _hard_log(
                "[LAMP] _begin_guide_flow: pending 비어 있음 → _guide_lamp_on 미호출 "
                "(필수 사전 모드 없음·이미 설치됨 등)"
            )
            safe = html.escape(main_name, quote=False)
            self._append_html(
                "<p><b>"
                f"{html.escape(self._tr.tr('guide.prereqs_already_title'), quote=False)}</b><br/>"
                f"{self._tr.tr('guide.prereqs_already_body', name=safe)}</p>"
            )
            self._append_optional_advanced_hints()
            self._nemesis_launch_clear_offer_state()
            mid0 = _main_mod_nexus_id_from_ctx(ctx)
            self._guide_awaiting_main_mod_nexus_id = mid0
            if mid0 > 0:
                _hard_log(
                    f"[LOOT/INSTALL] no prereqs path: awaiting main mod install nexus_id={mid0}"
                )
            else:
                _hard_log(
                    "[LOOT/INSTALL] no prereqs path: main_mod_id missing; LOOT hook disabled"
                )
            self._guide_context = None
            self._guide_reset_install_nexus_override()
            self._guide_work_queue = []
            self._guide_completed_prereqs = []
            self._guide_mcm_queue = []
            self._guide_optional_labels = []
            self._guide_advanced_labels = []
            self._dependency_graph = {}
            self._guide_skse_manual_step_active = False
            self._guide_skse_mo2_exec_verify_pending = False
            return
        self._guide_prereq_index = 0
        self._guide_awaiting_mod_id = False
        self._guide_awaiting_install_signal = False
        self._guide_mode_paused = False
        self._guide_need_resume_confirm = False
        self._guide_skse_manual_step_active = False
        self._guide_skse_mo2_exec_verify_pending = False
        _hard_log(
            f"[LAMP] _begin_guide_flow: pending={len(pending)} main_name={main_name!r} → _guide_lamp_on 예정"
        )
        self._guide_lamp_on(main_name)

        # ── 새 가이드 스택 초기화 ──
        self._guide_stack.clear()
        if self._guide_context:
            self._guide_stack.append(
                {
                    "mod_id": self._guide_context.get("main_mod_id", 0),
                    "mod_name": self._guide_context.get("main_mod_name", ""),
                    "pending": list(self._guide_context.get("pending_prereq_items", [])),
                    "completed": [],
                    "current_index": 0,
                }
            )
        self._append_guide_step_message()

    def _start_guide_step_worker(self, mod_id: int) -> None:
        """가이드 중 숫자 입력: 전체 ID 검색 없이 짧은 안내만 표시. ``mod_id``는 검증용(표시 생략)."""
        ctx = self._guide_context
        if ctx is None:
            return
        _ = mod_id  # 유효 범위는 _on_send에서 이미 검사함
        main_name = str(ctx.get("main_mod_name") or "").strip()
        items = self._guide_pending_items()
        idx = self._guide_prereq_index
        if idx < 0 or idx >= len(items):
            return
        prereq_name = _strip_priority_markers_from_label(str(items[idx].get("label") or "").strip())
        if self._id_search_btn is not None:
            self._id_search_btn.setEnabled(False)
        worker = GuideStepWorker(
            main_mod_name=main_name,
            current_prereq_name=prereq_name,
            parent=self,
        )
        self._guide_step_worker = worker
        worker.finished_ok.connect(self._on_guide_step_ok)
        worker.failed.connect(self._on_guide_step_failed)
        worker.finished.connect(self._on_guide_step_cleanup)
        worker.start()

    def _on_guide_step_ok(self, html_block: str) -> None:
        self._append_html(html_block)
        self._guide_awaiting_mod_id = False
        self._guide_awaiting_install_signal = True

    def _on_guide_step_failed(self, message: str, connection: bool) -> None:
        _ = connection
        self._append_system(message or self._tr.tr("guide.step_failed"))

    def _on_guide_step_cleanup(self) -> None:
        if self._id_search_btn is not None:
            self._id_search_btn.setEnabled(True)
        w = self._guide_step_worker
        self._guide_step_worker = None
        if w is not None:
            w.deleteLater()

    def _guide_advance_prereq_queue_after_success(self, clean: str) -> None:
        """현재 단계를 완료 처리한 뒤 인덱스를 올리고, 가이드 종료 또는 다음 안내를 연다."""
        pending = self._guide_pending_items()
        idx = self._guide_prereq_index
        if clean:
            self._guide_completed_prereqs.append(clean)
        idx += 1
        self._guide_prereq_index = idx
        self._guide_awaiting_install_signal = False
        if idx >= len(pending):
            if self._guide_phase == "mcm":
                self._finish_guide_main_mod_ready()
            elif self._guide_phase == "mandatory" and self._guide_mcm_queue:
                self._guide_phase = "mcm_prompt"
                self._guide_awaiting_mcm_prompt = True
                self._guide_awaiting_install_signal = False
                self._append_html(self._tr.tr("guide.mcm_prompt"))
            else:
                self._finish_guide_main_mod_ready()
        else:
            while (
                self._guide_stack
                and isinstance(self._guide_stack[-1], dict)
                and self._guide_stack[-1].get("_intercept") is True
            ):
                top = self._guide_stack[-1]
                si = top.get("saved_index")
                try:
                    si_i = int(si) if si is not None else -1
                except (TypeError, ValueError):
                    si_i = -1
                if si_i != idx:
                    break
                self._guide_stack.pop()
                restored = str(top.get("target_label") or "").strip()
                safe = html.escape(
                    _strip_priority_markers_from_label(restored),
                    quote=False,
                )
                rid = top.get("target_nexus_mod_id")
                _hard_log(
                    f"[ROUTER DIAG] resume target {restored!r} (id={rid!r}) "
                    f"(stack={len(self._guide_stack)})"
                )
                self._append_html(
                    self._tr.tr("guide.resume_main_after_prereq", name=safe)
                )
                break
            _hard_log(
                "[ROUTER DBG] advance_prereq_queue: calling _append_guide_step_message "
                f"(new_idx={self._guide_prereq_index})"
            )
            self._append_guide_step_message()

    def _guide_try_advance_after_install_claim(
        self, *, mo2_installed_nexus_id: int | None = None
    ) -> None:
        ctx = self._guide_context
        if ctx is None:
            self._guide_awaiting_install_signal = False
            _hard_log("[ROUTER DBG] try_advance_after_install: abort (no guide context)")
            return
        organizer = self._organizer_getter()
        if organizer is None:
            self._append_system(self._tr.tr("nexus.organizer_unavailable"))
            _hard_log("[ROUTER DBG] try_advance_after_install: abort (organizer None)")
            return
        active_names = _active_mod_display_names(organizer)
        active_nexus_ids = self._guide_effective_active_nexus_ids(organizer)
        gd = self._organizer_game_directory(organizer)
        game_dir: str | None = gd or None
        if not (gd or "").strip():
            game_dir = str(ctx.get("game_directory") or "").strip() or None

        pending = self._guide_pending_items()
        idx = self._guide_prereq_index
        if idx < 0 or idx >= len(pending):
            self._guide_awaiting_install_signal = False
            _hard_log(
                f"[ROUTER DBG] try_advance_after_install: abort (bad idx={idx}, "
                f"pending_len={len(pending)})"
            )
            return
        clean = _strip_priority_markers_from_label(str(pending[idx].get("label") or "").strip())
        raw_nid = pending[idx].get("nexus_mod_id")
        try:
            node_nid = int(raw_nid) if raw_nid is not None else 0
        except (TypeError, ValueError):
            node_nid = 0
        if self._guide_skse_mo2_exec_verify_pending and is_script_extender_guide_nexus_id(
            node_nid
        ):
            self._guide_skse_try_mo2_exec_verify_and_advance()
            return
        if self._guide_skse_manual_step_active and is_script_extender_guide_nexus_id(
            node_nid
        ):
            self._guide_skse_try_verify_and_advance()
            return
        target_ids = self._guide_current_install_target_nexus_ids()
        name_satisfied = _label_install_satisfied(
            clean,
            active_names,
            game_dir,
            active_nexus_ids=active_nexus_ids,
            node_nexus_id=node_nid,
        )
        mo2_claim = int(mo2_installed_nexus_id) if mo2_installed_nexus_id is not None else 0
        mo2_trust = mo2_claim > 0 and mo2_claim in target_ids
        satisfied = name_satisfied or mo2_trust
        in_active = node_nid > 0 and node_nid in active_nexus_ids
        _hard_log(
            "[ROUTER DBG] try_advance_after_install: "
            f"idx={idx} node_nid={node_nid} target_ids={sorted(target_ids)} "
            f"mo2_claim={mo2_claim or None} name_satisfied={name_satisfied} "
            f"mo2_trust={mo2_trust} node_in_active={in_active} -> advance={satisfied}"
        )
        if satisfied:
            self._guide_advance_prereq_queue_after_success(clean)
        else:
            _hard_log(
                "[ROUTER DBG] try_advance_after_install: blocked (no satisfied path) — "
                "user sees 'not recognized' system message"
            )
            self._append_system(self._tr.tr("guide.install_not_recognized"))

    def _start_id_search_worker(self, mod_id: int) -> None:
        organizer = self._organizer_getter()
        if organizer is None:
            self._append_system(self._tr.tr("nexus.organizer_unavailable"))
            return
        base_url = self._llama_base_url_getter()
        if not base_url:
            self._append_system(self._tr.tr("ai.portable_server_unavailable"))
            return
        api_key = read_nexus_api_key_from_mo2(
            organizer, fallback=self._nexus_api_key_getter()
        )
        if not (api_key or "").strip():
            self._append_system(self._tr.tr("chat.id_search_no_api_key"))
            return
        if self._id_search_btn is not None:
            self._id_search_btn.setEnabled(False)
        self._append_system(self._tr.tr("chat.id_search_analyzing"))
        active_names = _active_mod_display_names(organizer)
        active_nexus_ids = frozenset(_active_mod_nexus_ids(organizer))
        game_directory = self._organizer_game_directory(organizer)
        worker = IdSearchWorker(
            mod_id=mod_id,
            api_key=(api_key or "").strip(),
            game_domain=self._game_domain,
            application=self._application_getter(),
            base_url=base_url,
            active_mod_display_names=active_names,
            active_nexus_ids=active_nexus_ids,
            game_directory=game_directory,
            request_timeout=120.0,
            parent=self,
        )
        self._id_search_worker = worker
        worker.guide_context_ready.connect(self._on_id_search_guide_context)
        worker.finished_ok.connect(self._on_id_search_ok)
        worker.failed.connect(self._on_id_search_failed)
        worker.finished.connect(self._on_id_search_cleanup)
        worker.start()

    def _remove_guide_prompt_buttons(self) -> None:
        r = self._guide_prompt_buttons_range
        self._guide_prompt_buttons_range = None
        if r is None:
            return
        start, end = r
        doc = self._transcript.document()
        n = max(0, doc.characterCount())
        cur = QTextCursor(doc)
        cur.setPosition(min(start, n))
        cur.setPosition(min(end, n), QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()

    def _finish_guide_prompt_decline(self) -> None:
        self._guide_prompt_after_search = False
        self._nemesis_launch_clear_offer_state()
        self._guide_awaiting_main_mod_nexus_id = 0
        self._guide_context = None
        self._guide_reset_install_nexus_override()
        self._guide_prereq_index = 0
        self._guide_work_queue = []
        self._guide_completed_prereqs = []
        self._guide_phase = "mandatory"
        self._guide_mcm_queue = []
        self._guide_awaiting_mcm_prompt = False
        self._guide_optional_labels = []
        self._guide_advanced_labels = []
        self._guide_awaiting_mod_id = False
        self._guide_awaiting_install_signal = False
        self._guide_skse_manual_step_active = False
        self._guide_skse_mo2_exec_verify_pending = False
        self._guide_mode_paused = False
        self._guide_need_resume_confirm = False
        self._guide_stack.clear()
        self._dependency_graph = {}
        self._guide_lamp_off()
        self._append_assistant(self._tr.tr("guide.decline_ack"))

    def _finish_guide_prompt_accept(self) -> None:
        self._guide_prompt_after_search = False
        self._start_guide_scan_worker()

    def _on_guide_prompt_inline_yes(self) -> None:
        if not self._guide_prompt_after_search:
            return
        self._remove_guide_prompt_buttons()
        self._finish_guide_prompt_accept()

    def _on_guide_prompt_inline_no(self) -> None:
        if not self._guide_prompt_after_search:
            return
        self._remove_guide_prompt_buttons()
        self._finish_guide_prompt_decline()

    def _on_id_search_ok(self, text: str) -> None:
        self._guide_prompt_buttons_range = None
        if WEPAWN_GUIDE_PROMPT_FOOTER_MARKER in text:
            body, footer = text.split(WEPAWN_GUIDE_PROMPT_FOOTER_MARKER, 1)
            self._append_html(body)
            doc = self._transcript.document()
            cur = QTextCursor(doc)
            cur.movePosition(QTextCursor.MoveOperation.End)
            start = cur.position()
            cur.insertHtml(footer)
            cur.insertHtml("<br/>")
            end = cur.position()
            self._guide_prompt_buttons_range = (start, end)
            self._guide_prompt_after_search = True
        else:
            self._append_html(text)
            self._guide_prompt_after_search = False

    def _on_id_search_failed(self, message: str, connection: bool) -> None:
        if connection:
            self._append_system(self._tr.tr("ai.llm_connection_error", detail=message))
        else:
            self._append_system(self._tr.tr("chat.id_search_error", detail=message))

    def _on_id_search_cleanup(self) -> None:
        if self._id_search_btn is not None:
            self._id_search_btn.setEnabled(True)
        w = self._id_search_worker
        self._id_search_worker = None
        if w is not None:
            w.deleteLater()

    def _on_tier_llm(self) -> None:
        if self._tier_worker is not None and self._tier_worker.isRunning():
            return

        base_url = self._llama_base_url_getter()
        if not base_url:
            self._append_system(self._tr.tr("ai.portable_server_unavailable"))
            return

        organizer = self._organizer_getter()
        if organizer is None:
            self._append_system(self._tr.tr("nexus.organizer_unavailable"))
            return

        ml = organizer.modList()
        entries = _mod_list_selection_entries(organizer)
        if not entries:
            self._append_system(self._tr.tr("nexus.no_mod_selected"))
            return

        mod: mobase.IModInterface | None = None
        for entry in entries:
            candidate = _entry_to_mod(ml, entry)
            if candidate is None or candidate.isSeparator():
                continue
            mod = candidate
            break

        if mod is None:
            self._append_system(self._tr.tr("nexus.no_usable_mod_in_selection"))
            return

        mod_display = ml.displayName(mod.name())
        nexus_id = _coerce_positive_nexus_id(mod)
        _diag(
            f"TIER_UI resolved target internal={mod.name()!r} display={mod_display!r} "
            f"nexus_id={nexus_id}"
        )

        dep_lines: list[str] = []
        if nexus_id > 0:
            api_key = read_nexus_api_key_from_mo2(
                organizer, fallback=self._nexus_api_key_getter()
            )
            try:
                links = fetch_mod_file_dependencies(
                    api_key,
                    self._game_domain,
                    nexus_id,
                    0,
                    application=self._application_getter(),
                )
                dep_lines = [f"{link.name} (Nexus mod {link.mod_id}) — {link.url}" for link in links]
            except NexusAPIError as exc:
                dep_lines = [f"(Nexus API error while fetching dependencies: {exc})"]
        else:
            dep_lines = [
                "(This mod has no Nexus mod ID in MO2; Nexus dependency metadata was not fetched.)"
            ]

        enabled_mods = _active_mod_display_names(organizer)

        mo2_physical_context = collect_mo2_physical_diagnostics_text(organizer, mod, max_chars=2500)
        _diag(
            f"TIER_UI mo2_physical_context length={len(mo2_physical_context)} "
            f"text_preview={mo2_physical_context[:240]!r}"
        )

        nexus_page_url: str | None = None
        if nexus_id > 0:
            u = build_nexus_mod_page_url(self._game_domain, nexus_id).strip()
            nexus_page_url = u if u else None

        self._tier_pending_mod_display = mod_display
        self._tier_pending_mo2_physical_context = mo2_physical_context
        if self._tier_btn is not None:
            self._tier_btn.setEnabled(False)
        self._append_system(self._tr.tr("ai.tier_analyzing"))

        try:
            snap_game_name, snap_game_version = get_current_game_info(organizer)
        except Exception:
            snap_game_name, snap_game_version = "", ""
        worker = TierAnalysisWorker(
            mod_display,
            dep_lines,
            enabled_mods,
            base_url=base_url,
            request_timeout=90.0,
            nexus_mod_page_url=nexus_page_url,
            nexus_scrape_timeout=4.0,
            mo2_physical_context=mo2_physical_context,
            tier_nexus_id=nexus_id,
            nexus_game_domain=self._game_domain,
            current_game_version=(snap_game_version or "").strip(),
            current_game_name=(snap_game_name or "").strip(),
            parent=self,
        )
        self._tier_worker = worker
        worker.finished_ok.connect(self._on_tier_worker_ok)
        worker.failed.connect(self._on_tier_worker_failed)
        worker.finished.connect(self._on_tier_worker_cleanup)
        worker.start()

    def _on_tier_worker_ok(self, result: dict) -> None:
        mod_display = self._tier_pending_mod_display
        tier = str(result.get("tier", ""))
        reason = str(result.get("reason", ""))
        # Snapshot before link synthesis (cleanup may clear; same string regex uses).
        mo2_raw = self._tier_pending_mo2_physical_context or ""

        type_span, type_diag = _tier_grade_span_only(tier)
        reason_html = _format_tier_reason_html(reason)

        masters = extract_missing_master_filenames_from_mo2_context(mo2_raw)
        forced = html_forced_nexus_master_search_links(masters, self._game_domain)

        _diag(
            f"TIER_RENDER raw_tier_field={tier!r} heading_grade={type_diag!r} "
            f"reason_raw_prefix={reason[:240]!r} forced_master_count={len(masters)}"
        )
        label_reason = self._tr.tr("ai.tier_llm_reason_label")
        # Name must not be ``html`` — that shadows the stdlib ``html`` module and breaks
        # ``html.escape()`` above (UnboundLocalError).
        tier_block_html = (
            f"<p><b>{_esc(self._tr.tr('ai.tier_llm_heading'))}</b> "
            f"<span style=\"color:#666;\">({_esc(mod_display)})</span></p>"
            f"<p><b>{_esc(self._tr.tr('chat.tier_type_label'))}</b> {type_span}</p>"
            f"<p><b>{_esc(label_reason)}</b> {reason_html}</p>{forced}"
        )
        dump = (
            tier_block_html
            if len(tier_block_html) <= 6000
            else tier_block_html[:6000] + "…[truncated]"
        )
        _diag(f"TIER_RENDER final_html_for_transcript (dup_prefix_check)={dump!r}")
        self._append_html(tier_block_html)

    def _on_tier_worker_failed(self, message: str, raw: str, connection: bool) -> None:
        if connection:
            self._append_system(self._tr.tr("ai.llm_connection_error", detail=message))
            return
        raw_trim = (raw or "").strip()
        if raw_trim:
            self._append_system(
                self._tr.tr(
                    "ai.llm_parse_with_raw",
                    detail=message,
                    raw=raw_trim[:6000],
                )
            )
        else:
            self._append_system(self._tr.tr("ai.llm_parse_error", detail=message))

    def _on_tier_worker_cleanup(self) -> None:
        if self._tier_btn is not None:
            self._tier_btn.setEnabled(True)
        self._tier_pending_mo2_physical_context = ""
        w = self._tier_worker
        self._tier_worker = None
        if w is not None:
            w.deleteLater()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
