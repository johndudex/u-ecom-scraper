"""[wave-25e 25e-b] The bounded-embed FLIP + finisher honesty + pre-seed.

E2a [RISKY]: ``CODE_WRITER_EMBED_MODE`` defaults to ``bounded``. With the
flip live, bounded applies exactly when D5's wiring requests it — EOW fix
cycles on drafts ≥40K. First-cycle pristine templates stay full (explicit
"full" from the classify decision always wins at the resolver) and small
drafts keep the byte-identical legacy embed (nothing to elide).

E4 [SAFE]: the finisher seed stops claiming the whole draft is "already in
your context as the base" when the embed is bounded — it names
head+tail+map and the read_file(line=) redirection instead. Under full the
old sentence stays true and is kept. The five pinned seed tokens (rev-2)
survive: current strategy, strategies_tried, FINISH framing, tester
feedback, and the adapt-or-justify order.

E5 [SAFE]: on an EOW fix cycle the failure-targeted draft region is spliced
into the seed (the truncation-exempt task spec) — deterministic, from the
REAL remediation vocabulary {mapping, strategy, scraper}, capped at
CODE_WRITER_PRESEED_MAX_CHARS, marker-guarded for idempotency. This is the
mechanism that makes bounded embeds safe rather than blind.

Run: docker compose exec -T django sh -c "cd /app && pytest tests/test_wave25e_embed_flip.py -q"
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

graph = importlib.import_module("agents.graph")  # noqa: E402
from webapp.agents import subagents as sub  # noqa: E402

SLUG = "flip-site-com"


def _big_draft(target: int = 60_000) -> str:
    def _filler(i: int, pad: int) -> str:
        return (
            f"def _filler_fn_{i}(soup):\n"
            f'    """Filler function {i}."""\n'
            "    _blob = '" + ("x" * pad) + "'\n"
            f"    return len(_blob) + {i}\n\n\n"
        )

    parts = ["import argparse\n", "DEFAULT_LISTING_URL = 'https://x/c'\n", "\n\n"]
    total = sum(len(p) for p in parts)
    i = 0
    while total < target:
        fn = _filler(i, 2_400)
        parts.append(fn)
        total += len(fn)
        i += 1
    parts.append("def main() -> int:\n    return 0\n")
    return "".join(parts)


SMALL_DRAFT = "PRICE_SELECTOR = '.p'\n\n\ndef _extract_price(node):\n    return '9'\n"


# ── E2a: the flip ──────────────────────────────────────────────────────────


class TestE2aFlip:
    def test_embed_mode_default_is_bounded_after_flip(self):
        """THE sole default-asserting test (round-2 finding B6): D5's lazy-read
        test pins mechanics only; THIS test owns the value."""
        from django.conf import settings

        val = str(
            getattr(settings, "CODE_WRITER_EMBED_MODE", "full")
        ).strip().lower()
        assert val == "bounded"
        assert sub._resolve_embed_mode("") == "bounded"

    def test_retry_cycle_gets_bounded_embed_by_default(self, caplog):
        """The flip's whole point: an EOW fix cycle on a large draft ships
        the bounded embed with NO env override."""
        import logging

        big = _big_draft()
        with caplog.at_level(logging.INFO):
            agent = sub.create_code_writer(
                site_slug=SLUG, template_code=big,
                embed_mode="bounded", embed_kind="draft",
            )
        stats = agent._embed_stats
        assert stats["mode"] == "bounded"
        assert stats["embed_chars"] < len(big)
        assert any(
            "mode=bounded" in r.getMessage() and "[WRITER-EMBED]" in r.getMessage()
            for r in caplog.records
        )

    def test_first_cycle_still_full_embed(self):
        """E2a boundary: a first cycle's classify decision is explicit "full"
        (no EOW base) — the bounded default must NOT override it."""
        big = _big_draft()
        agent = sub.create_code_writer(
            site_slug=SLUG, template_code=big, embed_mode="full",
            embed_kind="file",
        )
        stats = agent._embed_stats
        assert stats["mode"] == "full"
        assert stats["embed_chars"] >= len(big)

    def test_small_eow_draft_still_full_embed(self):
        """A 17K EOW base is below head+tail — even a granted bounded request
        degrades to the exact legacy embed (nothing to elide)."""
        draft = "x = 1\n" * 2_833  # ~17K
        agent = sub.create_code_writer(
            site_slug=SLUG, template_code=draft, embed_mode="bounded",
            embed_kind="draft",
        )
        stats = agent._embed_stats
        assert stats["embed_chars"] >= len(draft), (
            "below the elide threshold the embed must carry the WHOLE draft"
        )


# ── E4: finisher seed honesty ──────────────────────────────────────────────


class FakeAgent:
    def __init__(self):
        self.invocations = []

    def invoke(self, messages, *a, **k):
        self.invocations.append(messages)
        return {"messages": [types.SimpleNamespace(content="done")]}


@pytest.fixture()
def fin_env(tmp_path, monkeypatch):
    """Workspace + hermetic graph side effects for _run_draft_finisher."""
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True)
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(graph, "_start_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_stop_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_log_event_row", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_persist_agent_logs", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_log_writer_embed_row", lambda *a, **k: None)
    import src.artifacts as art

    monkeypatch.setattr(art, "write", lambda key, data: len(data))
    return tmp_path


def _fin_state(**over):
    base = {
        "job_id": 569,
        "site_slug": SLUG,
        "scraper_analysis": {
            "scraping_method": "http_navigation",
            "strategies_tried": ["http_requests"],
        },
        "test_report": {"feedback_for_writer": "phase-1 discovery 403"},
    }
    base.update(over)
    return base


def _run_finisher(env, state, draft_text):
    (env / "workspace" / SLUG / "scraper_draft.py").write_text(draft_text)
    agent = FakeAgent()
    import src.artifacts as art

    orig_exists = getattr(art, "exists", None)

    class _FakeAgentWithStats:
        _embed_stats = None

        def invoke(self, messages, *a, **k):
            agent.invocations.append(messages)
            return {"messages": [types.SimpleNamespace(content="done")]}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(graph, "create_code_writer", lambda *a, **k: _FakeAgentWithStats())
    try:
        out = graph._run_draft_finisher(state, {}, SLUG, 569)
    finally:
        monkey.undo()
    seed = str(agent.invocations[0]) if agent.invocations else ""
    return out, seed


class TestE4FinisherSeedHonesty:
    def test_finisher_seed_names_map_not_full_context(self, fin_env):
        """Bounded finisher: the seed must NOT claim the whole draft is in
        context — it names head+tail+map and the line= redirection."""
        out, seed = _run_finisher(fin_env, _fin_state(), _big_draft())
        assert out is not None, "salvage contract unchanged"
        assert "already in your context as the base" not in seed
        assert "head+tail+map" in seed
        assert "read_file(line=" in seed

    def test_full_mode_finisher_seed_keeps_base_sentence(self, fin_env):
        """Under a FULL embed the old sentence is still TRUE — it stays."""
        out, seed = _run_finisher(fin_env, _fin_state(), SMALL_DRAFT)
        assert out is not None
        assert "already in your context as the base" in seed

    def test_finisher_seed_tokens_survive(self, fin_env):
        """The five pinned tokens (rev-2) survive the rewording."""
        _, seed = _run_finisher(fin_env, _fin_state(), _big_draft())
        assert "http_navigation" in seed, "current strategy missing"
        assert "http_requests" in seed, "strategies_tried missing"
        assert "FINISH" in seed.upper(), "finish framing missing"
        assert "403" in seed, "tester feedback missing"
        assert "adapt" in seed.lower(), "adapt-or-justify order missing"

    def test_finisher_uses_same_embed_mode_as_main_path(self):
        """Parity: the finisher derives its mode from classify_embed (the
        main path's helper) — no second inline mechanism."""
        body = inspect.getsource(graph._run_draft_finisher)
        assert "classify_embed(" in body
        assert "eow_active=None" in body, "the finisher golden is size-only"
        assert '"bounded" if len(' not in body, (
            "E4 must not add its own embed selection (plan: no second mechanism)"
        )
        assert "embed_kind=\"draft\"" in body or "embed_kind='draft'" in body


# ── E5: failure-targeted region pre-seed ───────────────────────────────────

E5_DRAFT = (
    "import argparse\n"
    "DEFAULT_LISTING_URL = 'https://x/c'\n"
    "\n"
    "def discover_item_urls(listing_url, min_tier):\n"
    "    return []\n"
    "\n"
    "def _get_next_page_url(soup):\n"
    "    return None\n"
    "\n"
    "def _extract_price(node):\n"
    "    return node.get_text()\n"
    "\n"
    "def main():\n"
    "    parser = argparse.ArgumentParser()\n"
    "    parser.add_argument('--sample')\n"
    "    return parser\n"
)


def _msg_list(content: str) -> list:
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content=content)]


class TestE5Preseed:
    def _block(self, tmp_path, state):
        draft = tmp_path / "scraper_draft.py"
        draft.write_text(E5_DRAFT)
        msgs = _msg_list("SEED TASK")
        out = graph._preseed_failure_region(state, msgs, str(draft))
        return out[0].content

    def test_mapping_remediation_preseeds_field_region(self, tmp_path):
        state = {"test_report": {"remediation": {"target": "mapping", "fields": ["price"]}}}
        block = self._block(tmp_path, state)
        assert "[FAILURE-REGION]" in block
        assert "_extract_price" in block
        assert "return node.get_text()" in block, "the region BODY rides the seed"
        assert "line=" in block, "rendered with line ranges for read_file(line=)"
        assert "discover_item_urls" not in block

    def test_strategy_target_preseeds_discovery_region(self, tmp_path):
        state = {"test_report": {"remediation": {"target": "strategy"}}}
        block = self._block(tmp_path, state)
        assert "[FAILURE-REGION]" in block
        assert "discover_item_urls" in block
        assert "_get_next_page_url" in block
        assert "argparse" in block, "main()'s CLI surface rides the seed"
        assert "_extract_price" not in block

    def test_discovery_coverage_signal_preseeds_discovery_region(self, tmp_path):
        """No remediation at all — but the tester reported discovery_coverage
        (a real test-report key): the discovery region still pre-seeds."""
        state = {"test_report": {"discovery_coverage": {"stop_reason": "max_pages"}}}
        block = self._block(tmp_path, state)
        assert "[FAILURE-REGION]" in block
        assert "discover_item_urls" in block

    def test_scraper_target_preseeds_nothing(self, tmp_path):
        state = {"test_report": {"remediation": {"target": "scraper", "fields": ["price"]}}}
        block = self._block(tmp_path, state)
        assert "[FAILURE-REGION]" not in block

    def test_no_remediation_preseeds_nothing(self, tmp_path):
        block = self._block(tmp_path, {"test_report": {}})
        assert "[FAILURE-REGION]" not in block
        assert block == "SEED TASK", "the seed is untouched"

    def test_preseed_is_capped(self, tmp_path):
        huge = (
            "def _extract_price(node):\n"
            "    _x = '" + ("p" * 30_000) + "'\n"
            "    return _x\n"
        )
        draft = tmp_path / "scraper_draft.py"
        draft.write_text(huge)
        state = {"test_report": {"remediation": {"target": "mapping", "fields": ["price"]}}}
        msgs = _msg_list("SEED TASK")
        out = graph._preseed_failure_region(state, msgs, str(draft))
        from django.conf import settings

        cap = int(
            getattr(settings, "CODE_WRITER_PRESEED_MAX_CHARS", 12_000)
        )
        assert len(out[0].content) <= cap + 1_000, (
            "the pre-seed is bounded — it replaces read round-trips, not adds them"
        )
        assert "SEED TASK" in out[0].content, "spliced INTO the seed"

    def test_preseed_splice_is_idempotent(self, tmp_path):
        draft = tmp_path / "scraper_draft.py"
        draft.write_text(E5_DRAFT)
        state = {"test_report": {"remediation": {"target": "mapping", "fields": ["price"]}}}
        msgs = _msg_list("SEED TASK")
        once = graph._preseed_failure_region(state, msgs, str(draft))
        twice = graph._preseed_failure_region(state, once, str(draft))
        assert twice[0].content == once[0].content, (
            "double-invoke must not duplicate the region block"
        )
        assert twice[0].content.count("[FAILURE-REGION]") == 1

    def test_preseed_setting_zero_disables(self, tmp_path):
        draft = tmp_path / "scraper_draft.py"
        draft.write_text(E5_DRAFT)
        state = {"test_report": {"remediation": {"target": "mapping", "fields": ["price"]}}}
        from django.test import override_settings

        with override_settings(CODE_WRITER_PRESEED_MAX_CHARS=0):
            out = graph._preseed_failure_region(state, _msg_list("SEED TASK"), str(draft))
        assert out[0].content == "SEED TASK"

    def test_preseed_wiring_is_fix_cycle_only(self):
        """Source contract: the splice rides the EOW fix-cycle condition in
        the writer node — first cycles are never touched."""
        body = inspect.getsource(graph._invoke_code_writer)
        assert "_preseed_failure_region(" in body
        cond = body.split("if _eow_active and _w24_fix:", 1)
        assert len(cond) == 2, "the splice must be gated on the EOW fix cycle"
        assert "_preseed_failure_region(" in cond[1].split("\n\n", 1)[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
