"""wave-29 Phase B — per-site code_writer memory on the File Master.

Problem: code_writer has zero cross-job memory. Re-drive a failed site →
fresh probe, fresh analyzers, wiped workspace (setup_workspace
PRESERVE_FILES=∅). ``_archive_failure_evidence`` writes test reports nobody
reads; skill_learner/nav_skill_review run SUCCESS-only; the only cross-job
writer fact is _prior_count_line (completed jobs). De-facto memory =
hand-pasted wave-gotcha strings.

Design (docs/plans/wave29-skills-memory-plan.md v2, Phase B):
- Store: FM ``scrapers/{slug}/analysis/writer_memory.json`` — ring buffer 12,
  per-failure-class cap 3, same-fingerprint-different-outcome supersedes,
  notes sanitized (URL-ban: truncate at first "http"; code fences stripped),
  per-slug flock around the RMW (running-sibling guard filters url NOT slug,
  so two URLs of one site CAN run concurrently).
- Reads: B4 first-attempt block (≤800 chars) beside _prior_count_line;
  B5 fingerprint-match lines (exact + degraded tiers, cap 2) in the wave-20
  retry slot with a probe-disagreement stale-guard.
- Deterministic injection > voluntary tool calls; memory is advisory.
"""
from __future__ import annotations

import os
import sys
import threading
import types
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import src.writer_memory as wm


def _fm_stub(state: dict | None = None):
    """Stub src.artifacts with a JSON-aware in-memory FM (same double-patch
    idiom as test_skills_store._PkgAttrPatch)."""
    state = state if state is not None else {}
    art = types.ModuleType("src.artifacts")
    art.read_json = mock.Mock(
        side_effect=lambda k: state[k] if k in state else (_ for _ in ()).throw(FileNotFoundError(k))
    )
    art.write_json = mock.Mock(side_effect=lambda k, v: state.__setitem__(k, v))
    art.scrapers_key = mock.Mock(
        side_effect=lambda slug, *parts: "/".join(["scrapers", slug, *parts])
    )
    return art, state


class _PkgAttrPatch:
    def __init__(self, art):
        self.art = art

    def __enter__(self):
        import src as _src_pkg

        self._real = getattr(_src_pkg, "artifacts", None)
        _src_pkg.artifacts = self.art
        self._dict = mock.patch.dict(sys.modules, {"src.artifacts": self.art})
        self._dict.__enter__()
        return self

    def __exit__(self, *a):
        import src as _src_pkg

        if self._real is not None:
            _src_pkg.artifacts = self._real
        else:
            delattr(_src_pkg, "artifacts")
        self._dict.__exit__(*a)


def _entry(job_id=1, outcome="failure", strategy="playwright", item_count=0,
           failure_class="crash", remediation_fp="", fp_parts=None, note="",
           probe_method="direct_http"):
    return {
        "job_id": job_id,
        "ts": 1700000000 + job_id,
        "outcome": outcome,
        "strategy": strategy,
        "item_count": item_count,
        "failure_class": failure_class,
        "remediation_fp": remediation_fp,
        "fp_parts": fp_parts or {},
        "probe_method": probe_method,
        "note": note,
    }


class TestSanitize:
    """The URL-ban sanitizer: fixes/lessons are memory; URL-shape facts are
    contamination-adjacent (H3 no-cross-run-URL-seeds made mechanical)."""

    def test_truncates_at_first_http(self):
        note = "discovery 403s; workaround https://site.com/collections/all works"
        out = wm.sanitize_note(note)
        assert "http" not in out
        assert out.startswith("discovery 403s")

    def test_strips_code_fences(self):
        out = wm.sanitize_note("```python\nuse jsonld\n```")
        assert "```" not in out
        assert "use jsonld" in out

    def test_caps_at_160_chars(self):
        out = wm.sanitize_note("x" * 500)
        assert len(out) <= 160

    def test_collapses_whitespace(self):
        out = wm.sanitize_note("a\n\n  b\tc")
        assert out == "a b c"


class TestLoad:
    def test_missing_file_returns_none(self):
        art, state = _fm_stub({})
        with _PkgAttrPatch(art):
            assert wm.load_memory("mysite") is None

    def test_corrupt_file_returns_none(self):
        art, _ = _fm_stub({})
        art.read_json = mock.Mock(side_effect=ValueError("bad json"))
        with _PkgAttrPatch(art):
            assert wm.load_memory("mysite") is None

    def test_roundtrip(self):
        art, state = _fm_stub({})
        with _PkgAttrPatch(art):
            wm.upsert_memory("mysite", entry=_entry(job_id=7, note="jsonld only"))
            mem = wm.load_memory("mysite")
        assert mem["site"] == "mysite"
        assert mem["entries"][0]["job_id"] == 7


class TestUpsert:
    def test_probe_fingerprint_stored(self):
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            mem = wm.upsert_memory(
                "mysite", entry=_entry(job_id=1),
                probe_fingerprint={"method": "fingerprint_chrome_none", "platform": "shopify"},
            )
        assert mem["probe_fingerprint"]["method"] == "fingerprint_chrome_none"
        assert mem["probe_fingerprint"]["platform"] == "shopify"

    def test_ring_buffer_max_12_newest_kept(self):
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            for j in range(1, 16):
                wm.upsert_memory("mysite", entry=_entry(job_id=j, failure_class=None))
            mem = wm.load_memory("mysite")
        ids = [e["job_id"] for e in mem["entries"]]
        assert len(ids) == 12
        assert ids == list(range(4, 16))  # oldest three dropped

    def test_per_failure_class_cap_3(self):
        """One repeating failure class cannot flood the slots. Trim runs per
        upsert, so by the time job 6 lands the crash entries already held
        jobs [3,4,5] — the cap keeps the newest 3 AT EACH STEP."""
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            for j in range(1, 6):
                wm.upsert_memory("mysite", entry=_entry(job_id=j, failure_class="crash"))
            wm.upsert_memory("mysite", entry=_entry(job_id=6, failure_class="timeout"))
            mem = wm.load_memory("mysite")
        crashes = [e for e in mem["entries"] if e["failure_class"] == "crash"]
        assert len(crashes) == 3
        assert [e["job_id"] for e in crashes] == [3, 4, 5]
        assert any(e["job_id"] == 6 for e in mem["entries"])

    def test_success_entries_exempt_from_class_cap(self):
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            for j in range(1, 6):
                wm.upsert_memory(
                    "mysite", entry=_entry(job_id=j, outcome="success", failure_class=None)
                )
            mem = wm.load_memory("mysite")
        assert len(mem["entries"]) == 5

    def test_supersede_same_fp_different_outcome(self):
        """Same structural remediation signature, new outcome → the older
        failure entry is marked superseded_by the new job."""
        parts = {"target": "price", "field": "price", "issue_types": ["WRONG_TYPE"], "exception": ""}
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            wm.upsert_memory(
                "mysite", entry=_entry(job_id=1, outcome="failure", remediation_fp="aaa", fp_parts=parts)
            )
            mem = wm.upsert_memory(
                "mysite", entry=_entry(job_id=2, outcome="success", remediation_fp="aaa", fp_parts=parts)
            )
        old = next(e for e in mem["entries"] if e["job_id"] == 1)
        assert old.get("superseded_by") == 2

    def test_no_supersede_same_outcome(self):
        parts = {"target": "price", "field": "price", "issue_types": ["WRONG_TYPE"], "exception": ""}
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            wm.upsert_memory("mysite", entry=_entry(job_id=1, outcome="failure", remediation_fp="aaa", fp_parts=parts))
            mem = wm.upsert_memory("mysite", entry=_entry(job_id=2, outcome="failure", remediation_fp="aaa", fp_parts=parts))
        old = next(e for e in mem["entries"] if e["job_id"] == 1)
        assert "superseded_by" not in old

    def test_notes_sanitized_on_write(self):
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            mem = wm.upsert_memory(
                "mysite", entry=_entry(job_id=1, note="see https://evil.example/payload for fix")
            )
        assert "http" not in mem["entries"][0]["note"]

    def test_lessons_digest_bounded_500(self):
        art, _ = _fm_stub({})
        with _PkgAttrPatch(art):
            for j in range(1, 6):
                wm.upsert_memory("mysite", entry=_entry(job_id=j, note=f"lesson {j}"))
            mem = wm.load_memory("mysite")
        assert mem["lessons_digest"]
        assert len(mem["lessons_digest"]) <= 500
        assert "lesson 5" in mem["lessons_digest"]  # newest present


class TestFlockConcurrency:
    """Two concurrent upserts must not lose an update — the running-sibling
    guard filters url=job.url NOT slug (tasks.py), so two URLs of one site
    CAN run concurrently and the FM write is a bare PUT."""

    def test_parallel_upserts_no_lost_update(self):
        art, state = _fm_stub({})
        with _PkgAttrPatch(art):
            barrier = threading.Barrier(2)

            def worker(start):
                barrier.wait()
                for j in range(start, start + 5):
                    wm.upsert_memory("mysite", entry=_entry(job_id=j, failure_class=f"c{j}"))

            t1 = threading.Thread(target=worker, args=(1,))
            t2 = threading.Thread(target=worker, args=(11,))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            mem = wm.load_memory("mysite")
        ids = {e["job_id"] for e in mem["entries"]}
        assert len(ids) == 10, f"lost update: {sorted(ids)}"


class TestDeriveFailureClass:
    """Whitelisted deterministic classes only — never free LLM text."""

    def test_crash(self):
        report = {"crash_error": "NameError: name 'x' is not defined"}
        assert wm.derive_failure_class(report) == "crash"

    def test_missing_fields(self):
        report = {"issues": [{"issue_type": "MISSING_FIELD"}, {"issue_type": "WRONG_TYPE"}]}
        assert wm.derive_failure_class(report) == "missing_fields"

    def test_empty_report_other(self):
        assert wm.derive_failure_class({}) == "other"
        assert wm.derive_failure_class(None) == "other"


class TestStructuralFingerprint:
    """Mirror of route_after_testing._remediation_fingerprint's structural
    keying (src stays django-free; wave-22 code untouched). Exact = all four
    parts; the hex must match the webapp side for the same report."""

    def test_hex_matches_route_after_testing(self):
        from agents.nodes.route_after_testing import _remediation_fingerprint as web_fp

        report = {
            "remediation": {"target": "price", "field": "price"},
            "issues": [{"issue_type": "wrong_type"}, {"issue_type": "missing_field"}],
            "crash_error": "TimeoutError: navigation timed out",
        }
        assert wm.structural_fingerprint(report) == web_fp(report)

    def test_empty_report_empty_hex(self):
        assert wm.structural_fingerprint({}) == ""
        assert wm.structural_fingerprint(None) == ""


class TestRenderFirstAttemptBlock:
    """B4: slim first-attempt block — hard cap 800 chars (every added char
    rides all writer turns; the 25KB ballooning precedent makes caps
    load-bearing)."""

    def test_empty_when_no_memory(self):
        assert wm.render_first_attempt_block(None) == ""
        assert wm.render_first_attempt_block({"entries": []}) == ""

    def test_lines_carry_job_strategy_outcome(self):
        mem = {"entries": [_entry(job_id=9, strategy="http_requests", outcome="failure",
                                  failure_class="http_status", note="listing 403 via bare requests")]}
        out = wm.render_first_attempt_block(mem)
        assert "job 9" in out
        assert "http_requests" in out
        assert "failure" in out
        assert "listing 403" in out

    def test_hard_cap_800_even_with_fat_digest(self):
        entries = [_entry(job_id=j, note="n" * 160, failure_class=None) for j in range(1, 13)]
        mem = {"entries": entries, "lessons_digest": "x" * 500}
        out = wm.render_first_attempt_block(mem)
        assert len(out) <= 800 + len("\n…(truncated)")


class TestRenderRetryFingerprintLines:
    """B5: the load-bearing surface — fingerprint-match lines for the wave-20
    retry slot (proven behavioral effect), exact + degraded tiers, cap 2,
    probe-disagreement stale-guard."""

    def _report(self, field="price"):
        return {
            "remediation": {"target": "price", "field": field},
            "issues": [{"issue_type": "WRONG_TYPE"}],
            "crash_error": "TypeError: unsupported operand",
        }

    def _parts(self, field="price"):
        return {"target": "price", "field": field,
                "issue_types": ["WRONG_TYPE"], "exception": "TypeError"}

    def test_exact_match_line(self):
        report = self._report()
        fp = wm.structural_fingerprint(report)
        mem = {"entries": [_entry(job_id=5, strategy="playwright", outcome="failure",
                                  failure_class="crash", remediation_fp=fp,
                                  fp_parts=self._parts(), probe_method="browser_none")]}
        lines = wm.render_retry_fingerprint_lines(report, mem, fresh_probe_method="browser_none")
        assert len(lines) == 1
        assert "job 5" in lines[0]
        assert "strategy playwright" in lines[0]
        assert "exact" in lines[0]

    def test_degraded_match_when_field_differs(self):
        report = self._report(field="title")
        fp = wm.structural_fingerprint(report)
        mem = {"entries": [_entry(job_id=5, remediation_fp=fp + "x",
                                  fp_parts=self._parts(field="price"),
                                  probe_method="browser_none")]}
        lines = wm.render_retry_fingerprint_lines(report, mem, fresh_probe_method="browser_none")
        assert len(lines) == 1
        assert "degraded" in lines[0]

    def test_cap_2(self):
        report = self._report()
        mem = {"entries": [_entry(job_id=j, fp_parts=self._parts(), probe_method="browser_none")
                           for j in (1, 2, 3, 4)]}
        lines = wm.render_retry_fingerprint_lines(report, mem, fresh_probe_method="browser_none")
        assert len(lines) == 2

    def test_stale_guard_drops_strategy_on_probe_disagreement(self):
        """Mechanical rule: when the entry's probe method disagrees with the
        fresh probe, strategy words are DROPPED (measured beats remembered).
        The failure mechanics (class/outcome/note) still surface."""
        report = self._report()
        mem = {"entries": [_entry(job_id=5, strategy="playwright", outcome="failure",
                                  failure_class="crash", fp_parts=self._parts(),
                                  probe_method="browser_none")]}
        lines = wm.render_retry_fingerprint_lines(report, mem, fresh_probe_method="fingerprint_chrome_none")
        assert len(lines) == 1
        assert "strategy" not in lines[0]
        assert "job 5" in lines[0]

    def test_no_match_no_lines(self):
        report = self._report()
        mem = {"entries": [_entry(job_id=5, fp_parts={"target": "discovery", "field": "",
                                                      "issue_types": ["EMPTY"], "exception": ""})]}
        assert wm.render_retry_fingerprint_lines(report, mem) == []

    def test_no_memory_no_lines(self):
        assert wm.render_retry_fingerprint_lines(self._report(), None) == []


class TestFirstAttemptStaleGuard:
    """B4 must drop strategy words on probe disagreement too — plan rule:
    strategy-flavored lines are dropped from ALL reads."""

    def test_stale_strategy_dropped(self):
        mem = {"entries": [_entry(job_id=9, strategy="playwright", outcome="failure",
                                  failure_class="crash", probe_method="browser_none")]}
        out = wm.render_first_attempt_block(mem, fresh_probe_method="fingerprint_chrome_none")
        assert "playwright" not in out
        assert "job 9" in out  # mechanics still surface

    def test_fresh_strategy_kept(self):
        mem = {"entries": [_entry(job_id=9, strategy="playwright", outcome="failure",
                                  failure_class="crash", probe_method="browser_none")]}
        out = wm.render_first_attempt_block(mem, fresh_probe_method="browser_none")
        assert "playwright" in out


class TestGraphCapture:
    """B1/B2: deterministic capture in graph.py — failure at the cleanup
    junction (next to _archive_failure_evidence), success at the
    skill_learner junction (SUCCESS-only guard). Notes composed from
    deterministic fields only."""

    def _graph(self):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from agents import graph

        return graph

    def _state(self, tmp_path, slug="capsite", job_id=42, status="FAILED",
               product_count=0):
        report = {
            "overall_assessment": "FAIL",
            "crash_error": "NameError: name 'discover_item_urls' is not defined",
            "issues": [{"issue_type": "CRASH"}],
            "remediation": {"target": "discovery", "field": ""},
        }
        ws = tmp_path / "workspace" / slug
        ws.mkdir(parents=True)
        import json

        (ws / "test_report.json").write_text(json.dumps(report))
        return {
            "site_slug": slug, "job_id": job_id, "execution_status": status,
            "product_count": product_count,
            "probe_result": {"connectivity": {"method_that_worked": "browser_none"}},
            "site_analysis": {"platform": "sfcc"},
            "scraper_analysis": {"strategy": "playwright"},
        }, report

    def test_failure_capture_writes_entry(self, tmp_path, monkeypatch):
        graph = self._graph()
        state, report = self._state(tmp_path)
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
        art, store = _fm_stub({})
        with _PkgAttrPatch(art):
            graph._record_writer_memory(state, outcome="failure")
            mem = wm.load_memory("capsite")
        e = mem["entries"][0]
        assert e["job_id"] == 42
        assert e["outcome"] == "failure"
        assert e["strategy"] == "playwright"
        assert e["failure_class"] == "crash"
        assert e["probe_method"] == "browser_none"
        assert e["remediation_fp"] == wm.structural_fingerprint(report)
        assert "discover_item_urls" in e["note"] or "crash" in e["note"].lower()

    def test_success_capture_writes_entry(self, tmp_path, monkeypatch):
        graph = self._graph()
        state, _ = self._state(tmp_path, status="SUCCESS", product_count=37)
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
        art, store = _fm_stub({})
        with _PkgAttrPatch(art):
            graph._record_writer_memory(state, outcome="success")
            mem = wm.load_memory("capsite")
        e = mem["entries"][0]
        assert e["outcome"] == "success"
        assert e["item_count"] == 37
        assert "37" in e["note"]

    def test_capture_never_raises(self, tmp_path, monkeypatch):
        graph = self._graph()
        state, _ = self._state(tmp_path)
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
        art, _ = _fm_stub({})
        art.write_json = mock.Mock(side_effect=RuntimeError("FM down"))
        with _PkgAttrPatch(art):
            graph._record_writer_memory(state, outcome="failure")  # must not raise

    def test_probe_fingerprint_recorded(self, tmp_path, monkeypatch):
        graph = self._graph()
        state, _ = self._state(tmp_path)
        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
        art, store = _fm_stub({})
        with _PkgAttrPatch(art):
            graph._record_writer_memory(state, outcome="failure")
            mem = wm.load_memory("capsite")
        assert mem["probe_fingerprint"]["method"] == "browser_none"
        assert mem["probe_fingerprint"]["platform"] == "sfcc"
