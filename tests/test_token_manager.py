"""Token-manager UI backend: list (blurred), create-once-reveal, revoke,
max-3-active; plus the WSS connect-recipe data (the ?token= flow).

The user asked: why must key management be a shell command when the user
is already logged into /intake? These tests lock the backend the modal
will call. RED first.
"""
from __future__ import annotations

import json
import os
import sys
import secrets as _secrets

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


def _user():
    return User.objects.create_user("_tm_" + _secrets.token_hex(3), password="x")


def _req(u, method="get", body=None):
    kw = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
    data = json.dumps(body) if body is not None else None
    fn = getattr(RequestFactory(), method)
    return fn("/intake/tokens/", data=data, content_type="application/json", **kw) if body is not None else fn("/intake/tokens/", **kw)


def _login(req, u):
    req.user = u
    return req


class TestList:
    def test_list_blurred_never_full(self, db):
        from scraper.api import tokens

        u = _user()
        raw = "pk_" + _secrets.token_hex(16)
        models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
        r = tokens.token_list(_login(_req(u), u))
        body = json.loads(r.content)
        assert body["keys"][0]["prefix"] == raw[:8]
        assert raw not in r.content.decode()  # full key never on list
        assert body["keys"][0]["blurred"] == raw[:8] + "••••••••••••"

    def test_list_shows_active_only_plus_counts(self, db):
        from scraper.api import tokens

        u = _user()
        from django.utils import timezone

        for i in range(2):
            models.ApiKey.objects.create(user=u, prefix=f"pk{i}aaa", key_hash=models.ApiKey.hash_key(f"k{i}"))
        revoked = models.ApiKey.objects.create(user=u, prefix="pk2bbb", key_hash=models.ApiKey.hash_key("k2"))
        revoked.revoked_at = timezone.now()
        revoked.save()
        r = tokens.token_list(_login(_req(u), u))
        body = json.loads(r.content)
        assert len(body["keys"]) == 2
        assert body["max_active"] == 3
        assert all(k["is_active"] for k in body["keys"])


class TestCreate:
    def test_create_returns_full_key_once(self, db):
        from scraper.api import tokens

        u = _user()
        r = tokens.token_create(_login(_req(u, "post", {}), u))
        assert r.status_code == 201
        body = json.loads(r.content)
        assert body["raw_key"].startswith("pk_")
        assert models.ApiKey.objects.filter(user=u, revoked_at__isnull=True).count() == 1
        assert models.ApiKey.hash_key(body["raw_key"]) == models.ApiKey.objects.get(user=u).key_hash

    def test_max_three_active(self, db):
        from scraper.api import tokens

        u = _user()
        for i in range(3):
            r = tokens.token_create(_login(_req(u, "post", {}), u))
            assert r.status_code == 201
        r4 = tokens.token_create(_login(_req(u, "post", {}), u))
        assert r4.status_code == 409
        assert json.loads(r4.content)["code"] == "token_limit_reached"

    def test_revoked_slots_free(self, db):
        from scraper.api import tokens

        u = _user()
        from django.utils import timezone

        for i in range(3):
            tokens.token_create(_login(_req(u, "post", {}), u))
        first = models.ApiKey.objects.filter(user=u).order_by("id").first()
        first.revoked_at = timezone.now()
        first.save()
        r = tokens.token_create(_login(_req(u, "post", {}), u))
        assert r.status_code == 201

    def test_superuser_refused(self, db):
        from scraper.api import tokens

        su = User.objects.create_user("_tm_su", password="x", is_superuser=True)
        r = tokens.token_create(_login(_req(su, "post", {}), su))
        assert r.status_code == 403


class TestRevoke:
    def test_revoke_own_key(self, db):
        from scraper.api import tokens

        u = _user()
        r = tokens.token_create(_login(_req(u, "post", {}), u))
        kid = json.loads(r.content)["id"]
        rr = tokens.token_revoke(_login(_req(u, "post", {"id": kid}), u), kid)
        assert rr.status_code == 200
        assert models.ApiKey.objects.get(pk=kid).revoked_at is not None

    def test_cannot_revoke_other_users_key(self, db):
        from scraper.api import tokens

        u, other = _user(), _user()
        r = tokens.token_create(_login(_req(u, "post", {}), u))
        kid = json.loads(r.content)["id"]
        rr = tokens.token_revoke(_login(_req(other, "post", {"id": kid}), other), kid)
        assert rr.status_code == 404
        assert models.ApiKey.objects.get(pk=kid).revoked_at is None


class TestConnectRecipe:
    def test_recipe_includes_wss_token_flow(self, db):
        from scraper.api import tokens

        u = _user()
        r = tokens.token_list(_login(_req(u), u))
        body = json.loads(r.content)
        rc = body["connect"]["wss"]
        assert "ws-token" in rc["step1"]
        assert "X-API-Key" in rc["step1"]
        assert "token" in rc["step2"] and "/ws/v1/jobs" in rc["step2"]
        assert rc["step2"].count("300") >= 1  # TTL stated


class TestSuperuserMessage:
    def test_refusal_explains_the_path(self, db):
        from scraper.api import tokens

        su = User.objects.create_user("_tm_su2", password="x", is_superuser=True)
        req = _req(su)
        req.user = su
        r = tokens.token_list(req)
        body = json.loads(r.content)
        assert body.get("operator_hint") is True
        assert "Add" in body["message"] and "non-admin" in body["message"] or "normal" in body["message"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


