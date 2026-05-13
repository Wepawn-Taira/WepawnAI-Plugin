"""
WepawnAI — MO2 Python module plugin (Skyrim SE/AE oriented).

Entry point: :func:`createPlugin` (required by Mod Organizer 2).
"""

from __future__ import annotations

import mobase

from .plugin import WepawnAIPlugin


def createPlugin() -> mobase.IPlugin:
    return WepawnAIPlugin()
