"""Wave-32 tier A: probe honesty (A1 stderr + exit classification, A2 [PROBE] row).

Job 587's smoke probe ran the draft, got rc=1 ("No --query, --category-url, or
--listing-url provided"), logged it as "OK", and discarded stderr — the one
component that tests Phase 1 had the diagnosis and threw it away. A1 pins:
`OK` only for rc == 0; any other rc without a crash signature logs the exit,
the rc, and the stderr tail.

Run: docker compose exec -T django sh -c "cd /app && pytest tests/test_wave32_probe_honesty.py -q"
"""
from __future__ import annotations

import logging
import os
import subprocess as _subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()


def _probe_draft() -> str:
    return (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--discover-only', action='store_true')\n"
        "p.add_argument('--fresh-discovery', action='store_true')\n"
    )


def _run_probe(monkeypatch, tmp_path, rc, stderr, stdout=""):
    """Probe with a draft whose subprocess exits `rc` carrying `stderr`."""
    from django.test.utils import override_settings

    from webapp.agents.graph import _probe_phase1_discovery

    ws = tmp_path / "workspace" / "s"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_text(_probe_draft(), encoding="utf-8")

    def fake_run(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(_subprocess, "run", fake_run)
    with override_settings(PROJECT_ROOT=str(tmp_path)):
        return _probe_phase1_discovery("s", {"input_mode": "list_page"}, 1)


class TestA1HonestExitClassification:
    def test_nonzero_exit_logs_stderr_tail(self, monkeypatch, tmp_path, caplog):
        """rc=1 without a Traceback is 'no crash signature', never 'OK' — and
        the stderr tail must ride the log line. ([wave-32 C1] the marker
        stderr itself is now the NAMED no_discovery_source outcome with its
        own tests; this generic path uses a neutral tail.)"""
        tail = "connect timeout after 30000ms — no traceback"
        with caplog.at_level(logging.INFO):
            result = _run_probe(
                monkeypatch, tmp_path, 1, stderr=tail + "\n", stdout="",
            )
        # Return contract unchanged: non-crash nonzero stays inconclusive.
        assert result == (False, None, None)
        honest = [r for r in caplog.records if "no crash signature" in r.getMessage()]
        assert honest, "rc=1 without a Traceback must log the no-crash-signature line"
        msg = honest[0].getMessage()
        assert "rc=1" in msg and "exit=1" in msg
        assert tail in msg, "the stderr tail must be appended to the log line"
        assert not [r for r in caplog.records if "OK (job" in r.getMessage()], (
            "'OK' must be reserved for rc == 0"
        )

    def test_zero_exit_still_logs_ok(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level(logging.INFO):
            result = _run_probe(monkeypatch, tmp_path, 0, stderr="", stdout="")
        assert result == (False, None, None)
        assert [r for r in caplog.records if "OK (job" in r.getMessage()], (
            "rc == 0 keeps the OK classification"
        )

    def test_crash_classification_untouched(self, monkeypatch, tmp_path, caplog):
        """A Traceback rc≠0 keeps the CRASHED verdict (True, tail, None)."""
        with caplog.at_level(logging.INFO):
            result = _run_probe(
                monkeypatch, tmp_path, 1,
                stderr="Traceback (most recent call last):\nBoomError: x\n",
            )
        assert result[0] is True and result[1] and "BoomError" in result[1]


class TestA2ProbeSessionLogRow:
    """Every smoke probe writes one SessionLog `[PROBE]` row: rc, stderr tail,
    the resolved listing candidate, the owned-output count, and the probe-cache
    state for the job's domain — the next 587 must be readable from the job
    log alone."""

    def _job(self):
        from scraper.models import ScrapeJob
        from django.contrib.auth.models import User

        u = User.objects.create_user(username=f"_t_probe_{self._n()}", password="x")
        return ScrapeJob.objects.create(
            url="https://shop.example.com/c/mens", user=u, created_via="api",
            status="running", input_mode="list_page", page_type="product",
        )

    _counter = [0]

    @classmethod
    def _n(cls):
        cls._counter[0] += 1
        return cls._counter[0]

    def _probe_with_job(self, monkeypatch, tmp_path, rc, stderr=""):
        from django.test.utils import override_settings

        from webapp.agents.graph import _probe_phase1_discovery

        job = self._job()
        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text(_probe_draft(), encoding="utf-8")

        def fake_run(argv, **kwargs):
            return _subprocess.CompletedProcess(argv, rc, stdout="", stderr=stderr)

        monkeypatch.setattr(_subprocess, "run", fake_run)
        with override_settings(PROJECT_ROOT=str(tmp_path)):
            result = _probe_phase1_discovery("s", {"input_mode": "list_page"}, job.id)
        return job, result

    @pytest.mark.django_db
    def test_probe_writes_sessionlog_row(self, monkeypatch, tmp_path):
        # [wave-32 C1] a stderr carrying C1's no-discovery-source marker is
        # now the NAMED outcome (outcome=no_discovery_source) with its own
        # tests — the generic rc=1 row here uses a neutral tail.
        tail = "connect timeout after 30000ms — no traceback frames"
        job, result = self._probe_with_job(monkeypatch, tmp_path, 1, stderr=tail)
        from scraper.models import SessionLog

        rows = list(
            SessionLog.objects.filter(job_id=job.id, agent="probe").order_by("seq")
        )
        assert rows, "the smoke probe must write a [PROBE] SessionLog row"
        content = rows[-1].content
        assert "[PROBE]" in content
        assert "rc=1" in content
        assert "no crash signature" in content
        assert tail[:60] in content, "stderr tail rides the row"
        assert "candidate=" in content, "resolved listing candidate is recorded"
        assert "owned_outputs=" in content, "output-detection count is recorded"
        assert "cache=" in content, "probe-cache state for the domain is recorded"

    @pytest.mark.django_db
    def test_row_written_on_success_and_crash_paths(self, monkeypatch, tmp_path):
        from scraper.models import SessionLog

        job, _ = self._probe_with_job(monkeypatch, tmp_path, 0)
        n0 = SessionLog.objects.filter(job_id=job.id, agent="probe").count()
        assert n0 == 1, "rc=0 probe writes its row too"
        job2, _ = self._probe_with_job(
            monkeypatch, tmp_path, 1, stderr="Traceback (most recent call last):\nE: x\n"
        )
        n1 = SessionLog.objects.filter(job_id=job2.id, agent="probe").count()
        assert n1 == 1, "crashed probes write their row too"
        crashed_row = SessionLog.objects.filter(job_id=job2.id, agent="probe").first()
        assert "crashed" in crashed_row.content
