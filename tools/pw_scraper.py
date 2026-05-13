"""
Nexus mod page fetch via Playwright: stdout = raw **UTF-8 bytes** only (``sys.stdout.buffer``).
On success, also writes ``dump_success.html`` (UTF-8) in the process CWD (typically ``tools/``) for DOM inspection.

Uses **persistent Chromium context** under ``bin/portable_python/browser_profile``. First run is detected
when the profile directory is missing or empty; user is escorted to Nexus sign-in until leaving the auth flow.

After ``domcontentloaded``, waits until the document title no longer looks like a Cloudflare
interstitial (``Just a moment`` / ``Checking``). No fixed post-load sleep: adult-notice visibility
uses the locator's own short timeout (``is_visible`` ~400ms).

**Adult gate (Nexus site notice):** From a real mod-page dump (``tools/dump_success.html``), the blocked
state uses ``div.site-notice`` with ``h3[id$="-title"]`` (e.g. "Adult content disabled") and primary CTA
``a.btn.btn-primary`` ("View adult content preferences" → ``next.nexusmods.com/settings/content-blocking``).
Additional locators (button / generic "View" links) are tried with tight timeouts so normal pages do not stall.

After gate handling, waits for Requirements-related DOM or ``__NEXT_DATA__`` when ``tab=requirements`` is in the URL.

Diagnostics and tracebacks go to ``sys.stderr.buffer`` as UTF-8 to avoid Windows cp949 mojibake.
:func:`_hard_log` also appends to ``wepawn_debug.log`` when ``utils.hard_log`` is importable.

``bin/portable_python/python.exe`` + ``PLAYWRIGHT_BROWSERS_PATH`` → ``ms-playwright``.
TimeoutError from ``wait_for_function`` is handled by the outer ``except`` and traced to stderr.
Empty ``page.content()`` is valid — still exit 0.
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path


def _stderr_utf8(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.buffer.flush()


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


def _hard_log(message: str) -> None:
    """Mirror to stderr; append to wepawn_debug.log when utils stack is available."""
    try:
        from utils.hard_log import _hard_log as _file_log  # type: ignore[attr-defined]

        _file_log(message)
    except Exception:
        pass
    _stderr_utf8(message + "\n")


def _nexus_adult_notice_visible(page: object) -> bool:
    """True when the standard Nexus adult-disabled site notice heading is visible."""
    try:
        h = page.locator('h3[id$="-title"]').filter(
            has_text=re.compile(r"adult\s+content", re.I)
        )
        return h.count() > 0 and h.first.is_visible(timeout=400)
    except Exception:
        return False


def _try_click_nexus_adult_gate(page: object) -> bool:
    """
    Try primary/secondary CTAs. Returns True if a click was dispatched.
    Selectors ordered by verified dump: ``.site-notice a.btn.btn-primary`` + View/adult text.
    """
    factories: list = [
        lambda: page.locator("div.site-notice").locator("a.btn.btn-primary").filter(
            has_text=re.compile(r"View\s+adult", re.I)
        ),
        lambda: page.locator("div.site-notice.warning a.btn-primary").filter(
            has_text=re.compile(r"View", re.I)
        ),
        lambda: page.get_by_role(
            "button", name=re.compile(r"view\s+adult|^\s*view\s*$", re.I)
        ),
        lambda: page.get_by_role(
            "link",
            name=re.compile(
                r"view\s+adult(?!(\s+content)?\s+preferences)", re.I
            ),
        ),
        lambda: page.locator("a").filter(
            has_text=re.compile(r"^View\b", re.I)
        ).filter(
            has=page.locator("text=/adult|content|mod/i")
        ),
    ]
    for _attempt in range(3):
        for factory in factories:
            try:
                loc = factory()
                if loc.count() == 0:
                    continue
                el = loc.first
                if not el.is_visible(timeout=700):
                    continue
                el.click(timeout=5000)
                return True
            except Exception:
                continue
        try:
            page.wait_for_timeout(220)
        except Exception:
            pass
    return False


def _recover_mod_url_after_gate_nav(page: object, original: str) -> None:
    """If CTA navigated to settings, return to the mod URL for a consistent scrape attempt."""
    try:
        cur = page.url or ""
    except Exception:
        return
    if "next.nexusmods.com" in cur and "content-blocking" in cur:
        try:
            page.goto(original, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass


def _wait_requirements_or_next_data(page: object, original_url: str) -> None:
    """Wait until ``__NEXT_DATA__`` is populated; on ``tab=requirements`` also table/heading.

    Caps are tight so the common case (SSR ``__NEXT_DATA__`` already in HTML) returns on the first
    poll. Live headless timing is often blocked by Cloudflare; caps are tuned down from 15s toward
    typical client render without matching the old no-wait scraper on slow tabs.
    """
    tab_req = "tab=requirements" in original_url
    _hard_log(
        "[SCRAPER] 오버레이 돌파 완료 및 Requirements 렌더링 대기 중"
    )
    # Description/other tabs: only ``__NEXT_DATA__`` length — usually immediate after DOM.
    # Requirements tab: extra wait for hydrated table or "Nexus requirements" heading.
    timeout_ms = 7500 if tab_req else 3200
    try:
        page.wait_for_function(
            """(tabReq) => {
              const nd = document.querySelector('script#__NEXT_DATA__');
              if (!nd || !nd.textContent || nd.textContent.length < 80) return false;
              if (!tabReq) return true;
              if (document.querySelector('td.table-require-name')) return true;
              const heads = document.querySelectorAll('h2, h3');
              for (const e of heads) {
                const t = (e.textContent || '').trim();
                if (/nexus\\s+requirements/i.test(t)) return true;
              }
              return false;
            }""",
            arg=tab_req,
            timeout=timeout_ms,
        )
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        _stderr_utf8("[PW_DIAG] missing URL (argv[1])\n")
        return 1
    url = (sys.argv[1] or "").strip()
    if not url:
        _stderr_utf8("[PW_DIAG] empty URL\n")
        return 1

    _stderr_utf8(
        f"[PW_DIAG] BROWSER_PATH={os.environ.get('PLAYWRIGHT_BROWSERS_PATH')!r}\n"
    )

    profile_path = (
        Path(__file__).resolve().parent.parent
        / "bin"
        / "portable_python"
        / "browser_profile"
    )
    profile_path.mkdir(parents=True, exist_ok=True)
    is_first_run = (
        not any(profile_path.iterdir()) if profile_path.exists() else True
    )

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        sys.stderr.buffer.write(traceback.format_exc().encode("utf-8"))
        sys.stderr.buffer.flush()
        return 1

    try:
        with sync_playwright() as p:
            # TODO: 안정화 후 headless=True로 전환
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                _stderr_utf8(
                    f"[PW_DIAG] Persistent Context \ub85c\ub4dc. "
                    f"\ucd5c\ucd08\uc2e4\ud589={is_first_run}\n"
                )
                _stderr_utf8(
                    f"[PW_DIAG] \ud65c\uc131 \ud0ed URL={page.url}\n"
                )

                if is_first_run:
                    _stderr_utf8(
                        "[PW_DIAG] \ucd5c\ucd08 \uc2e4\ud589 \u2014 "
                        "\ub125\uc11c\uc2a4 \ub85c\uadf8\uc778 \ud544\uc694\n"
                    )
                    page.goto("https://users.nexusmods.com/auth/sign_in")
                    try:
                        page.wait_for_function(
                            "() => !window.location.hostname.includes('users.nexusmods.com') "
                            "|| !window.location.pathname.includes('auth')",
                            timeout=300_000,
                        )
                    except Exception:
                        pass

                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_function(
                    "() => !document.title.includes('Just a moment') && !document.title.includes('Checking')",
                    timeout=20000,
                )

                _hard_log(
                    "[SCRAPER] Age Gate 감지 시도 (wait_until='domcontentloaded')"
                )
                notice = _nexus_adult_notice_visible(page)
                if notice:
                    if _try_click_nexus_adult_gate(page):
                        _recover_mod_url_after_gate_nav(page, url)
                        try:
                            page.wait_for_function(
                                "() => !document.title.includes('Just a moment') "
                                "&& !document.title.includes('Checking')",
                                timeout=15000,
                            )
                        except Exception:
                            pass

                try:
                    page.click(
                        "#CybotCookiebotDialogBodyButtonDecline",
                        timeout=2000,
                    )
                except Exception:
                    pass

                _wait_requirements_or_next_data(page, url)
                html = page.content()
            finally:
                context.close()
    except Exception:
        sys.stderr.buffer.write(traceback.format_exc().encode("utf-8"))
        sys.stderr.buffer.flush()
        return 1

    if "32444" in url:
        _root = Path(__file__).resolve().parent.parent
        (_root / "dump_32444.html").write_text(html, encoding="utf-8")

    with open("dump_success.html", "w", encoding="utf-8") as f:
        f.write(html)

    sys.stdout.buffer.write(html.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
