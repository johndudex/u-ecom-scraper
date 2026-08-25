"""POST /api/v1/jobs — create-path persistence + response-shape gaps.

Four functional fixes locked here (docs/specs/sync_api.yaml is authoritative):

F1. item_urls must be PERSISTED, not just validated — the url_list pipeline
    falls back to scrapers/{slug}/input_urls.json when Site.input_urls is
    empty (tasks.py _build_initial_state), so dropping the list made every
    url_list create run with 0 URLs. Mirrors intake_create_job
    (views.py:2593-2607) exactly: write_json(scrapers_key(slug,
    "input_urls.json"), {"urls": [...]}) inside try/except — FM failure must
    never break create.

F2. listing_urls (list_page) must reach the pipeline: it reads listing URLs
    from search_criteria (newline-joined — the CharField→TextField widening
    was done for exactly this; spec line 226 says "this API accepts an array
    and performs that join server-side").

F3. search_term's spec field name is `search_keywords` (spec line 1611:
    "Required for input_mode=search_term: the query. Stored as
    search_criteria"). The endpoint read only `search_criteria`, so a client
    following the spec got 422. Both names must work; the spec name wins.

F4. The 202 body is the spec's JobCreated schema (line 1680): required
    [job_id, state, created_at, status_url] plus the derived artifact URLs,
    and the 202 headers block documents a Location header.

Run: docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings \
     django bash -c "cd /app && python -m pytest tests/test_api_create_persist.py -q"
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from types import ModuleType
from contextlib import ExitStack
from unittest.mock import Mock, patch

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
from scraper.api.writers import create_job  # noqa: E402

rf = RequestFactory()


def _fm_stub(write_json=None):
    """A controllable stand-in for the src.artifacts File Master client.

    create_job resolves it with a function-local ``import src.artifacts as
    artifacts`` at CALL time. That import can bind through EITHER
    sys.modules["src.artifacts"] OR the parent package attribute
    src.artifacts (bpo-30024) — and sibling test files (test_f8) replace the
    sys.modules entry with a 2-attr stub at collection time. So a patch of
    one module object's attribute is not order-safe; _patch_fm() below
    points BOTH binding sites at the same stub for the duration of the test.
    """
    fake = ModuleType("src.artifacts")
    fake.scrapers_key = lambda slug, *parts: "/".join(["scrapers", slug, *parts])
    fake.write_json = write_json if write_json is not None else Mock()
    return fake


def _patch_fm(stack, fake):
    import src

    stack.enter_context(patch.dict(sys.modules, {"src.artifacts": fake}))
    stack.enter_context(patch.object(src, "artifacts", fake, create=True))


def _fake_delay(*a, **k):
    """dispatch does update(celery_task_id=task.id) — a MagicMock id breaks
    the SQL. Return a stub with a real string id (mirrors test_api_create)."""
    return type("T", (), {"id": "task-x"})()


_URL_A = "https://www.rmwilliams.com.au/comfort-craftsman-boot-chestnut-yearling-leather.html?lang=en_AU"
_URL_B = "https://www.rmwilliams.com.au/mulyungarie-quarter-zip-sweatshirt-brushed-back-sweat-pine.html?lang=en_AU"
_LIST_A = "https://www.rmwilliams.com.au/footwear/men/chelsea-boots?lang=en_AU"
_LIST_B = "https://www.rmwilliams.com.au/clothing/men/sweatshirts?lang=en_AU"


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_create_persist", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    key = models.ApiKey.objects.create(
        user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    )
    return u, raw, key


def _req(u, raw, body):
    return rf.post(
        "/api/v1/jobs",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_X_API_KEY=raw,
    )


VALID_URL_LIST = {
    "url": _URL_A,
    "input_mode": "url_list",
    "content_type": "product",
    "item_urls": [_URL_A, _URL_B],
    "target_fields": ["title", "price"],
}


# ── F1: item_urls persisted to the File Master ─────────────────────────────


class TestItemUrlsPersisted:
    def test_item_urls_written_to_input_urls_json(
        self, partner, db, django_capture_on_commit_callbacks
    ):
        """url_list create persists the deduped list to the File Master at
        scrapers/{slug}/input_urls.json — the file the url_list pipeline
        falls back to when Site.input_urls is empty."""
        u, raw, key = partner
        body = {**VALID_URL_LIST, "item_urls": [_URL_A, _URL_B, _URL_A]}  # dup
        fake = _fm_stub()
        with ExitStack() as st, django_capture_on_commit_callbacks(execute=True):
            _patch_fm(st, fake)
            st.enter_context(
                patch("scraper.tasks.run_scrape_task.delay", side_effect=_fake_delay)
            )
            r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        fake.write_json.assert_called_once()
        fm_key, payload = fake.write_json.call_args[0]
        assert fm_key.startswith("scrapers/") and fm_key.endswith("/input_urls.json")
        assert payload == {"urls": [_URL_A, _URL_B]}  # deduped, order kept

    def test_fm_write_failure_never_breaks_create(
        self, partner, db, django_capture_on_commit_callbacks
    ):
        """Intake parity: an FM outage logs a warning and returns 202 — the
        job must still be created and dispatched."""
        u, raw, key = partner
        delay_p = patch("scraper.tasks.run_scrape_task.delay", side_effect=_fake_delay)
        fake = _fm_stub(write_json=Mock(side_effect=RuntimeError("FM down")))
        with ExitStack() as st, django_capture_on_commit_callbacks(execute=True):
            _patch_fm(st, fake)
            delay = st.enter_context(delay_p)
            r = create_job(_req(u, raw, VALID_URL_LIST))
        assert r.status_code == 202
        assert models.ScrapeJob.objects.filter(url=_URL_A).exists()
        assert delay.called

    def test_no_input_urls_write_for_non_url_list_modes(
        self, partner, db, django_capture_on_commit_callbacks
    ):
        """list_page/search_term jobs never write input_urls.json — that file
        is the url_list input contract, and a stale one would be scraped by
        accident (nav validation says "input_urls.json is NOT used")."""
        u, raw, key = partner
        body = {
            "url": _URL_A,
            "input_mode": "list_page",
            "listing_urls": [_LIST_A, _LIST_B],
        }
        fake = _fm_stub()
        with ExitStack() as st, django_capture_on_commit_callbacks(execute=True):
            _patch_fm(st, fake)
            st.enter_context(
                patch("scraper.tasks.run_scrape_task.delay", side_effect=_fake_delay)
            )
            r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        fake.write_json.assert_not_called()


# ── F2: listing_urls reach the pipeline via search_criteria ────────────────


class TestListingUrlsPersisted:
    def test_listing_urls_newline_joined_into_search_criteria(self, partner, db):
        """list_page: the pipeline reads listing URLs from search_criteria
        (spec line 226 — the API accepts an array and joins server-side)."""
        u, raw, key = partner
        body = {
            "url": _URL_A,
            "input_mode": "list_page",
            "listing_urls": [_LIST_A, _LIST_B, _LIST_A],
        }
        r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        job = models.ScrapeJob.objects.get(url=_URL_A)
        assert job.search_criteria == f"{_LIST_A}\n{_LIST_B}"


# ── F3: spec field name search_keywords accepted ───────────────────────────


class TestSearchKeywords:
    def test_spec_field_name_accepted(self, partner, db):
        """The spec documents `search_keywords` (CreateJobRequest) — a client
        following the spec must not get a 422."""
        u, raw, key = partner
        body = {
            "url": _URL_A,
            "input_mode": "search_term",
            "search_keywords": "oral surgeon",
        }
        r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        job = models.ScrapeJob.objects.get(url=_URL_A)
        assert job.search_criteria == "oral surgeon"

    def test_legacy_alias_search_criteria_still_accepted(self, partner, db):
        u, raw, key = partner
        body = {
            "url": _URL_A,
            "input_mode": "search_term",
            "search_criteria": "chelsea boots",
        }
        r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        assert (
            models.ScrapeJob.objects.get(url=_URL_A).search_criteria == "chelsea boots"
        )

    def test_spec_name_wins_when_both_sent(self, partner, db):
        u, raw, key = partner
        body = {
            "url": _URL_A,
            "input_mode": "search_term",
            "search_keywords": "oral surgeon",
            "search_criteria": "legacy",
        }
        r = create_job(_req(u, raw, body))
        assert r.status_code == 202
        assert (
            models.ScrapeJob.objects.get(url=_URL_A).search_criteria == "oral surgeon"
        )


# ── F4: 202 body = the spec's JobCreated schema + Location header ──────────


class TestCreated202Shape:
    def test_body_matches_job_created_schema(self, partner, db):
        """sync_api.yaml JobCreated: required [job_id, state, created_at,
        status_url]; plus the artifact URLs the 202 example lists."""
        u, raw, key = partner
        r = create_job(_req(u, raw, VALID_URL_LIST))
        assert r.status_code == 202
        body = json.loads(r.content)
        jid = body["job_id"]
        assert {"job_id", "state", "created_at", "status_url"} <= set(body)
        assert body["state"] == "inprogress"
        # date-time format (spec: type string, format date-time)
        datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))
        assert body["status_url"] == f"/api/v1/jobs/{jid}"
        assert body["sample_url"] == f"/api/v1/jobs/{jid}/sample"
        assert body["output_url"] == f"/api/v1/jobs/{jid}/output"
        assert body["output_download_url"] == f"/api/v1/jobs/{jid}/output/download"
        assert body["scraper_code_url"] == f"/api/v1/jobs/{jid}/scraper-code"

    def test_location_header_set(self, partner, db):
        """The 202 headers block documents Location: the canonical status
        resource ("/api/v1/jobs/481")."""
        u, raw, key = partner
        r = create_job(_req(u, raw, VALID_URL_LIST))
        jid = json.loads(r.content)["job_id"]
        assert r.status_code == 202
        assert r["Location"] == f"/api/v1/jobs/{jid}"

    def test_created_at_agrees_with_the_stored_job(self, partner, db):
        u, raw, key = partner
        r = create_job(_req(u, raw, VALID_URL_LIST))
        body = json.loads(r.content)
        job = models.ScrapeJob.objects.get(pk=body["job_id"])
        assert body["created_at"] == job.created_at.isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
