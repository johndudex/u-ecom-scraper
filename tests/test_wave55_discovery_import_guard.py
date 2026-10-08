"""[wave-55 C1] _enforce_discovery_import must not inject an unresolvable import.

Prod evidence (audit 2026-10-05, C1): the amplifier that turned a cosmetic
``workspace/src/`` shadow into 30 crashed jobs. ``_enforce_discovery_import``
unconditionally injected ``from src.discovery import ...`` into every draft
(``_PATCHES_ENABLED`` is hard-wired True). Under the shadow that import
CANNOT resolve — ``workspace/src/`` holds a 4-module abiinteriors clone and
no ``discovery.py`` — so a draft that would have run fine on its inline
pagination was converted into a guaranteed import-time crash. 27 prod jobs
died on exactly ``ModuleNotFoundError: No module named 'src.discovery'``
(1183 dollartree's terminal error_message IS that traceback).

Contract: the injection happens only when ``src.discovery`` actually resolves
from the draft's import path — the pipeline package at the project root, or a
site-local ``src/`` package next to the draft. Otherwise the draft keeps its
inline pagination, which runs.

[round 6] Resolution is first-package-wins, not file-existence: the base order
comes from the anchor line the draft carries, and the first ``src`` package on
it captures the name. The pipeline's own ``src/`` is a namespace portion (no
``__init__.py``), so a REGULAR ``src/`` package anywhere on the path — the
``workspace/src/`` clone the root also ships alongside — shadows it even
though ``src/discovery.py`` is sitting right there at the root.
"""
from __future__ import annotations

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

SLUG = "notino-lt"

# The pipeline package as the image actually ships it — ``src/`` with NO
# ``__init__.py`` (a namespace portion), which is why a REGULAR ``src/``
# package on any other path entry captures the name.
_DISCOVERY_STUB = (
    "def discover_item_urls(*a, **k):\n"
    "    return []\n"
    "def config_for_load_more(*a, **k):\n"
    "    return {}\n"
)

# A draft with hand-rolled pagination — the shape the enforcement hook exists
# to convert over to src.discovery when it CAN.
_INLINE_PAGINATION_DRAFT = (
    "import os\n"
    "import sys\n"
    "\n"
    "def _click_load_more(page):\n"
    "    return page\n"
    "\n"
    "def main():\n"
    "    return []\n"
)

# The same draft carrying the anchor the pre-wave-55 templates shipped — the
# line the audit bans. For a draft at workspace/{slug}/scraper_draft.py it
# puts workspace/ at sys.path[0], which is the base a writer-minted
# workspace/src/ package sits on.
_ANCHORED_DRAFT = (
    "import os\n"
    "import sys\n"
    'sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))\n'
    "\n"
    "def _click_load_more(page):\n"
    "    return page\n"
    "\n"
    "def main():\n"
    "    return []\n"
)


def _proj(tmp_path):
    root = tmp_path / "app"
    slug_dir = root / "workspace" / SLUG
    slug_dir.mkdir(parents=True)
    (slug_dir / "scraper_draft.py").write_text(_INLINE_PAGINATION_DRAFT)
    return root


def _run(root, monkeypatch):
    import agents.graph as graph_mod

    monkeypatch.setattr(graph_mod, "_get_project_root", lambda: str(root))
    graph_mod._enforce_discovery_import(SLUG)
    return (root / "workspace" / SLUG / "scraper_draft.py").read_text()


class TestInjectionGuard:
    def test_poisoned_shadow_skips_injection(self, tmp_path, monkeypatch):
        """The PROD shape (audit C1): the pipeline package IS at the root —
        that alone must not read as resolvable, or the gate can never fire.
        The draft carries the pre-wave-55 parent-relative anchor, so
        ``workspace/`` is ``sys.path[0]`` and the stale clone's
        ``src/__init__.py`` there captures the name (first-package-wins): the
        root's ``discovery.py`` is never consulted. The import does not
        resolve, so the draft keeps its inline pagination instead of being
        converted into a guaranteed import crash."""
        root = _proj(tmp_path)
        (root / "src").mkdir()
        (root / "src" / "discovery.py").write_text(_DISCOVERY_STUB)
        shadow = root / "workspace" / "src"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("")
        (shadow / "http_fetch.py").write_text("# abiinteriors workspace-local build\n")
        (root / "workspace" / SLUG / "scraper_draft.py").write_text(_ANCHORED_DRAFT)

        code = _run(root, monkeypatch)

        assert "from src.discovery import" not in code, (
            "an unresolvable import must not be injected — inline pagination runs"
        )
        assert "_click_load_more" in code

    def test_slug_local_src_package_without_discovery_skips_injection(
        self, tmp_path, monkeypatch
    ):
        """A slug-local ``workspace/{slug}/src/`` REGULAR package — which the
        slug-scope guard explicitly permits a writer to mint — beats the
        root's namespace-only ``src/`` no matter which path entry leads: a
        regular package captures the name ahead of a namespace portion. The
        name is therefore decided inside the slug dir, where there is no
        ``discovery.py``, so injecting would re-create the 1183-class crash."""
        root = _proj(tmp_path)
        (root / "src").mkdir()
        (root / "src" / "discovery.py").write_text(_DISCOVERY_STUB)
        local_src = root / "workspace" / SLUG / "src"
        local_src.mkdir()
        (local_src / "__init__.py").write_text("")

        code = _run(root, monkeypatch)

        assert "from src.discovery import" not in code, (
            "the slug-local package shadows the pipeline's src/ — injecting "
            "would crash the draft at import"
        )

    def test_no_src_at_all_skips_injection(self, tmp_path, monkeypatch):
        root = _proj(tmp_path)

        code = _run(root, monkeypatch)

        assert "from src.discovery import" not in code

    def test_resolvable_pipeline_package_still_injects(self, tmp_path, monkeypatch):
        root = _proj(tmp_path)
        src = root / "src"
        src.mkdir()
        (src / "discovery.py").write_text(_DISCOVERY_STUB)

        code = _run(root, monkeypatch)

        assert "from src.discovery import discover_item_urls" in code
        assert "_DISCOVERY_IMPORT_APPLIED" in code

    def test_broken_draft_hits_the_rollback_not_the_gate(
        self, tmp_path, monkeypatch, caplog
    ):
        """A resolvable src.discovery + a draft that does not parse: the
        file must be left alone by the _safe_splice_write compile refusal —
        NOT by the resolution gate short-circuiting first. Without the log
        assertion this pin is tautological (wave-26's
        test_splice_rejected_on_already_broken_draft passed for exactly that
        wrong reason before it planted a resolvable src)."""
        root = _proj(tmp_path)
        src = root / "src"
        src.mkdir()
        (src / "discovery.py").write_text(
            "def discover_item_urls(*a, **k):\n    return []\n"
        )
        draft = root / "workspace" / SLUG / "scraper_draft.py"
        draft.write_text("import json\ndef run(:\n    return 1\n")
        before = draft.read_bytes()

        with caplog.at_level(logging.INFO, logger="agents.graph"):
            code = _run(root, monkeypatch)

        assert code == before.decode(), "broken draft was modified"
        with pytest.raises(SyntaxError):
            compile(code, "scraper_draft.py", "exec")
        assert "rejected — spliced draft does not compile" in caplog.text, (
            "the rollback never ran — the gate swallowed the splice, which is "
            "not the branch this pin exists to hold"
        )
        assert "does not resolve" not in caplog.text

    def test_draft_local_src_package_still_injects(self, tmp_path, monkeypatch):
        """A site-local ``src/`` package INSIDE the job's slug dir resolves via
        the draft's own directory — that one is legitimate (slug-scoped) and
        the injection may proceed."""
        root = _proj(tmp_path)
        local_src = root / "workspace" / SLUG / "src"
        local_src.mkdir()
        (local_src / "discovery.py").write_text(
            "def discover_item_urls(*a, **k):\n    return []\n"
        )

        code = _run(root, monkeypatch)

        assert "from src.discovery import" in code

    def test_already_compliant_draft_is_untouched(self, tmp_path, monkeypatch):
        """A draft that already imports src.discovery is left alone even when
        the module does not resolve (the enforcement hook is a drift backstop,
        not a validator — deleting a writer's own import is a different fix)."""
        root = _proj(tmp_path)
        draft = root / "workspace" / SLUG / "scraper_draft.py"
        draft.write_text(
            "from src.discovery import discover_item_urls\n"
            "\n"
            "def main():\n"
            "    return discover_item_urls()\n"
        )

        code = _run(root, monkeypatch)

        assert code == draft.read_text()
        assert "discover_item_urls()" in code
