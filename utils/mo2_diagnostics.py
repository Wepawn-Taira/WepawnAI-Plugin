"""
Collect Mod Organizer physical state for a target mod (plugin origins, states, master issues).

Uses ``mobase`` APIs available at runtime (Gamebryo games). Fails soft on missing methods.
"""

from __future__ import annotations

import mobase


def _diag(msg: str) -> None:
    print(f"[WepawnAI DIAG] {msg}", flush=True)


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


def _plugin_state_label(st: object) -> str:
    try:
        iv = int(st)
    except Exception:
        return repr(st)
    for attr in (
        "STATE_ACTIVE",
        "STATE_INACTIVE",
        "STATE_MISSING",
        "STATE_NOLOAD",
    ):
        if hasattr(mobase.PluginState, attr):
            try:
                if iv == int(getattr(mobase.PluginState, attr)):
                    return attr
            except Exception:
                continue
    return f"raw={iv}"


def _mod_state_flags(ml: mobase.IModList, internal: str) -> str:
    try:
        st = ml.state(internal)
        iv = int(st)
    except Exception as exc:
        return f"unknown ({exc})"
    parts: list[str] = []
    for attr in dir(mobase.ModState):
        if not attr.isupper():
            continue
        try:
            flag = getattr(mobase.ModState, attr)
            if int(flag) & iv:
                parts.append(attr)
        except Exception:
            continue
    return ", ".join(parts) if parts else repr(st)


def _master_issues_for_plugin(pl: object, plugin_name: str) -> list[str]:
    issues: list[str] = []
    try:
        masters_fn = getattr(pl, "masters", None)
        if masters_fn is None or not callable(masters_fn):
            return issues
        masters_raw = masters_fn(plugin_name)
    except Exception:
        return issues

    if masters_raw is None:
        return issues
    try:
        master_list = list(masters_raw)
    except TypeError:
        return issues

    try:
        plugin_names = list(pl.pluginNames())
    except Exception:
        return issues

    by_lower: dict[str, str] = {}
    for p in plugin_names:
        if p is None:
            continue
        ps = str(p)
        by_lower.setdefault(ps.lower(), ps)

    missing_state = getattr(mobase.PluginState, "STATE_MISSING", None)
    inactive_state = getattr(mobase.PluginState, "STATE_INACTIVE", None)

    for m in master_list:
        if m is None:
            continue
        ms = str(m).strip()
        if not ms:
            continue
        key = ms.lower()
        if key not in by_lower:
            issues.append(f"{plugin_name}: required master not in MO2 plugin list — {ms}")
            continue
        canon = by_lower[key]
        try:
            mst = pl.state(canon)
            mlab = _plugin_state_label(mst)
        except Exception:
            issues.append(f"{plugin_name}: could not read state of master {ms}")
            continue
        if missing_state is not None:
            try:
                if int(mst) == int(missing_state):
                    issues.append(f"{plugin_name}: master {ms} is STATE_MISSING (file/mod issue)")
                    continue
            except Exception:
                pass
        if inactive_state is not None:
            try:
                if int(mst) == int(inactive_state):
                    issues.append(f"{plugin_name}: master {ms} is inactive in this profile")
            except Exception:
                pass

    return issues


def collect_mo2_physical_diagnostics_text(
    organizer: mobase.IOrganizer,
    target_mod: mobase.IModInterface,
    *,
    max_chars: int = 2500,
) -> str:
    """
    Build a plain-text block: mod-list flags for the folder + plugins whose ``origin`` is this mod,
    including master / missing-plugin style issues when detectable.
    """
    lines: list[str] = []
    ml = organizer.modList()
    internal = target_mod.name()
    display = ml.displayName(internal)

    lines.append(f"Target mod (internal / folder name): {internal}")
    lines.append(f"Target mod (display name): {display}")
    lines.append(f"Mod list state flags: {_mod_state_flags(ml, internal)}")

    pl_get = getattr(organizer, "pluginList", None)
    if pl_get is None or not callable(pl_get):
        lines.append("pluginList(): not available (non-Gamebryo or unsupported).")
        text = "\n".join(lines)
        _diag(f"MO2_PHYS context length={len(text)} (no plugin list)")
        return text[:max_chars]

    try:
        pl = pl_get()
    except Exception as exc:
        lines.append(f"pluginList() call failed: {exc}")
        text = "\n".join(lines)
        _diag(f"MO2_PHYS context length={len(text)} (pluginList error)")
        return text[:max_chars]

    try:
        all_plugins = list(pl.pluginNames())
    except Exception as exc:
        lines.append(f"pluginNames() failed: {exc}")
        text = "\n".join(lines)
        _diag(f"MO2_PHYS context length={len(text)} (pluginNames error)")
        return text[:max_chars]

    origin_fn = getattr(pl, "origin", None)
    if origin_fn is None or not callable(origin_fn):
        lines.append("origin() not available on plugin list.")
        text = "\n".join(lines)
        _diag(f"MO2_PHYS context length={len(text)} (no origin())")
        return text[:max_chars]

    origin_matches: list[str] = []
    for pname in all_plugins:
        if pname is None:
            continue
        ps = str(pname)
        try:
            orig = origin_fn(ps)
        except Exception:
            continue
        if orig is None:
            continue
        if str(orig).casefold() != internal.casefold():
            continue
        origin_matches.append(ps)

    if not origin_matches:
        lines.append(
            "No plugins in this profile report origin() equal to this mod folder "
            "(mod may be meta-only, texture-only, or non-Gamebryo layout)."
        )
    else:
        lines.append(f"Plugins originating from this mod ({len(origin_matches)}):")
        origin_matches.sort(key=str.lower)
        all_issues: list[str] = []
        for pname in origin_matches[:80]:
            try:
                st = pl.state(pname)
                st_lab = _plugin_state_label(st)
            except Exception as exc:
                st_lab = f"state error: {exc}"
            try:
                lo = pl.loadOrder(pname)
            except Exception:
                lo = "?"
            try:
                pri = pl.priority(pname)
            except Exception:
                pri = "?"
            lines.append(f"  - {pname} | plugin state={st_lab} | loadOrder={lo} | priority={pri}")
            all_issues.extend(_master_issues_for_plugin(pl, pname))

        if all_issues:
            lines.append("Detected master / plugin dependency issues (MO2 view):")
            for issue in all_issues[:60]:
                lines.append(f"  ! {issue}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n… [truncated]"
    _diag(f"MO2_PHYS context length={len(text)} plugins_from_mod={len(origin_matches)}")
    return text
