"""[wave-59 C7] A strategy delta after a writer death rotates the dead draft.

The deterministic analyzer may pick a DIFFERENT strategy on the retry after
a writer death (the probe-derived pick + escalation ladder see the dead
rung). C4×C7 interplay (critic-2, resolved in plan v3.2): on a delta the
draft on disk belongs to the DEAD strategy — repairing it for an abandoned
strategy is wasted windows, so it is rotated aside (wave-57 r3's
``_archive_dead_strategy_draft``) and the edit-over-write arm is cleared.
NO finisher fires on a delta; on a SAME-strategy pick the draft stays and
the strike-2 finisher arms own it.

Arrival signal: ``code_writer_error_count > 0`` distinguishes a writer-retry
arrival from tester-bounce arrivals (those carry test_retry_count /
noop_fix_cycles). Test-FAIL arrivals with zero writer errors keep the draft
(the tester bounce re-runs the SAME strategy — edit-over-write is the
wanted framing there).
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

import agents.graph as graph  # noqa: E402

SLUG = "wave59-rotation-test"


def _decide(monkeypatch, tmp_path, state, with_draft=True):
    """Run the deterministic strategy node hermetically; return (command, rows)."""
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True, exist_ok=True)
    if with_draft:
        (ws / "scraper_draft.py").write_text("X = 1\n")
    monkeypatch.setattr(
        graph, "_derive_strategy",
        lambda s: {"strategy": s.get("_derived", "playwright")},
    )
    monkeypatch.setattr(graph, "_PATCHES_ENABLED", False)
    monkeypatch.setattr(
        graph, "_escalate_strategy",
        lambda analysis, tried, skip_approvals=False: (analysis, None),
    )
    monkeypatch.setattr(graph, "_notify_phase", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    rows = []
    monkeypatch.setattr(
        graph, "_log_event_row", lambda job_id, agent, content: rows.append(content)
    )
    result = graph._decide_strategy(state)
    return result, rows


def _state(**over):
    base = {
        "job_id": 623,
        "site_slug": SLUG,
        "_derived": "playwright",
        "scraper_analysis": {"strategy": "http_navigation"},
        "test_report": {"overall_assessment": "FAIL", "issues": []},
        "code_writer_error_count": 1,
    }
    base.update(over)
    return base


class TestDeathRotation:
    def test_delta_after_death_rotates_and_disarms_eow(self, tmp_path, monkeypatch):
        result, rows = _decide(monkeypatch, tmp_path, _state())
        assert result.update["strategy_rerun"] is False
        assert result.update.get("last_writer_template") == "", (
            "the EOW arm must be cleared — the fresh template rides the "
            "system prompt on the next cycle"
        )
        ws = tmp_path / "workspace" / SLUG
        assert not (ws / "scraper_draft.py").exists(), "the dead draft rotated aside"
        assert (ws / "scraper_draft-http_navigation-dead.py").exists()
        assert any("[DEATH-ROTATION]" in r for r in rows)

    def test_same_strategy_after_death_keeps_the_draft(self, tmp_path, monkeypatch):
        """The C4 finisher arms own the same-strategy path — no rotation."""
        state = _state(_derived="http_navigation")
        result, rows = _decide(monkeypatch, tmp_path, state)
        assert result.update["strategy_rerun"] is True
        assert "last_writer_template" not in result.update
        assert (tmp_path / "workspace" / SLUG / "scraper_draft.py").exists()
        assert not any("[DEATH-ROTATION]" in r for r in rows)

    def test_tester_bounce_without_writer_errors_keeps_the_draft(
        self, tmp_path, monkeypatch
    ):
        """A strategy-switch from a TEST failure (zero writer errors) is the
        normal ladder — edit-over-write framing on the fresh draft is fine,
        and no draft is destroyed behind the tester's verdict."""
        state = _state(code_writer_error_count=0)
        result, _rows = _decide(monkeypatch, tmp_path, state)
        assert "last_writer_template" not in result.update
        assert (tmp_path / "workspace" / SLUG / "scraper_draft.py").exists()

    def test_first_arrival_never_rotates(self, tmp_path, monkeypatch):
        """No prior strategy → nothing is dead; the arm needs _prior_strategy."""
        state = _state()
        del state["scraper_analysis"]
        result, rows = _decide(monkeypatch, tmp_path, state)
        assert "last_writer_template" not in result.update
        assert not any("[DEATH-ROTATION]" in r for r in rows)

    def test_rotation_survives_a_missing_draft(self, tmp_path, monkeypatch):
        """No draft on disk → the arm still clears the EOW arm and logs.
        (The first run rotates the fresh draft aside; the re-run arrives at
        an empty workspace and must stay best-effort.)"""
        state = _state()
        result, _rows = _decide(monkeypatch, tmp_path, state)
        ws = tmp_path / "workspace" / SLUG
        assert not (ws / "scraper_draft.py").exists()
        result2, rows2 = _decide(
            monkeypatch, tmp_path, state, with_draft=False
        )
        assert result2.update.get("last_writer_template") == ""
        assert any("no draft on disk" in r for r in rows2)


class TestWiring:
    def test_rotation_wired_in_decide_strategy(self):
        src = open(
            os.path.join(ROOT, "webapp", "agents", "graph.py")
        ).read()
        i = src.index("def _decide_strategy")
        j = src.index("\ndef ", i + 10)
        body = src[i:j]
        assert "_archive_dead_strategy_draft" in body
        assert 'update["last_writer_template"] = ""' in body
        assert "code_writer_error_count" in body, (
            "the arrival signal is the writer's own error counter — a "
            "tester-bounce arrival must not destroy the draft"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
