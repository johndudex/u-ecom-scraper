"""[wave-57 r3] A pivoted writer must not 'finish' the dead strategy's draft.

Guardian flag 1 (highest-leverage open defect): on jobs 603 AND 606 the
writer produced BYTE-IDENTICAL fingerprints ACROSS strategy switches —
the fresh template rode the system prompt (``[WRITER-EMBED] template=file``
with the new strategy's embed), but the dead strategy's
``workspace/{slug}/scraper_draft.py`` still sat on disk, and the
edit-over-write / draft-finisher lanes plus plain read_file habit pulled
the writer back into "finishing" it. The pivot's axis directive lost to
the draft on disk.

Fix: when an axis escape spends a rung (or the tier), the dead draft is
ROTATED out of the workspace first — the pivoted cycle starts from a
clean slate, so "finish the existing scraper" is not an available move.
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

SLUG = "w57r3-rotation-fixture"
DRAFT_BODY = "#!/usr/bin/env python3\n# dead strategy draft 45a9d7508700\n"


def _draft_path() -> str:
    from agents.graph import _get_project_root

    return os.path.join(
        _get_project_root(), "workspace", SLUG, "scraper_draft.py"
    )


@pytest.fixture(autouse=True)
def _draft_workspace():
    os.makedirs(os.path.dirname(_draft_path()), exist_ok=True)
    yield
    shutil.rmtree(os.path.dirname(_draft_path()), ignore_errors=True)


def _mk_draft() -> str:
    path = _draft_path()
    with open(path, "w") as fh:
        fh.write(DRAFT_BODY)
    return path


def _state(strategy="http_requests", tried=None, **over):
    state = {
        "site_slug": SLUG,
        "job_id": 603,
        "scraper_analysis": {"strategy": strategy},
        "strategies_tried": list(tried or []),
    }
    state.update(over)
    return state


def _archive_candidates() -> list[str]:
    d = os.path.dirname(_draft_path())
    return sorted(
        f
        for f in os.listdir(d)
        if f.startswith("scraper_draft-http_requests-dead")
    )


class TestDeadDraftRotation:
    def test_terminal_pivot_rotates_the_dead_draft(self):
        """The #603 shape: the pivot spends a rung — the dead draft must be
        gone from the workspace and preserved beside it."""
        from agents.graph import _writer_axis_pivot

        path = _mk_draft()
        out = _writer_axis_pivot(_state(), {"noop_fix_cycles": 2})
        assert out is not None
        assert not os.path.exists(path)
        archives = _archive_candidates()
        assert len(archives) == 1
        with open(os.path.join(os.path.dirname(path), archives[0])) as fh:
            assert fh.read() == DRAFT_BODY

    def test_strike1_bounce_rotates_the_dead_draft(self):
        """The shared escape body serves the strike-1 ladder too — the dead
        draft rotates there as well (606's cycle-1 shape)."""
        from agents.graph import _noop_strike1_ladder_bounce

        path = _mk_draft()
        out = _noop_strike1_ladder_bounce(_state(), {"noop_fix_cycles": 1})
        assert out is not None
        assert not os.path.exists(path)
        assert len(_archive_candidates()) == 1

    def test_tier_pivot_rotates_the_dead_draft(self):
        """Ladder exhausted, tier axis fires — still an axis change, the
        dead draft still rotates."""
        from agents.graph import _writer_axis_pivot

        path = _mk_draft()
        out = _writer_axis_pivot(
            _state(
                "http_requests",
                tried=[
                    {"strategy": "http_navigation"},
                    {"strategy": "playwright"},
                ],
            ),
            {"noop_fix_cycles": 2},
        )
        assert out is not None
        assert "proxy TIER" in (out.get("feedback_for_writer") or "")
        assert not os.path.exists(path)
        assert len(_archive_candidates()) == 1

    def test_refused_pivot_keeps_the_draft(self):
        """No axis experiment → no escape → the workspace is untouched (the
        honest death must not destroy failure evidence)."""
        from agents.graph import _writer_axis_pivot

        path = _mk_draft()
        assert (
            _writer_axis_pivot(
                _state("http_navigation+residential_proxy"),
                {"noop_fix_cycles": 2},
            )
            is None
        )
        assert os.path.exists(path)

    def test_rotation_collision_gets_a_suffix(self):
        """A second axis change in one job must not clobber the first
        archive — evidence accretes, newest wins the clean slate."""
        from agents.graph import _writer_axis_pivot

        _mk_draft()
        assert _writer_axis_pivot(
            _state(), {"noop_fix_cycles": 2}
        ) is not None
        _mk_draft()
        assert (
            _writer_axis_pivot(
                _state("http_navigation", tried=[{"strategy": "http_requests"}]),
                {"noop_fix_cycles": 2},
            )
            is not None
        )
        d = os.path.dirname(_draft_path())
        dead = [
            f for f in os.listdir(d) if "-dead" in f and f.endswith(".py")
        ]
        assert sorted(dead) == [
            "scraper_draft-http_navigation-dead.py",
            "scraper_draft-http_requests-dead.py",
        ]

    def test_no_draft_is_a_clean_noop(self):
        """Nothing on disk — the escape must not manufacture a directory or
        fail."""
        from agents.graph import _writer_axis_pivot

        out = _writer_axis_pivot(_state(), {"noop_fix_cycles": 2})
        assert out is not None
        assert _archive_candidates() == []

    def test_rotation_is_best_effort(self):
        """A draft path that cannot be replaced (a directory squatting the
        name) must not nullify the pivot — the escape outranks the chore."""
        from agents.graph import _writer_axis_pivot

        path = _mk_draft()
        os.remove(path)
        os.makedirs(path)  # a directory where the draft should be
        try:
            out = _writer_axis_pivot(_state(), {"noop_fix_cycles": 2})
            assert out is not None
            assert out["writer_axis_pivot_count"] == 1
        finally:
            shutil.rmtree(path, ignore_errors=True)


def test_rotation_lives_in_the_shared_escape_body():
    """Pin: the rotation is called from `_strategy_axis_escape` — both
    consumers (strike-1 bounce, terminal pivot) inherit it; a copy inside
    only one wrapper would leave the other lane re-serving the dead
    draft."""
    src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
    body = src[src.index("def _strategy_axis_escape"):]
    body = body[: body.index("def _noop_strike1_ladder_bounce")]
    assert "_archive_dead_strategy_draft(" in body
