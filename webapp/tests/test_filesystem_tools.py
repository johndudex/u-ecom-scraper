"""[job-71 popsockets] code_writer tooling must be able to read the templates.

Two related tool defects burned 2 of that job's 3 codegen cycles:

1. ``search_content`` walked its base path with ``os.walk`` — which iterates
   ZERO times when the base is a FILE — and returned a false
   ``No matches for pattern ...``. The writer passed template FILE paths
   ~20 times, concluded "the search tool has issues with this file", and
   burned its budget on probe-script workarounds instead of writing the
   draft.
2. ``read_file`` hard-truncates at 50K chars, and both navigation templates
   exceed 61K — the truncated advice pointed at ``search_content``, which
   (per defect 1) was broken on exactly those files. The middle of the
   template was unreachable by any tool.
"""
from __future__ import annotations

import pytest

from webapp.agents.tools.filesystem_tools import get_filesystem_tools


@pytest.fixture()
def ws(tmp_path):
    """A tiny project root with one oversized template-like file."""
    root = tmp_path / "proj"
    (root / "templates").mkdir(parents=True)
    lines = [f"def snippet_{i}():\n    return {i}\n" for i in range(4000)]
    content = "".join(lines)
    (root / "templates" / "big_template.py").write_text(content)
    (root / "templates" / "small.py").write_text("TARGET_NEEDLE = 'def real_match()'\n")
    assert len(content) > 50_000
    return root


def _tools(root):
    return {t.name: t for t in get_filesystem_tools(project_root=str(root))}


class TestSearchContentOnFiles:
    def test_search_content_accepts_a_file_path(self, ws):
        """Passing a FILE path must search that file, not report false 'No
        matches' (os.walk on a file yields nothing)."""
        sc = _tools(ws)["search_content"]
        out = sc.invoke(
            {
                "pattern": "def real_match",
                "path": str(ws / "templates" / "small.py"),
            }
        )
        assert "No matches" not in out
        assert "real_match" in out

    def test_search_content_on_missing_file_is_honest(self, ws):
        sc = _tools(ws)["search_content"]
        out = sc.invoke(
            {
                "pattern": "anything",
                "path": str(ws / "templates" / "nope.py"),
            }
        )
        assert "No matches" in out or "not found" in out.lower()

    def test_search_content_directory_still_works(self, ws):
        sc = _tools(ws)["search_content"]
        out = sc.invoke({"pattern": "def snippet_39", "path": "templates"})
        assert "snippet_39" in out


class TestReadFilePaging:
    def test_oversized_file_mentions_offset(self, ws):
        rf = _tools(ws)["read_file"]
        out = rf.invoke({"path": str(ws / "templates" / "big_template.py")})
        assert "TRUNCATED" in out
        assert "offset" in out

    def test_offset_reads_the_later_portion(self, ws):
        rf = _tools(ws)["read_file"]
        path = str(ws / "templates" / "big_template.py")
        total = len(path and open(path).read())
        tail = rf.invoke({"path": path, "offset": total - 1_000})
        assert "TRUNCATED" not in tail
        # the head must NOT be in the offset view
        assert "def snippet_0():" not in tail
        assert "def snippet_3999():" in tail

    def test_offset_out_of_range_is_honest(self, ws):
        rf = _tools(ws)["read_file"]
        out = rf.invoke(
            {"path": str(ws / "templates" / "big_template.py"), "offset": 10**9}
        )
        assert "out of range" in out

    def test_full_file_readable_via_paging(self, ws):
        rf = _tools(ws)["read_file"]
        path = str(ws / "templates" / "big_template.py")
        seen = []
        offset = 0
        for _ in range(10):
            chunk = rf.invoke({"path": path, "offset": offset})
            if "TRUNCATED" in chunk:
                seen.append(chunk.split("\n\n... [TRUNCATED")[0])
                offset += 50_000
                continue
            seen.append(chunk)
            break
        else:
            pytest.fail("paging never terminated")
        whole = "".join(seen)
        assert "def snippet_0():" in whole
        assert "def snippet_3999():" in whole
