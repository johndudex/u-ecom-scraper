"""Stderr truncation: 4000-char budget, TAIL-side (exception visible).

Prod 351 (glassons): head-truncation kept the scraper's log banner and cut
the actual exception — "page.goto timeout" never reached the error_message.
The exception type lives at the END of a Python traceback.
"""
from __future__ import annotations

import os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(p):
    return open(os.path.join(ROOT, p)).read()


class TestTailTruncation:
    def test_browser_path_tail_4000(self):
        src = _read("webapp/agents/nodes/run_execution.py")
        assert 'result.get("stderr", "")[-4000:]' in src
        assert 'result.get("stderr", "")[:2000]' not in src

    def test_inprocess_paths_tail_4000(self):
        src = _read("webapp/agents/nodes/run_execution.py")
        assert "stderr[-4000:]" in src
        assert "stderr[-2000:]" not in src and "stderr[-1500:]" not in src

    def test_tasks_exception_sites_tail(self):
        src = _read("webapp/scraper/tasks.py")
        assert "str(exc)[-4000:]" in src
        assert "str(exc)[:2000]" not in src

    def test_bs_sends_enough_stderr(self):
        # browser_service already returns 50k — the django-side tail has room
        src = _read("browser_service/scraper_runner.py")
        assert '"stderr": (result.stderr or "")[:50000]' in src
