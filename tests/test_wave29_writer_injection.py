"""wave-29 Phase B injection surfaces — B4/B5 into the writer's prompt.

B4: slim first-attempt block beside _prior_count_line (hard cap 800 —
every added char rides all writer turns; 25KB ballooning precedent).
B5: fingerprint-match lines inside _summarize_test_report — the wave-20
retry slot, the one injection point with proven behavioral effect (its
comment documents the exact pathology: regenerated drafts repeating a
defect because retry context never stated the rule). Probe-disagreement
stale-guard drops strategy words mechanically.

Deterministic injection > voluntary tool calls (plan v2 principle).
"""
from __future__ import annotations

import os
import sys
import types
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from agents import subagents as sub  # noqa: E402

import src.writer_memory as wm  # noqa: E402


def _fm_stub(state: dict):
    art = types.ModuleType("src.artifacts")
    art.read_json = mock.Mock(
        side_effect=lambda k: state[k] if k in state else (_ for _ in ()).throw(FileNotFoundError(k))
    )
    art.write_json = mock.Mock(side_effect=lambda k, v: state.__setitem__(k, v))
    art.scrapers_key = mock.Mock(
        side_effect=lambda slug, *parts: "/".join(["scrapers", slug, *parts])
    )
    return art


class _PkgAttrPatch:
    def __init__(self, art):
        self.art = art

    def __enter__(self):
        import src as _src_pkg

        self._real = getattr(_src_pkg, "artifacts", None)
        _src_pkg.artifacts = self.art
        self._dict = mock.patch.dict(sys.modules, {"src.artifacts": self.art})
        self._dict.__enter__()

    def __exit__(self, *a):
        import src as _src_pkg

        if self._real is not None:
            _src_pkg.artifacts = self._real
        else:
            delattr(_src_pkg, "artifacts")
        self._dict.__exit__(*a)


BASE = {
    "site_slug": "mysite-com",
    "url": "https://www.mysite.com/store",
    "sample_url": "https://www.mysite.com/product/1",
    "strategy": "http_requests",
}


def _fm_store_with(mem: dict) -> dict:
    """_fm_stub takes the KEY→VALUE store (not the memory object itself)."""
    return {"scrapers/mysite-com/analysis/writer_memory.json": mem}


def _mem_state(job_id=5):
    report = {
        "remediation": {"target": "price", "field": "price"},
        "issues": [{"issue_type": "WRONG_TYPE"}],
        "crash_error": "TypeError: unsupported operand",
    }
    parts = wm.fingerprint_parts(report)
    return {
        "site": "mysite-com",
        "updated": 1700000000,
        "probe_fingerprint": {"method": "browser_none", "platform": "sfcc"},
        "entries": [{
            "job_id": job_id, "ts": 1700000000, "outcome": "failure",
            "strategy": "playwright", "item_count": 0, "failure_class": "crash",
            "remediation_fp": wm.structural_fingerprint(report),
            "fp_parts": parts, "probe_method": "browser_none",
            "note": "bare requests got 403 on listing",
        }],
        "lessons_digest": "bare requests got 403 on listing",
    }


class TestFirstAttemptBlock:
    """B4 beside _prior_count_line — first attempt (no test report)."""

    def test_first_attempt_carries_site_memory(self):
        art = _fm_stub(_fm_store_with(_mem_state()))
        with _PkgAttrPatch(art):
            msg = str(sub.build_code_writer_message(dict(BASE))[0].content)
        assert "SITE MEMORY" in msg, "B4 block missing on first attempt"
        assert "job 5" in msg
        assert "playwright" in msg

    def test_no_memory_no_block(self):
        art = _fm_stub({})
        with _PkgAttrPatch(art):
            msg = str(sub.build_code_writer_message(dict(BASE))[0].content)
        assert "SITE MEMORY" not in msg

    def test_block_hard_capped_800(self):
        state = _mem_state()
        state["entries"] = [
            dict(state["entries"][0], job_id=j, note="n" * 160) for j in range(1, 13)
        ]
        state["lessons_digest"] = "x" * 500
        art = _fm_stub(_fm_store_with(state))
        with _PkgAttrPatch(art):
            msg = str(sub.build_code_writer_message(dict(BASE))[0].content)
        seg = msg.split("SITE MEMORY", 1)[1] if "SITE MEMORY" in msg else ""
        assert len("SITE MEMORY" + seg.split("\n###")[0]) <= 820


class TestRetryFingerprintLines:
    """B5 in _summarize_test_report — the wave-20 retry slot."""

    REPORT = {
        "overall_assessment": "FAIL",
        "confidence_score": 0.3,
        "issues": [{"issue_type": "WRONG_TYPE", "field": "price",
                    "description": "price extracted as string"}],
        "remediation": {"target": "price", "field": "price"},
        "crash_error": "TypeError: unsupported operand",
    }

    def _retry_state(self, probe_method="browser_none"):
        return dict(
            BASE, test_retry_count=1, test_report=dict(self.REPORT),
            probe_result={"connectivity": {"method_that_worked": probe_method}},
        )

    def test_matching_fingerprint_surfaces_prior_job(self):
        art = _fm_stub(_fm_store_with(_mem_state()))
        with _PkgAttrPatch(art):
            msg = str(sub.build_code_writer_message(self._retry_state())[0].content)
        assert "SITE MEMORY:" in msg
        assert "job 5" in msg
        assert "strategy playwright" in msg  # probe agrees → strategy survives

    def test_stale_guard_drops_strategy_words(self):
        art = _fm_stub(_fm_store_with(_mem_state()))
        with _PkgAttrPatch(art):
            msg = str(sub.build_code_writer_message(
                self._retry_state(probe_method="fingerprint_chrome_none")
            )[0].content)
        assert "SITE MEMORY:" in msg  # line still surfaces (mechanics matter)
        assert "strategy playwright" not in msg  # stale strategy dropped

    def test_non_matching_report_no_memory_lines(self):
        state = self._retry_state()
        state["test_report"] = dict(
            self.REPORT,
            remediation={"target": "discovery", "field": ""},
            issues=[{"issue_type": "EMPTY", "field": "products"}],
            crash_error="",
        )
        art = _fm_stub(_fm_store_with(_mem_state()))
        with _PkgAttrPatch(art):
            msg = str(sub.build_code_writer_message(state)[0].content)
        assert "SITE MEMORY:" not in msg
