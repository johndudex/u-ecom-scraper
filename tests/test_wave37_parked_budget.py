"""[wave-37 W37-3b] Cumulative parked-time budget.

The wave-16 B3 resumer re-dispatches parked rows whenever /health is ok —
a job can flap park→resume→park forever during repeated saturation windows
(prod 09-17/09-18: the same jobs parked through BOTH events), re-burning
discovery/tester wall-clock each round. Past PARKED_TIME_BUDGET_S of
CUMULATIVE park time the resumer finalizes the job honestly instead of
re-dispatching. Park episodes are measured accurately: park stamps the
episode start (last_parked_at), the resumer adds the finished episode to
parked_seconds when it flips the row. Parked rows themselves are only ever
touched via the claim-by-rowcount pattern.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from scraper.models import ScrapeJob  # noqa: E402
from scraper import tasks as st  # noqa: E402

pytestmark = pytest.mark.django_db

BUDGET = 7200


@pytest.fixture(autouse=True)
def _budget_env(monkeypatch):
    monkeypatch.setenv("PARKED_TIME_BUDGET_S", str(BUDGET))
    monkeypatch.setattr("agents.tools.browser_http.browser_service_strict_ok", lambda: True)


def _parked(seconds: int, episode_start: float | None = None) -> ScrapeJob:
    return ScrapeJob.objects.create(
        url="https://example.com/",
        site_name="example-com",
        status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
        parked_seconds=seconds,
        last_parked_at=episode_start,
    )


def test_over_budget_job_finalizes_not_redispatched(monkeypatch):
    job = _parked(seconds=BUDGET + 1)
    dispatched: list[int] = []
    monkeypatch.setattr(st, "dispatch_scrape_job", lambda jid: dispatched.append(jid))
    out = st.resume_browser_unavailable_jobs()
    assert dispatched == []
    assert job.id in {e["job_id"] for e in out.get("exhausted", [])}
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_FAILED
    assert "park budget exhausted" in (job.error_message or "").lower()


def test_under_budget_job_still_resumes(monkeypatch):
    job = _parked(seconds=60)
    dispatched: list[int] = []
    monkeypatch.setattr(st, "dispatch_scrape_job", lambda jid: dispatched.append(jid))
    out = st.resume_browser_unavailable_jobs()
    assert dispatched == [job.id]
    assert out["resumed"] == 1


def test_running_episode_counts_toward_budget(monkeypatch):
    # Parked 7000s historically + 301s into the CURRENT episode = over budget.
    job = _parked(seconds=7000, episode_start=time.time() - 301)
    dispatched: list[int] = []
    monkeypatch.setattr(st, "dispatch_scrape_job", lambda jid: dispatched.append(jid))
    out = st.resume_browser_unavailable_jobs()
    assert dispatched == []
    assert job.id in {e["job_id"] for e in out.get("exhausted", [])}


def test_under_budget_episode_accumulates_on_resume(monkeypatch):
    job = _parked(seconds=100, episode_start=time.time() - 60)
    monkeypatch.setattr(st, "dispatch_scrape_job", lambda jid: None)
    st.resume_browser_unavailable_jobs()
    job.refresh_from_db()
    assert 159 <= (job.parked_seconds or 0) <= 161  # 100 + ~60s episode
    assert job.last_parked_at is None  # episode closed


def test_park_stamps_episode_start_not_seconds(monkeypatch):
    import agents.tools.browser_http as bh

    monkeypatch.setattr(bh.time, "time", lambda: 1000.0)
    job = _job_park_flow()
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE
    assert job.last_parked_at == 1000.0
    assert (job.parked_seconds or 0) == 0  # episode not finished yet


def _job_park_flow() -> ScrapeJob:
    from agents.tools.browser_http import park_job_for_browser_service

    job = ScrapeJob.objects.create(
        url="https://example.com/2",
        site_name="example-com",
        status=ScrapeJob.STATUS_RUNNING,
        parked_seconds=0,
    )
    assert park_job_for_browser_service(job.id, "down again") is True
    return job
