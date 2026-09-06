"""[intake-ui] The Jobs & Saved library is a SHARED team view.

User request (2026-09-06): /intake/?view=jobs used to show each user only
their own jobs (admins saw everything). Instead, every user should see all
jobs across the team by default, with a filter to narrow by owner.

Behavior locked here:
- HTTP: intake_jobs returns ALL users' jobs for everyone; ``?user=<username>``
  narrows to one owner; a ``users`` roster (username + job_count) rides the
  response so the UI can build the filter without a second endpoint.
- PII: ``owner_username`` is now visible to everyone (the library needs it
  for the Owner column + filter); ``owner_email`` stays admin-only.
- UI: the Jobs tab carries a #job-user-filter select, populated from the
  response's ``users`` payload, and changing it refetches the library.

Run from repo root:  python3 -m pytest tests/test_intake_jobs_cross_user.py -v
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


def _job(owner, url, **kw):
    return models.ScrapeJob.objects.create(
        url=url,
        product_url=url,
        title=kw.pop("title", f"job {url}"),
        user=owner,
        **kw,
    )


@pytest.fixture
def alice(db):
    return User.objects.create_user("alice", password="x", email="alice@team.io")


@pytest.fixture
def bob(db):
    return User.objects.create_user("bob", password="x", email="bob@team.io")


@pytest.fixture
def admin(db):
    return User.objects.create_superuser("root", "root@team.io", password="x")


def _client(who):
    c = Client()
    c.force_login(who)
    return c


class TestCrossUserVisibility:
    def test_regular_user_sees_other_users_jobs(self, alice, bob):
        _job(alice, "https://a.example/p/1")
        r = _client(bob).get(reverse("intake_jobs"))
        assert r.status_code == 200
        ids = {j["id"] for j in r.json()["jobs"]}
        assert ids, "bob must see alice's job — the library is shared"

    def test_user_with_no_jobs_still_sees_everything(self, alice, bob):
        _job(alice, "https://a.example/p/1")
        assert not models.ScrapeJob.objects.filter(user=bob).exists()
        r = _client(bob).get(reverse("intake_jobs"))
        assert len(r.json()["jobs"]) == 1

    def test_owner_username_visible_to_regular_users(self, alice, bob):
        _job(alice, "https://a.example/p/1")
        row = _client(bob).get(reverse("intake_jobs")).json()["jobs"][0]
        assert row["owner_username"] == "alice"

    def test_owner_email_stays_admin_only(self, alice, bob, admin):
        _job(alice, "https://a.example/p/1")
        as_bob = _client(bob).get(reverse("intake_jobs")).json()["jobs"][0]
        as_admin = _client(admin).get(reverse("intake_jobs")).json()["jobs"][0]
        assert as_bob["owner_email"] is None, "emails must not leak cross-user"
        assert as_admin["owner_email"] == "alice@team.io"


class TestUserFilter:
    def test_user_param_narrows_to_that_owner(self, alice, bob):
        _job(alice, "https://a.example/p/1")
        _job(bob, "https://b.example/p/2")
        _job(bob, "https://b.example/p/3")
        r = _client(alice).get(reverse("intake_jobs"), {"user": "bob"})
        rows = r.json()["jobs"]
        assert {j["owner_username"] for j in rows} == {"bob"}
        assert len(rows) == 2

    def test_users_roster_in_response_even_when_filtered(self, alice, bob):
        _job(alice, "https://a.example/p/1")
        _job(bob, "https://b.example/p/2")
        d = _client(alice).get(reverse("intake_jobs"), {"user": "bob"}).json()
        roster = {u["username"]: u["job_count"] for u in d["users"]}
        assert roster == {"alice": 1, "bob": 1}, (
            "the roster must always cover everyone so the filter can switch back"
        )


class TestUiFilter:
    def _src(self):
        with open(INTAKE_HTML) as fh:
            return fh.read()

    def test_jobs_tab_has_user_filter_select(self):
        assert 'id="job-user-filter"' in self._src(), (
            "the Jobs tab needs a #job-user-filter select"
        )

    def test_refresh_library_sends_selected_user(self):
        m = re.search(r"function refreshLibrary\s*\([^)]*\)\s*\{", self._src())
        assert m, "refreshLibrary not found"
        body = self._src()[m.start(): m.start() + 1200]
        assert "job-user-filter" in body, "refreshLibrary must read the filter"
        assert "user=" in body and "encodeURIComponent" in body, (
            "the fetch must carry ?user=<selected>"
        )

    def test_refresh_library_populates_filter_from_roster(self):
        m = re.search(r"function refreshLibrary\s*\([^)]*\)\s*\{", self._src())
        body = self._src()[m.start(): m.start() + 1600]
        assert ".users" in body, "refreshLibrary must consume the users roster"

    def test_filter_change_refetches(self):
        src = self._src()
        assert "X('#job-user-filter')" in src, "filter select must be wired"
        # the LAST lookup is the event wiring (the earlier ones are inside
        # refreshLibrary) — it must listen for 'change' and refetch
        tail = src.rsplit("X('#job-user-filter')", 1)[-1][:600]
        assert "addEventListener('change'" in tail, (
            "changing the filter must be wired to a listener"
        )
        assert "refreshLibrary()" in tail, (
            "the change listener must refetch the library"
        )
