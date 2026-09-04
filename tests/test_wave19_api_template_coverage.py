"""[wave-19 T0.5 + T1.3] The api template must report discovery and ride the
shared ladder.

Job 324 RCA: the api_scraper-family draft (a) ran an INLINE per-tier fetch
ladder with no challenge detection (a 200 challenge body raised past the
``except requests.RequestException``), and (b) emitted NO
``metadata.discovery_coverage`` — which gated OFF the already-shipped
execution-time strategy+tier recycle (graph.py reads that block) and left the
failure's ``stop_reason=`` empty. The template contract mirrors
``requests_scraper.py``: always emit discovery_coverage when Phase 1 ran.

T1.3: Phase-1 ``fetch_api`` collapses to ``_get_fetch_json()`` (shared
session, detect_soft_block, real escalation — "the LLM cannot strip what it
never sees"), and ``http_navigation_scraper.py:_http_get`` propagates the
SoftBlock signal instead of collapsing it to ("", 0), so the form-search
loop can report a challenge wall as ``empty_first_page`` (the honest stop
reason) instead of ``navigate_error``.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

API_TEMPLATE = os.path.join(ROOT, "templates", "api_scraper.py")
NAV_TEMPLATE = os.path.join(ROOT, "templates", "http_navigation_scraper.py")


def _src(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _grab(path: str, name: str) -> str:
    src = _src(path)
    m = re.search(
        rf"^def {name}\(.*?"
        rf"(?=^(?:async )?def |^class |^@|^if __name__|^[A-Za-z_][A-Za-z0-9_]* = )",
        src,
        re.M | re.S,
    )
    assert m, f"{name} not found in {path}"
    return m.group(0)


class _FakeLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Block:
    """Stand-in for src.http_fetch.SoftBlock (falsy on purpose)."""

    def __init__(self, reason="challenge_marker", body_bytes=500):
        self.reason = reason
        self.body_bytes = body_bytes

    def __bool__(self):
        return False


# ─── T1.3a: api Phase-1 fetch rides the shared module ────────────────────────


class TestApiPhase1SharedLadder:
    def test_fetch_api_uses_get_fetch_json(self):
        src = _grab(API_TEMPLATE, "fetch_api")
        assert "_get_fetch_json()" in src, (
            "Phase-1 fetch_api must ride the shared src.http_fetch ladder"
        )
        # the strippable inline ladder is gone
        assert "get_escalation_tier()" not in src
        assert 'for tier in ["none"' not in src

    def test_fetch_api_passthrough_softblock_and_none(self):
        """A SoftBlock signal / None travels out of fetch_api untouched."""
        ns = {
            "logger": _FakeLogger(),
            "API_BASE_URL": "https://x/api",
            "requests": None,
            "_get_fetch_json": lambda: lambda url, params=None, min_tier=0: (
                _Block() if "blocked" in url else ({"products": []}, 200)
            ),
        }
        exec(compile(_grab(API_TEMPLATE, "fetch_api"), "<fetch_api>", "exec"), ns)
        blocked = ns["fetch_api"]("/products/blocked")
        assert isinstance(blocked, _Block), (
            "the challenge signal must REACH the discovery loop, not raise"
        )
        ok = ns["fetch_api"]("/products")
        assert ok == ({"products": []}, 200)
        exhausted = ns["fetch_api"]("/products/exhausted") if False else None
        # None passthrough: a falsy ladder result is returned as-is
        ns2 = dict(ns, _get_fetch_json=lambda: lambda url, params=None, min_tier=0: None)
        exec(compile(_grab(API_TEMPLATE, "fetch_api"), "<fetch_api2>", "exec"), ns2)
        assert ns2["fetch_api"]("/products") is None


class TestApiDiscoveryMeta:
    def _run_discovery(self, pages):
        """Exec fetch_all_products_via_api against a scripted fetch_api.

        ``pages``: list of per-page fetch results (tuple / _Block / None).
        """
        seq = list(pages)

        def fake_fetch(endpoint, params=None):
            return seq.pop(0) if seq else None

        ns = {
            "logger": _FakeLogger(),
            "fetch_api": fake_fetch,
            "PAGINATION_TYPE": "offset",
            "PAGE_SIZE": 10,
            "API_PRODUCTS_ENDPOINT": "/products.json",
            "SoftBlock": _Block,
        }
        exec(
            compile(
                _grab(API_TEMPLATE, "fetch_all_products_via_api"),
                "<discovery>",
                "exec",
            ),
            ns,
        )
        urls, products = ns["fetch_all_products_via_api"]()
        return urls, products, ns["_DISCOVERY_META"]

    def test_challenge_wall_reports_empty_first_page(self):
        """A 200 challenge on page 1 = the job-58 shape: zero URLs from an
        anti-bot wall, NOT a genuine catalog end."""
        _, _, meta = self._run_discovery([_Block()])
        assert meta["stop_reason"] == "empty_first_page"
        assert meta["soft_block_escalations"] == 1
        assert meta["ran_phase1"] is True

    def test_all_tiers_failed_reports_navigate_error(self):
        _, _, meta = self._run_discovery([None])
        assert meta["stop_reason"] == "navigate_error"

    def test_short_page_is_exhaustion(self):
        urls, _, meta = self._run_discovery(
            [({"products": [{"url": f"https://x/p/{i}"} for i in range(3)]}, 200)]
        )
        assert meta["stop_reason"] == "no_next_link"
        assert len(urls) == 3
        assert meta["discovered_urls"] == 3


class TestApiMainEmitsCoverage:
    def test_main_metadata_carries_discovery_coverage(self):
        main_src = _grab(API_TEMPLATE, "main")
        assert "discovery_coverage" in main_src, (
            "metadata.discovery_coverage is the block the execution recycle "
            "and the tester's coverage gate read — the api template must "
            "emit it like requests_scraper does"
        )
        assert '"discovered_urls"' in main_src or "'discovered_urls'" in main_src
        # ran_phase1 + skipped_reason live in _DISCOVERY_META, which main
        # spreads into the emitted block.
        assert "**_DISCOVERY_META" in main_src
        whole = _src(API_TEMPLATE)
        for key in ("ran_phase1", "skipped_reason", "soft_block_escalations"):
            assert f'"{key}"' in whole, key

    def test_discovery_urls_captured_before_sample_cut(self):
        main_src = _grab(API_TEMPLATE, "main")
        # the raw count must be captured before --sample/--limit truncate
        assert re.search(r"discovered_urls_raw\s*=", main_src), (
            "capture the pre-cut discovery count for the coverage block"
        )


# ─── T1.3b: _http_get propagates the SoftBlock signal ────────────────────────


class TestNavHttpGetPropagatesSoftBlock:
    def _ns(self, fetch_result):
        ns = {
            "logger": _FakeLogger(),
            "os": os,
            "httpx": None,
            "SoftBlock": _Block,
            "_get_fetch_text": lambda: (
                lambda url, params=None, min_tier=0: fetch_result
            ),
        }
        exec(
            compile(_grab(NAV_TEMPLATE, "_is_soft_block"), "<_is_soft_block>", "exec"),
            ns,
        )
        return ns

    def test_softblock_travels_out_of_http_get(self):
        ns = self._ns(_Block())
        exec(compile(_grab(NAV_TEMPLATE, "_http_get"), "<_http_get>", "exec"), ns)
        result = ns["_http_get"]("https://x.com/collections/sale")
        assert isinstance(result, _Block), (
            "_http_get must propagate the challenge signal — collapsing it "
            "to ('', 0) makes a soft wall indistinguishable from a transport "
            "failure"
        )

    def test_tuple_passthrough_and_hard_fail_unchanged(self):
        ns = self._ns(("<html>ok</html>", 200))
        exec(compile(_grab(NAV_TEMPLATE, "_http_get"), "<_http_get>", "exec"), ns)
        assert ns["_http_get"]("https://x.com/") == ("<html>ok</html>", 200)
        ns2 = self._ns(None)
        exec(compile(_grab(NAV_TEMPLATE, "_http_get"), "<_http_get>", "exec"), ns2)
        assert ns2["_http_get"]("https://x.com/") == ("", 0)

    def test_form_search_detects_but_does_not_reclass_challenge(self):
        """The form-search loop must DETECT the 200 challenge wall (soft-block
        log line — honest evidence) but must NOT reclass it per-path:
        per-path empty_first_page + _merge_stop_reason would fail a run whose
        primary path found items (T2.1 — the reclass lives ONCE in main() on
        the AGGREGATE list). navigate_error is the table-honest per-path label
        ("gave up due to 502/503/block", severity 5)."""
        src = _grab(NAV_TEMPLATE, "_discover_urls_via_form_search")
        assert "isinstance(form_html, SoftBlock)" in src or (
            "_is_soft_block(form_html)" in src
        )
        assert 'return [], "empty_first_page"' not in src, (
            "per-path reclass to the job-58 label is the T2.1 poison — "
            "the aggregate reclass in main() owns that label"
        )
        assert '"navigate_error"' in src
