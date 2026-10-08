"""[wave-55 R8 / H5] The FM keep-newest-5 prune must not orphan a job's own
output, and the job page's fallback must never serve another run's file.

Audit 2026-10-05 (jobs 1186/1198): every run for a site shares one
``scrapers/{slug}/`` prefix in the File Master, so the keep-5 prune deletes a
job's ``output_file`` the moment 5 newer outputs land for that slug — and the
view fallback, which only floored its window at job start and never capped it,
then served a LATER job's 10-row artifact to a job whose own product_count
was 1.

Two bounded halves over one grace window:
1. the prune deletes only files a full grace OLDER than the kept-5 set, never
   the finalizing job's own file, and never a file a live or recently-terminal
   sibling still points at;
2. the fallback resolves only inside ``[started_at - grace, completed_at +
   grace]`` — nothing outside the job's own era — and reports
   ``output pruned by retention`` when the era holds no surviving file.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.http import Http404  # noqa: E402

import scraper.views as views  # noqa: E402
from scraper import tasks  # noqa: E402
from scraper.models import ScrapeJob  # noqa: E402

pytestmark = pytest.mark.django_db

SLUG = "acme-store"
UTC = dt_tz.utc


# ── name helpers (the shape the templates write: <date>_<clock>_<micros>_<pid>) ─


def _dt(hour, minute=0, second=0):
    return datetime(2026, 10, 1, hour, minute, second, tzinfo=UTC)


def _name(stamp, pid=7):
    return (
        f"output_{stamp:%Y-%m-%d_%H%M%S}_000001_{pid}.json"
    )


def _keys(*stamps):
    return [f"scrapers/{SLUG}/{_name(s)}" for s in stamps]


def _aware(dtobj):
    return dtobj if dtobj.tzinfo else dtobj.replace(tzinfo=UTC)


def dj_now():
    from django.utils import timezone

    return timezone.now()


# ── FM stub (both binding sites — see test_wave55_rescue_provenance._fm) ────


def _fm(monkeypatch, keys):
    """Stub the File Master client to `keys`; return the list of deleted keys."""
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
    deleted: list[str] = []
    monkeypatch.setattr(real, "list_keys", lambda prefix="": list(keys))
    monkeypatch.setattr(real, "exists", lambda key: key in keys)
    monkeypatch.setattr(real, "read_text", lambda key: "")
    monkeypatch.setattr(real, "read_json",
                        lambda key: (_ for _ in ()).throw(ValueError(key)))
    monkeypatch.setattr(real, "write", lambda key, data: len(data))
    monkeypatch.setattr(real, "delete", lambda key: deleted.append(key) or True)
    monkeypatch.setattr("scraper.api.output_index.partner_owned_keys",
                        lambda slug: set())
    return deleted


def _no_partner(monkeypatch):
    monkeypatch.setattr("scraper.api.output_index.partner_owned_keys",
                        lambda slug: set())


# ── 1. the prune plan ───────────────────────────────────────────────────────


class TestPrunableOutputKeys:
    """`_prunable_output_keys` — what the keep-5 prune may delete."""

    def test_seven_keys_leave_only_the_two_oldest_candidates(self):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        doomed = tasks._prunable_output_keys(keys)
        assert len(doomed) == 2
        assert _name(_dt(1)) in doomed[0]
        assert _name(_dt(2)) in doomed[1]

    def test_five_or_fewer_keys_prune_nothing(self):
        assert tasks._prunable_output_keys(_keys(*[_dt(h) for h in range(1, 6)])) == []
        assert tasks._prunable_output_keys([]) == []

    def test_the_finalizing_jobs_own_file_is_exempt_even_as_the_oldest(self):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        doomed = tasks._prunable_output_keys(
            keys, own_name=_keys(_dt(1))[0])
        assert len(doomed) == 1
        assert _name(_dt(2)) in doomed[0]
        assert _name(_dt(1)) not in " ".join(doomed)

    def test_own_file_is_matched_by_bare_name_too(self):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        doomed = tasks._prunable_output_keys(keys, own_name=_name(_dt(2)))
        assert all(_name(_dt(2)) not in k for k in doomed)

    def test_a_candidate_inside_the_kept_eras_grace_is_exempt(self):
        # kept oldest = 03:00:00; 02:58:30 is only 90s older — same era.
        keys = _keys(_dt(1), _dt(2, 58, 30), _dt(3), _dt(4), _dt(5),
                     _dt(6), _dt(7))
        doomed = tasks._prunable_output_keys(keys)
        assert len(doomed) == 1
        assert _name(_dt(1)) in doomed[0]

    def test_a_candidate_older_than_the_grace_is_still_prunable(self):
        # 02:50:00 is 600s older than the kept oldest — outside the era.
        keys = _keys(_dt(1), _dt(2, 50), _dt(3), _dt(4), _dt(5),
                     _dt(6), _dt(7))
        doomed = tasks._prunable_output_keys(keys)
        assert len(doomed) == 2

    def test_an_unparsable_name_keeps_the_bare_keep5_rule(self):
        # No name-epoch to attest freshness with — the era rule cannot protect
        # it, so it stays governed by the keep-5 order alone (legacy names).
        legacy = f"scrapers/{SLUG}/output_0legacy.json"
        keys = [legacy] + _keys(*[_dt(h) for h in range(1, 7)])
        doomed = tasks._prunable_output_keys(keys)
        assert legacy in doomed

    def test_an_era_protected_sibling_file_is_exempt(self):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        doomed = tasks._prunable_output_keys(
            keys, protected={_name(_dt(1))})
        assert len(doomed) == 1
        assert _name(_dt(2)) in doomed[0]

    def test_the_grace_window_is_meaningful(self):
        assert tasks.FM_PRUNE_GRACE_SECONDS == 300


class TestEraProtectedNames:
    """`_era_protected_output_names` — who still resolves through a file."""

    def _create(self, status, output_file, *, completed_at=None):
        return ScrapeJob.objects.create(
            url="https://acme-store.example/",
            site_folder=SLUG,
            status=status,
            output_file=output_file,
            completed_at=completed_at,
        )

    def test_a_non_terminal_siblings_output_is_protected(self):
        self._create(ScrapeJob.STATUS_RUNNING, _keys(_dt(3))[0])
        assert _name(_dt(3)) in tasks._era_protected_output_names(SLUG)

    def test_a_recently_terminal_job_is_protected(self):
        done = dj_now() - timedelta(seconds=60)
        self._create(ScrapeJob.STATUS_COMPLETED, _keys(_dt(3))[0],
                     completed_at=done)
        assert _name(_dt(3)) in tasks._era_protected_output_names(SLUG)

    def test_an_old_terminal_job_is_not_protected(self):
        done = dj_now() - timedelta(hours=2)
        self._create(ScrapeJob.STATUS_COMPLETED, _keys(_dt(3))[0],
                     completed_at=done)
        assert _name(_dt(3)) not in tasks._era_protected_output_names(SLUG)

    def test_the_prefixed_site_folder_spelling_matches_too(self):
        ScrapeJob.objects.create(
            url="https://acme-store.example/",
            site_folder=f"scrapers/{SLUG}",
            status=ScrapeJob.STATUS_RUNNING,
            output_file=_keys(_dt(3))[0],
        )
        assert _name(_dt(3)) in tasks._era_protected_output_names(SLUG)

    def test_a_job_on_another_site_is_never_protective_here(self):
        ScrapeJob.objects.create(
            url="https://other.example/",
            site_folder="other-store",
            status=ScrapeJob.STATUS_RUNNING,
            output_file=_keys(_dt(3))[0],
        )
        assert tasks._era_protected_output_names(SLUG) == set()


# ── 2. the wired prune ──────────────────────────────────────────────────────


class TestPruneKeepsTheJobsOwnOutput:
    def test_the_finalizing_jobs_own_file_is_never_deleted(self, monkeypatch):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        deleted = _fm(monkeypatch, keys)
        job = ScrapeJob(url="https://acme-store.example/",
                        status=ScrapeJob.STATUS_COMPLETED,
                        site_folder=SLUG, output_file=_keys(_dt(1))[0])
        tasks._prune_fm_outputs(job, SLUG)
        assert deleted == [_keys(_dt(2))[0]]

    def test_a_live_siblings_file_survives_the_prune(self, monkeypatch):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        deleted = _fm(monkeypatch, keys)
        ScrapeJob.objects.create(
            url="https://acme-store.example/", site_folder=SLUG,
            status=ScrapeJob.STATUS_RUNNING, output_file=_keys(_dt(2))[0])
        job = ScrapeJob(url="https://acme-store.example/",
                        status=ScrapeJob.STATUS_COMPLETED,
                        site_folder=SLUG, output_file=_keys(_dt(1))[0])
        tasks._prune_fm_outputs(job, SLUG)
        assert deleted == []

    def test_with_no_era_claims_the_keep5_rule_still_prunes(self, monkeypatch):
        keys = _keys(*[_dt(h) for h in range(1, 8)])
        deleted = _fm(monkeypatch, keys)
        job = ScrapeJob(url="https://acme-store.example/",
                        status=ScrapeJob.STATUS_COMPLETED,
                        site_folder=SLUG, output_file="")
        tasks._prune_fm_outputs(job, SLUG)
        assert sorted(deleted) == sorted(_keys(_dt(1), _dt(2)))

    def test_the_prune_block_is_the_helper_not_the_inline_loop(self):
        import inspect

        src = inspect.getsource(tasks._finalize_job)
        assert "_prune_fm_outputs(" in src
        assert 'startswith("output_")' not in src, (
            "the key scan moved into the helper — the inline loop must not survive"
        )


# ── 3. the view fallback window ─────────────────────────────────────────────


class TestFallbackWindow:
    def _job(self, *, started_at=None, completed_at=None, created_at=None,
             output_file=""):
        job = ScrapeJob(
            url="https://acme-store.example/",
            status=ScrapeJob.STATUS_COMPLETED,
            site_folder=SLUG,
            output_file=output_file,
            started_at=_aware(started_at) if started_at else None,
            completed_at=_aware(completed_at) if completed_at else None,
        )
        if created_at:
            job.created_at = _aware(created_at)
        return job

    def test_a_later_runs_file_is_never_served(self, monkeypatch):
        # Genuinely LATER (12:30 is 1200s past completed_at, past the +300s
        # grace); the dangling pointer is what makes the pruned note a true
        # claim — this job had an output and its era no longer holds it.
        _fm(monkeypatch, _keys(_dt(12, 30)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10),
                        output_file=_keys(_dt(12, 5))[0])
        key, fname, note = views._resolve_job_output(job)
        assert key is None and fname is None
        assert note == views.OUTPUT_PRUNED_NOTE

    def test_an_older_runs_file_is_never_served(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(9)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10))
        assert views._resolve_job_output(job)[0] is None

    def test_a_file_inside_the_jobs_own_era_is_served(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(12, 5)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10))
        key, fname, note = views._resolve_job_output(job)
        assert fname == _name(_dt(12, 5))
        assert note == ""

    def test_a_file_within_the_grace_before_the_start_is_served(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(11, 57)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10))
        assert views._resolve_job_output(job)[1] == _name(_dt(11, 57))

    def test_a_file_past_completed_at_plus_grace_is_refused(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(12, 15, 30)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10))
        assert views._resolve_job_output(job)[0] is None

    def test_a_file_inside_completed_at_plus_grace_is_served(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(12, 14)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10))
        assert views._resolve_job_output(job)[1] == _name(_dt(12, 14))

    def test_a_dangling_output_file_falls_back_inside_the_window_only(
            self, monkeypatch):
        # The prune took the job's own file; the newest survivor is another
        # run's — the fallback must not hand it over.
        _fm(monkeypatch, _keys(_dt(13)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10),
                        output_file=_keys(_dt(12, 5))[0])
        key, fname, note = views._resolve_job_output(job)
        assert key is None
        assert note == views.OUTPUT_PRUNED_NOTE

    def test_no_started_at_anchors_on_created_at(self, monkeypatch):
        # The old unbounded "newest in slug" arm is gone: with no start stamp
        # the era anchors on created_at and is still capped by completed_at.
        # No pointer, so the out-of-era refusal carries no pruned claim either.
        _fm(monkeypatch, _keys(_dt(13)))
        job = self._job(created_at=_dt(12), completed_at=_dt(12, 10))
        assert views._resolve_job_output(job) == (None, None, "")

    def test_the_job_detail_listing_uses_the_same_window(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(13)))
        names = [k.split("/")[-1] for k in views._fm_list(f"scrapers/{SLUG}/")]
        listed = views._outputs_in_run_window(names, _aware(_dt(12)),
                                             _aware(_dt(12, 10)))
        assert listed == []

    def test_no_anchor_lists_nothing_at_all(self, monkeypatch):
        # A job with no start stamp has no era — the old unbounded
        # "the site's newest outputs" arm is gone, not merely capped.
        _fm(monkeypatch, _keys(_dt(12, 5)))
        names = [k.split("/")[-1] for k in views._fm_list(f"scrapers/{SLUG}/")]
        assert views._outputs_in_run_window(names, None) == []

    def test_an_unparsable_name_is_never_in_window(self, monkeypatch):
        _fm(monkeypatch, [f"scrapers/{SLUG}/output_0legacy.json"])
        names = [k.split("/")[-1] for k in views._fm_list(f"scrapers/{SLUG}/")]
        assert views._outputs_in_run_window(names, _aware(_dt(12)),
                                            _aware(_dt(12, 10))) == []

    def test_the_job_detail_view_calls_the_window_helper(self):
        import inspect

        src = inspect.getsource(views.job_detail)
        assert "_outputs_in_run_window(" in src


# ── 4. the pruned marker ────────────────────────────────────────────────────


class TestPrunedMarkerShape:
    def _job(self, *, started_at=None, completed_at=None, created_at=None,
             output_file=""):
        job = ScrapeJob(
            url="https://acme-store.example/",
            status=ScrapeJob.STATUS_COMPLETED,
            site_folder=SLUG,
            output_file=output_file,
            started_at=_aware(started_at) if started_at else None,
            completed_at=_aware(completed_at) if completed_at else None,
        )
        if created_at:
            job.created_at = _aware(created_at)
        return job

    def test_a_job_that_never_had_an_output_is_not_told_it_was_pruned(
            self, monkeypatch):
        # The marker claims THIS job's output was pruned. A job whose
        # output_file was always empty has nothing of its own to prune — the
        # honest note is the empty one, not a false retention claim.
        _fm(monkeypatch, _keys(_dt(9), _dt(20)))
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10),
                        output_file="")
        assert views._resolve_job_output(job) == (None, None, "")
        assert views._job_output_preview(job) is None

    def test_the_intake_card_renders_the_pruned_note(self):
        tpl = (Path(__file__).resolve().parents[1]
               / "webapp/scraper/templates/scraper/intake.html").read_text()
        # A raw substring pin cannot tell live code from a comment: a branch
        # commented out (or relocated into a dead handler) must fail here.
        live = re.sub(r"/\*.*?\*/", "", tpl, flags=re.S)
        live = re.sub(r"(?m)^\s*//.*$", "", live)  # whole-line // only — never https://
        branch = re.search(r"if\(op\.note\)\{(.*?)\} else \{", live, re.S)
        assert branch, "the pruned-note branch must be live code in intake.html"
        body = branch.group(1)
        assert "X('#sample-json-view').textContent=pruned" in body, (
            "the note text must reach the Sample Output card")
        assert "intakeState.output_download_url=null" in body, (
            "a pruned output must not leave the download button armed")
        assert "intakeState.output_download_url=op.download_url" in live, (
            "the healthy arm must keep the real download url")

    def test_the_marker_is_the_named_shape(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(13)))
        # The pointer is the prune's own claim to check: this job HAD an
        # output and the era no longer holds it.
        job = self._job(started_at=_dt(12), completed_at=_dt(12, 10),
                        output_file=_keys(_dt(12, 5))[0])
        prev = views._job_output_preview(job)
        assert prev is not None
        assert prev["count"] is None
        assert prev["filename"] is None
        assert prev["download_url"] is None
        assert prev["note"] == "output pruned by retention"

    def test_the_note_is_a_module_constant_the_api_carries(self):
        assert views.OUTPUT_PRUNED_NOTE == "output pruned by retention"

    def test_a_resolved_output_still_previews_without_a_note(self, monkeypatch):
        payloads = {}

        deleted = _fm(monkeypatch, _keys(_dt(12, 5)))
        import src.artifacts as artifacts

        payloads[_keys(_dt(12, 5))[0]] = {"products": [{"title": "t",
                                                       "price": "$1",
                                                       "url": "u"}]}
        monkeypatch.setattr(artifacts, "read_json",
                            lambda key: payloads.get(key))
        job = ScrapeJob(
            url="https://acme-store.example/", page_type="product",
            status=ScrapeJob.STATUS_COMPLETED, site_folder=SLUG,
            started_at=_aware(_dt(12)), completed_at=_aware(_dt(12, 10)),
        )
        prev = views._job_output_preview(job)
        assert prev["count"] == 1
        assert "note" not in prev
        assert deleted == []


# ── 5. the output routes ────────────────────────────────────────────────────


class TestOutputRoutesAreEraBounded:
    """`job_output_view` / `job_output_download` read ANY ``output_*.json`` in
    the slug dir — a stale bookmark or hand-typed URL must not serve another
    run's bytes under this job's route (the UI's own links are era-bounded;
    these routes are the residual consumers)."""

    def _job(self, *, output_file=""):
        return ScrapeJob.objects.create(
            url="https://acme-store.example/",
            site_folder=SLUG,
            status=ScrapeJob.STATUS_FAILED,
            output_file=output_file,
            started_at=_aware(_dt(12)),
            completed_at=_aware(_dt(12, 10)),
        )

    def _req(self):
        from django.contrib.auth.models import User
        from django.test import RequestFactory

        req = RequestFactory().get("/")
        req.user = User.objects.create_user(
            "op", "op@example.com", "x", is_superuser=True)
        return req

    def test_a_later_runs_file_downloads_404(self, monkeypatch):
        # The bytes ARE there and readable, so the only thing that can refuse
        # them is the run-window guard — not a missing-file 404.
        _fm(monkeypatch, _keys(_dt(20)))
        import src.artifacts as artifacts

        monkeypatch.setattr(artifacts, "read",
                            lambda key, timeout=None: b'{"products": []}')
        job = self._job()
        with pytest.raises(Http404, match="not from this job"):
            views.job_output_download(self._req(), job.id, _name(_dt(20)))

    def test_the_view_route_refuses_another_run_too(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(20)))
        import src.artifacts as artifacts

        monkeypatch.setattr(artifacts, "read_json", lambda key: {"products": []})
        job = self._job()
        with pytest.raises(Http404, match="not from this job"):
            views.job_output_view(self._req(), job.id, _name(_dt(20)))

    def test_the_jobs_own_output_file_still_downloads(self, monkeypatch):
        _fm(monkeypatch, _keys(_dt(12, 5)))
        job = self._job(output_file=_keys(_dt(12, 5))[0])
        import src.artifacts as artifacts

        monkeypatch.setattr(artifacts, "read",
                            lambda key, timeout=None: b'{"products": []}')
        resp = views.job_output_download(self._req(), job.id, _name(_dt(12, 5)))
        assert resp.status_code == 200
