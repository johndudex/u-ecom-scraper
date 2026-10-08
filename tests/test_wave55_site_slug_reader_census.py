"""[wave-55 R11 gap round] Pin the H14 Site-slug reader census.

The H14 row of ``docs/plans/wave55-fix-wave-status.md`` banks the unordered
``Site.objects.filter(slug=...)`` readers left in the tree after R11 ordered
the two audit-cited consumers (``views._job_input_urls`` and
``intake_check_site``). That enumeration rotted once inside a single round —
round 33 recorded seven, the tree holds ten (it missed
``management/commands/scrape.py``, ``management/commands/run_node.py`` and
``dedupe.py``) — and nothing re-checked it: the verifier's exhaustive sweep
is a one-time act. This file is the re-check, run on every gate.

Walk ``webapp/`` + ``src/`` (``migrations/``, ``tests/``, ``.pytest_cache``
and comment lines excluded), match ``.filter(slug=`` exactly (``site_slug=``
cannot match the regex), and classify each hit ordered/unordered by whether
``order_by("id")`` sits in the same statement window. The counts are pinned
by path + count, never by line number, so ordinary edits do not rot the pin;
the last assertion is census parity — every unordered path must still be
named in the H14 row, so doc and pin cannot drift apart (round-32 GAP-1c
pattern).
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

CENSUS_DOC = os.path.join(ROOT, "docs", "plans", "wave55-fix-wave-status.md")

# ``site_slug=`` cannot match: the regex demands the opening paren (or
# whitespace after it) immediately before the kwarg name.
SLUG_FILTER_RE = re.compile(r"\.filter\(\s*slug\s*=")
ORDER_BY_ID_RE = re.compile(r"\.order_by\(\s*['\"]id['\"]\s*\)")
# Continuation lines: ``filter(slug=`` may sit on its own line inside a
# parenthesised lookup chain (dedupe.py:79) with ``.order_by("id")`` a line
# or two later. Beyond this window a match is a different statement.
STATEMENT_WINDOW = 250

SKIP_DIRS = {"migrations", "tests", ".pytest_cache", "__pycache__"}
WALK_ROOTS = ("webapp", "src")

# Path + count, not line numbers.
EXPECTED_UNORDERED = {
    "webapp/agents/graph.py": 1,
    "webapp/scraper/dedupe.py": 1,
    "webapp/scraper/tasks.py": 2,
    "webapp/scraper/views.py": 2,
    "webapp/scraper/api/extractors.py": 1,
    "webapp/scraper/api/writers.py": 1,
    "webapp/scraper/management/commands/scrape.py": 1,
    "webapp/scraper/management/commands/run_node.py": 1,
}
EXPECTED_ORDERED = {"webapp/scraper/views.py": 2}


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _doc_name(rel_path: str) -> str:
    """The name the census row uses for this file (scraper/-relative)."""
    return (
        rel_path[len("webapp/scraper/") :] if rel_path.startswith("webapp/scraper/") else os.path.basename(rel_path)
    )


def _collect() -> tuple[dict[str, int], dict[str, int]]:
    """Every ``filter(slug=`` hit outside migrations/tests, by class."""
    ordered: dict[str, int] = {}
    unordered: dict[str, int] = {}
    for walk_root in WALK_ROOTS:
        base = os.path.join(ROOT, walk_root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
                # Line starts, so a hit can be tied back to its own line and
                # a comment line skipped without losing multi-line statements.
                starts = [0]
                for line in text.splitlines(keepends=True):
                    starts.append(starts[-1] + len(line))
                for lineno, line in enumerate(text.splitlines()):
                    if line.strip().startswith("#"):
                        continue
                    for match in SLUG_FILTER_RE.finditer(line):
                        window = text[starts[lineno] + match.start() :][:STATEMENT_WINDOW]
                        key = _rel(full)
                        bucket = ordered if ORDER_BY_ID_RE.search(window) else unordered
                        bucket[key] = bucket.get(key, 0) + 1
    return ordered, unordered


def _h14_row_text() -> str:
    with open(CENSUS_DOC, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("| H14 "):
                return line
    raise AssertionError("census doc has no H14 row to be at parity with")


class TestSiteSlugReaderCensus:
    def test_twelve_non_migration_slug_readers(self):
        ordered, unordered = _collect()
        total = sum(ordered.values()) + sum(unordered.values())
        assert total == 12, f"expected 12 filter(slug=) readers, found {total}: {ordered} + {unordered}"

    def test_unordered_readers_are_exactly_the_banked_set(self):
        _, unordered = _collect()
        assert unordered == EXPECTED_UNORDERED, (
            "the unordered filter(slug=) census drifted — a reader gained or lost "
            f"order_by('id') or a call site moved: {unordered} != {EXPECTED_UNORDERED}"
        )

    def test_ordered_readers_are_the_two_audit_cited_consumers(self):
        ordered, _ = _collect()
        assert ordered == EXPECTED_ORDERED, (
            "the ordered filter(slug=) census drifted — only views._job_input_urls and "
            f"intake_check_site are pinned as lowest-id reads: {ordered} != {EXPECTED_ORDERED}"
        )

    def test_every_unordered_path_is_named_in_the_h14_row(self):
        _, unordered = _collect()
        row = _h14_row_text()
        missing = sorted({name for name in map(_doc_name, unordered) if name not in row})
        assert not missing, f"H14 row no longer names the unordered reader census: missing {missing}"
