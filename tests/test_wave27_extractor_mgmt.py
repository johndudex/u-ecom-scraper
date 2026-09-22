"""Wave-27a — extractor management: Edit-form fix, delete guard, archive,
rerun lineage. (docs/plans/wave27-extractor-plan.md, items W27-1/2/3/8)

Locks:
- W27-1: the Site Edit form must POST to /sites/<id>/edit/, not /sites/add/
  (the shipped form made every UI edit fail duplicate-URL validation).
- W27-2: site_delete is SUPERUSER-only + requires confirm=<slug>; jobs are
  never touched (no FK); the detail page shows the control to superusers only.
- W27-3: Site.archived_at (migration 0040) + archive/unarchive endpoints;
  the default site list hides archived rows (?archived=1 shows them);
  site_scrape/site_rerun refuse archived sites; a NEW job submitted for an
  archived site auto-unarchives it (explicit user intent wins); the intake
  library gets "Remove from Saved" (posts the existing job_update is_saved=0).
- W27-8: ScrapeJob.parent_job/origin_job set ONLY by job_restart (origin =
  chain root); fresh-create paths stay unlinked; rerun_of rides JobStatus
  (partner API) + intake_jobs JSON.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave27_extractor_mgmt.py -q
"""
from __future__ import annotations

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
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from scraper import models  # noqa: E402

TPL_DIR = os.path.join(ROOT, "webapp", "scraper", "templates", "scraper")


def _tpl(name):
    with open(os.path.join(TPL_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _site(**kw):
    d = dict(url="https://w27.example/", name="w27-example", slug="w27-example")
    d.update(kw)
    return models.Site.objects.create(**d)


def _job(user, url="https://w27.example/", **kw):
    d = dict(
        url=url,
        product_url=url,
        title=f"job {url}",
        user=user,
        status=models.ScrapeJob.STATUS_COMPLETED,
        input_mode="url_list",
    )
    d.update(kw)
    return models.ScrapeJob.objects.create(**d)


def _client(who):
    c = Client()
    c.force_login(who)
    return c


def _site_with_seed():
    """[wave-40 T5] A Site whose URL list is recoverable — the site_scrape and
    job_restart url_list gates refuse a site with no list anywhere. The FM
    sync write is mocked: Site.save() mirrors input_urls into the File
    Master, and no test touches the dev file-master. get_or_create: restart
    chains re-enter this helper within one test."""
    with patch("src.artifacts.write_json", create=True), patch(
        "src.artifacts.exists", create=True, return_value=False
    ):
        site, _ = models.Site.objects.get_or_create(
            url="https://w27.example/",
            defaults=dict(
                name="w27-example", slug="w27-example",
                input_urls=["https://w27.example/p/1"],
            ),
        )
        return site


@pytest.fixture
def alice(db):
    return User.objects.create_user("w27alice", password="x")


@pytest.fixture
def root(db):
    return User.objects.create_superuser("w27root", "root@team.io", password="x")


# ────────────────────────────── W27-1: Edit form action ──────────────────────


class TestEditFormAction:
    def test_edit_form_posts_to_edit_url(self, alice):
        site = _site()
        r = _client(alice).get(reverse("site_edit", args=[site.id]))
        assert r.status_code == 200
        assert f'action="/sites/{site.id}/edit/"' in r.content.decode(), (
            "the Edit form must POST to the edit URL — posting to site_add "
            "fails duplicate-URL validation and makes UI edits impossible"
        )

    def test_add_form_still_posts_to_add_url(self, alice):
        r = _client(alice).get(reverse("site_add"))
        assert r.status_code == 200
        assert 'action="/sites/add/"' in r.content.decode()

    def test_edit_post_saves_and_redirects(self, alice):
        site = _site()
        r = _client(alice).post(
            reverse("site_edit", args=[site.id]),
            {
                "url": "https://w27.example/",
                "sample_url": "https://w27.example/p/1",
                "currency": "USD",
                "site_type": "shopping",
                "input_urls_json": "",
            },
        )
        assert r.status_code == 302
        site.refresh_from_db()
        assert site.sample_url == "https://w27.example/p/1"
        assert site.currency == "USD"


# ───────────────────────────── W27-2: Delete guard ───────────────────────────


class TestDeleteGuard:
    def test_non_superuser_post_blocked(self, alice):
        site = _site()
        r = _client(alice).post(
            reverse("site_delete", args=[site.id]), {"confirm": site.slug}
        )
        assert r.status_code == 403
        assert models.Site.objects.filter(pk=site.pk).exists()

    def test_missing_confirm_is_noop(self, root):
        site = _site()
        r = _client(root).post(reverse("site_delete", args=[site.id]))
        assert r.status_code == 302
        assert models.Site.objects.filter(pk=site.pk).exists()

    def test_wrong_confirm_is_noop(self, root):
        site = _site()
        r = _client(root).post(
            reverse("site_delete", args=[site.id]), {"confirm": "some-other-slug"}
        )
        assert r.status_code == 302
        assert models.Site.objects.filter(pk=site.pk).exists()

    def test_superuser_confirm_deletes_site_not_jobs(self, root, alice):
        site = _site()
        job = _job(alice, url=site.url)
        r = _client(root).post(
            reverse("site_delete", args=[site.id]), {"confirm": site.slug}
        )
        assert r.status_code == 302
        assert not models.Site.objects.filter(pk=site.pk).exists()
        assert models.ScrapeJob.objects.filter(pk=job.pk).exists(), (
            "ScrapeJob has no Site FK — the audit trail must survive a delete"
        )

    def test_delete_control_superusers_only(self, alice, root):
        site = _site()
        delete_url = reverse("site_delete", args=[site.id])
        assert delete_url in _client(root).get(
            reverse("site_detail", args=[site.id])
        ).content.decode(), "superusers need an actionable Delete control"
        assert delete_url not in _client(alice).get(
            reverse("site_detail", args=[site.id])
        ).content.decode(), "regular users must not see a delete control"


# ─────────────────────────────── W27-3: Archive ──────────────────────────────


class TestArchive:
    def test_archive_unarchive_round_trip(self, alice):
        site = _site()
        r = _client(alice).post(reverse("site_archive", args=[site.id]))
        assert r.status_code == 302
        site.refresh_from_db()
        assert site.archived_at is not None
        r = _client(alice).post(reverse("site_unarchive", args=[site.id]))
        assert r.status_code == 302
        site.refresh_from_db()
        assert site.archived_at is None

    def test_list_hides_archived_shows_with_flag(self, alice):
        keep = _site(url="https://keep.example/", slug="keep-example")
        gone = _site(url="https://gone.example/", slug="gone-example")
        _client(alice).post(reverse("site_archive", args=[gone.id]))
        html = _client(alice).get(reverse("site_list")).content.decode()
        assert "keep.example" in html
        assert "gone.example" not in html, "archived rows leave the default list"
        html_arch = (
            _client(alice).get(reverse("site_list"), {"archived": "1"}).content.decode()
        )
        assert "keep.example" in html_arch and "gone.example" in html_arch

    def test_scrape_refuses_archived(self, alice):
        site = _site()
        _client(alice).post(reverse("site_archive", args=[site.id]))
        with patch("scraper.tasks.dispatch_scrape_job") as dispatch:
            r = _client(alice).post(reverse("site_scrape", args=[site.id]), {"rescrape": "on"})
        assert r.status_code == 302
        dispatch.assert_not_called()
        assert not models.ScrapeJob.objects.filter(url=site.url).exists()

    def test_rerun_refuses_archived(self, alice):
        site = _site(
            has_scraper=True, default_scraper_path="scrapers/w27-example/scraper.py"
        )
        _client(alice).post(reverse("site_archive", args=[site.id]))
        r = _client(alice).post(reverse("site_rerun", args=[site.id]))
        assert r.status_code == 302
        site.refresh_from_db()
        assert site.last_scraped_at is None, "an archived site must not re-execute"

    def test_check_tracker_auto_unarchives_on_new_job(self, alice):
        site = _site()
        _client(alice).post(reverse("site_archive", args=[site.id]))
        from webapp.agents.nodes.check_tracker import _handle_new_site

        _handle_new_site(site.url, site.slug)
        site.refresh_from_db()
        assert site.archived_at is None, (
            "an explicit new job beats a stale archive — auto-unarchive + log"
        )

    def test_unsave_saved_job_via_job_update(self, alice):
        job = _job(alice, is_saved=True)
        r = _client(alice).post(
            reverse("job_update", args=[job.id]),
            {"is_saved": "0"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert r.status_code == 200
        assert r.json()["is_saved"] is False
        job.refresh_from_db()
        assert job.is_saved is False

    def test_intake_library_has_remove_from_saved(self):
        assert "Remove from Saved" in _tpl("intake.html"), (
            "the library needs an un-save affordance — un-saving IS the "
            "delete for saved extractors (jobs are never hard-deleted)"
        )


# ─────────────────────── W27-8: Rerun lineage (+ 0040 fields) ────────────────


class TestLineageFields:
    def test_migration_fields_exist(self):
        assert models.Site._meta.get_field("archived_at") is not None
        assert models.ScrapeJob._meta.get_field("field_notes") is not None
        assert models.ScrapeJob._meta.get_field("parent_job") is not None
        assert models.ScrapeJob._meta.get_field("origin_job") is not None

    def test_fresh_job_has_no_lineage(self, alice):
        job = _job(alice)
        assert job.parent_job_id is None
        assert job.origin_job_id is None


class TestRestartLineage:
    def _restart(self, client, job):
        # [wave-40 T5] a recoverable URL list on the Site row keeps this test
        # on lineage — the restart gate refuses a url_list job with none.
        _site_with_seed()
        with patch("scraper.tasks.dispatch_scrape_job") as dispatch:
            r = client.post(reverse("job_restart", args=[job.id]))
        assert r.status_code == 302
        dispatch.assert_called_once()
        new = models.ScrapeJob.objects.exclude(pk=job.pk).order_by("-id").first()
        assert new is not None
        # job_restart only clones TERMINAL sources — mark the clone terminal
        # so the next leg of the chain can restart it too.
        new.status = models.ScrapeJob.STATUS_COMPLETED
        new.save(update_fields=["status"])
        return new

    def test_restart_sets_parent_and_origin(self, alice):
        a = _job(alice)
        b = self._restart(_client(alice), a)
        assert b.parent_job_id == a.id
        assert b.origin_job_id == a.id

    def test_nested_restart_roots_at_a(self, alice):
        a = _job(alice)
        b = self._restart(_client(alice), a)
        c = self._restart(_client(alice), b)
        assert c.parent_job_id == b.id, "parent stays the immediate source"
        assert c.origin_job_id == a.id, "origin is the chain root, one read"

    def test_site_scrape_creates_unlinked_job(self, alice):
        site = _site_with_seed()  # [wave-40 T5] a scrapeable site has a URL list
        with patch("scraper.tasks.dispatch_scrape_job"):
            _client(alice).post(reverse("site_scrape", args=[site.id]))
        job = models.ScrapeJob.objects.get(url=site.url)
        assert job.parent_job_id is None and job.origin_job_id is None

    def test_intake_jobs_exposes_rerun_of(self, alice):
        a = _job(alice)
        b = self._restart(_client(alice), a)
        rows = {j["id"]: j for j in _client(alice).get(reverse("intake_jobs")).json()["jobs"]}
        assert rows[a.id]["rerun_of"] is None
        assert rows[b.id]["rerun_of"] == a.id


class TestPartnerApiRerunOf:
    def test_job_status_exposes_rerun_of(self, db):
        import json
        import secrets

        from django.test import RequestFactory

        from scraper.api.readers import job_status

        u = User.objects.create_user("_t_w27_" + secrets.token_hex(3), password="x")
        raw = "pk_" + secrets.token_hex(16)
        models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
        a = _job(u, url="https://api-w27.example/i", created_via="api")
        b = _job(
            u,
            url="https://api-w27.example/i",
            created_via="api",
            parent_job=a,
            origin_job=a,
        )
        rf = RequestFactory()
        body_b = json.loads(
            job_status(rf.get(f"/api/v1/jobs/{b.id}", HTTP_X_API_KEY=raw), b.id).content
        )
        body_a = json.loads(
            job_status(rf.get(f"/api/v1/jobs/{a.id}", HTTP_X_API_KEY=raw), a.id).content
        )
        assert body_b["rerun_of"] == a.id
        assert body_a["rerun_of"] is None
