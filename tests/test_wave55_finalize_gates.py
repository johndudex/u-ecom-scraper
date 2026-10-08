"""[wave-55 R6b] Finalize data gates — audit M3 / M13 / M15 + the COMPLETED bar.

Tier-2 item 14. The audit's inventory is the evidence: 1208 schylling shipped
``"price": "$3.99"`` (string), 1176 clothbase ``'$200.00'`` with currency
None, 1196 senser a Max Mara coat at ``price 1.0`` COMPLETED, 1212 chacos 10
rows = 3 products, 1191 houseoffraser 10 rows / 7 titles across ``#colcode=``
variants. None of it is judged anywhere at the stamp — `_finalize_job` reads
the artifact only to count it.

Four gates, one seam (the COMPLETED stamp, post-prune so the gates describe
the shipped bytes):

1. M3 — a string price is repaired when the digits + separators are
   unambiguous, else the row is dropped and the reason goes on the job row;
   a price with no currency is flagged, never silently passed;
2. M13 — a positive price below ``MIN_PLAUSIBLE_PRICE`` is dropped (no FX);
3. M15 — a duplicate-title share over the threshold is disclosed as variant
   collapse, with the distinct-title count;
4. the bar — COMPLETED requires >=1 row with a real title + numeric price;
   zero good rows is an honest FAIL (wave-18: never credit pc 0).

The one carve-out is deliberate and documented on the code: a uniform price
wall (every row's price null) is the honest-site-wall verdict the wave-55 M1
stamps already record and census OQ13(b) leaves to the owner — the bar does
not re-judge it.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from scraper import tasks  # noqa: E402
from scraper.models import ScrapeJob, Step  # noqa: E402

pytestmark = pytest.mark.django_db


# ── fixtures ────────────────────────────────────────────────────────────────


def _fm(monkeypatch, keys, payloads):
    """Stub the File Master client to `(keys, payloads)`; return the write log.

    Same reload-and-pin dance test_wave55_rescue_provenance documents (the
    full-suite collection stub shadows the real module). ``write`` records
    into the returned dict so a test can assert the gated bytes landed.
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    writes: dict = {}
    monkeypatch.setattr(real, "list_keys", lambda prefix="": keys)
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))
    monkeypatch.setattr(real, "exists", lambda key: key in payloads)
    monkeypatch.setattr(real, "read_json",
                        lambda key: json.loads(payloads[key]) if key in
                        payloads else (_ for _ in ()).throw(ValueError(key)))

    def _write(key, data):
        # A write lands: the next read sees the gated bytes, the way the real
        # File Master behaves (the C4 count re-reads after the gates).
        writes[key] = bytes(data)
        payloads[key] = bytes(data).decode("utf-8")

    monkeypatch.setattr(real, "write", _write)
    return writes


def _freeze_state(monkeypatch, state):
    """Freeze `_finalize_job`'s checkpoint read to `state` (wave-40 chain)."""
    from types import SimpleNamespace

    class _FakeGraph:
        def get_state(self, config):
            return SimpleNamespace(values=state)

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def build_graph(self):
            return _FakeGraph()

        @staticmethod
        def get_config(thread_id):
            return {"configurable": {"thread_id": thread_id}}

    monkeypatch.setattr(tasks, "LangGraphService", _FakeService)


def _job(**kw):
    kw.setdefault("url", "https://x.example/")
    kw.setdefault("status", ScrapeJob.STATUS_RUNNING)
    kw.setdefault("started_at", timezone.now() - timezone.timedelta(hours=1))
    return ScrapeJob.objects.create(**kw)


def _finalize(monkeypatch, tmp_path, rows, key_suffix="3", **job_kw):
    """Drive `_finalize_job` over one artifact; return the refreshed job."""
    key = f"scrapers/x-example/output_2026-09-20_050000_000001_{key_suffix}.json"
    writes = _fm(monkeypatch, [key], {key: json.dumps({"products": rows})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    job = _job(**job_kw)
    _freeze_state(monkeypatch, {
        "site_slug": "x-example", "input_mode": "url_list",
        "execution_status": "SUCCESS", "product_count": len(rows),
        "output_file": key, "test_report": {}})
    tasks._finalize_job(job)
    job.refresh_from_db()
    return job, key, writes


# ── M3: the string-price repair rules ───────────────────────────────────────


class TestParsePriceString:
    def test_the_audit_shapes_repair(self):
        assert tasks._parse_price_string("$3.99") == 3.99
        assert tasks._parse_price_string("$200.00") == 200.0  # 1176 clothbase
        assert tasks._parse_price_string("1,299.00") == 1299.0
        assert tasks._parse_price_string("1.299,00") == 1299.0
        assert tasks._parse_price_string("1,299") == 1299.0
        assert tasks._parse_price_string("3.99 USD") == 3.99
        assert tasks._parse_price_string("USD 3.99") == 3.99
        assert tasks._parse_price_string("£1,299.00") == 1299.0
        assert tasks._parse_price_string("29.99") == 29.99
        assert tasks._parse_price_string("9") == 9.0

    def test_the_retail_currency_suffixes_repair(self):
        """[round-10 GAP-1] The audit's own fleet ships SEK/PLN/HUF — a 2-letter
        or prose currency suffix is a real retail shape, not prose. Under the
        3-letter-code-only residue rule every one of these dropped every row
        and the job FAILED on '[PRICE-GATE] N row(s) dropped'."""
        assert tasks._parse_price_string("1 299 kr") == 1299.0   # SEK/NOK/DKK/ISK
        assert tasks._parse_price_string("1.295 kr") == 1295.0
        assert tasks._parse_price_string("1299 kr") == 1299.0
        assert tasks._parse_price_string("49,99 zł") == 49.99    # PLN
        assert tasks._parse_price_string("3 490 Ft") == 3490.0   # HUF
        assert tasks._parse_price_string("Ex VAT £12.99") == 12.99
        assert tasks._parse_price_string("£12.99 ex VAT") == 12.99

    def test_the_unresolvable_shapes_do_not_repair(self):
        # a lone separator that is neither a 2-digit decimal nor a 3-digit
        # group cannot be read without guessing at the locale.
        assert tasks._parse_price_string("1.2") is None
        assert tasks._parse_price_string("1.2345") is None
        assert tasks._parse_price_string("Save 20%") is None
        assert tasks._parse_price_string("") is None
        assert tasks._parse_price_string("Free") is None

    def test_a_lone_three_digit_dot_tail_never_mints_a_thousands_group(self):
        """[round-10 GAP-2] ``'19.990'`` is a legitimate 3-decimal price in the
        locale that writes decimals with a dot AND a German-style group
        separator one thousand times larger. The M13 evidence is exactly a
        plausible price ~3 orders off, so this arm may not guess: without an
        explicit currency token the string is unparseable and the row drops."""
        assert tasks._parse_price_string("19.990") is None
        assert tasks._parse_price_string("1.999") is None

    def test_the_dot_group_reading_needs_a_token_after_the_digits(self):
        """[wave-55 GAP-D] ``'$19.990'`` → 19990.0 and ``'£1.999'`` → 1999.0
        were the audit's M13 shape minted by the gate itself: a currency token
        ANYWHERE in the string is not locale evidence for a lone 3-digit dot
        tail — de-DE writes ``19.990 €`` for 19990, a US site writes ``$19.990``
        for 19.99 with a stray digit, so the leading symbol proves nothing.
        Only a token that FOLLOWS the digits (``1.295 kr`` / ``1.299 €``, the
        postfixed shapes the SEK/PLN/HUF fleet ships) corroborates the group
        reading; anything else drops rather than guess."""
        assert tasks._parse_price_string("1.295 kr") == 1295.0
        assert tasks._parse_price_string("1.299 €") == 1299.0
        assert tasks._parse_price_string("$19.990") is None
        assert tasks._parse_price_string("£1.999") is None
        # and a leading word is still a leading word: 'kr 1.299' says SEK and
        # leaves the dot tail exactly as unreadable as the bare string.
        assert tasks._parse_price_string("kr 1.299") is None

    def test_a_comma_group_still_reads_as_thousands(self):
        # the comma form is the shape the audit's fleet ships and the round-9
        # pin chose; the dot form is the one GAP-2 proved corrupts.
        assert tasks._parse_price_string("1,299") == 1299.0
        assert tasks._parse_price_string("19,990") == 19990.0

    def test_a_leading_zero_head_is_not_a_group(self):
        # '0,999' is a fraction spelled with a group shape — not 0999.
        assert tasks._parse_price_string("0,999") is None
        assert tasks._parse_price_string("0.999") is None

    def test_a_unit_annotation_keeps_the_number(self):
        # deliberate call: a digit-free short label after the price is a unit,
        # not prose — the price is still the price. Recorded here so the rule
        # cannot silently widen further.
        assert tasks._parse_price_string("12.99 per metre") == 12.99

    def test_typed_and_absent_prices_are_not_strings(self):
        assert tasks._normalize_price(3.99) == 3.99
        assert tasks._normalize_price(0) == 0.0     # job 468: typed zero is data
        assert tasks._normalize_price(None) is None
        assert tasks._normalize_price("") is None
        assert tasks._normalize_price(True) is None


# ── M3 + M13: the row gate ──────────────────────────────────────────────────


class TestGateOutputRows:
    def test_a_string_price_is_repaired_in_the_row(self):
        rows = [{"title": "p", "price": "$3.99", "currency": "USD"}]
        kept, notes, _chg = tasks._gate_output_rows(rows)
        assert kept == [{"title": "p", "price": 3.99, "currency": "USD"}]
        assert any("repaired" in n for n in notes)

    def test_an_unparseable_string_price_drops_the_row(self):
        kept, notes, _chg = tasks._gate_output_rows([
            {"title": "p", "price": "Save 20%", "currency": "USD"}])
        assert kept == []
        assert any("dropped" in n and "not parseable" in n for n in notes)

    def test_a_price_below_the_floor_is_dropped(self):
        kept, notes, _chg = tasks._gate_output_rows([
            {"title": "coat", "price": 0.25, "currency": "USD"}])
        assert kept == []
        assert any("below the 0.5 floor" in n for n in notes)

    def test_typed_zero_survives_the_floor(self):
        # job 468: five real $0.00 sample PDPs — a typed numeric zero is data.
        kept, _notes, _chg = tasks._gate_output_rows([
            {"title": "sample", "price": 0.0, "currency": "USD"}])
        assert len(kept) == 1

    def test_a_negative_price_is_dropped(self):
        kept, notes, _chg = tasks._gate_output_rows([
            {"title": "p", "price": -12.5, "currency": "USD"}])
        assert kept == []
        assert any("floor" in n for n in notes)

    def test_a_price_with_no_currency_is_flagged_not_dropped(self):
        rows = [{"title": "p", "price": 19.0}]
        kept, notes, _chg = tasks._gate_output_rows(rows)
        assert len(kept) == 1
        assert any(n.startswith("[CURRENCY-MISSING] 1/1") for n in notes)

    def test_an_absent_price_is_not_this_gates_verdict(self):
        # the M1 wall: a null price column is the coverage stamp's business.
        rows = [{"title": "Look 1", "price": None, "currency": ""},
                {"title": "Look 2", "currency": "USD"}]
        kept, notes, _chg = tasks._gate_output_rows(rows)
        assert len(kept) == 2
        assert not any("PRICE-GATE" in n for n in notes)

    def test_non_dict_rows_pass_through(self):
        kept, _notes, _chg = tasks._gate_output_rows(["not-a-dict", 7])
        assert kept == ["not-a-dict", 7]

    def test_a_digit_free_price_string_is_absence_not_a_drop(self):
        """[round-10 GAP-7] 'Book an appointment' is the producer's spelling of
        'no price', not a price that failed to parse. Dropping it failed an
        honest no-price site that the same site with ``price: null`` completed
        — the verdict decided by spelling."""
        rows = [{"title": "Look 1", "price": "Book an appointment"},
                {"title": "Look 2", "price": "See price in cart"}]
        kept, notes, changed = tasks._gate_output_rows(rows)
        assert kept == rows
        assert changed is False
        assert not any("PRICE-GATE" in n for n in notes)

    def test_a_digit_bearing_unparseable_price_still_drops(self):
        # the other half of GAP-7: digits mean a price was claimed, and a
        # claim the parser cannot honour is the gate's to drop.
        kept, notes, _chg = tasks._gate_output_rows(
            [{"title": "p", "price": "Save 20%"}])
        assert kept == []
        assert any("not parseable" in n for n in notes)


# ── [wave-55 GAP-D] one parser across every publisher ───────────────────────


class TestOneParserAcrossPublishers:
    """The gate may only own the verdict if it sees the bytes.

    ``run_execution.normalize_output_prices`` (the A3 persist-time normalizer,
    browser lane + the tester lane in ``shell_tools``) rewrites price strings
    BEFORE the finalize gate, under ``run_execution._parse_price_string``'s own
    rule — so on the main lanes the row arrives as a float and the GAP-2 drop
    pin is inert, while the bypass publishers this wave added (partner sample,
    site_rerun) hand the gate the raw string. Same bytes, publisher-dependent
    verdict.
    """

    @staticmethod
    def _gate_verdict(raw):
        kept, _notes, _chg = tasks._gate_output_rows(
            [{"title": "p", "price": raw, "currency": "USD"}])
        return kept[0]["price"] if kept else None

    @pytest.mark.xfail(
        strict=True,
        reason="[GAP-D] two parsers, one path: run_execution's A3 normalizer "
               "rewrites price strings before the finalize gate under a "
               "different rule, so these shapes read differently per "
               "publisher. Unifying the parser is census OQ13(b) — drop this "
               "marker the moment run_execution delegates.")
    def test_the_same_bytes_read_the_same_on_every_publisher(self, tmp_path):
        """The one-parser contract, carried as a strict xfail while the two
        parsers stand: whatever the persist-time normalizer leaves on disk,
        the finalize gate must judge the same way for the same bytes. Strict,
        so unifying the parser turns this into an ERROR and forces the marker
        off rather than letting the pin rot."""
        import importlib

        rexec = importlib.import_module("agents.nodes.run_execution")
        for raw in ("19.990", "$19.990", "1 299 kr"):
            out = tmp_path / f"output_{abs(hash(raw))}.json"
            out.write_text(json.dumps(
                {"products": [{"title": "p", "price": raw}]}), encoding="utf-8")
            rexec.normalize_output_prices(str(out))
            shipped = json.loads(
                out.read_text(encoding="utf-8"))["products"][0]["price"]
            persisted = shipped if isinstance(shipped, (int, float)) else None
            assert persisted == self._gate_verdict(raw), raw


# ── M15: the duplicate-title disclosure ─────────────────────────────────────


class TestVariantCollapse:
    def test_the_1212_shape_warns_variant_collapse(self):
        rows = ([{"title": "Z x2 Cloud", "url": f"https://x/p/{i}"} for i in range(4)]
                + [{"title": "Z x1 Low", "url": f"https://x/p/{i}"} for i in range(4, 7)]
                + [{"title": "Z x1 Mid", "url": f"https://x/p/{i}"} for i in range(7, 10)])
        note = tasks._variant_collapse_note(rows)
        assert note.startswith("[VARIANT-COLLAPSE]")
        assert "10 rows / 3 distinct titles" in note

    def test_the_1191_shape_is_disclosed_below_the_threshold(self):
        rows = [{"title": f"Shirt {i}", "url": f"https://x/p/{i}?c={i}"}
                for i in range(7)]
        rows += [{"title": "Shirt 0", "url": "https://x/p/0#colcode=10501305"},
                 {"title": "Shirt 1", "url": "https://x/p/1#colcode=10501303"},
                 {"title": "Shirt 2", "url": "https://x/p/2#colcode=10501301"}]
        note = tasks._variant_collapse_note(rows)
        assert note.startswith("[DUPLICATE-TITLES]")
        assert "10 rows / 7 distinct titles" in note

    def test_one_dominant_title_over_the_share_warns(self):
        rows = [{"title": "same", "url": f"https://x/p/{i}"} for i in range(7)]
        rows += [{"title": f"other {i}", "url": f"https://x/p/{i + 7}"}
                 for i in range(3)]
        assert tasks._variant_collapse_note(rows).startswith("[VARIANT-COLLAPSE]")

    def test_all_distinct_titles_are_silent(self):
        rows = [{"title": f"p{i}", "url": f"https://x/p/{i}"} for i in range(5)]
        assert tasks._variant_collapse_note(rows) == ""
        assert tasks._variant_collapse_note([]) == ""

    def test_title_match_ignores_case_and_padding(self):
        # 2/4 duplicated: no title group over the 60% share, aggregate 25%.
        rows = [{"title": "  Widget "}, {"title": "widget"},
                {"title": "other"}, {"title": "third"}]
        assert tasks._variant_collapse_note(rows).startswith("[DUPLICATE-TITLES]")


# ── the bar ─────────────────────────────────────────────────────────────────


class TestCompletedBar:
    def test_a_row_with_title_and_price_meets_the_bar(self):
        rows = [{"title": "p", "price": 19.0, "currency": "USD"}]
        assert tasks._real_item_bar_rows(rows) == rows
        assert tasks._completed_bar_reason(rows) == ""

    def test_no_numeric_price_fails_the_bar(self):
        """A priced row with no title and a titled row with no price: there is
        data on disk and not one real item in it. Not a wall — a wall needs
        every row to be an otherwise-real, uniformly unpriced item."""
        rows = [{"title": "", "price": 19.0},
                {"title": "q", "price": None}]
        reason = tasks._completed_bar_reason(rows)
        assert reason.startswith("[COMPLETED-BAR]")
        assert "0 of 2" in reason

    def test_no_rows_at_all_is_not_the_bars_verdict(self):
        # an empty/unreadable artifact is the zero-item ladder arm's business.
        assert tasks._completed_bar_reason([]) == ""
        assert tasks._completed_bar_reason(None) == ""

    def test_a_uniform_price_wall_is_carved_out(self):
        """OQ13(b): the 1219 shape is the M1 caveat's verdict, not the bar's.
        Flipping it here would re-open an owner call the census explicitly
        reserves — the bar kills every OTHER zero-good-row shape."""
        rows = [{"title": f"Look {i}", "price": None, "currency": "",
                 "url": f"https://x.example/Look-{i:02d}"}
                for i in range(4)]
        assert tasks._completed_bar_reason(rows) == ""

    def test_a_priced_row_with_no_title_is_not_a_wall(self):
        rows = [{"title": "", "price": 19.0, "url": "https://x/p/1"}]
        assert not tasks._price_wall(rows)
        assert tasks._completed_bar_reason(rows).startswith("[COMPLETED-BAR]")

    def test_rows_the_gate_dropped_fail_the_bar(self):
        reason = tasks._completed_bar_reason([], pre_count=5)
        assert reason.startswith("[COMPLETED-BAR]")
        assert "all 5" in reason


# ── the stamp, end to end ───────────────────────────────────────────────────


class TestFinalizeAppliesTheGates:
    def test_string_prices_are_repaired_in_the_shipped_bytes(
            self, monkeypatch, tmp_path):
        rows = [{"title": f"p{i}", "price": "$3.99", "url": f"https://x/p/{i}"}
                for i in range(3)]
        job, key, writes = _finalize(monkeypatch, tmp_path, rows)
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 3
        assert "[PRICE-GATE]" in (job.notes or "")
        assert "repaired" in job.notes
        shipped = json.loads(writes[key])
        assert all(r["price"] == 3.99 for r in shipped["products"])

    def test_a_currency_less_price_is_flagged_on_the_row(
            self, monkeypatch, tmp_path):
        rows = [{"title": f"p{i}", "price": 19.0, "url": f"https://x/p/{i}"}
                for i in range(3)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="4")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "[CURRENCY-MISSING] 3/3" in (job.notes or "")

    def test_the_gate_notes_reach_the_rendered_surface_too(
            self, monkeypatch, tmp_path):
        """[round-10 GAP-4] ``notes`` is a column nothing renders — job_detail
        prints error_message (the Data caveat panel on COMPLETED, the red Error
        panel on FAILED). A disclosure that never reaches that surface is not
        shipped disclosure."""
        rows = [{"title": f"p{i}", "price": 19.0, "url": f"https://x/p/{i}"}
                for i in range(3)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="40")
        assert "[CURRENCY-MISSING] 3/3" in (job.error_message or "")

    def test_a_price_gate_repair_reaches_the_rendered_surface_too(
            self, monkeypatch, tmp_path):
        rows = [{"title": f"p{i}", "price": "$3.99", "url": f"https://x/p/{i}"}
                for i in range(3)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="41")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "[PRICE-GATE]" in (job.error_message or "")
        assert "repaired" in (job.error_message or "")

    def test_sub_floor_rows_are_dropped_and_the_count_describes_the_bytes(
            self, monkeypatch, tmp_path):
        """M13 + the C4 invariant: the credited count is the SHIPPED row
        count, so a dropped row must leave the artifact."""
        rows = [{"title": f"p{i}", "price": 19.0, "url": f"https://x/p/{i}"}
                for i in range(3)]
        rows.append({"title": "junk", "price": 0.1, "url": "https://x/p/9"})
        job, key, writes = _finalize(monkeypatch, tmp_path, rows, key_suffix="5")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert job.product_count == 3
        assert "below the 0.5 floor" in (job.notes or "")
        shipped = json.loads(writes[key])
        assert len(shipped["products"]) == 3

    def test_variant_collapse_reaches_the_notes(self, monkeypatch, tmp_path):
        rows = [{"title": "Z x2 Cloud", "price": 149.0, "currency": "USD",
                 "url": f"https://x/p/{i}"} for i in range(4)]
        rows += [{"title": "Z x1 Low", "price": 129.0, "currency": "USD",
                  "url": f"https://x/p/{i}"} for i in range(4, 7)]
        rows += [{"title": "Z x1 Mid", "price": 139.0, "currency": "USD",
                  "url": f"https://x/p/{i}"} for i in range(7, 10)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="6")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "[VARIANT-COLLAPSE]" in (job.notes or "")
        assert "10 rows / 3 distinct titles" in job.notes
        # and on the rendered surface (round-10 GAP-4)
        assert "[VARIANT-COLLAPSE]" in (job.error_message or "")

    def test_a_clean_run_stays_silent(self, monkeypatch, tmp_path):
        rows = [{"title": f"p{i}", "price": 19.0 + i, "currency": "USD",
                 "url": f"https://x/p/{i}"} for i in range(3)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="7")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert (job.notes or "") == ""

    def test_a_bare_top_level_array_is_gated_and_written_back(
            self, monkeypatch, tmp_path):
        """The shape `_output_file_has_zero_items` also admits: no ``products``
        key, the records ARE the payload. The gate must repair it in place —
        a splice that missed would re-ship the original strings."""
        key = "scrapers/x-example/output_2026-09-20_050000_000001_16.json"
        rows = [{"title": f"p{i}", "price": "$9.99", "url": f"https://x/p/{i}"}
                for i in range(2)]
        writes = _fm(monkeypatch, [key], {key: json.dumps(rows)})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job()
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "url_list",
            "execution_status": "SUCCESS", "product_count": 2,
            "output_file": key, "test_report": {}})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "[PRICE-GATE] 2 row(s) repaired" in (job.notes or "")
        assert json.loads(writes[key]) == [
            {"title": "p0", "price": 9.99, "url": "https://x/p/0"},
            {"title": "p1", "price": 9.99, "url": "https://x/p/1"}]

    def test_a_digit_free_no_price_verdict_matches_a_null_price(
            self, monkeypatch, tmp_path):
        """[round-10 GAP-7] 1219 with the no-price verdict spelled as a string
        instead of null: same site, same verdict. Under the string-drop rule
        the spelling alone decided COMPLETED vs FAILED — and [GAP-C] the
        MISSING-CORE stamp must read both spellings as the same empty column,
        which is what this name has promised since it was written."""
        for suffix, price in (("18", "Book an appointment"), ("19", None)):
            rows = [{"title": f"Look {i}", "price": price, "currency": "",
                     "url": f"https://x.example/Look-{i:02d}",
                     "src_url": "https://x.example/collections/book"}
                    for i in range(1, 5)]
            job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix=suffix)
            assert job.status == ScrapeJob.STATUS_COMPLETED, price
            assert "[COMPLETED-BAR]" not in (job.error_message or "")
            assert (job.error_message or "").count(
                "[MISSING-CORE price: 4/4 rows empty]") == 1, price

    def test_a_digit_free_price_string_is_missing_core_for_the_stamp_too(
            self, monkeypatch, tmp_path):
        """[GAP-C] The audit's 1219 shape with the producer's prose spelling of
        'no price'. `_field_has_value('Book an appointment')` is True, so
        `null_core_columns` never named price — the run completed with no
        disclosure at all, while the same rows with ``price=None`` were
        stamped. One absence predicate, every consumer."""
        rows = [{"title": f"Look {i}", "price": "Book an appointment",
                 "currency": "",
                 "url": f"https://x.example/Look-{i:02d}",
                 "src_url": "https://x.example/collections/book"}
                for i in range(1, 5)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="21")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "[MISSING-CORE price: 4/4 rows empty]" in (job.error_message or "")


class TestFinalizeBar:
    def test_zero_good_rows_is_an_honest_fail(self, monkeypatch, tmp_path):
        """The audit's pc=0 class: rows on disk, none of them a real item."""
        rows = [{"title": "", "price": 19.0, "url": f"https://x/p/{i}"}
                for i in range(4)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="8")
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0
        assert "[COMPLETED-BAR]" in (job.error_message or "")

    def test_all_rows_dropped_by_the_price_gate_fails_too(
            self, monkeypatch, tmp_path):
        rows = [{"title": f"p{i}", "price": "Save 20%", "url": f"https://x/p/{i}"}
                for i in range(4)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="9")
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0
        assert "all 4" in (job.error_message or "")

    def test_the_price_wall_is_not_the_bars_verdict(self, monkeypatch, tmp_path):
        """1219: an honest no-price site stays COMPLETED with its caveat —
        the M1 stamp owns that verdict, the bar owns every other zero."""
        rows = [{"title": f"Look {i}", "price": None, "currency": "",
                 "url": f"https://x.example/Look-{i:02d}",
                 "src_url": "https://x.example/collections/book"}
                for i in range(1, 5)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="10")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "MISSING-CORE price" in (job.error_message or "")
        assert "[COMPLETED-BAR]" not in (job.error_message or "")

    def test_one_good_row_among_junk_still_completes(
            self, monkeypatch, tmp_path):
        rows = [{"title": f"nav {i}", "price": "", "currency": "USD",
                 "url": f"https://x/c/{i}"} for i in range(3)]
        rows.append({"title": "real", "price": 49.0, "currency": "USD",
                     "url": "https://x/p/1"})
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="11")
        assert job.status == ScrapeJob.STATUS_COMPLETED


# ── [round-10 GAP-3] the bar is content-type aware ──────────────────────────

# The registry core of each non-product domain, populated. A serp result, an
# article and a forum thread never carry a price, so a product's price floor
# flipped every honest non-product run to FAILED with a "0 priced row(s) were
# dropped" line that was false on its face.
_NON_PRODUCT_GOOD_ROW = {
    "article": {"title": "A Title {}", "author": "An Author",
                "publish_date": "2026-01-0{}", "content": "Some body copy",
                "url": "https://x.example/a/{}"},
    "job_posting": {"title": "A Job {}", "company": "A Co", "location": "Remote",
                    "description": "Duties", "url": "https://x.example/j/{}"},
    "forum_thread": {"title": "A Thread {}", "author": "An Author", "posts": 4,
                     "url": "https://x.example/t/{}"},
    "serp": {"rank": 1, "title": "A Result {}", "snippet": "A snippet",
             "url": "https://x.example/s/{}"},
    "page_content": {"title": "A Page {}", "content": "Some copy",
                     "url": "https://x.example/p/{}"},
}


def _rows_from(shape, count=3, **overrides):
    out = []
    for i in range(1, count + 1):
        row = {k: (v.format(i) if isinstance(v, str) else v)
               for k, v in shape.items()}
        row.update(overrides)
        out.append(row)
    return out


class TestBarKnowsTheContentType:
    @pytest.mark.parametrize("page_type", list(_NON_PRODUCT_GOOD_ROW))
    def test_a_good_non_product_extraction_completes(
            self, monkeypatch, tmp_path, page_type):
        rows = _rows_from(_NON_PRODUCT_GOOD_ROW[page_type])
        job, _, _ = _finalize(monkeypatch, tmp_path, rows,
                              key_suffix=f"2{page_type[:2]}",
                              page_type=page_type)
        assert job.status == ScrapeJob.STATUS_COMPLETED, page_type
        assert "price gate" not in (job.error_message or "")

    def test_serp_rows_carrying_search_result_urls_complete(
            self, monkeypatch, tmp_path):
        """The shape the critic drove: result URLs that are search links, which
        the product PDP-shape floor reads as a nav directory. Not a product
        haul, so the product bar is not the judge."""
        rows = [{"rank": i, "title": f"Result {i}", "snippet": f"snippet {i}",
                 "url": f"https://www.google.com/search?q=shoes&start={i}"}
                for i in range(1, 4)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="30",
                              page_type="serp")
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "dropped by the price gate" not in (job.error_message or "")

    def test_an_article_with_one_blank_title_still_completes(
            self, monkeypatch, tmp_path):
        rows = _rows_from(_NON_PRODUCT_GOOD_ROW["article"])
        rows.append(dict(rows[0], title=""))
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="31",
                              page_type="article")
        assert job.status == ScrapeJob.STATUS_COMPLETED

    def test_a_non_product_bar_failure_never_blames_the_price_gate(
            self, monkeypatch, tmp_path):
        """Rows with nothing but URLs are still an honest FAIL — but the reason
        names the domain's own core, never a price gate that never ran."""
        rows = [{"url": f"https://x.example/{i}"} for i in range(3)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="32",
                              page_type="serp")
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "[COMPLETED-BAR]" in (job.error_message or "")
        assert "price" not in (job.error_message or "")

    def test_a_product_list_page_type_is_still_price_bearing(
            self, monkeypatch, tmp_path):
        """`product_list`/`product_navigation` are page types of the PRODUCT
        content type — the bar's discriminator is the content type, not the
        page-type string, or a product job would escape the bar by its scope."""
        rows = [{"title": "", "price": 19.0, "url": f"https://x/p/{i}"}
                for i in range(4)]
        job, _, _ = _finalize(monkeypatch, tmp_path, rows, key_suffix="33",
                              page_type="product_list")
        assert job.status == ScrapeJob.STATUS_FAILED
        assert "[COMPLETED-BAR]" in (job.error_message or "")

    def test_the_second_stamp_passes_the_page_type_too(
            self, monkeypatch, tmp_path):
        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_34.json"
        _fm(monkeypatch, [key], {key: json.dumps({"results": [
            {"rank": i, "title": f"Result {i}", "snippet": f"snippet {i}",
             "url": f"https://www.google.com/search?q=shoes&start={i}"}
            for i in range(1, 4)]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(status=ScrapeJob.STATUS_RUNNING, output_file=key,
                   page_type="serp")
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert "[COMPLETED-BAR]" not in (job.error_message or "")


class TestTheSecondStampAppliesThemToo:
    """`finalize_from_artifacts` is the SECOND COMPLETED stamp (soft-time-limit
    kills) — a gate that only ran on `_finalize_job` would let the kill path
    ship exactly the rows the main path just cleaned."""

    def test_repairs_and_flags_reach_the_row(self, monkeypatch, tmp_path):
        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_12.json"
        writes = _fm(monkeypatch, [key], {key: json.dumps({"products": [
            {"title": f"p{i}", "price": "$65.00", "url": f"https://x/p/{i}"}
            for i in range(3)]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(status=ScrapeJob.STATUS_RUNNING, output_file=key)
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert job.product_count == 3
        assert "[PRICE-GATE]" in (job.notes or "")
        assert "[CURRENCY-MISSING] 3/3" in (job.notes or "")
        # the rendered surface too (round-10 GAP-4)
        assert "[PRICE-GATE]" in (job.error_message or "")
        assert "[CURRENCY-MISSING] 3/3" in (job.error_message or "")
        shipped = json.loads(writes[key])
        assert all(r["price"] == 65.0 for r in shipped["products"])

    def test_zero_good_rows_fail_the_soft_limit_stamp_too(
            self, monkeypatch, tmp_path):
        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_13.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": [
            {"title": f"p{i}", "price": "Save 20%", "url": f"https://x/p/{i}"}
            for i in range(4)]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr("django.db.close_old_connections", lambda: None)
        job = _job(status=ScrapeJob.STATUS_RUNNING, output_file=key)
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_FAILED
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.product_count == 0
        assert "[COMPLETED-BAR]" in (job.error_message or "")

    def test_a_price_wall_still_completes_with_its_caveat(
            self, monkeypatch, tmp_path):
        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_14.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": [
            {"title": f"Look {i}", "price": None, "currency": "",
             "url": f"https://x.example/Look-{i:02d}"}
            for i in range(1, 5)]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(status=ScrapeJob.STATUS_RUNNING, output_file=key)
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert "MISSING-CORE price" in (job.error_message or "")
        assert "[COMPLETED-BAR]" not in (job.error_message or "")

    def test_an_unreadable_artifact_stays_a_noop(self, monkeypatch, tmp_path):
        """A read failure must never zero a productive job (the C4 rule)."""
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        _fm(monkeypatch, [], {})
        job = _job(status=ScrapeJob.STATUS_RUNNING,
                   output_file="scrapers/x-example/gone.json")
        assert tasks._apply_finalize_row_gates(job, job.output_file) == (
            None, [], 0)

    def test_a_discovery_payload_is_not_gated_either(self, monkeypatch, tmp_path):
        """`_output_file_rows` refuses a --discover-only artifact and so must
        the gate: URL stubs have no title, so judging them would fail the bar
        on a payload the rest of the read chain refuses to call extraction."""
        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_15.json"
        _fm(monkeypatch, [key], {key: json.dumps({
            "metadata": {"phase": "discovery"},
            "products": [{"url": f"https://x.example/p/{i}"} for i in range(5)],
        })})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = _job(status=ScrapeJob.STATUS_RUNNING, output_file=key)
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks._read_output_payload(key) is None
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert "[COMPLETED-BAR]" not in (job.error_message or "")
