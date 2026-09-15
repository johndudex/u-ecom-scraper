"""Wave-33 T33-8 (C3): rc≠0 containment + scoped retry caps.

Four deliverables (docs/plans/wave33-browser-oom-plan.md §C3):

1. scraper_runner: on any non-zero scraper exit — especially returncode ≤
   -9 (the group leader was SIGKILLed; grandchildren escaped) — killpg the
   group AND run a one-shot chrome sweep. The BFS-from-dead-parent orphan
   killer cannot reach the reparented cloak Chromium (parent dead → hangs
   off init), so the sweep anchors on /proc starttime: only chrome
   processes BORN during this run, outside the protected set (persistent
   ∪ registry-live ∪ sibling-run trees), die. Four safety layers: pattern,
   anchor, protected set, and the anchored kill itself.
2. webapp: crash-retries stay load-bearing (F2/F3) but bounded —
   attempts = min(payload cap, max(2, floor(slack/timeout))).
3. short-probe callers (shell_tools / field_confirmation / views re-run)
   send max_retries=1 (graph.py:7295 precedent).
4. the rc=-9 fixture scraper is an explicit deliverable and drives the
   end-to-end test below (real subprocesses: escaped grandchild observed
   alive after the group kill, dead after the sweep).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_c3.py -q
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

RUNNER_PATH = os.path.join(ROOT, "browser_service", "scraper_runner.py")
FIXTURE_PATH = os.path.join(ROOT, "tests", "fixtures", "c3_rc_minus9_scraper.py")


def _grab(name: str, path: str = RUNNER_PATH) -> str:
    src = pathlib.Path(path).read_text()
    m = re.search(
        rf"^((?:async )?def {name}\(.*?)(?=^(?:async )?def |^@|^class |\Z)",
        src,
        re.M | re.S,
    )
    assert m, f"{name} not found in {path}"
    return m.group(1)


_SR: dict = {}


def _sr():
    """The REAL scraper_runner module, loaded by file path.

    ``import browser_service.scraper_runner`` would execute the package
    __init__ → server.py → fastapi (absent in the django test image).
    Package-less loading also means the two cross-module helpers
    (browser_pool / server registry) degrade — which the sweep is designed
    to survive.
    """
    import importlib.util

    if "mod" not in _SR:
        spec = importlib.util.spec_from_file_location("c3_scraper_runner", RUNNER_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SR["mod"] = mod
    return _SR["mod"]


# ── 1. the sweep itself (unit, fake pgrep + fake starttimes) ────────────────


class TestChromeSweep:
    @staticmethod
    def _wire(monkeypatch, pids, starttimes):
        """Exec _chrome_sweep bound to the (patched) module's globals; returns
        (fn, killed_spy)."""
        sr = _sr()
        killed: list[int] = []
        monkeypatch.setattr(sr, "_pgrep_chrome_pids", lambda: list(pids))
        monkeypatch.setattr(sr, "_proc_starttime_ticks", lambda p: starttimes.get(p))
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
        ns: dict = {
            "logger": logging.getLogger("t33c3"),
            "os": os,
            "signal": __import__("signal"),
            "_pgrep_chrome_pids": sr._pgrep_chrome_pids,
            "_proc_starttime_ticks": sr._proc_starttime_ticks,
        }
        exec(compile(_grab("_chrome_sweep"), "<sweep>", "exec"), ns)
        return ns["_chrome_sweep"], killed

    def test_kills_only_post_anchor_unprotected(self, monkeypatch):
        anchor = 1_000_000
        sweep, killed = self._wire(
            monkeypatch,
            pids=[11, 12, 13, 14],
            starttimes={11: 999_000, 12: anchor + 5, 13: anchor + 5},
        )
        out = sweep(anchor, protected={12})
        # 11 pre-anchor (someone else's old chrome), 12 protected (persistent /
        # sibling / registry), 14 gone since pgrep (starttime unreadable) —
        # only 13 dies
        assert killed == [13]
        assert out == [13]

    def test_none_anchor_kills_nothing(self, monkeypatch):
        sweep, killed = self._wire(monkeypatch, pids=[11], starttimes={11: 5})
        assert sweep(None, protected=set()) == []
        assert killed == []

    def test_gone_since_pgrep_is_skipped(self, monkeypatch):
        sweep, killed = self._wire(monkeypatch, pids=[11], starttimes={})
        assert sweep(1_000_000, protected=set()) == []
        assert killed == []

    def test_pgrep_fallback_walks_proc(self, monkeypatch):
        """pgrep can be absent in slim images — the /proc cmdline walk is the
        fallback and must match the same pattern family."""
        sr = _sr()
        monkeypatch.setattr(
            sr.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no pgrep")),
        )
        monkeypatch.setattr(
            os, "listdir", lambda p: ["1", "42", "notapid", "77"]
        )

        def fake_read(path, mode="r"):
            if path.endswith("cmdline"):
                import io

                return io.BytesIO(
                    {
                        "/proc/42/cmdline": b"/usr/bin/chromium-fixture -x\x00",
                        "/proc/77/cmdline": b"python3 -m pytest\x00",
                    }.get(path, b"")  # unknown pids read as empty (no match)
                )
            raise OSError(path)

        import builtins

        real_open = builtins.open
        monkeypatch.setattr(
            builtins, "open", lambda f, m="r", *a, **k: fake_read(f, m)
        )
        assert sr._pgrep_chrome_pids() == [42]


# ── 1b. the protected set ───────────────────────────────────────────────────


class TestProtectedSet:
    def _ns(self, **overrides):
        ns: dict = {
            "logger": logging.getLogger("t33c3"),
            "threading": threading,
            "_persistent_chrome_pids": lambda: {100, 200},
            "_descendants": lambda p: {p, p + 1},
            "_registry_live_pids": lambda: {400, 401},
            "_ACTIVE_RUNS": {"rid-x": {"pid": 300}},
            "_ACTIVE_RUNS_LOCK": threading.Lock(),
        }
        ns.update(overrides)
        exec(compile(_grab("_protected_pids_for_sweep"), "<prot>", "exec"), ns)
        return ns

    def test_union_of_three_sources(self):
        ns = self._ns()
        assert ns["_protected_pids_for_sweep"]() == {
            100, 101, 200, 201, 300, 301, 400, 401,
        }

    def test_source_failure_degrades_never_raises(self):
        def boom():
            raise ImportError("no browser_pool in slim containers")

        ns = self._ns(_persistent_chrome_pids=boom)
        assert ns["_protected_pids_for_sweep"]() == {300, 301, 400, 401}


class TestContainmentWiring:
    def test_nonzero_exit_arm_runs_containment(self):
        src = pathlib.Path(RUNNER_PATH).read_text()
        impl = src[src.index("def _run_scraper_script_impl"):]
        i_zero = impl.index("if result.returncode == 0:")
        tail = impl[i_zero: i_zero + 2500]
        assert "_contain_failed_attempt(proc" in tail, (
            "the rc≠0 arm must killpg + sweep before the retry decision"
        )

    def test_timeout_arm_runs_the_sweep(self):
        src = pathlib.Path(RUNNER_PATH).read_text()
        impl = src[src.index("def _run_scraper_script_impl"):]
        arm = impl[impl.index("except subprocess.TimeoutExpired:"):]
        head = arm[: arm.index("return {")]
        assert "_chrome_sweep(" in head, (
            "escaped cloak chrome survives the timeout killpg — sweep there too"
        )

    def test_containment_killpg_first_then_sweep(self):
        fn = _grab("_contain_failed_attempt")
        assert fn.index("os.killpg(") < fn.index("_chrome_sweep(")

    def test_child_starttime_captured_at_spawn(self):
        src = pathlib.Path(RUNNER_PATH).read_text()
        impl = src[src.index("def _run_scraper_script_impl"):]
        i_spawn = impl.index("subprocess.Popen(")
        i_cancel = impl.index("if _spawn_cancelled:")
        head = impl[i_spawn:i_cancel]
        assert "_proc_starttime_ticks(proc.pid)" in head, (
            "anchor = the attempt's own birth tick (grandchildren are born after)"
        )


# ── 2. budget-bounded attempts (webapp) ─────────────────────────────────────


class TestBoundedAttempts:
    @staticmethod
    def _fn():
        from agents.tools.browser_http import bounded_scrape_attempts

        return bounded_scrape_attempts

    def test_main_execution_gets_crash_recovery(self):
        # slack 240s of a 3600s timeout fits zero extra FULL runs — but the
        # floor guarantees ONE crash recovery (F2/F3)
        assert self._fn()(3600) == 2

    def test_short_timeouts_fit_more_attempts(self):
        # slack 240 / timeout 60 → 4 full runs fit, payload cap 3 wins
        assert self._fn()(60, payload_cap=3) == 3

    def test_payload_cap_honored_below_floor(self):
        # an explicit cap of 1 (short probes) must NOT be raised to 2
        assert self._fn()(300, payload_cap=1) == 1

    def test_run_execution_payload_carries_the_bound(self):
        src = pathlib.Path(
            os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")
        ).read_text()
        assert '"max_retries": bounded_scrape_attempts(timeout)' in src

    def test_run_execution_imports_the_helper(self):
        src = pathlib.Path(
            os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")
        ).read_text()
        head = src.split("def _run_via_browser_service")[1][:2000]
        assert "bounded_scrape_attempts" in head


# ── 3. short-probe callers pin max_retries=1 ────────────────────────────────


class TestShortProbeCallers:
    def test_shell_tools_probe_pin(self):
        src = pathlib.Path(
            os.path.join(ROOT, "webapp", "agents", "tools", "shell_tools.py")
        ).read_text()
        head = src[: src.index('"job_id": _scrape_job_id')]
        assert head.count('"max_retries": 1') == 1, (
            "the tester's /scrape payload must pin max_retries=1"
        )

    def test_field_confirmation_pin(self):
        src = pathlib.Path(
            os.path.join(ROOT, "webapp", "agents", "nodes", "field_confirmation.py")
        ).read_text()
        i = src.index('"timeout": 300,')
        assert '"max_retries": 1' in src[i: i + 120]

    def test_views_rerun_pin(self):
        src = pathlib.Path(
            os.path.join(ROOT, "webapp", "scraper", "views.py")
        ).read_text()
        i = src.index('"timeout": 3600,')
        assert '"max_retries": 1' in src[i: i + 220]


# ── 4. the rc=-9 fixture, end to end (REAL processes) ───────────────────────


class TestRcMinus9Fixture:
    def test_fixture_source_shape(self):
        src = pathlib.Path(FIXTURE_PATH).read_text()
        assert "start_new_session=True" in src, (
            "the grandchild must ESCAPE the group (the cloak shape)"
        )
        assert "killpg" in src, "the fixture must kill its own group"

    def test_group_kill_leaves_grandchild_then_sweep_reaps_it(self, caplog):
        """The C3 deliverable end-to-end: the runner observes rc=-9 (its group
        leader was SIGKILLed) and the containment sweep reaps the ESCAPED
        grandchild — the exact orphan the BFS-from-dead-parent killer can
        never see. The sweep's log line naming the grandchild pid is what
        proves the kill came from the sweep and not the group kill (a
        same-group grandchild would never appear there)."""
        import logging as _logging

        assert os.path.isfile(FIXTURE_PATH)
        with caplog.at_level(_logging.WARNING, logger="c3_scraper_runner"):
            result = _sr().run_scraper_script(
                scraper_path=FIXTURE_PATH,
                args=[],
                timeout=60,
                max_retries=1,
                rid="c3-fixture-test",
            )
        assert result["returncode"] == -9, (result.get("stderr") or "")[:300]

        gc_pid = int(
            re.search(r"GRANDCHILD (\d+)", result.get("stdout") or "").group(1)
        )
        # the sweep (not the group kill) reaped the escaped grandchild…
        assert "C3 sweep: killed 1" in caplog.text
        assert str(gc_pid) in caplog.text
        # …and it is gone.
        deadline = time.time() + 10
        while time.time() < deadline and _alive(gc_pid):
            time.sleep(0.25)
        assert not _alive(gc_pid), (
            f"escaped grandchild {gc_pid} survived the containment sweep"
        )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
