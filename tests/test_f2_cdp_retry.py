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
        assert "_is_target_closed(stderr)  # F3 [job-140]: retry despite traceback" in src


class TestTargetClosedClassifier:
    """F3 [job-140]: playwright ≥1.4x renamed the crash surface. The exception
    is `TargetClosedError` and its message is "Target page, context or browser
    has been closed" — no legacy marker matches it, and it ALWAYS rides a
    Python traceback (the draft failed to catch it), so the traceback veto
    suppressed the retry every time. Prod job-140: discovery found 23 URLs,
    Chrome died mid-run, the run was recorded failed with zero retries."""

    def setup_method(self):
        self.m = _load_runner()

    # the EXACT prod job-140 stderr shape
    JOB140_STDERR = (
        "Traceback (most recent call last):\n"
        '  File "scraper_draft.py", line 412, in discover\n'
        "    page = context.new_page()\n"
        "playwright._impl._errors.TargetClosedError: "
        "BrowserContext.new_page: Target page, context or browser has been closed\n"
    )

    def test_job140_stderr_was_invisible_to_the_legacy_classifier(self):
        # the gap this fix closes: legacy markers say no AND the traceback veto
        # says no — the run was recorded failed without any retry.
        assert self.m._is_chrome_death(self.JOB140_STDERR) is False
        assert self.m._has_traceback(self.JOB140_STDERR) is True

    def test_job140_stderr_is_now_target_closed(self):
        assert self.m._is_target_closed(self.JOB140_STDERR) is True

    def test_exception_name_alone_matches(self):
        assert self.m._is_target_closed(
            "playwright._impl._errors.TargetClosedError: Page.goto: Target closed\n"
        ) is True

    def test_lowercase_browser_variant_matches(self):
        # "browser has been closed" (lowercase) lost the case-sensitive legacy
        # match — the relaxed check is case-insensitive for exactly this reason.
        assert self.m._is_target_closed(
            "Error: page.screenshot: browser has been closed\n"
        ) is True

    def test_plain_code_bug_not_matched(self):
        stderr = (
            "Traceback (most recent call last):\n"
            '  File "scraper_draft.py", line 10, in <module>\n'
            "NameError: name 'foo' is not defined\n"
        )
        assert self.m._is_target_closed(stderr) is False

    def test_site_outage_not_matched(self):
        # a page that fails to LOAD is not a dead browser — same exclusion the
        # legacy list documents for net::ERR_* (a retry would re-fail identically)
        stderr = (
            "Traceback (most recent call last):\n"
            "playwright._impl._errors.Error: Page.goto: net::ERR_CONNECTION_TIMED_OUT\n"
        )
        assert self.m._is_target_closed(stderr) is False

    def test_empty_and_none(self):
        assert self.m._is_target_closed("") is False
        assert self.m._is_target_closed(None) is False


class TestTargetClosedRetryEndToEnd:
    """Job-140's failure end-to-end: a script that dies with TargetClosedError
    on attempt 1 must take the crash path (Chrome restart + retry) and recover
    on attempt 2 — not be recorded failed with zero retries."""

    def test_target_closed_failure_is_retried_and_recovers(self, monkeypatch, tmp_path):
        m = _load_runner()
        script = tmp_path / "flaky_target.py"
        marker = tmp_path / "attempts.txt"
        script.write_text(
            "import sys\n"
            f"marker = {str(marker)!r}\n"
            "with open(marker, 'a') as fh: fh.write('x')\n"
            "if len(open(marker).read()) == 1:\n"
            "    sys.stderr.write(\n"
            "        'Traceback (most recent call last):\\n'\n"
            "        'playwright._impl._errors.TargetClosedError: '\n"
            "        'BrowserContext.new_page: Target page, context or browser has been closed\\n')\n"
            "    sys.exit(1)\n"
            "with open('output_test.json', 'w') as fh: fh.write('{\"products\": [1, 2]}')\n"
        )
        restarts = []
        monkeypatch.setattr(m, "_restart_scraper_chrome", lambda: restarts.append(1))
        monkeypatch.setattr(m.time, "sleep", lambda s: None)  # skip backoff wait
        result = m._run_scraper_script_impl(str(script), timeout=60, max_retries=3)
        assert result["returncode"] == 0, result["stderr"][:300]
        assert restarts == [1], "the TargetClosed attempt must take the crash path"
        assert marker.read_text() == "xx", "the script must actually run twice"
        assert result["product_count"] == 2


class TestNavigateWiring:
    """/navigate maps a dead target to 503 crash — including the F3 shape —
    instead of a generic 502 page failure."""

    def test_navigate_uses_the_relaxed_death_check(self):
        with open(os.path.join(ROOT, "browser_service", "server.py")) as fh:
            src = fh.read()
        assert "_is_chrome_death(err) or _is_target_closed(err)" in src
        assert "_is_chrome_death(str(exc)) or _is_target_closed(str(exc))" in src


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
