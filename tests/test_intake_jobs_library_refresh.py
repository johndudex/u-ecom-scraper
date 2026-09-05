"""[intake-ui] Jobs & Saved library must be fresh and must not be re-trapped
by a stale ?job= deep-link.

User report (2026-09-05, pre-merge): from /intake, clicking "Jobs & Saved"
shows STALE job values, and because opening a job pushed `?job=N` into the
URL, a manual reload lands in the /intake/?job=N dashboard instead of the
library.

Root causes (both in intake.html + one in views.py):
1. `showLibrary()` only switches tabs + re-renders the cached ALL_JOBS /
   SAVED_EXTRACTORS arrays — `refreshLibrary()` (the /intake/jobs/ fetch)
   runs at boot and after save, never on library open. A running job's
   status/products freeze; jobs dispatched elsewhere never appear.
2. Opening a job does `history.pushState(null, '', '?job='+id)` and NOTHING
   strips it when the user returns to the library — so the next manual
   reload boots into `loadDeepLinkedJob()` (the old job) instead of the
   library.
3. `/intake/jobs/` (intake_jobs) returns a bare JsonResponse with no
   Cache-Control — an HTTP-layer cache between the browser and Django may
   serve a stale list even when the client does refetch.

Locks:
- intake_jobs response carries no-store Cache-Control (never_cache)
- intake_jobs reflects a job created after the first fetch
- showLibrary() body refetches the library on EVERY open
- showLibrary() body strips a stale ?job= deep-link from the URL
- both library entry points (#view-saved-link, #topbar-saved-link) route
  through showLibrary (so they inherit both fixes)
"""
from __future__ import annotations

import os
import re
import sys

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

INTAKE_HTML = os.path.join(
    ROOT, "webapp", "scraper", "templates", "scraper", "intake.html"
)


def _intake_src() -> str:
    with open(INTAKE_HTML) as fh:
        return fh.read()


def _function_body(src: str, name: str) -> str:
    """Extract a JS function body by brace-counting (no JS runtime in the
    test container — this is the repo's source-pin pattern, made structural:
    the assertion sees the REAL showLibrary body, not the whole file)."""
    m = re.search(r"function %s\s*\([^)]*\)\s*\{" % re.escape(name), src)
    assert m, f"function {name} not found in intake.html"
    i = m.end() - 1  # at the opening brace
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


@pytest.fixture
def user(db):
    return User.objects.create_user("intake_lib", password="x")


@pytest.fixture
def client_logged(user, db):
    c = Client()
    c.force_login(user)
    return c


class TestIntakeJobsHttpCache:
    def test_response_is_never_cached(self, client_logged):
        r = client_logged.get(reverse("intake_jobs"))
        assert r.status_code == 200
        cc = r.get("Cache-Control", "")
        assert "no-store" in cc, cc
        assert "no-cache" in cc, cc

    def test_refetch_sees_job_created_after_first_fetch(self, client_logged, user):
        r1 = client_logged.get(reverse("intake_jobs"))
        ids1 = {j["id"] for j in r1.json()["jobs"]}
        job = models.ScrapeJob.objects.create(
            url="https://lib.example/p/1",
            product_url="https://lib.example/p/1",
            title="library freshness pin",
            user=user,
        )
        r2 = client_logged.get(reverse("intake_jobs"))
        ids2 = {j["id"] for j in r2.json()["jobs"]}
        assert job.id in ids2 and job.id not in ids1


class TestShowLibraryBehavior:
    def test_show_library_refetches_library_on_open(self):
        body = _function_body(_intake_src(), "showLibrary")
        assert "refreshLibrary(" in body, (
            "showLibrary must refetch /intake/jobs/ on every open — rendering "
            "the cached ALL_JOBS freezes running jobs and hides new ones"
        )

    def test_show_library_url_is_reloadable_and_drops_stale_deeplink(self):
        # the library must be a real location: marked as ?view=jobs|saved with
        # the query (incl. any stale ?job=N) discarded — so F5 while browsing
        # the library lands back on Jobs & Saved, and a deep-linked ?job=N
        # can no longer trap a manual reload into the old job dashboard
        body = _function_body(_intake_src(), "showLibrary")
        assert "history.replaceState(" in body
        assert "?view=" in body
        assert "location.pathname" in body

    def test_boot_restores_library_view_before_deeplink(self):
        src = _intake_src()
        assert re.search(r"view=\(jobs\|saved\)", src), (
            "boot must recognize the ?view= library param"
        )
        m = re.search(
            r"if\s*\(\s*_libView\s*\)\s*\{[\s\S]*?showLibrary\([\s\S]*?\}"
            r"\s*else\s*\{\s*loadDeepLinkedJob\(\);\s*\}",
            src,
        )
        assert m, (
            "boot must showLibrary(?view=) when present and only otherwise "
            "fall back to the ?job= deep-link loader"
        )

    def test_library_entry_points_route_through_show_library(self):
        src = _intake_src()
        for link in ("#view-saved-link", "#topbar-saved-link"):
            m = re.search(
                r"X\('%s'\)\.addEventListener\('click',[\s\S]*?\}\);" % link, src
            )
            assert m, f"{link} click handler not found"
            assert "showLibrary(" in m.group(0), (
                f"{link} handler must route through showLibrary"
            )


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
