"""[wave-25e 25e-a] Embed redesign, SAFE tier — measure, map, budget, honesty.

E0: the embed and the per-turn input are MEASURED — one ``[WRITER-EMBED]``
log line at build time, the numbers carried on the agent object, one
SessionLog row per writer invocation written by the node (never scraped
from thread ContextVars — wave-32 D2 proved that path unreliable), and a
``[WRITER-TURN]`` per-pre-model-invocation total.

E1: ``draft_context`` grows an ast DRAFT MAP (bounded mode indexes what it
elides), a ``classify_embed`` helper factoring D5's inlined threshold
decisions at both call sites, and the staleness/navigation contract text.

E3: the embed counts against the truncation budget — BOTH gates (step-1's
early return and step-2's drop budget) subtract ``embed_chars``. Rev-2
BLOCKER: step-1 used to compare messages against raw max_chars, so
150K of messages + a 47K embed sailed through untrimmed.

E6: stale-copy honesty — edit_file's not-found reply, the CLI-contract
fixer strings, and code-writer.md stop claiming the writer can see (or
read back) what a bounded embed elides.

Run: docker compose exec -T django sh -c "cd /app && pytest tests/test_wave25e_embed_redesign.py -q"
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
from django.test import override_settings  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
)

from webapp.agents import draft_context as dc  # noqa: E402
from webapp.agents import subagents as sub  # noqa: E402

SP = "BASE SYSTEM PROMPT"


def _big_template(target: int = 60_000) -> str:
    """A parseable template >40K shaped like the real 587 base — a handful
    of LARGE functions — whose MIDDLE carries a function that neither the
    head nor the tail slice can show (the map's whole job)."""
    def _filler(i: int, pad: int) -> str:
        return (
            f"def _filler_fn_{i}(soup):\n"
            f'    """Filler function {i} for embed sizing."""\n'
            "    _blob = '" + ("x" * pad) + "'\n"
            f"    return len(_blob) + {i}\n\n\n"
        )

    fillers: list[str] = []
    total = 0
    i = 0
    while total < 25_000:
        fn = _filler(i, 2_400)
        fillers.append(fn)
        total += len(fn)
        i += 1
    middle = (
        "def _middle_price_fn(node):\n"
        '    """MIDDLE-ONLY-EXTRACTOR lives here."""\n'
        '    return "MIDDLE_BODY_TOKEN_12345"\n\n\n'
    )
    while total < target:
        fn = _filler(i, 2_400)
        fillers.append(fn)
        total += len(fn)
        i += 1
    return (
        "import argparse\n"
        "DEFAULT_LISTING_URL = 'https://example.com/c/all'\n"
        "\n\n" + "".join(fillers[: i // 2]) + middle + "".join(fillers[i // 2 :])
        + "def main() -> int:\n    return 0\n"
    )


# ── E0: measure the embed and the per-turn input ───────────────────────────


class TestE0Measure:
    def test_embed_size_is_logged_and_persisted(self, caplog):
        """Build-time: one [WRITER-EMBED] line and an _embed_stats attribute
        carrying the same numbers the node will persist."""
        big = _big_template()
        with override_settings(CODE_WRITER_EMBED_MODE="bounded"):
            with caplog.at_level(logging.INFO):
                agent = sub.create_code_writer(
                    site_slug="e0-site", template_code=big,
                    embed_mode="bounded", embed_kind="draft",
                )
        stats = getattr(agent, "_embed_stats", None)
        assert stats, "the agent must carry its embed measurements"
        assert stats["agent"] == "code_writer"
        assert stats["mode"] == "bounded"
        assert stats["template"] == "draft"
        assert stats["embed_chars"] > 25_000, "both slices + map are present"
        assert stats["embed_chars"] < len(big), (
            "a bounded embed must be SMALLER than the draft it indexes"
        )
        assert stats["system_prompt_chars"] >= stats["embed_chars"]
        rows = [r for r in caplog.records if "[WRITER-EMBED]" in r.getMessage()]
        assert rows, "the embed size must be logged at build time"
        msg = rows[0].getMessage()
        assert "agent=code_writer" in msg
        assert "mode=bounded" in msg and "template=draft" in msg
        assert f"{stats['embed_chars']:,}" in msg
        assert f"{stats['system_prompt_chars']:,}" in msg

    def test_full_mode_still_measured(self, caplog):
        big = _big_template()
        with caplog.at_level(logging.INFO):
            agent = sub.create_code_writer(
                site_slug="e0-site", template_code=big, embed_mode="full",
            )
        stats = getattr(agent, "_embed_stats", None)
        assert stats and stats["mode"] == "full"
        assert stats["template"] == "file", "default kind is the pristine file"
        assert stats["embed_chars"] >= len(big)

    @pytest.mark.django_db
    def test_node_persists_the_embed_row(self):
        """The NODE writes one [WRITER-EMBED] SessionLog row after the invoke
        returns — sizes from the agent object, job_id from the node's state."""
        from django.contrib.auth.models import User
        from scraper.models import ScrapeJob, SessionLog

        u = User.objects.create_user(username="_t_e0_node", password="x")
        job = ScrapeJob.objects.create(
            url="https://shop.example.com/c/mens", user=u, created_via="api",
            status="running", input_mode="list_page", page_type="product",
        )

        class _Agent:
            _embed_stats = {
                "agent": "code_writer", "mode": "bounded", "template": "draft",
                "embed_chars": 47_123, "system_prompt_chars": 99_456,
            }

        from webapp.agents import graph as g

        g._log_writer_embed_row(_Agent(), job.id)
        rows = list(
            SessionLog.objects.filter(job_id=job.id, agent="code_writer")
        )
        assert rows and "[WRITER-EMBED]" in rows[-1].content
        assert "47,123" in rows[-1].content and "99,456" in rows[-1].content
        assert "mode=bounded" in rows[-1].content

    @pytest.mark.django_db
    def test_no_embed_no_row(self):
        """An agent built without a template writes nothing."""
        from django.contrib.auth.models import User
        from scraper.models import ScrapeJob, SessionLog

        u = User.objects.create_user(username="_t_e0_norow", password="x")
        job = ScrapeJob.objects.create(
            url="https://shop.example.com/c/w", user=u, created_via="api",
            status="running", input_mode="list_page", page_type="product",
        )
        from webapp.agents import graph as g

        g._log_writer_embed_row(object(), job.id)
        assert not SessionLog.objects.filter(job_id=job.id).exists()

    def test_pre_model_hook_logs_per_turn_total(self, caplog):
        """E0(b): every pre-model pass logs the per-turn input total —
        messages chars + embed chars."""
        hook = sub._make_pre_model_hook(1_234)
        msgs = [HumanMessage(content="seed"), AIMessage(content="x" * 5_000)]
        with caplog.at_level(logging.INFO):
            hook({"messages": msgs})
        rows = [r for r in caplog.records if "[WRITER-TURN]" in r.getMessage()]
        assert rows, "the per-turn input total must be logged"
        msg = rows[0].getMessage()
        assert "embed=1,234" in msg
        assert "total=6,238" in msg, "messages chars + embed must sum in the log"


# ── E1: draft map + classification + staleness contract ────────────────────


class TestE1DraftMap:
    def test_map_lists_top_level_defs_with_line_spans(self):
        code = (
            "import os\n"
            "MAX_ITEMS = 10\n"
            "\n"
            "def _price(node):\n"
            '    """Extract the price value."""\n'
            "    return node.text\n"
            "\n"
            "class Extractor:\n"
            '    """Main extractor."""\n'
            "    pass\n"
            "\n"
            "hidden = 5\n"
        )
        m = dc.build_draft_map(code)
        assert m, "a parseable module must produce a map"
        assert "2-2" in m and "MAX_ITEMS" in m
        assert "4-6" in m and "def _price(" in m
        assert "Extract the price value." in m, "first docstring line is shown"
        assert "8-10" in m and "class Extractor" in m
        assert "hidden" not in m, "only ALL-CAPS assignments are indexed"

    def test_unparseable_code_drops_map_keeps_head_tail(self):
        """The 587 trap: an unparseable draft is a supported input state —
        build_draft_map returns '' (never raises) and the bounded renderer
        degrades to head+tail with a one-line note."""
        broken = "def broken(joined_marker:\n" + "# pad\n" * 12_000 + "    return 1\n"
        assert dc.build_draft_map(broken) == ""
        out = dc.render_writer_embed(SP, broken, mode="bounded", slug="s")
        assert "map unavailable" in out
        assert "def broken" in out and "return 1" in out
        assert "### Draft map" not in out

    def test_bounded_embed_carves_head_tail_and_map(self):
        big = _big_template()
        out = dc.render_writer_embed(SP, big, mode="bounded", slug="s")
        # head + tail survived
        assert "def _filler_fn_0" in out and "def main" in out
        # the middle function's NAME is reachable — via the map
        assert "### Draft map" in out
        assert "def _middle_price_fn(" in out
        # ...but its BODY is elided
        assert "MIDDLE_BODY_TOKEN_12345" not in out
        # snipped-byte accounting names the elided range
        assert "not shown" in out

    def test_map_is_capped(self):
        code = "".join(
            f"def _cap_fn_{i:04d}(a, b):\n"
            f'    """{"x" * 70}"""\n'
            f"    return {i}\n\n\n"
            for i in range(120)
        )
        body = dc.build_draft_map(code)
        assert len(body) <= dc.MAP_MAX_CHARS + 200, "the map must be capped"
        assert "map truncated" in body

    def test_bounded_embed_names_staleness_contract(self):
        out = dc.render_writer_embed(SP, _big_template(), mode="bounded", slug="s")
        assert "search_content" in out and "read_file(" in out
        assert "line=" in out
        assert "old_string not found" in out
        assert "stale" in out.lower()
        assert "rewrite" in out.lower()


class TestE1Classify:
    def test_classify_embed_matches_d5_call_site_behavior(self):
        """Golden parity with the conditions D5 inlined at BOTH call sites:
        main = ``_eow_active and len >= threshold``; finisher = size alone."""
        small = "x = 1\n"
        big = _big_template()
        for code in (small, big):
            for thr in (40_000, 5):
                for eow in (True, False):
                    want = "bounded" if (eow and len(code) >= thr) else "full"
                    got = dc.classify_embed(code, eow_active=eow, full_max_chars=thr)
                    assert got == want, (len(code), thr, eow)
                want_fin = "bounded" if len(code) >= thr else "full"
                got_fin = dc.classify_embed(
                    code, eow_active=None, full_max_chars=thr
                )
                assert got_fin == want_fin, "eow_active=None is size-only"

    def test_classify_default_threshold_matches_module_constant(self):
        assert dc.classify_embed("x" * 40_000, eow_active=True) == "bounded"
        assert dc.classify_embed("x" * 39_999, eow_active=True) == "full"

    def test_settings_declare_the_map_cap(self):
        from django.conf import settings

        assert getattr(settings, "CODE_WRITER_EMBED_MAP_MAX_CHARS", None) == 6_000

    def test_map_cap_setting_honored_by_renderer(self):
        """draft_context is Django-free: the SETTINGS value is read by the
        subagents helper; the renderer honors whatever cap it is passed."""
        with override_settings(CODE_WRITER_EMBED_MAP_MAX_CHARS=500):
            assert sub._embed_map_max_chars() == 500
        assert sub._embed_map_max_chars() == dc.MAP_MAX_CHARS
        out = dc.render_writer_embed(
            SP, _big_template(), mode="bounded", slug="s", map_max_chars=500
        )
        assert "map truncated" in out


# ── E3: embed-aware truncation budget (the rev-2 BLOCKER) ──────────────────


def _budget_fixture():
    """20 messages ≈ 142.8K chars, every one UNDER the per-message caps so
    step-1 has nothing to trim — only the budget gate can act."""
    msgs = [SystemMessage(content="S" * 100), HumanMessage(content="T" * 500)]
    for i in range(18):
        msgs.append(AIMessage(content=f"m{i} " + "x" * 7_896))
    return msgs


def _clen(m) -> int:
    n = len(str(m.content)) if hasattr(m, "content") else 0
    tc = getattr(m, "tool_calls", None)
    if tc:
        n += len(str(tc))
    return n


class TestE3EmbedAwareBudget:
    @override_settings(LLM_TRUNCATION_MAX_CHARS=180_000)
    def test_under_budget_gate_subtracts_embed(self):
        """THE BLOCKER: 142.8K of messages + a 47K embed must TRIM. Rev-1's
        spec early-returned untrimmed because step-1 compared against raw
        max_chars before the embed was ever subtracted."""
        hook = sub._make_pre_model_hook(embed_chars=47_000)
        msgs = _budget_fixture()
        assert sum(_clen(m) for m in msgs) < 180_000, "fixture under raw budget"
        out = hook({"messages": msgs})
        kept = out["llm_input_messages"]
        assert len(kept) < len(msgs), (
            "messages + embed over the budget MUST trim — the early-return "
            "gate must subtract the embed before declaring 'fits'"
        )
        total = sum(_clen(m) for m in kept)
        assert total + 47_000 <= 180_000, "the 180K budget must hold end-to-end"
        assert any(getattr(m, "type", "") == "system" for m in kept)
        assert kept[1].content.startswith("T"), "the seed survives"
        assert kept[-1].content.startswith("m17"), "newest message survives"
        assert not any(
            getattr(m, "content", "").startswith("m0 ") for m in kept
        ), "the OLDEST filler is what gets dropped"

    @override_settings(LLM_TRUNCATION_MAX_CHARS=180_000)
    def test_budget_subtracts_embed_chars(self):
        """Step-2's drop budget shrinks by exactly the embed: the same input
        keeps FEWER messages with an embed than without."""
        msgs = _budget_fixture()
        kept0 = sub._make_pre_model_hook(0)({"messages": msgs})["llm_input_messages"]
        kept47 = sub._make_pre_model_hook(47_000)(
            {"messages": msgs}
        )["llm_input_messages"]
        assert len(kept0) == len(msgs), "fixture fits at zero embed"
        assert len(kept47) < len(kept0)

    @override_settings(LLM_TRUNCATION_MAX_CHARS=180_000)
    def test_zero_embed_matches_legacy_truncation(self):
        """Golden path: embed=0 is byte-identical to the legacy contract on
        both a step-1 (trim-only) and a step-2 (drop) input."""
        hook0 = sub._make_pre_model_hook(0)
        # step-1 only: one oversized tool message gets trimmed, total fits
        trim_msgs = [
            HumanMessage(content="seed"),
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "c1"}]),
            __import__("langchain_core.messages", fromlist=["ToolMessage"]).ToolMessage(
                content="r" * 50_000, tool_call_id="c1"
            ),
        ]
        a = hook0({"messages": trim_msgs})
        b = sub._truncate_messages({"messages": trim_msgs})
        assert [str(m.content)[:20] for m in a["llm_input_messages"]] == [
            str(m.content)[:20] for m in b["llm_input_messages"]
        ]
        # step-2: 30 messages of 10K = 300K → oldest dropped
        drop_msgs = [HumanMessage(content="seed")] + [
            AIMessage(content=f"d{i} " + "y" * 9_996) for i in range(30)
        ]
        a2 = hook0({"messages": drop_msgs})
        b2 = sub._truncate_messages({"messages": drop_msgs})
        assert a2 == b2, "embed=0 must be the legacy truncation exactly"

    def test_truncate_messages_single_arg_still_works(self):
        out = sub._truncate_messages({"messages": [HumanMessage(content="hi")]})
        assert "llm_input_messages" in out


# ── E6: stale-copy and text-contract honesty ───────────────────────────────


class TestE6Honesty:
    def test_edit_file_not_found_names_stale_copy(self, tmp_path):
        from agents.tools.filesystem_tools import get_filesystem_tools

        (tmp_path / "draft.py").write_text("alpha = 1\n", encoding="utf-8")
        edit = next(
            t for t in get_filesystem_tools(project_root=str(tmp_path))
            if t.name == "edit_file"
        )
        reply = edit.invoke({
            "path": "draft.py", "old_string": "NOT_PRESENT_ANYWHERE",
            "new_string": "x",
        })
        assert "old_string not found" in reply
        assert "stale" in reply.lower()
        assert "search_content" in reply
        assert "read_file(path, line=" in reply
        assert "do NOT rewrite" in reply

    def test_contract_fix_message_does_not_promise_full_template(self):
        from webapp.agents.graph import _contract_fix_message

        msg = _contract_fix_message(
            "slug", "list_page", "missing --listing-url flag",
            "/nonexistent/template.py",
        )
        assert "full template is in your system prompt" not in msg
        assert "templates/" not in msg, "the writer container has no repo"
        assert "read_file(line=" in msg, "the bounded-safe redirection"
        # pinned tokens survive (the argparse example stays concrete)
        for tok in ("--listing-url", "--fresh-discovery", "edit_file"):
            assert tok in msg

    def test_fixer_messages_never_reference_templates_path(self):
        import inspect

        from webapp.agents import graph as g

        src = inspect.getsource(g._contract_fix_message)
        assert "the full template is in your system prompt" not in src
        assert "templates/" not in src
        fix_src = inspect.getsource(g._fix_scraper_syntax)
        assert "templates/" not in fix_src

    def test_writer_prompt_does_not_order_template_readfile(self):
        md = open(
            os.path.join(ROOT, ".opencode", "agents", "code-writer.md"),
            encoding="utf-8",
        ).read()
        assert "read_file` the template" not in md, (
            "the template is in the prompt; ordering a read of it burns a "
            "round on a path that does not exist"
        )
        assert "already in your prompt" in md
        # pinner tokens from test_cli_contract_prompt / job310 / wave14 /
        # wave19 / wave25 — E6d edits line 40 ONLY
        for tok in (
            "--listing-url", "--fresh-discovery", "--discover-only", "--query",
            "SCRAPER_LISTING_URL", "Zero-yield discovery must self-heal",
            "DEFAULT_LISTING_URL", "verification-scope", "same-host",
            "protocol-relative", "https://products/x", "Prices are NUMBERS",
            "_norm_price",
        ):
            assert tok in md, f"collateral edit: lost {tok}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
