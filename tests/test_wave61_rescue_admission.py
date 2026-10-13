"""[wave-61 N2] Durable listing-rescue admissions + shape-B verdict-gated
admission (job 653 ultrabeauty).

653's post-mortem finding: execution went FAILED on a wall-clock death with
NO output file, the listing rescue silently skipped it, and when the log
stream later died mid-batch there was NO durable record that the decision
had even been made. Two fixes, both pinned here:

1. Every exit of ``_maybe_retry_execution_listing`` — skip OR fire — writes
   a durable record twice: a ``listing_rescue_admissions`` entry on the
   result dict (rides every return path) and one SessionLog row via
   ``_log_event_row``.
2. Shape B (FAILED, no on-disk output, no ``no_fresh_output`` flag) admits
   the rescue ONLY on direct probe evidence — a ``listing_probe_verdict``
   naming THIS primary as dead. A failed run has no yield evidence of its
   own; without the verdict it stays skipped.

The three shapes that already rescued (A: SUCCESS + zero-coverage file,
job 77; C: ``no_fresh_output``, job 88) are replayed through REAL producer
shapes as regression guards — wave-61 must not disturb them.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave61_rescue_admission.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()  # noqa: E402


PDP = "https://adoreme.example.com/kaia-black-2-1"
PROMO = "https://adoreme.example.com/bras"


def _state(verdict=None, criteria=""):
    st = {
        "input_mode": "list_page",
        "url": PDP,
        "navigation_analysis": {"discovery": {"listing_url": PROMO}},
    }
    if criteria:
        st["search_criteria"] = criteria
    if verdict is not None:
        st["listing_probe_verdict"] = verdict
    return st


def _verdict_on_primary():
    return {
        "verdict": "zero_yield",
        "candidate": PDP,
        "demotion_target": PROMO,
        "stop_reason": "empty_render",
        "attempts": [
            {"candidate": PDP, "crashed": False, "dead": True,
             "inconclusive": False},
            {"candidate": PROMO, "crashed": True, "dead": False,
             "inconclusive": False},
        ],
    }


_UNSET = object()


class _Harness:
    """Redispatch recorder + SessionLog recorder, shared by the replays."""

    def __init__(self, monkeypatch, retry_result=_UNSET):
        self.calls = []
        self.retry_result = (
            {"product_count": 12, "execution_status": "SUCCESS"}
            if retry_result is _UNSET else retry_result
        )
        self.rows = []

        def redispatch(alt):
            self.calls.append(alt)
            return self.retry_result

        def log_row(job_id, node, message):
            self.rows.append((job_id, node, message))

        monkeypatch.setattr("agents.graph._log_event_row", log_row)
        self.redispatch = redispatch

    def run(self, result, state, job_id=1):
        from webapp.agents.nodes.run_execution import (
            _maybe_retry_execution_listing,
        )

        return _maybe_retry_execution_listing(
            result, state, PDP, self.redispatch, job_id=job_id
        )

    def events(self, result):
        return [
            r.get("event")
            for r in (result or {}).get("listing_rescue_admissions", [])
        ]


# ─── replays through REAL producer shapes ────────────────────────────────────


class TestShapeAReplay:
    """Job-77 shape: SUCCESS + a zero-coverage output file on disk."""

    def test_zero_file_rescues_onto_the_promotion(self, monkeypatch, tmp_path):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "products": [],
            "metadata": {"discovery_coverage": {
                "discovered_urls": 0, "stop_reason": "",
            }},
        }), encoding="utf-8")
        h = _Harness(monkeypatch)
        result = h.run(
            {"execution_status": "SUCCESS", "product_count": 0,
             "output_file": str(out)},
            _state(),
        )
        assert h.calls == [PROMO], "the rescue must fire on the real shape-A"
        assert result is h.retry_result
        assert result["listing_fallback"]["adopted"] is True
        assert h.events(result)[-1] == "rescue_adopted"
        # the adopt boundary carries the primary's admission trail over
        assert "rescue_fired" in h.events(result)


class TestShapeBReplay:
    """Job-653 shape: wall-clock/transport FAILED, NO output file.

    The verdict gate is candidate-attributed: it opens only when the verdict
    names the primary execution ACTUALLY ran (``primary_listing``). With the
    demoted chain the alt comes from the 3-deep fallback — the verdict-dead
    candidate never serves as the rescue target.
    """

    def _failed_result(self):
        return {"execution_status": "FAILED",
                "error_message": "writer wall-clock exceeded"}

    def test_no_verdict_stays_skipped_but_records(self, monkeypatch):
        h = _Harness(monkeypatch)
        result = h.run(self._failed_result(), _state())
        assert h.calls == [], "no speculative rescue on an unmeasured primary"
        assert h.events(result) == ["skip_failed_no_rescue_evidence"]
        rec = result["listing_rescue_admissions"][0]
        assert rec["shape_b"] is True and rec["verdict_on_primary"] is False

    def test_verdict_on_primary_admits_the_rescue(self, monkeypatch):
        # PDP verdict-dead; the rescue's alt comes from the 3-deep fallback in
        # its promotion-first order (job-76/85) — the demoted-target ranking
        # keeps the promotion ahead of criteria, and the probe-proven-dead PDP
        # can never serve as the rescue target.
        h = _Harness(monkeypatch)
        result = h.run(
            self._failed_result(),
            _state(criteria="https://adoreme.example.com/bras?sort=new",
                   verdict=_verdict_on_primary()),
        )
        assert h.calls == [PROMO], (
            "verdict evidence admits shape B onto a surviving fallback"
        )
        assert h.events(result)[:2] == [
            "shape_b_admitted_by_verdict", "rescue_fired",
        ]
        assert h.events(result)[-1] == "rescue_adopted"

    def test_verdict_on_another_candidate_does_not_admit(self, monkeypatch):
        v = dict(_verdict_on_primary(), candidate=PROMO)
        h = _Harness(monkeypatch)
        result = h.run(self._failed_result(), _state(v))
        assert h.calls == []
        assert h.events(result) == ["skip_failed_no_rescue_evidence"]

    def test_non_zero_yield_verdict_does_not_admit(self, monkeypatch):
        v = dict(_verdict_on_primary(), verdict="crashed")
        h = _Harness(monkeypatch)
        result = h.run(self._failed_result(), _state(v))
        assert h.calls == []
        assert h.events(result) == ["skip_failed_no_rescue_evidence"]


class TestShapeCReplay:
    """Job-88 shape: clean rc=0, ``no_fresh_output`` — no file to read."""

    def test_no_fresh_output_rescues(self, monkeypatch):
        h = _Harness(monkeypatch)
        result = h.run(
            {"execution_status": "SUCCESS", "no_fresh_output": True,
             "product_count": 0},
            _state(),
        )
        assert h.calls == [PROMO], "the job-88 shape must keep rescuing"
        assert h.events(result)[0] == "rescue_fired"


# ─── durable instrumentation at every exit ───────────────────────────────────


class TestDurableAdmissions:
    def test_every_admission_writes_result_field_and_session_row(
        self, monkeypatch, tmp_path,
    ):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "products": [],
            "metadata": {"discovery_coverage": {"discovered_urls": 0}},
        }), encoding="utf-8")
        h = _Harness(monkeypatch)
        result = h.run(
            {"execution_status": "SUCCESS", "product_count": 0,
             "output_file": str(out)},
            _state(),
        )
        admissions = result["listing_rescue_admissions"]
        assert len(admissions) == len(h.rows), (
            "every admission record must also land one SessionLog row"
        )
        for (_jid, node, msg), rec in zip(h.rows, admissions):
            assert node == "run_execution"
            assert msg.startswith("LISTING_RESCUE_ADMISSION ")
            assert json.loads(msg[len("LISTING_RESCUE_ADMISSION "):])["event"] \
                == rec["event"]

    def test_skip_exit_is_durable_too(self, monkeypatch):
        h = _Harness(monkeypatch)
        result = h.run(
            {"execution_status": "SUCCESS", "product_count": 9}, _state(),
        )
        # a healthy run skips with a record, not silently
        assert h.events(result) == ["skip_not_a_clean_zero"]
        assert len(h.rows) == 1

    def test_no_alt_exit_is_durable(self, monkeypatch, tmp_path):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "metadata": {"discovery_coverage": {"discovered_urls": 0}},
        }), encoding="utf-8")
        h = _Harness(monkeypatch)
        result = h.run(
            {"execution_status": "SUCCESS", "product_count": 0,
             "output_file": str(out)},
            {"input_mode": "list_page", "url": PDP, "navigation_analysis": {}},
        )
        assert h.calls == []
        assert h.events(result) == ["skip_no_alt"]

    def test_fallback_also_zero_keeps_primary_durably(self, monkeypatch, tmp_path):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "metadata": {"discovery_coverage": {"discovered_urls": 0}},
        }), encoding="utf-8")
        h = _Harness(monkeypatch, retry_result={"product_count": 0})
        result = h.run(
            {"execution_status": "SUCCESS", "product_count": 0,
             "output_file": str(out)},
            _state(),
        )
        assert result is not h.retry_result
        assert h.events(result)[-1] == "rescue_zero_kept_primary"

    def test_unusable_retry_result_is_recorded(self, monkeypatch, tmp_path):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "metadata": {"discovery_coverage": {"discovered_urls": 0}},
        }), encoding="utf-8")
        h = _Harness(monkeypatch, retry_result=None)
        result = h.run(
            {"execution_status": "SUCCESS", "product_count": 0,
             "output_file": str(out)},
            _state(),
        )
        assert h.events(result)[-1] == "rescue_result_unusable"

    def test_rescue_is_bounded_at_one_redispatch(self, monkeypatch, tmp_path):
        out = tmp_path / "out.json"
        out.write_text(json.dumps({
            "metadata": {"discovery_coverage": {"discovered_urls": 0}},
        }), encoding="utf-8")
        h = _Harness(monkeypatch, retry_result={"product_count": 0})
        h.run(
            {"execution_status": "SUCCESS", "product_count": 0,
             "output_file": str(out)},
            _state(),
        )
        assert len(h.calls) == 1, "one bounded fallback — never a flailer"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
