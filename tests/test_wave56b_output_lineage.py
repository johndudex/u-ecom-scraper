"""[wave-56b F9] Output-pointer lineage: the artifact the final pointer names
and the published count must be THE SAME SET, and the price disclosure must
count what the floor exempted.

Deep-audit proof (job 589 briscoes, 2026-10-09, verified in the pointer
file): the PRICE-GATE dropped 13 rows from the COUNT only — pc=9845 while
the named artifact held the pre-gate 9858 — and the ``0 < price < 0.5``
floor exempted 92 rows at price 0.00 plus 214 null-price rows, so the notes
on the job row understated retained bad prices ~8x. The dup-title annotation
cited the gated set (9845/9597) while the pointer held 9858/9609.

Zero and null are not prices. The gate does not verdict on them (GAP-7: the
MISSING-CORE stamp and the bar's wall carve-out own that) — but it must SAY
how many unpriced rows it retained, on the surface the operator reads.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


class TestUnpricedPriceDisclosure:
    def test_zero_price_rows_are_disclosed(self):
        from scraper.tasks import _gate_output_rows

        kept, notes, _ = _gate_output_rows([
            {"title": "A", "price": 0.0},
            {"title": "B", "price": 12.99},
        ])
        joined = " | ".join(notes)
        assert "zero or null price" in joined, notes
        assert "1" in joined, notes
        assert len(kept) == 2

    def test_null_price_rows_are_disclosed(self):
        from scraper.tasks import _gate_output_rows

        kept, notes, _ = _gate_output_rows([
            {"title": "A", "price": None},
            {"title": "B", "price": None},
            {"title": "C", "price": "8.50"},
        ])
        joined = " | ".join(notes)
        assert "zero or null price" in joined, notes
        assert "2" in joined, notes
        assert len(kept) == 3

    def test_unpriced_rows_are_never_dropped(self):
        """GAP-7 ownership unchanged: the gate discloses, the MISSING-CORE
        stamp and the bar own the verdict — disclosure must not become a
        silent row edit (that would be a missed pass on a real item)."""
        from scraper.tasks import _gate_output_rows

        rows = [
            {"title": "A", "price": None},
            {"title": "B", "price": 0},
            {"title": "C", "price": "Book an appointment"},
        ]
        kept, notes, _ = _gate_output_rows(rows)
        assert kept == rows
        assert not any("dropped" in n for n in notes), notes


class TestPointerCountAgreement:
    def test_gate_writeback_and_count_agree(self, tmp_path):
        """The same-set invariant: after the finalize gates run on a locator,
        the bytes at that locator hold exactly the gated set and the
        ground-truth counter reads the same rows."""
        from scraper.tasks import (
            _apply_finalize_row_gates,
            _output_file_product_count,
        )

        locator = str(tmp_path / "output_lineage.json")
        with open(locator, "w", encoding="utf-8") as fh:
            json.dump({"products": [
                {"title": "keep-a", "price": "$12.00", "url": "https://x/p/a"},
                {"title": "drop-me", "price": "$0.10", "url": "https://x/p/b"},
                {"title": "keep-b", "price": "$9.99", "url": "https://x/p/c"},
            ]}, fh)
        job = SimpleNamespace(id=1)
        kept, _notes, _pre = _apply_finalize_row_gates(job, locator)
        assert kept is not None and len(kept) == 2
        with open(locator, encoding="utf-8") as fh:
            on_disk = json.load(fh)["products"]
        assert on_disk == kept, "pointer names bytes the gate did not write"
        assert _output_file_product_count(locator) == len(kept)

    def test_collapse_annotation_counts_the_kept_set(self, tmp_path):
        """The dup-title disclosure must cite the artifact the pointer names —
        the 589 shape was a note saying 9845/9597 over a file holding
        9858/9609."""
        from scraper.tasks import _apply_finalize_row_gates

        locator = str(tmp_path / "output_collapse.json")
        with open(locator, "w", encoding="utf-8") as fh:
            json.dump({"products": [
                {"title": "dup", "price": "$1.00", "url": "https://x/p/1"},
                {"title": "dup", "price": "$1.00", "url": "https://x/p/2"},
                {"title": "uniq", "price": "$0.10", "url": "https://x/p/3"},
            ]}, fh)
        job = SimpleNamespace(id=1)
        kept, notes, _pre = _apply_finalize_row_gates(job, locator)
        assert len(kept) == 2
        collapse = [n for n in notes if "rows /" in n or "titles" in n]
        assert collapse, notes
        assert "2 rows" in collapse[0] and "1 distinct" in collapse[0], collapse
