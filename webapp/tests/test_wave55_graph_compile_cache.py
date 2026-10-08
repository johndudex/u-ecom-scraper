"""[wave-55 R12 M6] The compiled scrape graph is built once per process.

Audit (2026-10-05): ``LangGraphService.build_graph`` re-ran
``build_scrape_graph(checkpointer=...)`` on every call — 49 compiles for 30
job dispatches. The docstring claimed "cached by lru_cache, so repeated calls
are cheap", but only the CHECKPOINTER is cached; the ~26-node compile ran
every time. The cache now sits on the compiled graph itself (keyed on the
already-cached checkpointer singleton) inside ``scraper.services``.

``build_scrape_graph`` itself stays UNCACHED on purpose: the graph-module
tests (topology/recycle/traverse) call it directly with patched nodes, and a
cached binding there would freeze a patched node into later tests. All four
production call sites go through ``LangGraphService.build_graph``, so this is
where the once-per-process guarantee belongs.

The spies patch the names the real build would import (function-local
imports resolve through ``sys.modules`` at call time), so this test never
compiles the real graph or touches a Postgres checkpointer.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_graph_compile_cache.py -q"
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from scraper.services import LangGraphService  # noqa: E402

_CACHE_HOLDER = "scraper.services"


def _clear_cache() -> None:
    """Drop the module-level compiled-graph cache between tests.

    ``getattr`` because the RED run predates the cache existing at all.
    """
    import importlib

    holder = importlib.import_module(_CACHE_HOLDER)
    cache_clear = getattr(getattr(holder, "_compiled_graph", None), "cache_clear", None)
    if cache_clear is not None:
        cache_clear()


@pytest.fixture(autouse=True)
def _fresh_compile_cache():
    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture
def _spied_build(monkeypatch) -> MagicMock:
    """A counting stand-in for the real compile, plus a checkpointer stub."""
    compiled = object()
    spy = MagicMock(return_value=compiled)
    import agents.checkpointer as checkpointer_mod
    import agents.graph as graph_mod

    monkeypatch.setattr(checkpointer_mod, "get_checkpointer", MagicMock(return_value=object()))
    monkeypatch.setattr(graph_mod, "build_scrape_graph", spy)
    return spy


class TestCompileHappensOncePerProcess:
    def test_two_dispatches_compile_once(self, _spied_build):
        first = LangGraphService.build_graph()
        second = LangGraphService.build_graph()

        assert _spied_build.call_count == 1, (
            "build_scrape_graph ran on every build_graph() call — the audit's "
            "49-compiles-for-30-dispatches waste"
        )
        assert first is second

    def test_three_dispatches_still_compile_once(self, _spied_build):
        graphs = [LangGraphService.build_graph() for _ in range(3)]

        assert _spied_build.call_count == 1
        assert graphs[0] is graphs[1] is graphs[2]

    def test_checkpointer_is_resolved_only_until_cached(self, _spied_build):
        """get_checkpointer is itself lru_cached, so it may be consulted on
        the first call only — but the compiled graph must never be rebuilt
        even if the checkpointer factory is somehow asked again."""
        LangGraphService.build_graph()
        LangGraphService.build_graph()

        assert _spied_build.call_count == 1

    def test_the_cache_lives_in_scraper_services(self):
        """The once-per-process guarantee is pinned by the module attribute
        this test clears between tests — remove it and the first two tests
        fail (which is exactly how the RED run proved the drift)."""
        import importlib

        holder = importlib.import_module(_CACHE_HOLDER)
        assert hasattr(holder, "_compiled_graph"), (
            "scraper.services no longer exposes the _compiled_graph cache "
            "this file pins"
        )


class TestTheCompileLogIsOneLine:
    """Audit clause 3: the compile followed its one summary line with a
    per-node INFO dump — ~26 extra lines per compile that greps read as park
    / deadman events. One compile, one line; anything per-node must be
    logger.debug."""

    def test_build_scrape_graph_emits_only_its_summary_info_line(self):
        import inspect

        import agents.graph as graph_mod

        infos = [
            line.strip()
            for line in inspect.getsource(graph_mod.build_scrape_graph).splitlines()
            if "logger.info(" in line
        ]
        assert infos == [
            'logger.info("Scrape graph compiled with %d nodes", '
            "len(workflow.nodes))"
        ], (
            "build_scrape_graph logs more than its one summary INFO line — "
            "delete or demote anything per-node to logger.debug (M6): "
            f"{infos}"
        )
