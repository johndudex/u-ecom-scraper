"""[wave-32 A3/A4] Writer telemetry: truthful wall-clock payload + file-tool
arg position.

Job 587's writer died at ~2700s of REAL waiting (1800s base + activity
extension) while the return payload said ``wall-clock timeout after 1800s`` —
every downstream consumer (the twice-in-a-row arm, the terminal job row, the
postmortems) printed the base window as if it were the truth. A3 pins: the
WallClockTimeout payload names BOTH numbers and carries ``_waited_s``.

A4 pins: read_file/edit_file ToolCallLog summaries carry position
(``line=/num_lines=`` / a 60-char prefix of ``old_string``) so a read-spiral
is provable from the log alone (job 587: dozens of re-reads, all identical
summaries).

Run: docker compose exec -T django sh -c "cd /app && pytest tests/test_wave32_writer_telemetry.py -q"
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents import graph  # noqa: E402


class _BlockingAgent:
    """Sync invoke that outlives any sane test window (daemon-abandoned)."""

    def invoke(self, payload, cfg):
        time.sleep(5)
        return {"messages": []}


def _extended_sync_payload(monkeypatch, base=1.0, extension=0.5):
    """Drive the SYNC timeout arm through the activity-extension join so the
    real wait (base+extension) differs from the base window."""
    monkeypatch.setattr(
        graph, "_find_activity_logger", lambda agent_cfg: object()
    )

    def fake_join(thread, *, base_timeout, activity, fresh_s, max_timeout,
                  clock=time.monotonic, job_deadline=None, poll_s=5.0):
        t0 = time.monotonic()
        deadline = t0 + float(base_timeout) + float(extension)
        while time.monotonic() < deadline:
            thread.join(timeout=0.05)
            if not thread.is_alive():
                break
        return time.monotonic() - t0

    monkeypatch.setattr(graph, "_join_with_activity_extension", fake_join)
    return graph._invoke_agent_with_timeout(
        _BlockingAgent(), [], {}, "code_writer", 999,
        timeout=base, allow_activity_extension=True,
    )


class TestA3TruthfulWallClockPayload:
    def test_timeout_payload_carries_waited(self, monkeypatch):
        """The extension arm's payload names base AND the real stop time, and
        carries ``_waited_s`` with the actual seconds (587: died at 2700s,
        reported 1800s)."""
        res = _extended_sync_payload(monkeypatch)
        assert res.get("_error_class") == "WallClockTimeout"
        err = str(res.get("_error") or "")
        assert "wall-clock timeout" in err, "consumers substring-match the phrase"
        assert "after 1s base" in err, "the base window must be named"
        assert "thread stopped at" in err, "the real stop time must be named"
        waited = res.get("_waited_s")
        assert isinstance(waited, (int, float)), (
            "WallClockTimeout payload must carry the numeric _waited_s"
        )
        assert waited >= 1.4, f"_waited_s must be the REAL wait, got {waited}"

    def test_async_twin_derives_waited_from_t0(self, monkeypatch):
        """The async path has no ``_waited`` — its payload must derive the
        real seconds from its own t0 (same contract, both twins)."""
        import asyncio

        from django.test.utils import override_settings

        class _SlowAsyncAgent:
            async def ainvoke(self, payload, cfg):
                await asyncio.sleep(5)
                return {"messages": []}

        monkeypatch.setattr(graph, "_async_execution_enabled", lambda phase: True)
        with override_settings(LLM_ASYNC_EXECUTION=True):
            res = graph._invoke_agent_with_timeout(
                _SlowAsyncAgent(), [], {}, "site_analyzer", 999, timeout=1,
            )
        assert res.get("_error_class") == "WallClockTimeout"
        err = str(res.get("_error") or "")
        assert "wall-clock timeout" in err
        assert "base" in err and "thread stopped at" in err
        waited = res.get("_waited_s")
        assert isinstance(waited, (int, float))
        assert waited >= 0.9, f"async twin must report real wait, got {waited}"


class TestA4FileToolArgsCarryPosition:
    """ToolCallLog args summaries must prove WHERE a read/edit happened —
    587's read-spiral (dozens of re-reads of the same draft) produced dozens
    of identical "Read workspace/..." rows."""

    def test_read_file_summary_has_position(self):
        from agents.graph import _summarize_tool_args

        s = _summarize_tool_args("read_file", {
            "path": "workspace/s/scraper_draft.py", "line": 120, "num_lines": 400,
        })
        assert "line=120" in s and "num_lines=400" in s, s

    def test_read_file_summary_carries_offset_when_used(self):
        from agents.graph import _summarize_tool_args

        s = _summarize_tool_args("read_file", {
            "path": "d.py", "offset": 5000,
        })
        assert "offset=5000" in s, s

    def test_read_file_summary_plain_shape_unchanged(self):
        from agents.graph import _summarize_tool_args

        s = _summarize_tool_args("read_file", {"path": "d.py"})
        assert s.startswith("Read d.py"), s

    def test_edit_file_summary_carries_old_string_prefix(self):
        from agents.graph import _summarize_tool_args

        s = _summarize_tool_args("edit_file", {
            "path": "d.py", "old_string": "E" * 200, "new_string": "N",
        })
        assert "E" * 60 in s, "60-char prefix of old_string must ride the row"
        assert "E" * 61 not in s, "prefix must be capped at 60 chars"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
