"""
WepawnAI 4-step onboarding (QStackedWidget). All strings from locale ``onboard.*``.
"""

from __future__ import annotations

from typing import Callable

import mobase
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..i18n import (
    LOCALE_COMBO_ENTRIES,
    normalize_ui_locale_code,
    persist_selected_language,
    resolve_initial_locale_code,
    set_locale,
    tr,
)
from ..logic.config_manager import complete_onboarding
from ..logic.mo2_manager import (
    list_profile_names,
    try_show_profile_manager,
    validate_nexus_api_key,
)
from ..utils.hard_log import _hard_log

_NEXUS_KEYS_URL = "https://next.nexusmods.com/settings/api-keys"
_LOOT_URL = "https://loot.github.io/"


class OnboardingWizard(QDialog):
    def __init__(
        self,
        organizer: mobase.IOrganizer,
        parent: QWidget | None,
        *,
        plugin_name: str,
        application_name: str,
    ) -> None:
        super().__init__(parent)
        self._organizer = organizer
        self._plugin_name = plugin_name
        self._application_name = application_name
        self._nexus_ok = False
        self._nexus_checked = False
        self._loot_acknowledged = False

        self.setWindowFlags(
            (self.windowFlags() | Qt.WindowType.WindowCloseButtonHint)
            & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._stack = QStackedWidget()
        self._pages: list[QWidget] = []

        self._build_step_language()
        self._build_step_nexus()
        self._build_step_loot()
        self._build_step_profile()

        for p in self._pages:
            self._stack.addWidget(p)

        root = QVBoxLayout(self)
        self._title_lbl = QLabel()
        self._title_lbl.setObjectName("onboardTitle")
        f = self._title_lbl.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self._title_lbl.setFont(f)
        root.addWidget(self._title_lbl)
        root.addWidget(self._stack)

        nav = QHBoxLayout()
        self._btn_back = QPushButton()
        self._btn_back.clicked.connect(self._on_back)
        self._btn_next = QPushButton()
        self._btn_next.clicked.connect(self._on_next)
        self._btn_next.setDefault(True)
        nav.addWidget(self._btn_back)
        nav.addStretch(1)
        nav.addWidget(self._btn_next)
        root.addLayout(nav)

        self._idx = 0
        self._sync_nav()
        self._stack.setCurrentIndex(0)
        self._log_step(1)
        self._retranslate_ui()
        self.resize(560, 520)

    def _log_step(self, step_number_1based: int) -> None:
        _hard_log(f"[ONBOARD] Step {step_number_1based} 진입")

    def _build_step_language(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._s1_desc = QLabel()
        self._s1_desc.setWordWrap(True)
        lay.addWidget(self._s1_desc)
        self._lang_combo = QComboBox()
        self._lang_codes: list[str] = []
        for code, label in LOCALE_COMBO_ENTRIES:
            self._lang_codes.append(code)
            self._lang_combo.addItem(label, code)
        init = resolve_initial_locale_code(self._organizer)
        if init in self._lang_codes:
            self._lang_combo.setCurrentIndex(self._lang_codes.index(init))
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        lay.addWidget(self._lang_combo)
        lay.addStretch(1)
        self._pages.append(page)

    def _on_language_changed(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if not isinstance(code, str):
            return
        norm = normalize_ui_locale_code(code)
        if norm is None:
            return
        set_locale(norm)
        persist_selected_language(norm)
        self._retranslate_ui()

    def _build_step_nexus(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._nexus_desc = QLabel()
        self._nexus_desc.setWordWrap(True)
        lay.addWidget(self._nexus_desc)
        self._nexus_link = QLabel()
        self._nexus_link.setOpenExternalLinks(True)
        self._nexus_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lay.addWidget(self._nexus_link)
        self._nexus_api_guide = QLabel()
        self._nexus_api_guide.setWordWrap(True)
        lay.addWidget(self._nexus_api_guide)
        self._nexus_key_edit = QLineEdit()
        self._nexus_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        try:
            cur = str(self._organizer.pluginSetting(self._plugin_name, "nexus_api_key") or "")
            self._nexus_key_edit.setText(cur)
        except Exception:
            pass
        lay.addWidget(self._nexus_key_edit)
        self._btn_nexus_validate = QPushButton()
        self._btn_nexus_validate.clicked.connect(self._on_validate_nexus)
        lay.addWidget(self._btn_nexus_validate)
        self._nexus_status = QLabel()
        self._nexus_status.setWordWrap(True)
        lay.addWidget(self._nexus_status)
        lay.addStretch(1)
        self._pages.append(page)

    def _on_validate_nexus(self) -> None:
        key = self._nexus_key_edit.text().strip()
        self._btn_nexus_validate.setEnabled(False)
        ok = validate_nexus_api_key(key, application=self._application_name)
        self._nexus_ok = ok
        self._nexus_checked = True
        self._nexus_status.setText(
            tr("onboard.step2_status_ok") if ok else tr("onboard.step2_status_fail")
        )
        self._btn_nexus_validate.setEnabled(True)
        self._sync_nav()

    def _build_step_loot(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._loot_desc = QLabel()
        self._loot_desc.setWordWrap(True)
        lay.addWidget(self._loot_desc)
        self._loot_browser = QTextBrowser()
        self._loot_browser.setOpenExternalLinks(True)
        self._loot_browser.setMaximumHeight(120)
        lay.addWidget(self._loot_browser)
        row = QHBoxLayout()
        self._btn_loot_done = QPushButton()
        self._btn_loot_done.clicked.connect(self._on_loot_done)
        self._btn_loot_skip = QPushButton()
        self._btn_loot_skip.clicked.connect(self._on_loot_skip)
        row.addWidget(self._btn_loot_done)
        row.addWidget(self._btn_loot_skip)
        lay.addLayout(row)
        lay.addStretch(1)
        self._pages.append(page)

    def _on_loot_done(self) -> None:
        self._loot_acknowledged = True
        self._sync_nav()

    def _on_loot_skip(self) -> None:
        self._loot_acknowledged = True
        self._sync_nav()

    def _build_step_profile(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._prof_desc = QLabel()
        self._prof_desc.setWordWrap(True)
        lay.addWidget(self._prof_desc)
        self._rb_new = QRadioButton()
        self._rb_exist = QRadioButton()
        self._rb_new.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self._rb_new)
        grp.addButton(self._rb_exist)
        self._rb_new.toggled.connect(self._on_profile_mode_toggle)
        lay.addWidget(self._rb_new)
        lay.addWidget(self._rb_exist)
        self._sub_stack = QStackedWidget()
        p_new = QWidget()
        nl = QVBoxLayout(p_new)
        self._prof_new_body = QLabel()
        self._prof_new_body.setWordWrap(True)
        nl.addWidget(self._prof_new_body)
        self._btn_profile_mgr = QPushButton()
        self._btn_profile_mgr.clicked.connect(self._on_open_profile_manager)
        nl.addWidget(self._btn_profile_mgr)
        nl.addStretch(1)
        self._sub_stack.addWidget(p_new)
        p_ex = QWidget()
        el = QVBoxLayout(p_ex)
        self._prof_exist_hint = QLabel()
        self._prof_exist_hint.setWordWrap(True)
        el.addWidget(self._prof_exist_hint)
        self._profile_list = QListWidget()
        el.addWidget(self._profile_list)
        self._sub_stack.addWidget(p_ex)
        lay.addWidget(self._sub_stack)
        self._sub_stack.setCurrentIndex(0)
        lay.addStretch(1)
        self._pages.append(page)
        self._refresh_profile_list()

    def _on_profile_mode_toggle(self) -> None:
        self._sub_stack.setCurrentIndex(0 if self._rb_new.isChecked() else 1)

    def _on_open_profile_manager(self) -> None:
        ok = try_show_profile_manager(self._organizer, self)
        if not ok:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._organizer.basePath()))

    def _refresh_profile_list(self) -> None:
        self._profile_list.clear()
        names, _api = list_profile_names(self._organizer)
        for n in names:
            self._profile_list.addItem(n)
        try:
            cur = self._organizer.profileName()
        except Exception:
            cur = ""
        if cur:
            items = self._profile_list.findItems(cur, Qt.MatchFlag.MatchExactly)
            if items:
                self._profile_list.setCurrentItem(items[0])

    def _retranslate_ui(self) -> None:
        self.setWindowTitle(tr("onboard.window_title"))
        self._title_lbl.setText(tr("onboard.window_title"))
        self._btn_back.setText(tr("onboard.nav_back"))
        self._btn_next.setText(tr("onboard.nav_next"))
        self._s1_desc.setText(tr("onboard.step1_desc"))
        self._nexus_desc.setText(tr("onboard.step2_desc"))
        link = f'<a href="{_NEXUS_KEYS_URL}">{tr("onboard.step2_link_keys")}</a>'
        self._nexus_link.setText(link)
        self._nexus_api_guide.setText(tr("onboard.msg_api_key_guide"))
        self._btn_nexus_validate.setText(tr("onboard.step2_validate"))
        if self._nexus_checked:
            self._nexus_status.setText(
                tr("onboard.step2_status_ok")
                if self._nexus_ok
                else tr("onboard.step2_status_fail")
            )
        self._loot_desc.setText(tr("onboard.step3_desc"))
        loot = f'<a href="{_LOOT_URL}">{tr("onboard.step3_loot_link")}</a>'
        self._loot_browser.setHtml(f"<p>{loot}</p>")
        self._btn_loot_done.setText(tr("onboard.step3_btn_done"))
        self._btn_loot_skip.setText(tr("onboard.step3_btn_skip"))
        self._prof_desc.setText(tr("onboard.step4_desc"))
        self._rb_new.setText(tr("onboard.step4_mode_new"))
        self._rb_exist.setText(tr("onboard.step4_mode_existing"))
        self._prof_new_body.setText(tr("onboard.step4_new_body"))
        self._btn_profile_mgr.setText(tr("onboard.step4_open_pm"))
        self._prof_exist_hint.setText(tr("onboard.step4_exist_hint"))

    def _sync_nav(self) -> None:
        last = self._idx >= len(self._pages) - 1
        self._btn_back.setVisible(self._idx > 0)
        if last:
            self._btn_next.setText(tr("onboard.nav_finish"))
        else:
            self._btn_next.setText(tr("onboard.nav_next"))
        can_next = True
        if self._idx == 1:
            can_next = self._nexus_ok
        elif self._idx == 2:
            can_next = self._loot_acknowledged
        self._btn_next.setEnabled(can_next)

    def _on_back(self) -> None:
        if self._idx <= 0:
            return
        self._idx -= 1
        self._stack.setCurrentIndex(self._idx)
        self._log_step(self._idx + 1)
        self._sync_nav()

    def _on_next(self) -> None:
        if self._idx < len(self._pages) - 1:
            self._idx += 1
            self._stack.setCurrentIndex(self._idx)
            self._log_step(self._idx + 1)
            if self._idx == 3:
                self._refresh_profile_list()
            self._sync_nav()
            return
        if not self._finish_validate():
            return
        prof = self._resolve_selected_profile()
        complete_onboarding(
            selected_profile=prof,
            llama_base_url=None,
            nexus_api_key=self._nexus_key_edit.text().strip(),
            plugin_name=self._plugin_name,
            organizer=self._organizer,
        )
        self.accept()

    def _resolve_selected_profile(self) -> str:
        if self._rb_new.isChecked():
            try:
                return str(self._organizer.profileName() or "").strip()
            except Exception:
                return ""
        item = self._profile_list.currentItem()
        return (item.text() if item is not None else "").strip()

    def _finish_validate(self) -> bool:
        if self._rb_exist.isChecked():
            if self._profile_list.currentItem() is None:
                QMessageBox.warning(
                    self,
                    tr("onboard.window_title"),
                    tr("onboard.err_no_profile"),
                )
                return False
        return True


def run_onboarding_if_needed(
    organizer: mobase.IOrganizer,
    parent: QWidget | None,
    *,
    plugin_name: str,
    application_name: str,
    is_done: Callable[[], bool],
) -> bool:
    """
    Returns True if onboarding completed or was already done; False if user cancelled.
    """
    if is_done():
        return True
    wiz = OnboardingWizard(
        organizer,
        parent,
        plugin_name=plugin_name,
        application_name=application_name,
    )
    return wiz.exec() == QDialog.DialogCode.Accepted
