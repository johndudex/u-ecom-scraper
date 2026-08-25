"""F19: retries-exhausted without test_report must NOT finalize COMPLETED.

Prod 352: code_writer timed out 3x (900s ballooning), tester never produced a
report, route_after_testing -> cleanup (no execution), and the finalize ladder
saw no error_message + no execution_status -> COMPLETED with 0 items (the D2
pattern in new clothes). The fix: _invoke_code_tester records error_message +
execution_status=FAILED when the last attempt yields no report AND no real
output exists (the rescue path handles the has-output case).
"""
from __future__ import annotations

import os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE) if os.path.basename(os.path.dirname(_HERE)) != "webapp" \
    else os.path.dirname(os.path.dirname(_HERE))

_G = os.path.join(ROOT, "webapp", "agents", "graph.py")
_src = open(_G).read()


class TestF19:
    def test_error_set_on_exhausted_no_report(self):
        assert "Testing exhausted" in _src
        assert 'update["execution_status"] = "FAILED"' in _src

    def test_gated_on_last_attempt(self):
        # aab0f21: the original `_is_last = is_final_attempt or
        # retry_count >= MAX_TEST_RETRIES` raised NameError — is_final_attempt
        # is route_after_testing's local, never defined in _invoke_code_tester
        # (Railway job 4 died mid-graph). The gate now derives is-final
        # locally with the same truth table (route_after_testing.py defines
        # is_final_attempt = retry_count == FINAL_RETRY_SENTINEL).
        assert re.search(
            r"_is_last = \(\s*"
            r"retry_count == FINAL_RETRY_SENTINEL\s*"
            r"or retry_count >= MAX_TEST_RETRIES\s*"
            r"\)",
            _src,
        ), "exhausted-retry honesty gate must derive is-final attempt locally"

    def test_rescue_guard_present(self):
        # only fires when NO real output items exist (rescue path owns that case)
        assert "_has_real_out" in _src
        i_guard = _src.index("_has_real_out = False")
        i_fire = _src.index("if _is_last and not _has_real_out:")
        assert i_guard < i_fire

    def test_MAX_TEST_RETRIES_imported(self):
        assert "MAX_TEST_RETRIES,\n)" in _src

    def test_import_of_substantive_count(self):
        assert "_substantive_item_count,\n" in _src
