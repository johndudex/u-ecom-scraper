"""[wave-14 job-133] Seed contract + run-scope contract — PR-2 tests.

Job 133 (athleta.gap.com) died of a poisoned seed: the intake filter compared
REGISTRABLE domains, so ``www.gap.com`` / ``bananarepublic.gap.com`` item
links passed for an ``athleta.gap.com`` job, and every downstream phase
extracted another brand's pages. The filter is now FULL-HOST equality
(www-stripped), shared by every surface that writes / redirects / promotes a
seed file (``src/seed_urls.py``), with a last-line hygiene belt inside
``run_scraper``.

The same job's writer self-test showed the run-scope problem: a 5-URL
``--sample --input`` verification was floored to the 600s DISCOVERY budget
and then refused by the job-81 honesty guard (690s needed > window).
Verification-scope runs now get a ~180s floor and no discovery injection,
and whichever scope a run got is REPORTED in the tool result.

Run from repo root:
    PYTHONPATH=/app:/app/webapp python -m pytest tests/test_wave14_seed_contract.py -v
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from src.seed_urls import (  # noqa: E402
    filter_seed_payload,
    filter_seed_urls,
    normalize_host,
    seed_report,
)


# ─── the shared full-host filter ─────────────────────────────────────────────


class TestFullHostRule:
    JOB = "https://athleta.gap.com/"

    def test_job133_poison_is_dropped(self):
        """THE job-133 case: gap.com-family links must NOT pass for an
        athleta.gap.com job. The old registrable rule said gap.com == gap.com
        and admitted them."""
        urls = [
            "https://athleta.gap.com/product/p1",
            "https://www.gap.com/product/p2",
            "https://bananarepublic.gap.com/product/p3",
        ]
        kept, dropped = seed_report(urls, self.JOB)
        assert kept == ["https://athleta.gap.com/product/p1"]
        assert dropped.get("off-host") == 2

    def test_www_prefix_is_canonicalized(self):
        """www vs bare host is the SAME shop front — never a drop."""
        urls = [
            "https://www.shop-com-au.com/p/1",
            "https://shop-com-au.com/p/2",
        ]
        kept, dropped = seed_report(urls, "https://www.shop-com-au.com/")
        assert kept == urls
        assert dropped == {}

    def test_two_part_tld_needs_no_table(self):
        """Full-host equality is TLD-agnostic — priceline.com.au vs
        other.com.au differs on the FIRST label already."""
        kept, _ = seed_report(
            ["https://www.priceline.com.au/p/1", "https://other.com.au/p/2"],
            "https://www.priceline.com.au/",
        )
        assert kept == ["https://www.priceline.com.au/p/1"]

    def test_query_only_url_is_an_item_url(self):
        kept, _ = seed_report(["https://shop.com/search?q=boots"], "https://shop.com/x")
        assert kept == ["https://shop.com/search?q=boots"]

    def test_pathless_and_queryless_dropped(self):
        kept, dropped = seed_report(
            ["https://shop.com", "https://shop.com/"], self.JOB
        )
        assert kept == []
        assert dropped.get("no-path") == 2

    def test_non_http_and_garbage_dropped(self):
        kept, dropped = seed_report(
            ["ftp://shop.com/p/1", "javascript:void(0)", "", None, "   "],
            "https://shop.com/",
        )
        assert kept == []
        assert dropped.get("not-http") == 2
        assert dropped.get("blank") == 3

    def test_duplicates_first_wins(self):
        urls = [
            "https://shop.com/p/1",
            "https://shop.com/p/1",
            "https://shop.com/p/1?x=1",
        ]
        kept, dropped = seed_report(urls, "https://shop.com/")
        assert kept == [urls[0], urls[2]]
        assert dropped.get("duplicate") == 1

    def test_blank_job_url_disables_host_rule(self):
        urls = ["https://a.com/p/1", "https://b.com/p/2"]
        assert filter_seed_urls(urls, "") == urls

    def test_normalize_host(self):
        assert normalize_host("WWW.Example.COM.") in ("example.com", "example.com.")
        assert normalize_host("www.shop.com") == "shop.com"
        assert normalize_host("shop.com") == "shop.com"
        # subdomain www is NOT stripped (only a leading www label)
        assert normalize_host("wwws.shop.com") == "wwws.shop.com"


class TestFilterSeedPayload:
    def test_dict_shape_preserved_and_same_object_when_clean(self):
        payload = {"urls": ["https://shop.com/p/1"], "note": "x"}
        out, dropped = filter_seed_payload(payload, "https://shop.com/j")
        assert out is payload, "no drops → no rewrite (no mtime churn)"
        assert dropped == {}

    def test_dict_shape_filtered(self):
        payload = {
            "urls": [
                "https://shop.com/p/1",
                "https://evil.com/p/2",
            ]
        }
        out, dropped = filter_seed_payload(payload, "https://shop.com/")
        assert out["urls"] == ["https://shop.com/p/1"]
        assert out is not payload
        assert dropped.get("off-host") == 1

    def test_bare_array_shape_job88(self):
        """[job-88] legacy drafts seeded a bare JSON array — filtered in the
        same shape."""
        out, dropped = filter_seed_payload(
            ["https://evil.com/p/1", "https://shop.com/p/2"], "https://shop.com/"
        )
        assert out == ["https://shop.com/p/2"]
        assert dropped.get("off-host") == 1

    def test_non_list_payload_is_tolerated(self):
        out, dropped = filter_seed_payload({"urls": None}, "https://shop.com/")
        assert dropped == {}


# ─── surface: setup_workspace delegates to the shared rule ───────────────────


class TestSetupWorkspaceSurface:
    def test_filter_seed_urls_delegates_full_host_rule(self):
        from webapp.agents.nodes.setup_workspace import _filter_seed_urls

        urls = [
            "https://athleta.gap.com/product/p1",
            "https://www.gap.com/product/p2",
        ]
        assert _filter_seed_urls(urls, "https://athleta.gap.com/") == [urls[0]]


# ─── surface: FM sync (models._sync_input_urls_file) ─────────────────────────


class TestFmSyncSurface:
    def _sync(self, monkeypatch, urls, site_url):
        import src.artifacts as artifacts
        import scraper.models as models

        written = {}

        monkeypatch.setattr(artifacts, "exists", lambda key: False)
        monkeypatch.setattr(
            artifacts, "write_json",
            lambda key, payload: written.setdefault("payload", payload),
        )
        instance = SimpleNamespace(
            input_urls=list(urls), slug="athleta-gap-com", url=site_url,
        )
        models._sync_input_urls_file(instance)
        return written.get("payload")

    def test_poison_never_reaches_the_production_file(self, monkeypatch):
        payload = self._sync(
            monkeypatch,
            [
                "https://athleta.gap.com/product/p1",
                "https://www.gap.com/product/p2",
            ],
            "https://athleta.gap.com/",
        )
        assert payload["urls"] == ["https://athleta.gap.com/product/p1"]

    def test_clean_list_passes_through(self, monkeypatch):
        urls = ["https://athleta.gap.com/p/1", "https://athleta.gap.com/p/2"]
        payload = self._sync(monkeypatch, urls, "https://athleta.gap.com/")
        assert payload["urls"] == urls


# ─── surface: run_scraper seed-hygiene belt + run scope ──────────────────────


class _FakeScrapeResult:
    def __init__(self):
        self.ok = True
        self.error = ""
        self.error_class = ""
        self.transient = False
        self.data = {"stdout": "ok", "stderr": "", "duration": 1}


def _setup_ws(tmp_path, seed_urls):
    ws = tmp_path / "workspace" / "t"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "scraper_draft.py").write_text("# draft\n")
    import json

    (ws / "input_urls.json").write_text(json.dumps({"urls": seed_urls}))
    return ws


@pytest.fixture()
def _nav_state(monkeypatch):
    """Tool state asserting a navigation listing URL (injection candidate)."""
    from agents.tools import context as ctx

    state = {
        "job_id": 0,
        "url": "https://athleta.gap.com/",
        "site_slug": "t",
        "input_mode": "navigation",
        "navigation_analysis": {
            "discovery": {"listing_url": "https://athleta.gap.com/womens-dresses"}
        },
    }
    monkeypatch.setattr(ctx, "get_state", lambda: state)
    yield state
    ctx.clear_tool_context()


class TestRunScopeContract:
    def _tool(self, monkeypatch, needs_browser=True):
        from agents.tools import shell_tools

        tools = {t.name: t for t in shell_tools.get_shell_tools()}
        monkeypatch.setattr(
            shell_tools, "_scraper_needs_browser", lambda p: needs_browser
        )
        return tools["run_scraper"].func

    def test_verification_run_gets_the_cheap_floor(self, monkeypatch, tmp_path, _nav_state):
        """A tight LLM timeout (100s) floors UP to 180s — not 600s — so
        180s + 90s margin fits a 300s window where the old discovery floor
        refused (the job-133 writer self-test class)."""
        from agents.tools.context import set_tool_deadline

        ws = _setup_ws(tmp_path, ["https://athleta.gap.com/p/1"])
        run_scraper = self._tool(monkeypatch)
        set_tool_deadline(time.time() + 300)
        result = run_scraper(
            scraper_path=str(ws / "scraper_draft.py"),
            cli_args="--sample --input input_urls.json",
            timeout=100,
        )
        assert not result.startswith("SKIPPED:"), result

    def test_full_scope_run_still_needs_the_discovery_window(
        self, monkeypatch, tmp_path, _nav_state
    ):
        from agents.tools.context import set_tool_deadline

        ws = _setup_ws(tmp_path, ["https://athleta.gap.com/p/1"])
        run_scraper = self._tool(monkeypatch)
        set_tool_deadline(time.time() + 300)
        result = run_scraper(
            scraper_path=str(ws / "scraper_draft.py"), cli_args="", timeout=300,
        )
        assert result.startswith("SKIPPED:")
        # …and the refusal OFFERS the cheap escape hatch
        assert "verification-scope" in result
        assert "--input input_urls.json --sample" in result

    def _capture_dispatch(self, monkeypatch):
        from agents.tools import shell_tools

        captured = {}

        def fake_post(url, payload, timeout=None, **kw):
            captured["payload"] = payload
            return _FakeScrapeResult()

        monkeypatch.setattr(shell_tools, "post_scrape_with_retry", fake_post)
        return captured

    def test_discovery_injection_reported_not_hidden(self, monkeypatch, tmp_path, _nav_state):
        ws = _setup_ws(tmp_path, [])
        self._capture_dispatch(monkeypatch)
        run_scraper = self._tool(monkeypatch)
        result = run_scraper(
            scraper_path=str(ws / "scraper_draft.py"), cli_args="--limit 2",
            timeout=600,
        )
        assert "[run scope] DISCOVERY — SCRAPER_LISTING_URL injected" in result
        assert "athleta.gap.com/womens-dresses" in result

    def test_verification_run_suppresses_discovery_injection(
        self, monkeypatch, tmp_path, _nav_state
    ):
        """A seed-scoped run must STAY seed-scoped — injecting a listing URL
        converts the verification into a full discovery walk (job-133: a 5-URL
        self-test charged ~690s of discovery)."""
        captured = self._capture_dispatch(monkeypatch)
        ws = _setup_ws(tmp_path, ["https://athleta.gap.com/p/1"])
        run_scraper = self._tool(monkeypatch)
        result = run_scraper(
            scraper_path=str(ws / "scraper_draft.py"),
            cli_args="--sample --input input_urls.json",
            timeout=600,
        )
        env = (captured["payload"].get("env_overrides") or {})
        assert "SCRAPER_LISTING_URL" not in env
        assert "[run scope] VERIFICATION" in result

    def test_seed_hygiene_rewrites_a_poison_seed_before_dispatch(
        self, monkeypatch, tmp_path, _nav_state
    ):
        """THE belt: whatever wrote input_urls.json, the file the subprocess
        actually consumes is filtered with the full-host rule."""
        import json

        captured = self._capture_dispatch(monkeypatch)
        ws = _setup_ws(
            tmp_path,
            [
                "https://athleta.gap.com/p/1",
                "https://www.gap.com/p/2",
                "https://bananarepublic.gap.com/p/3",
            ],
        )
        run_scraper = self._tool(monkeypatch)
        result = run_scraper(
            scraper_path=str(ws / "scraper_draft.py"),
            cli_args="--sample --input input_urls.json",
            timeout=600,
        )
        on_disk = json.loads((ws / "input_urls.json").read_text())
        assert on_disk["urls"] == ["https://athleta.gap.com/p/1"]
        assert "seed hygiene" in result
        assert "off-host=2" in result
        # extra_files staging (what browser_service receives) is the CLEAN copy
        extra = captured["payload"]["extra_files"]["input_urls.json"]
        assert "gap.com/p/2" not in extra

    def test_seed_hygiene_leaves_a_clean_file_untouched(
        self, monkeypatch, tmp_path, _nav_state
    ):
        import json

        self._capture_dispatch(monkeypatch)
        ws = _setup_ws(tmp_path, ["https://athleta.gap.com/p/1"])
        run_scraper = self._tool(monkeypatch)
        before = (ws / "input_urls.json").read_text()
        run_scraper(
            scraper_path=str(ws / "scraper_draft.py"),
            cli_args="--sample --input input_urls.json",
            timeout=600,
        )
        assert (ws / "input_urls.json").read_text() == before

    def test_http_verification_run_hygiene_and_no_injection(
        self, monkeypatch, tmp_path, _nav_state
    ):
        """The HTTP/local branch gets the same scope + hygiene treatment."""
        import json

        from agents.tools import shell_tools

        ran = {}

        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(cmd, **kw):
            ran["env"] = kw.get("env")
            return _R()

        monkeypatch.setattr(shell_tools.subprocess, "run", fake_run)
        ws = _setup_ws(
            tmp_path,
            ["https://athleta.gap.com/p/1", "https://www.gap.com/p/2"],
        )
        run_scraper = self._tool(monkeypatch, needs_browser=False)
        result = run_scraper(
            scraper_path=str(ws / "scraper_draft.py"),
            cli_args="--sample --input input_urls.json",
            timeout=600,
        )
        assert json.loads((ws / "input_urls.json").read_text())["urls"] == [
            "https://athleta.gap.com/p/1"
        ]
        assert "SCRAPER_LISTING_URL" not in (ran["env"] or {})
        assert "[run scope] VERIFICATION" in result


# ─── surface: the code-writer's navigation-derived seed ──────────────────────


class TestWriterSeedSurface:
    def test_writer_seed_is_filtered(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        assert "filtered writer seed" in src
        i_seed = src.find("sample_urls = il.get(\"urls\") or il.get(\"url_examples\")")
        i_filter = src.find("filtered writer seed")
        assert 0 < i_seed < i_filter

    def test_writer_prompt_teaches_the_seed_contract(self):
        src = open(
            os.path.join(ROOT, ".opencode", "agents", "code-writer.md")
        ).read()
        assert "verification-scope" in src
        assert "same-host" in src

    def test_cleanup_prompt_guards_the_promotion(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "subagents.py")).read()
        assert "ONLY same-host item URLs" in src


# ─── surface: views re-run staging ───────────────────────────────────────────


class TestViewsSurfaces:
    def test_rerun_write_filters_the_seed(self):
        src = open(os.path.join(ROOT, "webapp", "scraper", "views.py")).read()
        i_write = src.find('{"urls": _seed_urls}')
        assert i_write != -1, "re-run FM write must use the filtered list"
        i_filter = src.find("filter_seed_urls(site.input_urls")
        assert 0 < i_filter < i_write

    def test_rerun_staging_filters_the_extra_file(self):
        src = open(os.path.join(ROOT, "webapp", "scraper", "views.py")).read()
        i_stage = src.find('for _sf in ("input_urls.json", "discovery_config.json"):')
        i_filter = src.find("filter_seed_payload(")
        assert 0 < i_filter
        # the filter sits inside the staging loop's neighbourhood
        assert i_stage < i_filter < i_stage + 2500


# ─── surface: the playwright template probe cap ──────────────────────────────


class TestTemplateProbeCap:
    def test_probe_pages_come_from_the_env_knob(self):
        src = open(
            os.path.join(ROOT, "templates", "playwright_scraper.py")
        ).read()
        assert (
            '_PROBE_MAX_PAGES = int(os.environ.get("SCRAPER_DISCOVERY_MAX_PAGES", "5"))'
            in src
        ), "the --discover-only probe cap must honor SCRAPER_DISCOVERY_MAX_PAGES"
        assert "max_pages=5" not in src.replace(
            'SCRAPER_DISCOVERY_MAX_PAGES", "5"', ""
        ), "no hardcoded probe cap may remain"
        assert src.count("_PROBE_MAX_PAGES") >= 3, (
            "the constant must feed both the config_for_load_more default and "
            "the explicit-config override"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
