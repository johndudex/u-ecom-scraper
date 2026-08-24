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
    def test_sync_default_is_html(self, db):
        u = User(username="_t_spec2"); u.save()
        try:
            req = rf.get("/docs/sync_api"); req.user = u
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            assert b"Sync API (OpenAPI 3.1)" in r.content
        finally:
            u.delete()

    def test_sync_yaml_mode(self, db):
        u = User(username="_t_spec5"); u.save()
        try:
            req = rf.get("/docs/sync_api?format=yaml"); req.user = u
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            assert "application/yaml" in r["Content-Type"]
            assert b"openapi: 3.1" in r.content
            assert "attachment" in r["Content-Disposition"]
        finally:
            u.delete()

    def test_async_yaml_mode(self, db):
        u = User(username="_t_spec3"); u.save()
        try:
            req = rf.get("/docs/async_api?format=yaml"); req.user = u
            r = views.docs_async_api(req)
            assert r.status_code == 200
            assert b"asyncapi:" in r.content[:200]
        finally:
            u.delete()

    def test_html_preview_mode(self, db):
        u = User(username="_t_spec4"); u.save()
        try:
            req = rf.get("/docs/sync_api"); req.user = u  # html is the default now
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            assert b"Sync API (OpenAPI 3.1)" in r.content
            assert b"/docs/async_api" in r.content  # sibling link
        finally:
            u.delete()


class TestRendererPages:
    """The full docs pages: spec JSON endpoint + renderer wiring (fixed
    2026-08-23 after both renderers rendered blank — see view docstring)."""

    def test_sync_json_endpoint(self, db):
        u = User(username="_t_spec6"); u.save()
        try:
            req = rf.get("/docs/sync_api?format=json"); req.user = u
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            assert "json" in r["Content-Type"]
            import json as _json
            doc = _json.loads(r.content)
            assert doc["openapi"].startswith("3.1")
            assert len(doc["paths"]) >= 9
            # jsonSchemaDialect is stripped from rendered copies (swagger-ui
            # refuses endpoints when non-default) but NOT from raw yaml
            assert "jsonSchemaDialect" not in doc
        finally:
            u.delete()

    def test_sync_renderer_page_wiring(self, db):
        u = User(username="_t_spec7"); u.save()
        try:
            req = rf.get("/docs/sync_api"); req.user = u
            r = views.docs_sync_api(req)
            assert r.status_code == 200
            body = r.content.decode()
            # swagger bundle + url-fetch load (inline spec: constructor drops
            # operations in v5 builds; updateSpec races store init)
            assert "/docs/assets/swagger-ui-bundle.js" in body
            assert "url: '/docs/sync_api?format=json'" in body
            assert "unpkg.com" not in body  # zero third-party loads
            assert "spec:" not in body.split("SwaggerUIBundle")[1].split("});")[0]
        finally:
            u.delete()

    def test_async_renderer_page_wiring(self, db):
        u = User(username="_t_spec8"); u.save()
        try:
            req = rf.get("/docs/async_api"); req.user = u
            r = views.docs_async_api(req)
            assert r.status_code == 200
            body = r.content.decode()
            # v3 web component, vendored same-origin (1.4.x parser predates
            # AsyncAPI 3.0 docs and renders a blank shadow root)
            assert "/docs/assets/asyncapi-web-component.js" in body
            assert "asyncapi-component" in body
            assert "unpkg.com" not in body  # zero third-party loads
            # shadow-root @import 'assets/default.min.css' is page-relative
            # -> must resolve against /docs/assets/
            assert "cssImport=" not in body
        finally:
            u.delete()

    def test_doc_assets_served_same_origin(self, db):
        u = User(username="_t_spec9"); u.save()
        try:
            for name, ctype in (
                ("default.min.css", "text/css"),
                ("asyncapi-web-component.js", "javascript"),
                ("swagger-ui.css", "text/css"),
                ("swagger-ui-bundle.js", "javascript"),
            ):
                req = rf.get(f"/docs/assets/{name}"); req.user = u
                r = views._serve_doc_asset(req, name)
                assert r.status_code == 200, name
                assert ctype in r["Content-Type"], name
                assert len(r.content) > 10000, name  # real bundles, not stubs
            # whitelist only — traversal/unknown names 404
            from django.http import Http404
            for bad in ("../settings.py", "evil.js", "settings.py"):
                try:
                    views._serve_doc_asset(rf.get(f"/docs/assets/{bad}"), bad)
                    raise AssertionError(f"expected 404 for {bad}")
                except Http404:
                    pass
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

    def test_cross_spec_callbackstatus_parity(self):
        """R2-B2: one CallbackStatus shape across both specs."""
        import yaml as _y
        sync = _y.safe_load(open(os.path.join(ROOT, "docs/specs/sync_api.yaml"), encoding="utf-8"))
        asyncs = _y.safe_load(open(os.path.join(ROOT, "docs/specs/async_api.yaml"), encoding="utf-8"))
        s_cb = sync["components"]["schemas"]["CallbackStatus"]["properties"]
        a_cb = asyncs["components"]["schemas"]["CallbackStatus"]["properties"]
        assert set(s_cb) == set(a_cb), f"field drift: {set(s_cb) ^ set(a_cb)}"
        # last_failure: same shape in both, never a date-time (R2-B2 fix)
        assert s_cb["last_failure"]["type"] == a_cb["last_failure"]["type"]
        assert "string" in str(s_cb["last_failure"]["type"])

    def test_ws_token_documented_in_sync(self):
        """R2-B1: async depends on POST /api/v1/ws-token; it must live in sync."""
        src = open(os.path.join(ROOT, "docs/specs/sync_api.yaml"), encoding="utf-8").read()
        assert "/api/v1/ws-token:" in src
        assert "createWsToken" in src

    def test_secret_policy_unified(self):
        """One policy, one maxLength for the CALLBACK secret (raw, HMAC-signable).

        Scoped to the callback secret's schema descriptions — API keys are
        a different mechanism and legitimately stored hashed.
        """
        import yaml as _y
        sync = _y.safe_load(open(os.path.join(ROOT, "docs/specs/sync_api.yaml"), encoding="utf-8"))
        asyncs = _y.safe_load(open(os.path.join(ROOT, "docs/specs/async_api.yaml"), encoding="utf-8"))
        for sch in ("CreateJobRequest", "CallbackUpdate"):
            props = sync["components"]["schemas"][sch]["properties"]
            d = props["callback_secret"]["description"]
            _clean = d.replace("— hashed\nstorage is impossible", "").replace("hashed storage is impossible", "")
            assert "hashed" not in _clean, f"{sch} says hashed"
            assert props["callback_secret"].get("maxLength") == 256, f"{sch} maxLength"
        a_txt = str(asyncs["operations"]["deliverCallback"])
        assert "CANNOT be stored hashed" in a_txt or "stored raw" in a_txt

    def test_rate_limits_published(self):
        """M11 (decision 4): limits + 429 are in the spec, not folklore."""
        sync = open(os.path.join(ROOT, "docs/specs/sync_api.yaml"), encoding="utf-8").read()
        assert "x-rate-limits:" in sync
        assert "RateLimited:" in sync
        assert sync.count("429") >= 3
        assert "rate_limited" in sync

    def test_cross_spec_vocabulary_locked(self):
        """The critique round's divergence fixes must hold: no orphaned names."""
        sync_src = open(os.path.join(ROOT, "docs/specs/sync_api.yaml"), encoding="utf-8").read()
        async_src = open(os.path.join(ROOT, "docs/specs/async_api.yaml"), encoding="utf-8").read()
        # tenancy oracle removed
        assert "job_not_owned" not in async_src
        # sibling filename correct
        assert "open_api.draft" not in async_src
        # one artifact model: no pre-signed artifact URLs. The two AUTH
        # token= URLs are allowed (ws-token exchange for WSS + SSE browser
        # clients — single-use 300s tokens, not pre-signed artifacts).
        _auth_urls = async_src.replace("ws/v1/jobs?token=", "")
        _auth_urls = _auth_urls.replace("events?token=", "")
        assert "token=" not in _auth_urls
        # callback secret exists in sync (the HMAC path is implementable)
        assert "callback_secret" in sync_src
        # both carry the 4-state vocabulary
        for state in ("inprogress", "sample_ready", "scraper_ready", "failed"):
            assert state in sync_src and state in async_src, state


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
