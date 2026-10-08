"""[wave-55 GAP-4] The partner sample gate is the artifact, not the verdict.

``persist_partner_sample`` runs inside ``_invoke_code_tester`` — BEFORE
``route_after_testing`` — and trusted ``overall_assessment``/
``ready_for_execution`` alone. The 1219 shape (10/10 rows with ``price=None``
under a "100% CORRECT" ``field_coverage`` claim) therefore published 5
null-price records to the partner FM and emitted ``job.sample_ready``; the M1
veto that fired moments later could not recall them. Same trust-the-verdict
pattern the round removed from the router, left live on the one consumer that
hands data to a partner.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_partner_sample_null_gate.py -q"
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from scraper import models  # noqa: E402
from scraper.api.sample_persist import persist_partner_sample  # noqa: E402

pytestmark = pytest.mark.django_db


# The 1219 shape: every row real-shaped, every price null. Title is populated
# so the rows clear every "real item" floor — only the core column is dead.
_NULL_ROWS = [
    {
        "title": f"Look {i}",
        "price": None,
        "availability": None,
        "currency": "",
        "url": f"https://aleensabbagh.com/CollectionProductDetails/Look-{i:02d}",
        "src_url": "https://aleensabbagh.com/collections/book-an-appointment",
    }
    for i in range(1, 4)
]

# The tester LLM's story about the same run (job 1219's own field_coverage).
_LLM_ALL_CORRECT = {
    "overall_assessment": "PASS",
    "ready_for_execution": True,
    "results": {
        "successful_extractions": 19,
        "field_coverage": {
            "price": {"count": 19, "coverage": "100%", "status": "CORRECT",
                      "quality": "excellent"},
        },
    },
}


def _job(u, slug_site="sampletest", target_fields=None):
    return models.ScrapeJob.objects.create(
        url="https://www.example.com/i", user=u, created_via="api",
        status="running", input_mode="url_list", page_type="product",
        target_fields=target_fields or [],
        site_folder=f"workspace/{slug_site}",
    )


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_smp_null", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    models.ApiKey.objects.create(user=u, prefix=raw[:8],
                                 key_hash=models.ApiKey.hash_key(raw))
    return u, raw


class TestPartnerSampleNullGate:
    def test_a_null_core_column_is_never_published_to_the_partner(
            self, partner, tmp_path):
        u, _raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output_x.json").write_text(json.dumps({"products": _NULL_ROWS}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            published = persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT)
        assert published is False
        assert fw.call_count == 0

    def test_no_sample_ready_event_rides_a_null_column(self, partner, tmp_path):
        u, _raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output_x.json").write_text(json.dumps({"products": _NULL_ROWS}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write"):
            persist_partner_sample(job, slug="sampletest",
                                   report=_LLM_ALL_CORRECT)
        assert models.EventOutbox.objects.filter(
            job=job, event_type="job.sample_ready").count() == 0
        assert models.EventOutbox.objects.filter(job=job).count() == 0

    def test_a_contract_scoped_sample_publishes_without_the_wholesale_core(
            self, partner, tmp_path):
        """[round-8 GAP-1b] The gate must judge the JOB'S contract, not the
        wholesale registry core. Rows that carry only what the job asked for
        (title+price+url, plus the bookkeeping src_url the drafts emit) — no
        ``availability``/``currency`` key at all, the ssr_div_list shape — are
        an honest sample. Judging the registry wholesale names those two
        unrequested columns dead and suppresses the sample for a run the
        router itself passes."""
        u, _raw = partner
        job = _job(u, target_fields=["title", "price", "url"])
        ws = tmp_path / "ws"
        ws.mkdir()
        rows = [
            {
                "title": f"Look {i}",
                "price": f"{19 + i}.00",
                "url": f"https://aleensabbagh.com/CollectionProductDetails/"
                       f"Look-{i:02d}",
                "src_url": "https://aleensabbagh.com/collections/"
                           "book-an-appointment",
            }
            for i in range(1, 4)
        ]
        (ws / "output_x.json").write_text(json.dumps({"products": rows}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            assert persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT) is True
        assert fw.call_count == 1
        assert models.EventOutbox.objects.filter(
            job=job, event_type="job.sample_ready").count() == 1

    def test_contract_scoped_gate_still_rejects_a_null_requested_column(
            self, partner, tmp_path):
        """Mirror-negative so the contract scope never loosens into a no-op:
        ``price`` is IN the job's contract, so a null price column is still a
        suppressed sample."""
        u, _raw = partner
        job = _job(u, target_fields=["title", "price", "url"])
        ws = tmp_path / "ws"
        ws.mkdir()
        rows = [
            {
                "title": f"Look {i}",
                "price": None,
                "url": f"https://aleensabbagh.com/CollectionProductDetails/"
                       f"Look-{i:02d}",
                "src_url": "https://aleensabbagh.com/collections/"
                           "book-an-appointment",
            }
            for i in range(1, 4)
        ]
        (ws / "output_x.json").write_text(json.dumps({"products": rows}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            published = persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT)
        assert published is False
        assert fw.call_count == 0

    def test_a_populated_sample_still_publishes(self, partner, tmp_path):
        """The gate keys on the null column — a priced sample must keep
        flowing (the audit's complaint is the null column, not the sample)."""
        u, _raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        rows = [dict(r, price=f"{19 + i}.00", availability="in stock",
                     currency="AED")
                for i, r in enumerate(_NULL_ROWS, start=1)]
        (ws / "output_x.json").write_text(json.dumps({"products": rows}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            assert persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT) is True
        assert fw.call_count == 1
        assert models.EventOutbox.objects.filter(
            job=job, event_type="job.sample_ready").count() == 1


# ── [round-10 GAP-6] the sample is a publishing surface: the string price ────


def _priced_rows():
    """The 1208/1215/1176 shape — real rows whose price shipped as a string."""
    return [dict(r, price="$65.00", availability="in stock", currency="USD")
            for i, r in enumerate(_NULL_ROWS, start=1)]


class TestPartnerSamplePriceGate:
    def test_a_string_price_is_repaired_before_the_partner_fm(
            self, partner, tmp_path):
        u, _raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "output_x.json").write_text(
            json.dumps({"products": _priced_rows()}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            assert persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT) is True
        payload = fw.call_args.args[1]
        assert payload["records"], "the sample still publishes"
        assert all(r["price"] == 65.0 for r in payload["records"])
        assert any(n.startswith("[PRICE-GATE]") and "repaired" in n
                   for n in payload["gate_notes"])

    def test_a_sample_whose_rows_all_fail_the_price_gate_is_rejected(
            self, partner, tmp_path):
        u, _raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        rows = [dict(r, price="Save 20%", availability="in stock",
                     currency="USD") for r in _NULL_ROWS]
        (ws / "output_x.json").write_text(json.dumps({"products": rows}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            published = persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT)
        assert published is False
        assert fw.call_count == 0
        assert models.EventOutbox.objects.filter(
            job=job, event_type="job.sample_ready").count() == 0

    def test_a_duplicate_title_sample_is_disclosed_not_suppressed(
            self, partner, tmp_path):
        """M15 rides the same pass: the collapse is disclosed on the published
        payload, the sample still flows — disclosure, not a verdict."""
        u, _raw = partner
        job = _job(u)
        ws = tmp_path / "ws"
        ws.mkdir()
        rows = [dict(_priced_rows()[0], price="19.00",
                     url=f"https://aleensabbagh.com/CollectionProductDetails/"
                         f"Look-{i:02d}")
                for i in range(1, 4)]
        (ws / "output_x.json").write_text(json.dumps({"products": rows}))
        with patch("scraper.api.sample_persist._workspace_output",
                   return_value=ws / "output_x.json"), \
             patch("scraper.api.sample_persist._fm_write") as fw:
            assert persist_partner_sample(
                job, slug="sampletest", report=_LLM_ALL_CORRECT) is True
        payload = fw.call_args.args[1]
        assert payload["records"]
        assert any(n.startswith("[VARIANT-COLLAPSE]")
                   for n in payload.get("gate_notes", []))
