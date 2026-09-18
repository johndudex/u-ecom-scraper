"""[wave-37 W37-OPS] Opt-in sustained-pressure Scraper-Chrome recycle — the
trigger arithmetic (docs/plans/wave37-browser-resilience-plan.md Task 10).

The policy module is stdlib-only so it loads by path (server.py imports
fastapi, absent in this image — same idiom as test_browser_resilience).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave37_proactive_recycle.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

POLICY_PATH = os.path.join(ROOT, "browser_service", "recycle_policy.py")
SERVER_PATH = os.path.join(ROOT, "browser_service", "server.py")


def _load(monkeypatch, flag: str | None = None):
    """Fresh policy module with pinned env (the module reads env at import)."""
    if flag is None:
        monkeypatch.delenv("BROWSER_PROACTIVE_RECYCLE", raising=False)
    else:
        monkeypatch.setenv("BROWSER_PROACTIVE_RECYCLE", flag)
    name = f"_recycle_policy_{flag or 'unset'}_{id(monkeypatch)}"
    spec = importlib.util.spec_from_file_location(name, POLICY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDefaults:
    def test_flag_defaults_off(self, monkeypatch):
        mod = _load(monkeypatch)
        assert mod.BROWSER_PROACTIVE_RECYCLE is False, (
            "flag-off is the ship state — prod behavior identical until the "
            "tradeoff is consciously taken"
        )

    def test_threshold_and_window_defaults(self, monkeypatch):
        mod = _load(monkeypatch)
        assert mod.BROWSER_RECYCLE_RATIO == 0.75
        assert mod.BROWSER_RECYCLE_SUSTAINED_S == 600.0

    def test_flag_truthy_forms(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "on"):
            assert _load(monkeypatch, val).BROWSER_PROACTIVE_RECYCLE is True, val
        for val in ("0", "false", "no", "off", ""):
            assert _load(monkeypatch, val).BROWSER_PROACTIVE_RECYCLE is False, val


class TestTriggerArithmetic:
    def _tracker(self, mod):
        return mod.SustainedPressureTracker(
            mod.BROWSER_RECYCLE_RATIO, mod.BROWSER_RECYCLE_SUSTAINED_S,
            mod.BROWSER_PROACTIVE_RECYCLE,
        )

    def test_flag_off_never_fires(self, monkeypatch):
        mod = _load(monkeypatch, "0")
        t = self._tracker(mod)
        # sustained far beyond the window while the flag is OFF
        for now in (0.0, 100.0, 700.0, 10_000.0):
            assert t.observe(0.99, now) is False
        assert t.high_since is None

    def test_sustained_above_fires(self, monkeypatch):
        mod = _load(monkeypatch, "1")
        t = self._tracker(mod)
        assert t.observe(0.80, 0.0) is False, "first sight arms, never fires"
        assert t.observe(0.85, 300.0) is False, "armed but window not elapsed"
        assert t.observe(0.82, 600.0) is True, "sustained 600s at >= 0.75 fires"

    def test_below_threshold_resets_the_clock(self, monkeypatch):
        mod = _load(monkeypatch, "1")
        t = self._tracker(mod)
        assert t.observe(0.80, 0.0) is False          # arm
        assert t.observe(0.50, 500.0) is False        # dip — clock resets
        assert t.high_since is None
        assert t.observe(0.90, 501.0) is False        # re-arm at 501
        assert t.observe(0.88, 501 + 599.0) is False, (
            "dwell must restart from the re-arm, not the original sighting"
        )
        assert t.observe(0.76, 501 + 600.0) is True

    def test_none_ratio_never_fires(self, monkeypatch):
        mod = _load(monkeypatch, "1")
        t = self._tracker(mod)
        assert t.observe(0.80, 0.0) is False          # arm
        assert t.observe(None, 10_000.0) is False, (
            "an unknowable ratio is not evidence of pressure — reset"
        )
        assert t.high_since is None

    def test_marginal_ratio_is_below_threshold(self, monkeypatch):
        mod = _load(monkeypatch, "1")
        t = self._tracker(mod)
        # exactly-at-threshold arms (>=); just-under never does
        assert t.observe(0.75, 0.0) is False
        assert t.observe(0.7499, 60.0) is False
        assert t.high_since is None


class TestServerWiring:
    """Source-level pins: the tracker rides the EXISTING idle-recycle guard
    chain — scrape protection (the between-navigations guarantee) MUST
    precede the observe call, and only the scraper Chrome is stopped."""

    def _fn_src(self) -> str:
        src = open(SERVER_PATH, encoding="utf-8").read()
        m = re.search(
            r"^def _maybe_recycle_scraper_chrome\(.*?(?=^def |^class )",
            src, re.M | re.S,
        )
        assert m, "_maybe_recycle_scraper_chrome not found in server.py"
        return m.group(0)

    def test_observe_rides_behind_the_scrape_guard(self):
        src = self._fn_src()
        i_guard = src.find("_scrape_protection_active()")
        i_observe = src.find("_PRESSURE_TRACKER.observe(")
        i_stop = src.find("stop_scraper_chrome")
        assert i_guard != -1, "the between-navigations guard must exist"
        assert i_observe != -1, "the pressure tracker must gate the recycle"
        assert i_stop != -1
        assert i_guard < i_observe < i_stop, (
            "pressure recycle must only fire where the idle recycle may: no "
            "in-flight /scrape run (the guard), and stop only the scraper "
            "Chrome (never MCP)"
        )

    def test_no_mcp_stop_in_the_recycle_path(self):
        src = self._fn_src()
        assert "stop_mcp" not in src and "restart_chrome(\"mcp\"" not in src, (
            "analyzer/tester sessions ride the MCP Chrome — it must never be "
            "recycled by this path"
        )
