"""Job-311 (pillowtalk e2e round 2) regressions.

What happened (job 311): the navigator misclassified BigCommerce's SSR
``?page=N`` search pagination as ``load_more``, so the playwright draft's
discovery (``config_for_load_more``) capped at ONE page (found=12 of ~1194).
Then a ~3-minute site-side soft-block window made the tester's runs render 0
items (``empty_render``) — a window that had ALREADY passed by the time the
strategy decision ran (the post-mortem probe found 12 URLs). The classifier
read the transient as "wrong strategy", and the up-only escalation ladder
declared exhaustion with only playwright tried → honest cleanup FAILED.

Fixes pinned here:
- F-A (src/discovery.py): before a same-page-only discovery declares
  NO_NEW_ITEMS having never left page 1, run ONE bounded, verified page-param
  probe (the existing alias ladder / configured param). Adoption requires
  ≥1 genuinely new URL, so healthy exhausted runs are unchanged.
- F-B (src/discovery.py): one bounded re-fetch when the FIRST render yields
  0 items — rides out short block windows before declaring empty_render.
- F-C (graph.py + route_after_testing.py): a newest-output ``empty_render``
  with an earlier same-phase output carrying items is attached as TRANSIENT
  evidence and classified as a same-draft re-test ("scraper" action) — the
  strategy rung is NOT recorded tried, so no strategy switch fires.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job311_transient_discovery.py -q
"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional
from urllib.parse import parse_qsl, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

from src.discovery import (  # noqa: E402
    DiscoveryConfig,
    StopReason,
    _END_OF_RUN_PROBES,
    discover_item_urls,
)

PAGE_SIZE = 12
TOTAL = 108  # pillowtalk search.php shape: 12/page over many ?page=N pages


def _items_for(url: str, honored: Optional[str]) -> list[str]:
    """SSR listing items for ``url`` — only ``honored`` paginates (1-indexed)."""
    qs = dict(parse_qsl(urlparse(url).query))
    start = 0
    if honored and honored in qs:
        start = (int(qs[honored]) - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, TOTAL)
    return [f"https://x.test/item/{i:04d}" for i in range(start, end)]


class _Resp:
    def __init__(self, status: int):
        self.status = status


class _FakePage:
    """PageLike double: SSR listing with ONE honored page param (or none).

    ``blocked`` simulates the job-311 soft-block window: while set, every
    render returns 0 items (the page LOADS — goto 200 — but no product links
    render). Flips off when ``wait_for_timeout`` sees the discovery retry wait.
    """

    def __init__(self, start_url: str, honored: Optional[str] = None):
        self._url = start_url
        self.honored = honored
        self.goto_urls: list[str] = []
        self.blocked = False
        self.retry_waits_seen: list[int] = []

    @property
    def url(self) -> str:
        return self._url

    @property
    def current_items(self) -> list[str]:
        if self.blocked:
            return []
        return _items_for(self._url, self.honored)

    def goto(self, url: str, timeout: int = 0) -> _Resp:
        self.goto_urls.append(url)
        self._url = url
        return _Resp(200)

    def wait_for_load_state(self, state: str) -> None:
        pass

    def evaluate(self, js, *args):
        return list(self.current_items)

    def query_selector(self, selector):
        return None  # no load-more button, ever

    def wait_for_timeout(self, ms):
        if ms >= 5000:
            self.retry_waits_seen.append(ms)
            self.blocked = False  # the block window passes during the wait


def _extract(page) -> list[str]:
    return page.current_items


def _cfg(**overrides) -> DiscoveryConfig:
    """load_more config (the playwright template default) with sleeps stripped."""
    base = dict(
        strategies=("load_more", "infinite_scroll"),
        page_settle_after_nav_s=0.0,
        click_wait_ms=0,
        scroll_wait_ms=0,
        max_pages=25,
        min_initial_links=1,
        initial_render_polls=3,
        empty_render_retry_wait_ms=8000,
    )
    base.update(overrides)
    return DiscoveryConfig(**base)


# ═══════════════════════════════════════════════════════════════════════════════
# F-A — end-of-run page-param probe
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndOfRunPageParamProbe:
    def test_load_more_config_adopts_page_param(self):
        """The job-311 shape: load_more cfg on an SSR ?page=N site discovers
        past page 1 via the verified end-of-run probe."""
        page = _FakePage("https://x.test/search.php?search_query=pillows", honored="page")
        result = discover_item_urls(page, page._url, _extract, _cfg())
        assert len(result.urls) == TOTAL, (
            f"discovery stopped at {len(result.urls)} of {TOTAL} — the "
            "end-of-run page-param probe did not adopt"
        )
        assert result.param_used == "page"
        assert result.stop_reason == StopReason.NO_NEW_ITEMS.value

    def test_configured_param_is_probed_at_terminal(self):
        """A declared-but-unlisted page_param is probed at the terminal too."""
        page = _FakePage("https://x.test/list", honored="p")
        cfg = _cfg(page_param_name="p")
        result = discover_item_urls(page, page._url, _extract, cfg)
        assert len(result.urls) == TOTAL
        assert result.param_used == "p"

    def test_no_param_site_stops_as_before(self):
        """Healthy exhausted run: nothing adopted, one bounded probe round."""
        page = _FakePage("https://x.test/list", honored=None)
        result = discover_item_urls(page, page._url, _extract, _cfg())
        assert len(result.urls) == PAGE_SIZE
        assert result.stop_reason == StopReason.NO_NEW_ITEMS.value
        # The ladder probed at most one round of candidates, then memoized.
        alias_gotos = [
            u for u in page.goto_urls[1:]  # [0] is the initial goto
            if any(a in dict(parse_qsl(urlparse(u).query))
                   for a, _m in _END_OF_RUN_PROBES)
        ]
        assert len(alias_gotos) <= len(_END_OF_RUN_PROBES)


# ═══════════════════════════════════════════════════════════════════════════════
# F-B — single bounded empty-render retry
# ═══════════════════════════════════════════════════════════════════════════════


class TestEmptyRenderRetry:
    def test_transient_block_recovers(self):
        page = _FakePage("https://x.test/list", honored="page")
        page.blocked = True  # first render: loaded but 0 items (block window)
        result = discover_item_urls(page, page._url, _extract, _cfg())
        assert page.retry_waits_seen == [8000]
        assert len(result.urls) > 0
        assert result.stop_reason != StopReason.EMPTY_RENDER.value

    def test_persistent_block_still_declares_empty_render(self):
        class _StuckPage(_FakePage):
            def wait_for_timeout(self, ms):
                self.retry_waits_seen.append(ms)  # window never passes

        page = _StuckPage("https://x.test/list", honored="page")
        page.blocked = True
        result = discover_item_urls(page, page._url, _extract, _cfg())
        assert result.urls == []
        assert result.stop_reason == StopReason.EMPTY_RENDER.value
        assert page.retry_waits_seen, "retry wait never fired"

    def test_retry_disabled_via_cfg(self):
        page = _FakePage("https://x.test/list", honored="page")
        page.blocked = True
        result = discover_item_urls(
            page, page._url, _extract, _cfg(empty_render_retry_wait_ms=0)
        )
        assert result.urls == []
        assert result.stop_reason == StopReason.EMPTY_RENDER.value
        assert page.retry_waits_seen == []


# ═══════════════════════════════════════════════════════════════════════════════
# F-C — transient classification (no strategy burn)
# ═══════════════════════════════════════════════════════════════════════════════


def _write_output(path, rows: int, stop_reason: str) -> None:
    data = {
        "site": {},
        "products": [
            {"url": f"u{i}", "price": "$1", "src_url": "s", "status_code": 200}
            for i in range(rows)
        ],
        "metadata": {
            "discovery_coverage": {
                "ran_phase1": True, "skipped_reason": None,
                "stop_reason": stop_reason, "found": rows,
                "discovered_urls": rows, "expected_total": None,
                "dimensions_iterated": 0, "dimensions_total": 0,
                "max_pages_hit": False, "pages_visited": 1,
            },
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class TestTransientEvidenceAndClassification:
    def _evidence(self, tmp_path, outputs):
        """outputs: list of (name, rows, stop_reason) in mtime order."""
        ws = tmp_path / "workspace" / "slug"
        ws.mkdir(parents=True, exist_ok=True)
        import time

        for i, (name, rows, reason) in enumerate(outputs):
            p = ws / name
            _write_output(str(p), rows, reason)
            os.utime(str(p), (time.time() + i, time.time() + i))

        from django.test.utils import override_settings

        from webapp.agents.graph import _attach_transient_render_evidence

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            return _attach_transient_render_evidence({}, "slug")

    def test_empty_newest_plus_earlier_items_is_transient(self, tmp_path):
        report = self._evidence(tmp_path, [
            ("output_a.json", 12, "no_new_items"),
            ("output_b.json", 0, "empty_render"),
            ("output_c.json", 0, "empty_render"),
        ])
        assert report["discovery_transient"]["suspected"] is True
        assert report["discovery_transient"]["best_items"] == 12

    def test_all_empty_is_not_transient(self, tmp_path):
        report = self._evidence(tmp_path, [
            ("output_a.json", 0, "empty_render"),
            ("output_b.json", 0, "empty_render"),
        ])
        assert "discovery_transient" not in report

    def test_healthy_newest_is_not_transient(self, tmp_path):
        report = self._evidence(tmp_path, [
            ("output_a.json", 0, "empty_render"),
            ("output_b.json", 12, "no_new_items"),
        ])
        assert "discovery_transient" not in report

    def test_classifier_retests_same_draft(self):
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        report = {
            "discovery_transient": {
                "suspected": True, "latest_stop_reason": "empty_render",
                "best_items": 12, "outputs_seen": 3,
            },
        }
        action, reason = classify_test_failure(report, "playwright")
        # [A1] "retest": same draft, no strategy switch, no strategy recorded.
        assert action == "retest", "transient must NOT switch strategy"
        assert "transient" in reason.lower()

    def test_no_evidence_keeps_strategy_verdict(self):
        from webapp.agents.nodes.route_after_testing import classify_test_failure

        action, _ = classify_test_failure({}, "playwright")
        assert action == "strategy"

    def test_strategy_not_recorded_tried_on_transient(self):
        """_decide_strategy only records the rung when classify says 'strategy'
        — pin the guard so a transient re-test cannot exhaust the ladder."""
        with open(os.path.join(
                ROOT, "webapp", "agents", "graph.py"), encoding="utf-8") as fh:
            src = fh.read()
        i = src.find('if _action == "strategy" and not any(')
        assert i != -1
        assert src.count('_new_tried = [{"strategy": _prior_strategy, "reason": _reason}]', 0) == 1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
