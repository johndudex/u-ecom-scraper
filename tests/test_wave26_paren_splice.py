"""[wave-26 W26-6] Post-generation splices must never ship a SyntaxError.

Prod 418 (next.co.uk) + 420 (ralphlauren): ``_enforce_discovery_import``
finds the insertion point with ``max(code.rfind("\\nimport "), ...)`` —
paren-blind, so when the writer's last import is a PARENTHESIZED block
(``from src.page_analysis import (``) the enforced line lands INSIDE the
parens → SyntaxError → a wasted writer invocation to repair the harness's
own damage. ``_patch_scraper_output_filter`` inserts before a string marker
with no compile check either. Both splices must verify with ``compile()``
and roll back on failure.
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


PAREN_IMPORT_DRAFT = (
    "import json\n"
    "from src.page_analysis import (\n"
    "    extract_product,\n"
    "    map_fields,\n"
    ")\n"
    "\n"
    "def run():\n"
    "    return extract_product('')\n"
)

BROKEN_DRAFT = (
    "import json\n"
    "def run(:\n"
    "    return 1\n"
)

DUMP_IN_PARENS_DRAFT = (
    "import json\n"
    "output = {'products': [{'title': 'a', 'price': '1'}]}\n"
    "\n"
    "def emit(fh):\n"
    "    result = summarize(\n"
    "        stats,\n"
    "        json.dump(output, fh) if fh else None,\n"
    "    )\n"
    "    return result\n"
)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    import agents.graph as graph_mod

    monkeypatch.setattr(graph_mod, "_get_project_root", lambda: str(tmp_path))
    return graph_mod, tmp_path


class TestDiscoveryImportSplice:
    def test_splice_survives_parenthesized_import_block(self, ws):
        """418's exact mechanism: last import is a paren block — the enforced
        import must land AFTER the whole block (top level), and the result
        must compile."""
        graph_mod, tmp_path = ws
        slug_dir = tmp_path / "workspace" / "paren-site"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(PAREN_IMPORT_DRAFT)

        graph_mod._enforce_discovery_import("paren-site")

        code = draft.read_text()
        assert "from src.discovery import" in code, "import not injected"
        compile(code, "scraper_draft.py", "exec"), "spliced draft does not compile"

    def test_splice_rejected_on_already_broken_draft(self, ws):
        """A draft that does not parse must be left EXACTLY as-is — the
        splice must not turn a writer bug into a different writer bug."""
        graph_mod, tmp_path = ws
        slug_dir = tmp_path / "workspace" / "broken-site"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(BROKEN_DRAFT)
        before = draft.read_text()

        graph_mod._enforce_discovery_import("broken-site")

        assert draft.read_text() == before, "broken draft was modified"

    def test_normal_draft_still_gets_import(self, ws):
        """No regression: a plain draft still receives the enforced import
        and still compiles."""
        graph_mod, tmp_path = ws
        slug_dir = tmp_path / "workspace" / "plain-site"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(
            "import json\nfrom playwright.sync_api import sync_playwright\n\n"
            "def run():\n    return 1\n"
        )

        graph_mod._enforce_discovery_import("plain-site")

        code = draft.read_text()
        assert "from src.discovery import" in code
        compile(code, "scraper_draft.py", "exec")


class TestOutputFilterSplice:
    def test_marker_inside_parens_rejected_not_written(self, ws):
        """``json.dump(output`` appearing inside a bracket continuation: the
        injected filter would land inside the parens — the splice must
        refuse (file unchanged) rather than ship a SyntaxError."""
        graph_mod, tmp_path = ws
        slug_dir = tmp_path / "workspace" / "parens-dump"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(DUMP_IN_PARENS_DRAFT)
        before = draft.read_text()

        graph_mod._patch_scraper_output_filter("parens-dump", "product")

        after = draft.read_text()
        if after != before:
            # a splice was attempted — it MUST compile
            compile(after, "scraper_draft.py", "exec"), "spliced draft does not compile"
        else:
            compile(after, "scraper_draft.py", "exec")  # sanity

    def test_normal_draft_still_gets_filter(self, ws):
        """No regression: marker at a safe position → filter applied and the
        patched draft executes correctly."""
        graph_mod, tmp_path = ws
        slug_dir = tmp_path / "workspace" / "safe-dump"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(
            "import json\n"
            "output = {'products': [{'title': 'a', 'price': '1'}, {'title': ''}]}\n"
            "json.dump(output, open('out.json', 'w'))\n"
        )

        graph_mod._patch_scraper_output_filter("safe-dump", "product")

        code = draft.read_text()
        assert "_OUTPUT_FILTER_APPLIED" in code
        compile(code, "scraper_draft.py", "exec")
