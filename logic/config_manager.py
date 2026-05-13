"""Onboarding and persisted client config (``wepawn_config.json``)."""

from __future__ import annotations

from typing import Any

import mobase

from ..utils.hard_log import _hard_log
from ..utils.wepawn_config import load_wepawn_config, save_wepawn_config_merged


def is_onboarding_done() -> bool:
    return bool(load_wepawn_config().get("is_onboarding_done"))


def get_selected_profile() -> str:
    raw = load_wepawn_config().get("selected_profile")
    return str(raw).strip() if raw is not None else ""


def get_saved_llama_base_url() -> str:
    raw = load_wepawn_config().get("onboarding_llama_base_url")
    return str(raw).strip().rstrip("/") if raw is not None else ""


def complete_onboarding(
    *,
    selected_profile: str,
    llama_base_url: str | None,
    nexus_api_key: str,
    plugin_name: str,
    organizer: mobase.IOrganizer | None,
) -> None:
    _hard_log("[CONFIG] 온보딩 최종 완료 및 데이터 쓰기 시작")
    patch: dict[str, Any] = {
        "is_onboarding_done": True,
        "selected_profile": (selected_profile or "").strip(),
    }
    if llama_base_url and str(llama_base_url).strip():
        patch["onboarding_llama_base_url"] = str(llama_base_url).strip().rstrip("/")
    save_wepawn_config_merged(patch)
    if organizer is not None and (nexus_api_key or "").strip():
        try:
            organizer.setPluginSetting(
                plugin_name, "nexus_api_key", str(nexus_api_key).strip()
            )
        except Exception as exc:
            _hard_log(f"[CONFIG] setPluginSetting nexus_api_key failed: {exc!r}")
