"""Wave-33 T33-10 (D2): /probe-single deadline + second probe slot.

docs/plans/wave33-browser-oom-plan.md §D2:

1. The single-rung dispatch gets a server deadline derived from the REQUEST
   (``request.timeout + 30`` — the client window is ``PROBE_TIMEOUT+10=190s``
   and probe_tools clamps at 120, so 150 < 190 always holds). Before this the
   dispatch had NO bound: a wedged PROBE_EXECUTOR thread browsed forever for
   a caller that had long gone.
2. Capacity via a SECOND probe slot counter, deliberately NOT ``PROBE_LOCK``:
   serializing a listing probe behind a multi-minute /probe ladder (or /render)
   converts lock-wait into a false site verdict ("playwright failed" when the
   site was never tried). Excess callers get the same 429 shape the C1a
   callers already park on (throttle_retry_after).
3. Registry integration deferred from T33-4: browser-launching rungs register
   their roots in the ephemeral registry, so the deadline arm abandon-kills
   the launched browser (reuse-guarded) and the reaper sees the residue.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave33_tier_d2.py -q
"""
from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")


def _grab(name: str, path: str = SERVER_PATH) -> str:
    src = open(path, encoding="utf-8").read()
    m = re.search(
        rf"^((?:async )?def {name}\(.*?)"
        rf"(?=^(?:async )?def |^@|^class "
        rf"|^[A-Za-z_][A-Za-z0-9_]*\s*[:=]\s|\Z)",
        src,
        re.M | re.S,
    )
    assert m, f"{name} not found in {path}"
    return m.group(1)


def _probe_single_body() -> str:
    src = open(SERVER_PATH, encoding="utf-8").read()
    i = src.index('@app.post("/probe-single")')
    j = src.index('@app.post("/render")')
    return src[i:j]


LAUNCH_HOOK = "launch_hook=lambda ctx: _ephemeral_track(call_id, [ctx.root_pid])"


# ── 1. the slot counter (unit) ──────────────────────────────────────────────


class TestProbeSlots:
    @staticmethod
    def _ns(cap: int = 2) -> dict:
        ns: dict = {"PROBE_MAX_CONCURRENT": cap, "_probe_slots_in_use": 0}
        exec(compile(_grab("_probe_slot_try_acquire"), "<a>", "exec"), ns)
        exec(compile(_grab("_probe_slot_release"), "<b>", "exec"), ns)
        return ns

    def test_acquire_below_cap_then_refuse_at_cap(self):
        ns = self._ns(cap=2)
        assert ns["_probe_slot_try_acquire"]() is True
        assert ns["_probe_slot_try_acquire"]() is True
        assert ns["_probe_slot_try_acquire"]() is False, "third concurrent probe must be refused"

    def test_release_frees_the_slot(self):
        ns = self._ns(cap=1)
        acquire, release = ns["_probe_slot_try_acquire"], ns["_probe_slot_release"]
        assert acquire() is True
        assert acquire() is False
        release()
        assert acquire() is True

    def test_release_floors_at_zero_never_negative(self):
        ns = self._ns(cap=1)
        ns["_probe_slot_release"]()
        ns["_probe_slot_release"]()  # unbalanced release must not corrupt
        assert ns["_probe_slots_in_use"] == 0
        assert ns["_probe_slot_try_acquire"]() is True


# ── 2. /probe-single wiring ─────────────────────────────────────────────────


class TestProbeSingleWiring:
    def test_never_takes_probe_lock(self):
        """THE D2 constraint: lock-wait would be reported as a site verdict."""
        assert "PROBE_LOCK" not in _probe_single_body()

    def test_config_env_tunable_default_2(self):
        src = open(SERVER_PATH, encoding="utf-8").read()
        assert (
            'PROBE_MAX_CONCURRENT = int(os.environ.get("PROBE_MAX_CONCURRENT", "2"))'
            in src
        )

    def test_deadline_is_request_derived(self):
        body = _probe_single_body()
        assert "asyncio.wait_for(" in body, "dispatch must be bounded"
        assert "request.timeout + 30" in body, (
            "deadline derives from the request (client window 190s), not a global"
        )
        assert "request.timeout + 60" not in body, "/probe's ladder bound leaked in"

    def test_deadline_arm_abandons_and_504s(self):
        body = _probe_single_body()
        arm = body[body.index("except asyncio.TimeoutError:"):]
        head = arm[: arm.index("except Exception")]
        assert "_ephemeral_abandon(" in head, (
            "deadline must kill the launched browser, not orphan it"
        )
        assert "504" in head

    def test_registry_register_and_release(self):
        body = _probe_single_body()
        assert "_ephemeral_register(call_id" in body, "T33-4 deferral lands here"
        fin = body[body.rindex("finally:"):]
        assert "_ephemeral_release(" in fin
        assert "_probe_slot_release()" in fin, "slot must free on EVERY exit path"

    def test_launch_hook_on_all_browser_rungs_only(self):
        """9 browser rungs (3 playwright + 3 cloak + 3 uc_chrome aliases) track
        their roots; the HTTP-flavoured rungs stay launch-free."""
        body = _probe_single_body()
        assert body.count("launch_hook=") == 9
        for line in body.splitlines():
            if "_try_direct_http(" in line or "_try_fingerprint(" in line:
                assert "launch_hook" not in line

    def test_slot_gate_scoped_to_browser_methods_after_400(self):
        """Slot acquired only for browser-launch methods AND only after the
        unknown-method 400 — otherwise a bogus method name leaks a slot."""
        body = _probe_single_body()
        i400 = body.index("Unknown method")
        islot = body.index("_probe_slot_try_acquire()")
        assert i400 < islot
        head = body[:islot]
        assert "_is_browser_launch_method(method):" in head, (
            "HTTP-flavoured diagnostic rungs must not consume a slot"
        )

    def test_429_backpressure_on_exhaustion(self):
        body = _probe_single_body()
        assert 'error_class="probe_slots_busy"' in body
        assert body.count("_backpressure(") == 2, (
            "memory-gate 429 (C1b) + probe-slot 429 (D2)"
        )
        assert 'error_class="memory_pressure"' in body

    def test_health_reports_probe_slots(self):
        src = open(SERVER_PATH, encoding="utf-8").read()
        assert '"probe_slots_busy":' in src
        assert '"probe_slots_total":' in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
