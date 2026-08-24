"""Output window cache (fold M10 residual): bounded page reads on large outputs.

Locks:
- first read of a file pulls it into a bounded LRU; the SAME page and any
  other page of the SAME file hit the cache (repeat pages free)
- distinct files get distinct entries; the cache is bounded (N files, M
  bytes total) — evicting the least-recently-used file
- read_output_page consults the cache (no per-request full-file refetch)
- concurrent key set on different jobs does not evict mid-read
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402

from scraper import models  # noqa: E402
from scraper.api.output_index import build_page_index, read_output_page  # noqa: E402
from scraper.api.window_cache import WindowCache  # noqa: E402

ITEMS = [{"id": i, "title": f"item {i}"} for i in range(7)]
FULL = {"site": "wc", "products": ITEMS, "metadata": {"count": 7}}


class TestWindowCache:
    def test_first_miss_then_hits(self):
        c = WindowCache(max_files=2, max_bytes=1_000_000)
        fetches = []

        def fetch(key, size):
            fetches.append(key)
            return FULL_JSON

        FULL_JSON = json.dumps(FULL)
        assert c.get("scrapers/a/out.json", len(FULL_JSON), fetch) == FULL_JSON
        assert c.get("scrapers/a/out.json", len(FULL_JSON), fetch) == FULL_JSON
        assert fetches == ["scrapers/a/out.json"]  # second read cached

    def test_different_pages_same_file_one_fetch(self):
        c = WindowCache(max_files=2, max_bytes=1_000_000)
        fetches = []

        FULL_JSON = json.dumps(FULL)

        def fetch(key, size):
            fetches.append(key)
            return FULL_JSON

        c.get("scrapers/a/out.json", len(FULL_JSON), fetch)
        c.get("scrapers/a/out.json", len(FULL_JSON), fetch)
        c.get("scrapers/a/out.json", len(FULL_JSON), fetch)
        assert len(fetches) == 1

    def test_lru_eviction(self):
        c = WindowCache(max_files=2, max_bytes=1_000_000)
        FULL_JSON = json.dumps(FULL)
        fetches = []

        def fetch(key, size):
            fetches.append(key)
            return FULL_JSON.replace('"wc"', f'"{key[-6:]}"')

        c.get("f1", len(FULL_JSON), fetch)
        c.get("f2", len(FULL_JSON), fetch)
        c.get("f1", len(FULL_JSON), fetch)  # f1 now most-recent
        c.get("f3", len(FULL_JSON), fetch)  # evicts f2 (LRU)
        c.get("f2", len(FULL_JSON), fetch)  # refetch (was evicted)
        assert fetches == ["f1", "f2", "f3", "f2"]

    def test_size_key_mismatch_invalidates(self):
        """The file changed under us (schema prune rewrite + re-index) — a
        size change must NOT serve stale bytes."""
        c = WindowCache(max_files=2, max_bytes=1_000_000)
        v1 = json.dumps(FULL)
        v2 = json.dumps({**FULL, "metadata": {"count": 99}})
        calls = {"n": 0}

        def fetch(key, size):
            calls["n"] += 1
            return v1 if calls["n"] == 1 else v2

        assert c.get("k", len(v1), fetch) == v1
        assert c.get("k", len(v2), fetch) == v2  # new size → refetch
        assert calls["n"] == 2

    def test_oversized_file_not_cached(self):
        """A file bigger than the whole cache budget skips the cache (no
        thrash) but still serves the read."""
        c = WindowCache(max_files=4, max_bytes=100)
        v = json.dumps(FULL)
        fetches = []

        def fetch(key, size):
            fetches.append(key)
            return v

        assert c.get("big", len(v), fetch) == v
        assert c.get("big", len(v), fetch) == v
        assert len(fetches) == 2  # never cached

    def test_total_bytes_bounded(self):
        c = WindowCache(max_files=10, max_bytes=1_000)
        v = "x" * 600
        c.get("a", 600, lambda k, s: v)
        c.get("b", 600, lambda k, s: v)
        # a+b = 1200 > 1000 → oldest evicted; cache holds ≤ budget
        assert c.total_bytes <= 1000


class TestReaderUsesCache:
    def test_read_output_page_hits_cache(self, db, tmp_path):
        u = User.objects.create_user("_t_wc", password="x")
        job = models.ScrapeJob.objects.create(
            url="https://e.com/i", user=u, created_via="api", status="completed",
            input_mode="url_list", page_type="product",
            site_folder="workspace/wc", output_file="scrapers/wc/out.json",
            product_count=7,
        )
        src = tmp_path / "o.json"
        src.write_text(json.dumps(FULL))
        index = build_page_index(str(src), items_key="products")

        fetches = []

        def fetch(key, size):
            fetches.append(key)
            return src.read_text()

        with patch("scraper.api.output_index._fm_read_json", return_value=index), \
             patch("scraper.api.window_cache.window_fetch", side_effect=fetch):
            r1 = read_output_page(job, page=1, page_size=3)
            r2 = read_output_page(job, page=2, page_size=3)
        assert r1["products"][0]["id"] == 0
        assert r2["products"][0]["id"] == 3
        assert len(fetches) == 1  # page 2 served from cache


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestIndexCarriesSize:
    def test_build_page_index_records_source_bytes(self, tmp_path):
        """The cache keys on (key, size); the index must carry the source
        size or a rewritten file would serve stale cached pages."""
        src = tmp_path / "o.json"
        src.write_text(json.dumps(FULL))
        index = build_page_index(str(src), items_key="products")
        assert index["source_bytes"] == len(src.read_text().encode())
