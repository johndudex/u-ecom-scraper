"""F8+F16 tests: output selection — mtime floor + best-of-N by substantive items.

Prod incidents this locks in:
- 319: code_tester's 5-item sample (written 32s BEFORE execution started) was
  credited to the run → job shipped 5 items as its own. F8's floor excludes it
  and the run fails honestly with no fresh output.
- 336/324: the newest file was a 0-item Phase-1 crash while a full file sat
  minutes older. F16 ranks by substantive count first, mtime tiebreak.
- FM laundering: a File-Master fallback download lands in a fresh-mtime
  tmpfile — a floored call must bypass the FM fallback entirely.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Extract the two functions under test without importing Django-bound modules:
# exec the source with the django-relative imports stubbed.
import types
import unittest.mock as mock

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "webapp", "agents", "nodes", "run_execution.py")
with open(_SRC, "r", encoding="utf-8") as _fh:
    _code = _fh.read()

# Extract both functions textually into ONE shared namespace so
# _find_newest_output can call _substantive_item_count (they reference each
# other at call time via the shared globals).
import re as _re

_shared: dict = {"os": os, "json": json, "time": time,
                 "logger": mock.MagicMock(), "tempfile": tempfile}
for _fname in ("_substantive_item_count", "_find_newest_output"):
    _pat = _re.compile(
        r"(^def " + _fname + r"\(.*?)(?=^def |^class |\Z)", _re.S | _re.M
    )
    _m2 = _pat.search(_code)
    assert _m2, f"could not extract {_fname}"
    exec(compile(_m2.group(1), _SRC + ":" + _fname, "exec"), _shared)

# `import src.artifacts as artifacts` inside the FM fallback resolves through
# sys.modules — pre-seed ONLY that submodule with a stub the FM tests control.
# src.content_types is left alone: the lazy real import (repo root is on
# sys.path) gives faithful output_filter_fields semantics — stubbing it would
# leak wrong content-type behavior into sibling test files (test_f15).
_src_art = types.ModuleType("src.artifacts")
_src_art.latest_output_key = None
_src_art.read = None
sys.modules.setdefault("src.artifacts", _src_art)
_shared["__builtins__"] = __builtins__

_find = _shared["_find_newest_output"]
_count = _shared["_substantive_item_count"]


def _write_output(directory: str, name: str, items: list[dict], age: float = 0.0) -> str:
    """Write an output_*.json with `age` seconds ago mtime; returns path."""
    payload = {"site": "t", "products": items,
               "metadata": {"failed_products": 0}}
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    past = time.time() - age
    os.utime(path, (past, past))
    return path


def _core_item(title: str = "T", price: str = "$1") -> dict:
    return {"title": title, "price": price}


def _coreless_item(title: str = "T") -> dict:
    return {"title": title, "brand": "X"}  # no price/availability


class TestSubstantiveCount:
    def test_counts_core_field_items(self, tmp_path):
        p = _write_output(str(tmp_path), "output_a.json",
                          [_core_item(), _core_item(), _coreless_item()])
        assert _count(p) == 2

    def test_coreless_all_zero(self, tmp_path):
        # prod 337: 36 brand-only rows -> 0 substantive
        p = _write_output(str(tmp_path), "output_b.json", [_coreless_item("A"), _coreless_item("B")])
        assert _count(p) == 0

    def test_unparseable_is_zero(self, tmp_path):
        p = os.path.join(str(tmp_path), "output_c.json")
        with open(p, "w") as fh:
            fh.write("{not json")
        assert _count(p) == 0

    def test_empty_list_zero(self, tmp_path):
        p = _write_output(str(tmp_path), "output_d.json", [])
        assert _count(p) == 0


class TestMtimeFloor:
    def test_floor_excludes_older_tester_sample(self, tmp_path):
        # prod 319: 5-item sample written 32s BEFORE subprocess start
        d = str(tmp_path)
        _write_output(d, "output_131950.json", [_core_item() for _ in range(5)], age=32.0)
        floor = time.time() - 1.0
        # No fresh file: floored call returns "" (no FM fallback, no stale credit)
        assert _find(d, mtime_floor=floor) == ""

    def test_floor_passes_fresh_file(self, tmp_path):
        d = str(tmp_path)
        fresh = _write_output(d, "output_fresh.json", [_core_item()], age=0.0)
        floor = time.time() - 5.0
        assert _find(d, mtime_floor=floor) == fresh

    def test_floored_call_never_hits_fm_fallback(self, tmp_path):
        # Even with a slug and NOTHING local, a floored call returns "" —
        # never downloads FM content into a fresh-mtime tmpfile.
        sys.modules["src.artifacts"].latest_output_key = lambda slug: "scrapers/x/output_1.json"
        sys.modules["src.artifacts"].read = lambda key: b"{}"
        assert _find(str(tmp_path), slug="x", mtime_floor=time.time() - 1) == ""


class TestBestOfN:
    def test_prefers_more_substantive_over_newer(self, tmp_path):
        # prod 336 pattern: newer 0-item crash file vs older 5-item file
        d = str(tmp_path)
        full = _write_output(d, "output_full.json", [_core_item() for _ in range(5)], age=120.0)
        _write_output(d, "output_empty_newer.json", [], age=10.0)
        assert _find(d) == full

    def test_mtime_tiebreak_on_equal_counts(self, tmp_path):
        d = str(tmp_path)
        older = _write_output(d, "output_older.json", [_core_item()], age=60.0)
        newer = _write_output(d, "output_newer.json", [_core_item()], age=5.0)
        assert _find(d) == newer

    def test_coreless_newest_loses_to_core_older(self, tmp_path):
        # prod 337-adjacent: newest file all brand-only rows
        d = str(tmp_path)
        core = _write_output(d, "output_core.json", [_core_item()], age=90.0)
        _write_output(d, "output_coreless.json", [_coreless_item() for _ in range(9)], age=1.0)
        assert _find(d) == core

    def test_legacy_call_keeps_fm_fallback(self, tmp_path):
        # graph.py:535/:3408 (tester + resume) pass no floor: FM fallback intact
        sys.modules["src.artifacts"].latest_output_key = lambda slug: "scrapers/x/output_1.json"
        sys.modules["src.artifacts"].read = lambda key: json.dumps({"products": [{"title": "x", "price": "$1"}]}).encode()
        out = _find(str(tmp_path), slug="x")
        assert out and out.startswith(tempfile.gettempdir())


class TestCallSiteWiring:
    def test_fresh_execution_site_passes_floor(self):
        # Static check: the in-process call site passes mtime_floor=start
        with open(_SRC, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "mtime_floor=start" in src, "fresh-execution call must floor at subprocess start"
        # And the failure branch exists for no fresh output
        assert "Execution produced no output file" in src
