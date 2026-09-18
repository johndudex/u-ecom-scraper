"""[wave-37 W37-NEW-D] One deterministic ladder-repair arm before the
honest-fail (prod 628/623/600: otherwise-sound drafts whose proxy ladder
wiring the writer stripped).

The repair is textual, honest, and boring: bare ``requests.<m>(...)`` call
sites gain a resolved ``proxies=`` kwarg (ProxyConfig.get_proxy_dict) so
the ACTUAL fetches go through the proxy — not a dead marker that passes
the gate while the runtime stays unproxied. Drafts that cannot be
repaired (no bare-requests call sites, unparseable, exempt strategy,
already wired) return None and keep today's honest-fail path.
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

import ast  # noqa: E402

from webapp.agents.draft_safety import (  # noqa: E402
    ladder_preservation_violation,
    repair_ladder_violation,
)

STRIPPED = '''
import requests

def scrape(url):
    r = requests.get(url, timeout=30)
    return r.text
'''

WIRED = '''
from src.http_fetch import create_fetch_page

def scrape(url):
    fetch_page = create_fetch_page()
    soup, status = fetch_page(url)
    return soup.get_text() if soup else ""
'''

SESSION_ONLY = '''
import requests

SESSION = requests.Session()

def scrape(url):
    r = SESSION.get(url, timeout=30)
    return r.text
'''

PROXIES_KW = '''
import requests

def scrape(url):
    r = requests.get(url, timeout=30, proxies=None)
    return r.text
'''


def _gate(src: str) -> str | None:
    """The real gate on a temp file, nav-mode, non-exempt strategy."""
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(src)
        return ladder_preservation_violation(path, "list_page", "http_navigation")
    finally:
        os.unlink(path)


def test_stripped_draft_violates_then_repairs():
    assert _gate(STRIPPED), "fixture must violate the gate"
    repaired = repair_ladder_violation(STRIPPED, "http_navigation")
    assert repaired is not None
    ast.parse(repaired)  # repair must produce parseable source
    assert "proxies=" in repaired
    assert not _gate(repaired), "repaired draft must satisfy the gate"


def test_wired_draft_repairs_to_none():
    assert repair_ladder_violation(WIRED, "http_navigation") is None


def test_exempt_strategy_repairs_to_none():
    assert repair_ladder_violation(STRIPPED, "playwright") is None


def test_unparseable_draft_unrepairable():
    assert repair_ladder_violation("def broken(:", "http_navigation") is None


def test_session_only_draft_unrepairable():
    # rewriting non-requests call sites (d.get(...)!) is unsafe — no targets
    # means no repair, and the honest-fail path stays.
    assert repair_ladder_violation(SESSION_ONLY, "http_navigation") is None


def test_proxies_kwarg_draft_repairs_to_none():
    # L2-satisfied already → the gate is clean → nothing to repair
    assert repair_ladder_violation(PROXIES_KW, "http_navigation") is None


def test_repaired_runtime_actually_proxies():
    # the injected kwarg must call the real ProxyConfig getter — not a
    # literal or a dead variable.
    repaired = repair_ladder_violation(STRIPPED, "http_navigation")
    assert repaired is not None
    assert "ProxyConfig" in repaired and "get_proxy_dict" in repaired
