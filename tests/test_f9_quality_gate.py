"""F9: nav-mode extraction-quality gate.

Prod shipped COMPLETED jobs with collapsed extractions: 330 (4 kept of 80
discovered, 95% failed), 335 (5/39, 87%), 337 (36 rows, zero core fields).
The gate fails nav-mode executions whose processed items are >=80% bad
(failed_products + core-less); warns at >=50%. url_list is out of scope.

Denominator is PROCESSED (good+bad), never total_discovered — a --limit run
of a healthy site must not false-positive.
"""
from __future__ import annotations

import json
import os
import sys
import types
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "webapp", "agents", "nodes", "run_execution.py")
with open(_SRC, "r", encoding="utf-8") as _fh:
    _code = _fh.read()

import re as _re

_shared: dict = {"os": os, "json": json, "time": __import__("time"),
                 "logger": mock.MagicMock(),
                 # [A3] the _item_has_core_field extraction window now spans
                 # the module-level _PRICE_*_RE = re.compile(...) globals.
                 "re": _re}
for _fname in ("_item_has_core_field", "_substantive_item_count",
               "_extraction_quality_gate"):
    _pat = _re.compile(
        r"(^def " + _fname + r"\(.*?)(?=^def |^class |\Z)", _re.S | _re.M
    )
    _m2 = _pat.search(_code)
    assert _m2, f"could not extract {_fname}"
    exec(compile(_m2.group(1), _SRC + ":" + _fname, "exec"), _shared)

_src_art = types.ModuleType("src.artifacts")
_src_art.latest_output_key = None
_src_art.read = None
sys.modules.setdefault("src.artifacts", _src_art)
_shared["__builtins__"] = __builtins__

_gate = _shared["_extraction_quality_gate"]


def _write(tmp_path, items, failed=0, name="output_x.json"):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w") as fh:
        json.dump({"products": items, "metadata": {"failed_products": failed}}, fh)
    return p


def _good(n=5):
    return [{"title": f"t{i}", "price": "$1"} for i in range(n)]


def _bad_rows(n=5):
    return [{"title": f"t{i}", "brand": "X"} for i in range(n)]


class TestFires:
    def test_prod_330_pattern_fails(self, tmp_path):
        # 4 good shipped, 76 failed_products → 76/80 = 95% bad
        p = _write(tmp_path, _good(4), failed=76)
        msg = _gate(p, "navigation", 4)
        assert msg and "quality gate" in msg.lower()
        assert "76/80" in msg

    def test_prod_337_pattern_fails(self, tmp_path):
        # 36 core-less rows, 0 failed_products → 36/36 = 100% bad
        p = _write(tmp_path, _bad_rows(36), failed=0)
        assert _gate(p, "navigation", 36)

    def test_prod_335_pattern_fails(self, tmp_path):
        # 5 good of 39 processed (34 failed) → 87%
        p = _write(tmp_path, _good(5), failed=34)
        assert _gate(p, "search_term", 5)


class TestDoesNotFire:
    def test_healthy_320_pattern_passes(self, tmp_path):
        # 511 good, 0 failed → passes
        p = _write(tmp_path, _good(511), failed=0)
        assert _gate(p, "navigation", 511) == ""

    def test_url_list_out_of_scope(self, tmp_path):
        # even 100% core-less rows: url_list is user-seeded, not gated here
        p = _write(tmp_path, _bad_rows(50), failed=50)
        assert _gate(p, "url_list", 50) == ""

    def test_low_processed_no_fire(self, tmp_path):
        # processed < 5 → no verdict (small-sample noise)
        p = _write(tmp_path, _good(1), failed=3)
        assert _gate(p, "navigation", 1) == ""

    def test_limit_truncation_no_false_positive(self, tmp_path):
        # denominator is processed, NOT total_discovered: 5 good processed,
        # discovery metadata says 1000 discovered but only 5 processed → pass
        p = _write(tmp_path, _good(5), failed=0)
        d = json.load(open(p))
        d["metadata"]["total_discovered"] = 1000
        json.dump(d, open(p, "w"))
        assert _gate(p, "navigation", 5) == ""

    def test_moderate_ratio_warns_not_fails(self, tmp_path):
        # 3 bad / 6 processed = 50% → warn only, returns ""
        p = _write(tmp_path, _good(3) + _bad_rows(3), failed=0)
        assert _gate(p, "navigation", 6) == ""

    def test_unreadable_output_passes(self, tmp_path):
        # gate must never invent a failure it can't compute
        p = os.path.join(str(tmp_path), "broken.json")
        open(p, "w").write("{nope")
        assert _gate(p, "navigation", 5) == ""

    def test_empty_output_passes(self, tmp_path):
        # no file → "" (F8 owns the no-output case)
        assert _gate("", "navigation", 0) == ""


class TestWiring:
    def test_gate_wired_at_both_success_returns(self):
        with open(_SRC) as fh:
            src = fh.read()
        assert src.count("_extraction_quality_gate(") >= 3  # def + 2 call sites
        assert "F9 quality gate (nav modes)" in src


class TestInputModeScopeRegression:
    """Railway job 1 (zquiet): the in-process F9 gate raised NameError
    'input_mode' — _run_in_process used the caller's local without it being a
    parameter, converting a SUCCESSFUL rc=0 execution (5 items) into
    execution_status=FAILED. This locks the parameter threading in."""

    def test_run_in_process_takes_input_mode(self):
        import inspect

        sig = inspect.signature(_shared["_run_in_process"]) if "_run_in_process" in _shared else None
        src = open(_SRC, encoding="utf-8").read()
        if sig is None:
            import re as _re
            m = _re.search(r"^def _run_in_process\(.*?\) ->", src, _re.S | _re.M)
            assert m, "fn not found"
            params = m.group(0)
            assert "input_mode" in params
        else:
            assert "input_mode" in sig.parameters

    def test_caller_passes_input_mode(self):
        src = open(_SRC, encoding="utf-8").read()
        assert "input_mode=input_mode," in src
