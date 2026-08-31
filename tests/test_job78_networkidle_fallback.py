"""[job-78 superdrug] A networkidle timeout must not fail the item it loaded.

Prod evidence (2026-08-31): job 78's re-drive regenerated a draft whose phase-1
discovery WORKED (62 product URLs from the listing page), but every phase-2 PDP
goto died on ``networkidle wait exceeded 30s`` — under Akamai cloak the page
never goes quiet, so the playwright template's bare
``page.goto(url, wait_until="networkidle")`` raised and the item was counted
failed. Discovery 62 → extraction 0 → FAIL → "cascade exhausted". On a cloak
site every PDP burns the full PAGE_LOAD_TIMEOUT and yields nothing.

A goto TimeoutError means the document LOADED (networkidle is the last load
stage) — the DOM is rendered and extractable. Only a hard navigation failure
(DNS/refused/aborted) should propagate to the per-item error handling.

Fixes pinned here:
- ``scrape_product`` catches the goto timeout, logs, and extracts from the
  rendered DOM (the 2s settle + substantive-field validity check upstream
  keep partially-rendered pages from shipping empty products).
- The main() warm-up goto gets the same treatment — a never-idle homepage
  must not kill the run before extraction starts.
- The import actually binds ``PlaywrightTimeoutError`` from playwright's API
  (the behavior tests below exec the function with a stand-in class, so only
  this pin proves the real exception is caught in production).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job78_networkidle_fallback.py -q
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

TEMPLATE = os.path.join(ROOT, "templates", "playwright_scraper.py")

# Stand-in for playwright.sync_api.TimeoutError — the behavior tests bind this
# into the exec'd function's namespace; the import pin above the tests proves
# the template binds the REAL one.
class PlaywrightTimeoutError(Exception):
    pass


class FakePage:
    def __init__(self, goto_raiser=None, data=None):
        self.goto_raiser = goto_raiser
        self.data = data or {}
        self.settles = []

    def goto(self, url, wait_until=None, timeout=None):
        if self.goto_raiser is not None:
            raise self.goto_raiser
        return type("R", (), {"status": 200})()

    def wait_for_timeout(self, ms):
        self.settles.append(ms)

    def evaluate(self, js, *args):
        return dict(self.data)


def _load_scrape_product():
    """Exec scrape_product from the template with a stubbed namespace."""
    src = open(TEMPLATE).read()
    m = re.search(r"^def scrape_product\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, "scrape_product not found"
    import logging

    ns = {
        "logging": logging,
        "logger": logging.getLogger("t.j78"),
        "datetime": datetime,
        "timezone": timezone,
        "PAGE_LOAD_TIMEOUT": 30000,
        "EXTRACT_PRODUCT_JS": "()",
        "PlaywrightTimeoutError": PlaywrightTimeoutError,
    }
    exec(m.group(0), ns)
    return ns["scrape_product"]


# ─── the timeout is non-fatal: extraction proceeds ───────────────────────────


class TestNetworkidleTimeoutNonFatal:
    def test_timeout_still_extracts_rendered_dom(self):
        scrape = _load_scrape_product()
        page = FakePage(
            goto_raiser=PlaywrightTimeoutError("Timeout 30000ms exceeded."),
            data={"title": "SHYNE Durag", "price": "£8"},
        )
        product = scrape(page, "https://x.com/p/1", "https://x.com/p/1", 1)
        assert product["title"] == "SHYNE Durag"
        assert product["price"] == "£8"
        assert page.settles, "the post-goto settle must still run"

    def test_hard_navigation_failure_still_propagates(self):
        scrape = _load_scrape_product()
        page = FakePage(goto_raiser=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
        with __import__("pytest").raises(RuntimeError):
            scrape(page, "https://x.com/p/1", "https://x.com/p/1", 1)

    def test_no_timeout_is_unchanged(self):
        scrape = _load_scrape_product()
        page = FakePage(data={"title": "ok"})
        assert scrape(page, "https://x.com/p/1", "https://x.com/p/1", 1)["title"] == "ok"


# ─── source pins: real import bound, warm-up guarded too ─────────────────────


class TestSourcePins:
    def test_template_imports_playwright_timeout_error(self):
        src = open(TEMPLATE).read()
        assert re.search(
            r"from playwright\.sync_api import sync_playwright,\s*"
            r"TimeoutError as PlaywrightTimeoutError",
            src,
        ), (
            "the template must bind playwright's real TimeoutError — the exec'd "
            "behavior tests use a stand-in class and cannot prove this"
        )

    def test_warmup_goto_is_guarded(self):
        src = open(TEMPLATE).read()
        m = re.search(
            r'Warming up session.*?\)\s*\n\s*try:\s*\n\s*'
            r'page\.goto\(SITE_URL.*?except PlaywrightTimeoutError:',
            src, re.S,
        )
        assert m, (
            "the main() warm-up goto must catch the timeout too — a never-idle "
            "homepage must not kill the run before extraction starts"
        )

    def test_scrape_product_goto_is_guarded(self):
        src = open(TEMPLATE).read()
        m = re.search(
            r"def scrape_product\(.*?try:\s*\n\s*"
            r'page\.goto\(url, wait_until="networkidle".*?except PlaywrightTimeoutError:',
            src, re.S,
        )
        assert m, "scrape_product's networkidle goto must catch the timeout"


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
