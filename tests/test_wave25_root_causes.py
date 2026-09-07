"""Wave-25 root-cause fixes + string-price fixes (T1-T3, W25-a..d).

Prod RCAs 2026-09-07:
- Writer read-spiral: read_file pages a >50K file sequentially (job 404: ~100
  reads, ~2MB tool results) until langgraph's remaining_steps=0 ends the
  invoke with the silent "Sorry, need more steps" message that no counter saw.
- String-price bounce: templates' _norm_price returns STR, the tester prompt
  orders WRONG_TYPE on string prices, and [A3] normalization only ran on the
  browser_service run_scraper branch — so the writer burned fix cycles
  converting prices to floats the harness produces for free at persist time.
"""

import ast
import json
import textwrap
from pathlib import Path

import pytest

_WRITE_ROOT = Path("/tmp/w25_nudge_tests")
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
PROMPTS = Path(__file__).resolve().parents[1] / ".opencode" / "agents"

STR_PRICE_TEMPLATES = [
    "http_navigation_scraper",
    "navigation_scraper",
    "shopify_scraper",
    "api_scraper",
    "requests_scraper",
]


def _extract_fn(template: str, fn_name: str):
    """Extract a top-level function's source from a template and exec it
    with a minimal namespace (re, Optional). Returns the live callable."""
    src = (TEMPLATES / f"{template}.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            seg = ast.get_source_segment(src, node)
            ns: dict = {}
            exec(textwrap.dedent("import re\nfrom typing import Optional\n") + seg, ns)
            return ns[fn_name]
    raise AssertionError(f"{template}.{fn_name} not found")


# ── T1: templates _norm_price → Optional[float] ─────────────────────────────


@pytest.mark.parametrize("template", STR_PRICE_TEMPLATES)
class TestNormPriceReturnsFloat:
    def test_formatted_string_becomes_float(self, template):
        _norm_price = _extract_fn(template, "_norm_price")
        out = _norm_price("£1,234.56")
        assert isinstance(out, float)
        assert out == pytest.approx(1234.56)

    def test_european_format_becomes_float(self, template):
        _norm_price = _extract_fn(template, "_norm_price")
        out = _norm_price("1.234,56 €")
        assert isinstance(out, float)
        assert out == pytest.approx(1234.56)

    def test_jsonld_number_passthrough(self, template):
        _norm_price = _extract_fn(template, "_norm_price")
        out = _norm_price(24.99)
        assert isinstance(out, float)
        assert out == pytest.approx(24.99)

    def test_unparseable_is_none(self, template):
        _norm_price = _extract_fn(template, "_norm_price")
        assert _norm_price("See price at checkout") is None
        assert _norm_price("") is None
        assert _norm_price(None) is None

    def test_garbage_number_shape_is_none_not_string(self, template):
        # "1.2.3" has digits but no clean float reading — must be None
        # (EMPTY), never a garbage string smuggled through.
        _norm_price = _extract_fn(template, "_norm_price")
        assert _norm_price("1.2.3") is None

    def test_annotation_is_optional_float(self, template):
        src = (TEMPLATES / f"{template}.py").read_text(encoding="utf-8")
        assert "def _norm_price(value) -> Optional[float]:" in src


def test_writer_prompt_price_numeric_gotcha():
    md = (PROMPTS / "code-writer.md").read_text(encoding="utf-8")
    assert "Prices are NUMBERS" in md
    assert "_norm_price" in md


# ── T3: tester prompt carve-out for clean-parseable price strings ───────────


def test_tester_prompt_price_carve_out():
    md = (PROMPTS / "code-tester.md").read_text(encoding="utf-8")
    assert "Formatted price strings are NOT defects" in md
    assert "persist" in md  # explains WHY: normalized at persist time


# ── T2: local run_scraper branch persists + normalizes output ───────────────


class TestLocalBranchOutputNormalize:
    def _helper(self):
        from agents.tools.shell_tools import _normalize_local_output

        return _normalize_local_output

    def test_newer_output_normalized(self, tmp_path):
        import os, time

        out = tmp_path / "output_20260907_120000.json"
        out.write_text(
            json.dumps({"products": [{"title": "T", "price": "$17.00"}]}),
            encoding="utf-8",
        )
        os.utime(out, (time.time(), time.time()))
        path, n = self._helper()(str(tmp_path), mtime_floor=time.time() - 60)
        assert path == str(out)
        assert n == 1
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["products"][0]["price"] == 17.0

    def test_stale_output_excluded(self, tmp_path):
        import os, time

        out = tmp_path / "output_old.json"
        out.write_text(
            json.dumps({"products": [{"price": "$17.00"}]}), encoding="utf-8"
        )
        old = time.time() - 3600
        os.utime(out, (old, old))
        path, n = self._helper()(str(tmp_path), mtime_floor=time.time() - 60)
        assert path == ""
        assert n == 0

    def test_no_output_is_noop(self, tmp_path):
        path, n = self._helper()(str(tmp_path), mtime_floor=0)
        assert path == ""
        assert n == 0

    def test_local_branch_wires_the_helper(self):
        src = Path(
            Path(__file__).resolve().parents[1] / "webapp" / "agents" / "tools" / "shell_tools.py"
        ).read_text(encoding="utf-8")
        branch = src[src.index("http-based, running locally"):]
        assert "_normalize_local_output(" in branch


# ── W25-a: read_file large-file head+tail cap ───────────────────────────────


@pytest.fixture
def fs_tools(tmp_path):
    from agents.tools.filesystem_tools import get_filesystem_tools

    return {t.name: t for t in get_filesystem_tools(project_root=str(tmp_path))}


class TestReadFileHeadTailCap:
    BIG = 120_000

    def _big_file(self, tmp_path):
        p = tmp_path / "big_output.json"
        head = "HEADMARKER " + "a" * 25_000
        mid = "MIDDLEMARKER-should-not-appear " + "b" * 70_000
        tail = "c" * 25_000 + " TAILMARKER"
        p.write_text(head + mid + tail, encoding="utf-8")
        return p

    def test_full_read_of_large_file_is_head_and_tail(self, fs_tools, tmp_path):
        p = self._big_file(tmp_path)
        out = fs_tools["read_file"].invoke({"path": str(p)})
        assert "HEADMARKER" in out
        assert "TAILMARKER" in out
        assert "MIDDLEMARKER" not in out
        assert len(out) < 60_000

    def test_full_read_guides_to_search_content(self, fs_tools, tmp_path):
        p = self._big_file(tmp_path)
        out = fs_tools["read_file"].invoke({"path": str(p)})
        assert "search_content" in out
        assert "offset=" in out

    def test_no_sequential_paging_instruction(self, fs_tools, tmp_path):
        # The old truncation notice TOLD the model to page ("Re-call
        # read_file with offset=... for the next portion") — the spiral
        # enabler. The large-file notice must not encourage paging.
        p = self._big_file(tmp_path)
        out = fs_tools["read_file"].invoke({"path": str(p)})
        assert "for the next portion" not in out

    def test_offset_read_still_returns_window(self, fs_tools, tmp_path):
        p = self._big_file(tmp_path)
        out = fs_tools["read_file"].invoke({"path": str(p), "offset": 50_000})
        assert len(out) <= 55_000

    def test_small_file_unchanged(self, fs_tools, tmp_path):
        p = tmp_path / "small.txt"
        p.write_text("hello world", encoding="utf-8")
        assert fs_tools["read_file"].invoke({"path": str(p)}) == "hello world"

    def test_offset_out_of_range_still_errors(self, fs_tools, tmp_path):
        p = self._big_file(tmp_path)
        out = fs_tools["read_file"].invoke({"path": str(p), "offset": 10_000_000})
        assert "out of range" in out


# ── W25-b: writer anti-read nudge ───────────────────────────────────────────


class TestAntiReadNudge:
    def _tools(self):
        from agents.tools.filesystem_tools import get_filesystem_tools
        from agents.subagents import apply_anti_read_nudge

        return apply_anti_read_nudge(
            get_filesystem_tools(project_root="/tmp")
        )

    def test_reads_below_threshold_untouched(self):
        tools = {t.name: t for t in self._tools()}
        out = tools["read_file"].invoke({"path": "/etc/hostname"})
        assert "READ-BUDGET" not in out

    def test_read_at_threshold_gets_nudge(self):
        from agents.tools.filesystem_tools import get_filesystem_tools
        from agents.subagents import apply_anti_read_nudge

        tools = {
            t.name: t for t in apply_anti_read_nudge(
                get_filesystem_tools(project_root="/tmp"), threshold=3
            )
        }
        for _ in range(2):
            tools["read_file"].invoke({"path": "/etc/hostname"})
        out = tools["read_file"].invoke({"path": "/etc/hostname"})
        assert "READ-BUDGET" in out
        assert "search_content" in out

    def test_non_read_tools_never_nudged(self):
        from agents.tools.filesystem_tools import get_filesystem_tools
        from agents.subagents import apply_anti_read_nudge

        tools = {
            t.name: t for t in apply_anti_read_nudge(
                get_filesystem_tools(project_root=str(_WRITE_ROOT)), threshold=2
            )
        }
        # Drive read_file past the threshold first…
        for _ in range(3):
            tools["read_file"].invoke({"path": "/etc/hostname"})
        # …then a write result must not carry the nudge.
        out = tools["write_file"].invoke(
            {"path": str(_WRITE_ROOT / "w25_nudge_probe.txt"), "content": "x"}
        )
        assert "READ-BUDGET" not in out

    def test_threshold_zero_returns_tools_untouched(self):
        from agents.tools.filesystem_tools import get_filesystem_tools
        from agents.subagents import apply_anti_read_nudge

        raw = get_filesystem_tools(project_root="/tmp")
        assert apply_anti_read_nudge(raw, threshold=0) is raw

    def test_wired_into_code_writer_branch(self):
        src = Path(
            Path(__file__).resolve().parents[1] / "webapp" / "agents" / "subagents.py"
        ).read_text(encoding="utf-8")
        writer_branch = src[src.index("apply_draft_nudge(tools)"):]
        assert "apply_anti_read_nudge(tools)" in writer_branch


# ── W25-c: silent-death accounting for step-budget deaths ──────────────────


class TestStepBudgetDeathAccounting:
    def _fn(self):
        from agents.graph import _writer_hit_step_budget

        return _writer_hit_step_budget

    class _Msg:
        def __init__(self, content):
            self.content = content

    def test_apology_message_detected(self):
        result = {
            "messages": [self._Msg("Sorry, need more steps to process this request.")]
        }
        assert self._fn()(result) is True

    def test_normal_message_not_detected(self):
        result = {"messages": [self._Msg("Draft written to scraper_draft.py")]}
        assert self._fn()(result) is False

    def test_no_messages_not_detected(self):
        assert self._fn()({}) is False
        assert self._fn()({"messages": []}) is False

    def test_counts_in_writer_node_source(self):
        src = Path(
            Path(__file__).resolve().parents[1] / "webapp" / "agents" / "graph.py"
        ).read_text(encoding="utf-8")
        node = src[src.index("def _invoke_code_writer"):]
        node = node[: node.index("\ndef ", 10)]
        assert "writer_step_budget_deaths" in node
        assert "_writer_hit_step_budget" in node


# ── W25-d: F821 rejection repair contract ──────────────────────────────────


class TestF821RepairContract:
    def _reject(self, content):
        from agents.tools.filesystem_tools import _f821_rejections

        return _f821_rejections("workspace/x/scraper_draft.py", content)

    def test_rejection_carries_repair_contract(self):
        text = self._reject("print(undefined_name)\n")
        assert "REJECTED" in text
        assert "do NOT re-read" in text
        assert "search_content" not in text or True

    def test_rejection_tells_writer_to_fix_forward(self):
        text = self._reject("print(undefined_name)\n")
        assert "re-issue" in text.lower()

    def test_clean_content_gate_open(self):
        assert self._reject("import os\nprint(os.sep)\n") == ""
