"""
Background tier analysis: HTTP to local llama-server runs off the MO2 UI thread.
"""

from __future__ import annotations

from typing import Sequence

from PyQt6.QtCore import QThread, pyqtSignal

from ..ai.llm_client import LLMConnectionError, LLMParseError, _diag, analyze_mod_tier
from ..ai.nexus_scraper import scrape_nexus_mod_context


class TierAnalysisWorker(QThread):
    """Runs :func:`analyze_mod_tier` without blocking the Qt GUI thread."""

    finished_ok = pyqtSignal(dict)
    # message, optional raw model/envelope text, True if LLMConnectionError (not parse)
    failed = pyqtSignal(str, str, bool)

    def __init__(
        self,
        mod_display_name: str,
        nexus_dep_lines: Sequence[str],
        enabled_mod_names: Sequence[str],
        *,
        base_url: str,
        request_timeout: float = 90.0,
        nexus_mod_page_url: str | None = None,
        nexus_scrape_timeout: float = 4.0,
        mo2_physical_context: str = "",
        tier_nexus_id: int = 0,
        nexus_game_domain: str = "skyrimspecialedition",
        current_game_version: str = "",
        current_game_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mod_display_name = mod_display_name
        self._nexus_dep_lines = list(nexus_dep_lines)
        self._enabled_mod_names = list(enabled_mod_names)
        self._base_url = base_url.rstrip("/")
        self._request_timeout = float(request_timeout)
        self._nexus_mod_page_url = (nexus_mod_page_url or "").strip() or None
        self._nexus_scrape_timeout = float(nexus_scrape_timeout)
        self._mo2_physical_context = str(mo2_physical_context or "")
        self._nexus_game_domain = (nexus_game_domain or "").strip().strip("/").lower() or "skyrimspecialedition"
        self._current_game_version = str(current_game_version or "").strip()
        self._current_game_name = str(current_game_name or "").strip()
        try:
            self._tier_nexus_id = int(tier_nexus_id)
        except (TypeError, ValueError):
            self._tier_nexus_id = 0

    def run(self) -> None:
        try:
            _diag(
                f"TIER_WORKER start mod={self._mod_display_name!r} tier_nexus_id={self._tier_nexus_id} "
                f"nexus_scrape_url={self._nexus_mod_page_url!r} "
                f"mo2_physical_context_len={len(self._mo2_physical_context)} "
                f"current_game_name={self._current_game_name!r} "
                f"current_game_version={self._current_game_version!r}"
            )
            nexus_context = ""
            if self._nexus_mod_page_url:
                nexus_context = scrape_nexus_mod_context(
                    self._nexus_mod_page_url,
                    timeout=self._nexus_scrape_timeout,
                    max_chars=2000,
                )
            result = analyze_mod_tier(
                self._mod_display_name,
                self._nexus_dep_lines,
                self._enabled_mod_names,
                base_url=self._base_url,
                request_timeout=self._request_timeout,
                nexus_context=nexus_context,
                mo2_physical_context=self._mo2_physical_context,
                nexus_game_domain=self._nexus_game_domain,
                current_game_version=self._current_game_version,
                current_game_name=self._current_game_name,
            )
            self.finished_ok.emit(result)
        except LLMConnectionError as exc:
            self.failed.emit(str(exc), "", True)
        except LLMParseError as exc:
            raw = exc.raw_text or ""
            self.failed.emit(str(exc), raw, False)
