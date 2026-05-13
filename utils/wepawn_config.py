"""
``wepawn_config.json`` — UI 언어 등 경량 설정(캐시와 별도).

저장 위치는 :mod:`wepawn_storage` (MO2 ``basePath()/WepawnAI`` 권장).

MO2 ``pluginSetting``과 달리 사용자가 콤보에서 고른 ``selected_language``를
항상 우선하도록 이 파일을 먼저 읽는다.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from .wepawn_storage import wepawn_data_dir

_CONFIG_FILENAME = "wepawn_config.json"
_io_lock = threading.RLock()


def wepawn_config_path() -> Path:
    return wepawn_data_dir() / _CONFIG_FILENAME


def load_wepawn_config() -> dict[str, Any]:
    """손상·누락 시 빈 dict."""
    with _io_lock:
        path = wepawn_config_path()
        if not path.is_file():
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        if not (raw or "").strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(data) if isinstance(data, dict) else {}


def save_wepawn_config_merged(patch: Mapping[str, Any]) -> None:
    """기존 키를 유지한 채 ``patch``만 덮어쓴 뒤 원자적으로 저장한다."""
    from .hard_log import _hard_log

    if not isinstance(patch, dict):
        return
    with _io_lock:
        path = wepawn_config_path()
        base: dict[str, Any] = {}
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
                if (raw or "").strip():
                    prev = json.loads(raw)
                    if isinstance(prev, dict):
                        base = dict(prev)
            except (OSError, json.JSONDecodeError) as e:
                _hard_log(f"[CONFIG] wepawn_config.json 읽기 실패(병합 전) — {e}")
        base.update(dict(patch))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        text = json.dumps(base, ensure_ascii=False, indent=2)
        try:
            tmp.write_text(text + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            _hard_log(f"[CONFIG] wepawn_config.json 쓰기 실패: {e}")
            try:
                tmp.unlink()
            except OSError:
                pass
