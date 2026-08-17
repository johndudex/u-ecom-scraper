"""F6 (per-template env gates) + F7 (listing-URL chain) + F17 (domain guard).

Prod incidents:
- 285/319: discovery flags stripped by the CLI-contract guard + the env gate
  existed only in playwright_scraper → HTTP/API drafts took the seed-file
  branch; discovery/pagination never ran (1 and 5 items).
- 331: nav followed a footer locale link → all artifacts .com.au under a .us
  job → 80/80 wrong-domain rows.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─────────────────────────────────────────────────────────────────────────────
# F6: static + behavioral checks on the templates
# ─────────────────────────────────────────────────────────────────────────────

def _tpl(name: str) -> str:
    with open(os.path.join(ROOT, "templates", name)) as fh:
        return fh.read()


class TestF6TemplateGates:
    def test_requests_scraper_gate_forces_branch(self):
        src = _tpl("requests_scraper.py")
        assert 'os.environ.get("SCRAPER_LISTING_URL"' in src
        assert "PRODUCT_LISTING_URLS[:] = [_env_listing]" in src  # in-place
        assert "args.discover_only or _env_listing" in src  # branch forced

    def test_api_scraper_declares_fresh_discovery(self):
        src = _tpl("api_scraper.py")
        # declared flag passes the CLI-contract guard (never stripped)
        assert '"--fresh-discovery"' in src
        # and the gate bypasses the seed file when set
        assert "_force_fresh" in src

    def test_http_navigation_feeds_args_below_form(self):
        src = _tpl("http_navigation_scraper.py")
        assert 'args.listing_url = _env_listing' in src
        assert "args.fresh_discovery = True" in src
        # gate inserted BEFORE the checkpoint read but must NOT override FORM
        assert src.index("_env_listing =") < src.index("checkpoint_urls = [] if args.fresh_discovery")
        # (FORM_ACTION branch itself untouched — presence check)
        assert "if FORM_ACTION and FORM_SELECT_NAME:" in src

    def test_navigation_scraper_two_liner(self):
        src = _tpl("navigation_scraper.py")
        assert 'args.listing_url = _env_listing' in src
        assert "args.fresh_discovery = True" in src

    def test_ssr_div_list_env_first(self):
        src = _tpl("ssr_div_list_scraper.py")
        assert "args.listing_url = _env_listing" in src
        # env override happens BEFORE the fallback derivation
        assert src.index("Env gate: SCRAPER_LISTING_URL") < src.index(
            "# Determine the listing URL")

    def test_playwright_template_unchanged(self):
        # playwright already had the gate (templates/playwright_scraper.py:311-336)
        src = _tpl("playwright_scraper.py")
        assert "SCRAPER_LISTING_URL" in src


# ─────────────────────────────────────────────────────────────────────────────
# F6 behavioral: requests gate actually forces discovery (subprocess run)
# ─────────────────────────────────────────────────────────────────────────────

class TestF6RequestsBehavior:
    def test_env_gate_runs_discovery_despite_seed_file(self, tmp_path, monkeypatch):
        try:
            import bs4  # noqa: F401
        except ImportError:
            import pytest
            pytest.skip("bs4 unavailable (host) — run in container")
        """With SCRAPER_LISTING_URL set AND input_urls.json present, the
        template must RUN discovery, not load the seed file."""
        import shutil
        ws = tmp_path / "ws"
        ws.mkdir()
        # The template is a code_writer TEMPLATE with {PLACEHOLDER} config
        # (unrendered it NameErrors on import). Render the placeholder block
        # the way code_writer's fill does, minimally.
        src = _tpl("requests_scraper.py")
        src = src.replace('SITE_URL = "{SITE_URL}"', 'SITE_URL = "https://x.example"')
        src = src.replace('PRODUCT_LISTING_URL = "{PRODUCT_LISTING_URL}"',
                          'PRODUCT_LISTING_URL = "https://x.example/list"')
        src = src.replace("{DELAY_BETWEEN_REQUESTS}", "0.1")
        (ws / "s.py").write_text(src)
        # seed file EXISTS (the condition that used to suppress discovery)
        (ws / "input_urls.json").write_text('{"urls": ["https://x.example/p/1"]}')
        monkeypatch.setenv("SCRAPER_LISTING_URL", "https://x.example/list")
        import subprocess
        r = subprocess.run(
            [sys.executable, str(ws / "s.py"), "--limit", "3"],
            capture_output=True, text=True, timeout=120, cwd=str(ws),
            env={**os.environ},
        )
        out = r.stdout + r.stderr
        # the gate log proves Phase 1 ran despite the seed file
        assert "Env gate: SCRAPER_LISTING_URL set" in out, out[-500:]
        # and the seed-suppression log must NOT fire
        assert "Loading previously discovered URLs" not in out


# ─────────────────────────────────────────────────────────────────────────────
# F17: the sanitizer (graph.py) — source-extracted, no Django deps
# ─────────────────────────────────────────────────────────────────────────────

import re as _re
import types
import unittest.mock as mock

_graph_src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
_m = _re.search(r"^def _sanitize_nav_domains\(.*?(?=^def |\Z)", _graph_src, _re.S | _re.M)
assert _m, "sanitizer not found"
_ns: dict = {"logger": mock.MagicMock(), "__builtins__": __builtins__}
# traversal import inside is lazy — pre-seed the real module if importable
try:
    from experimental.nav_traversal.traversal import _registrable  # noqa: F401
except Exception:
    _trav = types.ModuleType("experimental.nav_traversal.traversal")
    _trav._registrable = lambda u: (u.split("//")[-1].split("/")[0].removeprefix("www.")
                                    .rsplit(".", 2)[-2] if "//" in u else "")
    sys.modules.setdefault("experimental.nav_traversal.traversal", _trav)
exec(compile(_m.group(0), "graph:_sanitize_nav_domains", "exec"), _ns)
_sanitize = _ns["_sanitize_nav_domains"]
_sanitize_nav_domains = _sanitize


class TestF17Sanitizer:
    def _analysis(self):
        return {
            "search": {"working_url": "https://www.prettylittlething.us/categories/womens"},
            "discovery": {"listing_url": "https://www.prettylittlething.com.au/categories/womens-clothing"},
            "item_links": {"url_examples": [
                "https://www.prettylittlething.com.au/p/a",
                "https://www.prettylittlething.us/p/b",
            ]},
        }

    def test_cross_domain_blanked(self):
        a = self._analysis()
        out = _sanitize_nav_domains(a, "https://www.prettylittlething.us/")
        # discovery.listing_url was .com.au → blanked
        assert out["discovery"]["listing_url"] == ""
        # wrong-domain example dropped, same-domain kept
        assert out["item_links"]["url_examples"] == ["https://www.prettylittlething.us/p/b"]

    def test_same_domain_untouched(self):
        a = self._analysis()
        a["discovery"]["listing_url"] = "https://www.prettylittlething.us/c/womens"
        out = _sanitize_nav_domains(a, "https://www.prettylittlething.us/")
        assert out["discovery"]["listing_url"] == "https://www.prettylittlething.us/c/womens"

    def test_non_http_values_untouched(self):
        a = {"search": {"working_url": ""}, "discovery": {"listing_url": None}}
        out = _sanitize_nav_domains(a, "https://x.us/")
        assert out["search"]["working_url"] == "" and out["discovery"]["listing_url"] is None

    def test_two_part_tld_com_au_not_confused(self):
        # job on .com.au, artifact on .com.au → same registrable, keep
        a = {"discovery": {"listing_url": "https://shop.example.com.au/c/1"}}
        out = _sanitize_nav_domains(a, "https://www.example.com.au/")
        assert out["discovery"]["listing_url"] == "https://shop.example.com.au/c/1"


# ─────────────────────────────────────────────────────────────────────────────
# F7: the chain (static — the runtime chain is inside run_execution's body)
# ─────────────────────────────────────────────────────────────────────────────

class TestF7Chain:
    def test_cli_chain_prefers_discovery_listing_with_fallback(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")).read()
        assert "discovery.listing_url first" in src or "_candidates = [" in src
        assert "_working_url" in src
        # F17 guard present in the CLI chain
        assert "F17 dropped cross-domain --listing-url" in src

    def test_m6_browser_path_uses_computed_env(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")).read()
        assert '_state_bs["_listing_url_env"] = _listing_url_env' in src
        assert '(state or {}).get("_listing_url_env")' in src
