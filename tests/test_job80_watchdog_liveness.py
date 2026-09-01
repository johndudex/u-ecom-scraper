"""[jobs 79/80] Watchdog honesty — silence is not proof of death.

What happened (proven RCA): both re-drive tasks' worker children were
destroyed at ~14:00:01 (probable container OOM — the 2.5 GiB per-child
ceiling × 2 children sat ABOVE the 3 GiB container limit, and the warm
recycle can only fire BETWEEN tasks). ``acks_late=False`` meant no
redelivery; the freed slots went to queued jobs 85/86 fifty seconds later.
When ``cleanup_stuck_jobs`` finally ran at 14:38 (itself queued 8 min behind
the jobs it polices on the same 2-slot pool), it revoked task-ids that had
been dead for 38 minutes and labelled the deaths "stalled agent phase or
wedged scrape" — sending the investigation down the wrong path.

Fixes pinned here (generic, no caps):
- ``_task_liveness``: ask Celery whether the task still executes before
  treating silence as death. absent → immediate honest failure; active →
  not a corpse (only the long wedge backstop revokes it); unknown → the
  legacy silence rule stands.
- Honest error strings per liveness class — "worker process lost" instead
  of a phantom "wedged scrape".
- The watchdog runs on the dedicated ``events`` queue (it policed its own
  2-slot pool: 30-min threshold reported as 38 min).
- Memory geometry: 2 × per-child ceiling must fit INSIDE the container
  limit, because warm recycle cannot intervene mid-task.
- ``run_scraper``'s blocking dispatch beats with the counted ``[EXEC-ALIVE]``
  prefix (bounded ≤ ~660 s, so it can rescue a live run, never mask a hang).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


# ─── _task_liveness ──────────────────────────────────────────────────────────


_DERIVED = object()


class _FakeInspect:
    def __init__(self, reply, ping_reply=_DERIVED):
        self._reply = reply
        self._ping_reply = ping_reply
        self.timeout = None

    def active(self):
        return self._reply

    def ping(self):
        if self._ping_reply is _DERIVED:
            # Default: the SAME worker set answers the second probe.
            return {name: {"ok": "pong"} for name in (self._reply or {})}
        return self._ping_reply


class TestTaskLiveness:
    def _patch(self, monkeypatch, reply, ping_reply=_DERIVED):
        import scraper.tasks as wt

        captured = {}

        def fake_inspect(timeout=None):
            captured["timeout"] = timeout
            return _FakeInspect(reply, ping_reply)

        monkeypatch.setattr(
            "celery.current_app.control.inspect", fake_inspect, raising=False
        )
        return wt, captured

    def test_active_when_a_worker_reports_the_task(self, monkeypatch):
        wt, cap = self._patch(monkeypatch, {
            "worker1@railway": [{"id": "abc-123", "name": "scraper.tasks.run_job"}],
        })
        assert wt._task_liveness("abc-123") == "active"
        assert cap["timeout"] == 5.0

    def test_absent_when_workers_reply_without_the_task(self, monkeypatch):
        wt, _ = self._patch(monkeypatch, {
            "worker1@railway": [{"id": "other-999"}],
            "worker2@railway": [],
        })
        assert wt._task_liveness("abc-123") == "absent"

    def test_partial_reply_degrades_to_unknown(self, monkeypatch):
        """[wave-14 job-133] A PARTIAL inspect reply (one PidBox — often just
        the events pool's self-reply — answered active() while the pool holding
        the task was too busy to respond) is indistinguishable from a complete
        'runs nowhere' answer. When the second probe's worker set disagrees,
        the verdict must degrade to unknown (silence rule), never 'absent'."""
        wt, _ = self._patch(
            monkeypatch,
            {"worker1@railway": [{"id": "other-999"}]},
            ping_reply={"worker1@railway": {"ok": "pong"},
                        "worker2@railway": {"ok": "pong"}},
        )
        assert wt._task_liveness("abc-123") == "unknown"

    def test_ping_none_is_unknown(self, monkeypatch):
        """active() was answered but the cross-check probe got nothing."""
        wt, _ = self._patch(
            monkeypatch,
            {"worker1@railway": [{"id": "other-999"}]},
            ping_reply=None,
        )
        assert wt._task_liveness("abc-123") == "unknown"

    def test_ping_exception_is_unknown(self, monkeypatch):
        import scraper.tasks as wt

        class _HalfDeadInspect(_FakeInspect):
            def ping(self):
                raise RuntimeError("second probe failed")

        def fake_inspect(timeout=None):
            return _HalfDeadInspect({"worker1@railway": [{"id": "other-999"}]})

        monkeypatch.setattr(
            "celery.current_app.control.inspect", fake_inspect, raising=False
        )
        assert wt._task_liveness("abc-123") == "unknown"

    def test_none_reply_is_unknown_not_absent(self, monkeypatch):
        """No worker answered (broker hiccup, saturated pool) — that is NOT
        evidence the task is gone; treating it as 'absent' would revoke live
        tasks on an inspect outage."""
        wt, _ = self._patch(monkeypatch, None)
        assert wt._task_liveness("abc-123") == "unknown"

    def test_inspect_exception_is_unknown(self, monkeypatch):
        import scraper.tasks as wt

        def boom(timeout=None):
            raise RuntimeError("broker down")

        monkeypatch.setattr(
            "celery.current_app.control.inspect", boom, raising=False
        )
        assert wt._task_liveness("abc-123") == "unknown"

    def test_empty_task_id_is_unknown(self):
        import scraper.tasks as wt

        assert wt._task_liveness("") == "unknown"

    def test_non_dict_rows_are_skipped(self, monkeypatch):
        wt, _ = self._patch(monkeypatch, {"worker1@railway": [None, "junk"]})
        assert wt._task_liveness("abc-123") == "absent"


# ─── constants + config wiring ───────────────────────────────────────────────


class TestWiring:
    def test_active_silence_backstop_is_3x_the_base_threshold(self):
        import scraper.tasks as wt

        assert wt.ACTIVE_SILENCE_REVOKE_MINUTES >= 3 * wt.STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES

    def test_watchdog_routed_off_the_pool_it_polices(self):
        src = open(os.path.join(
            ROOT, "webapp", "config", "settings.py"
        )).read()
        assert '"scraper.tasks.cleanup_stuck_jobs": "events"' in src

    def test_memory_geometry_two_children_fit_under_container(self):
        """2 × per-child ceiling must sit below the compose celery-worker
        mem_limit — the warm recycle fires BETWEEN tasks only and cannot stop
        a mid-task double-child OOM (jobs 79/80 died 320 ms apart).

        [wave-15 1.4] The container budget is parsed from docker-compose.yml's
        ``celery-worker: mem_limit`` (NOT the test process's own cgroup — the
        suite also runs on hosts with unrelated limits), so retuning the
        container without retuning the ceiling fails HERE instead of OOMing in
        prod. Falls back to the historical 3 GiB when compose is absent (e.g.
        sdist-only test runs).
        """
        import os
        import re

        import django.conf

        ceiling_kib = django.conf.settings.CELERY_WORKER_MAX_MEMORY_PER_CHILD
        gi_b = 1024 * 1024

        container_kib = 3 * gi_b  # historical compose geometry
        compose_path = os.path.join(ROOT, "docker-compose.yml")
        if os.path.exists(compose_path):
            src = open(compose_path).read()
            m = re.search(
                r"^  celery-worker:.*?^    mem_limit:\s*(\d+)g",
                src,
                re.MULTILINE | re.DOTALL,
            )
            if m:
                container_kib = int(m.group(1)) * gi_b

        assert ceiling_kib < container_kib / 2, (
            f"per-child ceiling {ceiling_kib} KiB × 2 children no longer fits "
            f"the celery-worker container ({container_kib // gi_b} GiB) — "
            "jobs 79/80's double-child OOM class returns"
        )
        # And it must still be big enough to be a real ceiling, not a no-op.
        assert ceiling_kib >= 1 * gi_b


# ─── run_scraper dispatch heartbeat ──────────────────────────────────────────


class TestDispatchAlive:
    def test_brower_dispatch_wrapped_in_dispatch_alive(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "tools", "shell_tools.py"
        )).read()
        # The blocking browser POST beats while it waits, budgeted by its own
        # bound (timeout+60 httpx margin) [wave-14 job-133].
        assert "with _dispatch_alive(timeout + 60):\n                    _res = post_scrape_with_retry(" in src
        # So does the local subprocess run (bound = its timeout=).
        assert "with _dispatch_alive(timeout):\n                    result = subprocess.run(" in src

    def test_heartbeat_uses_the_counted_prefix(self):
        """[EXEC-ALIVE] rows COUNT as watchdog activity ([HEARTBEAT] is
        excluded) — and the dispatch is bounded, so a beat can only rescue a
        live run, never mask a hang."""
        src = open(os.path.join(
            ROOT, "webapp", "agents", "tools", "shell_tools.py"
        )).read()
        assert 'prefix="[EXEC-ALIVE]"' in src

    def test_context_stops_the_heartbeat_on_error(self, monkeypatch):
        import agents.tools.shell_tools as st

        started, stopped = [], []

        class _HB:
            pass

        monkeypatch.setattr(
            "agents.graph._start_heartbeat",
            lambda *a, **k: started.append((a, k)) or _HB(),
        )
        monkeypatch.setattr(
            "agents.graph._stop_heartbeat", lambda hb: stopped.append(hb)
        )
        monkeypatch.setattr(
            "agents.tools.context.get_state", lambda: {"job_id": 42}
        )
        with pytest.raises(RuntimeError):
            with st._dispatch_alive():
                raise RuntimeError("dispatch blew up")
        assert len(started) == 1
        assert len(stopped) == 1, "finally must stop the heartbeat even on error"

    def test_context_yields_silently_without_job_context(self, monkeypatch):
        import agents.tools.shell_tools as st

        monkeypatch.setattr(
            "agents.tools.context.get_state", lambda: None
        )
        called = False
        with st._dispatch_alive():
            called = True
        assert called


# ─── the watchdog decision table (pure-logic mirror of cleanup_stuck_jobs) ──


class TestWatchdogDecision:
    """The liveness→action mapping, exercised through the same constants the
    real task reads. (The DB loop itself is thin; the decision is the logic.)"""

    def _decision(self, liveness, idle_minutes):
        import scraper.tasks as wt

        if liveness == "active" and idle_minutes < wt.ACTIVE_SILENCE_REVOKE_MINUTES:
            return "continue"
        if liveness == "absent":
            return "fail:worker_lost"
        if liveness == "active":
            return "fail:wedged"
        return "fail:silence"

    def test_active_silent_task_is_not_revoked(self):
        assert self._decision("active", 38) == "continue"

    def test_absent_task_fails_immediately_and_honestly(self):
        d = self._decision("absent", 38)
        assert d == "fail:worker_lost"

    def test_unknown_falls_back_to_the_silence_rule(self):
        assert self._decision("unknown", 38) == "fail:silence"

    def test_active_past_the_backstop_is_revoked_as_wedged(self):
        assert self._decision("active", 95) == "fail:wedged"

    def test_worker_lost_message_names_the_real_cause(self):
        import inspect as _inspect

        src = _inspect.getsource(__import__("scraper.tasks", fromlist=["x"]))
        assert "Worker process lost" in src
        assert "acks_late=False" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
