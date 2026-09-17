"""[wave-36 Fix 1b] Resolver wiring — persistence, identity lanes, guards.

- ``_resolve_field_mapping`` (celery seam, ONE place for intake UI / partner
  API / restart / update / CLI / auto-queue): hash reuse, Site cache skip,
  partner-API identity, kill-switch identity, no-op identity, field-notes
  rekey, persistence on the job row.
- ``_llm_field_map_adapter``: parses the small-LLM reply; {} on any failure.
- ``job_update`` chip edits clear the blob (F3 invalidation).
- ``writers.py`` partner target_fields guard (F7 — bare string poisoning).
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from src.field_mapping import content_hash_for  # noqa: E402


def _job(**over):
    base = dict(
        id=1, url="https://example.com/", page_type="product",
        target_fields=["product name", "rrp"],
        field_notes={"product name": "include brand"},
        field_mapping=None, created_via="ui",
    )
    base.update(over)
    calls = []

    def _save(update_fields=None, **kw):
        calls.append(list(update_fields or []))

    job = SimpleNamespace(**base)
    job.save = _save
    return job, calls


class TestResolveFieldMapping:
    def test_alias_resolution_persists_and_rekeys(self):
        from scraper.tasks import _resolve_field_mapping

        job, save_calls = _job()
        blob, resolved, notes = _resolve_field_mapping(job)
        assert resolved == ["title", "original_price"]
        assert blob["mapping"]["product name"]["source"] == "alias"
        assert blob["content_hash"] == content_hash_for(
            ["product name", "rrp"], "product"
        )
        assert notes == {"title": "include brand"}  # F4 rekey
        assert ["field_mapping"] in save_calls  # persisted on the job row

    def test_partner_api_identity(self):
        from scraper.tasks import _resolve_field_mapping

        job, save_calls = _job(created_via="api")
        blob, resolved, notes = _resolve_field_mapping(job)
        assert blob == {}
        assert resolved == ["product name", "rrp"]  # RAW — partner contract
        assert notes == {"product name": "include brand"}
        assert save_calls == []

    def test_kill_switch_identity(self, monkeypatch):
        from scraper.tasks import _resolve_field_mapping

        monkeypatch.setenv("FIELD_MAPPING_ENABLED", "0")
        job, save_calls = _job()
        blob, resolved, _ = _resolve_field_mapping(job)
        assert blob == {}
        assert resolved == ["product name", "rrp"]
        assert save_calls == []

    def test_no_chips_identity(self):
        from scraper.tasks import _resolve_field_mapping

        job, _ = _job(target_fields=[], field_notes={})
        blob, resolved, _ = _resolve_field_mapping(job)
        assert blob == {} and resolved == []

    def test_noop_contract_is_identity(self):
        # Chips that already ARE canonical resolve to themselves → no blob,
        # no persist (legacy byte-compat for canonical-chip jobs).
        from scraper.tasks import _resolve_field_mapping

        job, save_calls = _job(target_fields=["price", "title"],
                               field_notes={})
        blob, resolved, _ = _resolve_field_mapping(job)
        assert blob == {}
        assert set(resolved) == {"price", "title"}
        assert save_calls == []

    def test_hash_reuse_skips_resolution(self):
        from scraper.tasks import _resolve_field_mapping

        existing = {
            "mapping": {"product name": {"target": "title",
                                         "confidence": 0.97,
                                         "rationale": "alias",
                                         "source": "alias"},
                        "rrp": {"target": "original_price",
                                "confidence": 0.97,
                                "rationale": "alias",
                                "source": "alias"}},
            "resolved_fields": ["title", "original_price"],
            "content_hash": content_hash_for(["product name", "rrp"],
                                             "product"),
        }
        job, save_calls = _job(field_mapping=existing)
        blob, resolved, notes = _resolve_field_mapping(job)
        assert blob is existing
        assert resolved == ["title", "original_price"]
        assert notes == {"title": "include brand"}
        # Hash matched → blob already persisted; no redundant save.
        assert ["field_mapping"] not in save_calls

    def test_stale_hash_re_resolves(self):
        from scraper.tasks import _resolve_field_mapping

        stale = {"mapping": {}, "resolved_fields": ["x"],
                 "content_hash": "deadbeef"}
        job, save_calls = _job(field_mapping=stale)
        blob, resolved, _ = _resolve_field_mapping(job)
        assert blob["content_hash"] == content_hash_for(
            ["product name", "rrp"], "product"
        )
        assert resolved == ["title", "original_price"]


class TestLlmAdapter:
    def _fake_llm(self, content):
        class _Resp:
            pass

        r = _Resp()
        r.content = content
        return r

    def test_parses_json_reply(self, monkeypatch):
        from scraper import tasks as t

        captured = {}

        def fake_get_small_llm(temperature=0.3, timeout=None):
            captured["temp"] = temperature
            captured["timeout"] = timeout

            class _LLM:
                def invoke(self, msgs):
                    return self._resp

            llm = _LLM()
            llm._resp = self._fake_llm(json.dumps({
                "maker label": {"target": "brand", "confidence": 0.9,
                                "rationale": "domain term"},
            }))
            return llm

        monkeypatch.setattr("agents.llm.get_small_llm", fake_get_small_llm)
        out = t._llm_field_map_adapter(
            ["maker label"], "product", registry_block="- title\n- brand",
        )
        assert out["maker label"]["target"] == "brand"
        assert captured["temp"] == 0.0 and captured["timeout"] == 20

    def test_failure_returns_empty(self, monkeypatch):
        from scraper import tasks as t

        def boom(**kw):
            raise RuntimeError("down")

        monkeypatch.setattr("agents.llm.get_small_llm", boom)
        assert t._llm_field_map_adapter(["x"], "product") == {}

    def test_fenced_reply_stripped(self, monkeypatch):
        from scraper import tasks as t

        class _LLM:
            def invoke(self, msgs):
                r = SimpleNamespace()
                r.content = '```json\n{"a": {"target": "title", ' \
                            '"confidence": 0.8, "rationale": "r"}}\n```'
                return r

        monkeypatch.setattr(
            "agents.llm.get_small_llm", lambda **kw: _LLM()
        )
        assert t._llm_field_map_adapter(["a"], "product")["a"]["target"] == (
            "title"
        )


class TestGuards:
    def test_job_update_clears_blob_on_chip_edit(self):
        src = open(os.path.join(
            ROOT, "webapp", "scraper", "views.py"
        )).read()
        assert "job.field_mapping = None" in src

    def test_partner_target_fields_guard(self):
        src = open(os.path.join(
            ROOT, "webapp", "scraper", "api", "writers.py"
        )).read()
        assert 'target_fields must be an array' in src
        assert "[a-zA-Z0-9_ ]{1,64}" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
