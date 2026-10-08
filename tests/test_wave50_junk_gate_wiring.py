"""[wave-50 T5e] The nav-tree junk gate in run_execution: a discovery-mode
run whose output rows are sibling section links shallower than the listing
must FAIL with a named directive — junk rows carry core fields (nav title +
defaulted price), so the F9 emptiness gate alone cannot see them.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _write_run(tmp_path, rows, seed_urls=None):
    out = tmp_path / "output_test.json"
    out.write_text(json.dumps({"products": rows}), encoding="utf-8")
    if seed_urls is not None:
        (tmp_path / "input_urls.json").write_text(
            json.dumps({"urls": seed_urls}), encoding="utf-8"
        )
    return str(out)


def _junk_rows():
    return [
        {"title": t, "url": f"https://books.example/personalized-books/{t}",
         "price": 5.0}
        for t in ("new-in", "bestsellers", "gifts", "baby", "toddler")
    ]


LISTING = "https://books.example/personalized-books/kids/age/0-3"


@pytest.fixture
def gate():
    from agents.nodes.run_execution import _shallow_section_gate

    return _shallow_section_gate


class TestGate:
    def test_junk_rows_fail_with_named_directive(self, gate, tmp_path):
        out = _write_run(tmp_path, _junk_rows())
        msg = gate(out, LISTING, "list_page")
        assert msg.startswith("Nav-tree junk bar:")
        assert "5/5" in msg
        assert "personalized-books" in msg
        assert "not item pages" in msg

    def test_real_item_rows_pass(self, gate, tmp_path):
        rows = [
            {"title": f"Book {i}", "price": 18.99,
             "url": f"https://books.example/personalized-products/slug-{i}"}
            for i in range(5)
        ]
        out = _write_run(tmp_path, rows)
        assert gate(out, LISTING, "list_page") == ""

    def test_seed_urls_are_candidates_when_rows_carry_no_urls(self, gate, tmp_path):
        rows = [{"title": t, "price": 5.0}
                for t in ("new-in", "bestsellers", "gifts")]
        out = _write_run(
            tmp_path, rows,
            seed_urls=[f"https://books.example/personalized-books/{t}"
                       for t in ("new-in", "bestsellers", "gifts", "baby")],
        )
        msg = gate(out, LISTING, "list_page")
        assert msg.startswith("Nav-tree junk bar:")

    def test_url_list_now_in_scope(self, gate, tmp_path):
        """[wave-55 C2] url_list used to be exempt (its seeds are
        user-provided), which is how job 1229 shipped unsubmitted rows as
        COMPLETED. Its anchor is the submitted PDP — grounded the same way a
        list_page anchor is."""
        out = _write_run(tmp_path, _junk_rows())
        assert gate(out, LISTING, "url_list").startswith("Nav-tree junk bar:")

    def test_search_and_navigation_out_of_scope(self, gate, tmp_path):
        """Depth judgment needs an anchor that IS the walked listing — only
        list_page guarantees that. search/navigation anchors are the submitted
        URL (often a deep category), so those modes never trip this gate
        (their junk nets are the T5d gate + core-value bar + F9)."""
        out = _write_run(tmp_path, _junk_rows())
        assert gate(out, LISTING, "search_term") == ""
        assert gate(out, LISTING, "navigation") == ""

    def test_missing_files_no_op(self, gate, tmp_path):
        assert gate(str(tmp_path / "nope.json"), LISTING, "list_page") == ""
        assert gate("", LISTING, "list_page") == ""

    def test_bad_listing_url_no_op(self, gate, tmp_path):
        out = _write_run(tmp_path, _junk_rows())
        assert gate(out, "", "list_page") == ""

    def test_relative_seed_urls_counted(self, gate, tmp_path):
        out = _write_run(
            tmp_path, [],
            seed_urls=["/personalized-books/new-in",
                       "/personalized-books/bestsellers",
                       "/personalized-books/gifts"],
        )
        assert gate(out, LISTING, "list_page").startswith("Nav-tree junk bar:")


class TestWiring:
    def test_success_path_calls_the_gate_after_f9(self):
        """The gate must sit in the browser_service success path, AFTER the
        F9 emptiness gate, as its own FAILED verdict."""
        src = (ROOT / "webapp" / "agents" / "nodes" / "run_execution.py").read_text(
            encoding="utf-8"
        )
        assert "_sh = _shallow_section_gate(" in src
        f9_at = src.find("if _q:")
        gate_at = src.find("_sh = _shallow_section_gate(")
        assert 0 < f9_at < gate_at
        assert "Nav-tree junk bar:" in src

    def test_detector_is_site_agnostic(self):
        """No site names, no domain lists — structural signals only."""
        src = (ROOT / "src" / "discovery_quality.py").read_text(encoding="utf-8")
        for site_word in ("wonderbly", "jcrew", "tatcha", "briscoes",
                          "example.com", "shopify", "algolia"):
            assert site_word not in src, f"site-specific token leaked: {site_word}"
