"""[wave-60 N1] Gate-lift sidecar diversion — the draft of record survives.

Job 632 (paulsmith): the writer wrote a real ~1,800-line draft, then copied
the system's own tool-call-arg stubs into code as literals → gate-0 rejected
18× → at strike 4 the bounded lift let a 73-byte placeholder OVERWRITE the
draft → the strike-2 repair arm found 0 lines ≥ its 200-line floor and had
nothing to salvage. Honest cleanup of a site the writer had effectively
finished.

Contract: a streak-LIFTED write/edit touches ``scraper_draft.py`` ONLY when
no substantial draft exists to protect. When a ≥200-line draft IS on disk,
the lifted payload diverts to a rolling sidecar
``scraper_draft-rejected.py`` (same dir, OVERWRITTEN per divert) and the
tool result says so explicitly. No substantial draft (absent / under the
floor) → the payload lands exactly as before — C3's lift contract (a wedged
writer is never blocked) is untouched. Rotation-pivot safe:
``_archive_dead_strategy_draft`` removes the draft before a pivot, so an
ABSENT draft never diverts.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

fs = importlib.import_module("agents.tools.filesystem_tools")  # noqa: E402
subagents = importlib.import_module("agents.subagents")  # noqa: E402

# 623's unterminated-string class: parses as NOthing, never reaches ruff.
BAD_SYNTAX = 'CATEGORY_RE = re.compile(r"/collections/.+-g\\d+, re.I)\n'
# Parses clean but ruff F821-rejects.
BAD_NAME = "print(MISSING_NAME)\n"
DIVERT_MSG_TOKEN = "SYNTAX GATE lift"
SIDECAR_NAME = "scraper_draft-rejected.py"


def _pad_draft(n_lines: int) -> str:
    """A valid-syntax draft of exactly ``n_lines`` (no trailing newline)."""
    return "\n".join(f"LINE_{i:04d} = {i}" for i in range(n_lines))


@pytest.fixture(autouse=True)
def _fresh_gate(tmp_path, monkeypatch):
    """Streak is module state; the kill switch must start disengaged."""
    fs._syntax_reject_streak["n"] = 0
    monkeypatch.delenv("SCRAPER_DRAFT_SYNTAX_GATE", raising=False)
    monkeypatch.delenv("SCRAPER_DRAFT_CALL_GATE", raising=False)
    yield tmp_path
    fs._syntax_reject_streak["n"] = 0


def _tools(root) -> dict:
    tools = fs.get_filesystem_tools(project_root=str(root), syntax_gate=True)
    return {t.name: t.func for t in tools}


def _slug_dir(root, slug: str = "w60") -> Path:
    d = Path(root) / "workspace" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _raise_streak(write, scratch_path: str) -> None:
    for _ in range(3):
        out = write(path=scratch_path, content=BAD_SYNTAX)
        assert out.startswith("REJECTED"), f"strike-up failed: {out[:120]}"
    assert fs._syntax_reject_streak["n"] == fs._SYNTAX_REJECT_MAX


class TestDivertBehavior:
    def test_lifted_write_to_substantial_draft_diverts(self, tmp_path):
        """The 632 replay: streak-lifted junk aimed at a real draft MUST
        land on the sidecar; the draft of record is byte-identical."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        original = _pad_draft(250)
        draft.write_text(original, encoding="utf-8")
        _raise_streak(write, str(root / "scratch.py"))

        out = write(path=str(draft), content=BAD_SYNTAX)
        assert DIVERT_MSG_TOKEN in out, f"expected divert note, got: {out[:200]}"
        assert SIDECAR_NAME in out
        assert "Fix the syntax error and write the draft again" in out
        assert draft.read_text(encoding="utf-8") == original, (
            "the draft of record must be byte-identical after a diverted lift"
        )
        sidecar = root / SIDECAR_NAME
        assert sidecar.read_text(encoding="utf-8") == BAD_SYNTAX

    def test_lifted_write_to_absent_draft_lands(self, tmp_path):
        """Rotation-pivot regression case: ``_archive_dead_strategy_draft``
        os.replace's the draft away, so an ABSENT draft → the lifted pivot
        draft LANDS (C3's lift contract intact)."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        _raise_streak(write, str(root / "scratch.py"))

        out = write(path=str(draft), content=BAD_SYNTAX)
        assert out.startswith("Successfully wrote"), f"lift must land: {out[:200]}"
        assert draft.read_text(encoding="utf-8") == BAD_SYNTAX
        assert not (root / SIDECAR_NAME).exists()

    def test_lifted_write_to_short_draft_lands(self, tmp_path):
        """Under the 200-line floor there is nothing worth protecting — the
        lifted payload overwrites the stub exactly as before N1."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        draft.write_text("DRAFT = 'stub'\n", encoding="utf-8")
        _raise_streak(write, str(root / "scratch.py"))

        out = write(path=str(draft), content=BAD_SYNTAX)
        assert out.startswith("Successfully wrote"), out[:200]
        assert not (root / SIDECAR_NAME).exists()

    def test_boundary_exactly_200_lines_diverts(self, tmp_path):
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        draft.write_text(_pad_draft(200), encoding="utf-8")
        _raise_streak(write, str(root / "scratch.py"))

        out = write(path=str(draft), content=BAD_SYNTAX)
        assert DIVERT_MSG_TOKEN in out, f"200 is substantial: {out[:200]}"

    def test_boundary_199_lines_lands(self, tmp_path):
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        draft.write_text(_pad_draft(199), encoding="utf-8")
        _raise_streak(write, str(root / "scratch.py"))

        out = write(path=str(draft), content=BAD_SYNTAX)
        assert out.startswith("Successfully wrote"), f"199 lands: {out[:200]}"

    def test_non_draft_big_file_never_diverts(self, tmp_path):
        """The basename guard: the sidecar contract is scraper_draft.py's
        alone — other big workspace files take lifted writes as before."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        other = root / "helpers_big.py"
        original = _pad_draft(300)
        other.write_text(original, encoding="utf-8")
        _raise_streak(write, str(root / "scratch.py"))

        out = write(path=str(other), content=BAD_SYNTAX)
        assert out.startswith("Successfully wrote"), out[:200]
        assert other.read_text(encoding="utf-8") == BAD_SYNTAX
        assert not (root / SIDECAR_NAME).exists()

    def test_sidecar_overwritten_per_divert(self, tmp_path):
        """Rolling = one fixed path, OVERWRITTEN — never sidecar-1, sidecar-2
        (a sidecar farm would leak junk files into the workspace)."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        draft.write_text(_pad_draft(250), encoding="utf-8")
        _raise_streak(write, str(root / "scratch.py"))

        assert DIVERT_MSG_TOKEN in write(path=str(draft), content=BAD_SYNTAX)
        payload_b = "def broken(:\n"
        assert DIVERT_MSG_TOKEN in write(path=str(draft), content=payload_b)
        sidecars = list(root.glob("scraper_draft-rejected*.py"))
        assert len(sidecars) == 1, f"sidecar farm: {sidecars}"
        assert sidecars[0].read_text(encoding="utf-8") == payload_b
        assert fs._syntax_reject_streak["n"] > fs._SYNTAX_REJECT_MAX, (
            "the streak stays terminal across diverts until a clean pass"
        )

    def test_edit_diverts_full_post_edit_hypothetical(self, tmp_path):
        """edit_file's sidecar gets ``updated`` — the full post-edit file —
        never the bare ``new_string``; the draft on disk is untouched."""
        root = _slug_dir(tmp_path)
        tools = _tools(tmp_path)
        draft = root / "scraper_draft.py"
        original = _pad_draft(250) + "\nMID = 1\n"
        draft.write_text(original, encoding="utf-8")
        _raise_streak(tools["write_file"], str(root / "scratch.py"))

        out = tools["edit_file"](
            path=str(draft),
            old_string="MID = 1",
            new_string=BAD_SYNTAX.rstrip("\n"),
        )
        assert DIVERT_MSG_TOKEN in out, f"expected divert note: {out[:200]}"
        assert draft.read_text(encoding="utf-8") == original, (
            "an edited-then-diverted draft must be byte-identical"
        )
        expected = original.replace("MID = 1", BAD_SYNTAX.rstrip("\n"), 1)
        sidecar = root / SIDECAR_NAME
        assert sidecar.read_text(encoding="utf-8") == expected

    def test_gate1_rejection_keeps_lift_inert(self, tmp_path, monkeypatch):
        """Guardian cond-4, inert-flag path: when gate 1 (ruff F821)
        REJECTS, the write is blocked and nothing diverts — even with the
        streak terminal. A rejection never rides a lift."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        original = _pad_draft(250)
        draft.write_text(original, encoding="utf-8")
        monkeypatch.setattr(fs, "_syntax_gate_outcome", lambda c: ("", True))

        out = write(path=str(draft), content=BAD_NAME)
        assert out.startswith("REJECTED — NOT applied"), out[:200]
        assert "MISSING_NAME" in out
        assert draft.read_text(encoding="utf-8") == original
        assert not (root / SIDECAR_NAME).exists()


class TestLiftHygiene:
    def test_clean_pass_resets_streak(self, tmp_path):
        """Pre-existing C3 behavior, re-pinned through the tuple seam: a
        clean gate-0 pass resets the streak, so lifts never outlive their
        failure episode."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        for _ in range(2):
            assert write(path=str(root / "scratch.py"), content=BAD_SYNTAX)
        assert fs._syntax_reject_streak["n"] == 2
        out = write(path=str(draft), content="X = 1\n")
        assert out.startswith("Successfully wrote"), out[:200]
        assert fs._syntax_reject_streak["n"] == 0
        # the streak restarts from 0: three more rejections are still
        # rejected (had the reset not happened, the third would lift)
        for _ in range(3):
            assert write(
                path=str(root / "scratch.py"), content=BAD_SYNTAX
            ).startswith("REJECTED")
        assert fs._syntax_reject_streak["n"] == 3

    def test_create_code_writer_resets_streak(self):
        """Guardian rev-2: the reset rides ``create_code_writer`` — the
        factory all THREE writer seams build through (main window,
        progress escalation, strike-2 finisher). A terminal streak from a
        dead window must never lift the next window's FIRST bad write."""
        fs._syntax_reject_streak["n"] = fs._SYNTAX_REJECT_MAX + 1
        subagents.create_code_writer(
            site_slug="w60-reset", template_code="x = 1",
            embed_mode="full", embed_kind="file",
        )
        assert fs._syntax_reject_streak["n"] == 0

    def test_internal_error_fall_open_carries_no_lift(self, tmp_path, monkeypatch):
        """Fall-open is not a lift: a gate malfunction must not divert —
        the write proceeds exactly as pre-N1."""
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        original = _pad_draft(250)
        draft.write_text(original, encoding="utf-8")

        def _boom(*a, **k):
            raise RuntimeError("checker exploded")

        monkeypatch.setattr("builtins.compile", _boom)
        out = write(path=str(draft), content=BAD_SYNTAX)
        assert out.startswith("Successfully wrote"), out[:200]
        assert draft.read_text(encoding="utf-8") == BAD_SYNTAX
        assert not (root / SIDECAR_NAME).exists()

    def test_kill_switch_no_divert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCRAPER_DRAFT_SYNTAX_GATE", "0")
        root = _slug_dir(tmp_path)
        write = _tools(tmp_path)["write_file"]
        draft = root / "scraper_draft.py"
        draft.write_text(_pad_draft(250), encoding="utf-8")

        out = write(path=str(draft), content=BAD_SYNTAX)
        assert out.startswith("Successfully wrote"), out[:200]
        assert not (root / SIDECAR_NAME).exists()
        assert fs._syntax_reject_streak["n"] == 0, "disarmed gate must not count"


class TestWiringPins:
    def test_run_scraper_bucket_excludes_sidecar(self):
        """The sidecar is a rejected payload, not a runnable draft — it must
        not burn the writer's 2-run draft cap."""
        assert subagents._run_scraper_bucket(
            "workspace/w60/scraper_draft-rejected.py"
        ) == "other"
        assert subagents._run_scraper_bucket("scraper_draft-rejected") == "other"
        assert subagents._run_scraper_bucket(
            "workspace/w60/scraper_draft.py"
        ) == "scraper_draft", "the real draft keeps its bucket"

    def test_public_gate_wrappers_keep_string_contract(self):
        """Guardian rev-1: ~15 pinned asserts across wave-22/25/40/59 read
        the public string returns — they must stay strings."""
        assert fs._syntax_gate_rejection(BAD_SYNTAX).startswith("REJECTED")
        assert fs._syntax_gate_rejection("X = 1\n") == ""
        msg = fs._f821_rejections("scraper_draft.py", BAD_NAME)
        assert msg.startswith("REJECTED — NOT applied")
        assert fs._f821_rejections("scraper_draft.py", "X = 1\n") == ""

    def test_both_closures_wired_to_the_helper(self):
        """One helper, two call sites — write_file diverts ``content``,
        edit_file diverts ``updated`` (the post-edit hypothetical), both via
        the outcome wrapper inside the syntax_gate-armed branch; and the
        writer factory resets the streak."""
        src = open(
            os.path.join(ROOT, "webapp", "agents", "tools", "filesystem_tools.py"),
            encoding="utf-8",
        ).read()
        i_write = src.index("def write_file(")
        i_edit = src.index("def edit_file(")
        i_search = src.index("def search_files(")
        write_body = src[i_write:i_edit]
        edit_body = src[i_edit:i_search]
        for body, payload in ((write_body, "content"), (edit_body, "updated")):
            assert "_f821_outcome(safe, " + payload + ")" in body, (
                "closure must read the call-scoped outcome"
            )
            assert "_divert_lifted_write(safe, " + payload + ")" in body
            i_gate = body.index("if syntax_gate and safe.endswith")
            i_outcome = body.index("_f821_outcome(safe")
            i_divert = body.index("_divert_lifted_write(safe")
            assert i_gate < i_outcome < i_divert, (
                "divert must ride the armed branch, after the gate consult"
            )
        sub_src = open(
            os.path.join(ROOT, "webapp", "agents", "subagents.py"),
            encoding="utf-8",
        ).read()
        i_factory = sub_src.index("def create_code_writer(")
        i_next = sub_src.index("def create_code_tester(")
        assert "reset_syntax_reject_streak()" in sub_src[i_factory:i_next]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
