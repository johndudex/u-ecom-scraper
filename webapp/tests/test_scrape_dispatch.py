"""Regression tests for the browser_service /scrape dispatch surface (W8 slip).

Prod job 51 (sephora): every browser-strategy code_tester run died with
``'ScrapeResult' object has no attribute 'status_code'`` — commit 16db180
replaced the bare httpx.post with post_scrape_with_retry (which returns a
ScrapeResult exposing ``status``), but two 404 special-cases kept reading the
old httpx attribute. The scraper never executed; the tester recorded CRASH.

Three layers here:
1. behavioral — run_scraper's browser branch (the code_tester path),
2. behavioral — run_execution._run_via_browser_service's 404/not-ok early
   returns (the production execution path),
3. structural — an AST guard asserting NO caller of post_scrape_with_retry
   ever touches ``.status_code`` again (the whole bug class, including
   future call sites).
"""

import ast
import json

import pytest

from agents.tools import shell_tools as st
from agents.tools.browser_http import ScrapeResult


def _ok_result(output_name="output_sample.json", items=3):
    return ScrapeResult(
        ok=True,
        status=200,
        attempts=1,
        data={
            "returncode": 0,
            "stdout": f"scraped {items} items",
            "stderr": "",
            "output_content": json.dumps({"products": list(range(items))}),
            "output_name": output_name,
            "duration": 2,
        },
    )


def _write_browser_scraper(tmp_path, name="scraper_draft.py"):
    """A draft whose imports sniff as browser-based (needs_browser=True)."""
    path = tmp_path / name
    path.write_text("import playwright\nprint('fake scraper')\n", encoding="utf-8")
    return str(path)


def _get_run_scraper(tmp_path):
    (_run_bash, run_scraper) = st.get_shell_tools(project_root=str(tmp_path))
    return run_scraper


class TestRunScraperBrowserDispatch:
    """The code_tester path: needs_browser → post_scrape_with_retry."""

    def test_success_dispatches_and_persists_output(self, monkeypatch, tmp_path):
        sent = {}
        calls = []

        def fake_post(url, payload, *, timeout, **kw):
            calls.append(url)
            sent.update(payload)
            return _ok_result()

        monkeypatch.setattr(st, "post_scrape_with_retry", fake_post)
        scraper = _write_browser_scraper(tmp_path)
        res = _get_run_scraper(tmp_path).invoke(
            {"scraper_path": scraper, "cli_args": "--sample", "timeout": 60}
        )
        assert calls and "/scrape" in calls[0]
        assert "scraped 3 items" in res
        assert "[ran on browser_service" in res
        # output CONTENT is persisted next to the draft for the ground-truth read
        out_file = tmp_path / "output_sample.json"
        assert out_file.exists()
        assert json.loads(out_file.read_text())["products"] == [0, 1, 2]
        assert "[output_file:" in res

    def test_404_returns_rejection_message_not_attributeerror(
        self, monkeypatch, tmp_path
    ):
        """THE prod-51 regression: with the old `.status_code` access this
        branch raised inside the tool and the tester got 'Error dispatching
        to browser_service: ... no attribute status_code' with a CRASH
        verdict and the scraper never executed."""

        def fake_post(url, payload, *, timeout, **kw):
            return ScrapeResult(
                ok=False,
                status=404,
                error="Scraper rejected by browser_service (source invalid)",
                attempts=1,
            )

        monkeypatch.setattr(st, "post_scrape_with_retry", fake_post)
        scraper = _write_browser_scraper(tmp_path)
        res = _get_run_scraper(tmp_path).invoke(
            {"scraper_path": scraper, "cli_args": "", "timeout": 60}
        )
        assert res == "Scraper rejected by browser_service (source invalid)"
        assert "Error dispatching" not in res
        assert "status_code" not in res

    def test_not_ok_fatal_surfaces_error_class(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            st,
            "post_scrape_with_retry",
            lambda *a, **kw: ScrapeResult(
                status=500, error="browser_service returned HTTP 500", attempts=1
            ),
        )
        scraper = _write_browser_scraper(tmp_path)
        res = _get_run_scraper(tmp_path).invoke(
            {"scraper_path": scraper, "cli_args": "", "timeout": 60}
        )
        assert "Scraper run failed (fatal)" in res
        assert "HTTP 500" in res


class TestRunExecutionBrowserPath:
    """The production path: run_execution._run_via_browser_service.

    post_scrape_with_retry is imported INSIDE the function from
    ..tools.browser_http, so the patch targets the source module.
    """

    @pytest.fixture(autouse=True)
    def _patch_source_module(self, monkeypatch):
        # agents.nodes re-exports the run_execution FUNCTION — import the
        # module itself or the attribute access below resolves to the function.
        import importlib

        re_mod = importlib.import_module("agents.nodes.run_execution")
        from agents.tools import browser_http as bh

        self._re = re_mod
        self._bh = bh
        self.fake = None
        monkeypatch.setattr(
            bh, "post_scrape_with_retry", lambda *a, **kw: self.fake
        )

    def _run(self, tmp_path):
        scraper = _write_browser_scraper(tmp_path)
        return self._re._run_via_browser_service(scraper, [], str(tmp_path), None)

    def test_404_fails_with_rejection_message(self, tmp_path):
        self.fake = ScrapeResult(
            ok=False,
            status=404,
            error="Scraper rejected by browser_service (source invalid)",
            attempts=1,
        )
        result = self._run(tmp_path)
        # Old code: AttributeError → generic except → the AttributeError text
        # landed in error_message. Pinned to the real rejection message.
        assert result["execution_status"] == "FAILED"
        assert (
            result["error_message"]
            == "Scraper rejected by browser_service (source invalid)"
        )

    def test_not_ok_fatal_fails_with_helper_error(self, tmp_path):
        self.fake = ScrapeResult(
            status=503, error="browser_service returned HTTP 503", attempts=3
        )
        result = self._run(tmp_path)
        assert result["execution_status"] == "FAILED"
        assert result["error_message"] == "browser_service returned HTTP 503"


class TestNoStaleStatusCode:
    """AST guard over the whole bug class: any name assigned directly from
    post_scrape_with_retry is a ScrapeResult — .status_code must never be
    read off it (catches future call sites too, not just these two)."""

    def _assigned_names(self, tree):
        names = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            called = getattr(func, "id", None) or getattr(func, "attr", None)
            if called == "post_scrape_with_retry":
                names.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
        return names

    @pytest.mark.parametrize(
        "module",
        ["agents.tools.shell_tools", "agents.nodes.run_execution"],
    )
    def test_no_status_code_on_scrape_results(self, module):
        mod = __import__(module, fromlist=["__doc__"])
        with open(mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = self._assigned_names(tree)
        assert names, f"{module}: expected at least one post_scrape_with_retry call"
        offenders = [
            f"{mod.__file__}:{n.lineno}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and n.attr == "status_code"
            and isinstance(n.value, ast.Name)
            and n.value.id in names
        ]
        assert offenders == [], (
            f"{module}: ScrapeResult has .status, not .status_code — "
            f"stale access at {offenders}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
