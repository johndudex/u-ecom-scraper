"""[wave-55 H4] Intake opts into the selective-rescrape path when it can.

Audit (2026-10-05, H4 #19): ``intake_create_job`` dispatched with
``rescrape=False`` unconditionally, so every re-submission of a site with a
production scraper paid the full pipeline price — job 1198 re-ran site
analysis + field mapping + code generation (4m00s) where its reuse-eligible
sibling 1200 (the same site re-driven through /restart/ with the flag) took
the selective path and only re-executed. ``check_tracker`` has judged the
config diff since wave-36; only the intake half of the plumb was missing.

Safe default: reuse ONLY when the Site finished AND the submitted config
(url / mode / fields / search term) is identical to the newest prior
COMPLETED job for that URL — anything else keeps today's full build. The
intake form has no explicit rebuild flag: ``force=1`` is the W31
duplicate-modal click-through that ANY re-submission of a processed host
carries, not a rebuild request (the Full re-run button's ``force_full`` on
the re-run endpoints is the only from-scratch escape, and it bypasses this
gate downstream).

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_intake_resume_path.py -q"
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from agents.nodes.check_tracker import (  # noqa: E402
    check_tracker,
    config_unchanged_since_prior,
)
from django.test import Client  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402
from model_bakery import baker  # noqa: E402
from scraper.models import ScrapeJob, Site  # noqa: E402

URL = "https://books.example.com/catalogue/some-item"
# intake_create_job derives the Site slug from the host, exactly as
# tasks._generate_slug does.
SLUG = "books-example-com"


def _mute_resume_log(monkeypatch):
    """``check_tracker`` logs one [RESUME-INVOCATION] row via graph's helper.
    Patch the module OBJECTS (never the string path — ``agents.*`` and
    ``webapp.agents.*`` are distinct module objects on this sys.path) so the
    test does not depend on which spelling some earlier file imported."""
    import types

    try:  # the node imports it lazily — load it before patching
        import agents.graph  # noqa: F401
    except Exception:
        return
    for name in ("agents.graph", "webapp.agents.graph"):
        mod = sys.modules.get(name)
        if isinstance(mod, types.ModuleType) and hasattr(mod, "_log_event_row"):
            monkeypatch.setattr(mod, "_log_event_row", lambda *a, **k: None)


# ─── the shared comparator (check_tracker) ───────────────────────────────────


class TestConfigUnchangedSincePrior:
    def _prior(self, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("status", ScrapeJob.STATUS_COMPLETED)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        return baker.make(ScrapeJob, **kw)

    @pytest.mark.django_db
    def test_no_prior_completed_job_is_never_reuse(self):
        self._prior(status=ScrapeJob.STATUS_FAILED)

        assert config_unchanged_since_prior(URL, "url_list", "", []) is False

    @pytest.mark.django_db
    def test_identical_config_reuses(self):
        self._prior(target_fields=["title", "price"])

        assert config_unchanged_since_prior(
            URL, "url_list", "", ["price", "title"]
        ) is True

    @pytest.mark.django_db
    def test_changed_fields_do_not_reuse(self):
        self._prior(target_fields=["title", "price"])

        assert config_unchanged_since_prior(
            URL, "url_list", "", ["title", "price", "url"]
        ) is False

    @pytest.mark.django_db
    def test_changed_mode_does_not_reuse(self):
        self._prior(input_mode="url_list")

        assert config_unchanged_since_prior(URL, "list_page", "", []) is False

    @pytest.mark.django_db
    def test_changed_search_term_does_not_reuse(self):
        self._prior(input_mode="search_term", search_criteria="wool coat")

        assert config_unchanged_since_prior(
            URL, "search_term", "cashmere coat", []
        ) is False

    @pytest.mark.django_db
    def test_the_newest_completed_job_is_the_one_judged(self):
        """An older identical submission must not mask what the most recent
        run actually built — that newest row is the artifact set reuse would
        re-execute."""
        now = dj_tz.now()
        self._prior(target_fields=["title"], completed_at=now - timedelta(hours=2))
        self._prior(target_fields=["title", "price"], completed_at=now)

        assert config_unchanged_since_prior(
            URL, "url_list", "", ["title", "price"]
        ) is True
        assert config_unchanged_since_prior(
            URL, "url_list", "", ["title"]
        ) is False

    @pytest.mark.django_db
    def test_another_site_never_reuses(self):
        self._prior(url="https://other.example.com/p/1")

        assert config_unchanged_since_prior(URL, "url_list", "", []) is False


# ─── the receiving end: the flag still selects the selective arm ─────────────


class TestCheckTrackerRescrapeArm:
    def _site(self, **kw) -> Site:
        kw.setdefault("url", URL)
        kw.setdefault("slug", SLUG)
        kw.setdefault("status", "complete")
        return baker.make(Site, **kw)

    def _prior(self, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("status", ScrapeJob.STATUS_COMPLETED)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        return baker.make(ScrapeJob, **kw)

    def _state(self, **kw) -> dict:
        # A real row: check_tracker logs one [RESUME-INVOCATION] SessionLog
        # against it (FK-verified), and the diff excludes this pk.
        job = baker.make(ScrapeJob, url=URL, input_mode="url_list")
        state = {
            "job_id": job.id,
            "url": URL,
            "site_slug": SLUG,
            "input_mode": "url_list",
            "skip_approvals": True,
        }
        state.update(kw)
        return state

    @pytest.mark.django_db
    def test_rescrape_true_takes_the_selective_arm(self, monkeypatch):
        _mute_resume_log(monkeypatch)
        site = self._site()
        self._prior()  # the completed twin the config diff judges against

        result = check_tracker(self._state(rescrape=True))

        assert result.goto == "setup_workspace"
        update = result.update
        assert update["skip_site_analysis"] is True
        assert update["skip_product_analysis"] is True
        assert update["skip_code_generation"] is True
        site.refresh_from_db()
        assert site.status == "in_progress"

    @pytest.mark.django_db
    def test_rescrape_false_still_full_builds(self, monkeypatch):
        """The flag is the selector: without it a complete site falls through
        to the skip_approvals auto-approve, which regenerates every phase."""
        _mute_resume_log(monkeypatch)
        self._site()
        self._prior()

        result = check_tracker(self._state(rescrape=False))

        assert result.goto == "setup_workspace"
        update = result.update
        # the full-build arm never asserts reuse — it omits the flags (the
        # graph seed defaults them False) rather than setting them True
        assert update.get("skip_site_analysis") is not True
        assert update.get("skip_product_analysis") is not True
        assert update.get("skip_code_generation") is not True


# ─── the intake half: what the view actually dispatches ──────────────────────


class TestIntakeDispatchArgs:
    @pytest.fixture(autouse=True)
    def _client(self):
        self.client = Client()

    def _post(self, **extra):
        # force=1 is the W31 modal click-through — the realistic payload for
        # every re-submission this fix exists for.
        data = {"url": URL, "list_urls": URL, "force": "1"}
        data.update(extra)
        dispatch = MagicMock()
        with patch("scraper.tasks.run_scrape_task", dispatch), patch(
            "src.artifacts.exists", MagicMock(return_value=False), create=True
        ), patch("src.artifacts.write_json", MagicMock(), create=True):
            response = self.client.post(
                "/intake/create-job/", data, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
        return response, dispatch

    def _site(self, **kw) -> Site:
        kw.setdefault("url", URL)
        kw.setdefault("slug", SLUG)
        kw.setdefault("status", "complete")
        return baker.make(Site, **kw)

    def _prior(self, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("status", ScrapeJob.STATUS_COMPLETED)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        return baker.make(ScrapeJob, **kw)

    def _dispatched_kwargs(self, dispatch) -> dict:
        assert dispatch.apply_async.called, "the job was never dispatched"
        return dispatch.apply_async.call_args.kwargs["kwargs"]

    @pytest.mark.django_db
    def test_complete_site_with_identical_config_reuses(self):
        self._site()
        self._prior(target_fields=["title", "price"])

        _response, dispatch = self._post(target_fields="title,price")

        assert self._dispatched_kwargs(dispatch) == {"rescrape": True}

    @pytest.mark.django_db
    def test_complete_site_with_identical_blank_config_reuses(self):
        """The common re-submission: same URL, no field chips, no search term."""
        self._site()
        self._prior()

        _response, dispatch = self._post()

        assert self._dispatched_kwargs(dispatch) == {"rescrape": True}

    @pytest.mark.django_db
    def test_no_site_row_full_builds(self):
        self._prior()

        _response, dispatch = self._post()

        assert self._dispatched_kwargs(dispatch) == {"rescrape": False}

    @pytest.mark.django_db
    def test_unfinished_site_full_builds(self):
        """A failed/in-progress Site has no trustworthy artifact set to reuse —
        the selective arm's own gate refuses it, so intake must not opt in."""
        self._site(status="failed")
        self._prior()

        _response, dispatch = self._post()

        assert self._dispatched_kwargs(dispatch) == {"rescrape": False}

    @pytest.mark.django_db
    def test_changed_fields_full_build(self):
        self._site()
        self._prior(target_fields=["title", "price"])

        _response, dispatch = self._post(target_fields="title,price,url")

        assert self._dispatched_kwargs(dispatch) == {"rescrape": False}

    @pytest.mark.django_db
    def test_changed_mode_full_build(self):
        self._site()
        self._prior(input_mode="url_list")

        _response, dispatch = self._post(nav_method="listing", listing_urls=URL)

        assert self._dispatched_kwargs(dispatch) == {"rescrape": False}

    @pytest.mark.django_db
    def test_changed_search_term_full_build(self):
        self._site()
        self._prior(input_mode="search_term", search_criteria="wool coat")

        _response, dispatch = self._post(
            nav_method="search", search_keywords="cashmere coat"
        )

        assert self._dispatched_kwargs(dispatch) == {"rescrape": False}

    @pytest.mark.django_db
    def test_the_force_click_through_is_not_a_rebuild_request(self):
        """W31's ``force=1`` is how ANY re-submission of a processed host gets
        past the duplicate modal — it must not opt the job out of reuse, or H4
        would be dead code for exactly the traffic it exists to help."""
        self._site()
        self._prior()

        _response, dispatch = self._post(force="1")

        assert self._dispatched_kwargs(dispatch) == {"rescrape": True}

    @pytest.mark.django_db
    def test_comparator_error_fails_open_to_a_full_build(self, monkeypatch):
        """The reuse decision must never gate the job: if the comparator
        cannot answer, intake builds from scratch — today's behavior."""
        self._site()
        self._prior()

        # agents/nodes/__init__ rebinds the package attribute `check_tracker`
        # to the FUNCTION, so `import a.b.c as x` would hand back the function
        # — importlib always yields the module object.
        import importlib

        module = importlib.import_module("agents.nodes.check_tracker")
        monkeypatch.setattr(
            module, "_prior_completed_job",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db boom")),
        )

        _response, dispatch = self._post()

        assert self._dispatched_kwargs(dispatch) == {"rescrape": False}


# ─── the partner half: the same gate on the API surface ──────────────────────


def _patch_fm(stack) -> None:
    """Point BOTH ``src.artifacts`` binding sites (the sys.modules entry and
    the parent package attribute) at a stub — the API create resolves the
    client with a function-local import at call time, and bpo-30024 means
    either binding can win (see tests/test_api_create_persist.py::_patch_fm)."""
    from types import ModuleType

    import src

    fake = ModuleType("src.artifacts")
    fake.scrapers_key = lambda slug, *parts: "/".join(["scrapers", slug, *parts])
    fake.write_json = MagicMock()
    stack.enter_context(patch.dict(sys.modules, {"src.artifacts": fake}))
    stack.enter_context(patch.object(src, "artifacts", fake, create=True))


class TestPartnerDispatchArgs:
    """Mirror of TestIntakeDispatchArgs on POST /api/v1/jobs: a partner
    re-post of a URL whose Site is complete and whose config is unchanged
    pays the reuse price, not the full-pipeline one."""

    @pytest.fixture(autouse=True)
    def _partner(self, db):
        from django.contrib.auth.models import User
        from scraper.models import ApiKey

        self.user = User.objects.create_user(username="_t_partner", password="x")
        raw = "pk_test_" + os.urandom(16).hex()
        ApiKey.objects.create(
            user=self.user, prefix=raw[:8], key_hash=ApiKey.hash_key(raw)
        )
        self.raw_key = raw

    def _post(self, **extra):
        from django.test import RequestFactory

        body = {
            "url": URL,
            "input_mode": "url_list",
            "item_urls": [URL],
            # W31: a completed host 409s without an explicit force, exactly
            # like intake's duplicate-modal click-through.
            "force": True,
        }
        body.update(extra)
        request = RequestFactory().post(
            "/api/v1/jobs",
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_API_KEY=self.raw_key,
        )
        dispatch = MagicMock()
        with ExitStack() as st:
            _patch_fm(st)
            st.enter_context(patch("scraper.tasks.run_scrape_task", dispatch))
            # the two side effects ride transaction.on_commit; firing them
            # inline is the point — this test reads what got published.
            st.enter_context(
                patch("django.db.transaction.on_commit", side_effect=lambda fn: fn())
            )
            from scraper.api.writers import create_job

            response = create_job(request)
        assert response.status_code == 202, response.content
        assert dispatch.apply_async.called, "the job was never dispatched"
        return dispatch.apply_async.call_args.kwargs["kwargs"]

    def _site(self, **kw) -> Site:
        kw.setdefault("url", URL)
        kw.setdefault("slug", SLUG)
        kw.setdefault("status", "complete")
        return baker.make(Site, **kw)

    def _prior(self, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("status", ScrapeJob.STATUS_COMPLETED)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        return baker.make(ScrapeJob, **kw)

    @pytest.mark.django_db
    def test_complete_site_with_identical_config_reuses(self):
        self._site()
        self._prior(target_fields=["title", "price"])

        kwargs = self._post(target_fields=["title", "price"])

        assert kwargs == {"rescrape": True}

    @pytest.mark.django_db
    def test_complete_site_with_identical_blank_config_reuses(self):
        """The common re-post: same URL, no field list, no search term."""
        self._site()
        self._prior()

        assert self._post() == {"rescrape": True}

    @pytest.mark.django_db
    def test_unfinished_site_full_builds(self):
        self._site(status="failed")
        self._prior()

        assert self._post() == {"rescrape": False}

    @pytest.mark.django_db
    def test_no_site_row_full_builds(self):
        self._prior()

        assert self._post() == {"rescrape": False}

    @pytest.mark.django_db
    def test_changed_fields_full_build(self):
        self._site()
        self._prior(target_fields=["title", "price"])

        assert self._post(target_fields=["title", "price", "url"]) == {
            "rescrape": False
        }

    @pytest.mark.django_db
    def test_changed_search_term_full_build(self):
        self._site()
        self._prior(input_mode="search_term", search_criteria="wool coat")

        assert self._post(
            input_mode="search_term", search_keywords="cashmere coat"
        ) == {"rescrape": False}

    @pytest.mark.django_db
    def test_comparator_error_fails_open_to_a_full_build(self, monkeypatch):
        """Same fail-open contract as intake: a broken reuse gate costs
        pipeline time, never a job."""
        import importlib

        self._site()
        self._prior()

        module = importlib.import_module("agents.nodes.check_tracker")
        monkeypatch.setattr(
            module, "_prior_completed_job",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db boom")),
        )

        assert self._post() == {"rescrape": False}


# ─── the re-dispatch lanes: reuse is re-derived at the moment of publish ─────


class TestRedispatchLanesReDeriveReuse:
    """The reuse decision lives only in the publish kwargs — nothing on the
    row records it — so the two lanes that re-dispatch a PENDING row
    (maintenance-lock drain, abandoned-PENDING sweep) hardcoding
    ``rescrape=False`` silently revert a reuse-eligible job to the full-pipeline
    build. Both lanes re-derive the same gate intake applied."""

    @pytest.fixture(autouse=True)
    def _sweep_enabled(self, monkeypatch):
        from django.conf import settings

        monkeypatch.setattr(settings, "REDISPATCH_SWEEP_ENABLED", True)

    def _pending(self, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("status", ScrapeJob.STATUS_PENDING)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        job = baker.make(ScrapeJob, celery_task_id="", **kw)
        # created_at is auto_now_add; both lanes are gated on age.
        ScrapeJob.objects.filter(pk=job.pk).update(
            created_at=dj_tz.now() - timedelta(minutes=30)
        )
        return ScrapeJob.objects.get(pk=job.pk)

    def _site(self) -> Site:
        return baker.make(Site, url=URL, slug=SLUG, status="complete")

    def _prior(self, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("status", ScrapeJob.STATUS_COMPLETED)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        return baker.make(ScrapeJob, **kw)

    def _capture(self, monkeypatch) -> MagicMock:
        from scraper import tasks as scraper_tasks

        published = MagicMock()
        monkeypatch.setattr(scraper_tasks, "dispatch_scrape_job", published)
        return published

    @pytest.mark.django_db
    def test_a_held_reuse_eligible_job_drains_with_reuse(self, monkeypatch):
        self._site()
        self._prior(target_fields=["title", "price"])
        held = self._pending(target_fields=["title", "price"])
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        assert scraper_tasks.resume_maintenance_held_jobs() == [held.id]
        published.assert_called_once_with(held.id, rescrape=True)

    @pytest.mark.django_db
    def test_a_held_mismatched_job_drains_full(self, monkeypatch):
        self._site()
        self._prior(target_fields=["title", "price"])
        held = self._pending(target_fields=["title", "price", "url"])
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        scraper_tasks.resume_maintenance_held_jobs()

        published.assert_called_once_with(held.id, rescrape=False)

    @pytest.mark.django_db
    def test_a_held_job_with_no_site_drains_full(self, monkeypatch):
        """No Site row → nothing trustworthy to reuse, whatever the prior."""
        self._prior()
        self._pending()
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        scraper_tasks.resume_maintenance_held_jobs()

        assert published.call_args.kwargs == {"rescrape": False}

    @pytest.mark.django_db
    def test_an_abandoned_reuse_eligible_job_redispatches_with_reuse(self, monkeypatch):
        self._site()
        self._prior(target_fields=["title", "price"])
        abandoned = self._pending(target_fields=["title", "price"])
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        result = scraper_tasks.redispatch_abandoned_pending()

        assert result == {"action": "redispatched", "job_id": abandoned.id}
        published.assert_called_once_with(abandoned.id, rescrape=True)

    @pytest.mark.django_db
    def test_an_abandoned_mismatched_job_redispatches_full(self, monkeypatch):
        self._site()
        self._prior(input_mode="search_term", search_criteria="wool coat")
        abandoned = self._pending(input_mode="list_page")
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        scraper_tasks.redispatch_abandoned_pending()

        published.assert_called_once_with(abandoned.id, rescrape=False)

    @pytest.mark.django_db
    def test_an_abandoned_job_with_no_prior_redispatches_full(self, monkeypatch):
        """The job is the site's first — nothing to reuse, full build."""
        self._site()
        self._pending()
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        scraper_tasks.redispatch_abandoned_pending()

        assert published.call_args.kwargs == {"rescrape": False}

    # ── round-30 GAP 2: the two park resumers publish the same way ──────────
    # Both hold only ids and both re-dispatch a row whose reuse decision was
    # made by a lane that no longer exists — a provider-dead or browser-
    # parked job on a site with a production scraper was re-driven through
    # the 164m full build on every resume tick.

    def _parked(self, status: str, **kw) -> ScrapeJob:
        kw.setdefault("url", URL)
        kw.setdefault("input_mode", "url_list")
        kw.setdefault("target_fields", [])
        kw.setdefault("parked_seconds", 0)
        job = baker.make(ScrapeJob, status=status, celery_task_id="", **kw)
        # last_parked_at is a plain float epoch (not an ORM datetime) in both
        # resumers; age it past PROVIDER_REDRIVE_DELAY_S (1500s).
        ScrapeJob.objects.filter(pk=job.pk).update(
            last_parked_at=time.time() - 1800
        )
        return ScrapeJob.objects.get(pk=job.pk)

    def _healthy_browser(self, monkeypatch) -> None:
        import agents.tools.browser_http as browser_http

        monkeypatch.setattr(
            browser_http, "browser_service_strict_ok", lambda: True
        )

    @pytest.mark.django_db
    def test_a_provider_parked_reuse_eligible_job_resumes_with_reuse(
        self, monkeypatch
    ):
        self._site()
        self._prior(target_fields=["title", "price"])
        parked = self._parked(
            ScrapeJob.STATUS_PROVIDER_DEAD, target_fields=["title", "price"]
        )
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        result = scraper_tasks.resume_provider_dead_jobs()

        assert parked.id in result["jobs"], result
        published.assert_called_once_with(parked.id, rescrape=True)

    @pytest.mark.django_db
    def test_a_provider_parked_mismatched_job_resumes_full(self, monkeypatch):
        self._site()
        self._prior(target_fields=["title", "price"])
        parked = self._parked(
            ScrapeJob.STATUS_PROVIDER_DEAD,
            target_fields=["title", "price", "url"],
        )
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        scraper_tasks.resume_provider_dead_jobs()

        published.assert_called_once_with(parked.id, rescrape=False)

    @pytest.mark.django_db
    def test_a_browser_parked_reuse_eligible_job_resumes_with_reuse(
        self, monkeypatch
    ):
        self._healthy_browser(monkeypatch)
        self._site()
        self._prior(target_fields=["title", "price"])
        parked = self._parked(
            ScrapeJob.STATUS_BROWSER_UNAVAILABLE, target_fields=["title", "price"]
        )
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        result = scraper_tasks.resume_browser_unavailable_jobs()

        assert parked.id in result["jobs"], result
        published.assert_called_once_with(parked.id, rescrape=True)

    @pytest.mark.django_db
    def test_a_browser_parked_mismatched_job_resumes_full(self, monkeypatch):
        self._healthy_browser(monkeypatch)
        self._site()
        self._prior(input_mode="search_term", search_criteria="wool coat")
        parked = self._parked(ScrapeJob.STATUS_BROWSER_UNAVAILABLE, input_mode="list_page")
        published = self._capture(monkeypatch)

        from scraper import tasks as scraper_tasks

        scraper_tasks.resume_browser_unavailable_jobs()

        published.assert_called_once_with(parked.id, rescrape=False)
