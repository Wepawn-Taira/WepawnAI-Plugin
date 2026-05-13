"""
로컬 영구 JSON 캐시 (Nexus API 메타데이터 · 요구사항 스크랩).

``wepawn_cache.json`` — 멀티 스레드 안전(RLock), 손상 시 빈 캐시로 폴백.
경로는 :mod:`wepawn_storage` (``basePath()/WepawnAI``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .hard_log import _hard_log
from .wepawn_storage import wepawn_data_dir

_CACHE_FILENAME = "wepawn_cache.json"
_DEFAULT_TTL_SEC = 15 * 24 * 3600


def _cache_file_path() -> Path:
    return wepawn_data_dir() / _CACHE_FILENAME


class WepawnPersistentCache:
    """싱글톤: ``metadata`` / ``requirements`` 버킷, TTL 기본 15일."""

    _instance: WepawnPersistentCache | None = None
    _singleton_lock = threading.Lock()

    @classmethod
    def instance(cls) -> WepawnPersistentCache:
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._path = _cache_file_path()
        self.ttl_sec = int(_DEFAULT_TTL_SEC)
        self._io_lock = threading.RLock()

    @staticmethod
    def entry_key(domain: str, mod_id: int) -> str:
        return f"{(domain or '').strip().lower()}:{int(mod_id)}"

    def _read_root_unlocked(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"metadata": {}, "requirements": {}}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            _hard_log(f"[CACHE DBG] 캐시 읽기 실패(OSError) → 무시: {e}")
            return {"metadata": {}, "requirements": {}}
        if not (raw or "").strip():
            return {"metadata": {}, "requirements": {}}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            _hard_log(
                f"[CACHE DBG] JSONDecodeError(손상) → 캐시 초기화 후 네트워크 폴백: {e}"
            )
            return {"metadata": {}, "requirements": {}}
        if not isinstance(data, dict):
            return {"metadata": {}, "requirements": {}}
        meta = data.get("metadata")
        req = data.get("requirements")
        return {
            "metadata": meta if isinstance(meta, dict) else {},
            "requirements": req if isinstance(req, dict) else {},
        }

    def _write_root_unlocked(self, root: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = {
            "metadata": dict(root.get("metadata") or {}),
            "requirements": dict(root.get("requirements") or {}),
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _entry_fresh(entry: Any, ttl_sec: int, now: float) -> bool:
        if not isinstance(entry, dict):
            return False
        try:
            ts = int(entry.get("timestamp"))
        except (TypeError, ValueError):
            return False
        return (now - float(ts)) < float(ttl_sec)

    def get_metadata_data(self, domain: str, mod_id: int) -> dict[str, Any] | None:
        key = self.entry_key(domain, mod_id)
        now = time.time()
        with self._io_lock:
            root = self._read_root_unlocked()
            ent = root["metadata"].get(key)
            if not isinstance(ent, dict):
                return None
            if not self._entry_fresh(ent, self.ttl_sec, now):
                return None
            data = ent.get("data")
            return dict(data) if isinstance(data, dict) else None

    def set_metadata_data(self, domain: str, mod_id: int, data: Mapping[str, Any]) -> None:
        key = self.entry_key(domain, mod_id)
        with self._io_lock:
            root = self._read_root_unlocked()
            root["metadata"][key] = {
                "data": dict(data),
                "timestamp": int(time.time()),
            }
            self._write_root_unlocked(root)

    def get_requirements_bundle(self, domain: str, mod_id: int) -> dict[str, Any] | None:
        """``rows``(list), ``title_clean``, ``is_external`` 포함 dict 또는 None."""
        key = self.entry_key(domain, mod_id)
        now = time.time()
        with self._io_lock:
            root = self._read_root_unlocked()
            ent = root["requirements"].get(key)
            if not isinstance(ent, dict):
                return None
            if not self._entry_fresh(ent, self.ttl_sec, now):
                return None
            data = ent.get("data")
            if not isinstance(data, dict):
                return None
            out = dict(data)
            rows = out.get("rows")
            if not isinstance(rows, list):
                return None
            out["rows"] = list(rows)
            return out

    def set_requirements_bundle(
        self,
        domain: str,
        mod_id: int,
        *,
        rows: list[dict[str, Any]],
        title_clean: str | None = None,
        is_external: bool | None = None,
    ) -> None:
        key = self.entry_key(domain, mod_id)
        now = time.time()
        with self._io_lock:
            root = self._read_root_unlocked()
            tc = title_clean
            ie = is_external
            prev = root["requirements"].get(key)
            if isinstance(prev, dict) and self._entry_fresh(prev, self.ttl_sec, now):
                pdata = prev.get("data")
                if isinstance(pdata, dict):
                    if tc is None and pdata.get("title_clean") is not None:
                        tc = pdata.get("title_clean")
                    if ie is None and "is_external" in pdata:
                        ie = pdata.get("is_external")
            payload = {
                "rows": list(rows),
                "title_clean": tc,
                "is_external": ie,
            }
            root["requirements"][key] = {
                "data": payload,
                "timestamp": int(time.time()),
            }
            self._write_root_unlocked(root)


def get_wepawn_cache() -> WepawnPersistentCache:
    return WepawnPersistentCache.instance()
