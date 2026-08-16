"""F2+M2: CDP-connect failures are retryable Chrome death despite their
traceback; restarts carry a cooldown against concurrent /scrape thrash.

Prod 325/328/334: `connect_over_cdp(http://127.0.0.1:9223)` → ECONNREFUSED
1-3s after an orphan-kill cycle. The traceback made the classifier call it
a code bug → no retry, though a Chrome restart was exactly the remedy.

Pure-python: browser_service modules are importable directly (httpx dep
only at call time — stubbed).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_STUB = None


def _load_runner():
    global _STUB
    if _STUB is not None:
        return _STUB
    # stub httpx (only used inside _restart_scraper_chrome / run_scraper_script)
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.post = lambda *a, **kw: types.SimpleNamespace(status_code=503, text="stub")
    sys.modules.setdefault("httpx", httpx_stub)
    spec = importlib.util.spec_from_file_location(
        "sr_test", os.path.join(ROOT, "browser_service", "scraper_runner.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _STUB = mod
    return mod


class TestCdpConnectClassifier:
    def setup_method(self):
        self.m = _load_runner()

    def test_connect_over_cdp_econnrefused_with_traceback_is_chrome_death(self):
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "scraper_draft.py", line 368, in main\n'
            "    browser = get_browser(p)\n"
            '  File "...", line 84, in get_browser\n'
            "    pw.chromium.connect_over_cdp('http://127.0.0.1:9223')\n"
            "playwright._impl._errors.Error: BrowserType.connect_over_cdp: "
            "connect ECONNREFUSED 127.0.0.1:9223\n"
        )
        assert self.m._is_cdp_connect_failure(stderr) is True

    def test_target_closed_variant(self):
        stderr = "Traceback (most recent call last):\n...connect_over_cdp...Target closed\n"
        assert self.m._is_cdp_connect_failure(stderr) is True

    def test_plain_code_bug_not_matched(self):
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "scraper_draft.py", line 10, in <module>\n'
            "NameError: name 'foo' is not defined\n"
        )
        assert self.m._is_cdp_connect_failure(stderr) is False

    def test_site_connection_error_without_cdp_naming_not_matched(self):
        # a genuine code bug connecting to some dead LOCAL service — retrying
        # is pointless, and the CDP-endpoint naming is what discriminates.
        stderr = "Traceback ... requests.exceptions.ConnectionError: [Errno 111] Connection refused api.local\n"
        assert self.m._is_cdp_connect_failure(stderr) is False

    def test_empty(self):
        assert self.m._is_cdp_connect_failure("") is False
        assert self.m._is_cdp_connect_failure(None) is False


class TestClassifierIntegration:
    """The retry loop's classification expression includes the CDP branch."""

    def test_classification_expression(self):
        src = open(os.path.join(ROOT, "browser_service/scraper_runner.py")).read()
        assert "_is_cdp_connect_failure(stderr)  # F2: retry despite traceback" in src


class TestRestartCooldown:
    def test_cooldown_constant_and_state(self):
        m = _load_runner()
        assert m._RESTART_COOLDOWN_S == 30.0
        assert hasattr(m, "_last_restart_ts")

    def test_second_restart_within_cooldown_skipped(self):
        import unittest.mock as mk
        m = _load_runner()
        calls = []
        with mk.patch.object(m.httpx, "post", side_effect=lambda *a, **kw: calls.append(1) or types.SimpleNamespace(status_code=200, text="ok")):
            m._restart_scraper_chrome()   # performs the POST, sets _last_restart_ts
            m._restart_scraper_chrome()   # within cooldown → skipped
        assert len(calls) == 1, f"expected exactly 1 POST, got {len(calls)}"
