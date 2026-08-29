"""Job-312 (pillowtalk e2e round 3) regressions: the browser-run timeout floor.

What happened (job 312): two-phase playwright drafts run Phase 1 discovery AND
Phase 2 sample extraction in ONE subprocess. pillowtalk's measured discovery
alone is ~124s (5 pages); + 5 PDP samples the full run needs ~350s — but
run_scraper floored browser timeouts at 240s (chosen when lw.com's sample-only
runs needed ~160s), so every full two-phase test run was SIGKILLed mid-flight
(exit -1), classified as a timeout, and burned a strategy rung — even though
discovery was demonstrably working (F-A probe verified in the same job's logs).

Fix pinned here: the browser floor is 600s (covers a full multi-page walk +
samples with headroom; /scrape accepts up to 7200s; the runner never retries
timeouts, so the worst case is one bounded 660s wait).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job312_browser_timeout_floor.py -q
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

SHELL_TOOLS = os.path.join(ROOT, "webapp", "agents", "tools", "shell_tools.py")


def _src() -> str:
    with open(SHELL_TOOLS, encoding="utf-8") as fh:
        return fh.read()


class TestBrowserTimeoutFloor:
    def test_floor_is_600_for_browser_scrapers(self):
        src = _src()
        assert "if needs_browser and timeout < 600:" in src, (
            "browser timeout floor must be 600s — two-phase drafts (discovery "
            "+ sample extraction in one subprocess) need ~350s+; the old 240s "
            "floor SIGKILLed healthy runs mid-flight (job 312)"
        )
        assert "flooring browser timeout %ds → 600s" in src

    def test_floor_applies_after_browser_detection(self):
        """The floor must sit AFTER needs_browser is decided (exec_mode sniff),
        so HTTP scrapers keep their tight budgets."""
        src = _src()
        i_sniff = src.find('_scraper_needs_browser(full_path)')
        i_floor = src.find("if needs_browser and timeout < 600:")
        assert -1 < i_sniff < i_floor, "floor must follow browser detection"

    def test_http_path_has_no_floor(self):
        """The http branch must not inherit the 600s floor — HTTP scrapers
        (api/internal_api strategies) are fast and their budgets stay tight."""
        src = _src()
        i_floor = src.find("if needs_browser and timeout < 600:")
        http_branch = src[src.find("http-based, running locally"):]
        assert "timeout = 600" not in http_branch
        assert i_floor != -1


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
