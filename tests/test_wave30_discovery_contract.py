"""[wave-30 W30-3] Discovery-callable entry contract (fetch_page arity).

Prod proof (job 570, revolve.com): the writer OBEYED the wave-28 brief — it
imported ``create_fetch_page`` — but wrapped it thread-locally as
``def fetch_page(url, **kwargs)`` (exactly what the seed's concurrency bullet
ordered). ``**kwargs`` cannot absorb ``discover_listing_urls``' positional
``fetch_page(paginated_url, min_tier)`` call (src/listing_discovery.py:315),
so the wrapper crashed with TypeError the first time Phase 1 ever ran —
which, for a url_list job, is at run_execution (the tester skips Phase 1).
The crash surfaced as a 0-product execution after a PASSing test cycle.

Contract:
1. FATAL arity check at the ``discover_listing_urls`` entry:
   ``inspect.signature(fetch_page).bind(url, 0)`` must succeed — a wrapper
   that cannot take ``(url, min_tier)`` positionally raises
   ``DiscoveryContractError`` (ValueError subclass) with an actionable
   message (pass the closure DIRECTLY; wrappers must forward positionally
   AND carry the closure's ladder attributes).
2. WARNING-only attribute check: missing ``min_tier_floor``/``tiers_total``
   never blocks discovery (9 probe-cap test stubs lack them) but is recorded
   as ``discovery_meta["ladder_aware"] = False`` plus a loud log line.
3. The tier-floor write-back is setattr-guarded: a callable that carries
   ``tiers_total`` but rejects attribute writes (slots) escalates and
   completes, losing only the floor persistence.
4. ``discover_listing_urls_with_retry`` asserts too (fail-fast, before any
   backoff sleep).
5. The REAL ``create_fetch_page`` closure passes clean (no regression on the
   documented shape).
"""
from __future__ import annotations

import importlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

bs4 = pytest.importorskip("bs4")
ld = importlib.import_module("src.listing_discovery")  # noqa: E402

LISTING = "https://x.test/list"
EMPTY_HTML = "<html><body></body></html>"
LINKS_HTML = '<html><body><a href="/p/1">x</a></body></html>'
ANY = re.compile(".")


def _anchors(soup):
    return [a["href"] for a in soup.find_all("a")]


def _soup(html):
    return bs4.BeautifulSoup(html, "html.parser")


def _run(fetch_page, **cfg):
    cfg.setdefault("url_filter", ANY)
    cfg.setdefault("max_pages", 5)
    return ld.discover_listing_urls(fetch_page, [LISTING], _anchors, **cfg)


class Scripted:
    """Contract-carrying closure stub: (url, min_tier) → page by tier."""

    min_tier_floor = 0
    tiers_total = 3

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, min_tier=0):
        self.calls.append((url, min_tier))
        return _soup(self.pages[min(min_tier, len(self.pages) - 1)]), 200


# ═══════════════════════════════════════════════════════════════════════════
# 1 — the fatal arity check
# ═══════════════════════════════════════════════════════════════════════════


class TestArityCheck:
    def test_kwargs_wrapper_is_rejected(self):
        """Job 570's exact shape: thread-local ``**kwargs`` wrapper."""
        def fetch_page(url, **kwargs):
            return _soup(EMPTY_HTML), 200

        with pytest.raises(ld.DiscoveryContractError) as ei:
            _run(fetch_page)
        msg = str(ei.value)
        assert isinstance(ei.value, ValueError)
        assert "min_tier" in msg, "message must name the positional contract"
        assert "directly" in msg.lower(), "message must say pass the closure DIRECTLY"

    def test_zero_arg_callable_is_rejected(self):
        with pytest.raises(ld.DiscoveryContractError):
            _run(lambda: None)

    def test_error_is_a_valueerror_not_a_typeerror(self):
        """Raising TypeError verbatim would masquerade as an unrelated bug
        deeper in the stack; the contract error must be its own type."""
        def fetch_page(url, **kwargs):
            return None

        with pytest.raises(ld.DiscoveryContractError):
            try:
                _run(fetch_page)
            except TypeError as exc:
                raise AssertionError(f"raw TypeError leaked: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
# 2 — the warning-only attribute check
# ═══════════════════════════════════════════════════════════════════════════


class TestAttributeCheck:
    def test_bare_closure_discovery_proceeds_with_ladder_aware_false(self, caplog):
        """No ladder attributes → WARNING + meta flag, never a crash (the
        probe-cap stubs and simple HTTP drafts must keep running)."""
        def bare(url, min_tier=0):
            return _soup(EMPTY_HTML), 200

        urls, meta = _run(bare)
        assert urls == []
        assert meta["ladder_aware"] is False
        assert any("min_tier_floor" in r.message or "min_tier_floor" in str(r.msg)
                   for r in caplog.records), "missing ladder attrs must log loudly"

    def test_full_contract_closure_is_ladder_aware(self):
        stub = Scripted([EMPTY_HTML])
        urls, meta = _run(stub)
        assert meta["ladder_aware"] is True
        assert ld._assert_fetch_page_contract(stub)["missing_ladder_attrs"] == []

    def test_real_create_fetch_page_closure_passes_clean(self):
        """The documented shape (src/http_fetch.create_fetch_page) must sail
        through: right arity AND the ladder attributes. Never invoked here —
        construction only, zero network."""
        from src.http_fetch import create_fetch_page

        fetch_page = create_fetch_page(delay_s=0)
        contract = ld._assert_fetch_page_contract(fetch_page)
        assert contract["ladder_aware"] is True
        assert hasattr(fetch_page, "min_tier_floor")
        assert hasattr(fetch_page, "tiers_total")


# ═══════════════════════════════════════════════════════════════════════════
# 3 — the tier-floor write-back is setattr-guarded
# ═══════════════════════════════════════════════════════════════════════════


class TestFloorWriteBack:
    def test_escalated_tier_is_stamped_on_the_closure(self):
        """Page-1 zero-links escalates to tier 1; the tier that finally yields
        links becomes the closure's floor (job-62 contract)."""
        stub = Scripted([EMPTY_HTML, LINKS_HTML])
        urls, meta = _run(stub)
        assert urls, "tier-1 page must yield links"
        assert stub.min_tier_floor == 1, "winning tier not stamped as floor"
        assert [t for _, t in stub.calls] == [0, 1, 1], (
            "expected: tier-0 empty → tier-1 links → tier-1 page-2 dedupe"
        )

    def test_setattr_rejecting_closure_completes_without_crash(self):
        class Rigid:
            __slots__ = ("tiers_total", "pages", "calls")

            def __init__(self, pages):
                self.tiers_total = 3
                self.pages = pages
                self.calls = []

            def __call__(self, url, min_tier=0):
                self.calls.append((url, min_tier))
                return _soup(self.pages[min(min_tier, len(self.pages) - 1)]), 200

        rigid = Rigid([EMPTY_HTML, LINKS_HTML])
        urls, meta = _run(rigid)
        assert urls, "discovery must complete despite the rejected write-back"
        assert meta["ladder_aware"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 4 — the retry wrapper asserts too
# ═══════════════════════════════════════════════════════════════════════════


class TestWithRetryAsserts:
    def test_wrapper_rejected_before_any_backoff_sleep(self, monkeypatch):
        def fetch_page(url, **kwargs):
            return None

        def no_sleep(_s):
            raise AssertionError("contract assert must fire before the backoff")

        monkeypatch.setattr(ld.time, "sleep", no_sleep)
        with pytest.raises(ld.DiscoveryContractError):
            ld.discover_listing_urls_with_retry(
                fetch_page, [LISTING], _anchors, url_filter=ANY, retry_delay_s=45,
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
