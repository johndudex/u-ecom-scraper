"""[job-315 citybeach] Progress-aware execution wall-clock extension.

What happened: the draft's execution was HEALTHY — 1,317 URLs discovered, SFCC
offset pagination working, ``Progress: [k/N]`` logged every ~90s at ~25
items/90s — and the flat ``EXECUTION_TIMEOUT`` (3600s) backstop killed the
subprocess at 72% (950/1317). Extraction alone at a polite ~2.4s/item needs
~79 min. The stall detector (the real hang protection) never fired because
nothing was hung.

Fix pinned here: the monitor treats a ``Progress: [k/N]`` stderr line as
measured life and extends the wall-clock deadline to budget the REMAINING
items (generous 12s/item + 600s slack), clamped to ``EXECUTION_MAX_TIMEOUT``.
``EXECUTION_TIMEOUT`` becomes the base budget; the ceiling must stay under
the celery task soft time limit so a ceiling-capped run still finalizes.

Run: docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.settings -e PYTHONPATH=/app:/app/webapp django sh -c "cd /app && pytest tests/test_execution_wall_clock_extension.py -q"
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from agents.nodes.run_execution import (
    _EXEC_EXTENSION_SLACK_S,
    _EXEC_PER_ITEM_SECONDS,
    _extended_wall_clock_deadline,
)


class TestDeadlineArithmetic:
    """Pure-function contract of ``_extended_wall_clock_deadline``."""

    def test_no_progress_line_leaves_deadline_alone(self):
        base = 1000.0 + 3600.0
        assert _extended_wall_clock_deadline(
            base, 1000.0 + 100, "INFO fetched https://x/p/1\n", 1000.0, 9600.0,
        ) == base

    def test_progress_extends_by_remaining_items(self):
        start = 1000.0
        now = start + 3500.0  # base budget nearly spent
        # citybeach's exact death row: 950/1317 done, base deadline is NOW
        chunk = "INFO - Progress: [950/1317] (72.1%)\n"
        deadline = _extended_wall_clock_deadline(
            start + 3600.0, now, chunk, start, 9600.0,
        )
        proposed = now + (1317 - 950) * _EXEC_PER_ITEM_SECONDS + _EXEC_EXTENSION_SLACK_S
        assert deadline == min(proposed, start + 9600.0)
        # the extension must actually clear the remaining work: 367 items at
        # the observed ~2.4s/item pace is ~880s; the allowance is far larger
        assert deadline - now > 367 * 2.4

    def test_last_match_in_the_chunk_wins(self):
        chunk = "Progress: [10/100] (10.0%)\nProgress: [90/100] (90.0%)\n"
        start, now = 0.0, 3500.0
        deadline = _extended_wall_clock_deadline(
            start + 3600.0, now, chunk, start, 99999.0,
        )
        assert deadline == now + 10 * _EXEC_PER_ITEM_SECONDS + _EXEC_EXTENSION_SLACK_S

    def test_completed_total_extends_nothing(self):
        base = 100.0 + 3600.0
        chunk = "Progress: [1317/1317] (100.0%)\n"
        assert _extended_wall_clock_deadline(
            base, 100.0 + 3500, chunk, 100.0, 9600.0,
        ) == base

    def test_never_shortens_an_existing_deadline(self):
        """A late total (small remaining) must not pull an already-later
        deadline back in."""
        start = 0.0
        deadline = start + 9000.0  # previously extended
        chunk = "Progress: [1310/1317] (99.5%)\n"
        assert _extended_wall_clock_deadline(
            deadline, start + 8000.0, chunk, start, 9600.0,
        ) == deadline

    def test_extension_is_clamped_to_the_ceiling(self):
        start, now = 0.0, 100.0
        # 10,000 remaining items would propose ~120,600s — the ceiling holds
        chunk = "Progress: [0/10000] (0.0%)\n"
        deadline = _extended_wall_clock_deadline(
            start + 3600.0, now, chunk, start, 9600.0,
        )
        assert deadline == start + 9600.0

    def test_partial_line_across_chunks_is_ignored_until_the_next_line(self):
        """stderr chunks can split a line mid-token; neither fragment carries
        the full ``Progress: [k/N]`` shape so neither extends (no crash, no
        partial match) — the NEXT complete Progress line ~90s later lands the
        extension."""
        base = 0.0 + 3600.0
        assert _extended_wall_clock_deadline(
            base, 3500.0, "Progress: [9", 0.0, 9600.0,
        ) == base
        assert _extended_wall_clock_deadline(
            base, 3509.0, "50/1317] (72.1%)\n", 0.0, 9600.0,
        ) == base
        extended = _extended_wall_clock_deadline(
            base, 3590.0, "Progress: [960/1317] (72.9%)\n", 0.0, 9600.0,
        )
        assert extended == 3590.0 + 357 * _EXEC_PER_ITEM_SECONDS + _EXEC_EXTENSION_SLACK_S


class TestBudgetComposition:
    """The knobs must compose: base < ceiling < celery task soft limit."""

    def test_settings_declare_the_ceiling(self):
        from django.conf import settings

        assert settings.EXECUTION_TIMEOUT == 3600
        assert settings.EXECUTION_MAX_TIMEOUT == 9600
        assert settings.EXECUTION_MAX_TIMEOUT > settings.EXECUTION_TIMEOUT

    def test_ceiling_fits_inside_the_celery_soft_limit(self):
        """A ceiling-capped subprocess must leave the task room to finalize:
        EXECUTION_MAX_TIMEOUT < CELERY_TASK_SOFT_TIME_LIMIT (tasks.py reads
        the setting with a matching fallback)."""
        from django.conf import settings

        soft = getattr(settings, "CELERY_TASK_SOFT_TIME_LIMIT", 10800)
        assert settings.EXECUTION_MAX_TIMEOUT < soft

    def test_task_defaults_were_raised_for_full_catalogue_runs(self):
        """[job-68 theiconic] The old 2h soft limit killed a heavy job at
        exactly task-start + 7200s. The fallbacks in tasks.py must match the
        new 3h / 3h6m defaults."""
        with open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")) as fh:
            src = fh.read()
        assert '"CELERY_TASK_SOFT_TIME_LIMIT", 10800' in src
        assert '"CELERY_TASK_TIME_LIMIT", 11160' in src


class TestMonitorWiring:
    """Static anchors: the loop must USE the helper and the deadline."""

    def _src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")) as fh:
            return fh.read()

    def test_loop_reads_the_ceiling_setting(self):
        assert 'getattr(_settings, "EXECUTION_MAX_TIMEOUT", 9600)' in self._src()

    def test_deadline_initializes_from_the_base_budget(self):
        assert "deadline = start + _hard" in self._src()

    def test_loop_feeds_chunks_to_the_extension_helper(self):
        src = self._src()
        assert "_extended_wall_clock_deadline(" in src
        assert 'chunk.decode("utf-8", "replace")' in src

    def test_kill_condition_uses_the_moving_deadline(self):
        src = self._src()
        assert "if time.time() > deadline:" in src
        assert "if time.time() - start > _hard:" not in src

    def test_stall_detector_is_untouched(self):
        """The stall detector stays the hang protection — extension must not
        have weakened it."""
        src = self._src()
        assert "time.time() - last_activity > _stall" in src
        assert 'getattr(_settings, "EXECUTION_STALL_TIMEOUT", 300)' in src

    def test_extension_failure_cannot_kill_the_monitor(self):
        """Bookkeeping is wrapped — a regex/decode surprise must never take
        down the loop that owns the kill switch."""
        src = self._src()
        anchor = src.index("_new_deadline = _extended_wall_clock_deadline(")
        window = src[anchor:anchor + 700]
        assert "except Exception" in window


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
