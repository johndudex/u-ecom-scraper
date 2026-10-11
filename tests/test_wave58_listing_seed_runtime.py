"""[wave-58 class 1] Listing-shaped url_list seeds drive RUNTIME discovery.

Batch 613-616 flagship. wave-57 taught the finalize scope gate that a
listing-seed url_list job's rows extend beyond the seeds — and then let the
run ship the seeds THEMSELVES as products: job 613 completed with exactly 3
"rows" that ARE its 3 listing seeds (category titles, defaulted prices),
while the real 913-row /product/... extraction sat in an earlier artifact.

Five runtime gates still key on ``input_mode in
("navigation","list_page","search_term")`` and leave url_list single-phase:

1. run_execution's SCRAPER_LISTING_URL env gate has no url_list arm;
2. the browser lane re-checks that mode set before injecting;
3. the tester lane SUPPRESSES the injection for verification-scope runs
   ("the seed file drives this run" — the exact mechanism that produced
   the junk pass);
4. ``_derive_strategy`` never reads input_mode (url_list + api stays api,
   whose template semantically refuses the listing env);
5. the writer brief FORBIDS discovery for url_list ("You MUST use these
   URLs exactly as provided. Do NOT discover new URLs").

And the classifier hole: ``row_url_is_item_shaped("/all/x")`` is True, so
rows==seeds passes the loosened scope arm; the tester's category-rows
finding rides at MEDIUM (never blocks) and the GROUND-TRUTH override
(``_override_min = 1`` for url_list) credits the 3 title+price rows.

Bar: a jjshouse-shaped job must produce rows whose URLs are NOT its seeds.
The inverse guard stands too: genuine PDP-paste sets keep every strict
behavior (wave-55 C2 / wave-57's own pins), and a listing-seed run that
DID expand passes untouched.
"""

import json
from unittest import mock

from src.discovery_quality import (
    listing_seed_urls,
    seed_url_is_listing_shaped,
    seeds_are_listing_shaped,
    url_list_discovery_seed,
)

# The batch-613 shape, verbatim (3 seeds; /shop/* is ambiguous-inert under
# the wave-57 classifier and must neither cause nor block).
JJHOUSE_SEEDS = [
    "https://www.jjshouse.com/all/bridesmaid-dresses",
    "https://www.jjshouse.com/collection/girls-dresses",
    "https://www.jjshouse.com/shop/accessories-shoes",
]


def _write_output(tmp_path, rows, name="output_run.json"):
    out = tmp_path / name
    out.write_text(
        json.dumps({"site": "jjshouse-com", "products": rows, "metadata": {}}),
        encoding="utf-8",
    )
    return str(out)


def _seed_row(url, title="Category", price=49.0):
    return {"url": url, "title": title, "price": price}


def _item_row(i):
    return {
        "url": f"https://www.jjshouse.com/product/jjshouse-dress-g2980{i:02d}",
        "title": f"JJ's House Dress {i}",
        "price": 99.0,
    }


# ── 1a. runtime-facing classifier helpers ────────────────────────────────


class TestListingSeedHelpers:
    def test_listing_seed_urls_returns_the_positive_members(self):
        """Only positively listing-shaped seeds, in order — the ambiguous
        /shop/* member is inert (the wave-57 contract, now runtime-facing)."""
        assert listing_seed_urls([
            "https://www.jjshouse.com/all/bridesmaid-dresses",
            "https://www.jjshouse.com/shop/accessories-shoes",
            "https://www.jjshouse.com/collection/girls-dresses",
        ]) == [
            "https://www.jjshouse.com/all/bridesmaid-dresses",
            "https://www.jjshouse.com/collection/girls-dresses",
        ]

    def test_listing_seed_urls_dedupes_and_keeps_order(self):
        assert listing_seed_urls([
            "https://shop.com/all/dresses",
            "https://shop.com/all/dresses",
            "https://shop.com/new-in",
        ]) == ["https://shop.com/all/dresses", "https://shop.com/new-in"]

    def test_listing_seed_urls_empty_for_item_sets(self):
        seeds = [
            "https://shop.com/product/dress-g1",
            "https://shop.com/p/12345",
        ]
        assert listing_seed_urls(seeds) == []
        assert not seeds_are_listing_shaped(seeds)

    def test_discovery_seed_picks_the_first_listing_seed(self):
        assert url_list_discovery_seed(
            JJHOUSE_SEEDS, "https://www.jjshouse.com/some-page"
        ) == "https://www.jjshouse.com/all/bridesmaid-dresses"

    def test_discovery_seed_empty_for_item_seed_sets(self):
        seeds = [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "https://www.jjshouse.com/product/jjshouse-odetta-g293497",
        ]
        assert url_list_discovery_seed(
            seeds, "https://www.jjshouse.com/x"
        ) == ""

    def test_discovery_seed_refuses_multi_host_sets(self):
        assert url_list_discovery_seed(
            [
                "https://www.jjshouse.com/all/dresses",
                "https://www.other-shop.com/collection/girls",
            ],
            "https://www.jjshouse.com/x",
        ) == ""

    def test_discovery_seed_refuses_cross_domain_candidate(self):
        """F17: the discovery seed must sit on the JOB's own host — a paste
        of someone else's listings never arms discovery."""
        assert url_list_discovery_seed(
            ["https://www.other-shop.com/all/dresses"],
            "https://www.jjshouse.com/x",
        ) == ""


# ── 1b. run_execution arms discovery for listing seeds ───────────────────


class TestRunExecutionArmsDiscovery:
    _SRC = None

    @classmethod
    def _source(cls):
        if cls._SRC is None:
            import importlib
            import inspect

            # MODULE source — the env gate lives in run_execution() but the
            # browser lane + the expansion gate live in sibling functions.
            # (importlib, NOT `import webapp.agents.nodes.run_execution`: the
            # nodes package re-exports a FUNCTION named run_execution that
            # shadows the submodule attribute.)
            cls._SRC = inspect.getsource(
                importlib.import_module("webapp.agents.nodes.run_execution"))
        return cls._SRC

    def test_env_gate_has_a_url_list_listing_seed_arm(self):
        src = self._source()
        assert 'elif input_mode == "url_list":' in src
        assert "url_list_discovery_seed" in src

    def test_browser_lane_admits_the_armed_url_list_run(self):
        """M6's single source of truth carries the env; the browser lane's
        mode re-check must not re-exclude url_list after the gate armed it."""
        src = self._source()
        i = src.index('_listing_env_bs and')
        assert 'input_mode") == "url_list' in src[i:i + 400]

    def test_expansion_gate_wired_in_both_lanes(self):
        src = self._source()
        assert src.count("_listing_seed_expansion_gate(") >= 3  # def + 2 lanes


# ── 1d-i. the expansion gate (rows==seeds is NOT extraction) ────────────


class TestListingSeedExpansionGate:
    def test_seed_rows_fail_the_gate(self, tmp_path):
        """The 613 junk pass, byte-for-byte: 3 rows that ARE the 3 seeds."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        out = _write_output(
            tmp_path,
            [_seed_row(u) for u in JJHOUSE_SEEDS],
        )
        verdict = _listing_seed_expansion_gate(out, JJHOUSE_SEEDS, "url_list")
        assert "listing" in verdict.lower()
        assert "discovery" in verdict.lower()

    def test_expanded_rows_pass(self, tmp_path):
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        out = _write_output(tmp_path, [_item_row(i) for i in range(50)])
        assert _listing_seed_expansion_gate(
            out, JJHOUSE_SEEDS, "url_list"
        ) == ""

    def test_seeds_among_expanded_rows_pass(self, tmp_path):
        """wave-57's normal listing-seed run shape: deeper items PLUS a seed
        row or two — discovery happened, the yield bars own the rest."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        rows = [_item_row(i) for i in range(5)]
        rows.append(_seed_row(JJHOUSE_SEEDS[0]))
        out = _write_output(tmp_path, rows)
        assert _listing_seed_expansion_gate(
            out, JJHOUSE_SEEDS, "url_list"
        ) == ""

    def test_zero_rows_stays_silent(self, tmp_path):
        """Zero rows is the zero-yield gate's subject, not ours."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        out = _write_output(tmp_path, [])
        assert _listing_seed_expansion_gate(
            out, JJHOUSE_SEEDS, "url_list"
        ) == ""

    def test_rows_without_url_keys_stay_silent(self, tmp_path):
        """F4b spelling: rows shipped but none carry a URL key — the
        comparison is unverifiable, never a false assertion."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        out = _write_output(
            tmp_path,
            [{"title": "no url key", "price": 1.0} for _ in range(3)],
        )
        assert _listing_seed_expansion_gate(
            out, JJHOUSE_SEEDS, "url_list"
        ) == ""

    def test_canonicalised_seed_row_still_counts_as_a_seed(self, tmp_path):
        """A listing that comes back with a tracking query / trailing slash
        is still the submitted listing (the _submitted_url_key contract)."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        rows = [
            _seed_row("https://jjshouse.com/all/bridesmaid-dresses?utm=x"),
            _seed_row("https://www.jjshouse.com/collection/girls-dresses/"),
        ]
        out = _write_output(tmp_path, rows)
        assert _listing_seed_expansion_gate(out, JJHOUSE_SEEDS, "url_list")

    def test_scheme_flip_is_the_same_item(self, tmp_path):
        """[critique B4] _submitted_url_key's own docstring promises scheme-
        insensitivity, but the code retained the scheme — so a site that
        canonicalizes the submitted http:// listing to https:// read as a
        STRANGER: the strict scope gate false-vetoed an honest run and this
        gate false-passed a junk one. Host+path identity, both directions."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
            _submitted_url_key,
        )

        assert _submitted_url_key(
            "http://www.jjshouse.com/all/bridesmaid-dresses"
        ) == _submitted_url_key(
            "https://www.jjshouse.com/all/bridesmaid-dresses/"
        )
        rows = [
            _seed_row("https://www.jjshouse.com/all/bridesmaid-dresses"),
            _seed_row("https://www.jjshouse.com/collection/girls-dresses"),
        ]
        seeds = [
            "http://www.jjshouse.com/all/bridesmaid-dresses",
            "http://www.jjshouse.com/collection/girls-dresses",
        ]
        out = _write_output(tmp_path, rows)
        assert _listing_seed_expansion_gate(out, seeds, "url_list")

    def test_single_seed_row_stays_silent(self, tmp_path):
        """[critique S1] A correct ONE-product store whose only page sits at
        a listing-shaped URL is byte-identical to the junk shape — a gate
        that fired on it would burn the fix ladder against a site that has
        nothing more to give. A listing that expands yields many; require
        more than one all-seed row before asserting discovery never ran."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        out = _write_output(
            tmp_path, [_seed_row(JJHOUSE_SEEDS[0])])
        assert _listing_seed_expansion_gate(
            out, JJHOUSE_SEEDS, "url_list"
        ) == ""

    def test_item_seeds_rows_equal_seeds_pass(self, tmp_path):
        """The mode's own contract: PDP seeds whose rows are the PDPs is a
        real url_list extraction — never vetoed."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        seeds = [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "https://www.jjshouse.com/product/jjshouse-odetta-g293497",
        ]
        rows = [_seed_row(s, "Dress", 99.0) for s in seeds]
        out = _write_output(tmp_path, rows)
        assert _listing_seed_expansion_gate(out, seeds, "url_list") == ""

    def test_other_modes_silent(self, tmp_path):
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        out = _write_output(
            tmp_path, [_seed_row(u) for u in JJHOUSE_SEEDS]
        )
        assert _listing_seed_expansion_gate(
            out, JJHOUSE_SEEDS, "list_page"
        ) == ""
        assert _listing_seed_expansion_gate(out, JJHOUSE_SEEDS, "") == ""

    def test_multi_host_listing_seeds_stay_silent_here(self, tmp_path):
        """Multi-host listing pastes keep the strict scope gate (wave-57);
        the expansion bar does not manufacture a second verdict for a set
        no runtime arm would have driven discovery for anyway."""
        from webapp.agents.nodes.run_execution import (
            _listing_seed_expansion_gate,
        )

        seeds = [
            "https://www.jjshouse.com/all/dresses",
            "https://www.other-shop.com/collection/girls",
        ]
        rows = [_seed_row(u) for u in seeds]
        out = _write_output(tmp_path, rows)
        assert _listing_seed_expansion_gate(out, seeds, "url_list") == ""


# ── 1c. strategy + writer brief see the listing shape ────────────────────


class TestStrategySeesListingSeeds:
    def test_api_strategy_upgrades_for_listing_seeds(self):
        from agents.graph import _apply_listing_seed_strategy

        analysis = {"strategy": "api"}
        out = _apply_listing_seed_strategy(analysis, {
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "url": "https://www.jjshouse.com/x",
        })
        assert out["strategy"] == "http_navigation"
        assert out.get("listing_seed_discovery") is True

    def test_http_requests_strategy_is_kept(self):
        """[wave-59 C6 — contract FLIPPED] wave-58 kept http_requests on
        listing seeds ("requests_scraper already carries the env-gate
        two-phase branch"). That branch is env-gated and NEVER fired in prod,
        and the fleet scan found ZERO http_requests+listing-shaped shipped
        scrapers — while R2 showed a single-phase fetch of a LISTING page
        cannot discover items. Listing-shaped seeds now walk via
        http_navigation too, with the reason stamped."""
        from agents.graph import _apply_listing_seed_strategy

        out = _apply_listing_seed_strategy({"strategy": "http_requests"}, {
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "url": "https://www.jjshouse.com/x",
        })
        assert out["strategy"] == "http_navigation"
        assert out.get("listing_seed_discovery") is True
        assert "http_requests" in (out.get("listing_seed_reason") or "")

    def test_api_strategies_constant_untouched(self):
        """The widen is LOCAL to the listing-seed arm — template-family flag
        maps and the escalation ladder must not see http_requests
        reclassified globally."""
        from agents.constants import API_STRATEGIES

        assert API_STRATEGIES == frozenset({"api", "internal_api"})

    def test_http_requests_outside_listing_seeds_untouched(self):
        from agents.graph import _apply_listing_seed_strategy

        for mode in ("search_term", "navigation", "list_page"):
            out = _apply_listing_seed_strategy(
                {"strategy": "http_requests"},
                {
                    "input_mode": mode,
                    "input_urls": JJHOUSE_SEEDS,
                    "url": "https://www.jjshouse.com/x",
                },
            )
            assert out["strategy"] == "http_requests", mode

    def test_item_seed_sets_are_never_touched(self):
        from agents.graph import _apply_listing_seed_strategy

        state = {
            "input_mode": "url_list",
            "input_urls": [
                "https://shop.com/product/dress-g1",
                "https://shop.com/p/2",
            ],
            "url": "https://shop.com/x",
        }
        out = _apply_listing_seed_strategy({"strategy": "api"}, state)
        assert out["strategy"] == "api"
        assert "listing_seed_discovery" not in out

    def test_decide_strategy_invokes_the_arm(self):
        import inspect

        from agents import graph

        src = inspect.getsource(graph._decide_strategy)
        assert "_apply_listing_seed_strategy" in src


class TestWriterBriefSeesListingSeeds:
    def _build(self, state):
        from agents import subagents as sub

        msgs = sub.build_code_writer_message(state)
        return str(msgs[0].content)

    def test_listing_seed_brief_prescribes_discovery(self):
        content = self._build({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
        })
        assert "LISTING" in content.upper()
        assert "SCRAPER_LISTING_URL" in content
        assert "Phase 1" in content or "PHASE 1" in content
        # the junk-pass directives must be gone on this branch
        assert "You MUST use these URLs exactly as provided" not in content
        assert (
            "Do NOT discover, crawl, or add any product URLs" not in content
        )

    def test_item_seed_brief_keeps_the_strict_directives(self):
        """The inverse guard: PDP-paste sets keep every forbid (the
        job-1227 class is still policed)."""
        seeds = [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "https://www.jjshouse.com/product/jjshouse-odetta-g293497",
        ]
        content = self._build({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "input_mode": "url_list",
            "input_urls": seeds,
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
        })
        assert "You MUST use these URLs exactly as provided" in content
        assert "Do NOT discover, crawl, or add any product URLs" in content

    def test_brief_tells_the_truth_about_the_runtime_seed(self):
        """[guardian change 7] the helper delivers ONE registrable-host seed
        and compares scheme-insensitively — the brief must say exactly that,
        never promise runtime delivery of the remaining seeds."""
        content = self._build({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "scraper_analysis": {"strategy": "http_requests"},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
        }).lower()
        assert "exactly one seed" in content
        assert "registrable host" in content
        assert "scheme-insensitively" in content
        # the remaining seeds are the scraper's OWN walk
        assert "not injected at runtime" in content
        # the multi-host promise the helper does not implement is gone
        assert "the first listing seed)" not in content
        assert "seeds the first one" not in content
        # the OBJECTIVE fork (the branch that drove 613's writer) carries
        # the same truth in its own words
        assert "nothing else injects" in content

    def test_api_strategy_listing_seed_gets_two_phase_template(self):
        from agents import subagents as sub

        with mock.patch.dict("os.environ", {}, clear=False):
            content = str(sub.build_code_writer_message({
                "site_slug": "jjhouse",
                "url": "https://www.jjshouse.com/x",
                "input_mode": "url_list",
                "input_urls": JJHOUSE_SEEDS,
                "scraper_analysis": {"strategy": "api"},
                "target_fields": ["title", "price"],
                "page_type": "product",
                "site_analysis": {},
            })[0].content)
        assert "http_navigation_scraper.py" in content

    def test_no_mechanism_listing_seed_gets_the_requests_template(self):
        from agents import subagents as sub

        content = str(sub.build_code_writer_message({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "scraper_analysis": {},
            "target_fields": ["title", "price"],
            "page_type": "product",
            "site_analysis": {},
        })[0].content)
        assert "requests_scraper.py" in content


class TestTesterLaneAndBrief:
    def test_tester_lane_injects_for_listing_seed_url_list(
            self, monkeypatch, tmp_path):
        """The exact shape that produced 613's junk pass: a url_list job with
        listing-shaped seeds hits the tester's default verification-scope run
        (--sample --input input_urls.json) and the suppression note swallowed
        discovery. Behavior pin (wave-14 idiom): the listing env IS injected,
        the run keeps the 600s DISCOVERY floor (a two-phase walk does not fit
        the 180s verification floor), and the note says so."""
        import time as _time

        from agents.tools import context as ctx
        from agents.tools import shell_tools
        from agents.tools.context import set_tool_deadline

        state = {
            "job_id": 0,
            "url": "https://www.jjshouse.com/",
            "site_slug": "jjhouse",
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
        }
        monkeypatch.setattr(ctx, "get_state", lambda: state)

        ws = tmp_path / "workspace" / "jjhouse"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("# draft\n")
        (ws / "input_urls.json").write_text(json.dumps({"urls": JJHOUSE_SEEDS}))

        captured = {}

        class _FakeResult:
            ok = True
            error = ""
            error_class = ""
            transient = False
            unavailable = False
            server_error_class = ""
            data = {"stdout": "ok", "stderr": "", "duration": 1}

        def fake_post(url, payload, timeout=None, **kw):
            captured["payload"] = payload
            return _FakeResult()

        monkeypatch.setattr(
            shell_tools, "post_scrape_with_retry", fake_post)
        tools = {t.name: t for t in shell_tools.get_shell_tools()}
        monkeypatch.setattr(
            shell_tools, "_scraper_needs_browser", lambda p: True)
        run_scraper = tools["run_scraper"].func
        set_tool_deadline(_time.time() + 700)
        try:
            result = run_scraper(
                scraper_path=str(ws / "scraper_draft.py"),
                cli_args="--sample --input input_urls.json",
                timeout=100,
            )
        finally:
            ctx.clear_tool_context()
        env = (captured["payload"] or {}).get("env_overrides") or {}
        assert env.get("SCRAPER_LISTING_URL") == JJHOUSE_SEEDS[0], result[:400]
        # floored UP to the 600s discovery floor — a two-phase walk cannot
        # fit the 180s verification floor (the job-312 SIGKILL class)
        assert captured["payload"].get("timeout") == 600
        assert "DISCOVERY" in result

    def test_tester_lane_keeps_ordinary_url_list_suppressed(
            self, monkeypatch, tmp_path):
        """job-133's budget contract is untouched: an ORDINARY url_list
        verification run (item-shaped seeds) still suppresses injection."""
        import time as _time

        from agents.tools import context as ctx
        from agents.tools import shell_tools
        from agents.tools.context import set_tool_deadline

        seeds = ["https://www.jjshouse.com/product/jjshouse-cesia-g298054"]
        state = {
            "job_id": 0,
            "url": "https://www.jjshouse.com/",
            "site_slug": "jjhouse",
            "input_mode": "url_list",
            "input_urls": seeds,
        }
        monkeypatch.setattr(ctx, "get_state", lambda: state)

        ws = tmp_path / "workspace" / "jjhouse"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text("# draft\n")
        (ws / "input_urls.json").write_text(json.dumps({"urls": seeds}))

        captured = {}

        class _FakeResult:
            ok = True
            error = ""
            error_class = ""
            transient = False
            unavailable = False
            server_error_class = ""
            data = {"stdout": "ok", "stderr": "", "duration": 1}

        def fake_post(url, payload, timeout=None, **kw):
            captured["payload"] = payload
            return _FakeResult()

        monkeypatch.setattr(
            shell_tools, "post_scrape_with_retry", fake_post)
        tools = {t.name: t for t in shell_tools.get_shell_tools()}
        monkeypatch.setattr(
            shell_tools, "_scraper_needs_browser", lambda p: True)
        run_scraper = tools["run_scraper"].func
        set_tool_deadline(_time.time() + 700)
        try:
            result = run_scraper(
                scraper_path=str(ws / "scraper_draft.py"),
                cli_args="--sample --input input_urls.json",
                timeout=100,
            )
        finally:
            ctx.clear_tool_context()
        env = (captured["payload"] or {}).get("env_overrides") or {}
        assert "SCRAPER_LISTING_URL" not in env
        # the cheap verification floor is the ordinary url_list contract
        assert captured["payload"].get("timeout") == 180
        assert "suppressed" in result

    def test_tester_brief_reports_phase1_truthfully_for_listing_seeds(self):
        from agents import subagents as sub

        content = str(sub.build_code_tester_message({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "page_type": "product",
            "target_fields": ["title", "price"],
        })[0].content)
        assert "For url_list jobs, skip this (no Phase 1)" not in content
        assert "LISTING" in content.upper()

    def test_tester_brief_keeps_the_url_list_skip_for_item_seeds(self):
        from agents import subagents as sub

        content = str(sub.build_code_tester_message({
            "site_slug": "jjhouse",
            "url": "https://www.jjshouse.com/x",
            "input_mode": "url_list",
            "input_urls": [
                "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            ],
            "page_type": "product",
            "target_fields": ["title", "price"],
        })[0].content)
        assert "For url_list jobs, skip this (no Phase 1)" in content


# ── 1d-ii. the tester-side classifier close ──────────────────────────────


class TestTesterClassifierClose:
    def _seed_output(self, monkeypatch, tmp_path, rows):
        (tmp_path / "workspace" / "jjhouse").mkdir(parents=True, exist_ok=True)
        (tmp_path / "workspace" / "jjhouse" / "output_test.json").write_text(
            json.dumps({"products": rows, "metadata": {}}), encoding="utf-8"
        )
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

    def test_deterministic_issue_fires_for_seed_rows(self, monkeypatch, tmp_path):
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        self._seed_output(
            monkeypatch, tmp_path, [_seed_row(u) for u in JJHOUSE_SEEDS]
        )
        issues = rat.deterministic_output_issues("jjhouse", {
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
        })
        hit = [i for i in issues if i.get("severity") == "high"]
        assert hit, f"expected a high-severity listing-seed issue, got {issues}"
        assert "listing" in str(hit[0].get("description", "")).lower()

    def test_deterministic_issue_silent_when_expanded(self, monkeypatch, tmp_path):
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        self._seed_output(
            monkeypatch, tmp_path, [_item_row(i) for i in range(5)]
        )
        issues = rat.deterministic_output_issues("jjhouse", {
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
        })
        assert not [i for i in issues if i.get("severity") == "high"]

    def test_ground_truth_override_vetoed_for_seed_rows(self, monkeypatch):
        """The 613 PASS path: _scraper_has_real_items sees 3 title+price
        rows and the override fires. With listing-shaped seeds whose rows
        are the seeds themselves, the override must be vetoed — centrally,
        inside _scraper_has_real_items, so EVERY arm it gates inherits the
        veto (critique B2: the no-report/exhausted/dead-store/final-attempt
        rescues read the same predicate; a per-arm veto would have left 8 of
        ~10 arms crediting rows==seeds)."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        rows = [_seed_row(u) for u in JJHOUSE_SEEDS]
        state = {
            "site_slug": "jjhouse",
            "job_id": 0,
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "test_report": {
                "sample_products": rows,
                "overall_assessment": "PASS",
            },
        }
        assert rat._listing_seed_rows_unexpanded(state) is True
        assert rat._scraper_has_real_items(state) is False

    def test_veto_needs_more_than_one_seed_row(self, monkeypatch):
        """[critique S1] the degenerate one-product store: a single row that
        happens to be the submitted listing-shaped URL stays real items."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        state = {
            "site_slug": "jjhouse",
            "job_id": 0,
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "test_report": {
                "sample_products": [_seed_row(JJHOUSE_SEEDS[0])],
                "overall_assessment": "PASS",
            },
        }
        assert rat._listing_seed_rows_unexpanded(state) is False

    def test_single_seed_row_gets_no_high_issue(self, monkeypatch, tmp_path):
        """[critique S1] the deterministic issue follows the same >=2 bar."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        self._seed_output(
            monkeypatch, tmp_path, [_seed_row(JJHOUSE_SEEDS[0])]
        )
        issues = rat.deterministic_output_issues("jjhouse", {
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
        })
        assert not [i for i in issues if i.get("severity") == "high"]

    def test_override_not_vetoed_when_expanded(self):
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        state = {
            "site_slug": "jjhouse",
            "job_id": 0,
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
            "test_report": {
                "sample_products": [_item_row(i) for i in range(4)],
                "overall_assessment": "PASS",
            },
        }
        assert rat._listing_seed_rows_unexpanded(state) is False

    def test_item_seed_sets_are_never_vetoed(self):
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        seeds = [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "https://www.jjshouse.com/product/jjshouse-odetta-g293497",
        ]
        state = {
            "site_slug": "jjhouse",
            "job_id": 0,
            "input_mode": "url_list",
            "input_urls": seeds,
            "test_report": {
                "sample_products": [
                    _seed_row(s, "Dress", 99.0) for s in seeds
                ],
            },
        }
        assert rat._listing_seed_rows_unexpanded(state) is False


# ── guardian change 3: the veto judges the FULL floored artifact ─────────


def _all_seed_state(sample_rows=None):
    return {
        "site_slug": "jjhouse",
        "job_id": 0,
        "input_mode": "url_list",
        "input_urls": JJHOUSE_SEEDS,
        "test_report": {
            "sample_products": (
                [_seed_row(u) for u in JJHOUSE_SEEDS]
                if sample_rows is None else sample_rows
            ),
            "overall_assessment": "PASS",
        },
    }


class TestFullFileVeto:
    def test_floored_file_with_off_seed_rows_refuses_the_veto(
        self, monkeypatch
    ):
        """[guardian change 3] the tester sample is a slice: an all-seed
        SAMPLE must not veto when the job's own artifact holds off-seed
        rows — that is a real extraction, not seed echo."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        seen = {}

        def fake_file(slug, mtime_floor=None):
            seen["slug"], seen["floor"] = slug, mtime_floor
            return [_seed_row(u) for u in JJHOUSE_SEEDS] + [
                _item_row(i) for i in range(4)
            ]

        monkeypatch.setattr(rat, "_items_from_output_file", fake_file)
        monkeypatch.setattr(rat, "_job_started_floor", lambda st: 123.0)
        assert rat._listing_seed_rows_unexpanded(_all_seed_state()) is False
        # the floor is MANDATORY — the read goes through the started floor
        assert seen["slug"] == "jjhouse"
        assert seen["floor"] == 123.0

    def test_override_survives_when_only_the_file_expanded(
        self, monkeypatch
    ):
        """613's inverse shape: the tester sampled the seeds but the artifact
        expanded — the ground-truth override still credits the job."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        monkeypatch.setattr(
            rat,
            "_items_from_output_file",
            lambda slug, mtime_floor=None: [
                _item_row(i) for i in range(5)
            ],
        )
        assert rat._scraper_has_real_items(_all_seed_state()) is True

    def test_veto_stands_when_the_floored_file_is_all_seed(
        self, monkeypatch
    ):
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        monkeypatch.setattr(
            rat,
            "_items_from_output_file",
            lambda slug, mtime_floor=None: [
                _seed_row(u) for u in JJHOUSE_SEEDS
            ],
        )
        monkeypatch.setattr(rat, "_job_started_floor", lambda st: 5.0)
        assert rat._listing_seed_rows_unexpanded(_all_seed_state()) is True

    def test_stale_artifact_cannot_rescue_the_run(self, monkeypatch):
        """The floor cuts both ways: an artifact OLDER than the job (the
        floored read returns nothing) leaves the sample verdict standing —
        job-309's inverse false-pass stays closed."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        monkeypatch.setattr(
            rat, "_items_from_output_file", lambda slug, mtime_floor=None: []
        )
        monkeypatch.setattr(rat, "_job_started_floor", lambda st: 9.0)
        assert rat._listing_seed_rows_unexpanded(_all_seed_state()) is True

    def test_unreadable_artifact_never_blinds_the_veto(self, monkeypatch):
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        def boom(slug, mtime_floor=None):
            raise RuntimeError("unreadable artifact")

        monkeypatch.setattr(rat, "_items_from_output_file", boom)
        monkeypatch.setattr(rat, "_job_started_floor", lambda st: 9.0)
        assert rat._listing_seed_rows_unexpanded(_all_seed_state()) is True

    def test_partial_seed_run_gets_a_medium_issue(
        self, monkeypatch, tmp_path
    ):
        """[guardian change 3] real items ARE present, so no veto and no
        HIGH — a non-blocking count instead."""
        import importlib

        rat = importlib.import_module(
            "webapp.agents.nodes.route_after_testing")

        rows = [_seed_row(u) for u in JJHOUSE_SEEDS] + [
            _item_row(i) for i in range(4)
        ]
        TestTesterClassifierClose()._seed_output(monkeypatch, tmp_path, rows)
        issues = rat.deterministic_output_issues("jjhouse", {
            "input_mode": "url_list",
            "input_urls": JJHOUSE_SEEDS,
        })
        med = [
            i for i in issues
            if i.get("severity") == "medium"
            and "listing seeds" in str(i.get("description", ""))
        ]
        assert med, f"expected the partial-seed MEDIUM, got {issues}"
        assert med[0]["count"] == len(JJHOUSE_SEEDS)
        assert not [
            i for i in issues
            if i.get("severity") == "high"
            and "listing" in str(i.get("description", "")).lower()
        ]


# ── the wave-57 finalize gate keeps its shape ────────────────────────────


class TestWave57GateUnchanged:
    def test_566_scope_gate_still_loosens(self, tmp_path):
        from webapp.agents.nodes.run_execution import _url_list_scope_gate

        out = _write_output(tmp_path, [_item_row(i) for i in range(913)])
        assert _url_list_scope_gate(out, JJHOUSE_SEEDS, "url_list") == ""

    def test_classifier_pins_unchanged(self):
        assert seed_url_is_listing_shaped(JJHOUSE_SEEDS[0])
        assert not seed_url_is_listing_shaped(JJHOUSE_SEEDS[2])
        assert seeds_are_listing_shaped(JJHOUSE_SEEDS)
