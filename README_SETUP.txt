================================================================================
WepawnAI — MO2 plugin layout (paths in English)
================================================================================

Install the release zip so the plugin root ends up exactly here:

  <ModOrganizer>/plugins/WepawnAI/

All relative paths below are from that folder (WepawnAI = plugin root).

--------------------------------------------------------------------------------
Required layout (after extraction)
--------------------------------------------------------------------------------

  plugins/WepawnAI/plugin.py              — MO2 entry point
  plugins/WepawnAI/game_context.py        — game / script-extender helpers
  plugins/WepawnAI/i18n.py, locale/, ui/, logic/, nexus/, utils/, ai/

  plugins/WepawnAI/bin/llama-server.exe   — local LLM server (Windows)
  plugins/WepawnAI/bin/*.dll              — llama.cpp runtime DLLs next to the exe

  plugins/WepawnAI/models/default.gguf    — default GGUF model (or replace)

  plugins/WepawnAI/bin/portable_python/python.exe
                                          — Embedded Python 3.11 (no system PATH)

  plugins/WepawnAI/bin/portable_python/ms-playwright/
                                          — Playwright Chromium bundle (offline)

--------------------------------------------------------------------------------
Portable Playwright (embedded Python + Chromium)
--------------------------------------------------------------------------------

Runtime code sets PLAYWRIGHT_BROWSERS_PATH to:

  <WepawnAI>/bin/portable_python/ms-playwright

Do not move ms-playwright out of that folder; scraping uses the portable
python.exe under bin/portable_python/ with that path.

--------------------------------------------------------------------------------
Developer rebuild notes (optional)
--------------------------------------------------------------------------------

To refresh Chromium inside ms-playwright, from a CMD with
PLAYWRIGHT_BROWSERS_PATH set to the path above, run:

  bin\portable_python\python.exe -m playwright install chromium

================================================================================
