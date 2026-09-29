"""[wave-49] Honest recording — the job-468 class.

Plan: docs/plans/wave49-honest-recording-plan.md. Job 468 (tatcha-com,
list_page, firstn/5) recorded COMPLETED/0 while its own published output held
five real rows: ``price: 0.0`` 1-ml samples with ``status_code: 200``. The
graph counted 5 (checkpoint: execution_status=SUCCESS, product_count=5); the
finalize ground-truth override zeroed it through ``has_substantive_field``'s
bare-truthiness test, and ``_scope_shortfall_row``'s ``count <= 0`` exemption
then silenced the honesty marker for exactly that asked-5-shipped-0 shape.

Three fixes, each pinned here:

- F1  ``has_substantive_field``: a typed numeric zero (int/float, bool
      excluded) IS a value; plus the override stops treating a MISSING
      ``status_code`` key as a failed fetch (F1b).
- F2  ``_scope_shortfall_row`` fires at zero — the marker exists precisely
      for the COMPLETED-0-with-rows shape (the old "zero-item runs fail
      loudly" premise is what job 468 falsified).
- F3  ``_real_items_evidence`` FM source (c): payload-timestamp-first window
      gate — an in-payload ``site.scraped_at``/row ``scraped_at`` outranks
      the filename in BOTH directions, killing the verified 86-phantom-row
      mis-credit (81 rows from a Sep-16 file a prior job left in the FM,
      immortal under the prune's partner-owned exemption).
"""

import json

import pytest
from django.conf import settings
from django.utils import timezone

from src.content_types import has_substantive_field


# ── F1 — numeric zero is data ────────────────────────────────────────────────


class TestHasSubstantiveFieldNumericZero:
    def test_zero_price_row_is_substantive(self):
        # The exact job-468 row shape: real $0.00 1-ml sample PDP.
        row = {"title": "The Silk Cream | 1ml", "price": 0.0,
               "url": "https://x/p/1", "status_code": 200}
        assert has_substantive_field(row) is True

    def test_int_zero_is_substantive(self):
        assert has_substantive_field({"title": "t", "quantity": 0}) is True

    def test_empty_string_price_still_not_substantive(self):
        assert has_substantive_field(
            {"title": "t", "price": "", "url": "u"}) is False

    def test_none_value_still_not_substantive(self):
        assert has_substantive_field(
            {"title": "t", "price": None, "url": "u"}) is False

    def test_title_only_still_not_substantive(self):
        # title is bookkeeping (_ALWAYS_PRESENT_FIELDS) — unchanged.
        assert has_substantive_field({"title": "t", "url": "u"}) is False

    def test_false_flag_still_not_substantive(self):
        # bool excluded from the numeric carve-out: a lone False stays empty.
        assert has_substantive_field(
            {"title": "t", "on_sale": False, "url": "u"}) is False

    def test_true_flag_is_substantive(self):
        assert has_substantive_field(
            {"title": "t", "on_sale": True, "url": "u"}) is True


class TestFinalizeOverrideCount:
    """The 468 replay, at unit level: the override's row counter."""

    def _count(self, items):
        from scraper.tasks import _override_count_items

        return _override_count_items(items)

    def test_five_zero_price_rows_count_five(self):
        # The 468 payload: five $0.00 samples — the override must record 5.
        items = [{"title": f"Sample {i}", "price": 0.0,
                  "url": f"https://x/p/{i}", "status_code": 200}
                 for i in range(5)]
        assert self._count(items) == 5

    def test_written_zero_status_code_still_rejected(self):
        items = [{"title": "p", "price": "$1", "status_code": 0}]
        assert self._count(items) == 0

    def test_missing_status_code_no_longer_zeroes(self):
        # F1b: absence of evidence of fetch failure is not fetch failure.
        items = [{"title": "p", "price": "$1"} for _ in range(3)]
        assert self._count(items) == 3

    def test_none_status_code_no_longer_zeroes(self):
        items = [{"title": "p", "price": "$1", "status_code": None}]
        assert self._count(items) == 1

    def test_stub_rows_still_rejected(self):
        # No field beyond bookkeeping → still not counted.
        items = [{"title": "p", "url": "u", "status_code": 200}]
        assert self._count(items) == 0

    def test_mixed_real_and_stub(self):
        items = [{"title": "real", "price": "$5", "status_code": 200},
                 {"title": "stub", "url": "u", "status_code": 200}]
        assert self._count(items) == 1


# ── F2 — the shortfall marker fires at zero ─────────────────────────────────


class TestShortfallRowFiresAtZero:
    def test_zero_shipped_produces_row(self):
        # Reversed wave-48 pin: job 468 proved a zero-item run can be
        # recorded COMPLETED, so asked-N-shipped-0 must be visible.
        from scraper.tasks import _scope_shortfall_row

        row = _scope_shortfall_row("firstn", "10", 0)
        assert row.startswith("[SCOPE-SHORTFALL]")
        assert "asked 10" in row and "shipped 0" in row

    def test_partial_still_produces_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("firstn", "10", 2).startswith(
            "[SCOPE-SHORTFALL]")

    def test_scope_met_still_no_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("firstn", "10", 10) == ""
        assert _scope_shortfall_row("firstn", "10", 12) == ""

    def test_non_firstn_still_no_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("all", "", 0) == ""

    def test_garbage_still_no_row(self):
        from scraper.tasks import _scope_shortfall_row

        assert _scope_shortfall_row("firstn", "abc", 0) == ""


# ── F3 — the FM rescue window ────────────────────────────────────────────────


def _fm(monkeypatch, keys, payloads):
    """Stub the File Master the wave-40 way (collection-time module traps)."""
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    monkeypatch.setattr(real, "list_keys", lambda prefix="": keys)
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))


def _root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))


def _job(started_at):
    from scraper.models import ScrapeJob

    return ScrapeJob(url="https://x.example/", started_at=started_at)


def _rows(n):
    return [{"title": f"p{i}", "price": "$1", "url": f"https://x/p/{i}"}
            for i in range(n)]


class TestFmRescueWindow:
    def test_stale_payload_excluded_even_with_fresh_name(self, tmp_path,
                                                          monkeypatch):
        # The verified 86-phantom shape: a prior job's output whose PAYLOAD
        # predates this job's window must never be admitted, name aside.
        from scraper import tasks

        started = timezone.now() - timezone.timedelta(hours=2)
        stale_iso = (timezone.now() - timezone.timedelta(days=13)).isoformat()
        key = "scrapers/tatcha-com/output_2026-09-29_120000_000001_1.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"site": {"scraped_at": stale_iso}, "products": _rows(81)})})
        _root(monkeypatch, tmp_path)
        n, loc = tasks._real_items_evidence(
            "tatcha-com", _job(started),
            final_state={"tested_draft_sha256": "a"})
        assert n == 0 and loc == ""

    def test_fresh_payload_admitted_despite_old_name(self, tmp_path,
                                                     monkeypatch):
        # The prod-770 shape, under the refined contract: a same-run publish
        # whose NAME is older than the window is admitted on its payload.
        from scraper import tasks

        started = timezone.now() - timezone.timedelta(hours=2)
        fresh_iso = timezone.now().isoformat()
        key = "scrapers/sephora-cz/output_2026-09-20_235455_000001_99.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"site": {"scraped_at": fresh_iso}, "products": _rows(19)})})
        _root(monkeypatch, tmp_path)
        n, loc = tasks._real_items_evidence(
            "sephora-cz", _job(started),
            final_state={"last_tested_draft_fp": "abc"})
        assert n == 19 and loc.endswith(key.rsplit("/", 1)[-1])

    def test_row_level_scraped_at_backfills_missing_site_block(
            self, tmp_path, monkeypatch):
        from scraper import tasks

        started = timezone.now() - timezone.timedelta(hours=2)
        stale_iso = (timezone.now() - timezone.timedelta(days=13)).isoformat()
        key = "scrapers/tatcha-com/output_2026-09-29_130000_000001_2.json"
        rows = [{"title": "p", "price": "$1", "scraped_at": stale_iso}]
        _fm(monkeypatch, [key], {key: json.dumps({"products": rows})})
        _root(monkeypatch, tmp_path)
        n, _ = tasks._real_items_evidence(
            "tatcha-com", _job(started),
            final_state={"tested_draft_sha256": "a"})
        assert n == 0

    def test_unstamped_payload_with_old_name_still_excluded(
            self, tmp_path, monkeypatch):
        # Honest under-credit: no payload timestamp, pre-window name → the
        # name fallback excludes it (today's only signal, unchanged).
        from scraper import tasks

        started = timezone.now() - timezone.timedelta(hours=2)
        key = "scrapers/sephora-cz/output_2026-09-20_235455_000001_99.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(19)})})
        _root(monkeypatch, tmp_path)
        n, _ = tasks._real_items_evidence(
            "sephora-cz", _job(started),
            final_state={"last_tested_draft_fp": "abc"})
        assert n == 0

    def test_unstamped_payload_with_fresh_name_still_admitted(
            self, tmp_path, monkeypatch):
        # The wave-40 770 fixture contract (name fallback, fresh name) —
        # unchanged for keys whose payload carries no timestamp.
        from scraper import tasks

        started = timezone.now() - timezone.timedelta(hours=2)
        key = f"scrapers/sephora-cz/output_2026-09-29_235455_000001_99.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(19)})})
        _root(monkeypatch, tmp_path)
        n, loc = tasks._real_items_evidence(
            "sephora-cz", _job(started),
            final_state={"last_tested_draft_fp": "abc"})
        assert n == 19

    def test_unparsable_timestamp_degrades_to_name(self, tmp_path,
                                                   monkeypatch):
        from scraper import tasks

        started = timezone.now() - timezone.timedelta(hours=2)
        key = "scrapers/x-com/output_2026-09-29_140000_000001_3.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"site": {"scraped_at": "not-a-date"}, "products": _rows(3)})})
        _root(monkeypatch, tmp_path)
        n, _ = tasks._real_items_evidence(
            "x-com", _job(started),
            final_state={"tested_draft_sha256": "a"})
        assert n == 3  # fresh NAME carries it when the payload can't
