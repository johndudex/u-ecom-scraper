"""F2 sanitize-on-write: the write_file/edit_file .json guard (M1/M3 net).

Root cause (docs/plans/artifact-corruption-rootcause.md): write_file flushed
the tool-call argument verbatim with no JSON validation, so every LLM-authored
artifact reached disk exactly as the model typed it. Three defect shapes were
observed in one day: unquoted prose scalars (sidley), literal newlines inside a
string (priceline), and unescaped quotes inside an embedded JS regex (job 10).

The guard: for .json paths, parse the content BEFORE writing — strip a leading
```json fence, try strict json.loads, then strict=False (which legalizes literal
control chars inside strings), and on success write the CANONICAL re-dump. When
both parses fail, write the raw bytes anyway (the phase-exit repair pass owns
salvage) but return a warning line in the tool result and log a WARNING — that
log is the measurement signal for whether a corrective-refusal layer is needed.
"""
from __future__ import annotations

import json
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


def _tools(tmp_path):
    from agents.tools.filesystem_tools import get_filesystem_tools

    return {t.name: t for t in get_filesystem_tools(project_root=str(tmp_path))}


# ─── write_file: .json branch ────────────────────────────────────────────────

class TestWriteFileJsonSanitize:
    def test_valid_json_canonical_redump(self, tmp_path):
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        content = '{"b": 1,\n  "a": [1, 2]}'  # non-canonical formatting
        r = tools["write_file"].invoke({"path": str(p), "content": content})
        assert "Successfully wrote" in r
        assert "not valid JSON" not in r
        on_disk = p.read_text()
        assert on_disk == json.dumps(json.loads(content), indent=1)
        assert json.loads(on_disk) == {"b": 1, "a": [1, 2]}

    def test_fenced_json_stripped_and_parsed(self, tmp_path):
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        r = tools["write_file"].invoke(
            {"path": str(p), "content": "```json\n{\"a\": 1}\n```"}
        )
        assert "Successfully wrote" in r
        assert json.loads(p.read_text()) == {"a": 1}
        assert "```" not in p.read_text()

    def test_literal_control_char_repaired(self, tmp_path):
        """The priceline shape: a REAL newline byte inside a JSON string.
        Strict parse fails; strict=False parses; canonical write escapes it."""
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        content = '{"feedback_for_writer": "para one' + chr(10) + chr(10) + 'para two"}'
        with pytest.raises(json.JSONDecodeError):
            json.loads(content)  # proves the input really is strict-invalid
        r = tools["write_file"].invoke({"path": str(p), "content": content})
        assert "Successfully wrote" in r
        on_disk = p.read_text()
        json.loads(on_disk)  # STRICT parse — the file is now strictly valid
        assert json.loads(on_disk) == {
            "feedback_for_writer": "para one\n\npara two"
        }

    def test_non_json_path_untouched(self, tmp_path):
        tools = _tools(tmp_path)
        p = tmp_path / "scraper_draft.py"
        body = "x = {'a': 1}\n" + chr(10) + "y = 2  # comment\n"
        r = tools["write_file"].invoke({"path": str(p), "content": body})
        assert "Successfully wrote" in r
        assert "not valid JSON" not in r
        assert p.read_text() == body

    def test_unparseable_json_written_raw_with_warning(self, tmp_path, caplog):
        """The sidley shape: unquoted prose scalar. Nothing can parse it, so the
        bytes land as-is BUT the tool result carries the warning line and a
        WARNING is logged (the F1-lite measurement signal)."""
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        corrupt = (
            '{\n  "search_criteria": {\n'
            '    "offices": 25 offices with counts,\n'
            '    "titles": "ok"\n  }\n}\n'
        )
        with caplog.at_level(logging.WARNING, logger="agents.tools.filesystem_tools"):
            r = tools["write_file"].invoke({"path": str(p), "content": corrupt})
        assert p.read_text() == corrupt  # raw bytes survive verbatim
        assert "not valid JSON" in r
        assert "repair pass" in r
        warned = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
        assert warned, "expected a WARNING log record for unparseable .json write"
        assert any(str(p) in rec.getMessage() for rec in warned)

    def test_bare_scalar_whole_file_written_raw(self, tmp_path):
        """Even a totally non-JSON .json body is written (repair owns it later)."""
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        r = tools["write_file"].invoke({"path": str(p), "content": "not json at all"})
        assert p.read_text() == "not json at all"
        assert "not valid JSON" in r

    def test_unicode_preserved(self, tmp_path):
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        r = tools["write_file"].invoke(
            {"path": str(p), "content": '{"name": "Rudiño"}'}
        )
        assert "not valid JSON" not in r
        assert json.loads(p.read_text()) == {"name": "Rudiño"}

    def test_indent_one_canonical_shape(self, tmp_path):
        """The canonical redump uses indent=1 (matches the existing repair
        convention) so sanitized and repaired artifacts look alike."""
        tools = _tools(tmp_path)
        p = tmp_path / "a.json"
        tools["write_file"].invoke({"path": str(p), "content": '{"a": {"b": 2}}'})
        assert p.read_text() == '{\n "a": {\n  "b": 2\n }\n}'
