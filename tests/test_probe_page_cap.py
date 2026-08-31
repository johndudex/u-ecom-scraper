"""[job-316 citybeach] Probe page cap for the Phase-2 discovery gate.

What happened on the first live firing of ``_probe_phase1_discovery``: the
probe passed ``--discover-only`` with NO page bound, citybeach's full
discovery walk is 29 pages ≈ 8 min, and the probe's subprocess bound is
180s — ``timed out (job 316) — inconclusive``. The zero-yield gate went
blind exactly on the biggest catalogues (the ones it exists for).

Fix pinned here: the probe sets ``SCRAPER_DISCOVERY_MAX_PAGES`` (constant
``_PROBE_DISCOVERY_PAGE_CAP`` = 3) and ``src.listing_discovery`` honors it
as the default ``max_pages`` when the caller passes none. The probe's
verdict only needs "does this listing yield item URLs" — 3 pages answers
that in ~1 min. Template callers (execution) never set the env and are
unaffected; an explicit ``max_pages`` argument still wins over the env.

Run: docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.settings -e PYTHONPATH=/app:/app/webapp django sh -c "cd /app && pytest tests/test_probe_page_cap.py -q"
"""
from __future__ import annotations

import os
import sys

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.listing_discovery import discover_listing_urls  # noqa: E402


def _soup(page_no: int, per_page: int = 2) -> BeautifulSoup:
    links = "".join(
        f'<a href="https://site.test/p/{page_no}-{i}">item</a>'
        for i in range(per_page)
    )
    return BeautifulSoup(f"<html><body>{links}</body></html>", "html.parser")


def _fake_fetch(monkeypatch, pages: int, per_page: int = 2):
    """fetch_page stand-in: page N carries N*per_page unique URLs; pages past
    ``pages`` return the empty-page soft-block shape (200, zero links)."""
    calls: list[str] = []

    def fetch_page(url, min_tier=0):
        calls.append(url)
        page_no = len(calls)
        if page_no > pages:
            return BeautifulSoup("<html><body></body></html>", "html.parser"), 200
        return _soup(page_no, per_page), 200

    return fetch_page, calls


def _extract(soup):
    return [a["href"] for a in soup.find_all("a")]


class TestModuleEnvCap:
    def test_env_cap_stops_discovery_at_three_pages(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_DISCOVERY_MAX_PAGES", "3")
        fetch_page, calls = _fake_fetch(monkeypatch, pages=10)
        urls, meta = discover_listing_urls(
            fetch_page, ["https://site.test/c/x"], _extract,
        )
        assert len(calls) == 3
        assert meta["stop_reason"] == "max_pages_hit"
        assert meta["page_cap"] == 3
        assert len(urls) == 6  # 2 per page x 3 pages
        assert meta["discovered_urls"] == 6

    def test_no_env_runs_uncapped(self, monkeypatch):
        monkeypatch.delenv("SCRAPER_DISCOVERY_MAX_PAGES", raising=False)
        fetch_page, calls = _fake_fetch(monkeypatch, pages=4)
        urls, meta = discover_listing_urls(
            fetch_page, ["https://site.test/c/x"], _extract,
        )
        # 4 item pages then the empty-page shape -> short_page, cap untouched
        assert len(calls) == 5
        assert meta["stop_reason"] == "short_page"
        assert meta["page_cap"] is None
        assert len(urls) == 8

    def test_invalid_env_degrades_to_uncapped(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_DISCOVERY_MAX_PAGES", "abc")
        fetch_page, calls = _fake_fetch(monkeypatch, pages=2)
        _, meta = discover_listing_urls(
            fetch_page, ["https://site.test/c/x"], _extract,
        )
        assert meta["page_cap"] is None

    def test_explicit_max_pages_argument_wins_over_env(self, monkeypatch):
        """Template callers that pass max_pages explicitly must not inherit
        the probe's env cap."""
        monkeypatch.setenv("SCRAPER_DISCOVERY_MAX_PAGES", "3")
        fetch_page, calls = _fake_fetch(monkeypatch, pages=10)
        _, meta = discover_listing_urls(
            fetch_page, ["https://site.test/c/x"], _extract, max_pages=2,
        )
        assert len(calls) == 2
        assert meta["page_cap"] == 2
        assert meta["stop_reason"] == "max_pages_hit"

    def test_cap_verdict_is_positive_yield_not_exhaustion(self, monkeypatch):
        """The classifier contract the probe relies on: a capped probe with
        items is a PASS yield (discovered > 0) and 'max_pages_hit' is NOT an
        exhaustion-flavored reason — it must pass through normalization
        untouched (it describes the CAP, not the catalogue's end)."""
        from agents.graph import (
            _PROBE_EXHAUSTION_STOP_REASONS,
            _normalize_probe_stop_reason,
        )

        assert "max_pages_hit" not in _PROBE_EXHAUSTION_STOP_REASONS
        assert _normalize_probe_stop_reason("max_pages_hit") == "max_pages_hit"
        monkeypatch.setenv("SCRAPER_DISCOVERY_MAX_PAGES", "3")
        fetch_page, _ = _fake_fetch(monkeypatch, pages=10)
        urls, _ = discover_listing_urls(
            fetch_page, ["https://site.test/c/x"], _extract,
        )
        assert len(urls) > 0


class TestProbeWiring:
    """Static anchors: BOTH probe dispatch paths must hand the cap over."""

    def _src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def test_cap_constant_is_defined(self):
        assert '_PROBE_DISCOVERY_PAGE_CAP = "3"' in self._src()

    def test_local_subprocess_env_carries_the_cap(self):
        src = self._src()
        anchor = src.index("_probe_env = {**os.environ,")
        window = src[anchor:anchor + 220]
        assert '"SCRAPER_DISCOVERY_MAX_PAGES": _PROBE_DISCOVERY_PAGE_CAP' in window

    def test_browser_service_env_overrides_carry_the_cap(self):
        src = self._src()
        anchor = src.index('"env_overrides": {')
        window = src[anchor:anchor + 300]
        assert '"SCRAPER_DISCOVERY_MAX_PAGES": _PROBE_DISCOVERY_PAGE_CAP' in window

    def test_cap_constant_sits_with_the_probe_helpers(self):
        """Guard against the constant drifting away from the gate constants
        it composes with."""
        src = self._src()
        cap_at = src.index("_PROBE_DISCOVERY_PAGE_CAP = ")
        exh_at = src.index("_PROBE_EXHAUSTION_STOP_REASONS = ")
        assert abs(cap_at - exh_at) < 900


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
