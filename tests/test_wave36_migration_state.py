"""[wave-36] Migration 0043 + ScrapeState declarations for the field-mapping contract.

Locks (docs/plans/wave36-field-mapping-plan.md §1a/§1b):
- migration 0043 adds ScrapeJob.field_mapping AND Site.field_mapping_cache
  (round-2 M6: Site has no free JSON field — output_schema is contract-bearing)
- ScrapeState declares resolved_fields / field_mapping / field_notes
  (undeclared keys are stripped from graph state, state.py:52-55) and the
  Fix-3 keys shortfall_remediation_count / prior_output_file /
  prior_product_count (round-2 B3: an undeclared counter reads 0 every pass →
  unbounded remediation loop)
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from scraper import models  # noqa: E402

MIGRATIONS_DIR = os.path.join(ROOT, "webapp", "scraper", "migrations")


class TestMigration0043:
    def test_migration_file_exists(self):
        path = os.path.join(MIGRATIONS_DIR, "0043_field_mapping.py")
        assert os.path.exists(path), "migration 0043_field_mapping.py missing"

    def test_migration_adds_both_columns(self):
        with open(os.path.join(MIGRATIONS_DIR, "0043_field_mapping.py")) as fh:
            src = fh.read()
        assert "ScrapeJob" in src and "field_mapping" in src
        assert "Site" in src and "field_mapping_cache" in src

    def test_model_fields_present(self):
        assert hasattr(models.ScrapeJob, "field_mapping")
        assert hasattr(models.Site, "field_mapping_cache")
        assert models.ScrapeJob._meta.get_field("field_mapping").null is True
        assert models.Site._meta.get_field("field_mapping_cache").null is True


class TestStateDeclarations:
    def test_mapping_keys_declared_in_scrape_state(self):
        with open(os.path.join(ROOT, "webapp", "agents", "state.py")) as fh:
            src = fh.read()
        for field in (
            "resolved_fields",
            "field_mapping",
            "field_notes",
        ):
            assert field in src, f"ScrapeState is missing {field}"

    def test_fix3_keys_declared_in_scrape_state(self):
        with open(os.path.join(ROOT, "webapp", "agents", "state.py")) as fh:
            src = fh.read()
        for field in (
            "shortfall_remediation_count",
            "prior_output_file",
            "prior_product_count",
        ):
            assert field in src, f"ScrapeState is missing {field}"

    def test_keys_survive_typing_check(self):
        from webapp.agents.state import ScrapeState

        annotations = ScrapeState.__annotations__
        for field in (
            "resolved_fields",
            "field_mapping",
            "field_notes",
            "shortfall_remediation_count",
            "prior_output_file",
            "prior_product_count",
        ):
            assert field in annotations, f"ScrapeState.__annotations__ lacks {field}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
