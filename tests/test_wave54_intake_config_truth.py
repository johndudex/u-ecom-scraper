"""[wave-54] Intake config truth — the panel must describe the running job.

Job-1200 RCA (2026-10-04): a scope=all list_page job read as misconfigured
on /intake/?job=N. Four display defects, all display-only:

1. **Mode lied.** Every intake-born job is created ``full_extraction=False``
   (only the legacy /home form has that checkbox), so Details rendered
   "Sample" on jobs that extract everything. Delivery is driven by
   ``scope`` (rides the graph seed, tasks.py seed dict), not by the flag —
   wonderbly's 1082-row job carried the same "Sample" chip.

   Fix is deliberately DISPLAY-ONLY: ``sample_only = not full_extraction``
   gates field_confirmation's skip and the pre-execution-approval bypass
   (field_confirmation.py, pre_execution_approval.py), so flipping the flag
   at creation would activate HITL interrupts on every scope=all prod job.
   The flag stays untouched; ``ScrapeJob.is_full_extraction`` derives the
   truth for rendering (full_extraction OR scope-covers-everything, with
   the same narrowed predicate the graph uses at graph.py: scope in
   firstn/filter OR scope_value non-empty).

2. **Scope was never restored.** The form kept its "First 10" default on a
   deep link; j.scope/j.scope_value had zero handling in the JS.

3. **The listing box was empty.** The wave-41 one-shot cfg restore backfills
   textareas for url_list and search jobs only — a list_page job's listing
   URL (j.url) appeared nowhere in Current Configuration.

4. **No Scope row.** Details showed Mode/Records/Source but never the scope
   actually in force.

The model property is the single derivation; templates render it. The JS
mirrors the same predicate client-side (job_api has no computed field).
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
from scraper.models import ScrapeJob  # noqa: E402

_TPL = os.path.join(
    ROOT, "webapp", "scraper", "templates", "scraper"
)


def _read(name: str) -> str:
    with open(os.path.join(_TPL, name), encoding="utf-8") as fh:
        return fh.read()


# ─── the model property (the one derivation) ─────────────────────────────────


class TestIsFullExtractionProperty:
    def _job(self, **kw) -> ScrapeJob:
        kw.setdefault("url", "https://example.com/collections/jackets")
        return ScrapeJob.objects.create(**kw)

    @pytest.mark.django_db
    def test_scope_all_is_full(self):
        assert self._job(scope="all").is_full_extraction

    @pytest.mark.django_db
    def test_firstn_is_sample(self):
        job = self._job(scope="firstn", scope_value="10")
        assert not job.is_full_extraction

    @pytest.mark.django_db
    def test_filter_is_sample(self):
        job = self._job(scope="filter", scope_value="rating 4 and up")
        assert not job.is_full_extraction

    @pytest.mark.django_db
    def test_all_with_a_scope_value_is_sample(self):
        """Parity with the graph's narrowed predicate: scope_value non-empty
        means a narrowed run even under scope=all."""
        job = self._job(scope="all", scope_value="5")
        assert not job.is_full_extraction

    @pytest.mark.django_db
    def test_legacy_flag_wins(self):
        job = self._job(full_extraction=True, scope="firstn", scope_value="10")
        assert job.is_full_extraction

    @pytest.mark.django_db
    def test_blank_scope_is_sample(self):
        assert not self._job(scope="").is_full_extraction


# ─── server-rendered sites (badge + Mode row) ────────────────────────────────


class TemplateModeTruth:
    def test_render_gates_use_the_property(self):
        src = _read(self.template)
        assert "job.is_full_extraction" in src, (
            f"{self.template} must render truth via the property"
        )
        assert "{% if job.full_extraction %}" not in src, (
            f"{self.template} still gates Mode on the raw flag — intake-born "
            "rows are always False, so every full-scope job reads 'Sample'"
        )


class TestJobDetailModeTruth(TemplateModeTruth):
    template = "job_detail.html"


class TestJobListModeTruth(TemplateModeTruth):
    template = "job_list.html"


class TestSiteDetailModeTruth(TemplateModeTruth):
    template = "site_detail.html"


# ─── the intake page JS (client-side mirror of the same truth) ───────────────


class TestIntakeDetailsTruth:
    def test_mode_derives_from_scope(self):
        src = _read("intake.html")
        assert "j.scope==='all'" in src.replace(" ", ""), (
            "Mode must treat scope=all as Full, not read the raw flag alone"
        )
        mode_line = next(
            l for l in src.splitlines() if "det.push(['Mode'" in l
        )
        assert "_full" in mode_line, (
            "the Mode row must render the derived verdict"
        )

    def test_details_has_a_scope_row(self):
        assert "det.push(['Scope'" in _read("intake.html")


class TestIntakeScopeRestore:
    def test_scope_radios_restored_from_the_job(self):
        src = _read("intake.html")
        assert 'input[name="scope"][value="' in src, (
            "the scope radios keep the firstn default on a deep link — "
            "a scope=all job must read as All before any re-run"
        )

    def test_scope_value_restored(self):
        src = _read("intake.html")
        one_shot = self._one_shot_block(src)
        assert "j.scope_value" in one_shot

    def test_guarded_to_known_values(self):
        src = _read("intake.html")
        assert "['firstn','filter','all'].indexOf(j.scope)" in src, (
            "an unknown scope value must not check a non-existent radio"
        )

    def test_restore_is_one_shot(self):
        """The Details fetch polls; the restore must ride the same one-shot
        cfgPopulated block as the wave-41 restore or a poll would clobber
        user edits."""
        src = _read("intake.html")
        one_shot = self._one_shot_block(src)
        assert "j.scope" in one_shot

    @staticmethod
    def _one_shot_block(src: str) -> str:
        start = src.index("if(!intakeState.cfgPopulated){")
        end = src.index("intakeState.cfgPopulated=true", start)
        return src[start:end]


class TestIntakeListPageBackfill:
    def test_listing_textareas_backfilled_from_the_job_url(self):
        src = _read("intake.html")
        one_shot = TestIntakeScopeRestore._one_shot_block(src)
        assert "j.input_mode==='list_page'" in one_shot
        assert "nav-listing-text" in one_shot
        assert "cfg-listing-text" in one_shot

    def test_backfill_uses_the_job_url(self):
        src = _read("intake.html")
        one_shot = TestIntakeScopeRestore._one_shot_block(src)
        backfill = next(
            l for l in one_shot.splitlines()
            if "nav-listing-text" in l
        )
        assert "j.url" in backfill


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
