"""[wave-26 W26-2] Seed-file lifecycle: the seed is part of the tested contract.

Prod 419 (nike.in): the writer's cycle-1 ground-truth seed — 1 real PDP,
proven by a PASS verdict — was overwritten at the cycle-2 writer re-entry by
the navigator's 20-URL raw nav dump (homepage, /help-center, category
listings). The count-only preserve guard (len(existing) > len(seeds)) made
1 good URL lose to 20 junk ones; every later Phase-2 run extracted the
homepage false-positive row and the cascade burned out on a defect the seed
file, not the scraper, carried.

Fixes pinned here:
- F-A: ``src/seed_urls.seed_report`` drops query-only homepages and
  utility-first-segment URLs (site chrome the host filter can't see).
- F-B: ``graph._invoke_code_writer`` writes ``input_urls.json`` only when
  ABSENT — never overwrites an existing seed, count comparison removed.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")

from src.seed_urls import (  # noqa: E402
    dropped_summary,
    filter_seed_payload,
    seed_report,
)

JOB = "https://www.nike.in/p/some-shoe-P1000.html"


# ─── F-A: plausibility rules ─────────────────────────────────────────────────


class TestSeedPlausibility:
    def test_query_only_homepage_dropped(self):
        """Prod 419's #1 poison entry: pathless + query survived the old
        'pathless AND queryless' rule and became the homepage false-positive
        row the tester kept flagging."""
        kept, dropped = seed_report(
            ["https://www.nike.in/?ptype=homepage"], JOB
        )
        assert kept == []
        assert dropped.get("no-path") == 1

    def test_bare_homepage_still_dropped(self):
        kept, dropped = seed_report(["https://www.nike.in/"], JOB)
        assert kept == []
        assert dropped.get("no-path") == 1

    def test_utility_first_segment_dropped(self):
        """Prod 419's /help-center: same-host, path-rich enough to pass the
        old filters — but never an item page."""
        kept, dropped = seed_report(
            [
                "https://www.nike.in/help-center",
                "https://www.nike.in/HELP",
                "https://www.nike.in/faq/shipping",
                "https://www.nike.in/customer-service",
            ],
            JOB,
        )
        assert kept == []
        assert dropped.get("utility-page") == 4

    def test_real_pdp_and_category_listing_survive(self):
        """The belt must not over-drop: a real PDP and a category listing
        (nav_method=listing jobs legitimately seed from listings) survive."""
        kept, dropped = seed_report(
            [
                "https://www.nike.in/p/some-shoe-P1000.html",
                "https://www.nike.in/men/shoes/lifestyle/c/92589",
            ],
            JOB,
        )
        assert len(kept) == 2
        assert not dropped

    def test_dropped_summary_reports_new_reason(self):
        _, dropped = seed_report(["https://www.nike.in/help-center"], JOB)
        assert "utility-page=1" in dropped_summary(dropped)

    def test_filter_seed_payload_applies_new_rules(self):
        payload = {"urls": ["https://www.nike.in/?ptype=homepage", JOB]}
        filtered, dropped = filter_seed_payload(payload, JOB)
        assert filtered["urls"] == [JOB]
        assert dropped.get("no-path") == 1


# ─── F-B: the harness never overwrites an existing seed ──────────────────────


def _grab(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, f"{name} not found"
    return m.group(0)


class TestSeedFreeze:
    def test_writer_write_is_absence_gated(self):
        """Source pin: the input_urls.json write block must skip when the
        file already exists, and the count-only overwrite guard must be
        gone (it is what let 20 nav URLs beat 1 ground-truth URL)."""
        body = _grab(open(GRAPH).read(), "_invoke_code_writer")
        block = body[body.find("input_urls.json"):]
        assert 'if os.path.isfile(iu_path):' in block, (
            "the seed write must be gated on file ABSENCE — an existing "
            "input_urls.json is part of the tested contract (prod 419)"
        )
        assert "preserving it" in block
        assert "len(_existing)" not in block, (
            "the count-only guard (len(existing) > len(seeds)) must be gone"
        )
        # the write only happens in the else-branch of the existence check
        write_at = block.find('_json.dump({"urls": sample_urls}')
        exists_at = block.find("if os.path.isfile(iu_path):")
        assert write_at > exists_at

    def test_contract_comment_names_the_incident(self):
        body = _grab(open(GRAPH).read(), "_invoke_code_writer")
        assert "prod-419" in body
