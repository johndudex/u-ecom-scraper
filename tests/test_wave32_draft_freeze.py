"""[wave-32 D3+D4] Known-good draft freeze + negative-progress tripwire.

D3: the per-job FM draft key (``scraper-draft-{job_id}.py``) is snapshotted
after EVERY writer invocation — including the invocation that left the
draft BROKEN (587: a SyntaxError draft clobbered the last restorable copy).
A second ``-good`` key freezes the last PARSEABLE draft, and both restore
paths (``draft_safety.restore_job_draft`` — the tester-entry/step-death
path — and the ``setup_workspace`` watchdog re-drive) prefer it when the
latest archive does not parse. Parseable-but-untested drafts are NEVER
rolled back.

D4: a fix cycle under a FAIL verdict that grows the draft >10% with no
field passing testing is bloat, not progress (587: 107→124KB). Log-only
``[DRAFT-BLOAT]`` SessionLog row this wave.
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

# the package re-exports the node FUNCTION under the same name — import the
# true module by path.
import importlib  # noqa: E402

import pytest  # noqa: E402

import webapp.agents.graph as g  # noqa: E402
from webapp.agents import draft_safety as ds  # noqa: E402

sw = importlib.import_module("webapp.agents.nodes.setup_workspace")

GOOD_DRAFT = (
    b"import argparse\n"
    b"parser = argparse.ArgumentParser()\n"
    b"parser.add_argument('--sample', action='store_true')\n"
    b"print('ok')\n"
)

BROKEN_DRAFT = (
    b"def _dom_price(soup) -> str joined_marker:\n"
    b"    return ''\n"
)


class _FakeArtifacts:
    """In-memory stand-in for src.artifacts (the FM HTTP client)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def write(self, key, data):
        self.store[key] = bytes(data)
        return len(data)

    def read(self, key, timeout=None):
        if key not in self.store:
            raise FileNotFoundError(key)
        return self.store[key]

    def exists(self, key):
        return key in self.store

    def scrapers_key(self, slug, *parts):
        return "/".join(("scrapers", slug) + parts)


@pytest.fixture()
def fake_art(monkeypatch):
    art = _FakeArtifacts()
    import src.artifacts as real_art

    monkeypatch.setattr(real_art, "write", art.write)
    monkeypatch.setattr(real_art, "read", art.read)
    monkeypatch.setattr(real_art, "exists", art.exists)
    return art


def _seed_ws(tmp_path, body: bytes, slug="freeze-com"):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_bytes(body)
    return tmp_path


class TestGoodFreeze:
    def test_broken_draft_does_not_clobber_good_key(self, tmp_path, fake_art):
        root = str(tmp_path)
        _seed_ws(tmp_path, GOOD_DRAFT)
        assert ds.freeze_good_draft(root, "freeze-com", 42) is True
        good_key = fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-42-good.py")
        assert fake_art.exists(good_key)
        # The writer then leaves a BROKEN draft and the main snapshot runs:
        _seed_ws(tmp_path, BROKEN_DRAFT)
        assert ds.freeze_good_draft(root, "freeze-com", 42) is False, (
            "a freeze only ever records a PARSEABLE draft"
        )
        assert fake_art.read(good_key) == GOOD_DRAFT, (
            "a broken draft must not clobber the known-good freeze"
        )

    def test_freeze_refuses_missing_or_unparseable(self, tmp_path, fake_art):
        root = str(tmp_path)
        _seed_ws(tmp_path, BROKEN_DRAFT)
        assert ds.freeze_good_draft(root, "freeze-com", 7) is False
        assert ds.freeze_good_draft(root, "no-such-slug", 7) is False


class TestRestorePrefersGood:
    def test_step_death_restores_good_draft(self, tmp_path, fake_art):
        """restore_job_draft with a BROKEN latest archive must restore the
        known-good freeze instead (the 587 shape)."""
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-42.py"),
            BROKEN_DRAFT,
        )
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-42-good.py"),
            GOOD_DRAFT,
        )
        root = str(_seed_ws(tmp_path, BROKEN_DRAFT))
        (tmp_path / "workspace" / "freeze-com" / "scraper_draft.py").unlink()
        restored = ds.restore_job_draft(root, "freeze-com", 42)
        assert restored, "a restore must fire when the workspace draft is gone"
        assert open(restored, "rb").read() == GOOD_DRAFT, (
            "the restore must prefer the known-good freeze over a broken "
            "latest archive"
        )

    def test_latest_still_wins_when_it_parses(self, tmp_path, fake_art):
        """Parseable-but-untested latest is NOT rolled back to -good."""
        newer = GOOD_DRAFT + b"\n# newer accepted edit\n"
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-42.py"),
            newer,
        )
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-42-good.py"),
            GOOD_DRAFT,
        )
        root = str(tmp_path / "empty")
        restored = ds.restore_job_draft(root, "freeze-com", 42)
        assert restored and open(restored, "rb").read() == newer

    def test_broken_latest_without_good_restores_anyway(self, tmp_path, fake_art):
        """Legacy behavior kept when no freeze exists: the broken latest is
        still the best available evidence (the syntax fixer owns it)."""
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-42.py"),
            BROKEN_DRAFT,
        )
        root = str(tmp_path / "empty")
        restored = ds.restore_job_draft(root, "freeze-com", 42)
        assert restored and open(restored, "rb").read() == BROKEN_DRAFT


class TestSetupWorkspaceGoodPreference:
    def test_setup_workspace_restores_good_draft(self, tmp_path, fake_art):
        """The watchdog re-drive twin (setup_workspace.py per-job restore)
        gets the same -good preference."""
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-77.py"),
            BROKEN_DRAFT,
        )
        fake_art.write(
            fake_art.scrapers_key("freeze-com", "jobs", "scraper-draft-77-good.py"),
            GOOD_DRAFT,
        )
        ws = tmp_path / "workspace" / "freeze-com"
        ws.mkdir(parents=True, exist_ok=True)
        state = {"job_id": 77, "site_slug": "freeze-com"}
        sw._restore_job_draft_from_fm(state, "freeze-com", str(ws))
        got = (ws / "scraper_draft.py").read_bytes()
        assert got == GOOD_DRAFT, (
            "the watchdog restore must prefer the known-good freeze over a "
            "broken latest archive"
        )


class TestBloatTripwire:
    def _state(self, verdict="FAIL", tested="empty"):
        return {
            "test_report": {
                "overall_assessment": verdict,
                "remediation": {"target": "mapping", "fields": ["price"]},
                "field_verification": {"price": {"tested": tested}},
            },
            "last_tested_draft_bytes": 1000,
            "test_retry_count": 1,
        }

    def test_growth_under_fail_logs_bloat_row(self, tmp_path, monkeypatch):
        rows: list[str] = []
        monkeypatch.setattr(g, "_log_event_row", lambda j, a, c: rows.append(c))
        draft = tmp_path / "scraper_draft.py"
        draft.write_bytes(b"x" * 2000)  # +100% under the 1000-byte baseline
        assert g._log_bloat_tripwire(9, self._state(), str(draft)) is True
        assert rows and "[DRAFT-BLOAT]" in rows[0]
        assert "1000" in rows[0] and "2000" in rows[0]

    def test_no_growth_no_row(self, tmp_path, monkeypatch):
        rows: list[str] = []
        monkeypatch.setattr(g, "_log_event_row", lambda j, a, c: rows.append(c))
        draft = tmp_path / "scraper_draft.py"
        draft.write_bytes(b"x" * 1050)  # +5% — inside the threshold
        assert g._log_bloat_tripwire(9, self._state(), str(draft)) is False
        assert rows == []

    def test_pass_verdict_no_row(self, tmp_path, monkeypatch):
        rows: list[str] = []
        monkeypatch.setattr(g, "_log_event_row", lambda j, a, c: rows.append(c))
        draft = tmp_path / "scraper_draft.py"
        draft.write_bytes(b"x" * 2000)
        assert g._log_bloat_tripwire(9, self._state(verdict="PASS"), str(draft)) is False
        assert rows == []

    def test_field_passing_no_row(self, tmp_path, monkeypatch):
        """A field passing testing means there IS progress — no row."""
        rows: list[str] = []
        monkeypatch.setattr(g, "_log_event_row", lambda j, a, c: rows.append(c))
        draft = tmp_path / "scraper_draft.py"
        draft.write_bytes(b"x" * 2000)
        st = self._state(tested="pass")
        assert g._log_bloat_tripwire(9, st, str(draft)) is False
        assert rows == []

    def test_no_baseline_no_row(self, tmp_path, monkeypatch):
        rows: list[str] = []
        monkeypatch.setattr(g, "_log_event_row", lambda j, a, c: rows.append(c))
        draft = tmp_path / "scraper_draft.py"
        draft.write_bytes(b"x" * 2000)
        st = self._state()
        st["last_tested_draft_bytes"] = 0
        assert g._log_bloat_tripwire(9, st, str(draft)) is False
        assert rows == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
