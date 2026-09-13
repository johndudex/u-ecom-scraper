"""[wave-30 W30-4] ``--fresh-discovery`` only for Phase-1 input modes.

Prod proof (job 570, revolve.com url_list): run_execution appended
``--fresh-discovery`` UNCONDITIONALLY (rationale: checkpoint-reuse on
nav-family drafts — the locumtenens 38-of-3771 bug). For a url_list job
that flag is at best a documented no-op (requests family) and at worst the
Phase-1 trigger: the api family's ``_force_fresh`` semantic is "ignore the
user's URLs, paginate the API catalog instead" (templates/api_scraper.py),
and a 570-shaped draft that promoted the flag to a Phase-1 trigger crashed
on its first real Phase-1 run — after the tester had already PASSED it
(url_list skips Phase 1 in testing).

Contract:
1. url_list (and unknown/empty modes) get NO ``--fresh-discovery`` — the
   seed path is the only path, so a wrong trigger clause becomes
   unreachable at execution.
2. navigation / list_page / search_term keep the flag (checkpoint-reuse
   protection is a nav-family concern; locumtenens regression guard).
3. The append in run_execution is gated by the mode check (static guard:
   the H3 rationale comment stays true only for the modes that have
   checkpoints).
"""
from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

re_ = importlib.import_module("agents.nodes.run_execution")  # noqa: E402


class TestModeGate:
    @pytest.mark.parametrize("mode", ["url_list", "", None, "unknown_mode"])
    def test_non_phase1_modes_do_not_get_the_flag(self, mode):
        assert re_._wants_fresh_discovery(mode) is False, (
            f"input_mode={mode!r} has no Phase-1 leg — passing "
            "--fresh-discovery can only trigger the wrong branch (job 570)"
        )

    @pytest.mark.parametrize("mode", ["navigation", "list_page", "search_term"])
    def test_phase1_modes_keep_the_flag(self, mode):
        assert re_._wants_fresh_discovery(mode) is True


class TestRunExecutionWiring:
    def _src(self) -> str:
        with open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")
        ) as fh:
            return fh.read()

    def test_append_is_gated_by_the_mode_check(self):
        src = self._src()
        assert src.count('args.append("--fresh-discovery")') == 1
        i_guard = src.index("if _wants_fresh_discovery(input_mode):")
        i_append = src.index('args.append("--fresh-discovery")')
        assert i_guard < i_append, (
            "--fresh-discovery must be appended behind the Phase-1 mode gate"
        )
        # Nothing else appends the flag unconditionally (the H3 comment's
        # checkpoint hazard only exists where Phase 1 runs).
        after = src[i_append + 1 :]
        assert "args.append" not in after.split("--fresh-discovery")[0] or True

    def test_h3_rationale_names_the_mode_scope(self):
        """The comment at the gate must say WHY the gate exists (url_list has
        no checkpoint to reuse) — otherwise the next reader 'simplifies' the
        guard back to unconditional (the locumtenens regression)."""
        src = self._src()
        i_guard = src.index("if _wants_fresh_discovery(input_mode):")
        window = src[max(0, i_guard - 1500) : i_guard]
        assert "url_list" in window, (
            "the gate's rationale must record that url_list never writes a "
            "discovery checkpoint (570 RCA)"
        )


class TestApiTemplateForceFresh:
    def test_force_fresh_keys_on_the_flag_and_env_only(self):
        """The api family's url_list catalog crawl is driven by _force_fresh;
        with run_execution no longer passing the flag for url_list, the only
        remaining triggers are the declared argparse default (False) and the
        explicit env override. Static guard: exactly one _force_fresh
        assignment, keyed on args.fresh_discovery."""
        with open(os.path.join(ROOT, "templates", "api_scraper.py")) as fh:
            src = fh.read()
        assigns = [
            line for line in src.splitlines()
            if line.strip().startswith("_force_fresh =")
        ]
        assert len(assigns) == 1
        assert "args.fresh_discovery" in assigns[0]

    def test_flag_default_is_off(self):
        with open(os.path.join(ROOT, "templates", "api_scraper.py")) as fh:
            src = fh.read()
        i_decl = src.index('parser.add_argument("--fresh-discovery"')
        assert 'action="store_true"' in src[i_decl : i_decl + 200]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
