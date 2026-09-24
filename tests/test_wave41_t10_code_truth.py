"""[wave-41 T10] Code-truth fixes from the spec audits (T8/T9 found the
specs promising things the code did not deliver; this locks the code to the
committed contracts).

- validate-schema dual-accept: `schema` (object) XOR `schema_text` (string)
  — both/neither is a 400 `invalid_request` (sync_api.yaml requestBody).
- cancel returns the FULL JobStatus projection (sync_api.yaml: 200 is a
  $ref JobStatus), not the old 3-key summary — on both the fresh-cancel
  and the idempotent already-cancelled path.
- ws-token + the SSE header-key handshake pass through the same global
  per-key limiter as every api_view endpoint (spec: "Rate-limited with
  the global per-key limits").
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time

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
from scraper.api import readers, sse  # noqa: E402
from scraper.api.writers import cancel_job  # noqa: E402
from scraper.services import _get_redis  # noqa: E402

rf = RequestFactory()

SCHEMA_OBJ = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "price": {"type": "string"}},
    "required": ["title", "price"],
}


@pytest.fixture
def key_user(db):
    user = User.objects.create_user("_t_t10_" + secrets.token_hex(3), password="x")
    raw = "pk_" + secrets.token_hex(16)
    key = models.ApiKey.objects.create(
        user=user, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw)
    )
    return user, key, raw


def _post(path, body, raw):
    return rf.post(
        path, json.dumps(body), content_type="application/json", HTTP_X_API_KEY=raw
    )


# ── validate-schema dual-accept ─────────────────────────────────────────────


class TestValidateSchemaDualAccept:
    def test_object_form_still_works(self, key_user):
        _u, _k, raw = key_user
        body = json.loads(
            readers.validate_schema(_post("/x", {"schema": SCHEMA_OBJ}, raw)).content
        )
        assert body["valid"] is True
        assert set(body["derived_fields"]) == {"title", "price"}

    def test_text_form_is_accepted(self, key_user):
        """schema_text = the form the /intake UI posts (spec parity form)."""
        _u, _k, raw = key_user
        body = json.loads(
            readers.validate_schema(
                _post("/x", {"schema_text": json.dumps(SCHEMA_OBJ)}, raw)
            ).content
        )
        assert body["valid"] is True
        assert set(body["derived_fields"]) == {"title", "price"}

    def test_both_supplied_is_400(self, key_user):
        _u, _k, raw = key_user
        req = _post(
            "/x", {"schema": SCHEMA_OBJ, "schema_text": json.dumps(SCHEMA_OBJ)}, raw
        )
        resp = readers.validate_schema(req)
        assert resp.status_code == 400
        assert json.loads(resp.content)["code"] == "invalid_request"

    def test_neither_supplied_is_400(self, key_user):
        """Committed spec: 'neither is a 400' (was a 422 schema_invalid)."""
        _u, _k, raw = key_user
        resp = readers.validate_schema(_post("/x", {}, raw))
        assert resp.status_code == 400
        assert json.loads(resp.content)["code"] == "invalid_request"

    def test_wrong_typed_schema_stays_422(self, key_user):
        """A SUPPLIED-but-not-a-schema `schema` is a validation failure
        (422 schema_invalid), not a request-shape 400."""
        _u, _k, raw = key_user
        resp = readers.validate_schema(_post("/x", {"schema": 42}, raw))
        assert resp.status_code == 422
        assert json.loads(resp.content)["code"] == "schema_invalid"

    def test_text_not_valid_json_is_400(self, key_user):
        _u, _k, raw = key_user
        resp = readers.validate_schema(_post("/x", {"schema_text": "{not json"}, raw))
        assert resp.status_code == 400
        assert json.loads(resp.content)["code"] == "invalid_request"

    def test_text_not_a_string_is_400(self, key_user):
        _u, _k, raw = key_user
        resp = readers.validate_schema(_post("/x", {"schema_text": 42}, raw))
        assert resp.status_code == 400
        assert json.loads(resp.content)["code"] == "invalid_request"

    def test_text_with_invalid_schema_reports_200_valid_false(self, key_user):
        """Validation failure is a RESULT (200 valid:false), not a 422 —
        validate-schema is the pre-flight check, not a gate."""
        _u, _k, raw = key_user
        body = json.loads(
            readers.validate_schema(_post("/x", {"schema_text": "{}"}, raw)).content
        )
        assert body["valid"] is False
        assert body["issues"]


# ── cancel returns the full JobStatus projection ────────────────────────────


class TestCancelFullPayload:
    FULL_KEYS = {
        "job_id",
        "state",
        "internal_status",
        "url",
        "input_mode",
        "content_type",
        "title",
        "site_name",
        "rerun_of",
        "platform",
        "scraping_method",
        "current_phase",
        "phases",
        "sample_available",
        "output_available",
        "scraper_available",
        "item_count",
        "output_filename",
        "callback",
        "failure",
        "created_at",
        "started_at",
        "completed_at",
    }

    def _job(self, user, **over):
        defaults = dict(
            url="https://www.example.com/item",
            user=user,
            created_via="api",
            page_type="product",
            input_mode="url_list",
            status="running",
        )
        defaults.update(over)
        return models.ScrapeJob.objects.create(**defaults)

    def test_fresh_cancel_returns_full_status(self, key_user):
        user, _k, raw = key_user
        job = self._job(user)
        body = json.loads(cancel_job(_post("/x", {}, raw), job.id).content)
        assert set(body) == self.FULL_KEYS
        assert body["state"] == "failed"
        assert body["failure"]["code"] == "cancelled"
        assert body["internal_status"] == "cancelled"
        assert body["job_id"] == job.id

    def test_already_cancelled_is_idempotent_full_status(self, key_user):
        user, _k, raw = key_user
        job = self._job(user, status="cancelled")
        body = json.loads(cancel_job(_post("/x", {}, raw), job.id).content)
        assert set(body) == self.FULL_KEYS
        assert body["failure"]["code"] == "cancelled"

    def test_terminal_completed_is_409_with_state(self, key_user):
        user, _k, raw = key_user
        job = self._job(user, status="completed")
        resp = cancel_job(_post("/x", {}, raw), job.id)
        assert resp.status_code == 409
        body = json.loads(resp.content)
        assert body["code"] == "not_cancellable"
        assert body["details"]["state"] == "scraper_ready"


# ── ws-token / SSE handshake behind the global limiter ──────────────────────


class TestStreamAuthRateLimited:
    def _over_budget(self, key):
        """Simulate an already-bursting second window for this key."""
        conn = _get_redis()
        conn.set(f"rl:{key.key_hash[:16]}:b:{int(time.time())}", 30)

    def test_ws_token_rate_limited(self, key_user, db):
        user, key, raw = key_user
        self._over_budget(key)
        resp = sse.ws_token(_post("/x", {}, raw))
        assert resp.status_code == 429
        assert resp["Retry-After"]
        assert json.loads(resp.content)["code"] == "rate_limited"

    def test_ws_token_ok_when_under_limit(self, key_user, db):
        _user, _key, raw = key_user
        resp = sse.ws_token(_post("/x", {}, raw))
        assert resp.status_code == 201
        assert json.loads(resp.content)["connect_url"] is None

    def test_sse_header_handshake_rate_limited(self, key_user, db):
        user, key, raw = key_user
        job = models.ScrapeJob.objects.create(
            url="https://www.example.com/item",
            user=user,
            created_via="api",
            input_mode="url_list",
            status="running",
        )
        self._over_budget(key)
        req = rf.get(f"/api/v1/jobs/{job.id}/events", HTTP_X_API_KEY=raw)
        resp = sse.job_events_sse(req, job.id)
        assert resp.status_code == 429
        assert resp["Retry-After"]
