"""Transparent CloakBrowser activation for generated Playwright scrapers.

TODO(dies-with-/scrape): this global ``.pth`` monkeypatch exists ONLY to serve
the /scrape subprocess execution path (legacy Playwright scrapers launched via
scraper_runner with ``STEALTH_BROWSER=cloak``). Once /scrape + scraper_runner
are removed and callers migrate to /navigate (which drives cloak via
``_launch_page`` directly, no monkeypatch needed), delete this file AND its
companion ``cloak_stealth.pth``. See docs/browser-service-rework-plan.md.

Loaded automatically at Python startup via the companion ``cloak_stealth.pth``
file (site-packages). When ``STEALTH_BROWSER=cloak`` is set (scraper_runner does
this for anti-bot sites), it wraps ``playwright.BrowserType.launch`` (sync +
async) so the *existing* Playwright launch drives CloakBrowser's stealth
Chromium binary + fingerprint args — rather than calling cloak's own ``launch()``
(which would start a second Playwright and clash with the scraper's
``sync_playwright()`` context).

This is cloak's "Option 1: framework launches our binary directly" integration:
the framework (Playwright) launches cloak's compiled-in-patched Chromium. No
JavaScript injection, no config-only stealth — C++-level fingerprint patches.

It is a safety net: cloak activates for ANY Playwright scraper regardless of
whether ``code_writer`` preserved the template's ``STEALTH_BROWSER`` branch.
Cloak imports are deferred to launch-time to avoid import-time asyncio/playwright
side effects (which otherwise break ``sync_playwright().__enter__()``).
"""

import logging
import os

_logger = logging.getLogger("cloak_stealth")

if os.environ.get("STEALTH_BROWSER", "").strip().lower() == "cloak":

    def _cloak_overrides() -> dict:
        """Return {executable_path, args, ignore_default_args} for cloak's stealth Chromium.

        Uses ``build_args(...)`` (the SAME function ``cloakbrowser.launch()`` uses)
        rather than ``get_default_stealth_args()`` — the two pick different
        ``--fingerprint=<id>`` values, and heavy anti-bot (calvklein) blocks the
        one ``get_default_stealth_args`` returns.  ``ignore_default_args`` drops
        Playwright's ``--enable-automation`` tell.  Together these make Option 1
        (framework launches the binary) as stealthy as ``cloakbrowser.launch()``,
        without the dual-Playwright conflict.
        """
        from cloakbrowser.download import ensure_binary
        from cloakbrowser.browser import build_args, IGNORE_DEFAULT_ARGS

        chrome_args = build_args(
            True, [], timezone=None, locale=None, headless=True, extension_paths=None
        )
        return {
            "executable_path": ensure_binary(),
            "args": list(chrome_args),
            "ignore_default_args": list(IGNORE_DEFAULT_ARGS),
        }

    try:
        from playwright.sync_api import BrowserType as _SyncBT

        _orig_sync_launch = _SyncBT.launch

        def _sync_launch(self, **kwargs):
            ov = _cloak_overrides()
            kwargs["executable_path"] = ov["executable_path"]
            kwargs["args"] = list(kwargs.get("args") or []) + ov["args"]
            _existing = list(kwargs.get("ignore_default_args") or [])
            kwargs["ignore_default_args"] = list(
                dict.fromkeys(_existing + ov["ignore_default_args"])
            )
            _logger.info("cloak_stealth: sync launch() → CloakBrowser stealth binary")
            return _orig_sync_launch(self, **kwargs)

        _SyncBT.launch = _sync_launch

        from playwright.async_api import BrowserType as _AsyncBT

        _orig_async_launch = _AsyncBT.launch

        async def _async_launch(self, **kwargs):
            ov = _cloak_overrides()
            kwargs["executable_path"] = ov["executable_path"]
            kwargs["args"] = list(kwargs.get("args") or []) + ov["args"]
            _existing = list(kwargs.get("ignore_default_args") or [])
            kwargs["ignore_default_args"] = list(
                dict.fromkeys(_existing + ov["ignore_default_args"])
            )
            _logger.info("cloak_stealth: async launch() → CloakBrowser stealth binary")
            return await _orig_async_launch(self, **kwargs)

        _AsyncBT.launch = _async_launch

        _logger.info("cloak_stealth: wrapped BrowserType.launch → cloak binary")
    except Exception as exc:  # never break scraper startup
        _logger.warning("cloak_stealth: could not apply cloak patch: %s", exc)
