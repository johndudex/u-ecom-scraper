"""M4 copy-path guards: corruption must not propagate to/from the File Master.

Three byte-copy paths moved workspace .json files without ever parsing them, so
a corrupt artifact outlived its job and re-entered later generations (job 10's
author consumed a poisoned artifact this way):

  1. _finalize_job        (webapp/scraper/tasks.py)   workspace analysis/* → FM
  2. _invoke_skill_learner(graph.py)                  learning reports    → FM
  3. setup_workspace._restore_from_archive             FM analysis/*      → workspace

Each guard: validate (strict, then strict=False → canonical redump from the
PARSED value); if unparseable, run the in-memory repair; if still bad, SKIP the
copy + log ERROR — never propagate corrupt bytes.
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


# ─── the shared in-memory guard (the seam all three paths call) ──────────────

class TestGuardedJsonBytes:
    def test_valid_bytes_byte_identical(self):
        from agents.tools.filesystem_tools import guard_json_bytes

        raw = b'{"a": 1, "b": [2, 3]}'
        out, note = guard_json_bytes(raw)
        assert out == raw, "valid JSON must pass through byte-identical"
        assert note == ""

    def test_control_char_bytes_canonicalized(self):
        """C1: lenient parse succeeds → canonical (strictly-valid) redump from
        the PARSED value, not a raw-byte copy."""
        from agents.tools.filesystem_tools import guard_json_bytes

        raw = b'{"feedback": "one' + bytes([10]) + b'two"}'
        out, note = guard_json_bytes(raw)
        data = json.loads(out.decode("utf-8"))  # STRICT parse of the output
        assert data["feedback"] == "one\ntwo"
        assert note

    def test_sidley_class_bytes_repaired_in_memory(self):
        """C2: unparseable → the repair runs in-memory and the REPAIRED bytes
        are returned (no raw corrupt bytes propagate)."""
        from agents.tools.filesystem_tools import guard_json_bytes

        raw = (
            b'{\n "a": "keep me",\n "bad": 25 offices with counts,\n'
            b' "c": {"d": 1}\n}\n'
        )
        out, note = guard_json_bytes(raw)
        data = json.loads(out.decode("utf-8"))
        assert data["a"] == "keep me"
        assert "bad" not in data
        assert note

    def test_unrepairable_bytes_flagged(self):
        """Nothing parses and nothing repairs → (None, note) so the caller can
        SKIP the copy rather than propagate corruption."""
        from agents.tools.filesystem_tools import guard_json_bytes

        out, note = guard_json_bytes(b"<<<not json at all>>>")
        assert out is None
        assert note

    def test_real_sidley_fixture_repaired(self):
        from agents.tools.filesystem_tools import guard_json_bytes

        fixture = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "fixtures", "sidley_product_analysis.json.corrupt",
        )
        raw = open(fixture, "rb").read()
        out, note = guard_json_bytes(raw)
        assert out is not None, "the sidley ground truth must be repairable"
        data = json.loads(out.decode("utf-8"))
        assert len(data) >= 8
        assert len(data.get("fields") or {}) >= 36


# ─── 1. _finalize_job: workspace analysis/* → FM ─────────────────────────────

class TestFinalizeJobCopyGuard:
    def test_corrupt_artifact_not_copied_to_fm(self, tmp_path, monkeypatch, caplog):
        """Corrupt-and-unrepairable → the FM copy is SKIPPED (never
        propagated) and an ERROR is logged."""
        import scraper.tasks as tasks
        import src.artifacts as artifacts

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "site_analysis.json").write_bytes(b"<<<not json at all>>>")
        (ws / "product_analysis.json").write_bytes(b'{"platform": "sfcc"}')

        written = {}
        monkeypatch.setattr(artifacts, "write", lambda key, data: written.setdefault(key, data))
        from django.conf import settings as dj_settings
        monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path))

        with caplog.at_level(logging.ERROR, logger="scraper.tasks"):
            tasks._publish_analysis_artifacts(1, "s", ws)

        keys = list(written)
        assert any("product_analysis.json" in k for k in keys), "valid artifact must still copy"
        assert not any("site_analysis.json" in k for k in keys), (
            "corrupt artifact must NOT reach the File Master"
        )
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_repairable_artifact_copies_repaired(self, tmp_path, monkeypatch):
        """Repairable corruption → the FM receives the REPAIRED bytes."""
        import scraper.tasks as tasks
        import src.artifacts as artifacts

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        corrupt = b'{"a": "keep me", "bad": 25 offices with counts,}'
        (ws / "test_report.json").write_bytes(corrupt)

        written = {}
        monkeypatch.setattr(artifacts, "write", lambda key, data: written.setdefault(key, data))
        from django.conf import settings as dj_settings
        monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path))

        tasks._publish_analysis_artifacts(1, "s", ws)

        sent = [v for k, v in written.items() if "test_report.json" in k]
        assert sent, "repairable artifact must still be copied"
        data = json.loads(sent[0].decode("utf-8"))
        assert data == {"a": "keep me"}

    def test_valid_artifact_byte_identical(self, tmp_path, monkeypatch):
        import scraper.tasks as tasks
        import src.artifacts as artifacts

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        raw = b'{\n "platform": "sfcc",\n "n": 1\n}\n'
        (ws / "site_analysis.json").write_bytes(raw)

        written = {}
        monkeypatch.setattr(artifacts, "write", lambda key, data: written.setdefault(key, data))
        from django.conf import settings as dj_settings
        monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path))

        tasks._publish_analysis_artifacts(1, "s", ws)
        sent = [v for k, v in written.items() if "site_analysis.json" in k]
        assert sent and sent[0] == raw, "valid file must pass through byte-identical"


# ─── 2. _invoke_skill_learner: learning reports → FM ─────────────────────────

class TestSkillLearnerCopyGuard:
    def _run(self, tmp_path, monkeypatch, body):
        import agents.graph as g
        import src.artifacts as artifacts

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "learning_report.json").write_bytes(
            body if isinstance(body, bytes) else body.encode()
        )
        written = {}
        monkeypatch.setattr(artifacts, "write", lambda key, data: written.setdefault(key, data))
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        from django.conf import settings as dj_settings
        monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path))

        class _Stub:
            def invoke(self, *a, **k):
                return {"messages": []}

        monkeypatch.setattr(g, "build_skill_learner_message", lambda state: [])
        monkeypatch.setattr(g, "create_skill_learner", lambda site_slug="": _Stub())
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(g, "_log_agent_context", lambda *a, **k: None)
        from langgraph.types import RunnableConfig

        g._invoke_skill_learner(
            {"job_id": 0, "site_slug": "s", "execution_status": "SUCCESS"},
            RunnableConfig(),
        )
        return written

    def test_unrepairable_report_skipped(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level(logging.ERROR, logger="agents.graph"):
            written = self._run(tmp_path, monkeypatch, b"<<<garbage>>>")
        assert not [k for k in written if "learning_report" in k], (
            "unrepairable report must not be copied to the FM"
        )
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_repairable_report_copies_repaired(self, tmp_path, monkeypatch):
        corrupt = b'{"a": "keep", "bad": 25 offices with counts,}'
        written = self._run(tmp_path, monkeypatch, corrupt)
        sent = [v for k, v in written.items() if "learning_report" in k]
        assert sent
        assert json.loads(sent[0].decode("utf-8")) == {"a": "keep"}

    def test_valid_report_byte_identical(self, tmp_path, monkeypatch):
        raw = b'{\n "patterns": []\n}\n'
        written = self._run(tmp_path, monkeypatch, raw)
        sent = [v for k, v in written.items() if "learning_report" in k]
        assert sent and sent[0] == raw


# ─── 3. setup_workspace re-hydration: FM analysis/* → workspace ─────────────

class TestRestoreFromArchiveGuard:
    def _restore(self, tmp_path, monkeypatch, body):
        import src.artifacts as artifacts
        from agents.nodes.setup_workspace import _restore_from_archive

        monkeypatch.setattr(artifacts, "exists", lambda key: True)
        monkeypatch.setattr(
            artifacts, "read", lambda key: body if isinstance(body, bytes) else body.encode()
        )
        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        ok = _restore_from_archive("s", str(ws), "site_analysis.json")
        p = ws / "site_analysis.json"
        return ok, (p.read_bytes() if p.exists() else None)

    def test_corrupt_fm_copy_not_rehydrated(self, tmp_path, monkeypatch, caplog):
        """A corrupt FM copy is quarantined rather than faithfully restored
        into the next job's workspace (corruption is durable across jobs)."""
        import logging as _l

        with caplog.at_level(_l.ERROR, logger="agents.nodes.setup_workspace"):
            ok, content = self._restore(tmp_path, monkeypatch, b"<<<not json>>>")
        assert content is None, "unrepairable FM bytes must not land in the workspace"
        assert ok is False
        assert any(r.levelno >= _l.ERROR for r in caplog.records)

    def test_repairable_fm_copy_rehydrated_repaired(self, tmp_path, monkeypatch):
        ok, content = self._restore(
            tmp_path, monkeypatch, b'{"a": "keep", "bad": 25 offices with counts,}'
        )
        assert content is not None
        assert json.loads(content.decode("utf-8")) == {"a": "keep"}

    def test_valid_fm_copy_byte_identical(self, tmp_path, monkeypatch):
        raw = b'{\n "platform": "sfcc"\n}\n'
        ok, content = self._restore(tmp_path, monkeypatch, raw)
        assert ok is True
        assert content == raw

    def test_existing_workspace_copy_left_alone(self, tmp_path, monkeypatch):
        import src.artifacts as artifacts
        from agents.nodes.setup_workspace import _restore_from_archive

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "site_analysis.json").write_bytes(b'{"local": 1}')
        monkeypatch.setattr(artifacts, "exists", lambda key: True)
        monkeypatch.setattr(artifacts, "read", lambda key: b'{"fm": 2}')
        ok = _restore_from_archive("s", str(ws), "site_analysis.json")
        assert ok is False
        assert json.loads((ws / "site_analysis.json").read_bytes()) == {"local": 1}
