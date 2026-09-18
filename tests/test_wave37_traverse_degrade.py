"""[wave-37 W37-NEW-C part 2] Minority off-domain contamination in a
traversal result degrades (drops the bad links, keeps the clean run)
instead of killing the job after one forced re-traverse (prod 626-class).
Majority off-domain / off-domain api.url (wave-34 F3, count>0) still
refuses — the veto semantics are unchanged.
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

from webapp.agents.graph import _strip_off_domain_links  # noqa: E402


class _R:
    """Traversal-result stub: the attrs _strip_off_domain_links reads."""

    def __init__(self, links, goal_url=None, api=None):
        self.item_links = links
        self.goal_url = goal_url
        self.api = api


JOB = "https://www.example.com/shop"


def test_minority_off_domain_links_dropped():
    links = [
        "https://www.example.com/p/1",
        "https://www.example.com/p/2",
        "https://static.klaviyo.com/form",
        "https://www.vinted.net/x",
    ]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert clean.item_links == [
        "https://www.example.com/p/1",
        "https://www.example.com/p/2",
    ]
    assert dropped == 2


def test_majority_off_domain_not_strippable():
    links = [
        "https://a.com/x",
        "https://b.com/y",
        "https://www.example.com/p/1",
    ]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert clean is None and dropped == 2


def test_off_domain_api_capture_never_stripped():
    # wave-34 F3: an off-domain api.url claiming data is identity-bearing —
    # never silently rewritten; the run stays vetoed.
    clean, _ = _strip_off_domain_links(
        _R(
            ["https://www.example.com/p/1"],
            api={"url": "https://cdn.no/x", "count": 25},
        ),
        JOB,
    )
    assert clean is None


def test_clean_result_passes_through():
    links = ["https://www.example.com/p/1"]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert dropped == 0 and clean.item_links == links


def test_relative_links_kept():
    links = ["/p/1", "/p/2", "https://tracker.io/pixel"]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert clean.item_links == ["/p/1", "/p/2"]
    assert dropped == 1


def test_empty_links_passthrough():
    res = _R([])
    clean, dropped = _strip_off_domain_links(res, JOB)
    assert clean is res and dropped == 0
