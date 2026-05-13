"""Local LLM: portable llama-server lifecycle + HTTP client."""

from __future__ import annotations

from .llm_client import (
    LLMConnectionError,
    LLMParseError,
    analyze_mod_tier,
    analyze_mod_tier_ollama,
)
from .nexus_scraper import build_nexus_mod_page_url, scrape_nexus_mod_context
from .portable_server import PortableLlamaServer, plugin_root

__all__ = [
    "LLMConnectionError",
    "LLMParseError",
    "PortableLlamaServer",
    "analyze_mod_tier",
    "analyze_mod_tier_ollama",
    "build_nexus_mod_page_url",
    "plugin_root",
    "scrape_nexus_mod_context",
]
