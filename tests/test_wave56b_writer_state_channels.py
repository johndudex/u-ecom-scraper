"""[wave-56b veto-class fix] The writer-channel state keys MUST be declared.

Guardian-verified during the 593-600 batch (2026-10-10): langgraph silently
strips undeclared keys from a node's update — proven live against the repo's
pinned langgraph with the repo's own ``ScrapeState`` (a node returning a
declared key beside undeclared ones persists only the declared one; state.py
itself documents this contract at the ``shortfall_remediation_count`` note,
"undeclared keys are stripped from graph state").

``feedback_for_writer`` (edit-or-die directive + execution-recycle channel)
and ``writer_dup_read_count`` (edit-or-die cross-window spiral counter) were
both returned by ``code_writer``'s fix-loop without being declared — the
directive never reached the writer and the cross-window counter always read 0.
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

from agents.state import ScrapeState  # noqa: E402


def test_feedback_for_writer_is_declared():
    assert "feedback_for_writer" in ScrapeState.__annotations__


def test_writer_dup_read_count_is_declared():
    assert "writer_dup_read_count" in ScrapeState.__annotations__


def test_directive_writer_reads_only_declared_keys():
    """Guard over the exact spelling both writers use — a rename on one side
    of the channel without the other re-creates the silent drop."""
    src = open(os.path.join(ROOT, "webapp", "agents", "state.py")).read()
    for key in ("feedback_for_writer", "writer_dup_read_count"):
        assert f"    {key}:" in src, key
