# Wave-40 Prod-RCA Fixes Implementation Plan (rev 2 — post-critique)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the eight defect classes behind all 37 failed prod jobs in window 753–815 (2026-09-20/21): intake accepting empty url_list, converging runs killed by writer aborts, mid-run workspace deletion, draft call-signature crashes, test→execution discovery variance, dead cross-node state channels, the `park_browser_unavailable` KeyError, multi-line `search_criteria` mangling, and the unauthenticated `approval_inline` endpoint.

**Architecture:** Six parallel deep-planning agents verified every defect against the current tree (`file:line`) and designed minimal, kill-switched, TDD-able fixes; **five adversarial critique agents then re-verified the plan itself** (fact-check, behavior-activation audit, test-reality validation, prod-replay validation, scope/residuals audit) and this rev-2 incorporates every confirmed finding. Three structural insights drive the design: (1) LangGraph **silently strips undeclared keys** from node returns, so six shipped safety nets were behavior-dead — declare the channels and harden their reset semantics, don't rewrite the arms; (2) the real-items invariant is evaluated **once at finalize** ("did this JOB produce items") rather than per-routing-arm — ~20 terminal sites exist and per-arm wiring is how jobs 770/762 escaped; (3) checkpoint reuse is armed **entirely on the `run_execution` side** — the rev-1 design put the arming in `browser_service/scraper_runner.py`, which the replay audit proved was dead code (`--fresh-discovery` is appended unconditionally for phase-1 modes).

**Prod-replay verdict (critique agent 4, against the real artifacts of all 37 jobs):** this plan as written would have prevented **~30 of 37 (~81%)** — 21/21 intake instant-fails (T5), ≥5/7 writer-abort deaths rescued at finalize with 770/765 verified directly against their on-disk outputs (T8/T9), 807 restored (T10), all 3 signature crashes rejected at write time (T6/T7), 769 rescued by checkpoint reuse (T13–T15). 763 nastygal is genuinely unsavable (residual R1). The honest singles (754/755/761, 782 captcha, 790 cancelled) are correct verdicts, not misses.

**Tech Stack:** Django 5 + LangGraph 1.2.11 (TypedDict state, conditional edges), Celery, pytest (django_db), ruff (E4/E7/E9/F/I/UP).

**Spec/Evidence:** Prod RCA (memory `prod-failure-window-sept20-21-rca`, 6 clusters, job ids 753–815) + six planning-agent reports + five critique-agent reports (2026-09-21). Every defect claim and every test seam below was re-verified against the tree by those agents; where a detail must be read at implement time it is marked ADAPT (adapt the invocation, never the assertion).

### Rev-2 changes vs rev-1 (from the critique round)

1. **T5 pivots to repair-on-coercion.** Reject-only would have 422'd job 758's *working* COMPLETED flow: W37-NEW-C coerces a PDP submission to `url_list` and the run is valid **if the coerced URL is seeded**. Rev-2 seeds it and rejects only when genuinely nothing exists. Surface coverage widened to every url_list producer: intake create, `site_scrape` (views.py:1721), the partner-API writer (`item_urls_deduped` predicate), and `manage.py scrape`.
2. **T13–T15 arming moves entirely into `run_execution`** (`_run_in_process` env dict, the `--fresh-discovery` append gate at :883-884, the staging tuple at :1625-1634). **Zero `browser_service/` changes** — this kills the rev-1 contradiction (the runner-side env arm could never fire because `--fresh-discovery` forces re-discovery) and avoids the browser-service import trap and deploy skew entirely.
3. **T11 rescoped to the cross-job class** with the corrected liveness protocol: register at TASK START (covers soft-limit deaths), read-and-clear the job's own entry inside `_maybe_delete_workspace` (unregister runs in the outermost finally *after* finalize, so it cannot be relied on at delete time), `alive_for_slug` must NOT exclude self (the 765 zombie shares the job_id). The other wipe sites (`check_tracker.py:56/:76`, `setup_workspace.py:96`) join the guard; `_trash` becomes a guarded namespace; the Railway-ephemeral (container-restart) deletion class is documented as residual R9, not claimed fixed.
4. **T12's progress gate re-signalled** to the tester-stamped `last_tested_draft_fp` (state.py:117, graph.py:8710): `tested_draft_sha256` is only written at execution launch, so it is empty on exactly the runs (765) this task targets — rev-1's gate could never fire on its own motivation job. Window clamp goes through the real `_effective_timeout` helper, not a phantom `job_budget_remaining` key. T1's fast-fail ×2 arm gets its own gate (`SCRAPER_FAST_FAIL`, default off) so declaring channels doesn't silently activate a never-live terminalizer.
5. **T9 loses the `_finalize_job(job_id, final_state=...)` signature change** (its real surface is `_finalize_job(job: ScrapeJob)`, state read from the checkpoint at tasks.py:1666-1674; tests freeze the read instead). Draft promotion is **compile-gated** with `-good`-twin fallback — rev-1 would have promoted 762's crashing draft to production.
6. **T1 hardened with activation semantics:** every newly-live channel gets reset/clear rules (stale `browser_unavailable_detail` must not park a job that has a real report — park-loop guard; `no_fresh_output` resets on fresh-output returns; `tester_wall_clock_timeouts` resets at the park early-return :8613).
7. **Every test snippet re-derived against the real tree.** Removed phantom surfaces: `tasks.PROJECT_ROOT` (does not exist — patch `django.conf.settings.PROJECT_ROOT`), `ScrapeJob.site_slug` (no such field — slug is `_generate_slug(job.url)`), `_gate_check`/`_fake_state`, `make_http_fetcher`, `_login_staff`/`_intake_payload`, `job_budget_remaining`, import-based registry helpers. Module-shadowing idioms (`agents.nodes.__init__` re-exports `route_after_testing` and `run_execution` functions over their submodules) are baked into the tests.
8. **763 reclassified** from "covered by T13–T15" to accepted residual; the 770 narrative corrected (its 19-product output was moved to `scrapers/` by 770's own cleanup agent, not by finalize's publish block); the 758 causality corrected (intake coercion seeded before any probe; the multi-line string broke the listing *probe* only — T4 is a probe-quality fix and 758 a near-miss).

## Defect → Task Coverage Matrix

| # | Defect (prod jobs) | Root cause (verified) | Tasks | Replay verdict |
|---|---|---|---|---|
| D1 | 21 instant fails: url_list with NO URL list (775–780, 784, 789, 793–796, 799, 802–804, 806, 811–814) | W37-NEW-C coerces PDP→`url_list` but nothing seeds or validates URLs; seeding branch keys off pre-coercion `nav_method` labels | T5 | **21/21 prevented** — coerced PDPs seeded, truly-empty submissions 422'd at intake |
| D2 | 7 writer no-progress aborts killing converging runs (764, 765, 768, 770, 771, 801, 773); 770 failed n=0 with a **19-product output on disk**; 762 had 4–17 | Rescue consulted only ~6 of ~20 terminal sites, workspace-only scan, attempt-scoped freshness floor | T8, T9, T12 | **≥5/7 rescued at finalize** (770/765 verified: their outputs pass the evidence gate); T12 (opt-in) prevents the abort itself |
| D3 | 2 mid-run workspace deletions (765, 807 — 807 FAILED despite PASS 0.94 + 25 products) | finalize `rmtree` guard URL-scoped/slug-blind; `run_execution` refuses a missing draft with no restore attempt | T10, T11 | **807 prevented** (restore + publish); 765's same-process zombie class guarded; container-restart class = residual R9 |
| D4 | 3 draft call-signature crashes (760 `unexpected kwarg 'post'`, 791 `multiple values for 'fetch_page'`, 762 `re.error`) | Compile + F821 gates check neither helper-call signatures nor regex validity | T6, T7 | **3/3 rejected at write time** |
| D5 | 2 test→execution discovery variance (769 sallybeauty 20 products then DISCOVERY_ZERO; 763 nastygal) | Checkpoint never staged into `/scrape`; `--fresh-discovery` unconditionally appended forces re-discovery | T13, T14, T15 | **769 rescued** (checkpoint reuse armed runner-side); 763 unsavable (residual R1 — dead site, honest verdict) |
| D6 | Dead safety nets: 769's recycle never fired (`no_fresh_output` stripped) | Six state channels written but never declared; LangGraph strips undeclared keys | T1 | Restored + activation-guarded |
| D7 | Browser-service outage during testing → permanent FAILED (KeyError) | `route_after_testing` returns `"park_browser_unavailable"` from 3 arms; edge map lacks the key | T2 | Prevented |
| D8 | (a) job 758 birkenstock COMPLETED n=1 (multi-line criteria mangled the listing probe); (b) anonymous POST can approve ownerless-job approvals (reproduced live) | `urlsplit` deletes newlines → one same-host mangled URL at 5 sites; `approval_inline` missing `@login_required` | T4, T3 | 758 = near-miss closed (probe quality + T5 seeding closes the collapse class); approval hole closed |

## Global Constraints

- **Restart matrix** (after the listed tasks; `docker compose restart <services>`; celery-events/beat are never restarted — they don't import the graph):

  | Task(s) | Restart |
  |---|---|
  | T1 + T2 | **ONE** shared `celery-worker django` after BOTH — do not restart between them (same graph/state files) |
  | T3 | django |
  | T4, T7, T10, T11, T12 | celery-worker django |
  | T5 | django |
  | T6, T8, T9 | celery-worker |
  | T13 | celery-worker |
  | T14 | celery-worker **and** browser_service IF templates are baked into the image — check `docker-compose*.yml` for a `templates/` bind mount first; if bind-mounted, no restart needed on the browser side |
  | T15 | celery-worker django |
  | T16 | one final full `celery-worker django` |

  This plan **touches no `browser_service/` file** (deliberate, rev-2) — the standing deploy rule (django+celery BEFORE browser-service) still governs the eventual PR.
- **Test invocation:** `docker compose exec django sh -c "cd /app/webapp && pytest ../tests ."` (full suite). While iterating on one task, target just the new/changed files.
- **Lint gate:** `docker compose exec django ruff check webapp/ src/` then `ruff format webapp/ src/` (rules pinned in `ruff.toml`: E4, E7, E9, F, I, UP). Import placement matters for I001 — new `src.` imports in `graph.py` go in its module-level `src` import block (~line 56).
- **Patch idioms for tests (non-negotiable, all verified):**
  - `tasks.py` has **no** `PROJECT_ROOT` attribute — the code reads `os.environ["PROJECT_ROOT"]` and `django.conf.settings.PROJECT_ROOT`; tests patch `django.conf.settings.PROJECT_ROOT` (precedent `tests/test_artifact_copy_guards.py:107-112`).
  - `agents.nodes.__init__.py:14` re-exports the `route_after_testing` and `run_execution` **functions** over their submodules. To get the MODULE: `import agents.nodes.route_after_testing` then `sys.modules["agents.nodes.route_after_testing"]`, or `importlib.import_module("webapp.agents.nodes.run_execution")`.
  - Never `import browser_service.scraper_runner` in django-side tests (its `__init__` drags in fastapi) — irrelevant here since rev-2 touches no browser_service file; stated so nobody "helpfully" adds one.
- **Never credit a COMPLETED with product_count=0** — the rescue ladder (T9) only fires with `rescue_count > 0` AND `product_count == 0`.
- **Prod is READ-ONLY** during development. No drives, no restarts, no Railway mutations. PR creation/merge is the user's action.
- **Kill switches** (all default-ON behavior restored, `0` = off): `SCRAPER_DRAFT_CALL_GATE=0` (T7), `REAL_ITEMS_RESCUE_ENABLED=0` (T8/T9), `FINALIZE_WORKSPACE_DELETE=0` (T11 — **always tombstones when 0; there is no old-behavior mode for the guard itself, rollback = revert**). Opt-in (default OFF): `WRITER_PROGRESS_ESCALATION` (T12), `SCRAPER_FAST_FAIL` (T1's ×2 arm), `SCRAPER_CHECKPOINT_REUSE` (T13–T15, armed per-job by run_execution, never global). T2/T3/T4/T5/T10 intentionally have NO switch: they restore arms already written and budget-bounded, or close a security hole.
- Pinned env literals: `SCRAPER_CHECKPOINT_MIN_URLS`, `SCRAPER_CHECKPOINT_MAX_AGE_S` (T13; checkpoint max age default **86400** — 1 day, not the rev-1 604800).
- **Commit after each task**, message prefix `feat(wave-40):` / `test(wave-40):` / `fix(wave-40):`. Branch: current feature branch off `file-master-artifacts` — never commit to main directly.
- Never commit: `docker-compose.override.yml`, `config/proxy.json`, `out.json`, session transcripts, scraper outputs.
- **Deploy order** (when this wave eventually ships): django + celery-worker BEFORE browser-service (browser-service is untouched here, the rule still governs the PR).

---

# Phase 1 — Graph safety nets (T1–T4, one shared restart for T1+T2)

### Task 1: Declare the six dead cross-node state channels + activation guards

**Files:**
- Modify: `webapp/agents/state.py` (Retry counters block, after `forced_retest_count`, ~line 124)
- Modify: `webapp/agents/graph.py` + `webapp/agents/nodes/run_execution.py` (reset/clear semantics — exact sites below)
- Modify: `webapp/agents/nodes/route_after_testing.py` (fast-fail ×2 arm gate)
- Test: `tests/test_wave40_state_channel_contract.py` (new)

**Interfaces:**
- Consumes: nothing — declarations + reset semantics only.
- Produces: six declared `ScrapeState` channels, plain LastValue (overwrite) semantics: `tester_wall_clock_timeouts: int`, `fast_fail_detail: str`, `browser_unavailable_detail: str`, `draft_absent_count: int`, `last_tested_draft_bytes: int`, `no_fresh_output: bool`. Later tasks rely on these surviving node returns (T2's pre-flight park lane, T9's finalize evidence, T12's `last_tested_draft_fp` gate signal).

Verified defect (planning agent E, reproduced live in-container): langgraph strips undeclared `TypedDict(total=False)` keys from node returns — a node returning `{"ghost": 1}` leaves nothing in state, no error. Writers/readers all exist and are wired; the channels were never declared:

| channel | writer | reader | net that was off |
|---|---|---|---|
| `tester_wall_clock_timeouts` | `graph.py:8822` (reset-to-0 is load-bearing), `:8824-8830` | `route_after_testing.py:1336`, `:1512`; `graph.py:8582` | forced-retest guard + ×2 escalation |
| `fast_fail_detail` | `graph.py:8795` | `route_after_testing.py:1492-1498` | wave-22 A5 named fast-fail |
| `browser_unavailable_detail` | `graph.py:8613`; `nodes/run_execution.py:1558/1671/1734` | `route_after_testing.py:1467-1468`; `graph.py:5081` | entire tester-side dependency park |
| `draft_absent_count` | increment `graph.py:8506`, stamp `:8645` | `route_after_testing.py:2136-2161` | T1.4 absent-draft bound (was an **unbounded** regen loop) |
| `last_tested_draft_bytes` | `graph.py:8714-8718` | `graph.py:6261` (`_log_bloat_tripwire`) | wave-32 D4 `[DRAFT-BLOAT]` row |
| `no_fresh_output` | `nodes/run_execution.py:1440/1474/1721` | `graph.py:5367` (`_zero_discovery_failed` → recycle at `:5369-5372`) | job-769 strategy recycle |

**Activation guards (critique agent 2 — declaring a channel activates its reader; each reader needs a reset rule so stale values can't misfire):**

1. **Park-loop guard:** the pre-flight park arm (`route_after_testing.py:1484`) consults `browser_unavailable_detail` **only when `not test_report`** — a stale detail from an earlier attempt must never park a job that has a real report (that would be an infinite park loop).
2. **Clear-on-success:** `browser_unavailable_detail` is reset to `""` at the tester success stamp (`graph.py:8645`) and on every run_execution return that is NOT an infra-unavailable return (`:1558/:1671/:1734` keep stamping it; all other returns clear it).
3. **`no_fresh_output` reset:** run_execution's fresh-output returns stamp `no_fresh_output: False` explicitly (only the zero-output paths at `:1440/:1474/:1721` stamp True).
4. **`tester_wall_clock_timeouts` reset at park:** the park early-return (`graph.py:8613`) resets the counter to 0 so a beat-resumed generation doesn't inherit the dead generation's strike count.
5. **Fast-fail ×2 gate:** the ×2 escalation arm (`route_after_testing.py:1492-1498`) was never live; activating it silently as a side effect of a declaration is exactly the kind of unreviewed behavior change the critique flagged. Gate it behind `SCRAPER_FAST_FAIL` (default `0` = off) via a tiny `_fast_fail_escalation_enabled() -> bool` helper in the same module.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wave40_state_channel_contract.py
"""[wave-40 T1] Cross-node channels must be DECLARED in ScrapeState.

langgraph strips undeclared keys from node returns (verified live: a node
returning {"ghost": 1} leaves the key out of state, no error). Six shipped
channels were undeclared, so their safety nets were silently off. Pins the six
keys AND the round-trip: declared + last-write-wins (never operator.add).
Prod evidence: job 769 re-ran discovery, rc=3 DISCOVERY_ZERO, no_fresh_output
was stripped, the recycle at graph.py:5369 never fired, job FAILED n=0."""

import re
import sys

import pytest
from langgraph.graph import StateGraph, START, END

from agents.state import ScrapeState

CHANNELS = {                       # key -> the value a writer stamps
    "tester_wall_clock_timeouts": 1,
    "fast_fail_detail": "code_tester hit its wall clock",
    "browser_unavailable_detail": "waited 120s for /health",
    "draft_absent_count": 1,
    "last_tested_draft_bytes": 1024,
    "no_fresh_output": True,
}


def _stale(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    return "stale " + value


def test_six_channels_are_declared():
    for key in CHANNELS:
        assert key in ScrapeState.__annotations__, f"ScrapeState lacks {key}"


def test_no_channel_is_an_accumulating_reducer():
    for key, ann in ScrapeState.__annotations__.items():
        if key in CHANNELS:
            assert "operator.add" not in repr(ann), key
            assert "add_messages" not in repr(ann), key


@pytest.mark.parametrize("key,value", sorted(CHANNELS.items()))
def test_channel_survives_a_node_return_and_overwrites(key, value):
    """A seeded prior value must be REPLACED by the writer's value. Undeclared
    -> stripped (reads None); operator.add -> summed. Both are regressions."""
    seen = {}
    g = StateGraph(ScrapeState)
    g.add_node("seed", lambda _s: {key: _stale(value)})
    g.add_node("writer", lambda _s: {key: value})
    g.add_node("reader", lambda s: seen.update(s) or {})
    g.add_edge(START, "seed")
    g.add_edge("seed", "writer")
    g.add_edge("writer", "reader")
    g.add_edge("reader", END)
    g.compile().invoke({})
    assert seen.get(key) == value, f"{key} stripped or merged instead of overwritten"
```

Then the behavior half — job 769's recycle must now actually route (helpers `_zero_state`/`_goto` copied from `tests/test_job65_execution_recycle.py` — copy the definitions, do not import across test files; note `scraper_analysis` is REQUIRED: the recycle consults the strategy):

```python
# appended to tests/test_wave40_state_channel_contract.py
def _zero_state(**overrides):
    st = {
        "job_id": 0, "url": "https://x.example/", "site_slug": "x",
        "input_mode": "navigation", "test_retry_count": 0,
        "execution_recycle_count": 0, "execution_status": "", "product_count": 0,
        "scraper_analysis": {"strategy": "http_requests"},
    }
    st.update(overrides)
    return st


def test_769_discovery_zero_recycles_instead_of_cleanup():
    # run_execution's exact rc=3 update shape (run_execution.py:1432-1450)
    from agents.graph import _route_after_execution
    res = _route_after_execution(_zero_state(
        execution_status="FAILED", product_count=0, no_fresh_output=True,
        discovery_coverage={"ran_phase1": True, "discovered_urls": 0,
                            "stop_reason": "empty_first_page"}))
    goto = res.goto if hasattr(res, "goto") else res.get("goto", "")
    assert goto == "scraper_analyzer"        # was: cleanup (dead channel)
    assert res.update["execution_recycle_count"] == 1


def test_no_fresh_output_absent_still_cleans_up_crashes():
    from agents.graph import _route_after_execution
    res = _route_after_execution(_zero_state(
        execution_status="FAILED", product_count=0, discovery_coverage={}))
    goto = res.goto if hasattr(res, "goto") else res.get("goto", "")
    assert goto == "cleanup"
```

And the fast-fail gate (module-binding idiom — `agents.nodes.__init__` shadows the submodule with the function):

```python
# appended to tests/test_wave40_state_channel_contract.py
def _rat_mod():
    # agents.nodes.__init__ re-exports the route_after_testing FUNCTION over
    # the submodule — attribute access yields a function, not the module.
    import agents.nodes.route_after_testing  # noqa: F401
    return sys.modules["agents.nodes.route_after_testing"]


def test_fast_fail_double_strike_defaults_off(monkeypatch):
    rat = _rat_mod()
    monkeypatch.delenv("SCRAPER_FAST_FAIL", raising=False)
    assert rat._fast_fail_escalation_enabled() is False
    monkeypatch.setenv("SCRAPER_FAST_FAIL", "1")
    assert rat._fast_fail_escalation_enabled() is True
```

*(If `_route_after_execution`'s import surface differs at implement time — it is module-level in `agents/graph.py` — adjust the import, not the assertions.)*

- [ ] **Step 2: Run tests, verify RED**

Run: `docker compose exec django sh -c "cd /app/webapp && pytest ../tests/test_wave40_state_channel_contract.py -q"`
Expected: `test_six_channels_are_declared` FAILS ("ScrapeState lacks no_fresh_output"); the round-trips FAIL (stripped); `test_769_...` FAILS (goto == cleanup); the fast-fail test FAILS (helper absent).

- [ ] **Step 3: Declare the channels**

`webapp/agents/state.py`, in the Retry counters block after `forced_retest_count`:

```python
    # [wave-40 T1] Tester-side accounting channels. UNDECLARED keys are
    # silently stripped from node returns by langgraph, so every one of these
    # was behavior-dead: writers stamped them, readers always read 0/""/False.
    # Plain (LastValue) channels — each writer OVERWRITES; none may use
    # operator.add (graph.py's reset-to-0 for tester_wall_clock_timeouts is
    # load-bearing). Activation guards live at the writer sites — see
    # tests/test_wave40_state_channel_contract.py.
    tester_wall_clock_timeouts: int
    fast_fail_detail: str
    browser_unavailable_detail: str
    draft_absent_count: int
    last_tested_draft_bytes: int
    no_fresh_output: bool
```

- [ ] **Step 4: Apply the five activation guards** — implement each per the numbered list above (park arm keyed on `not report`; clear-on-success at `graph.py:8645` + non-infra run_execution returns; `no_fresh_output: False` on fresh-output returns; counter reset at the park early-return `:8613`; `_fast_fail_escalation_enabled()` + env consult wrapping the `:1492-1498` arm).
- [ ] **Step 5: Run tests, verify GREEN** (same command; also run `../tests/test_job65_execution_recycle.py` to confirm no routing regression, and `../tests/test_wave22_writer_timeout_accounting.py` for the fast-fail arm's neighbors)
- [ ] **Step 6: Commit** — `test(wave-40): declare six dead cross-node state channels + activation guards (T1)`
- **No restart yet — T2 shares it.**

### Task 2: Register `park_browser_unavailable` in the route_after_testing edge map

**Files:**
- Modify: `webapp/agents/graph.py:10528-10543` (the `add_conditional_edges("code_tester", ...)` path map)
- Test: `tests/test_f13_topology.py` (extend — it already has `TestLiveGraph` that builds the real graph)

**Interfaces:**
- Consumes: T1's `browser_unavailable_detail` (the pre-flight park arm at `route_after_testing.py:1484` only fires once that channel survives).
- Produces: every literal `route_after_testing` can return is a registered edge-map key. T9's finalize work does NOT depend on this (KeyError deaths bypass the ladder via `_finalize_job_failed`), but no task may reintroduce an unmapped return.

Verified defect (planning agent E, live graph build + mini StateGraph probe): the map at `graph.py:10528-10543` has exactly 8 keys; `route_after_testing` returns `"park_browser_unavailable"` at `:1484` (pre-flight), `:1902` (`_aw_dest`, access_wall_all_throttled — passes both wrappers' `if dest not in ("cleanup", "human_approval")` short-circuits unchanged), and `:2190` (park_unhealthy + dead gateway). Unmapped return → langgraph 1.2.11 raises `KeyError` out of `invoke` → `_finalize_job_failed` → permanent FAILED that beat's `resume_browser_unavailable_jobs` (`tasks.py:2460`, park-status-only) can never recover. Returning `Command` instead is NOT an option: verified `TypeError: unhashable type: 'dict'` with a path_map. The node itself is registered (`graph.py:10447` → `_park_browser_unavailable` `:5068-5108`, parks and `goto=END`, no static out-edge — same shape as the existing `check_accessibility → park` path at `:5335`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_f13_topology.py`; `re` must already be imported at module top — add it if not)

```python
class TestConditionalEdgeMapCompleteness:
    """[wave-40 T2] Every name a routing fn can return must be a key in the
    conditional-edge path map. Prod: the wave-16/37 park lanes returned
    "park_browser_unavailable", the map lacked it, langgraph raised KeyError
    and the job FAILED instead of parking (never resumed by beat)."""

    def _rat_mod(self):
        # agents.nodes.__init__ re-exports the route_after_testing FUNCTION
        # over the submodule — attribute access would yield a callable.
        import sys
        import agents.nodes.route_after_testing  # noqa: F401
        return sys.modules["agents.nodes.route_after_testing"]

    def _build(self):
        import django, os
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        from agents.graph import build_scrape_graph
        return build_scrape_graph()

    def _ends(self, g, source):
        ends = {}
        for br in getattr(g.builder, "branches", {}).get(source, {}).values():
            ends.update(getattr(br, "ends", None)
                        or getattr(br, "path_map", None) or {})
        return ends

    def test_park_branch_is_registered(self):
        ends = self._ends(self._build(), "code_tester")
        assert ends.get("park_browser_unavailable") == "park_browser_unavailable"

    def test_every_router_return_literal_is_registered(self):
        rat = self._rat_mod()
        with open(rat.__file__, encoding="utf-8") as fh:
            src = fh.read()
        ends = self._ends(self._build(), "code_tester")
        rets = set(re.findall(r'return "([a-z_]+)"', src))
        rets |= {"cleanup", "human_approval", "code_writer"}  # helper-rewritten dests
        assert rets <= set(ends), f"unmapped router destinations: {rets - set(ends)}"

    def test_park_arm_yields_a_registered_destination(self):
        rat = self._rat_mod()
        ends = self._ends(self._build(), "code_tester")
        # job_id 0 keeps _log_cascade off the DB; no report -> the wave-16
        # pre-flight arm is the one under test.
        dest = rat.route_after_testing(
            {"job_id": 0, "test_report": None,
             "browser_unavailable_detail": "waited 120s for /health"})
        assert dest == "park_browser_unavailable"
        assert dest in ends, "router returned an unmapped destination (KeyError)"
```

- [ ] **Step 2: Run, verify RED**

Run: `docker compose exec django sh -c "cd /app/webapp && pytest ../tests/test_f13_topology.py -q"`
Expected: all three new tests FAIL (map lacks the key).

- [ ] **Step 3: Add the map entry** — inside the path map, after the `"cleanup": "cleanup",` line (`graph.py:10543`):

```python
            "cleanup": "cleanup",
            # [wave-16 B3 / wave-37 / wave-40 T2] infra-park lanes: tester
            # pre-flight unhealthy (route_after_testing.py:1484), report-level
            # infra outage, access_wall_all_throttled (:1902) and the
            # park_unhealthy-with-dead-gateway arm (:2190) all return this
            # name. An unmapped return raises KeyError inside langgraph and
            # FAILED the job instead of parking it.
            "park_browser_unavailable": "park_browser_unavailable",
        },
    )
```

- [ ] **Step 4: Run, verify GREEN** — same command, full `test_f13_topology.py`.
- [ ] **Step 5: Commit** — `fix(wave-40): register park_browser_unavailable edge (T2)`
- [ ] **Step 6: Shared restart for T1+T2** — `docker compose restart celery-worker django` (once for both tasks).

### Task 3: `approval_inline` requires login

**Files:**
- Modify: `webapp/scraper/views.py:865` (insert decorator above the def)
- Test: `webapp/tests/test_views.py` (new class after `TestApprovalDetailView`, ~line 223)

**Interfaces:**
- Produces: `approval_inline` behaves like its siblings (`pending_approvals_fragment :898`, `approval_list :918`, `approval_detail :934` — all `@login_required`).

Verified defect (planning agent E, reproduced live against `config.test_settings`): anonymous `POST /jobs/1/approve/1/` on an **ownerless** job (every auto-queued intake job has `user=None`, and `_approval_visible` at `views.py:854-862` grants ownerless jobs to any caller) → `302 /jobs/1/`, approval flipped to `approved`, `resume_scrape_task.delay` dispatched. Logged-in non-owner → 404 (ownership is sound; only authentication is missing). House convention is `@login_required` only — no `@require_POST` anywhere in views.py; do not add one.

Critique fix (test-reality agent): rev-1's "owned job" fixture used `baker.make(ScrapeJob)` — which creates `user=None`, so the owner test was actually testing the ownerless path again. The owned job needs a REAL user.

- [ ] **Step 1: Write the failing tests**

```python
# appended to webapp/tests/test_views.py
from django.conf import settings
from django.test import override_settings
from unittest.mock import patch

NO_AUTO_LOGIN = [m for m in settings.MIDDLEWARE if "DebugAutoLogin" not in m]


@override_settings(MIDDLEWARE=NO_AUTO_LOGIN)
class TestApprovalInlineAuth(TestCase):
    """[wave-40 T3] approval_inline shipped without @login_required:
    _approval_visible hands OWNERLESS jobs to any caller, so an anonymous POST
    resolved an approval and resumed the graph (reproduced: 302 to job page +
    status=approved + resume_scrape_task.delay dispatched)."""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user("owner", password="pw")
        self.orphan_job = baker.make(ScrapeJob, user=None)
        self.orphan = baker.make(Approval, job=self.orphan_job,
                                 approval_type="field_confirm", question="q")
        self.owned_job = baker.make(ScrapeJob, user=self.owner)
        self.owned = baker.make(Approval, job=self.owned_job,
                                approval_type="field_confirm", question="q")

    def _post(self, approval, choice="Approve"):
        return self.client.post(
            reverse("approval_inline",
                    kwargs={"job_id": approval.job_id, "approval_id": approval.id}),
            {"choice": choice})

    def test_anonymous_post_is_redirected_and_resolves_nothing(self):
        resp = self._post(self.orphan)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        self.orphan.refresh_from_db()
        self.assertEqual(self.orphan.status, Approval.STATUS_PENDING)

    def test_anonymous_get_is_redirected_too(self):
        resp = self.client.get(
            reverse("approval_inline",
                    kwargs={"job_id": self.orphan.job_id,
                            "approval_id": self.orphan.id}))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_logged_in_non_owner_gets_404(self):
        self.client.force_login(User.objects.create_user("intruder", password="pw"))
        resp = self._post(self.owned)
        self.assertEqual(resp.status_code, 404)
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.status, Approval.STATUS_PENDING)

    def test_owner_still_resolves_and_resumes(self):
        self.client.force_login(self.owner)
        with patch("scraper.tasks.resume_scrape_task") as task:  # function-level import
            resp = self._post(self.owned)
        self.assertEqual(resp.status_code, 302)
        task.delay.assert_called_once()
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.status, Approval.STATUS_APPROVED)
```

(Match existing imports in the file for `Client`, `reverse`, `baker`, `ScrapeJob`, `Approval`, `User` — they are already imported by neighboring classes; extend only what's missing, e.g. `override_settings`/`patch`.)

- [ ] **Step 2: Run, verify RED**

Run: `docker compose exec django sh -c "cd /app/webapp && pytest tests/test_views.py::TestApprovalInlineAuth -q"`
Expected: `test_anonymous_post...` FAILS (approval flips to approved, 302 to job page not login). The `MIDDLEWARE` override is required — `config.test_settings` sets `DEBUG_AUTO_LOGIN=True`, which would otherwise auto-authenticate every request; Django's `setting_changed` handler rebuilds the chain (verified).

- [ ] **Step 3: Add the decorator** — directly above `def approval_inline(request, job_id, approval_id):` at `views.py:865`:

```python
@login_required
def approval_inline(request, job_id, approval_id):
```

- [ ] **Step 4: Run, verify GREEN** — same command, then full `tests/test_views.py`.
- [ ] **Step 5: Commit** — `fix(wave-40): approval_inline requires login (T3)` — restart django.

### Task 4: Multi-line `search_criteria` → first URL line at every parse site

**Files:**
- Modify: `src/seed_urls.py` (add helper + extend `__all__` at `:33`)
- Modify: `webapp/agents/graph.py:2103`, `:2228`, `:4267`, `:7442` + new import beside `:56`
- Modify: `webapp/agents/nodes/run_execution.py:472-478` (`_url_shaped_criteria`)
- Test: `tests/test_wave34_pdp_seed_flip.py` (extend — helpers `_probe_data`, `LISTING_URL`, `PDP_URL` already exist; reuse `_mock_probe` from `tests/test_wave39_swap_restart_shape.py:38-47` if a standalone probe double is needed)

**Interfaces:**
- Produces: `src.seed_urls.first_criteria_url(criteria: str | None) -> str` — first `http(s)://` token in free text, `""` when none. Consumers: `_pdp_listing_swap` (graph `:2103`), advisory probe gate (`:2228`), `_invoke_navigation_traverse` fallback (`:4267`), `_probe_listing_candidates` (`:7442`), `_url_shaped_criteria` (run_execution `:472`).
- Contract: `state.search_criteria` / `job.search_criteria` are NEVER rewritten — the full multi-line string stays intact for the writer brief and UI; only URL-*parsing* consumers see line one. Multi-URL input remains `url_list` mode via `input_urls.json`, so "first line" cannot break any multi-listing scraper — it stops handing them a mangled 404 string. Lines 2..N are intentionally un-parsed (accepted residual R11 — T5's per-line seeding is where additional listing lines belong).

Verified defect (planning agent E; all five sites re-read in-session): intake's listing textarea (`templates/scraper/intake.html:509` — *"One listing page per line"*) posts `listing_urls`, stored **verbatim** into `search_criteria` (`views.py:3013`; the wave-32 B3 host gate at `:3029-3031` already `re.findall`s it as a list). Every URL consumer does `str(criteria).strip()` + `startswith(("http://","https://"))` — TRUE for the multi-line string — then `urlsplit`, which **deletes ASCII newlines** (verified live: `https://h/a\nhttps://h/b` → `netloc=h`, `path=/ahttps://h/b`).

**Causality correction (critique agent 4, prod-replay):** job 758 birkenstock reached COMPLETED n=1 because intake coercion seeded a PDP before any probe ran; the multi-line string broke the LISTING-PROBE only (advisory probe 404 → swap gate failed closed → wave-34 demote). T4 is therefore a **probe-quality fix** and 758 a near-miss, not the direct cause of a FAILED job. The 1-item collapse class is closed by T5's seeding (the coerced URL is a valid listing/seed) plus T9's rescue floor. Keep the fix — the mangled-URL parse is real and fires on every multi-criteria job.

Out of scope (deliberately untouched — not URL semantics): `subagents.py:4159/:5022-5023` (writer `--query` text), `field_confirmation.py:268-276`, `navigate_explore.py:2157` (already `.replace("\n", " ")`), `browser_traverse`'s search-phrase plumbing at `graph.py:3982`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_wave34_pdp_seed_flip.py`)

```python
MULTI = f"{LISTING_URL}\nhttps://www.vinted.be/catalog/6-women"


class TestMultiLineCriteria:
    """[wave-40 T4] intake.html:509 puts 'One listing page per line' text into
    search_criteria; urlsplit strips the newline and every consumer parsed ONE
    same-host mangled URL (prod 758 birkenstock: advisory probe 404 -> swap
    gate failed closed -> wave-34 demote -> 1-item COMPLETED)."""

    def test_first_criteria_url_helper(self):
        from src.seed_urls import first_criteria_url
        assert first_criteria_url(MULTI) == LISTING_URL
        assert first_criteria_url(f"see below\n{LISTING_URL}, {PDP_URL}") == LISTING_URL
        assert first_criteria_url("levis 501") == ""
        assert first_criteria_url(None) == ""
        assert first_criteria_url(LISTING_URL) == LISTING_URL

    def test_swap_uses_first_line_only(self):
        st = {"job_id": 0, "url": PDP_URL, "input_mode": "list_page",
              "site_slug": "vinted-be", "search_criteria": MULTI}
        seed = _probe_data([{"@type": "Product", "offers": {"price": 44.99}}])
        lp = _probe_data([{"@type": "ItemList",
                           "itemListElement": [{"@type": "Product"}]}])
        updates, note = graph._pdp_listing_swap(st, PDP_URL, seed, lp)
        assert updates["url"] == LISTING_URL and "\n" not in updates["url"]
        assert updates["product_url"] == LISTING_URL
        assert LISTING_URL in note and "catalog/6-women" not in note

    def test_url_shaped_criteria_returns_first_line(self):
        from webapp.agents.nodes.run_execution import _url_shaped_criteria
        assert _url_shaped_criteria({"search_criteria": MULTI}) == LISTING_URL
        assert _url_shaped_criteria({"search_criteria": "levis"}) == ""

    def test_traverse_and_probe_sites_read_the_helper(self):
        with open(graph.__file__, encoding="utf-8") as fh:
            src = fh.read()
        assert src.count("first_criteria_url(") >= 4
```

- [ ] **Step 2: Run, verify RED**

Run: `docker compose exec django sh -c "cd /app/webapp && pytest ../tests/test_wave34_pdp_seed_flip.py -q"`
Expected: `ImportError` on `first_criteria_url`; the swap test FAILS (`updates["url"]` is the mangled joined string).

- [ ] **Step 3: Implement** — `src/seed_urls.py` (add `re` to the stdlib import block; extend `__all__`):

```python
def first_criteria_url(criteria: str | None) -> str:
    """First http(s) URL in a free-text ``search_criteria`` value.

    Intake stores the ``listing_urls`` textarea verbatim ("One listing page
    per line", intake.html:509), so search_criteria is routinely MULTI-line.
    urlsplit deletes ASCII newlines, so parsing the whole string yields ONE
    same-host mangled URL (prod job 758: advisory probe 404 -> swap gate
    failed closed -> wave-34 demote). Token rule matches the intake host gate
    (views.py:3030). "" when no line is a URL; the full string stays in
    state/job.search_criteria untouched. Lines 2..N are intentionally not
    parsed here — per-line seeding is intake's job (wave-40 T5).
    """
    for token in re.findall(r"https?://[^\s,]+", str(criteria or "")):
        return token
    return ""
```

`webapp/agents/graph.py` — add `from src.seed_urls import first_criteria_url` in the module-level `src` import block (beside `:56`), then replace the four read lines exactly:

```python
:2103   _criteria = first_criteria_url(state.get("search_criteria"))
:2228   _criteria = first_criteria_url(state.get("search_criteria"))
:4267   _crit = first_criteria_url(state.get("search_criteria"))
:7442   _sc = first_criteria_url(state.get("search_criteria"))
```

`webapp/agents/nodes/run_execution.py:472-478`:

```python
def _url_shaped_criteria(state: ScrapeState) -> str:
    """The user's search_criteria when it is itself a URL (job 85's real
    listing lived ONLY in search_criteria while the job URL was a PDP).
    Multi-line criteria: the FIRST http(s) line — a mangled joined URL is
    a 404, not a listing (prod job 758)."""
    from src.seed_urls import first_criteria_url

    return first_criteria_url(state.get("search_criteria"))
```

Existing `if _alt == primary: _alt = ""` guards downstream of `:7442`/`:514` are kept — first-line normalization cannot create a degenerate retry candidate.

- [ ] **Step 4: Run, verify GREEN** — same command; then `../tests/test_wave39_swap_restart_shape.py` (swap contract) and `../tests/test_wave32_host_gate.py` (host gate still finds all hosts — it `re.findall`s the raw string, unaffected).
- [ ] **Step 5: Commit** — `fix(wave-40): first_criteria_url — parse line one of multi-line criteria (T4)` — restart celery-worker django.

---

# Phase 2 — Intake repair + gate (T5)

### Task 5: Repair-on-coercion + reject truly-empty url_list jobs (422, before job creation)

**Files:**
- Create: `src/intake_url_list.py`
- Modify: `webapp/scraper/views.py` — intake create path (coercion site + pre-create gate), `site_scrape` (~`:1721`), `job_restart` (after the `:668-696` file checks)
- Modify: partner-API intake writer — `grep -rn "item_urls_deduped" webapp/` (`writers.py:74`): the url_list decision there must require a non-empty deduped item list (flip the `or` to `and not item_urls_deduped` — an empty item list must never yield url_list)
- Modify: `manage.py scrape` (or its management-command module — grep `def scrape` under `webapp/`): same reason check as a hard `SystemExit` before enqueueing (a CLI cannot 422)
- Test: `tests/test_wave40_intake_url_list_gate.py` (new, pure unit) + `webapp/tests/test_wave40_intake_url_list_gate.py` (new, view layer)

**Interfaces:**
- Consumes: `_parse_url_lines` (`views.py:2793-2800`), `_fm_exists` (`views.py:93-98`), `_INTAKE_NAV_TO_INPUT_MODE` (`views.py:2651-2658` — contains `"pdp": "url_list"`, the W37-NEW-C coercion that created this class).
- Produces (`src.intake_url_list`, stdlib-only, importable from `src/` and `webapp/`):
  - `_has_url_text(list_urls: str) -> bool` — any non-blank line.
  - `coerced_seed_url(url: str, input_mode: str, list_urls: str) -> str` — the PDP URL to seed a coerced `url_list` job with (`url` itself, http(s)-checked), or `""`. Repair-on-coercion: **the caller** passes it through the same W37-NEW-C predicate that produced the coercion (read the predicate at the coercion site; do not invent a second PDP heuristic).
  - `missing_url_list_reason(input_mode: str, list_urls: str, site_has_urls: bool, fm_file_exists: bool | None, seed_url: str = "") -> str` — `""` (OK) or a human reason. Returns non-empty ONLY for `input_mode == "url_list"` with zero parsed lines AND no site URLs AND no seed URL AND a definitive FM `False`. `fm_file_exists=None` means "couldn't check" → **fail-open** (reason `""`) so a transient FM outage can't lock out legitimate re-scrapes (`_fm_exists` itself fails closed on exception — the view wraps it).
- HTTP: `422 {"error": <reason>, "missing_url_list": True}` from intake create and `job_restart`.

**Why repair-first (critique agents 4+5):** job 758 birkenstock is proof the coerced PDP flow can WORK — it reached COMPLETED because a PDP got seeded. Rev-1's reject-only gate would have 422'd that shape at intake. The 21 instant-fails died because NOTHING seeded the coerced URL (`setup_workspace.py:327-343`: "Site.input_urls is empty and no scrapers/{slug}/input_urls.json") after burning a queued slot each. Seeding the coerced URL at intake turns all 21 into valid 1-item url_list jobs; the 422 remains for the genuinely-nothing case (explicit url_list, empty textarea, no PDP, no site URLs, no FM file).

- [ ] **Step 1: Write the failing unit tests** — `tests/test_wave40_intake_url_list_gate.py`:

```python
"""[wave-40 T5] url_list intake: a coerced PDP is SEEDED (repair), a truly
empty url_list is REJECTED (422). Prod 775-780, 784, 789, 793-796, 799,
802-804, 806, 811-814 — 21 instant fails in one aarya batch — died in ~4s in
setup_workspace because nothing seeded the coerced URL. 758 birkenstock is
the counter-proof that a seeded coerced PDP works (COMPLETED)."""

from src.intake_url_list import (
    coerced_seed_url, missing_url_list_reason as reason)


def test_url_list_with_no_urls_is_rejected():
    out = reason("url_list", "", False, False, "")
    assert out and "url" in out.lower()


def test_seed_url_rescues_repair_on_coercion():
    assert reason("url_list", "", False, False,
                  "https://x.example/p/tee-1") == ""


def test_url_list_with_a_url_passes():
    assert reason("url_list", "https://x.example/p/1", False, False, "") == ""


def test_site_urls_still_rescue_an_empty_textarea():
    assert reason("url_list", "", True, False, "") == ""


def test_fm_file_still_rescues():
    assert reason("url_list", "", False, True, "") == ""


def test_fm_unknown_fails_open():
    assert reason("url_list", "", False, None, "") == ""


def test_non_url_list_modes_never_rejected():
    for mode in ("navigation", "list_page", "search_term", ""):
        assert reason(mode, "", False, False, "") == ""


def test_whitespace_only_lines_do_not_count():
    assert reason("url_list", "  \n  \n", False, False, "")


def test_coerced_seed_url_returns_pdp():
    assert coerced_seed_url(
        "https://x.example/p/tee-1", "url_list", ""
    ) == "https://x.example/p/tee-1"


def test_coerced_seed_url_needs_url_list_mode_and_empty_textarea():
    assert coerced_seed_url("https://x.example/p/1", "navigation", "") == ""
    assert coerced_seed_url(
        "https://x.example/p/1", "url_list", "https://x.example/p/2") == ""
    assert coerced_seed_url("levis 501", "url_list", "") == ""
```

- [ ] **Step 2: Run, verify RED** — `ModuleNotFoundError: src.intake_url_list`.
- [ ] **Step 3: Implement the helper** — `src/intake_url_list.py`:

```python
"""[wave-40 T5] Intake-side repair + validation for url_list jobs.

Prod 09-20/21: 21 jobs submitted as bare PDP URLs were coerced to url_list
(W37-NEW-C), created, queued, then died in ~4s in setup_workspace because
neither the textarea nor scrapers/{slug}/input_urls.json held any URLs.
Repair: seed the coerced PDP URL (that flow works — prod 758). Gate: reject
with 422 only when genuinely nothing exists. Stdlib-only; importable from
src/ and webapp/.
"""


def _has_url_text(list_urls: str) -> bool:
    for line in str(list_urls or "").splitlines():
        if line.strip():
            return True
    return False


def coerced_seed_url(url: str, input_mode: str, list_urls: str) -> str:
    """The URL to seed a coerced url_list job with, or "".

    Repair-on-coercion (wave-40 T5): when intake's W37-NEW-C coercion turned
    a PDP submission into url_list, that URL IS the item list. The caller
    must apply the same predicate the coercion used — this helper only
    checks mode/emptiness/shape.
    """
    if (input_mode or "").lower() != "url_list":
        return ""
    if _has_url_text(list_urls):
        return ""
    u = str(url or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def missing_url_list_reason(
    input_mode: str,
    list_urls: str,
    site_has_urls: bool,
    fm_file_exists: bool | None,
    seed_url: str = "",
) -> str:
    """'' when the intake payload is acceptable, else a human reason.

    fm_file_exists: True/False from the File-Master check, None when the
    check could not run (transient FM error) — None fails OPEN so a brief
    FM outage cannot lock out legitimate re-scrapes.
    """
    if (input_mode or "").lower() != "url_list":
        return ""
    if _has_url_text(list_urls) or site_has_urls or seed_url:
        return ""
    if fm_file_exists is None or fm_file_exists:
        return ""
    return (
        "input_mode url_list requires at least one product URL: paste URLs "
        "in the list box (one per line), or pick a mode that discovers URLs."
    )
```

- [ ] **Step 4: Run unit tests, verify GREEN**
- [ ] **Step 5: Write the failing view tests** — `webapp/tests/test_wave40_intake_url_list_gate.py`:

```python
"""[wave-40 T5] view layer: coerced PDP intake is seeded (job created,
url_list, URL stored); a truly-empty url_list intake returns 422 and creates
NO ScrapeJob row; job_restart refuses an unrecoverable url_list job."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from model_bakery import baker

from scraper.models import ScrapeJob, Site

PDP = "https://www.example.com/products/tee-1"
HOME = "https://www.example.com/"


@patch("scraper.tasks.run_scrape_task.delay", MagicMock())  # CELERY_TASK_ALWAYS_EAGER would otherwise run the graph
class TestIntakeUrlListGate(TestCase):
    """Intake view facts (verified): requires HTTP_X_REQUESTED_WITH=
    XMLHttpRequest (else 400); nav_method ABSENT on a PDP-shaped url
    coerces to url_list (W37-NEW-C)."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("staff", password="pw")
        self.client.force_login(self.user)

    def _post(self, **over):
        data = {"url": over.pop("url", PDP), "list_urls": over.pop("list_urls", "")}
        data.update(over)
        return self.client.post(
            reverse("intake_create_job"), data,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_coerced_pdp_is_seeded_not_rejected(self):
        resp = self._post()
        self.assertNotEqual(resp.status_code, 422)
        job = ScrapeJob.objects.latest("id")
        self.assertEqual(job.input_mode, "url_list")
        # ADAPT the store, never the contract: assert the seeded URL landed in
        # whichever store the intake seeding branch populates (Site.input_urls
        # and/or scrapers/{slug}/input_urls.json — views.py seeding branch).
        site = Site.objects.filter(
            input_urls__contains="products/tee-1").first()
        self.assertIsNotNone(site)

    def test_truly_empty_url_list_is_422_and_no_job_row(self):
        resp = self._post(url=HOME)
        self.assertEqual(resp.status_code, 422)
        self.assertTrue(resp.json()["missing_url_list"])
        self.assertFalse(ScrapeJob.objects.filter(url=HOME).exists())

    def test_explicit_url_list_with_urls_passes(self):
        resp = self._post(
            list_urls="https://www.example.com/p/1\nhttps://www.example.com/p/2")
        self.assertNotEqual(resp.status_code, 422)
        self.assertTrue(ScrapeJob.objects.exists())

    def test_listing_mode_unaffected(self):
        resp = self._post(nav_method="listing",
                          list_urls="https://www.example.com/collections/all")
        self.assertNotEqual(resp.status_code, 422)
        self.assertTrue(ScrapeJob.objects.exists())

    def test_job_restart_blocked_when_url_list_unrecoverable(self):
        job = baker.make(ScrapeJob, url=HOME, input_mode="url_list",
                         status=ScrapeJob.STATUS_FAILED)
        resp = self.client.post(
            reverse("job_restart", kwargs={"job_id": job.id}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 422)
        self.assertTrue(resp.json()["missing_url_list"])
```

*(ADAPT notes: the endpoint/URL name is `intake_create_job` per the wave-32 host-gate harness — verify with `reverse()` at implement time; if the field name for the mode selector differs from `nav_method`, keep the view's real name. The URL-name field for the listing textarea is `list_urls` per `views.py:2793`. The 422-when-empty case drives via `url=HOME`: with no nav_method and a non-PDP URL the intake default is still url_list (verified); if the intake defaults a homepage elsewhere, drive the 422 via the explicit mode field instead — the status/no-row assertions are fixed.)*

- [ ] **Step 6: Run, verify RED** — the empty case currently creates a job (201/302), restart returns 302.
- [ ] **Step 7: Wire into views**

Fail-open FM wrapper (beside `_parse_url_lines`, `views.py:2793-2800`):

```python
def _fm_file_exists_failopen(site_slug: str) -> bool | None:
    """True/False from _fm_exists; None on any error (fail-open, T5)."""
    try:
        return _fm_exists(f"scrapers/{site_slug}/input_urls.json")
    except Exception:
        return None
```

Intake create — in the coercion path, seed first, then gate (both before `ScrapeJob` creation at `:3136`):

```python
# [wave-40 T5] repair-on-coercion: a PDP coerced to url_list IS a 1-item
# list — seed it (prod 758 proves the flow works). Gate only the empty case.
_seed = coerced_seed_url(url, input_mode, list_urls)
if _seed:
    list_urls = _seed   # flows into the existing seeding branch (:3159-3176)
_reason = missing_url_list_reason(
    input_mode, list_urls, bool(site and site.input_urls),
    _fm_file_exists_failopen(site_slug), _seed)
if _reason:
    return JsonResponse({"error": _reason, "missing_url_list": True}, status=422)
```

Apply the same repair+gate at `site_scrape` (`views.py:1721`) and the same gate at `job_restart` (after the `:668-696` file checks, same 422 body); flip the partner-API writer predicate (`writers.py:74` → `and not item_urls_deduped`); hard-fail `manage.py scrape` with the reason text before enqueueing. Import `from src.intake_url_list import coerced_seed_url, missing_url_list_reason` beside the other `src.` imports.

- [ ] **Step 8: Run, verify GREEN** — new view tests + `webapp/tests/test_intake_site_dedupe.py` + `webapp/tests/test_wave32_host_gate.py`. **Check their fixtures first:** if either posts a url_list payload with no URLs (dedupe/host-gate tests at `test_intake_site_dedupe.py:109-117`, `test_wave32_host_gate.py:92-96`), give the fixture a non-empty `list_urls` line so it keeps testing what it meant to test (dedupe / host gate, not the new 422).
- [ ] **Step 9: Lint + full suite + commit** — `fix(wave-40): intake repairs coerced PDPs, 422s empty url_list (T5)`. Restart django.

---

# Phase 3 — Draft call-signature AST gate (T6–T7)

### Task 6: AST-only helper registry + `draft_call_violation` in `webapp/agents/draft_safety.py`

**Files:**
- Modify: `webapp/agents/draft_safety.py` (append; module already holds the draft-freeze/restore machinery — `restore_job_draft` at `:319`)
- Test: `tests/test_wave40_draft_call_gate.py` (new)

**Interfaces:**
- Produces:
  - `DRAFT_CALL_VIOLATION_MARKER: str = "HELPER CALL SIGNATURE VIOLATION"`
  - `_REGISTRY_MODULES: list[str]` — repo-relative module paths: `src/http_fetch.py`, `src/listing_discovery.py`, `src/seed_urls.py`, `src/content_types.py`, `src/intake_coerce.py`, `src/intake_url_list.py`, plus the active template set `templates/http_navigation_scraper.py`, `templates/navigation_scraper.py`, `templates/undetected_chromedriver_scraper.py`, `templates/api_scraper.py`, `templates/playwright_scraper.py`, `templates/requests_scraper.py`, `templates/shopify_scraper.py` (retired per-domain templates excluded — see CLAUDE.md dead-templates list).
  - **AST-only registry** — `_helper_registry() -> dict[str, dict]`, memoized. For each module: `ast.parse` the file, walk ALL `FunctionDef`/`AsyncFunctionDef` at any depth, and record a plain dict per name: `{"positional": [arg names], "kwonly": [...], "vararg": bool, "kwarg": bool, "required_positional": int, "required_kwonly": [names]}` derived from `node.args` directly. **No `inspect.getfullargspec` on synthetic objects (meaningless), no importing of the registry modules** (rev-1's import approach silently fall-opens when an import fails — critique finding). Factory→closure aliasing: for a module-level function whose body ends in `return <nested_name>` where `<nested_name>` is a FunctionDef nested inside it, register the nested signature under BOTH names (`create_fetch_json` and `fetch_json`).
  - `draft_call_violation(source: str) -> str` — `""` PASS, else a capped (5) newline-joined findings string prefixed with `DRAFT_CALL_VIOLATION_MARKER`. Per `ast.Call`:
    1. Build a draft-local view first: `def`/`async def` names shadow the registry; `name = factory_call()` assignments alias `name` → the factory's returned closure signature (this is how generated drafts actually call the http_fetch closures — `fetch_json = create_fetch_json()` then bare `fetch_json(url, ...)`).
    2. For a call on a name with a known signature: **unexpected-kwarg** (kwarg not in positional+kwonly and no `**kwarg`), **multiple-values-for-argument** (positional slot collides with a keyword naming that positional), **too-many-positionals** (exceeds positional count with no vararg) are the violations. `bind_partial` semantics otherwise: missing-required and starred calls are LEGAL (the writer may know runtime defaults).
    3. Regex literal check: a `Constant str` first positional arg to `re.{compile,match,fullmatch,search,sub,subn,split,findall,finditer}` is `re.compile`-checked; `re.error` → violation (job-762 class).
  - Any internal error in the checker → return `""` (fall OPEN — a gate bug must never block a good draft).

Verified defect (planning agent C, signatures read from source): real http_fetch surface is **factories returning closures** — `create_fetch_page` (`src/http_fetch.py:290`) → `fetch_page(url, min_tier=0)` `:307`; `create_fetch_text` (`:393`) → `fetch_text(url, params=None, min_tier=0)` `:420`; `create_fetch_json` (`:500`) → `fetch_json(url, params=None, min_tier=0)` `:512`; drafts call the closures BARE. 760 crashed `fetch_json() got an unexpected keyword argument 'post'`; 791 `_discover_listing_urls_with_retry() got multiple values for argument 'fetch_page'` (`src/listing_discovery.py:528`, keyword-only after positional); 762 `re.error: nothing to repeat`. Compile gate + F821 gate (`filesystem_tools.py:63-96/:449/:527`) check neither.

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_draft_call_gate.py`:

```python
"""[wave-40 T6] AST pre-gate: helper CALL signatures in generated drafts.

Prod: 760 (unexpected kwarg 'post' on the create_fetch_json closure),
791 (multiple values for 'fetch_page'), 762 (re.error nothing to repeat) —
all crashed at execution after passing compile + F821."""

from agents.draft_safety import (
    DRAFT_CALL_VIOLATION_MARKER, draft_call_violation)

FETCHER_BAD = (
    "from src.http_fetch import create_fetch_json\n"
    "fetch_json = create_fetch_json()\n"
    "rows = fetch_json('https://x.example/api', post={'q': 1})\n"
)  # job 760 shape: closure takes (url, params=None, min_tier=0)

DISCOVERY_BAD = (
    "from src.listing_discovery import (\n"
    "    discover_listing_urls_with_retry, create_fetch_page)\n"
    "fetch_page = create_fetch_page()\n"
    "urls = discover_listing_urls_with_retry(\n"
    "    'https://x.example', fetch_page, fetch_page=fetch_page)\n"
)  # job 791 shape: fetch_page positional AND keyword

REGEX_BAD = "import re\nPAT = re.compile('a{2,1}')\n"  # job 762 shape

FETCHER_GOOD = (
    "from src.http_fetch import create_fetch_json\n"
    "fetch_json = create_fetch_json()\n"
    "rows = fetch_json('https://x.example/api', params={'q': 1})\n"
)

DISCOVERY_GOOD = (
    "from src.listing_discovery import (\n"
    "    discover_listing_urls_with_retry, create_fetch_page)\n"
    "fetch_page = create_fetch_page()\n"
    "urls = discover_listing_urls_with_retry('https://x.example', fetch_page)\n"
)


def test_unexpected_kwarg_is_caught():
    out = draft_call_violation(FETCHER_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "post" in out


def test_multiple_values_for_kwarg_is_caught():
    out = draft_call_violation(DISCOVERY_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "fetch_page" in out


def test_bad_regex_literal_is_caught():
    out = draft_call_violation(REGEX_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)


def test_valid_registry_calls_pass():
    assert draft_call_violation(FETCHER_GOOD + "\nimport re\nre.findall(r'x+', html)\n") == ""
    assert draft_call_violation(DISCOVERY_GOOD) == ""


def test_draft_local_defs_shadow_the_registry():
    src = ("def fetch_json(url, post=None):\n"
           "    return {}\n"
           "fetch_json('https://x', post={'a': 1})\n")
    assert draft_call_violation(src) == ""


def test_starred_and_missing_args_stay_legal():
    src = (FETCHER_GOOD +
           "fetch_json(*parts)\n"
           "fetch_json()\n")
    assert draft_call_violation(src) == ""


def test_unknown_helpers_fall_open():
    assert draft_call_violation("my_invented_helper(url, post=1, bogus=2)\n") == ""


def test_findings_are_capped_at_five():
    calls = "\n".join(
        f"fetch_json('https://x', post={i})" for i in range(9))
    src = ("from src.http_fetch import create_fetch_json\n"
           "fetch_json = create_fetch_json()\n" + calls)
    out = draft_call_violation(src)
    assert out.count("\n") <= 5


def test_internal_error_falls_open(monkeypatch):
    from agents import draft_safety as ds
    monkeypatch.setattr(ds, "_helper_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert draft_call_violation("fetch_json('https://x', post=1)") == ""
```

*(ADAPT: read the real `discover_listing_urls_with_retry` signature at `src/listing_discovery.py:528` before finalizing DISCOVERY_BAD — the positional/keyword split must reproduce the actual `multiple values` collision; if the factory aliasing differs, adjust the draft text, never the assertion intent.)*

- [ ] **Step 2: Run, verify RED** — ImportError (function absent).
- [ ] **Step 3: Implement** — append to `webapp/agents/draft_safety.py` per the Interfaces contract. Resolve registry module paths against the repo root the same way the module's existing code locates files (read its imports first). Memoize `_helper_registry` with a module-level cache; treat a missing/unreadable module file as "contributes nothing" but if the WHOLE registry parses empty, log loudly and let `draft_call_violation` fall open (a half-built gate must not block drafts).
- [ ] **Step 4: Run, verify GREEN** — new file green; run the existing draft_safety suite (grep `draft_safety` under `tests/`) to confirm no regression in the restore/freeze machinery.
- [ ] **Step 5: Commit** — `feat(wave-40): draft_call_violation AST helper-signature gate (T6)`

### Task 7: Wire the call gate into the write-path gates + fence salvage

**Files:**
- Modify: `webapp/agents/tools/filesystem_tools.py:63-96` — the real seam is `_f821_rejections(path: str, content: str) -> str`; call sites `:450`, `:528`
- Modify: `webapp/agents/subagents.py:1424` — the gate arms via `syntax_gate=(agent_name == "code_writer")`; the call gate rides the same arming (no new plumbing)
- Modify: `webapp/agents/graph.py:9217-9300` (L2 chain — `report["deterministic_gate"]` gains `_violation_kind="call"`) and `:6920-6950` (fence-salvage bypass — same call)
- Test: `tests/test_wave40_draft_call_gate.py` (extend) + existing L2 gate tests (grep `deterministic_gate` under `tests/`)

**Interfaces:**
- Consumes: T6's `draft_call_violation(source) -> str` and `DRAFT_CALL_VIOLATION_MARKER`.
- Produces: a draft that violates a helper signature is **rejected at write time** (same predicate/message path as F821) instead of crashing at execution hours later. `report["deterministic_gate"]` records violation kind `"call"` alongside the existing F821 kind; `route_after_testing.py:1733-1757` needs **no change** (it routes on the marker string, already generic).

Critique fix (test-reality agent): rev-1's `_gate_check`/`_fake_state` helpers are phantom — the real tool surface is `get_filesystem_tools(project_root=..., syntax_gate=True)` returning tool objects with `.name`/`.func`, and the F821 rejection appears as the write_file tool RESULT with the file NOT created. Tests below use that real surface.

- [ ] **Step 1: Write the failing wire-in tests** (append to `tests/test_wave40_draft_call_gate.py`):

```python
def _tools(tmp_path):
    from agents.tools import filesystem_tools as ft
    return {t.name: t.func
            for t in ft.get_filesystem_tools(project_root=str(tmp_path),
                                             syntax_gate=True)}


class TestCallGateWiring:
    """[wave-40 T7] the write-path gate runs draft_call_violation beside the
    F821 check, under SCRAPER_DRAFT_CALL_GATE (default on)."""

    def test_write_tool_rejects_a_bad_signature_draft(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "1")
        out = _tools(tmp_path)["write_file"](
            path="workspace/example-com/scraper_draft.py", content=FETCHER_BAD)
        assert "HELPER CALL SIGNATURE VIOLATION" in str(out)
        assert not (tmp_path / "workspace" / "example-com"
                    / "scraper_draft.py").exists()

    def test_kill_switch_disables_the_call_gate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "0")
        out = _tools(tmp_path)["write_file"](
            path="workspace/example-com/scraper_draft.py", content=FETCHER_BAD)
        assert "HELPER CALL SIGNATURE VIOLATION" not in str(out)
        assert (tmp_path / "workspace" / "example-com"
                / "scraper_draft.py").exists()

    def test_f821_rejections_carries_the_call_gate(self, monkeypatch):
        from agents.tools import filesystem_tools as ft
        monkeypatch.setenv("SCRAPER_DRAFT_CALL_GATE", "1")
        assert "HELPER CALL SIGNATURE VIOLATION" in ft._f821_rejections(
            "workspace/x/scraper_draft.py", FETCHER_BAD)
        assert ft._f821_rejections(
            "workspace/x/scraper_draft.py", FETCHER_GOOD) == ""

    def test_l2_report_carries_violation_kind_call(self, monkeypatch):
        # ADAPT: copy the existing deterministic_gate L2 test body (grep
        # deterministic_gate under tests/), swap the draft for FETCHER_BAD,
        # and assert report["deterministic_gate"]["violation_kind"] == "call".
        ...
```

*(Fill the `...` by copying the existing L2 deterministic-gate test — harness, state fixture, and report assertions come from that test; only the input draft and the `violation_kind` assertion differ.)*

- [ ] **Step 2: Run, verify RED.**
- [ ] **Step 3: Implement** — in `_f821_rejections` (the single seam feeding both call sites `:450`/`:528`), immediately after the F821 check:

```python
import os
from agents.draft_safety import draft_call_violation
...
if os.getenv("SCRAPER_DRAFT_CALL_GATE", "1") != "0":
    _call = draft_call_violation(content)
    if _call:
        return _call   # same rejection shape F821 already returns
```

Import placement: `draft_call_violation` is `webapp.agents.*` and `filesystem_tools.py` is inside the package — use a module-lazy import inside the function if a top-level import would create a cycle (check `draft_safety`'s imports first). In the L2 chain (`graph.py:9217-9300`), extend the existing deterministic-gate stamp so `report["deterministic_gate"]` records `_violation_kind="call"` when the rejection carries `DRAFT_CALL_VIOLATION_MARKER`; apply the same call at the fence-salvage bypass (`graph.py:6920-6950`) so salvaged drafts cannot re-introduce the crash class.
- [ ] **Step 4: Run, verify GREEN** — wire-in tests + the existing L2 + filesystem_tools test files.
- [ ] **Step 5: Lint + full suite + commit** — `feat(wave-40): enforce helper call signatures at write time (T7)`. Restart celery-worker + django. Deferred (documented, not in this wave): a run_execution-side re-check — the write-path gate covers the crash class.

---

# Phase 4 — Finalize evidence + workspace hardening (T8–T12)

### Task 8: `_real_items_evidence` — job-scoped, three-source best-of-N

**Files:**
- Modify: `webapp/scraper/tasks.py` (append helpers near `_final_status_ladder`, ~`:1509`)
- Test: `tests/test_wave40_real_items_evidence.py` (new, no DB — unsaved ScrapeJob instances)

**Interfaces:**
- Produces (consumed by T9):
  - `_rescue_min_count(input_mode: str) -> int` — `1` for `url_list`/`list_page`, else `3` (identical to the wave-37 ladder rule).
  - `_output_name_epoch(name: str) -> float | None` — parse `output_%Y-%m-%d_%H%M%S[_%f]_{pid}.json` (the `%f` field is 6 digits, e.g. `000001`) to a UTC epoch. Filename IS the write time; FM `/list` returns keys without mtimes.
  - `_good_rows(data, fields: list[str], output_key: str) -> list[dict]` — dict payload, top-level list under `output_key`/`products`/`jobs`/`articles`/..., drop `_is_dead_product` rows, keep rows carrying a core/schema field (same predicate chain as `_scraper_has_real_items`, `route_after_testing.py:623-646` + job-118 schema union).
  - `_real_items_evidence(slug: str, job: ScrapeJob, final_state: dict | None = None) -> tuple[int, str]` — `(good_row_count, locator)`, `(0, "")` when nothing qualifies. Candidates, each freshness-gated by `max(job.started_at - 5s)`: (a) `workspace/{slug}/output_*.json` (mtime), (b) local `scrapers/{slug}/output_*.json` (mtime — dev bind-mount only), (c) FM keys `scrapers/{slug}/output_*.json` (filename epoch) via `src.artifacts.list_keys`/`read_text`. Guards: skip `metadata.phase == "discovery"` stubs; trace guard requires `final_state` `tested_draft_sha256` OR `last_tested_draft_fp` OR FM per-job draft key `scrapers/{slug}/jobs/scraper-{job_id}.py`; kill switch `REAL_ITEMS_RESCUE_ENABLED=0`; every read error degrades to `(0, "")`.
- Paths resolve via `django.conf.settings.PROJECT_ROOT` (tasks.py's existing idiom, `:1787`/`:3001`) — **NOT** a `tasks.PROJECT_ROOT` module attribute (does not exist; critique finding).

**Why job-scoped, not attempt-scoped (planning agent B, verified):** the wave-37 rescue predicate `_scraper_has_real_items` scans workspace-only (`route_after_testing.py:623-646/:736`) and its `_freshness_floor` (`:468-517`) admits only outputs newer than the current draft / `last_tested_at`. Job 762: 8 workspace outputs from earlier cycles, all older than the (crashed) current attempt's draft floor → honest FAIL with 4+ real products on disk. Job 770: the 19-product output was moved to `scrapers/` **by 770's own cleanup agent** (narrative correction — not finalize's publish block), so a workspace-only scan sees nothing. Attribution is job-scoped (`started_at` window) + draft-present; outputs carry no draft sha, so sha-level attribution is impossible retroactively — stated honestly in the docstring.

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_real_items_evidence.py`:

```python
"""[wave-40 T8] Real-items evidence must be JOB-scoped and must scan
workspace, local scrapers/, AND the FM. Prod 770 (19 products in scrapers/,
FAILED n=0) and 762 (8 workspace outputs pre-dating the crashed attempt's
draft floor) both escaped the attempt-scoped workspace-only guard."""

import json

from django.conf import settings
from django.utils import timezone

from scraper import tasks
from scraper.models import ScrapeJob

OUT_NAME = "output_2026-09-20_235455_000001_99.json"  # %H%M%S_%f_%pid


def _job(started_at, **kw):
    # unsaved instance — the helper reads only attributes.
    # NOTE: ScrapeJob has NO site_slug field; the slug is a separate arg.
    return ScrapeJob(url="https://x.example/", started_at=started_at, **kw)


def _root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))


def _rows(n):
    return [{"title": f"p{i}", "price": "$1", "url": f"https://x/p/{i}"}
            for i in range(n)]


def _fm(monkeypatch, keys, payloads):
    monkeypatch.setattr("src.artifacts.list_keys", lambda prefix="": keys)
    monkeypatch.setattr("src.artifacts.read_text",
                        lambda key: payloads.get(key, ""))


def test_770_shape_fm_output_rescued_workspace_empty(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=2)
    key = f"scrapers/sephora-cz/{OUT_NAME}"
    _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(19)})})
    (tmp_path / "workspace" / "sephora-cz").mkdir(parents=True)  # no outputs
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "sephora-cz", _job(started),
        final_state={"last_tested_draft_fp": "abc"})
    assert n == 19 and loc.endswith(OUT_NAME)


def test_762_shape_workspace_outputs_from_earlier_attempt_admitted(
        tmp_path, monkeypatch):
    import os
    started = timezone.now() - timezone.timedelta(hours=3)
    ws = tmp_path / "workspace" / "marimekko"
    ws.mkdir(parents=True)
    for i in range(4):   # four files, ALL older than any draft, newer than start
        f = ws / f"output_2026-09-20_0{i}0000_000001_1.json"
        f.write_text(json.dumps({"products": _rows(1)}))
    (ws / "scraper_draft.py").write_text("# current attempt draft")
    old = (timezone.now() - timezone.timedelta(hours=2)).timestamp()
    for f in ws.glob("output_*.json"):
        os.utime(f, (old, old))
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "marimekko", _job(started),
        final_state={"last_tested_draft_fp": "fp"})
    assert n == 4 and "marimekko" in loc


def test_prior_job_output_never_rescues(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "sally"
    ws.mkdir(parents=True)
    f = ws / "output_2026-09-20_010000_000001_1.json"   # hours BEFORE started
    f.write_text(json.dumps({"products": _rows(20)}))
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "sally", _job(started), final_state={"tested_draft_sha256": "a"})
    assert n == 0


def test_discovery_stub_file_is_skipped(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "stub"
    ws.mkdir(parents=True)
    (ws / "output_2026-09-20_020000_000001_1.json").write_text(json.dumps(
        {"products": _rows(5), "metadata": {"phase": "discovery"}}))
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "stub", _job(started), final_state={"tested_draft_sha256": "a"})
    assert n == 0


def test_no_tested_draft_provenance_blocks(monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    key = "scrapers/z/output_2026-09-20_020000_000001_1.json"
    _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(9)})})
    n, _ = tasks._real_items_evidence("z", _job(started), final_state=None)
    assert n == 0   # trace guard: no draft provenance -> no rescue


def test_corrupt_json_degrades_to_zero(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "bad"
    ws.mkdir(parents=True)
    (ws / "output_2026-09-20_030000_000001_1.json").write_text("{not json")
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "bad", _job(started), final_state={"tested_draft_sha256": "a"})
    assert n == 0 and loc == ""


def test_kill_switch_disables(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_ITEMS_RESCUE_ENABLED", "0")
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "k", _job(timezone.now()), final_state={"tested_draft_sha256": "a"})
    assert n == 0


def test_min_count_is_1_for_url_list_and_3_otherwise():
    assert tasks._rescue_min_count("url_list") == 1
    assert tasks._rescue_min_count("list_page") == 1
    assert tasks._rescue_min_count("navigation") == 3


def test_output_name_epoch_parses_both_formats():
    assert tasks._output_name_epoch(
        "output_2026-09-20_235455_000001_99.json") is not None
    assert tasks._output_name_epoch(
        "output_2026-09-20_235455_99.json") is not None
    assert tasks._output_name_epoch("input_urls.json") is None
```

- [ ] **Step 2: Run, verify RED** — AttributeErrors (helpers absent).
- [ ] **Step 3: Implement** the four helpers in `webapp/scraper/tasks.py` exactly per the Interfaces contract. Notes: `_output_name_epoch` uses `datetime.strptime` with the two known formats (`%Y-%m-%d_%H%M%S_%f_%H%M%S`-pid shape — match on the `output_` prefix + trailing `_{pid}.json`, parse the timestamp portion with both `%f` and no-`%f` variants); FM reads go through `src.artifacts` (already imported in tasks.py); the started_at window is `job.started_at - 5s` to tolerate same-second writes; reads of `settings.PROJECT_ROOT` follow the file's existing idiom.
- [ ] **Step 4: Run, verify GREEN.**
- [ ] **Step 5: Commit** — `feat(wave-40): job-scoped real-items evidence helper (T8)`

### Task 9: Finalize chokepoint — the ladder consults the evidence; compile-gated draft promotion

**Files:**
- Modify: `webapp/scraper/tasks.py` — `_final_status_ladder` (gains `rescue_count: int = 0, rescue_file: str = ""` kwargs; new arm) + `_finalize_job` wiring + draft promotion
- Test: `tests/test_wave40_finalize_rescue.py` (new, `pytestmark = pytest.mark.django_db`)

**Interfaces:**
- Consumes: T8's `_real_items_evidence(slug, job, final_state) -> tuple[int, str]` and `_rescue_min_count(input_mode) -> int`.
- Produces: a FAILED/zero finalize is upgraded to COMPLETED **only when** the evidence proves ≥ `_rescue_min_count` real rows produced during this job; `job.product_count` set to the row count; `job.output_file` repointed to the chosen file's FM key; the stale error message scrubbed by the existing rule (`tasks.py:1917-1924`); the draft promoted to `scrapers/{slug}/scraper.py` **only if it compiles** (see below).
- **Signature seam (critique fix):** `_finalize_job(job: ScrapeJob) -> None` is a plain function (tasks.py:1646) whose state comes from the LangGraph checkpoint (`:1666-1674`); its callers are `tasks.py:618`/`:794`. **No signature change.** The tests freeze the checkpoint read through a `LangGraphService` double.
- **Promotion gate (critique fix):** rev-1 promoted the workspace/FM draft unconditionally — that would have promoted 762's *crashing* draft. Promotion order: FM per-job draft `scrapers/{slug}/jobs/scraper-{job_id}.py` → `-good` twin → workspace draft; the FIRST candidate whose `compile(source, name, "exec")` succeeds wins (plus `draft_call_violation(source) == ""` when `agents.draft_safety` imports cleanly); none compile → **skip promotion, log loudly** — a COMPLETED with items but no promotable draft is honest (the invariant that must never break is product_count>0, not draft presence).
- **Honest limits (in the docstring):** `store_job_listings`/`nav_skill_review` do not re-run (identical to `finalize_from_artifacts`, wave-37 W37-3a); attribution is job-scoped, not output-to-sha.

Placement: the new arm sits **after** the cancel arm and **above** the `execution_status == "FAILED"` arm in `_final_status_ladder` (ladder order is authority order: cancel > real-items > execution verdict > diagnostics).

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_finalize_rescue.py`:

```python
"""[wave-40 T9] One chokepoint at finalize: productive evidence outranks a
stale failure verdict. Prod 770 (19 products, FAILED n=0), 762 (4-17), 765
(36/36 extraction destroyed by a 2-strike abort) all died at finalize.
_finalize_job(job) reads state from the langgraph checkpoint — the tests
freeze that read; no signature change."""

import json

import pytest

from scraper import tasks
from scraper.models import ScrapeJob

pytestmark = pytest.mark.django_db


def _freeze_state(monkeypatch, state):
    """Freeze _finalize_job's checkpoint read (tasks.py:1666-1674) to
    `state`. ADAPT the double to the real call chain: read tasks.py first —
    whatever service/function _finalize_job uses to fetch state, patch THAT;
    if it is a module-level helper rather than LangGraphService, patch it."""
    from types import SimpleNamespace

    class _FakeGraph:
        def get_state(self, config):
            return SimpleNamespace(values=state)

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def build_graph(self):
            return _FakeGraph()

        @staticmethod
        def get_config(thread_id):
            return {"configurable": {"thread_id": thread_id}}

    monkeypatch.setattr(tasks, "LangGraphService", _FakeService)


def _fm(monkeypatch, keys, payloads):
    monkeypatch.setattr("src.artifacts.list_keys", lambda prefix="": keys)
    monkeypatch.setattr("src.artifacts.read_text",
                        lambda key: payloads.get(key, ""))
    monkeypatch.setattr("src.artifacts.write", lambda *a, **kw: None)
    monkeypatch.setattr("src.artifacts.exists", lambda key: bool(payloads))


def test_ladder_rescue_outranks_execution_failed():
    st, diag = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0},
        already_terminal=False, was_cancelled=False,
        error_message="Phase-1 discovery crashed: re.error …",
        output_file="", rescue_count=19,
        rescue_file="scrapers/x/output_1.json")
    assert st == ScrapeJob.STATUS_COMPLETED and diag == ""


def test_ladder_never_rescues_a_cancelled_job():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0},
        already_terminal=False, was_cancelled=True,
        error_message="", output_file="", rescue_count=19,
        rescue_file="scrapers/x/output_1.json")
    assert st == ScrapeJob.STATUS_CANCELLED


def test_ladder_leaves_productive_execution_alone():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "SUCCESS", "product_count": 7},
        already_terminal=False, was_cancelled=False,
        error_message="", output_file="scrapers/x/output_1.json",
        rescue_count=19, rescue_file="scrapers/x/output_1.json")
    assert st == ScrapeJob.STATUS_COMPLETED   # normal path, no rescue involvement


def test_ladder_holds_the_min_count_line():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0,
         "input_mode": "navigation"},
        already_terminal=False, was_cancelled=False, error_message="",
        output_file="", rescue_count=2, rescue_file="scrapers/x/output_1.json")
    assert st != ScrapeJob.STATUS_COMPLETED   # 2 < 3 for navigation mode


def test_never_credits_completed_with_zero():
    st, _ = tasks._final_status_ladder(
        {"execution_status": "FAILED", "product_count": 0},
        already_terminal=False, was_cancelled=False, error_message="boom",
        output_file="", rescue_count=0, rescue_file="")
    assert st == ScrapeJob.STATUS_FAILED      # invariant stands


def test_770_finalize_completes_with_19_from_fm_output(monkeypatch, tmp_path):
    from django.conf import settings
    from django.utils import timezone
    job = ScrapeJob.objects.create(
        url="https://www.sephora.cz/",   # slug "sephora-cz" via _generate_slug
        status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=2))
    key = "scrapers/sephora-cz/output_2026-09-20_235455_000001_99.json"
    _fm(monkeypatch, [key], {key: json.dumps(
        {"products": [{"title": f"p{i}", "price": "9",
                       "url": f"https://x/p/{i}"} for i in range(19)]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "workspace" / "sephora-cz").mkdir(parents=True)
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "last_tested_draft_fp": "abc", "input_mode": "navigation",
        "error_message": "noop draft twice"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED
    assert job.product_count == 19
    assert "noop draft" not in (job.error_message or "")


def test_769_still_fails_honestly_when_only_prior_job_evidence_exists(
        monkeypatch, tmp_path):
    from django.conf import settings
    from django.utils import timezone
    job = ScrapeJob.objects.create(
        url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(minutes=30))
    _fm(monkeypatch, [], {})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "no_fresh_output": True, "input_mode": "navigation",
        "error_message": "DISCOVERY_ZERO"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_FAILED   # prior-job evidence is not this job's


def test_promotion_skips_an_uncompilable_draft(monkeypatch, tmp_path):
    """762 lesson: the crashing draft must never be promoted. compile() only
    catches SyntaxError — pair it with draft_call_violation when importable."""
    from django.conf import settings
    from django.utils import timezone
    job = ScrapeJob.objects.create(
        url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=1))
    draft_key = f"scrapers/x/jobs/scraper-{job.id}.py"
    out_key = "scrapers/x/output_2026-09-20_040000_000001_1.json"
    _fm(monkeypatch, [draft_key, out_key], {
        draft_key: "def broken(:\n    pass\n",   # SyntaxError
        out_key: json.dumps({"products": [
            {"title": "p", "price": "1", "url": "https://x/p/1"}]})})
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    _freeze_state(monkeypatch, {
        "execution_status": "FAILED", "product_count": 0,
        "input_mode": "url_list", "error_message": "crashed"})
    tasks._finalize_job(job)
    job.refresh_from_db()
    assert job.status == ScrapeJob.STATUS_COMPLETED   # items still rescued...
    promoted = tmp_path / "scrapers" / "x" / "scraper.py"
    assert not promoted.exists()                       # ...but nothing promoted
```

- [ ] **Step 2: Run, verify RED** — TypeError (`_final_status_ladder` lacks kwargs) / status FAILED.
- [ ] **Step 3: Implement**

Ladder arm (after the cancel arm, above the FAILED arm) — the kwargs already shown in the tests:

```python
    # [wave-40 T9] Job-scoped real-items evidence outranks a stale failure
    # verdict. Fires ONLY when the state claims zero items and this job
    # demonstrably produced >= min_count good rows (T8 evidence, started_at
    # window + draft provenance). input_mode arrives inside the state dict
    # _finalize_job already passes down. Never fires for cancelled jobs;
    # never credits a zero (count > 0 is structural).
    if (
        rescue_count > 0
        and not was_cancelled
        and int((state or {}).get("product_count") or 0) == 0
        and rescue_count >= _rescue_min_count(
            (state or {}).get("input_mode") or "navigation")
    ):
        return ScrapeJob.STATUS_COMPLETED, ""
```

(ADAPT the state parameter name to `_final_status_ladder`'s real first parameter.)

`_finalize_job` wiring: compute `(count, locator) = _real_items_evidence(slug, job, state)` **before** the publish block (workspace mtimes must still be live), let publish/rmtree proceed, resolve the FM key for `locator`, then pass `rescue_count=count, rescue_file=<fm key>` into `_final_status_ladder`; on a rescue COMPLETED set `job.product_count = count`, `job.output_file = <fm key>` (the `:1917-1924` rule then scrubs the stale error), and run the compile-gated promotion described in Interfaces.
- [ ] **Step 4: Run, verify GREEN** — new file + `../tests/test_wave37_honest_finalize.py` (the wave-37 ladder semantics must not move) + `../tests/test_wave26_terminal_cleanup_invariant.py`.
- [ ] **Step 5: Commit** — `feat(wave-40): finalize real-items rescue chokepoint + compile-gated promotion (T9)`

### Task 10: Restore-before-refuse + publish the draft at finalize

**Files:**
- Modify: `webapp/agents/nodes/run_execution.py:693-698` — the missing-draft refusal. **Verified shape: it RETURNS a state-update dict (it does not raise).**
- Modify: `webapp/scraper/tasks.py:1591-1597` (`_publish_analysis_artifacts` — add the draft)
- Test: `tests/test_wave40_draft_restore_exec.py` (new)

**Interfaces:**
- Consumes: `draft_safety.restore_job_draft(root: str, slug: str, job_id)` (`draft_safety.py:319`) — already used by tester (`graph.py:8489-8497`) and writer (`graph.py:7221`), with a third caller `setup_workspace._restore_job_draft_from_fm` (`:176/:288`); restores per-job key → `-good` twin; consult its docstring for the exact return contract at implement time (returns the restored path/source, `None` semantics documented there — the tests assert on FILE EXISTENCE and on the refusal dict, not on the return value).
- Produces: job 807's exact shape (PASS 0.94 + 25 products, draft vanished mid-run → "scraper_draft.py not found" → FAILED) becomes a COMPLETED: execution restores the draft from the FM archive instead of refusing, and finalize publishes the draft alongside the five analysis JSONs so even a later workspace loss leaves the FM holding the executable.
- New seam: `_ensure_draft_or_restore(root: str, slug: str, job_id: int, ws: Path) -> dict | None` extracted from the `:693-698` refusal — `None` = draft present or restored (execution proceeds); `dict` = the honest refusal update, returned ONLY after `restore_job_draft` fails (message byte-identical to today's).

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_draft_restore_exec.py`:

```python
"""[wave-40 T10] run_execution must attempt draft restore before refusing
(prod 807: papier, PASS 0.94 + 25 products, draft vanished mid-run, job
FAILED 'scraper_draft.py not found'), and finalize must publish the draft."""

import importlib

import pytest

pytestmark = pytest.mark.django_db


def _re_mod():
    # agents.nodes.__init__ re-exports the run_execution FUNCTION over the
    # submodule — attribute access gives a function, not the module.
    return importlib.import_module("webapp.agents.nodes.run_execution")


def test_missing_draft_is_restored_from_fm_archive(tmp_path, monkeypatch):
    re_mod = _re_mod()
    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    calls = {}

    def fake_restore(root, slug, job_id):
        calls["args"] = (root, slug, job_id)
        (ws / "scraper_draft.py").write_text("# restored\n")
        return str(ws / "scraper_draft.py")

    monkeypatch.setattr("agents.draft_safety.restore_job_draft", fake_restore)
    out = re_mod._ensure_draft_or_restore(str(tmp_path), "papier", 807, ws)
    assert calls["args"] == (str(tmp_path), "papier", 807)
    assert (ws / "scraper_draft.py").exists()
    assert out is None   # no refusal — execution proceeds


def test_refusal_only_after_restore_fails(tmp_path, monkeypatch):
    re_mod = _re_mod()
    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    monkeypatch.setattr("agents.draft_safety.restore_job_draft",
                        lambda *a, **kw: None)
    out = re_mod._ensure_draft_or_restore(str(tmp_path), "papier", 807, ws)
    assert isinstance(out, dict)      # the honest refusal REMAINS (no silent pass)
    assert not (ws / "scraper_draft.py").exists()


def test_finalize_publishes_the_draft(tmp_path, monkeypatch):
    """_publish_analysis_artifacts (tasks.py:1591-1597) must also publish
    workspace/{slug}/scraper_draft.py -> FM, so a later workspace loss can
    still be restored (the T10 half of the 807 fix)."""
    from django.conf import settings
    from scraper import tasks as t
    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft\n")
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    published = {}
    monkeypatch.setattr("src.artifacts.write",
                        lambda key, text: published.__setitem__(key, text))
    t._publish_analysis_artifacts("papier", 807)   # ADAPT to the real signature
    assert any("scraper-draft-807" in k for k in published)
```

*(ADAPT: `_publish_analysis_artifacts`' real signature at `tasks.py:1591` — keep the assertion (a `scraper-draft-{job_id}` key is published); do NOT promote to `scrapers/{slug}/scraper.py` here — promotion stays T9's compile-gated job.)*

- [ ] **Step 2: Run, verify RED.**
- [ ] **Step 3: Implement** — extract `_ensure_draft_or_restore` per Interfaces and call it from the `:693-698` site (refusal dict returned only when restore also fails); extend `_publish_analysis_artifacts` to also publish `workspace/{slug}/scraper_draft.py` → `scrapers/{slug}/jobs/scraper-draft-{job_id}.py` (~5 lines, guarded by exists()).
- [ ] **Step 4: Run, verify GREEN** — new file + the existing restore suite (grep `restore_job_draft` under tests/).
- [ ] **Step 5: Commit** — `fix(wave-40): restore draft before refusing execution; publish draft at finalize (T10)`

### Task 11: Slug-scoped finalize liveness guard + tombstone rename (cross-job class)

**Files:**
- Create: `webapp/agents/invocation_registry.py` (~60 lines)
- Modify: `webapp/scraper/tasks.py` — register/unregister in `run_scrape_task` (`~:564`) and the resume path (`~:715`); extract `_maybe_delete_workspace(job, ws) -> str` from the rmtree tail (`:1810-1822`); `purge_retention` (`:2435` area) gains the `_trash` sweep
- Modify: `webapp/agents/nodes/check_tracker.py:56/:76` and `webapp/agents/nodes/setup_workspace.py:96` — the other workspace-wipe sites consult the same guard
- Modify: `webapp/scraper/job_health_agent.py:111-117` (`_workspaces` — filter `_`-prefixed dirs)
- Test: `tests/test_wave40_finalize_liveness_gate.py` (new)

**Interfaces:**
- Produces (`webapp/agents/invocation_registry.py`, process-local, stdlib-only so nodes and tasks can both import it):
  - `register(slug: str, job_id: int) -> str` — returns a generation token; called at **TASK START** (`run_scrape_task` and the resume path), so every in-flight generation is counted, including soft-limit deaths raised in the task thread (`:482`/`:769`).
  - `unregister(slug: str, job_id: int, token: str) -> None` — discards exactly the token; called in the task's **outermost finally AFTER finalize** (so it cannot be relied on at delete time — hence the read-and-clear below).
  - `register_abandoned(slug: str, job_id: int) -> None` — called at the agent-abandonment sites (`graph.py:2951` sync timeout, `:2696` async); adds an entry NOTHING discards: an abandoned daemon thread keeps walking after its task's token is unregistered, and its registration must outlive the task (conservative until process restart / the `_trash`-style purge).
  - `clear_own(slug: str, job_id: int, token: str) -> None` — the read-and-clear `_maybe_delete_workspace` performs BEFORE the liveness check (this generation's own walk is finished — finalize is post-graph). Token propagation: `register()` stores the returned token in a `contextvars.ContextVar` set at task start, so `_maybe_delete_workspace(job, ws)` reads the current generation's token implicitly — abandoned entries (separate store) are NOT clearable this way and keep blocking.
  - `alive_for_slug(slug: str) -> bool` — any remaining entry for the slug; **must NOT special-case job_ids** (the 765 zombie shares the resumed job's id — an entry under the same `(slug, job_id)` from an abandoned generation must still count).
  - `delete_blocked_reason(slug: str, exclude_job_id: int | None = None) -> str` — the shared guard: `""` = may delete; else the reason. Combines (a) `_`-namespace refusal (a slug starting with `_` is system namespace — never auto-managed), (b) `alive_for_slug`, (c) the DB sibling check (same slug via `_generate_slug`-equivalent slugs, statuses RUNNING/PENDING/WAITING_APPROVAL, excluding `exclude_job_id`). The DB check is what the current `:1810-1815` URL-scoped filter should have been.
  - `_maybe_delete_workspace(job, ws) -> str` — `"kept"` (guard blocked), `"tombstoned"` (rename to `workspace/_trash/{slug}-{job_id}-{ts}/` — same-filesystem rename, no data loss; the zombie's `os.makedirs` then recreates a fresh empty workspace harmlessly), or `"deleted"`. Order: `clear_own` → `delete_blocked_reason` → blocked? tombstone : (env `FINALIZE_WORKSPACE_DELETE=0` ? tombstone : delete).
- Guard wiring: `_maybe_delete_workspace` replaces the raw rmtree in `_finalize_job`; `check_tracker.py:56/:76` and `setup_workspace.py:96` route their wipes through `delete_blocked_reason` (tombstone on block, not silent skip); `purge_retention` sweeps `_trash` entries older than its existing retention window.
- Namespace guard: `job_health_agent._workspaces` (`:111-117`) filters `_`-prefixed dirs so `_trash` never shows up as a site workspace.
- **Railway-ephemeral honesty (residual R9):** a container restart deletes workspaces with no process alive to consult — the registry cannot see that class. T11 guards the same-process zombie class (765) and the concurrent-sibling class (783/790 accessorize); the 807 mechanism (candidates: finalize-rmtree racing a zombie vs container restart) is narrowed, not proven — the Railway restart timeline is the missing evidence.

Verified mechanism (planning agent F, all line-verified): `_invoke_agent_with_timeout` (`graph.py:2928-2951`) abandons daemon threads on timeout; the zombie latch only guards tool ENTRY (`subagents.py:1301-1302`), so a writer inside `run_scraper`'s body keeps running; that body persists output with `os.makedirs` (`shell_tools.py:706-726`) — recreating a deleted workspace; `_finalize_job` rmtrees at `:1822` behind the URL-scoped, slug-blind, self-excluding guard `:1810-1815`; no liveness flag exists (`graph_thread_id` written `:552`, never read); the watchdog never calls `_finalize_job`. Prod pairs: 765 (nike.in) and 807 (papier) — plus 783/790 (accessorize) which ran concurrently in prod, the slug-collision case the URL guard cannot see.

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_finalize_liveness_gate.py`:

```python
"""[wave-40 T11] finalize may not delete a workspace a live/queued sibling
job (same slug) still needs — and a job's own registration must not block
its own cleanup. Prod 765+807: workspace vanished mid-run; the URL-scoped
guard (tasks.py:1810-1815) is slug-blind."""

import pytest

from scraper.models import ScrapeJob

PAPIER = "https://www.papier.com/x"   # _generate_slug -> "papier-com"


def _reg():
    from agents import invocation_registry as reg
    reg._entries().clear()             # ADAPT: whatever resets the process-local store
    return reg


def test_registry_roundtrip_and_abandoned_outlives_unregister():
    reg = _reg()
    assert reg.alive_for_slug("nike-in") is False
    tok = reg.register("nike-in", 765)
    assert reg.alive_for_slug("nike-in") is True
    reg.unregister("nike-in", 765, tok)
    assert reg.alive_for_slug("nike-in") is False
    # an ABANDONED walk shares the job_id and outlives the task token:
    reg.register_abandoned("nike-in", 765)
    reg.unregister("nike-in", 765, "stale-token")   # task's own finally
    assert reg.alive_for_slug("nike-in") is True    # zombie still counted


def test_trash_namespace_is_refused():
    reg = _reg()
    assert reg.delete_blocked_reason("_trash", exclude_job_id=None) != ""


@pytest.mark.django_db
def test_finalize_tombstones_when_sibling_same_slug_is_running(
        db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import tasks as t
    reg = _reg()
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    other = ScrapeJob.objects.create(url="https://www.papier.com/y",
                                     status=ScrapeJob.STATUS_RUNNING)
    reg.register_abandoned("papier-com", other.id)
    ws = tmp_path / "workspace" / "papier-com"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft")
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    out = t._maybe_delete_workspace(mine, ws)
    assert out == "tombstoned"
    assert not ws.exists()
    trash = tmp_path / "workspace" / "_trash"
    assert trash.exists() and any(trash.iterdir())


@pytest.mark.django_db
def test_own_registration_does_not_block_own_delete(db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import tasks as t
    reg = _reg()
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    tok = reg.register("papier-com", mine.id)
    ws = tmp_path / "workspace" / "papier-com"
    ws.mkdir(parents=True)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    out = t._maybe_delete_workspace(mine, ws)
    assert out == "deleted"            # read-and-clear cleared our own entry
    assert not ws.exists()
    reg.unregister("papier-com", mine.id, tok)


@pytest.mark.django_db
def test_kill_switch_always_tombstones(db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import tasks as t
    _reg()
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    ws = tmp_path / "workspace" / "papier-com"
    ws.mkdir(parents=True)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("FINALIZE_WORKSPACE_DELETE", "0")
    assert t._maybe_delete_workspace(mine, ws) == "tombstoned"
```

*(ADAPT: `_maybe_delete_workspace(job, ws) -> str` is the extraction this task performs — the tests above are its contract. The wipe sites in `check_tracker.py:56/:76` and `setup_workspace.py:96` get a source-pin assertion added here: their module source must reference `delete_blocked_reason`.)*

```python
def test_other_wipe_sites_consult_the_guard():
    import agents.nodes.check_tracker as ct   # ADAPT binding: sys.modules idiom
    import agents.nodes.setup_workspace as sw
    import inspect
    assert "delete_blocked_reason" in inspect.getsource(ct)
    assert "delete_blocked_reason" in inspect.getsource(sw)
```

- [ ] **Step 2: Run, verify RED.**
- [ ] **Step 3: Implement** — the registry module per Interfaces; `register` at task start in both `run_scrape_task` and the resume path, token passed down and `unregister`ed in the outermost finally AFTER finalize; `register_abandoned` at the two timeout paths (`graph.py:2951`/`:2696`); `_maybe_delete_workspace` extraction (clear_own → delete_blocked_reason → tombstone/delete) wired into `_finalize_job` replacing the raw rmtree; the two node wipe sites routed through the guard; `_trash` sweep in `purge_retention`; `job_health_agent._workspaces` filters `_`-prefixed dirs. Slug strings come from `tasks._generate_slug` (`:1112`) — there is no `ScrapeJob.site_slug` field.
- [ ] **Step 4: Run, verify GREEN** — new file + `../tests/test_artifact_copy_guards.py` (the neighboring finalize guards) + `../tests/test_wave35_retention.py` (purge contract gains the `_trash` sweep without breaking its pins).
- [ ] **Step 5: Commit** — `feat(wave-40): slug-scoped finalize liveness guard + tombstones (T11)`

### Task 12: Opt-in writer progress escalation, gated on the tester-stamped draft fingerprint (flag OFF by default)

**Files:**
- Modify: `webapp/agents/graph.py` — the 2-strike abort block (`:7090-7174`) and the window clamp via the real `_effective_timeout(phase_timeout, job_deadline, now)` helper (`graph.py:2742-2773`; `JOB_BUDGET_FINALIZE_MARGIN = 360.0` at `:2739`)
- Modify: `webapp/agents/state.py` (one more declared channel: `writer_escalation_used: bool` — same T1 pattern)
- Test: `tests/test_wave40_writer_progress_escalation.py` (new, extending the harness of `tests/test_wave22_writer_timeout_accounting.py`; window semantics from `webapp/tests/test_wave30_writer_window.py`)

**Interfaces:**
- Consumes (read-only): `last_tested_draft_fp` (`state.py:117`, **tester-stamped at `graph.py:8710`** — survives even when execution never ran), `noop_fix_cycles` (`:196-201`), `draft_mutated_during_test` (`:132-141`).
- **Gate signal (critique fix):** `last_tested_draft_fp`, NOT `tested_draft_sha256` — the sha is written only by `_stamp_tested_draft` at execution launch (`run_execution.py:131-143` ← `graph.py:9309-9311`), so on the 765 shape (abort fired before execution) it is empty and rev-1's gate could never fire on its own motivation job. The fingerprint is stamped by the tester on every converging test cycle.
- Produces: `_writer_escalation_window(state, job_deadline, now=None) -> int | None`. When `WRITER_PROGRESS_ESCALATION=1` (default `0` — OFF) and the abort is about to fire while the run shows **verifiable progress** (`last_tested_draft_fp` set AND `draft_mutated_during_test` False AND `noop_fix_cycles == 0`) and `writer_escalation_used` is False: return ONE granted window = the writer's wall-clock window doubled, clamped through `_effective_timeout(window, job_deadline, now)` so it can never exceed the job's remaining budget; the caller skips the abort, re-invokes the writer with the granted window, stamps a `[WRITER-PROGRESS-ESCALATION]` agent-log row, and sets `writer_escalation_used: True`. Flag OFF = byte-identical behavior to today. A second strike never escalates.
- **Prior-art caution (memory `writer-read-spiral-rca` / docs/code-writer-context-ballooning.md):** budget changes previously ballooned writer context and were reverted. This is why the default is OFF and the grant is capped at 1 — the flag exists so the 765 class (abort fired while cycle-3 was converging at 36/36) can be enabled in a controlled prod experiment, not shipped blind.

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_writer_progress_escalation.py`:

```python
"""[wave-40 T12] WRITER_PROGRESS_ESCALATION: one capped, clamped extra writer
window when the run shows verifiable convergence (prod 765: cycle-3 PASS
36/36 destroyed by the 2-strike abort). Gate signal is the tester-stamped
last_tested_draft_fp — tested_draft_sha256 is EMPTY on exactly these runs
(it is only written at execution launch). Default OFF."""

from datetime import timedelta

from django.utils import timezone

from agents import graph


def _deadline(seconds):
    return timezone.now() + timedelta(seconds=seconds)


def _progress_state(**over):
    st = {"job_id": 0, "site_slug": "nike-in", "test_retry_count": 2,
          "last_tested_draft_fp": "fp-abc123",
          "draft_mutated_during_test": False,
          "noop_fix_cycles": 0, "writer_wall_clock_timeouts": 2,
          "writer_escalation_used": False}
    st.update(over)
    return st


def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "0")
    assert graph._writer_escalation_window(
        _progress_state(), _deadline(600)) is None


def test_flag_on_grants_one_doubled_window(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    win = graph._writer_escalation_window(_progress_state(), _deadline(600))
    assert win and win > 0


def test_gate_signal_is_last_tested_draft_fp(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    # the 765 shape: execution never ran -> no sha, but the tester stamped
    # the fingerprint on the converging cycle-3
    assert graph._writer_escalation_window(
        _progress_state(tested_draft_sha256=""), _deadline(600)) is not None
    assert graph._writer_escalation_window(
        _progress_state(last_tested_draft_fp=""), _deadline(600)) is None


def test_no_progress_signals_no_grant(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    assert graph._writer_escalation_window(
        _progress_state(draft_mutated_during_test=True), _deadline(600)) is None
    assert graph._writer_escalation_window(
        _progress_state(noop_fix_cycles=2), _deadline(600)) is None


def test_grant_clamped_by_remaining_job_budget(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    win = graph._writer_escalation_window(_progress_state(), _deadline(30))
    assert 0 < win <= 30    # _effective_timeout clamp, JOB_BUDGET_FINALIZE_MARGIN


def test_second_strike_never_escalates(monkeypatch):
    monkeypatch.setenv("WRITER_PROGRESS_ESCALATION", "1")
    assert graph._writer_escalation_window(
        _progress_state(writer_escalation_used=True), _deadline(600)) is None
```

- [ ] **Step 2: Run, verify RED** (helper absent → AttributeError).
- [ ] **Step 3: Implement** — `_writer_escalation_window` per Interfaces; declare `writer_escalation_used: bool` in `state.py`; wire the abort block (`:7090-7174`) so a non-None result skips the abort, re-invokes the writer with the granted window, stamps `[WRITER-PROGRESS-ESCALATION]`, and sets the latch.
- [ ] **Step 4: Run, verify GREEN** — new file + `../tests/test_wave22_writer_timeout_accounting.py` + `webapp/tests/test_wave30_writer_window.py` (existing window accounting must not move with the flag off).
- [ ] **Step 5: Commit** — `feat(wave-40): opt-in writer progress escalation gated on tester fingerprint, default off (T12)`

---

# Phase 5 — Discovery checkpoint reuse (T13–T15, armed entirely in run_execution)

> **Rev-2 architecture note:** rev-1 armed reuse in `browser_service/scraper_runner.py`. The critique proved that arm was **dead code**: `--fresh-discovery` is appended unconditionally for phase-1 modes (`_wants_fresh_discovery` `run_execution.py:530-536`, append `:883-884`), so even with the env set the template re-discovered from scratch. Rev-2 arms reuse on the run_execution side (env in `_run_in_process`'s env dict `:1347-1355` and in the `/scrape` payload via the existing env-forwarding mechanism) and **gates the `--fresh-discovery` append** when reuse is armed. **No `browser_service/` file is touched** — no import trap, no deploy skew. Verification obligation: confirm at implement time that `/scrape` forwards payload env to the subprocess (grep `env_overrides` in `browser_service/server.py` — this mechanism already exists per wave-22/24); if it does NOT, STOP and surface to the user rather than adding a browser_service feature.

### Task 13: Checkpoint validation/merge helpers in `src/listing_discovery.py`

**Files:**
- Modify: `src/listing_discovery.py` (append)
- Test: `tests/test_wave40_checkpoint_reuse.py` (new, pure unit)

**Interfaces:**
- Produces:
  - Constants: `CHECKPOINT_FILENAME = "discovered_urls_checkpoint.json"`, `CHECKPOINT_REUSE_ENV = "SCRAPER_CHECKPOINT_REUSE"`, `CHECKPOINT_MIN_URLS_ENV = "SCRAPER_CHECKPOINT_MIN_URLS"`, `CHECKPOINT_MAX_AGE_S_ENV = "SCRAPER_CHECKPOINT_MAX_AGE_S"`, `DEFAULT_MAX_AGE_S = 86400` (**1 day — rev-1's 604800 would have reused a week-stale URL set; critique finding**), `CHECKPOINT_REUSE_MARKER = "[DISCOVERY-CHECKPOINT-REUSED]"`, `CHECKPOINT_REUSED_STOP_REASON = "checkpoint_reused"`.
  - `zero_discovery_rescuable(stop_reason: str) -> bool` — False ONLY for `navigate_unavailable` (an infra verdict must never be papered over with old URLs).
  - `load_discovery_checkpoint(path: str, input_mode: str, host: str) -> dict` — `{"urls": list[str], "reason": str, "raw_count": int, "dropped": int, "age_s": float | None, "capped": bool}`; `reason ∈ {ok, absent, disabled, unparseable, empty, stale, below_floor, filtered_empty}` (payload shape `{"urls", "count", "ts"}` — written by `templates/http_navigation_scraper.py:296-341/:1589/:1627/:1640` and `templates/navigation_scraper.py:116-160/:1010/:1038/:1058`); disabled = `SCRAPER_CHECKPOINT_REUSE=0`; stale = older than the max-age env (default `DEFAULT_MAX_AGE_S`); below_floor = fewer than `SCRAPER_CHECKPOINT_MIN_URLS` (floor reuses `ZERO_YIELD_JUNK_LINKS` semantics); **`host` is REQUIRED for the same-host filter** (rev-1 omitted it — the filter silently passed everything; critique finding); max-URL cap reuses the module's existing caps.
  - `checkpoint_coverage_patch(fresh_coverage: dict, reused: dict) -> dict` — merges reused-URL coverage into the fresh attempt's coverage dict, preserving the fresh verdict under `fresh_stop_reason`; top-level `stop_reason` becomes `checkpoint_reused` so honest-fail sets are unaffected.

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_checkpoint_reuse.py`:

```python
"""[wave-40 T13] checkpoint reuse helpers. Prod 769: tester discovered 20
products; execution re-ran discovery hours later, got 0 (empty_first_page),
exit 3 DISCOVERY_ZERO, job FAILED with the checkpoint sitting unread in the
workspace."""

import json
import time

from src.listing_discovery import (
    CHECKPOINT_FILENAME, CHECKPOINT_REUSED_STOP_REASON,
    checkpoint_coverage_patch, load_discovery_checkpoint,
    zero_discovery_rescuable)


def _write_checkpoint(tmp_path, urls, age_s=3600):
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text(json.dumps({"urls": urls, "count": len(urls),
                             "ts": time.time() - age_s}))
    return p


def test_rescuable_reasons_and_infra_exclusion():
    assert zero_discovery_rescuable("empty_first_page")
    assert zero_discovery_rescuable("empty_render")
    assert not zero_discovery_rescuable("navigate_unavailable")


def test_happy_path(tmp_path):
    p = _write_checkpoint(tmp_path, [f"https://x.example/p/{i}" for i in range(12)])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "ok" and out["raw_count"] == 12 and len(out["urls"]) == 12


def test_absent_and_unparseable(tmp_path):
    out = load_discovery_checkpoint(str(tmp_path / "nope.json"), "navigation",
                                    host="x.example")
    assert out["reason"] == "absent" and out["urls"] == []
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text("{bad")
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "unparseable"


def test_disabled_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "0")
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "disabled"


def test_stale_checkpoint_rejected_at_one_day_default(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"], age_s=86400 * 2)
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "stale"


def test_below_floor_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_MIN_URLS", "5")
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "below_floor"


def test_cross_host_urls_filtered(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1",
                                     "https://evil.example/p/2"])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert all(u.startswith("https://x.example") for u in out["urls"])
    assert out["reason"] in ("ok", "filtered_empty")


def test_coverage_patch_preserves_fresh_verdict():
    fresh = {"discovered_urls": 0, "stop_reason": "empty_first_page"}
    patched = checkpoint_coverage_patch(fresh, {"urls": ["https://x/p/1"],
                                                "raw_count": 20})
    assert patched["stop_reason"] == CHECKPOINT_REUSED_STOP_REASON
    assert patched["fresh_stop_reason"] == "empty_first_page"
    assert patched["checkpoint_urls"] == 1
```

- [ ] **Step 2: Run, verify RED** — ImportErrors.
- [ ] **Step 3: Implement** the constants + three functions per contract (same-host filter and floor reuse the module's existing helpers — read them first; the host comes from the caller, which derives it the way the templates already derive their seed host).
- [ ] **Step 4: Run, verify GREEN** — new file + the existing `tests/` listing_discovery suite (grep `listing_discovery` under tests/).
- [ ] **Step 5: Commit** — `feat(wave-40): discovery checkpoint validation/merge helpers (T13)`

### Task 14: Template zero-discovery rescue branch (before the DISCOVERY_ZERO exit)

**Files:**
- Modify: `templates/http_navigation_scraper.py` — the zero branch feeding `exit 3` (`:1650-1653` reclass, `:1655-1677` exit) and the coverage merge site (`:1750-1763`)
- Modify: `templates/navigation_scraper.py` — same branch at its exit (`:1010-:1058` region)
- Test: `tests/test_wave40_checkpoint_template.py` (new)

**Interfaces:**
- Consumes: T13 helpers. **Imports are LAZY — inside the rescue branch** (`from src.listing_discovery import ...` at point of use): the browser-service image may briefly run an older `src/` than the django/celery image (benign deploy skew) — an import-time failure must fall through to the honest exit, not crash the template.
- Produces: on a rescuable zero discovery (Phase 1 found 0, `stop_reason` rescuable, checkpoint loads with `reason == "ok"`, **and** `not args.discover_only`), the template loads the checkpoint URLs into the discovery seed list, runs Phase 2 over them, stamps `[DISCOVERY-CHECKPOINT-REUSED]` in the run log, and emits coverage with `stop_reason = checkpoint_reused` + `fresh_stop_reason` preserved. `--fresh-discovery` and `SCRAPER_LISTING_URL` CLI behavior are UNCHANGED at the template level (the runner simply stops *sending* the flag when reuse is armed — T15). The job-77 guard (refuse 0-overwrite checkpoint writes, `:296-341` region) is untouched. On ANY exception in the branch: fall through to the original `exit 3` (honest zero preserved).

Critique fix (test-reality): rev-1's subprocess test plan assumed a fixture site harness that doesn't exist for templates. Rev-2 pins the branch with non-vacuous source-contract tests (compile + regex-sliced region asserts) and defers the behavioral proof to T16's local dead-seed gate.

- [ ] **Step 1: Write the failing test** — `tests/test_wave40_checkpoint_template.py`:

```python
"""[wave-40 T14] both phase-1 templates carry a checkpoint rescue branch
between the zero-yield reclass and the DISCOVERY_ZERO exit; imports are lazy
(benign browser-service deploy skew); the honest exit is preserved."""

import re


def _tpl(name):
    with open(f"templates/{name}", encoding="utf-8") as fh:
        src = fh.read()
    compile(src, name, "exec")          # sanity: still valid Python
    return src


def _rescue_region(src):
    m = re.search(r"zero_discovery_rescuable\(", src)
    assert m, "rescue branch absent"
    return src[m.start(): m.start() + 2500]


def test_http_navigation_template_has_rescue_branch():
    region = _rescue_region(_tpl("http_navigation_scraper.py"))
    assert "load_discovery_checkpoint" in region
    assert "checkpoint_coverage_patch" in region
    assert "[DISCOVERY-CHECKPOINT-REUSED]" in region
    assert "discover_only" in region                      # --discover-only exempt
    assert region.index("from src.listing_discovery import") >= 0  # lazy import


def test_navigation_template_has_rescue_branch():
    region = _rescue_region(_tpl("navigation_scraper.py"))
    assert "load_discovery_checkpoint" in region
    assert "[DISCOVERY-CHECKPOINT-REUSED]" in region


def test_honest_zero_exit_is_preserved():
    for name in ("http_navigation_scraper.py", "navigation_scraper.py"):
        src = _tpl(name)
        rescue = _rescue_region(src)
        # the original DISCOVERY_ZERO exit must still exist AFTER the branch
        tail = src[src.index(rescue[:40]) + len(rescue):]
        assert re.search(r"(exit\(3\)|sys\.exit\(3\))", tail) or \
            re.search(r"(exit\(3\)|sys\.exit\(3\))", src)
```

*(ADAPT the exit-pattern regex to how each template actually performs the exit 3 — read `http_navigation_scraper.py:1655-1677` and `navigation_scraper.py:1010-1058` first. The assertions that matter: branch present, lazy import inside it, marker stamped, honest exit still reachable.)*

- [ ] **Step 2: Run, verify RED** (rescue branch absent).
- [ ] **Step 3: Implement** — in both templates, between the zero-yield reclass and the `exit 3`, insert the rescue branch (≤ 25 lines): `if not args.discover_only and zero_discovery_rescuable(stop_reason):` → lazy-import + `load_discovery_checkpoint(checkpoint_path, phase1_mode, host=...)` → on `reason == "ok"`: set the URL list from the checkpoint, log the marker, `discovery_coverage.update(checkpoint_coverage_patch(discovery_coverage, ckpt))` (merge site `:1750-1763`), skip the exit, proceed into Phase 2. Wrap the whole branch in try/except → fall through to the original exit.
- [ ] **Step 4: Run, verify GREEN** + run the local dead-seed harness for these templates if present (grep `dead_seed`/`sentinel` under `tests/` and `scripts/` — the wave-36 14-site gate is the behavioral proof).
- [ ] **Step 5: Commit** — `feat(wave-40): template checkpoint rescue before DISCOVERY_ZERO exit (T14)`

### Task 15: run_execution arms checkpoint reuse + docs contract amendment

**Files:**
- Modify: `webapp/agents/nodes/run_execution.py` — new `_checkpoint_reuse_env(state, workspace_folder) -> dict`; env merge in `_run_in_process` (`:1347-1355`) and the `/scrape` payload env; staging tuple (`:1625-1634`) gains `discovered_urls_checkpoint.json` staged **verbatim** (no `filter_seed_payload` — the reader filters); `--fresh-discovery` append gate at `:883-884` (predicate `_wants_fresh_discovery` `:530-536`)
- Modify: `docs/discovery-coverage-gate-contract.md` §2/§3/§4 — document the `checkpoint_reused` stop reason, its honesty guarantees, and that the probe path never sees checkpoints
- Test: `tests/test_wave40_checkpoint_staging.py` (new)

**Interfaces:**
- Consumes: T13/T14. The probe staging tuple (`graph.py:8065-8074`) is **explicitly untouched** — discovery probes must never see checkpoints (that's what kept 763 nastygal honest).
- Reuse-armed predicate: `input_mode ∈ phase-1 modes` AND `workspace/discovered_urls_checkpoint.json` exists AND not `--discover-only`/force-full AND `os.getenv("SCRAPER_CHECKPOINT_REUSE", "0") != "0"` → env `{"SCRAPER_CHECKPOINT_REUSE": "1"}`. When armed, the `--fresh-discovery` append (`:883-884`) is SKIPPED (this is the rev-1 contradiction fix — without it the rescue is unreachable).
- Produces: the phase-1 execution run receives the env + the checkpoint file; the template's rescue branch (T14) does the rest.

- [ ] **Step 1: Write the failing tests** — `tests/test_wave40_checkpoint_staging.py`:

```python
"""[wave-40 T15] reuse is armed on the run_execution side: env for the
subprocess, checkpoint in the staging tuple, --fresh-discovery gated when
armed — and the probe path stays checkpoint-free."""

import importlib
import inspect
import re


def _re_mod():
    # module-shadowing idiom — see Global Constraints
    return importlib.import_module("webapp.agents.nodes.run_execution")


def _fn_src(src, name):
    m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, f"{name} not found"
    return m.group(0)


def _ckpt(tmp_path):
    p = tmp_path / "discovered_urls_checkpoint.json"
    p.write_text('{"urls": ["https://x/p/1"], "count": 1, "ts": 0}')
    return p


def test_arm_sets_env_for_phase1_mode_with_checkpoint(tmp_path, monkeypatch):
    re_mod = _re_mod()
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    _ckpt(tmp_path)
    env = re_mod._checkpoint_reuse_env({"input_mode": "navigation"},
                                       str(tmp_path))
    assert env == {"SCRAPER_CHECKPOINT_REUSE": "1"}


def test_no_checkpoint_no_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation"}, str(tmp_path)) == {}


def test_url_list_mode_never_arms(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    _ckpt(tmp_path)
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "url_list"}, str(tmp_path)) == {}


def test_kill_switch_blocks_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "0")
    _ckpt(tmp_path)
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation"}, str(tmp_path)) == {}


def test_staging_tuple_includes_the_checkpoint():
    src = inspect.getsource(_re_mod())
    assert "discovered_urls_checkpoint.json" in src


def test_probe_phase1_path_stays_checkpoint_free():
    re_mod = _re_mod()
    src = inspect.getsource(re_mod)
    probe = _fn_src(src, "_probe_phase1_discovery_once")  # ADAPT location: grep
    assert "checkpoint" not in probe.lower()


def test_fresh_discovery_append_gates_on_reuse():
    src = inspect.getsource(_re_mod())
    m = re.search(r'"--fresh-discovery"', src)
    assert m, "append site moved — re-locate (:883-884)"
    region = src[max(0, m.start() - 600): m.end() + 200]
    # the append must sit behind the reuse-armed predicate, not bare
    assert "_checkpoint_reuse_env" in region or "checkpoint" in region.lower()
```

- [ ] **Step 2: Run, verify RED.**
- [ ] **Step 3: Implement** — `_checkpoint_reuse_env` per Interfaces; merge into `_run_in_process`'s env dict (`:1347-1355`) and the `/scrape` payload env (verify the forwarding exists — see the Phase-5 architecture note; STOP if not); append the checkpoint to the staging tuple (`:1625-1634`) verbatim; gate the `--fresh-discovery` append; docs amendment per the probe-exclusion rule + `checkpoint_reused` honesty guarantees (fresh verdict preserved; `listing_yield_failure` unaffected — `found_n != 0` short-circuits at `route_after_testing.py:135`; `stop_reason` is in neither fail set, `:135-157`).
- [ ] **Step 4: Run, verify GREEN** — new file + the existing run_execution test files (grep `fresh_discovery|_run_in_process` under tests/).
- [ ] **Step 5: Commit** — `feat(wave-40): arm checkpoint reuse in run_execution + contract docs (T15)`

---

### Task 16: Full-suite verification + restart + wave memory

- [ ] **Step 1:** `docker compose exec django sh -c "cd /app/webapp && pytest ../tests ."` — expect the known-pre-existing reds only (suite was 3345+4 at wave-38; anything NEW failing is a regression to fix before proceeding).
- [ ] **Step 2:** `docker compose exec django ruff check webapp/ src/` + `ruff format webapp/ src/`.
- [ ] **Step 3:** Restart once for the whole wave: `docker compose restart celery-worker django` (+ browser_service only if T14's template check determined the image bakes `templates/` — django+celery FIRST per the standing deploy rule).
- [ ] **Step 4:** Smoke: submit ONE local url_list job with no URLs via the intake UI → expect the 422 modal, no job row. Submit ONE local navigation job → expect normal flow (nothing in Phases 1–5 should alter a healthy run). If the local dead-seed harness is available, run one phase-1 site with a staged checkpoint + `empty_first_page` to see `[DISCOVERY-CHECKPOINT-REUSED]` end-to-end.
- [ ] **Step 5:** Update the wave memory: `wave40-shipped-prod-rca-fixes.md` (tasks shipped, test counts, kill switches and their defaults, restart notes) + index line in `MEMORY.md`. Hand the sync/deploy question (EB branch + Railway PR, per `extractorbuilder-sync-rules`) to the user — do not push or open PRs unprompted.

---

## Accepted Residuals (explicitly out of scope, with reasons)

| # | Residual | Why not in this wave |
|---|---|---|
| R1 | 763 nastygal (all_tiers_blocked despite positive discovery probe) | Genuinely dead site per replay: 404 seed, 50-byte empty checkpoint, "SITE DEAD" verdict. No mechanism can rescue it; the honest verdict stands. |
| R2 | Import-time side effects other than bad call signatures (e.g. a draft that does network I/O at import) | T6/T7 cover the observed crash classes (signature + regex). Broader import sandboxing is a different (large) project. |
| R3 | Swap-burn class (755 vestiairecollective, 761 karenmillen, 762 marimekko, 764: swap fired, 2–3h run, n=0) | T9 rescues their outputs at finalize; the *burn* (full budget spent on a listing that then yields nothing) needs the wave-40-candidate zales-style gate-relaxation work — separate wave, see memory `wave40-candidates`. |
| R4 | Queue latency (celery concurrency=2 × ~2h jobs → aarya's 43-job batch took ~10h) | Capacity, not correctness. Railway sizing decision is the user's. |
| R5 | T11 same-process zombie conservatism | An abandoned-generation registration stays `alive` until process restart/purge — conservative by design (tombstone, never delete). Slightly more `_trash` traffic on hot slugs. |
| R6 | FM archive decay (`newest-5` prune) | `restore_job_draft` may find nothing for very old jobs. Acceptable: rescue happens within the job's own lifetime. |
| R7 | Intake Retry silent no-op on blocked rows (memory `wave40-candidates`) | Distinct intake defect (retry path), untouched here — logged as its own wave-40 candidate. |
| R8 | SessionLog `created_at` UTC vs agent-content IST+5:30 display skew | Display/TZ consistency issue in log tooling; no pipeline behavior impact. |
| R9 | Container-restart workspace deletion (the unproven half of 807) | Needs the Railway deploy/restart timeline to confirm; the registry cannot see cross-restart deletions. T11 narrows the mechanism; residual documented in T11. |
| R10 | Honest singles (754 jayjays quality-gate 2/10, 761 karenmillen conf 0.85, 782 captcha, 790 cancelled) | Correct verdicts, not misses. |
| R11 | `search_criteria` lines 2..N unparsed | T4 parses line one by contract; per-line seeding belongs to intake (T5). Multi-URL input's real channel is `url_list` mode. |

## Reconciliation notes (decisions made while merging the six planning + five critique reports)

1. **State channels / edge map:** Agents B and E both designed these. E's version wins (six channels vs B's five — B missed `last_tested_draft_bytes`; E verified the stripping behavior live in-container and the reducer semantics; E found all three park return sites vs B's one). B's behavioral routing test (769 recycle) is preserved inside T1. Critique additions: the five activation guards (T1) and the module-binding idiom in every test that touches `agents.nodes`.
2. **Rescue chokepoint vs per-arm wiring:** B recommends finalize-only; F's escalation and T11 operate at the abort/finalize layer too. Adopted B's chokepoint because ~20 terminal sites exist, per-arm wiring is the verified 770/762 escape mechanism, and a finalize chokepoint has no loop risk. The wave-37 per-arm rescues stay untouched (they're a subset of what T8/T9 now cover).
3. **Finalize ordering inside `_finalize_job`:** T8's evidence read happens BEFORE the publish/rmtree block (workspace mtimes live); T11's tombstone rename happens at the END of finalize (after evidence read + publish). T9's rescue never fights T11's guard: if the workspace was already tombstoned by a sibling's finalize, evidence still resolves via the FM keys (candidate c).
4. **Late-finalize resurrection** (`tasks.py:1895-1908`, `already_terminal` excludes FAILED): deliberately UNCHANGED this wave — a rescue COMPLETED can still overwrite a FAILED row (that's the point), but terminal COMPLETED rows remain immutable. Recorded as an explicit non-decision.
5. **Kill-switch inventory (rev 2):** T7 `SCRAPER_DRAFT_CALL_GATE` (default on), T8/T9 `REAL_ITEMS_RESCUE_ENABLED` (default on), T11 `FINALIZE_WORKSPACE_DELETE` (default on; `0` = always tombstone — no old-behavior mode, rollback = revert), T12 `WRITER_PROGRESS_ESCALATION` (default **off** — opt-in experiment), T1's fast-fail ×2 `SCRAPER_FAST_FAIL` (default **off**), T13–T15 `SCRAPER_CHECKPOINT_REUSE` (default **off**, armed per-job by run_execution, never global). T2/T3/T4/T5/T10 have no switch by design.
6. **Deferred, documented:** run_execution-side call-gate re-check (T7 covers the write path); `input_urls.json` rescue for requests/playwright templates (checkpoint work covers the http/navigation templates where 769 lived); FM `list_keys` mtime support (filename-epoch parsing is the workaround); `/scrape` env-forwarding gap (if the implement-time verification in T15 finds forwarding absent, that is surfaced to the user as its own decision — no browser_service feature work is smuggled into this wave).
7. **Narrative corrections carried into memory-compatible documents:** 770's 19-product output was moved to `scrapers/` by its own cleanup agent (not finalize publish); 758's COMPLETED n=1 came from intake coercion seeding before any probe — the multi-line string broke the listing probe only. Both corrections are reflected in the matrix and task docstrings so future RCAs don't re-propagate the wrong story.

