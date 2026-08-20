"""Spec-serving views: /docs/sync_api and /docs/async_api (login-gated).

Validates: auth gate, YAML content served, HTML preview mode, cross-links,
and that the spec FILES themselves stay structurally valid (YAML parses +
internal $refs resolve) — a broken edit to the specs fails here, not in
front of a partner.

Run: docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings \
     celery-worker bash -c "cd /app && python -m pytest tests/test_api_docs_views.py -v"
"""

from __future__ import annotations

import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from django.contrib.auth.models import AnonymousUser, User  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from scraper import views  # noqa: E402

rf = RequestFactory()


class TestSpecViewsAuth:
    def test_sync_anon_redirects_to_login(self):
        req = rf.get("/docs/sync_api")
        req.user = AnonymousUser()
        r = views.docs_sync_api(req)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"]

    def test_async_anon_redirects_to_login(self):
        req = rf.get("/docs/async_api")
        req.user = AnonymousUser()
        r = views.docs_async_api(req)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"]

    def test_unknown_spec_404(self, db):
        u = User(username="_t_spec"); u.save()
        try:
            req = rf.get("/docs/nope_api"); req.user = u
            from django.http import Http404

            try:
                views._serve_spec(req, "nope")
                raise AssertionError("expected Http404")
            except Http404:
                pass
        finally:
            u.delete()


class TestSpecViewsContent:
    def test_sync_serves_openapi_yaml(self, db):
        u = User(username="_t_spec2"); u.save()
        try:
            req = rf.get("/docs/sync_api"); req.user = u
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            assert "application/yaml" in r["Content-Type"]
            assert b"openapi: 3.1" in r.content
        finally:
            u.delete()

    def test_async_serves_asyncapi_yaml(self, db):
        u = User(username="_t_spec3"); u.save()
        try:
            req = rf.get("/docs/async_api"); req.user = u
            r = views.docs_async_api(req)
            assert r.status_code == 200
            assert b"asyncapi:" in r.content[:200]
        finally:
            u.delete()

    def test_html_preview_mode(self, db):
        u = User(username="_t_spec4"); u.save()
        try:
            req = rf.get("/docs/sync_api?format=html"); req.user = u
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            assert b"Sync API (OpenAPI 3.1)" in r.content
            assert b"/docs/async_api" in r.content  # sibling link
        finally:
            u.delete()


class TestSpecFilesStructural:
    """The specs are contracts — keep them valid YAML with resolving refs."""

    def _check(self, fname):
        path = os.path.join(ROOT, "docs", "specs", fname)
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        src = open(path, encoding="utf-8").read()
        refs = set(re.findall(r"\$ref:\s*['\"]?#/([^'\"\s]+)", src))

        def resolve(node, ref):
            cur = doc
            for part in ref.split("/"):
                if not isinstance(cur, dict) or part not in cur:
                    return False
                cur = cur[part]
            return True

        bad = [r for r in refs if not resolve(doc, r)]
        assert not bad, f"{fname} broken refs: {bad}"
        return doc

    def test_sync_spec_valid(self):
        doc = self._check("sync_api.yaml")
        assert doc["openapi"].startswith("3.1")
        assert "/api/v1/jobs" in doc["paths"]

    def test_async_spec_valid(self):
        doc = self._check("async_api.yaml")
        assert doc["asyncapi"].startswith("3.0")

    def test_cross_spec_vocabulary_locked(self):
        """The critique round's divergence fixes must hold: no orphaned names."""
        sync_src = open(os.path.join(ROOT, "docs/specs/sync_api.yaml"), encoding="utf-8").read()
        async_src = open(os.path.join(ROOT, "docs/specs/async_api.yaml"), encoding="utf-8").read()
        # tenancy oracle removed
        assert "job_not_owned" not in async_src
        # sibling filename correct
        assert "open_api.draft" not in async_src
        # one artifact model: no pre-signed URLs / token query auth
        assert "token=" not in async_src.replace("ws/v1/jobs?token=", "")
        # callback secret exists in sync (the HMAC path is implementable)
        assert "callback_secret" in sync_src
        # both carry the 4-state vocabulary
        for state in ("inprogress", "sample_ready", "scraper_ready", "failed"):
            assert state in sync_src and state in async_src, state


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
