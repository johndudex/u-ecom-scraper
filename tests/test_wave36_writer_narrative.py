"""[wave-36 Fix 1c] Writer narrative + [FIELD-MAP] audit.

- ``_user_requirements_section`` shows the mapping ("user asked for
  'avaliability' → emit BOTH `availability` AND `avaliability`") instead of
  bare chips when a renaming mapping exists; identity jobs unchanged.
- ``_platform_distillation`` fields line shows resolved ∪ raw.
- ``_field_guidance_section`` is flag-gated (round-2 M7 — declaring
  ``field_notes`` would otherwise activate the dead W27-4 section fleet-wide
  in the same deploy).
- ``_persist_field_mapping`` emits the ``[FIELD-MAP]`` audit row.
"""

from __future__ import annotations

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

from webapp.agents.subagents import (  # noqa: E402
    _field_guidance_section,
    _mapping_narrative_lines,
    _user_requirements_section,
)

BLOB = {
    "mapping": {
        "product name": {"target": "title", "confidence": 0.97,
                         "rationale": "alias", "source": "alias"},
        "avaliability": {"target": "availability", "confidence": 0.97,
                         "rationale": "alias", "source": "alias"},
        "price": {"target": "price", "confidence": 1.0,
                  "rationale": "verbatim", "source": "verbatim"},
        "sku code": {"target": "CUSTOM", "confidence": 0.9,
                     "rationale": "domain term", "source": "llm",
                     "target_key": "sku_code"},
    },
    "resolved_fields": ["title", "availability", "price", "sku_code"],
    "content_hash": "h",
}


def _state(**over):
    st = {
        "target_fields": ["product name", "avaliability", "price",
                          "sku code"],
        "resolved_fields": ["title", "availability", "price", "sku_code"],
        "field_mapping": BLOB,
        "user_notes": "",
        "field_notes": {},
    }
    st.update(over)
    return st


class TestWriterNarrative:
    def test_mapping_lines_show_both_vocabularies(self):
        lines = "\n".join(_mapping_narrative_lines(_state()))
        assert "'product name' → `title`" in lines
        assert "BOTH `availability` AND `avaliability`" in lines
        assert "CUSTOM" in lines and "sku_code" in lines
        # Verbatim chips are never narrated (noise, not signal).
        assert "'price'" not in lines

    def test_identity_mapping_is_silent(self):
        st = _state(field_mapping={})
        assert _mapping_narrative_lines(st) == []
        st2 = _state(field_mapping={"mapping": {
            "price": {"target": "price", "source": "verbatim"},
        }})
        assert _mapping_narrative_lines(st2) == []

    def test_requirements_section_includes_mapping(self):
        section = _user_requirements_section(_state())
        assert "Fields requested by the user:" in section
        assert "'avaliability' → `availability`" in section
        assert "BOTH `availability` AND `avaliability`" in section

    def test_requirements_section_identity_unchanged(self):
        st = _state(field_mapping={})
        section = _user_requirements_section(st)
        assert "Fields requested by the user: product name" in section
        assert "→" not in section

    def test_requirements_section_advisory_when_no_fields(self):
        st = _state(target_fields=[], user_notes="just prices please")
        section = _user_requirements_section(st)
        assert "advisory" in section
        assert "just prices please" in section


class TestDistillationFieldsLine:
    def test_fields_line_uses_union(self):
        from webapp.agents.subagents import _platform_distillation

        state = _state()
        state["site_analysis"] = {}
        text = _platform_distillation(state)
        assert "Fields to extract:" in text
        # resolved names present (and raw verbatim keys kept alongside)
        assert "title" in text and "availability" in text


class TestGuidanceGating:
    def test_guidance_off_when_kill_switch(self, monkeypatch):
        monkeypatch.setenv("FIELD_MAPPING_ENABLED", "0")
        st = _state(field_notes={"title": "include brand"})
        assert _field_guidance_section(st) == ""

    def test_guidance_on_by_default(self, monkeypatch):
        monkeypatch.delenv("FIELD_MAPPING_ENABLED", raising=False)
        st = _state(field_notes={"title": "include brand"})
        section = _field_guidance_section(st)
        assert "### Field guidance" in section
        assert "title: include brand" in section


class TestAuditRow:
    def test_persist_emits_field_map_audit(self):
        from scraper.tasks import _persist_field_mapping

        saves = []

        job = SimpleNamespace(id=42, field_mapping=None, notes="old note")
        job.save = lambda update_fields=None, **kw: saves.append(
            list(update_fields or [])
        )
        _persist_field_mapping(job, BLOB)
        assert job.field_mapping is BLOB
        assert ["field_mapping"] in saves
        assert "[FIELD-MAP]" in (job.notes or "")
        assert "2 canonical / 1 custom / 1 verbatim" in (job.notes or "")
        assert "product name→title (alias)" in (job.notes or "")
        assert ["notes"] in saves

    def test_audit_survives_missing_notes_attr(self):
        from scraper.tasks import _persist_field_mapping

        job = SimpleNamespace(id=43, field_mapping=None)
        job.save = lambda update_fields=None, **kw: None
        _persist_field_mapping(job, BLOB)  # must not raise
        assert job.field_mapping is BLOB


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
