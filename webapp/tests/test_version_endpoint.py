"""[wave-22 D3] ``/api/version/`` — what does prod RUN, as a GET.

Four prod failures in a row (365/370/371/372) were diagnosed against a
version question that took a forensic agent to answer: the deploy fork is
not a configured remote, prod images carry no git metadata, and the only
ground truth was bounded indirectly. This endpoint makes "what does prod
run" forever answerable with one authenticated GET.

Contract:
- authed GET → 200 JSON with ``git_sha`` (RAILWAY_GIT_COMMIT_SHA, the same
  env celery.py already consumes; "local-dev" locally), ``wave`` (WAVE_TAG),
  ``branch``, ``built_at``;
- anonymous GET → redirect to login (same auth posture as /api/health/).
"""
from __future__ import annotations

import pytest
from django.urls import reverse


@pytest.fixture()
def authed_client(client, settings, db):
    from django.contrib.auth.models import User

    user, _ = User.objects.get_or_create(
        username="version-tester", defaults={"is_staff": True}
    )
    client.force_login(user)
    return client


class TestVersionEndpoint:
    def test_authed_get_returns_identity_json(self, authed_client, settings):
        settings.RAILWAY_GIT_COMMIT_SHA = "abc1234"
        settings.WAVE_TAG = "wave-22"
        settings.WAVE_BUILT_AT = "2026-09-06T03:00:00Z"
        resp = authed_client.get(reverse("version_api"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_sha"] == "abc1234"
        assert data["wave"] == "wave-22"
        assert data["built_at"] == "2026-09-06T03:00:00Z"
        assert "branch" in data

    def test_git_sha_defaults_to_local_dev(self, authed_client, settings):
        settings.RAILWAY_GIT_COMMIT_SHA = ""
        settings.WAVE_TAG = ""
        data = authed_client.get(reverse("version_api")).json()
        assert data["git_sha"] == "local-dev"
        assert data["wave"] == ""

    def test_anonymous_get_redirects_to_login(self, client, db, settings):
        # DebugAutoLoginMiddleware auto-authenticates under pytest when
        # DEBUG_AUTO_LOGIN is on (the wave-19 gotcha) — turn it off for this
        # test so the anonymous posture is actually exercised.
        settings.DEBUG_AUTO_LOGIN = False
        resp = client.get(reverse("version_api"))
        assert resp.status_code == 302
        assert "login" in resp["Location"]
