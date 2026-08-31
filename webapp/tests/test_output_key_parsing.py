"""[job-71/76] FM output-key parsing + discovery-artifact exclusion in views.

``_resolve_job_output`` parsed run timestamps with a strict
``output_%Y-%m-%d_%H%M%S.json`` strptime — the unique-per-process names the
templates now write (``output_<ts>_<micros>_<pid>.json``, job-71) would have
been silently skipped, blanking the run-window output lookup. And the
discovery-artifact filter (job-76: "40 products" shown on a FAILED job) must
apply here, not just in the routing gate.
"""
from __future__ import annotations

from datetime import timezone

from scraper.views import _is_discovery_output_key, _output_key_ts


class TestOutputKeyTimestampParsing:
    def test_legacy_second_resolution_name(self):
        ts = _output_key_ts("output_2026-08-31_051402.json")
        assert ts is not None
        assert ts.year == 2026 and ts.hour == 5 and ts.minute == 14

    def test_new_microsecond_pid_name(self):
        ts = _output_key_ts("output_2026-08-31_051402_123456_4321.json")
        assert ts is not None
        assert ts.hour == 5 and ts.minute == 14 and ts.second == 2

    def test_non_output_name_is_none(self):
        assert _output_key_ts("site_analysis.json") is None
        assert _output_key_ts("input_urls.json") is None

    def test_garbage_body_is_none(self):
        assert _output_key_ts("output_not-a-date.json") is None

    def test_timezone_is_utc(self):
        assert _output_key_ts("output_2026-08-31_051402.json").tzinfo == timezone.utc


class TestDiscoveryKeyExclusion:
    def test_tagged_discovery_key_is_true(self, monkeypatch):
        import scraper.views as views

        monkeypatch.setattr(
            views,
            "_fm_read_json",
            lambda key: {"products": [{"url": "x"}],
                         "metadata": {"phase": "discovery"}},
        )
        assert _is_discovery_output_key("scrapers/acme-com/output_x.json") is True

    def test_extraction_key_is_false(self, monkeypatch):
        import scraper.views as views

        monkeypatch.setattr(
            views,
            "_fm_read_json",
            lambda key: {"products": [{"title": "t", "price": 1}],
                         "metadata": {"phase": "extraction"}},
        )
        assert _is_discovery_output_key("scrapers/acme-com/output_x.json") is False

    def test_untagged_key_is_false(self, monkeypatch):
        import scraper.views as views

        monkeypatch.setattr(views, "_fm_read_json", lambda key: {"products": []})
        assert _is_discovery_output_key("scrapers/acme-com/output_old.json") is False
