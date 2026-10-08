"""[wave-55 GAP3] every doc that names ``code-writer-v1`` says the file is gone.

``.opencode/agents/code-writer-v1.md`` was deleted in wave-55 (its line 17
taught the banned parent-of-draft ``sys.path`` insert that C1 removed). Five of
the six repo references were annotated as part of that deletion; the sixth —
``docs/cli-contract-plan.md`` — still presented the deletion as pending future
work ("Delete dead ... eventually; do not edit"), so a reader following the
plan would look for a file that is not there and could not tell how to get it
back.

This guard pins the whole set: every *tracked* file under docs/ (top level and
plans/) and .opencode/ whose text names the deleted prompt must carry a
deletion/attribution marker on the line that names it. The tracked-file set is
what ``git ls-files`` enumerates — the django container ships no git binary, so
the list is read straight out of ``.git/index`` (version 2, no path
compression), with a ``git ls-files`` fast-path when a git is on PATH. Walking
the tree instead would sweep scratch/ and _trash/ notes that no doc reader is
meant to trust.
"""
from __future__ import annotations

import fnmatch
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

# Pathspecs, spelled the way ``git ls-files`` accepts them. Without glob magic
# a pathspec ``*`` crosses directory separators, and ``fnmatch``'s ``*`` does
# the same — so one tuple serves both enumeration paths.
SCOPE_PATTERNS = ("docs/*.md", "docs/plans/*.md", ".opencode/**")

# Every spelling a doc has used for the retired prompt.
VARIANTS = ("code-writer-v1", "code_writer_v1", "writer-v1.md")

# A line naming the deleted file is attributed when it says the deletion
# happened (revert-baseline notes), that the cited path is gone (dangling
# evidence citations), or marks the task done in the wave that did it.
MARKERS = ("deleted in wave-55", "file deleted wave-55", "Done (wave-55")


def _tracked_via_git():
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", *SCOPE_PATTERNS],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:  # no git on PATH (the django container ships none)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.splitlines()


def _tracked_via_index():
    """Parse the git index the way ``git ls-files`` would.

    Index v2: a 12-byte header, then one fixed 62-byte record per path
    (40 bytes of stat data, 20-byte sha1, 2-byte flags) followed by the
    NUL-terminated path and padding out to an 8-byte boundary. Entries with a
    non-zero stage are unresolved conflicts and are not on disk as tracked
    content, so they are skipped.
    """
    with open(os.path.join(ROOT, ".git", "index"), "rb") as fh:
        data = fh.read()
    if data[:4] != b"DIRC" or struct.unpack(">I", data[4:8])[0] != 2:
        return None
    count = struct.unpack(">I", data[8:12])[0]
    paths, pos = [], 12
    for _ in range(count):
        flags = struct.unpack(">H", data[pos + 60:pos + 62])[0]
        if flags & 0x3000:  # stage 1-3: conflict, not a clean tracked path
            namelen = flags & 0x0FFF
            step = 62 + namelen if namelen < 0x0FFF else data.index(b"\0", pos + 62) + 1 - pos
            pos += (step + 8) // 8 * 8
            continue
        namelen = flags & 0x0FFF
        if namelen < 0x0FFF:
            name = data[pos + 62:pos + 62 + namelen].decode("utf-8")
        else:
            end = data.index(b"\0", pos + 62)
            name = data[pos + 62:end].decode("utf-8")
        paths.append(name)
        pos += ((62 + len(name)) + 8) // 8 * 8
    return paths


def _tracked():
    tracked = _tracked_via_git() or _tracked_via_index()
    assert tracked is not None, "cannot enumerate tracked files (no git, no index)"
    return [
        p
        for p in tracked
        if any(fnmatch.fnmatch(p, pat) for pat in SCOPE_PATTERNS)
    ]


def _mentions():
    """(path, lineno, attributed) for every line naming the deleted prompt."""
    found = []
    for rel in _tracked():
        # ls-files lists index entries, so a worktree deletion that has not
        # been staged still shows up here (code-writer-v1.md itself does) and
        # has no text to scan.
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        for lineno, line in enumerate(lines, 1):
            if any(v in line for v in VARIANTS):
                found.append(
                    (
                        f"{rel}:{lineno}",
                        any(m in line for m in MARKERS),
                    )
                )
    return found


def _unattributed():
    """path:line for every mention of the deleted prompt with no marker."""
    return [where for where, attributed in _mentions() if not attributed]


def test_every_code_writer_v1_reference_is_attributed():
    bad = _unattributed()
    assert not bad, (
        "code-writer-v1.md was deleted in wave-55; every doc that names it "
        "must carry a deletion note or point at git history instead of the "
        f"path: {bad}"
    )


def test_the_sweep_sees_the_attributed_notes_it_protects():
    """The guard above passes on an empty sweep, so pin the enumeration: the
    five docs that already carry the wave-55 note have to be inside the
    tracked scope, and at least one attributed line has to come out of it."""
    swept = set(_tracked())
    for rel in (
        "docs/cli-contract-plan.md",
        "docs/code-writer-prompt-simplification.md",
        "docs/code-writer-summarization-plan.md",
        "docs/plans/codegen-regression-analysis.md",
        "docs/plans/iteration-economics-critique.md",
    ):
        assert rel in swept, f"{rel} is outside the sweep scope"
    assert sum(1 for _, attributed in _mentions() if attributed) >= 5, (
        "no attributed code-writer-v1 line was found — the enumeration broke"
    )
