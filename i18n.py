"""
JSON-based translations for WepawnAI (UI and helper messages).

MO2 also supports Qt Linguist; this module follows the project layout you chose
(`locale/en.json`, `locale/ko.json`, …) and is safe to call from PyQt6 widgets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_DEFAULT_LOCALE = "en"

# UI JSON packs under ``locale/*.json`` (two-letter codes).
SUPPORTED_UI_LOCALE_CODES: frozenset[str] = frozenset({"en", "ko", "ja", "zh", "ru"})

LOCALE_COMBO_ENTRIES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ko", "한국어"),
    ("ja", "日本語"),
    ("zh", "中文"),
    ("ru", "Русский"),
)

_LLM_SYSTEM_LANG_LINES: dict[str, str] = {
    "en": "Please respond in English.",
    "ko": "Please respond in Korean.",
    "ja": "Please respond in Japanese.",
    "zh": "Please respond in Simplified Chinese.",
    "ru": "Please respond in Russian.",
}


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _load_locale_file(code: str) -> Mapping[str, Any]:
    path = _plugin_root() / "locale" / f"{code}.json"
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Locale {code!r} root must be a JSON object")
    return data


def _get_nested(mapping: Mapping[str, Any], dotted_key: str) -> Any:
    node: Any = mapping
    for part in dotted_key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


class Translator:
    """
    Resolve dotted keys such as ``chat.send`` against the active locale with
    fallback to ``en``.
    """

    __slots__ = ("_fallback", "_active", "locale_code")

    def __init__(self, locale_code: str | None = None) -> None:
        self._fallback = _load_locale_file(_DEFAULT_LOCALE)
        code = (locale_code or _DEFAULT_LOCALE).replace("-", "_").split("_")[0].lower()
        self.locale_code = code if code else _DEFAULT_LOCALE
        try:
            self._active = (
                _load_locale_file(self.locale_code)
                if self.locale_code != _DEFAULT_LOCALE
                else self._fallback
            )
        except FileNotFoundError:
            self._active = self._fallback
            self.locale_code = _DEFAULT_LOCALE

    def tr(self, key: str, **kwargs: Any) -> str:
        value = _get_nested(self._active, key)
        if value is None:
            value = _get_nested(self._fallback, key)
        if value is None:
            return key
        if not isinstance(value, str):
            return str(value)
        if kwargs:
            try:
                return value.format(**kwargs)
            except KeyError:
                return value
        return value


_GLOBAL: Translator | None = None


def set_locale(locale_code: str | None) -> Translator:
    """Set the process-wide translator (used by UI and plugin helpers)."""
    global _GLOBAL
    _GLOBAL = Translator(locale_code)
    return _GLOBAL


def translator() -> Translator:
    """Lazy default translator (English) if :func:`set_locale` was not called."""
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = Translator(_DEFAULT_LOCALE)
    return _GLOBAL


def tr(key: str, **kwargs: Any) -> str:
    """Shorthand for :meth:`Translator.tr` on the active translator."""
    return translator().tr(key, **kwargs)


def normalize_ui_locale_code(raw: str | None) -> str | None:
    if not (raw or "").strip():
        return None
    code = str(raw).replace("-", "_").split("_")[0].strip().lower()
    return code if code in SUPPORTED_UI_LOCALE_CODES else None


def resolve_initial_locale_code(organizer: Any | None = None) -> str:
    """
    우선순위: ``wepawn_config.json`` 의 ``selected_language`` (지원 코드일 때만) →
    그다음 Qt/MO2 표시 언어 → 시스템 로케일 → ``en``.

    ``organizer`` 인자는 하위 호환용으로만 남겨 두었으며 언어 결정에는 사용하지 않는다.
    """
    from .utils.hard_log import _hard_log
    from .utils.wepawn_config import load_wepawn_config

    cfg = load_wepawn_config()
    raw_cfg = cfg.get("selected_language")
    if raw_cfg is not None:
        norm = normalize_ui_locale_code(str(raw_cfg))
        if norm is not None:
            _hard_log(f"[LANG] wepawn_config.json 의 selected_language 적용: {norm}")
            return norm

    try:
        from PyQt6.QtCore import QLocale
        from PyQt6.QtWidgets import QApplication
    except Exception:
        return _DEFAULT_LOCALE

    app = QApplication.instance()
    if app is not None:
        loc = app.property("locale")
        if hasattr(loc, "name"):
            code = loc.name()  # type: ignore[union-attr]
            if isinstance(code, str) and len(code) >= 2:
                cand = normalize_ui_locale_code(code[:2])
                if cand is not None:
                    return cand
    name = QLocale.system().name()
    cand = normalize_ui_locale_code(name[:2] if len(name) >= 2 else name)
    return cand if cand is not None else _DEFAULT_LOCALE


def llm_system_prompt_language_line() -> str:
    """Single English sentence for the LLM (append to system prompt)."""
    code = translator().locale_code
    return _LLM_SYSTEM_LANG_LINES.get(code, _LLM_SYSTEM_LANG_LINES[_DEFAULT_LOCALE])


def persist_selected_language(code: str) -> None:
    """``wepawn_config.json`` 에 ``selected_language`` 저장 (MO2 자동 감지보다 우선되도록)."""
    from .utils.wepawn_config import save_wepawn_config_merged

    c = normalize_ui_locale_code(code)
    if not c:
        return
    save_wepawn_config_merged({"selected_language": c})
