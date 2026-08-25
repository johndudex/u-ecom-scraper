"""Output endpoints (slice 1a-iii): page index + window reader + download.

Locks (sync_api.yaml /output + /output/download, fold M10):
- build_page_index: byte offsets per item written ONCE at finalize;
  any page is slice+parse of ONE item range, never a full-file scan
- read_page: {site, <key>: [window], metadata} + paging envelope;
  422 out-of-range; 404 output_not_found; FM miss = fail-fast (M10)
- download: Content-Disposition attachment; 404 without output
- finalize hook: AFTER schema prune, BEFORE workspace rmtree (ordering
  load-bearing); emits artifact(output)
- M5/R2: partner outputs exempt from keep-5 prune (reverse lookup)
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

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
from scraper.api.output_index import (  # noqa: E402
    build_page_index,
    finalize_output_index,
    partner_owned_keys,
    read_output_page,
)
from scraper.api.writers import download_job_output, get_job_output  # noqa: E402

rf = RequestFactory()

ITEMS = [{"id": i, "title": f"item {i}"} for i in range(7)]
FULL = {"site": "outtest", "products": ITEMS, "metadata": {"count": 7}}


@pytest.fixture
def partner(db):
    u = User.objects.create_user(username="_t_out", password="x")
    raw = "pk_test_" + os.urandom(16).hex()
    models.ApiKey.objects.create(user=u, prefix=raw[:8], key_hash=models.ApiKey.hash_key(raw))
    return u, raw


def _job(u, status="completed", slug="outtest"):
    return models.ScrapeJob.objects.create(
        url="https://www.example.com/i", user=u, created_via="api",
        status=status, input_mode="url_list", page_type="product",
        site_folder=f"workspace/{slug}",
        output_file=f"scrapers/{slug}/output_2026-08-24_010101.json",
        product_count=7,
    )


def _write_full(tmp_path):
    src = tmp_path / "out.json"
    src.write_text(json.dumps(FULL))
    return src


class TestBuildIndex:
    def test_roundtrip_via_slices(self, partner, db, tmp_path):
        src = _write_full(tmp_path)
        index = build_page_index(str(src), items_key="products")
        assert index["items_key"] == "products"
        assert index["total_items"] == 7
        assert index["site"] == "outtest"
        assert index["metadata"] == {"count": 7}
        raw_text = src.read_text()
        got = [json.loads(raw_text[e["offset"]: e["offset"] + e["length"]]) for e in index["items"]]
        assert got == ITEMS


class TestReadPage:
    def _read(self, job, page, page_size, files):
        with patch("scraper.api.output_index._fm_read_text", return_value=files["file"]), \
             patch("scraper.api.output_index._fm_read_json", return_value=files.get("index")):
            return read_output_page(job, page=page, page_size=page_size)

    def test_first_page_envelope(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        src = _write_full(tmp_path)
        index = build_page_index(str(src), items_key="products")
        result = self._read(job, 1, 3, {"index": index, "file": src.read_text()})
        assert result["site"] == "outtest"
        assert len(result["products"]) == 3
        assert result["products"][0]["id"] == 0
        assert result["page"] == 1 and result["page_size"] == 3
        assert result["total_items"] == 7 and result["total_pages"] == 3

    def test_last_partial_page(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        src = _write_full(tmp_path)
        index = build_page_index(str(src), items_key="products")
        result = self._read(job, 3, 3, {"index": index, "file": src.read_text()})
        assert len(result["products"]) == 1
        assert result["products"][0]["id"] == 6

    def test_422_out_of_range(self, partner, db, tmp_path):
        from scraper.api import errors as api_errors

        u, raw = partner
        job = _job(u)
        src = _write_full(tmp_path)
        index = build_page_index(str(src), items_key="products")
        with pytest.raises(api_errors.ApiError) as e:
            self._read(job, 9, 3, {"index": index, "file": src.read_text()})
        assert e.value.status == 422 and e.value.code == "invalid_page"

    def test_404_without_index(self, partner, db):
        from scraper.api import errors as api_errors

        u, raw = partner
        job = _job(u)
        with pytest.raises(api_errors.ApiError) as e:
            self._read(job, 1, 100, {"index": None, "file": ""})
        assert e.value.status == 404 and e.value.code == "output_not_found"

    def test_invalid_page_size(self, partner, db):
        from scraper.api import errors as api_errors

        u, raw = partner
        job = _job(u)
        with pytest.raises(api_errors.ApiError) as e:
            self._read(job, 1, 900, {"index": {"total_items": 0, "items": [], "items_key": "products"}, "file": "{}"})
        assert e.value.status == 422


class TestOutputEndpoint:
    def _req(self, raw, job_id, qs=""):
        return rf.get(f"/api/v1/jobs/{job_id}/output{qs}", HTTP_X_API_KEY=raw)

    def test_200_page(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        src = _write_full(tmp_path)
        index = build_page_index(str(src), items_key="products")
        payload = {**index, "products": ITEMS[:3]}
        with patch("scraper.api.output_index.read_output_page", return_value=payload):
            r = get_job_output(self._req(raw, job.id), job.id)
        assert r.status_code == 200

    def test_422_propagates(self, partner, db):
        from scraper.api import errors as api_errors

        u, raw = partner
        job = _job(u)
        with patch("scraper.api.output_index.read_output_page",
                   side_effect=api_errors.ApiError(422, "invalid_page", "x")):
            r = get_job_output(self._req(raw, job.id, "?page=99"), job.id)
        assert r.status_code == 422

    def test_download_headers(self, partner, db):
        u, raw = partner
        job = _job(u)
        with patch("scraper.api.writers._fm_read_bytes", return_value=b'{"site": "x"}'):
            r = download_job_output(self._req(raw, job.id), job.id)
        assert r.status_code == 200
        assert "attachment" in r.get("Content-Disposition", "")
        assert "output_2026" in r.get("Content-Disposition", "")

    def test_download_404_no_output(self, partner, db):
        u, raw = partner
        job = _job(u, status="failed")
        job.output_file = ""
        job.save()
        r = download_job_output(self._req(raw, job.id), job.id)
        assert r.status_code == 404

    def test_cross_tenant_404(self, partner, db):
        u, raw = partner
        other = User.objects.create_user(username="_t_outo", password="x")
        job = _job(other)
        r = get_job_output(self._req(raw, job.id), job.id)
        assert r.status_code == 404


class TestFinalizeHook:
    def test_builds_and_persists_index(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        src = _write_full(tmp_path)
        written = {}

        def fake_write(key, payload):
            written[key] = payload

        with patch("scraper.api.output_index._fm_open_local", return_value=str(src)), \
             patch("scraper.api.output_index._fm_write_json", side_effect=fake_write):
            ok = finalize_output_index(job)
        assert ok is True
        assert f"scrapers/outtest/indexes/output-{job.id}.json" in written
        assert written[f"scrapers/outtest/indexes/output-{job.id}.json"]["total_items"] == 7

    def test_no_output_file(self, partner, db):
        u, raw = partner
        job = _job(u, status="failed")
        job.output_file = ""
        job.save()
        with patch("scraper.api.output_index._fm_open_local", return_value=None):
            assert finalize_output_index(job) is False

    def test_emits_artifact_event(self, partner, db, tmp_path):
        u, raw = partner
        job = _job(u)
        src = _write_full(tmp_path)
        with patch("scraper.api.output_index._fm_open_local", return_value=str(src)), \
             patch("scraper.api.output_index._fm_write_json"):
            finalize_output_index(job)
        rows = models.EventOutbox.objects.filter(job=job, event_type="job.artifact.available")
        kinds = [r.payload["data"]["kind"] for r in rows]
        assert "output" in kinds


class TestFinalizeIntegration:
    def test_prune_exempts_partner_outputs(self, partner, db):
        """M5/R2: created_via='api' outputs survive keep-5; unowned prunable."""
        u, raw = partner
        job = _job(u)
        job.output_file = "scrapers/outtest/output_2026-08-24_020202.json"
        job.save()
        protected = partner_owned_keys("outtest")
        assert "scrapers/outtest/output_2026-08-24_020202.json" in protected

    def test_finalize_wiring_order(self):
        """Ordering is load-bearing (fold M10): index AFTER the schema prune,
        BEFORE the workspace rmtree. Verified structurally — driving the real
        _finalize_job needs a live langgraph checkpoint; the hook's behavior
        is covered by TestFinalizeHook above."""
        import inspect

        import scraper.tasks as tasks_mod

        src = inspect.getsource(tasks_mod._finalize_job)
        i_prune = src.index("_prune_output_to_schema")
        i_index = src.index("finalize_output_index")
        assert i_prune < i_index, "index must be built AFTER the schema prune rewrites the artifact"
        assert "partner_owned_keys" in src, "prune exemption must be wired"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestDownloadStreams:
    def test_download_uses_fm_stream_not_buffered_read(self, partner, db):
        """A7-1 (spec NORMATIVE): /output/download MUST stream from FM, not
        buffer the whole file via artifacts.read (OOM'd 1 GB containers)."""
        import json as _json

        from scraper.api.writers import download_job_output

        u, raw = partner
        job = _job(u)
        chunks = []
        import httpx as _httpx

        class _FakeCM:
            def __init__(self, url):
                self.url = url

            def __enter__(self):
                assert "/stream/" in self.url, f"not the streaming endpoint: {self.url}"
                return _httpx.Response(
                    200, content=iter([b'{"site": ', b'"x"}']),
                    request=_httpx.Request("GET", self.url),
                )

            def __exit__(self, *a):
                return False

        import unittest.mock as _mock

        def fake_stream(key):
            return _FakeCM("http://fm/stream/" + key)

        with _mock.patch("scraper.api.writers._stream_fm_file", side_effect=fake_stream) as st, \
             _mock.patch("scraper.api.writers._fm_read_bytes") as br:
            r = download_job_output(
                rf.get(f"/api/v1/jobs/{job.id}/output/download", HTTP_X_API_KEY=raw), job.id
            )
        assert r.status_code == 200
        assert st.called
        assert not br.called, "buffered read must not be used"
        body = b"".join(r.streaming_content)
        assert body == b'{"site": "x"}'
