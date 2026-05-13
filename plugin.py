"""
WepawnAI — Mod Organizer 2 tool plugin entry point.

``mobase.IOrganizer`` is the live session API injected through :meth:`WepawnAIPlugin.init`;
MO2 implements it in C++ and plugins must not subclass it. This module keeps full typing
against :class:`mobase.IOrganizer` for all organizer calls.
"""

from __future__ import annotations

from functools import partial
from typing import List, Optional

import mobase
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .ai.portable_server import PortableLlamaServer
from .i18n import set_locale, tr
from .logic.config_manager import is_onboarding_done
from .ui.chat_window import WepawnChatWindow
from .ui.onboarding_wizard import run_onboarding_if_needed
from .utils.game_info import get_current_game_info
from .utils.mo2_authority import ping_mo2_api_authority
from .utils.wepawn_storage import configure_wepawn_data_dir_from_organizer

VERSION = "0.1.0"


def _version_info_from_string(v: str) -> mobase.VersionInfo:
    parts = [p for p in v.replace("-", ".").split(".") if p.isdigit()]
    nums = [int(x) for x in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return mobase.VersionInfo(nums[0], nums[1], nums[2], mobase.ReleaseType.FINAL)


class WepawnAIPlugin(mobase.IPluginTool):
    """
    AI-oriented helper UI for Bethesda games (Skyrim SE/AE) under MO2.

    Inherits from :class:`mobase.IPluginTool` only; use :attr:`_organizer` typed as
    :class:`mobase.IOrganizer` for MO2 session operations.
    """

    _organizer: Optional[mobase.IOrganizer]
    _window: WepawnChatWindow | None
    _llama_server: Optional[PortableLlamaServer]
    _about_to_quit_hooked: bool

    def __init__(self) -> None:
        super().__init__()
        self._organizer = None
        self._window = None
        self._llama_server = None
        self._about_to_quit_hooked = False

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        configure_wepawn_data_dir_from_organizer(organizer)
        _diagnostic_wepawn_log_paths(organizer)
        self._hook_application_quit()
        self._start_portable_llama()
        _ensure_qt_locale(organizer)
        QTimer.singleShot(0, partial(self._authority_ping_on_main_queue, organizer))
        return True

    def _authority_ping_on_main_queue(self, organizer: mobase.IOrganizer) -> None:
        try:
            ping_mo2_api_authority(organizer, archive_abs=None)
        except Exception as exc:
            from .utils.hard_log import _hard_log

            _hard_log(f"MO2_AUTHORITY INIT_PING raised {type(exc).__name__}: {exc}")

    def _hook_application_quit(self) -> None:
        app = QApplication.instance()
        if app is None or self._about_to_quit_hooked:
            return
        app.aboutToQuit.connect(self._on_application_quit)
        self._about_to_quit_hooked = True

    def _on_application_quit(self) -> None:
        if self._llama_server is not None:
            self._llama_server.stop()
            self._llama_server = None

    def _start_portable_llama(self) -> None:
        if self._llama_server is not None:
            return
        srv = PortableLlamaServer()
        if srv.start():
            self._llama_server = srv

    def name(self) -> str:
        return "WepawnAI"

    def localizedName(self) -> str:
        return tr("plugin.display_name")

    def author(self) -> str:
        return "WepawnAI contributors"

    def description(self) -> str:
        return tr("plugin.description")

    def version(self) -> mobase.VersionInfo:
        return _version_info_from_string(VERSION)

    def isActive(self) -> bool:
        if self._organizer is None:
            return False
        return bool(self._organizer.pluginSetting(self.name(), "enabled"))

    def settings(self) -> List[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("enabled", "Enable WepawnAI", True),
            mobase.PluginSetting(
                "nexus_api_key",
                "Nexus Mods API key (optional; used when ModOrganizer.ini has no key, e.g. OAuth)",
                "",
            ),
            mobase.PluginSetting(
                "application_name",
                "Application name sent as Nexus API User-Agent",
                f"WepawnAI/{VERSION}",
            ),
        ]

    def displayName(self) -> str:
        return tr("plugin.display_name")

    def tooltip(self) -> str:
        return tr("plugin.tooltip")

    def icon(self) -> QIcon:
        return QIcon()

    def display(self) -> None:
        if self._organizer is None:
            return
        self._hook_application_quit()
        _ensure_qt_locale(self._organizer)
        parent = self._parentWidget()
        organizer = self._organizer
        game_domain = _safe_game_nexus_name(organizer)
        plugin_name = self.name()

        def api_key() -> str:
            # Fresh read each call (MO2 persists edits from Settings immediately).
            return str(organizer.pluginSetting(plugin_name, "nexus_api_key") or "")

        def application() -> str:
            return str(
                organizer.pluginSetting(plugin_name, "application_name")
                or f"WepawnAI/{VERSION}"
            )

        def llama_base_url() -> str | None:
            srv = self._llama_server
            if srv is not None and srv.is_running():
                return srv.base_url
            return None

        if not run_onboarding_if_needed(
            organizer,
            parent,
            plugin_name=plugin_name,
            application_name=application(),
            is_done=is_onboarding_done,
        ):
            return

        if self._window is None:
            self._window = WepawnChatWindow(
                parent,
                game_domain=game_domain,
                nexus_api_key_getter=api_key,
                game_version_getter=lambda: _safe_game_version(organizer),
                application_getter=application,
                organizer_getter=lambda: self._organizer,
                llama_base_url_getter=llama_base_url,
            )
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()


def _mo2_plugin_log_info(msg: str) -> None:
    """Best-effort line in MO2 log (signatures differ by MO2 build)."""
    try:
        ll = getattr(mobase, "LogLevel", None)
        if ll is not None:
            lvl = getattr(ll, "Info", None) or getattr(ll, "INFO", None)
            if lvl is not None:
                mobase.logMessage(lvl, msg)
                return
    except Exception:
        pass
    try:
        mobase.logMessage(msg)
    except Exception:
        print(msg, flush=True)


def _diagnostic_wepawn_log_paths(organizer: mobase.IOrganizer) -> None:
    """
    TEMP: FO4 등 인스턴스별 ``wepawn_debug.log`` 위치 확인.

    ``configure_wepawn_data_dir_from_organizer`` 직후 호출.
    대상 경로: ``resolve(organizer.basePath()) / 'WepawnAI' / 'wepawn_debug.log'`` 가 아니면
    ``basePath()`` 실패·빈 값으로 플러그인 폴더에 폴백된 것.
    """
    from .utils.hard_log import _hard_log
    from .utils.wepawn_storage import wepawn_data_dir

    base_path = ""
    base_exc = ""
    try:
        raw = organizer.basePath()
        base_path = str(raw).strip() if raw is not None else ""
    except Exception as exc:
        base_exc = f"{type(exc).__name__}: {exc}"

    data_dir = wepawn_data_dir()
    try:
        data_resolved = data_dir.resolve()
        log_path = (data_dir / "wepawn_debug.log").resolve()
    except Exception:
        data_resolved = data_dir
        log_path = data_dir / "wepawn_debug.log"

    game_parts: list[str] = []
    try:
        mg = organizer.managedGame()
        if mg is None:
            game_parts.append("managedGame=None")
        else:
            for meth in ("gameName", "gameShortName", "gameNexusName"):
                fn = getattr(mg, meth, None)
                if not callable(fn):
                    continue
                try:
                    g = str(fn()).strip()
                    game_parts.append(f"{meth}={g!r}" if g else f"{meth}=<empty>")
                except Exception as exc:
                    game_parts.append(f"{meth}_err={type(exc).__name__}:{exc}")
    except Exception as exc:
        game_parts.append(f"managedGame_exc={type(exc).__name__}:{exc}")

    _mo2_plugin_log_info(
        "WepawnAI [BOOT DIAG] "
        f"basePath={base_path!r} basePath_exc={base_exc!r} "
        f"wepawn_data_dir={str(data_resolved)!r} wepawn_debug.log={str(log_path)!r} "
        f"{' | '.join(game_parts)}"
    )

    wr_ok = False
    wr_err = ""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".wepawn_write_probe_tmp"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except OSError:
            pass
        wr_ok = True
    except Exception as exc:
        wr_err = f"{type(exc).__name__}: {exc}"

    _mo2_plugin_log_info(
        f"WepawnAI [BOOT DIAG] wepawn_data_dir parent writable={wr_ok} err={wr_err!r}"
    )

    _hard_log(
        f"[BOOT DIAG] organizer.basePath()={base_path!r} basePath_exc={base_exc!r} "
        f"wepawn_data_dir={str(data_resolved)!r} log_file={str(log_path)!r} "
        f"game {' | '.join(game_parts)} "
        f"data_dir_writable={wr_ok} write_probe_err={wr_err!r}"
    )


def _ensure_qt_locale(organizer: Optional[mobase.IOrganizer] = None) -> None:
    from .i18n import resolve_initial_locale_code

    set_locale(resolve_initial_locale_code(organizer))


def _safe_game_nexus_name(organizer: mobase.IOrganizer) -> str:
    game = organizer.managedGame()
    if game is None:
        return "skyrimspecialedition"
    try:
        name = game.gameNexusName()
    except Exception:
        return "skyrimspecialedition"
    text = str(name).strip()
    return text if text else "skyrimspecialedition"


def _safe_game_version(organizer: mobase.IOrganizer) -> str:
    return get_current_game_info(organizer)[1]
