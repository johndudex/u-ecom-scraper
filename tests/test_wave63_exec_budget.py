"""Wave-63: execution time-budget (graceful bound) + timeout salvage.

Fix-loop entry for Guardian verdict 662 FAIL (jomashop execution-budget wall):
the tester certifies the draft at ``--limit 50`` (579s) then run_execution
fires the SAME scraper UNCAPPED on a catalogue-scale listing — the walk
mathematically exceeds the browser-service 3600s wall, the runner SIGKILLs at
timeout, and the templates (output written only at the end) lose everything:
honest FAILED pc=0 from a certified-good draft.

Contract (docs/plans/wave63-exec-budget-plan.md, critique-PASSED v3):

- **N1 graceful bound**: ``SCRAPER_EXEC_TIME_BUDGET`` env (injected from
  setting ``EXECUTION_ITEM_TIME_BUDGET``, default 3000) at ALL THREE dispatch
  sites; templates split it into TWO deadlines — discovery gets budget minus a
  CLAMPED reserve, extraction may run to the full budget — so execution can
  always FINISH with rows. Sites that finish under budget are unaffected.
- **N2 salvage**: templates flush partial output every 25 items (atomic
  single-name ``os.replace`` via ``.json.tmp`` — must NOT match the
  ``output_*.json`` glob); on runner timeout or crash-with-fresh-output both
  lanes harvest the flushed file: status FAILED (never COMPLETED), rows on the
  board, ``PARTIAL HARVEST`` note. rc==3/DISCOVERY_ZERO keeps priority.
- **N3 marker**: ``budget_wall_timeout`` distinguishes a budget wall from a
  hang.

House style: templates are not importable (see test_template_output_collision)
— template pins are static source contracts; the new pure helpers are
executed from source and tested behaviorally.
"""
from __future__ import annotations

import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEMPLATES = os.path.join(ROOT, "templates")

NAV = os.path.join(TEMPLATES, "navigation_scraper.py")
HTTP = os.path.join(TEMPLATES, "http_navigation_scraper.py")
RUN_EXEC = os.path.join(ROOT, "webapp", "agents", "nodes", "run_execution.py")
RUNNER = os.path.join(ROOT, "browser_service", "scraper_runner.py")
GRAPH = os.path.join(ROOT, "webapp", "agents", "graph.py")
SETTINGS = os.path.join(ROOT, "webapp", "config", "settings.py")

BUDGET_ENV = "SCRAPER_EXEC_TIME_BUDGET"
STOP_REASON = "exec_time_budget"
BOUNDED_KEY = "time_budget_bounded"
MARKER = "budget_wall_timeout"


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _grab(src: str, name: str) -> str:
    """Extract a top-level ``def name`` block EXACTLY (ast-derived, no
    regex-overrun into following module constants)."""
    import ast

    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg, f"{name}: empty segment"
            return seg
    raise AssertionError(f"{name} not found")


# ────────────────────────────────────────────────────────────────────────────
# N1 — templates: budget env read + clamped two-deadline math
# ────────────────────────────────────────────────────────────────────────────


class TestTemplateBudgetEnv:
    def test_both_templates_read_budget_env(self):
        for path in (NAV, HTTP):
            src = _read(path)
            assert BUDGET_ENV in src, (
                f"{os.path.basename(path)}: missing {BUDGET_ENV} read — "
                "the graceful bound never arms"
            )

    def test_nav_budget_read_beside_listing_gate(self):
        """The budget env READ sits in main() beside the SCRAPER_LISTING_URL gate."""
        src = _read(NAV)
        read_idx = src.index(f'os.environ.get("{BUDGET_ENV}"')
        assert read_idx > src.index("def main("), (
            "navigation_scraper: budget env must be read inside main() "
            "(beside the SCRAPER_LISTING_URL gate), not at import time"
        )

    def test_clamped_reserve_math(self):
        """reserve = min(max(600, 0.3*budget), 0.5*budget) — pinned at BOTH
        budget values and property-swept: never negative, discovery deadline
        never already-expired (budget - reserve > 0), extraction gets the
        remainder."""
        for path in (NAV, HTTP):
            src = _read(path)
            ns: dict = {"__name__": "t"}
            exec(_grab(src, "_exec_budget_deadlines"), ns)  # noqa: S102
            fn = ns["_exec_budget_deadlines"]
            # Pinned values from the plan (B4 fix): 3000 → 900/2100, 540 → 270/270.
            d3000 = fn(3000, 0.0)
            assert d3000 == (2100.0, 3000.0), d3000
            d540 = fn(540, 0.0)
            assert d540 == (270.0, 540.0), d540
            # Property sweep: reserve in (0, budget/2], discovery window > 0.
            for budget in range(101, 20000, 7):
                disc, ext = fn(budget, 0.0)
                assert 0 < disc < ext <= budget, (path, budget, disc, ext)
            # Reserve formula is literally the clamped one (source pin).
            assert re.search(
                r"reserve\s*=\s*min\(\s*max\(\s*600,\s*int\(0\.3\s*\*\s*budget\)\s*\),"
                r"\s*int\(0\.5\s*\*\s*budget\)\s*\)",
                src,
            ), f"{os.path.basename(path)}: reserve formula not the clamped one"

    def test_stop_reason_not_in_gate_sets(self):
        """``exec_time_budget`` is a BOUNDED verdict, not a failure: it must
        sit in neither the coverage-fail set nor the access-wall set."""
        sys.path.insert(0, os.path.join(ROOT, "webapp"))
        try:
            from webapp.agents.nodes.route_after_testing import (  # noqa: E402
                _ACCESS_WALL_STOP_REASONS,
                _COVERAGE_FAIL_STOP_REASONS,
            )

            assert STOP_REASON not in _COVERAGE_FAIL_STOP_REASONS
            assert STOP_REASON not in _ACCESS_WALL_STOP_REASONS
        finally:
            sys.path.remove(os.path.join(ROOT, "webapp"))


class TestTemplateDeadlineChecks:
    def test_nav_walk_loops_honor_discovery_deadline(self):
        """Both walk loops break with the bounded stop reason beside the
        max_pages/limit checks."""
        src = _read(NAV)
        for fn in ("_discover_urls_via_search", "_discover_urls_via_category"):
            body = _grab(src, fn)
            assert "deadline" in body, f"{fn}: no deadline parameter"
            assert "EXEC_BUDGET_STOP" in body, (
                f"{fn}: walk loop does not record the bounded stop reason"
            )

    def test_nav_dimension_loop_checks_before_rotation(self):
        """The CATEGORY_URLS dimension loop checks the budget BEFORE the
        browser rotation (no wasted browser spin on a spent budget)."""
        src = _read(NAV)
        main_idx = src.index("def main(")
        check = src.find("EXEC_BUDGET_STOP", main_idx)
        rotation = src.find("ROTATE_EVERY", main_idx)
        assert check != -1 and rotation != -1
        assert check < rotation, (
            "navigation_scraper: dimension-loop budget check must come "
            "before the browser rotation point"
        )

    def test_nav_extraction_stops_between_items(self):
        """The extraction loop checks the extraction deadline BETWEEN items —
        finalize through the normal output path, never mid-item."""
        src = _read(NAV)
        main_idx = src.index("def main(")
        extraction = src.index("Phase 2", main_idx)
        region = src[extraction:]
        assert "extraction_deadline" in region, (
            "navigation_scraper: extraction loop has no budget stop"
        )

    def test_http_phase1b_honors_budget(self):
        """http_nav's CATEGORY_URLS loop already bounds itself via
        DISCOVERY_DEADLINE_SECONDS — the exec budget must be honored there
        too (min of both)."""
        src = _read(HTTP)
        p1b = src.index("Phase 1b")
        end = src.index("Phase 1 complete", p1b)
        region = src[p1b:end]
        assert BUDGET_ENV in src or "exec" in region.lower()
        assert "min(" in region or BUDGET_ENV in src, (
            "http_navigation_scraper: Phase 1b loop ignores the exec budget"
        )

    def test_http_extraction_stops_between_completions(self):
        src = _read(HTTP)
        pool = src.index("as_completed")
        region = src[pool:]
        assert "extraction_deadline" in region, (
            "http_navigation_scraper: thread-pool loop has no budget stop"
        )


class TestTemplateBoundedHonesty:
    def test_bounded_flag_recorded(self):
        """metadata.discovery_coverage records time_budget_bounded so the
        note-delivery and the verdict gate can see a bounded run."""
        for path in (NAV, HTTP):
            src = _read(path)
            assert BOUNDED_KEY in src, f"{os.path.basename(path)}: bounded flag missing"

    def test_found_stays_post_filter_count(self):
        """``found`` is NOT repurposed — it stays the post-filter extracted
        count so _execution_shortfall_reason never arms on a bounded run."""
        for path in (NAV, HTTP):
            src = _read(path)
            assert re.search(r'"found":\s*len\(items\)', src), (
                f"{os.path.basename(path)}: found repurposed"
            )


# ────────────────────────────────────────────────────────────────────────────
# N2 — templates: periodic flush (glob-safe, full shape, atomic final write)
# ────────────────────────────────────────────────────────────────────────────


class TestTemplateFlush:
    def test_output_filename_start_of_run(self):
        """The output filename is computed BEFORE the extraction loop so the
        flush and the final write share one name (single-file contract, job-71
        collision pin). The %f+pid expression must survive the move."""
        for path in (NAV, HTTP):
            src = _read(path)
            main_idx = src.index("def main(")
            fname = src.find("output_filename", main_idx)
            phase2 = src.find("Phase 2: Extracting", main_idx)
            assert fname != -1 and phase2 != -1
            assert fname < phase2, (
                f"{os.path.basename(path)}: output filename computed after the "
                "extraction loop — the flush has no target"
            )
            # Collision pin compatibility: subsecond timestamp + pid.
            fname_window = src[max(0, fname - 200) : fname + 300]
            assert re.search(r"strftime\([^)]*%f", fname_window) and "os.getpid()" in fname_window

    def test_flush_atomic_and_glob_safe(self):
        """Flush writes via ``.json.tmp`` + ``os.replace`` — the tmp name must
        NOT match the runner's output_*.json scan (``endswith('.json')``)."""
        for path in (NAV, HTTP):
            src = _read(path)
            assert ".json.tmp" in src or '.tmp"' in src, (
                f"{os.path.basename(path)}: flush not via a .tmp side name"
            )
            assert "os.replace" in src, (
                f"{os.path.basename(path)}: flush not atomic (os.replace missing)"
            )

    def test_flush_interval_25(self):
        for path in (NAV, HTTP):
            src = _read(path)
            assert re.search(r"=\s*25\b", src), (
                f"{os.path.basename(path)}: flush interval not 25"
            )

    def test_flush_carries_full_output_shape(self):
        """A harvested flush must stay gate-readable: the flushed dict includes
        metadata.discovery_coverage (run_execution._read_discovery_coverage)."""
        for path in (NAV, HTTP):
            src = _read(path)
            flush_idx = src.find("os.replace(tmp_path")
            assert flush_idx != -1
            region = src[:flush_idx]
            # The output-shape builder the flush uses must include coverage.
            assert '"discovery_coverage"' in region, (
                f"{os.path.basename(path)}: flush shape lacks discovery_coverage"
            )

    def test_final_write_reuses_atomic_path(self):
        """The FINAL write goes through the same tmp+replace — a SIGKILL mid
        final write cannot leave a truncated file that a later run harvests."""
        for path in (NAV, HTTP):
            src = _read(path)
            assert src.count("os.replace") >= 2, (
                f"{os.path.basename(path)}: final write not atomic"
            )


# ────────────────────────────────────────────────────────────────────────────
# N1 — run_execution: budget injection at ALL THREE dispatch sites
# ────────────────────────────────────────────────────────────────────────────


class TestRunExecutionBudgetInjection:
    def test_settings_default_3000(self):
        src = _read(SETTINGS)
        assert re.search(
            r'EXECUTION_ITEM_TIME_BUDGET\s*=\s*config\(\s*"EXECUTION_ITEM_TIME_BUDGET",'
            r"\s*default=3000",
            src,
        ), "settings: EXECUTION_ITEM_TIME_BUDGET missing (default 3000)"

    def test_env_helper_exists(self):
        src = _read(RUN_EXEC)
        ns: dict = {"__name__": "t"}
        # The helper needs django settings; stub the getattr chain instead.
        helper = _grab(src, "_exec_budget_env")
        assert helper
        assert "SCRAPER_LISTING_URL" not in helper  # orthogonal envs stay separate

    def test_explicit_both_directions_shape(self):
        """The SCRAPER_CHECKPOINT_REUSE shape: absent/disabled ⇒ {} (an
        explicit disarm), never an ambient inherited value."""
        src = _read(RUN_EXEC)
        m = re.search(r"def _exec_budget_env\(.*?\n(?:    .*\n|\n)*", src)
        assert m
        body = m.group(0)
        assert "return {}" in body, (
            "_exec_budget_env must return {} (explicit disarm) when disabled"
        )

    def test_injected_at_bs_site(self):
        """Three distinct call sites: bs payload, in-process Popen, category
        legs (scaled — pinned separately below)."""
        src = _read(RUN_EXEC)
        n = len(re.findall(r"_exec_budget_env\(", src))
        assert n >= 3, f"budget env injected at {n} sites, expected >= 3"
        # The bs payload merge is at the main /scrape post.
        bs = src.index('f"{service_url}/scrape"')
        payload = src[bs : bs + 900]
        assert "_exec_budget_env" in payload, (
            "browser_service /scrape payload does not carry the budget env"
        )

    def test_injected_at_inprocess_site(self):
        src = _read(RUN_EXEC)
        ip = src.index("subprocess.Popen(")
        region = src[ip : ip + 2500]
        assert "_exec_budget_env" in region, (
            "in-process Popen env does not carry the budget env"
        )

    def test_injected_scaled_at_category_legs(self):
        """Category legs get min(budget, leg_timeout - 60) — the 600s leg wall
        minus a 60s finalize margin (critique-1 B3 / critique-2 B4)."""
        src = _read(RUN_EXEC)
        m = re.search(r"_exec_budget_env\(\s*min\(", src)
        assert m, "category legs: budget not scaled"
        seg = src[m.start() : m.start() + 200]
        assert "60" in seg, f"category legs: no 60s finalize margin in {seg[:120]!r}"

    def test_probe_lane_clean(self):
        """graph.py composes the probe env itself — the budget must NEVER
        reach the probe lane (probes are discovery, not execution)."""
        src = _read(GRAPH)
        assert BUDGET_ENV not in src and "_exec_budget_env" not in src

    def test_budget_never_forces_a_cli_limit(self):
        """MUST-NOT: no --limit ladder at execution — the budget travels by
        env only (immune to _filter_supported_args). The helper must never
        append a CLI arg."""
        src = _read(RUN_EXEC)
        body = _grab(src, "_exec_budget_env")
        assert "args.append" not in body and "args.extend" not in body


class TestNoteDelivery:
    def test_note_helper_follows_house_pattern(self):
        """run-scope note mechanism (none existed): ORM append with
        update_fields=['notes'] and exception-swallow, per the graph.py
        PDP-flip pattern."""
        src = _read(RUN_EXEC)
        assert "def _append_execution_note(" in src
        helper = _grab(src, "_append_execution_note")
        assert 'update_fields=["notes"]' in helper
        assert "except Exception" in helper, "note writes must never raise"

    def test_bounded_coverage_reaches_notes(self):
        src = _read(RUN_EXEC)
        assert BOUNDED_KEY in src, (
            "run_execution never consumes the bounded flag — the budget trim "
            "would be invisible in job notes"
        )
        assert "EXEC BUDGET" in src, "bounded note text missing"

    def test_partial_harvest_note(self):
        src = _read(RUN_EXEC)
        assert "PARTIAL HARVEST" in src
        assert "raw row count" in src, "partial-harvest note must say the count is RAW"


# ────────────────────────────────────────────────────────────────────────────
# N2 — run_execution: harvest on both lanes, rc==3 priority, honesty
# ────────────────────────────────────────────────────────────────────────────


class TestHarvestBsLane:
    def test_trigger_key_exact(self):
        """Harvest fires when timed_out OR (rc≠0 AND fresh output_content).
        rc≠0 with NO output keeps today's generic FAILED shape."""
        src = _read(RUN_EXEC)
        bs_fail = src.index('if result.get("returncode", 0) != 0:')
        region = src[bs_fail : bs_fail + 6000]
        assert 'result.get("timed_out")' in region, "trigger: timed_out key unread"
        assert 'result.get("output_content")' in region, "trigger: content unread"

    def test_rc3_discovery_zero_keeps_priority(self):
        """rc==3/DISCOVERY_ZERO branch (tags no_fresh_output + zero_discovery_exit,
        pinned by test_job83_job88_classes) fires BEFORE the harvest arm — a
        flushed file on a zero-discovery exit changes nothing."""
        src = _read(RUN_EXEC)
        bs_fail = src.index('if result.get("returncode", 0) != 0:')
        region = src[bs_fail : bs_fail + 6000]
        dz = region.find("DISCOVERY_ZERO")
        harv = region.find("timed_out")
        assert dz != -1 and harv != -1
        assert dz < harv, "harvest arm must sit AFTER the DISCOVERY_ZERO branch"

    def test_harvest_flows_through_persist_and_coverage(self):
        """B2b: the harvest arm persists output_file and reads
        discovery_coverage BEFORE the FAILED return (today the rc≠0 branch
        returns before the persist block — rows lost)."""
        src = _read(RUN_EXEC)
        bs_fail = src.index('if result.get("returncode", 0) != 0:')
        region = src[bs_fail : bs_fail + 6000]
        assert "_read_discovery_coverage" in region, (
            "harvest arm skips the coverage read"
        )
        assert "PARTIAL HARVEST" in region

    def test_harvested_status_is_failed_never_completed(self):
        """C4 belt: a harvested run is an honest FAILED with rows — never a
        COMPLETED (which would bypass prune/gates/classification)."""
        src = _read(RUN_EXEC)
        bs_fail = src.index('if result.get("returncode", 0) != 0:')
        region = src[bs_fail : bs_fail + 6000]
        arm = region[: region.find('# Persist the returned output content')]
        assert '"COMPLETED"' not in arm


class TestHarvestInProcessLane:
    def test_stall_return_harvests(self):
        """B2a: the stall kill (the DEFAULT lane for the HTTP family) reads
        the floored F8 finder — rows flushed before the kill survive."""
        src = _read(RUN_EXEC)
        stall = src.index("if stall_reason:")
        region = src[stall : stall + 2500]
        assert "_inprocess_partial_harvest" in region, (
            "stall return loses flushed rows"
        )
        helper = _grab(src, "_inprocess_partial_harvest")
        assert "mtime_floor=start" in helper, "stall harvest must be floored"

    def test_rc0_crash_return_harvests(self):
        src = _read(RUN_EXEC)
        rc0 = src.index("if returncode != 0:")
        dz = src.find("DISCOVERY_ZERO", rc0)
        gen = src.index("Scraper exited with code", dz)
        region = src[dz : dz + 3500]
        assert "_find_newest_output" in region, "generic rc≠0 return loses flushed rows"

    def test_rc3_priority_before_harvest_inprocess(self):
        src = _read(RUN_EXEC)
        rc0 = src.index("if returncode != 0:")
        dz = src.find("DISCOVERY_ZERO", rc0)
        harv = src.find("_find_newest_output", rc0)
        assert dz != -1 and harv != -1 and dz < harv

    def test_harvested_status_is_failed_inprocess(self):
        src = _read(RUN_EXEC)
        stall = src.index("if stall_reason:")
        region = src[stall : stall + 2500]
        arm = region[: region.index("returncode != 0")] if "returncode != 0" in region else region
        assert '"execution_status": "FAILED"' in arm


class TestLane3Merge:
    def test_timed_out_leg_content_still_merges(self):
        """Lane-3 merge pin: category-leg merge is CONTENT-gated, not
        returncode-gated — a timed-out leg's flushed rows merge exactly like
        today's failed-leg rows (B3's env change must not alter this)."""
        src = _read(RUN_EXEC)
        leg = src.index('"timeout": 600')
        region = src[leg : leg + 3000]
        assert 'cat_result.get("output_content")' in region
        assert "if cat_content:" in region


class TestBudgetWallMarker:
    def test_runner_timeout_dict_marked(self):
        src = _read(RUNNER)
        to = src.index('"Scraper timed out after %ds')
        region = src[to : to + 2200]
        assert MARKER in region, "runner timeout dict lacks the budget-wall marker"

    def test_run_execution_marked_on_timeout_paths(self):
        src = _read(RUN_EXEC)
        assert MARKER in src, "in-process timeout path lacks the budget-wall marker"


class TestRunnerHarvest:
    def _runner_ns(self):
        src = _read(RUNNER)
        ns: dict = {"__name__": "t"}
        ns["json"] = __import__("json")
        ns["os"] = os
        ns["logging"] = __import__("logging")
        ns["logger"] = __import__("logging").getLogger("t")
        for fn in ("_find_output_file", "_count_products", "_harvest_partial_output"):
            exec(_grab(src, fn), ns)  # noqa: S102
        return ns

    def test_harvest_fresh_output(self, tmp_path):
        ns = self._runner_ns()
        p = tmp_path / "output_2026-10-14_010203_000001_123.json"
        p.write_text('{"products": [{"title": "a"}, {"title": "b"}]}', encoding="utf-8")
        out = ns["_harvest_partial_output"](str(tmp_path), 0.0)
        assert out is not None
        assert out["output_name"] == p.name
        assert out["product_count"] == 2
        assert '"title": "a"' in out["output_content"]

    def test_harvest_none_when_no_output(self, tmp_path):
        ns = self._runner_ns()
        assert ns["_harvest_partial_output"](str(tmp_path), 0.0) is None

    def test_harvest_floored_to_attempt_start(self, tmp_path):
        """A pre-attempt file (tester output, prior retry) is never credited."""
        import time as _time

        ns = self._runner_ns()
        p = tmp_path / "output_stale.json"
        p.write_text('{"products": [{"title": "old"}]}', encoding="utf-8")
        old = _time.time() - 3600
        os.utime(p, (old, old))
        assert ns["_harvest_partial_output"](str(tmp_path), _time.time() - 60) is None

    def test_harvest_ignores_tmp_sidecar(self, tmp_path):
        """A mid-flush .json.tmp sidecar must never be harvested (and the
        runner's own scan filters it by suffix)."""
        ns = self._runner_ns()
        (tmp_path / "output_x.json.tmp").write_text('{"products": [{"title": "p"}]}')
        assert ns["_harvest_partial_output"](str(tmp_path), 0.0) is None
        assert ns["_find_output_file"](str(tmp_path)) == ""

    def test_timeout_dict_embeds_harvest(self):
        """Source pin: the timeout catch returns timed_out=True and embeds the
        harvest (output_content) instead of the unconditional hard-zero."""
        src = _read(RUNNER)
        to = src.index('"Scraper timed out after %ds')
        region = src[to : to + 2600]
        assert '"timed_out": True' in region
        assert "_harvest_partial_output" in region
        assert MARKER in region

    def test_hard_zero_unchanged_when_no_harvest(self):
        """No fresh output ⇒ today's hard-zero dict shape (fields intact)."""
        src = _read(RUNNER)
        to = src.index('"Scraper timed out after %ds')
        region = src[to : to + 2600]
        for field in ('"returncode": -1', '"stdout": ""', '"product_count": 0'):
            assert field in region, f"hard-zero field {field} lost"
