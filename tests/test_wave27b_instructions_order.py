"""Wave-27b — per-field instructions (W27-4) + field order made real (W27-5).

Team asks #3 + #4 (docs/plans/wave27-extractor-plan.md):

W27-4: fields stop being bare names. The schema validator accepts per-field
``description`` (internal dialect + JSON-Schema properties), capped at 300
chars with a warning; ``extract_field_notes`` is the shared reader; the
intake/partner create flows persist ``ScrapeJob.field_notes``; the
product_analyzer + code_writer prompts render a bounded
``### Field guidance`` section; Site.output_schema persistence carries
descriptions; validate-schema responses surface ``fields``.

W27-5: output record key order follows target_fields order (bookkeeping
appended), enforced at the deterministic prune; intake chips gain reorder
arrows + a note affordance.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave27b_instructions_order.py -q
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import Client, RequestFactory  # noqa: E402
from django.urls import reverse  # noqa: E402

from scraper import models  # noqa: E402
from src.schema_validation import extract_field_notes, validate_user_schema  # noqa: E402

TPL = os.path.join(ROOT, "webapp", "scraper", "templates", "scraper", "intake.html")


def _tpl():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


def _client(who):
    c = Client()
    c.force_login(who)
    return c


@pytest.fixture
def alice(db):
    return User.objects.create_user("w27balice", password="x")


# ───────────────────────── validator: description support ────────────────────


class TestValidatorDescriptions:
    def test_internal_dialect_carries_description(self):
        r = validate_user_schema(
            json.dumps({"fields": [
                {"name": "price", "type": "number", "description": "Current listed price"},
                {"name": "title"},
            ]})
        )
        assert r.valid
        assert r.normalized["fields"] == [
            {"name": "price", "description": "Current listed price"},
            {"name": "title"},
        ]

    def test_standard_properties_description_carried(self):
        r = validate_user_schema(
            json.dumps({
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Product title only"},
                    "price": {"type": "number"},
                },
            })
        )
        assert r.valid
        assert r.normalized["fields"][0] == {
            "name": "title", "description": "Product title only",
        }

    def test_description_capped_with_warning(self):
        r = validate_user_schema(json.dumps({"fields": [
            {"name": "price", "description": "x" * 500},
        ]}))
        assert r.valid, "a too-long description must NOT block the schema"
        codes = [i.code for i in r.issues]
        assert "DESCRIPTION_TOO_LONG" in codes
        assert codes.count("DESCRIPTION_TOO_LONG") == 1
        warn = [i for i in r.issues if i.code == "DESCRIPTION_TOO_LONG"][0]
        assert warn.severity == "warning"
        assert len(r.normalized["fields"][0]["description"]) == 300

    def test_no_descriptions_keeps_exact_legacy_shape(self):
        r = validate_user_schema(json.dumps({"fields": [{"name": "a"}, {"name": "b"}]}))
        assert r.valid
        assert r.normalized == {"content_type": r.normalized["content_type"],
                                "fields": [{"name": "a"}, {"name": "b"}]}, (
            "the no-description normalized shape must be byte-identical to today"
        )

    def test_extract_field_notes_roundtrip(self):
        doc = {"fields": [
            {"name": "price", "description": "Sale price if present"},
            {"name": "title", "type": "text"},
        ]}
        assert extract_field_notes(doc) == {"price": "Sale price if present"}
        assert extract_field_notes(json.dumps(doc)) == {"price": "Sale price if present"}
        assert extract_field_notes({"type": "object", "properties": {
            "sku": {"type": "string", "description": "Manufacturer SKU"}}}) == {
            "sku": "Manufacturer SKU",
        }

    def test_extract_field_notes_never_raises(self):
        assert extract_field_notes("not json {") == {}
        assert extract_field_notes(None) == {}
        assert extract_field_notes({"fields": [{"name": "x", "description": 42}]}) == {}


# ───────────────────────── W27-5: output key order ───────────────────────────


class TestOutputKeyOrder:
    def test_prune_emits_in_target_fields_order(self):
        from src.content_types import prune_record_to_schema

        rec = {"status_code": 200, "title": "t", "url": "u", "price": "$9",
               "scraped_at": "now", "extra": "dropped"}
        allowed = {"title", "price", "url", "src_url", "scraped_at", "status_code"}
        out = prune_record_to_schema(rec, allowed, order=["price", "title"])
        assert list(out.keys()) == ["price", "title", "status_code", "url", "scraped_at"], (
            "target_fields order first, remaining (bookkeeping) in record order"
        )

    def test_prune_without_order_unchanged(self):
        from src.content_types import prune_record_to_schema

        rec = {"b": 1, "a": 2}
        out = prune_record_to_schema(rec, {"a", "b"})
        assert out == {"b": 1, "a": 2}, "order=None keeps today's record order"

    def test_prune_output_helper_threads_order(self, monkeypatch):
        # The helper reads/writes via src.artifacts (File Master) — patch the
        # store instead of fighting it with a raw temp path.
        import src.artifacts as artifacts

        key = "scrapers/w27b/output.json"
        store = {key: {"products": [
            {"status_code": 200, "title": "t", "price": 1},
            {"status_code": 200, "title": "t2", "price": 2},
        ]}}
        monkeypatch.setattr(artifacts, "read_json", lambda k: json.loads(json.dumps(store[k])))
        captured = {}
        monkeypatch.setattr(artifacts, "write_json", lambda k, d: captured.setdefault(k, d))

        from scraper.tasks import _prune_output_to_schema

        allowed = {"title", "price", "url", "src_url", "scraped_at", "status_code"}
        changed = _prune_output_to_schema(key, allowed, None, order=["price", "title"])
        assert changed is True, "a reorder alone is a change worth rewriting"
        assert [list(r.keys()) for r in captured[key]["products"]] == [
            ["price", "title", "status_code"],
            ["price", "title", "status_code"],
        ]


# ─────────────────────── create flows persist field_notes ────────────────────


class TestCreateFlowsPersistNotes:
    def test_intake_create_merges_schema_descriptions_and_form_notes(self, alice):
        schema = json.dumps({"fields": [
            {"name": "price", "description": "Sale price if present"},
            {"name": "title"},
        ]})
        r = _client(alice).post(
            reverse("intake_create_job"),
            {
                "url": "https://w27b.example/p/1",
                "nav_method": "list",
                "list_urls": "https://w27b.example/p/1",
                "target_fields": "price, title",
                "schema_text": schema,
                "field_notes_json": json.dumps({"title": "Exact product title"}),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert r.status_code == 200, r.content
        job = models.ScrapeJob.objects.get(url="https://w27b.example/p/1")
        assert job.field_notes == {
            "price": "Sale price if present",
            "title": "Exact product title",
        }, "form notes merge OVER schema descriptions"

    def test_restart_copies_field_notes(self, alice):
        a = models.ScrapeJob.objects.create(
            url="https://w27b2.example/p/1", user=alice,
            status=models.ScrapeJob.STATUS_COMPLETED,
            field_notes={"price": "in cents"},
            # [wave-40 T5] list_page keeps this test on notes carry-over — the
            # restart gate refuses a url_list job with no URL list anywhere.
            input_mode="list_page",
        )
        with patch("scraper.tasks.dispatch_scrape_job"):
            rr = _client(alice).post(reverse("job_restart", args=[a.id]))
        assert rr.status_code == 302
        b = models.ScrapeJob.objects.exclude(pk=a.pk).order_by("-id").first()
        assert b.field_notes == {"price": "in cents"}

    def test_job_update_edits_field_notes(self, alice):
        job = models.ScrapeJob.objects.create(url="https://w27b3.example/p/1", user=alice)
        r = _client(alice).post(
            reverse("job_update", args=[job.id]),
            {"field_notes_json": json.dumps({"price": "include currency"})},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert r.status_code == 200
        job.refresh_from_db()
        assert job.field_notes == {"price": "include currency"}

    def test_partner_create_accepts_field_instructions(self, db):
        import secrets

        from scraper.api.writers import create_job

        u = User.objects.create_user("_t_w27b_" + secrets.token_hex(3), password="x")
        raw = "pk_" + secrets.token_hex(16)
        models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
        body = {
            "url": "https://api-w27b.example/i",
            "input_mode": "url_list",
            "item_urls": ["https://api-w27b.example/i/1"],
            "target_fields": ["price", "title"],
            "field_instructions": {"price": "numeric only, no currency symbol"},
        }
        rf = RequestFactory()
        with patch("scraper.tasks.dispatch_scrape_job"), patch(
            "scraper.api.writers.transaction"
        ) as _tx:
            _tx.atomic.return_value.__enter__ = lambda *a: None
            _tx.atomic.return_value.__exit__ = lambda *a: False
            resp = create_job(
                rf.post(
                    "/api/v1/jobs",
                    json.dumps(body),
                    content_type="application/json",
                    HTTP_X_API_KEY=raw,
                )
            )
        assert resp.status_code == 202, resp.content
        job = models.ScrapeJob.objects.get(url="https://api-w27b.example/i")
        assert job.field_notes == {"price": "numeric only, no currency symbol"}

    def test_partner_create_rejects_oversized_instruction(self, db):
        import secrets

        from scraper.api.writers import create_job

        u = User.objects.create_user("_t_w27b2_" + secrets.token_hex(3), password="x")
        raw = "pk_" + secrets.token_hex(16)
        models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
        body = {
            "url": "https://api-w27b2.example/i",
            "input_mode": "url_list",
            "item_urls": ["https://api-w27b2.example/i/1"],
            "field_instructions": {"price": "x" * 301},
        }
        rf = RequestFactory()
        # api_view converts ApiError into the envelope response — assert on it.
        resp = create_job(
            rf.post(
                "/api/v1/jobs",
                json.dumps(body),
                content_type="application/json",
                HTTP_X_API_KEY=raw,
            )
        )
        assert resp.status_code == 422, resp.content
        assert b"limit is 300" in resp.content


# ────────────────────────────── prompt surfacing ─────────────────────────────


class TestPromptGuidance:
    def _state(self, notes):
        return {
            "site_slug": "w27b", "url": "https://w27b.example/",
            "product_url": "https://w27b.example/p/1",
            "field_notes": notes,
        }

    def test_product_analyzer_renders_guidance(self):
        from agents.subagents import build_product_analyzer_message

        msgs = build_product_analyzer_message(self._state({"price": "in cents"}))
        assert "Field guidance" in msgs[0].content
        assert "price: in cents" in msgs[0].content

    def test_code_writer_renders_guidance(self):
        from agents.subagents import build_code_writer_message

        msgs = build_code_writer_message(self._state({"price": "in cents"}))
        assert "Field guidance" in msgs[0].content
        assert "price: in cents" in msgs[0].content

    def test_no_notes_no_section(self):
        from agents.subagents import build_product_analyzer_message

        msgs = build_product_analyzer_message(self._state({}))
        assert "Field guidance" not in msgs[0].content


# ───────────────────────────── Site persistence + endpoints ──────────────────


class TestPersistenceAndEndpoints:
    def test_site_schema_merge_helper(self):
        from src.content_types import merge_field_notes

        fields = merge_field_notes(["price", "title"], {"price": "in cents"})
        assert fields == [
            {"name": "price", "description": "in cents"},
            {"name": "title"},
        ]
        assert merge_field_notes(["a"], {}) == [{"name": "a"}]

    def test_tasks_site_write_uses_merge_helper(self):
        with open(os.path.join(ROOT, "webapp", "scraper", "tasks.py"), encoding="utf-8") as fh:
            src = fh.read()
        assert "merge_field_notes" in src, (
            "Site.output_schema persistence must carry descriptions"
        )

    def test_partner_validate_schema_returns_fields(self, db):
        import secrets

        from scraper.api.readers import validate_schema

        u = User.objects.create_user("_t_w27b3_" + secrets.token_hex(3), password="x")
        raw = "pk_" + secrets.token_hex(16)
        models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
        rf = RequestFactory()
        req = rf.post(
            "/api/v1/validate-schema",
            json.dumps({"schema": {"fields": [
                {"name": "price", "description": "in cents"},
            ]}}),
            content_type="application/json",
            HTTP_X_API_KEY=raw,
        )
        body = json.loads(validate_schema(req).content)
        assert body["fields"] == [{"name": "price", "description": "in cents"}]
        assert body["derived_fields"] == ["price"]


# ─────────────────────────────────── UI (intake) ─────────────────────────────


class TestIntakeUi:
    def test_chips_have_reorder_arrows(self):
        src = _tpl()
        assert "data-field-move" in src, "chips need up/down reorder controls"
        assert "splice(" in src, "reorder must splice fieldsArr in place"

    def test_chips_have_note_affordance(self):
        src = _tpl()
        assert "data-field-note" in src, "chips need a per-field note (✎) affordance"
        assert "field_notes_json" in src, "notes must ride the create-job POST"
