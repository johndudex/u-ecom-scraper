"""[job-73 madewell] The freshness floor must not blind the CURRENT attempt,
and a no-op fix cycle must not spend the final test round.

Job 73 extracted 20/20 products in its last test cycle — and still FAILED
with "testing cascade exhausted". Two defects interacted:

RC1 (decisive): ``_invoke_code_tester`` stamped ``last_tested_at`` at node
EXIT — after the draft had already written its outputs. On a same-draft
re-test, ``_freshness_floor`` raised the floor to that stamp, excluding every
output INCLUDING the current attempt's own 20-product run. With
``code_tester`` never populating ``report.sample_products``, the file scan is
the only ground truth → the ground-truth override, the partial escape, and
the skip_approvals arm all saw zero items → cleanup → FAILED.

RC2 (trigger): the remediation ``code_writer`` made no edit (byte-identical
draft) and the A2 no-op gate — which escalates only on the SECOND consecutive
no-op — let that no-op spend the last test round.

Fixes pinned here:
- F1: ``last_tested_at`` is stamped at node ENTRY (before the tester runs) —
  prior attempts' outputs stay excluded (A6's protection), the current
  attempt's outputs are visible.
- F2: the primary output scan ranks candidate files by GOOD item count, not
  raw row count — an untagged 40-row ``--discover-only`` stub file must not
  outrank a 20-row extraction file (job-73 was one tag away from this).
- F3: ``_noop_should_escalate`` — a first no-op escalates immediately when
  the main retry budget is already exhausted.
- F5: when the floor excludes outputs, the scan SAYS so (a warning with the
  floor and the exclusion count — job 73 produced zero signal here).

Pure-python per the test_f15/test_job71 pattern (source extraction with
stubbed namespaces).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py")
GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")


def _grab(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, f"{name} not found"
    return m.group(0)


def _load_scan():
    src = open(NODE).read()
    fn_src = (
        _grab(src, "_freshness_floor")
        + _grab(src, "_is_dead_product")
        + _grab(src, "_is_discovery_output")
        + _grab(src, "_scraper_has_real_items")
    )
    from src.content_types import has_substantive_field, output_filter_fields

    ns = {
        "logging": logging,
        "logger": logging.getLogger("t.j73"),
        "output_filter_fields": output_filter_fields,
        "has_substantive_field": has_substantive_field,
        "ScrapeState": dict,
        "DEAD_STATUS_CODES": {"out of stock", "sold out"},
        "SOFT_404_MARKERS": ("soft 404", "product not found"),
        "__name__": "t_j73",
    }
    exec(fn_src, ns)
    return ns


def _ws(tmp_path, files: dict):
    """Draft FIRST (freshness floor keys off its mtime), then outputs."""
    ws = tmp_path / "workspace" / "acme-com"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft\n")
    for name, data in files.items():
        (ws / name).write_text(json.dumps(data))
    return ws


def _mtime(p, epoch):
    os.utime(p, (epoch, epoch))


GOOD_20 = {
    "products": [
        {"title": f"Shirt {i}", "price": f"${i}.99", "availability": "InStock"}
        for i in range(20)
    ]
}
STUBS_40 = {
    "products": [
        {"url": f"https://acme.com/p/{i}", "src_url": "https://acme.com/c/shirts"}
        for i in range(40)
    ]
}
BRAND_ONLY_36 = {
    "products": [{"title": f"B{i}", "brand": "Acme"} for i in range(36)]
}


# ─── F2: rank ground-truth files by GOOD items, not raw rows ─────────────────


class TestGoodCountRanking:
    def test_untagged_stub_file_does_not_outrank_extraction_file(
        self, tmp_path, monkeypatch
    ):
        """Job-73-shape, untagged (prod outputs have no phase tag yet): the
        NEWER 40-stub file must not beat the 20-product extraction file."""
        ns = _load_scan()
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        _ws(tmp_path, {"output_000001.json": GOOD_20, "output_000002.json": STUBS_40})
        _mtime(tmp_path / "workspace/acme-com/scraper_draft.py", 500)
        _mtime(tmp_path / "workspace/acme-com/output_000001.json", 1000)
        _mtime(tmp_path / "workspace/acme-com/output_000002.json", 2000)
        state = {
            "site_slug": "acme-com",
            "input_mode": "list_page",
            "content_type_config": {"content_type": "product", "output_key": "products"},
            "test_report": {"sample_products": []},
        }
        assert ns["_scraper_has_real_items"](state, min_count=1) is True

    def test_brand_only_file_still_cannot_rescue(self, tmp_path, monkeypatch):
        """F15 guard intact: ranking by GOOD count must not resurface the
        job-337 brand-only rescue."""
        ns = _load_scan()
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        _ws(tmp_path, {"output_000001.json": BRAND_ONLY_36})
        _mtime(tmp_path / "workspace/acme-com/scraper_draft.py", 500)
        _mtime(tmp_path / "workspace/acme-com/output_000001.json", 1000)
        state = {
            "site_slug": "acme-com",
            "input_mode": "list_page",
            "content_type_config": {"content_type": "product", "output_key": "products"},
            "test_report": {"sample_products": []},
        }
        assert ns["_scraper_has_real_items"](state, min_count=1) is False


# ─── F1: entry-stamp semantics keep A6's protection without self-blinding ────


class TestEntryStampSemantics:
    def test_current_attempt_outputs_visible_when_stamp_precedes_them(
        self, tmp_path, monkeypatch
    ):
        """Entry-stamp world: draft T0 → stamp T0+5 → current outputs T0+10.
        The floor (= stamp) excludes only what predates THIS test."""
        ns = _load_scan()
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        _ws(tmp_path, {"output_000001.json": GOOD_20})
        _mtime(tmp_path / "workspace/acme-com/scraper_draft.py", 1000)
        _mtime(tmp_path / "workspace/acme-com/output_000001.json", 1010)
        import hashlib

        fp = hashlib.sha1(open(tmp_path / "workspace/acme-com/scraper_draft.py", "rb").read()).hexdigest()
        state = {
            "site_slug": "acme-com",
            "input_mode": "list_page",
            "content_type_config": {"content_type": "product", "output_key": "products"},
            "test_report": {"sample_products": []},
            "last_tested_draft_fp": fp,
            "last_tested_at": 1005,  # node ENTRY — before the outputs
        }
        assert ns["_scraper_has_real_items"](state, min_count=1) is True

    def test_prior_attempt_outputs_stay_excluded(self, tmp_path, monkeypatch):
        """A6's actual protection, preserved: a PREVIOUS cycle's output
        (predating this test's entry stamp) still cannot rescue."""
        ns = _load_scan()
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        _ws(tmp_path, {"output_000001.json": GOOD_20})
        _mtime(tmp_path / "workspace/acme-com/scraper_draft.py", 1000)
        _mtime(tmp_path / "workspace/acme-com/output_000001.json", 1001)
        import hashlib

        fp = hashlib.sha1(open(tmp_path / "workspace/acme-com/scraper_draft.py", "rb").read()).hexdigest()
        state = {
            "site_slug": "acme-com",
            "input_mode": "list_page",
            "content_type_config": {"content_type": "product", "output_key": "products"},
            "test_report": {"sample_products": []},
            "last_tested_draft_fp": fp,
            "last_tested_at": 1005,
        }
        assert ns["_scraper_has_real_items"](state, min_count=1) is False

    def test_stamp_happens_before_the_tester_runs(self):
        """Source pin: the recorded ``last_tested_at`` value must be captured
        BEFORE the agent invoke in _invoke_code_tester (the job-73 defect was
        stamp-at-exit). The capture sits at node entry; the state update
        carries that captured value, never a fresh time.time()."""
        src = open(GRAPH).read()
        body = _grab(src, "_invoke_code_tester")
        capture = body.find("_test_started_at = time.time()")
        invoke = body.find("_invoke_agent_with_timeout(")
        assign = body.find('update["last_tested_at"] = _test_started_at')
        assert capture != -1 and invoke != -1 and assign != -1
        assert capture < invoke, (
            "the test timestamp must be captured at node ENTRY — the exit "
            "stamp excludes the current attempt's own outputs from the A6 "
            "floor (job-73 RC1)"
        )
        assert "time.time()" not in body.split('update["last_tested_at"]')[-1].split("\n")[0], (
            "last_tested_at must carry the entry capture, not a fresh stamp"
        )


# ─── F3: the no-op gate must be budget-aware ─────────────────────────────────


class TestNoopGateBudgetAwareness:
    def test_first_noop_on_a_non_final_round_is_allowed(self):
        from webapp.agents.graph import _noop_should_escalate

        assert _noop_should_escalate(1, 0) is False
        assert _noop_should_escalate(1, 1) is False

    def test_second_noop_always_escalates(self):
        from webapp.agents.graph import _noop_should_escalate

        assert _noop_should_escalate(2, 0) is True
        assert _noop_should_escalate(3, 1) is True

    def test_first_noop_on_the_final_round_escalates(self):
        """The job-73 shape: retry budget exhausted, writer ships a
        byte-identical draft — the last round must not be spent on it."""
        from webapp.agents.graph import _noop_should_escalate

        assert _noop_should_escalate(1, 2) is True

    def test_no_noop_never_escalates(self):
        from webapp.agents.graph import _noop_should_escalate

        assert _noop_should_escalate(0, 2) is False

    def test_gate_uses_the_budget_aware_helper(self):
        src = open(GRAPH).read()
        body = _grab(src, "_invoke_code_writer")
        assert "_noop_should_escalate(" in body


# ─── F5: the scan must SAY when the floor excluded the evidence ──────────────


class TestFloorObservability:
    def test_floor_exclusion_is_logged(self, tmp_path, monkeypatch, caplog):
        ns = _load_scan()
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        _ws(tmp_path, {"output_000001.json": GOOD_20})
        _mtime(tmp_path / "workspace/acme-com/scraper_draft.py", 1000)
        _mtime(tmp_path / "workspace/acme-com/output_000001.json", 1001)
        import hashlib

        fp = hashlib.sha1(open(tmp_path / "workspace/acme-com/scraper_draft.py", "rb").read()).hexdigest()
        state = {
            "site_slug": "acme-com",
            "input_mode": "list_page",
            "content_type_config": {"content_type": "product", "output_key": "products"},
            "test_report": {"sample_products": []},
            "last_tested_draft_fp": fp,
            "last_tested_at": 1005,
        }
        with caplog.at_level(logging.WARNING, logger="t.j73"):
            ns["_scraper_has_real_items"](state, min_count=1)
        assert any(
            "excluded" in r.getMessage().lower() or "floor" in r.getMessage().lower()
            for r in caplog.records
        ), "ground-truth scan must log floor exclusions (job-73 F5)"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
