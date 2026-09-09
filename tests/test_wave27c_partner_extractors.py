"""Wave-27c — partner-API extractor resource (W27-6) + job.created rerun_of.

W27-6: Sites become first-class partner resources, scoped to the API key's
OWN jobs (distinct Site rows whose url matches the partner's ScrapeJobs —
the same tenancy rule as list_jobs, without a Site.owner migration).
PATCH is a strict allowlist; archive/unarchive is the removal path; there is
NO partner DELETE (documented as deliberate — archive is removal in v1).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave27c_partner_extractors.py -q
"""
from __future__ import annotations

import json
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from scraper import models  # noqa: E402


def _partner(name):
    u = User.objects.create_user(name, password="x")
    raw = "pk_" + secrets.token_hex(16)
    models.ApiKey.objects.create(
        user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    )
    return u, raw


def _site(url, slug, **kw):
    return models.Site.objects.create(url=url, slug=slug, **kw)


def _job(user, url):
    return models.ScrapeJob.objects.create(url=url, user=user)


@pytest.fixture
def tenant(db):
    """alice + her sites (own + archived), bob + his site, one orphan site."""
    alice, raw_a = _partner("_t27c_alice")
    bob, raw_b = _partner("_t27c_bob")
    own = _site("https://extractor-a.example/", "extractor-a-example")
    _job(alice, "https://extractor-a.example")
    archived = _site("https://archived.example/", "archived-example")
    _job(alice, "https://archived.example")
    theirs = _site("https://extractor-b.example/", "extractor-b-example")
    _job(bob, "https://extractor-b.example")
    _site("https://orphan.example/", "orphan-example")  # nobody's jobs
    return {
        "alice": alice, "bob": bob, "raw_a": raw_a, "raw_b": raw_b,
        "own": own, "archived": archived, "theirs": theirs,
    }


def _req(method, path, body=None, key="", **extra):
    rf = RequestFactory()
    kw = {"HTTP_X_API_KEY": key, **extra}
    if body is not None:
        return getattr(rf, method.lower())(
            path, json.dumps(body), content_type="application/json", **kw
        )
    return getattr(rf, method.lower())(path, **kw)


# ───────────────────────────── list + tenancy ────────────────────────────────


class TestExtractorList:
    def test_list_scoped_to_own_jobs(self, tenant):
        from scraper.api.extractors import list_extractors

        resp = list_extractors(_req("GET", "/api/v1/extractors", key=tenant["raw_a"]))
        body = json.loads(resp.content)
        slugs = [e["slug"] for e in body["extractors"]]
        assert sorted(slugs) == ["archived-example", "extractor-a-example"], (
            "only sites reachable from THIS key's jobs"
        )
        assert "extractor-b-example" not in slugs, "other partners' sites never leak"
        assert "orphan-example" not in slugs, "job-less sites never leak"

    def test_list_envelope_mirrors_list_jobs(self, tenant):
        from scraper.api.extractors import list_extractors

        resp = list_extractors(_req("GET", "/api/v1/extractors", key=tenant["raw_a"]))
        body = json.loads(resp.content)
        assert set(body) >= {"extractors", "page", "page_size", "total_items", "total_pages"}

    def test_list_page_size_cap(self, tenant):
        from scraper.api.extractors import list_extractors

        resp = list_extractors(
            _req("GET", "/api/v1/extractors?page_size=200", key=tenant["raw_a"])
        )
        assert resp.status_code == 422


# ─────────────────────────────────── detail ──────────────────────────────────


class TestExtractorDetail:
    def test_detail_shape(self, tenant):
        from scraper.api.extractors import extractor_detail

        resp = extractor_detail(
            _req("GET", "/api/v1/extractors/extractor-a-example",
                 key=tenant["raw_a"]), slug="extractor-a-example"
        )
        assert resp.status_code == 200, resp.content
        body = json.loads(resp.content)
        assert body["slug"] == "extractor-a-example"
        assert body["url"] == "https://extractor-a.example/"
        for k in ("name", "site_type", "platform", "scraping_method", "has_scraper",
                  "product_count", "last_scraped_at", "archived_at",
                  "input_urls_count", "fields"):
            assert k in body, f"detail must carry {k}"
        assert body["archived_at"] is None

    def test_detail_fields_carry_descriptions(self, tenant):
        tenant["own"].output_schema = {"fields": [
            {"name": "price", "description": "in cents"},
            {"name": "title"},
        ]}
        tenant["own"].save(update_fields=["output_schema"])
        from scraper.api.extractors import extractor_detail

        resp = extractor_detail(
            _req("GET", "/api/v1/extractors/extractor-a-example",
                 key=tenant["raw_a"]), slug="extractor-a-example"
        )
        body = json.loads(resp.content)
        assert body["fields"] == [
            {"name": "price", "description": "in cents"},
            {"name": "title"},
        ]
        assert body["input_urls_count"] == 0

    def test_cross_tenant_slug_is_404_non_oracle(self, tenant):
        from scraper.api.extractors import extractor_detail

        resp = extractor_detail(
            _req("GET", "/api/v1/extractors/extractor-b-example",
                 key=tenant["raw_a"]), slug="extractor-b-example"
        )
        assert resp.status_code == 404

    def test_unknown_slug_404(self, tenant):
        from scraper.api.extractors import extractor_detail

        resp = extractor_detail(
            _req("GET", "/api/v1/extractors/nope", key=tenant["raw_a"]), slug="nope"
        )
        assert resp.status_code == 404


# ──────────────────────────────────── PATCH ──────────────────────────────────


class TestExtractorPatch:
    def test_patch_name(self, tenant):
        from scraper.api.extractors import patch_extractor

        resp = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"name": "Renamed"}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert resp.status_code == 200, resp.content
        tenant["own"].refresh_from_db()
        assert tenant["own"].name == "Renamed"

    def test_patch_unknown_key_is_422(self, tenant):
        from scraper.api.extractors import patch_extractor

        resp = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"platform": "shopify"}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert resp.status_code == 422, resp.content
        tenant["own"].refresh_from_db()
        assert tenant["own"].platform == "", "rejected keys must not half-apply"

    def test_patch_input_urls_validated(self, tenant):
        from scraper.api.extractors import patch_extractor

        ok = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"input_urls": ["https://extractor-a.example/i/1"]}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert ok.status_code == 200
        tenant["own"].refresh_from_db()
        assert tenant["own"].input_urls == ["https://extractor-a.example/i/1"]

        bad = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"input_urls": "not-a-list"}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert bad.status_code == 422

    def test_patch_field_notes_caps(self, tenant):
        from scraper.api.extractors import patch_extractor

        ok = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"field_notes": {"price": "numeric, no currency symbol"}},
                 key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert ok.status_code == 200
        tenant["own"].output_schema = {"fields": [{"name": "price"}]}
        tenant["own"].save(update_fields=["output_schema"])
        bad = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"field_notes": {"price": "x" * 301}}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert bad.status_code == 422

    def test_patch_site_type_must_be_known(self, tenant):
        from scraper.api.extractors import patch_extractor

        ok = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"site_type": "articles"}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert ok.status_code == 200
        tenant["own"].refresh_from_db()
        assert tenant["own"].site_type == "articles"

        bad = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-a-example",
                 {"site_type": "warp-drive"}, key=tenant["raw_a"]),
            slug="extractor-a-example",
        )
        assert bad.status_code == 422

    def test_patch_scoped(self, tenant):
        from scraper.api.extractors import patch_extractor

        resp = patch_extractor(
            _req("PATCH", "/api/v1/extractors/extractor-b-example",
                 {"name": "hijack"}, key=tenant["raw_a"]),
            slug="extractor-b-example",
        )
        assert resp.status_code == 404
        tenant["theirs"].refresh_from_db()
        assert tenant["theirs"].name != "hijack"


# ─────────────────────────── archive / unarchive / DELETE ────────────────────


class TestExtractorArchive:
    def test_archive_roundtrip(self, tenant):
        from scraper.api.extractors import archive_extractor, unarchive_extractor

        resp = archive_extractor(
            _req("POST", "/api/v1/extractors/archived-example/archive",
                 key=tenant["raw_a"]), slug="archived-example"
        )
        assert resp.status_code == 200, resp.content
        tenant["archived"].refresh_from_db()
        assert tenant["archived"].is_archived

        resp = unarchive_extractor(
            _req("POST", "/api/v1/extractors/archived-example/unarchive",
                 key=tenant["raw_a"]), slug="archived-example"
        )
        assert resp.status_code == 200
        tenant["archived"].refresh_from_db()
        assert not tenant["archived"].is_archived

    def test_archive_scoped_404(self, tenant):
        from scraper.api.extractors import archive_extractor

        resp = archive_extractor(
            _req("POST", "/api/v1/extractors/extractor-b-example/archive",
                 key=tenant["raw_a"]), slug="extractor-b-example"
        )
        assert resp.status_code == 404
        tenant["theirs"].refresh_from_db()
        assert not tenant["theirs"].is_archived


class TestNoPartnerDelete:
    def test_delete_is_405_deliberately(self, tenant):
        from scraper.api.extractors import extractor_dispatch

        resp = extractor_dispatch(
            _req("DELETE", "/api/v1/extractors/extractor-a-example",
                 key=tenant["raw_a"]), slug="extractor-a-example"
        )
        assert resp.status_code == 405, (
            "archive is the removal path; DELETE stays a superuser UI action"
        )
        assert models.Site.objects.filter(slug="extractor-a-example").exists()


# ─────────────────────── W27-7 async touchpoint: job.created ─────────────────


class TestJobCreatedCarriesRerunOf:
    def test_created_event_data_has_rerun_of(self, db):
        from scraper.api.writers import create_job

        u, raw = _partner("_t27c_emit")
        body = {
            "url": "https://emit-w27c.example/i",
            "input_mode": "url_list",
            "item_urls": ["https://emit-w27c.example/i/1"],
        }
        rf = RequestFactory()
        from unittest.mock import patch

        with patch("scraper.tasks.dispatch_scrape_job"), patch(
            "scraper.api.writers.transaction"
        ) as _tx:
            _tx.atomic.return_value.__enter__ = lambda *a: None
            _tx.atomic.return_value.__exit__ = lambda *a: False
            resp = create_job(
                rf.post(
                    "/api/v1/jobs", json.dumps(body),
                    content_type="application/json", HTTP_X_API_KEY=raw,
                )
            )
        assert resp.status_code == 202, resp.content
        job = models.ScrapeJob.objects.get(url="https://emit-w27c.example/i")
        row = models.EventOutbox.objects.filter(job_id=job.id, event_type="job.created").first()
        assert row is not None
        assert "rerun_of" in row.payload["data"], (
            "job.created must carry rerun_of (null at creation) per async_api.yaml"
        )
        assert row.payload["data"]["rerun_of"] is None
