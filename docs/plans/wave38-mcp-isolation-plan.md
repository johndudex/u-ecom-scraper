# Wave-38 — MCP Shared-Browser Isolation: Wrong-Site Abort, Quiesce, Capture Filters, Lock Hygiene & Scrape Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the shared Playwright-MCP Chrome safe under concurrency — a walk can never silently read, judge, or capture ANOTHER job's pages (the 411/412 cross-job bleed), and the deprecated `/scrape` lane stops sharing destructive state between sibling runs.

**Architecture:** Defense at the only boundary that always executes — inside the walk itself. `browser_traverse` gains an in-loop domain assertion (2 consecutive off-domain reads → honest abort), a best-effort quiesce at walk boundaries (no read while a navigation is in flight), and job-domain filters on both capture paths (API candidates filtered BEFORE verify probes, item links filtered after parse). Graph-side, every recovery walk runs under a freshly-acquired traversal lock via one new `_retraverse_locked` helper, and the lock TTL is renewed only on heartbeat progress so long healthy walks stop outliving their own lock. Scrape-lane hardening is minimal and mechanical: honor `SCRAPER_CDP_PORT` in `scraper_runner`, drop `--remote-allow-origins=*` from the deprecated scraper Chrome, and (decision-gated) drop the scraper concurrency cap to 1.

**Tech Stack:** Python (LangGraph node + pure module `experimental/nav_traversal/traversal.py`), Redis Lua CAS scripts, pytest with fake MCP tools, browser_service (FastAPI) env/source pins.

**Spec:** This plan is self-contained — the exploration, three subagent investigations (tab-tool semantics, /scrape audit, driver census), and the adversarial "screws the existing system" review were folded in per user approval. §2 is the evidence base; §0 is the design-constraint contract the implementation must not violate. No separate spec doc.

---

## Global Constraints

- **Branch:** work on `file-master-artifacts` (baseline = wave-37 tip). One commit per task, TDD order (test first, watch it fail, implement, watch it pass, commit).
- **Prod is READ-ONLY during development.** No probes of target sites from the dev box. All pipeline fetching in this plan is behind fake tools in tests; the only real-network step is the T7 gate drive, which is the product working.
- **Deploy order (unchanged): django + celery BEFORE browser-service.** T4 rides celery; T6 rides browser-service. Restart `celery-worker` + `django` after graph/traversal edits; restart `browser_service` LAST.
- **No DB migrations this wave.**
- **Suite baseline:** 3298 passed + exactly the 4 known pre-existing reds (truncation `non_seed_capped`; views `regular_intake_own_only`; filesystem_tools `oversized_offset`; wave16 `DeadTesterProbeArm none_report_arm`). Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'`. Budget a stale-fixture repair pass (wave-37 lesson: new gates trip old PASS fixtures) — repair preserving intent, never delete.
- **Ruff:** `docker compose exec django ruff check webapp/ src/ experimental/` on changed files. Ruff format is NOT the gate (standing policy).
- **Notes contract (routing safety):** any traversal abort note must NOT contain the substring `"MCP"` — `_invoke_navigation_traverse` routes `"MCP" in (result.notes or "")` into the navigate_explore fallback (graph.py:3842), which drives the SAME shared browser we just found dirty.
- **Declared-state rule:** `_invoke_navigation_traverse` is Command-routed; every return carries its own goto (F13/F17 precedent). New code paths return via the existing Command shapes only.
- **Telemetry/hygiene never aborts the walk:** quiesce (T2) and lock renewal (T4) are best-effort — log-and-proceed on any exception.
- **`TraversalResult` is constructed positionally in ~10 places; new fields MUST be keyword-with-default** (`wrong_site_abort: bool = False`, `wrong_site_url: str = ""`) appended at the END of the dataclass.
- **Never commit:** `docker-compose.override.yml`, `config/proxy.json`, session transcripts, `out.json`, scraper outputs.

---

## §0 Design-Review Constraints (the "screws the existing system" pass — executor-binding)

These five amendments came from the adversarial review the user demanded. They are constraints, not suggestions:

- **D1 — Wrong-site abort must route DIRECTLY to a locked+quiesced re-traverse, never into the generic fallback chain.** The observed 412 recovery (forced re-traverse → 10/10) must be preserved, not flipped into a 0-item failure: on a JS-listing site the HTTP `traverse()` lane returns 0 items, and the `navigate_explore` fallback drives the same shared browser. Recovery = `_retraverse_locked` (T4), bounded-wait, then the existing HTTP lane as last resort.
- **D2 — Capture filters anchor on the JOB's `start_url` registrable, never `goal_url`.** During a bleed, `goal_url` IS the wrong site — filtering against it launders the poison (412's westelm `/api/items` capture "matched" the westelm goal page). `job_registrable = _registrable(start_url)` computed once at walk start. API candidates are filtered BEFORE `verify_api` probes fire (a filtered candidate must not be requested at all). Unparseable-registrable candidates (`""`) are KEPT (conservative: don't drop what we can't classify).
- **D3 — Abort only after 2 consecutive off-domain reads** (no on-domain read in between). Legit SSO/consent hops resolve within one read; a bleed persists. Counter resets on any on-domain read. Off-domain abort applies to ALL input modes (a user-seeded `list_page` URL that genuinely redirects cross-domain degrades honestly to the HTTP lane — acceptable; the T1.6 result guard remains the backstop).
- **D4 — Lock-TTL renewal is progress-gated, never unconditional.** TTL expiry (1500s) is today's hung-walk self-heal; renewal fires only when the heartbeat's `actions` count increased since the previous beat. A stalled walk stops renewing and stays self-healing.
- **D5 — Quiesce is best-effort, never a gate.** `wait_for_stable_page` never raises, never aborts, and treats evaluate exceptions (including "Execution context destroyed" — the very condition it exists for) as `False` → proceed. Recovery walks wait ≤150s for the lock, then take the HTTP lane instead of stacking another 15-minute walker behind the walk that poisoned them.

---

## §1 Scope Summary

| # | Deliverable | Files | Evidence |
|---|-------------|-------|----------|
| T1 | In-walk wrong-site abort (2-read tolerance) + honest result fields | `experimental/nav_traversal/traversal.py` | §2.1, §2.2 |
| T2 | `wait_for_stable_page` best-effort quiesce at 3 walk boundaries | `experimental/nav_traversal/traversal.py` | §2.1 |
| T3 | Job-domain capture filters (API candidates pre-verify + item links) | `experimental/nav_traversal/traversal.py` | §2.1, D2 |
| T4 | Lock hygiene: progress-gated TTL renewal, `_retraverse_locked`, recovery wiring | `webapp/agents/graph.py` | §2.3 |
| T5 | Remove `browser_tabs` from product_analyzer allowlist | `webapp/agents/tools/__init__.py`, skill doc | §2.3 |
| T6 | Scrape hardening: CDP port truth, drop wildcard allow-origins, cap decision | `browser_service/scraper_runner.py`, `browser_pool.py`, `server.py` | §2.4 |
| T7 | Concurrent two-site gate drive (the 411/412 shape) + §5 evidence | ops (no source) | §2.1 |

---

## §2 Evidence

### 2.1 The 411/412 incident (prod celery logs, 2026-09-17, wave-36 e2e)

Timeline from the celery log of the shared browser-service:

| t | event |
|---|-------|
| 13:11:08 | job 411 (westelm) acquires `[TRAVERSAL-LOCK]` |
| 13:19:56 | 411's last logged walk action: `goto` `westelm.com.au/bath?page=3` — **still in flight** |
| 13:20:00 | 411 releases the lock |
| 13:20:04 | job 412 (renttherunway) acquires the lock; walks 68s exclusively |
| 13:20:04+ | 412's **step-0** read shows WESTELM `/bath` content (40 items, `reached:True`) — the in-flight `goto` landed on the shared tab after handoff |
| + | 412's LLM judges `is_listing=True` on wrong-site content; capture pulls westelm's resource log (`/api/items`) + 12 westelm item links |
| + | Downstream nets catch it: wave-34 F2 off-domain api-drop ERROR + T1.6 contamination → forced re-traverse (OUTSIDE the lock) comes back clean → 412 still finished 10/10 |

Root cause: the lock WORKED — the bleed was leftover tab state, not interleaved walks. Every defense that fired was a DOWNSTREAM net; nothing in the walk itself noticed the wrong domain.

### 2.2 Active-tab rule (@playwright/mcp@0.0.78, verified from the running container's npx cache)

- Each one-shot SSE tool call does its own `connectOverCDP`, takes `contexts()[0]`, and the per-session `_currentTab` defaults to the **FIRST page in `browserContext.pages()`** — the OLDEST still-open tab. There is no per-tool page argument.
- `browser_tabs` (action enum: list/new/close/select, positional index) is the ONLY tab tool. Closing a tab retargets every session to `tabs[min(index, len-1)]`.
- The browser_service tab reaper (MCP_TAB_KEEP=4, HARD_CAP=8, 1800s interval) can close a walk's tab mid-walk — which silently retargets the walk to another tab.
- `--isolated` is FATAL for the one-shot client: its per-call cookie-less context is destroyed at session end.
- Consequence: per-walk dedicated tabs (approach B) are unreliable by construction → in-walk defense (approach A) is the selected design; ephemeral MCP Chrome per walk (approach C) is deferred to wave-39 (Phase-D migration) because it cuts against wave-37's memory-capacity work.

### 2.3 Driver census (what the lock covers today)

The traversal lock covers exactly ONE of ≥6 concurrent drivers of the shared MCP Chrome:

1. `browser_traverse` — locked (graph.py:3824).
2. **product_analyzer — EVERY job, unlocked**, and its allowlist includes `browser_tabs` (`webapp/agents/tools/__init__.py:90`) — it can retarget tabs under a concurrent walk.
3. site_analyzer on url_list jobs — unlocked.
4. Agent Playground — unlocked. 5. `run_node` — unlocked. 6. `run_traversal.py` CLI — unlocked.

Lock escape hatches inside the locked path itself: the forced re-traverse (graph.py:3962) runs OUTSIDE the lock; wait-timeout PROCEEDS unlocked by design (900s); TTL 1500s < walk ceiling 3600s → walks >25 min silently outlive their own lock. Allowlist filtering FAIL-OPEN (`subagents.py:1374-1380`) — removing a name is safe exactly because the remaining names resolve to real tools (pinned by T5's test).

### 2.4 /scrape audit verdict

- **REFUTED:** cookie/storage bleed between concurrent `/scrape` runs — the playwright template always builds a fresh off-the-record context over CDP.
- **CONFIRMED hazards:** (M2) restart-kill — concurrent `/scrape`s share one Chrome with no mutual exclusion; `_restart_scraper_chrome()` SIGTERMs the shared Chrome, killing a sibling mid-run. Structural sibling-tab enumeration via the unauthenticated 0.0.0.0 CDP `/json/list`. Shared transport-level reputation (TLS/QUIC/HSTS) — not fixable at this layer, accepted.
- **Port mismatch:** compose sets `SCRAPER_CDP_PORT=19223`; `scraper_runner.py:569` hard-codes `9223` → draft attach ECONNREFUSED locally (dead path), prod runs the default 9223 so it works by accident. T6 makes env the single truth — which makes the local attach path LIVE for the first time (supervised in T7, not regression-checked).

---

## §3 Non-Goals (this wave)

- **Phase-D migration** (ephemeral per-walk MCP Chrome / playwright-attach) → wave-39.
- Adopting the lock in product_analyzer / site_analyzer / Playground / run_node (single-shot readers; the in-walk defense + capture filters cover the capture path).
- Allowlist fail-open → fail-closed.
- CDP `--remote-debugging-address` 0.0.0.0 → 127.0.0.1 (docker port-publish of 9222-9223 to the host requires 0.0.0.0; revisit only when the compose file is revised).
- The `code_tester` DEAD allowlist entry (tools/__init__.py:104-107) — untouched.
- Tab reaper changes; CDP authentication; MCP Chrome launch flags (its `--remote-allow-origins=*` at browser_pool.py:428 STAYS — load-bearing for every navigation job; only the deprecated scraper Chrome's flag is dropped).
- Any change to the navigate_explore fallback chain itself.

---

## Interfaces (cross-task contract)

```python
# experimental/nav_traversal/traversal.py
@dataclass
class TraversalResult:
    ...  # existing fields unchanged ...
    wrong_site_abort: bool = False   # NEW, last two fields, keyword-with-default
    wrong_site_url: str = ""         # NEW

def wait_for_stable_page(ev, *, timeout_s: float = 20.0,
                         stable_reads: int = 2, poll_s: float = 1.0) -> bool: ...
    # Never raises. True = URL settled (stable_reads consecutive identical
    # non-empty reads within timeout_s); False = timeout or any tool error.
    # NOTE: no `wait` tool param — sleeps via time.sleep so the quiesce works
    # even when the wait tool is unavailable.

def _capture_api_from_session(ev, goal_url: str, query: str,
                              *, job_registrable: str = "") -> dict | None: ...
def _extract_item_links(ev, *, job_registrable: str = "") -> list[str]: ...

def browser_traverse(start_url, content_type, query, *, mcp_tools=None,
                     step_fn=None, max_actions=12, trust_start_as_listing=False,
                     heartbeat_fn=None, heartbeat_interval=300.0,
                     max_seconds=None,
                     job_id: int | None = None,            # NEW keyword
                     wrong_site_tolerance: int = 2)        # NEW keyword
    -> TraversalResult: ...

# webapp/agents/graph.py
_RENEW_LOCK_LUA = """..."""   # compare-and-expire, mirrors _RELEASE_LOCK_LUA
def _traverse_heartbeat_writer(job_id, *, renew_lock: bool = False) -> Callable[[dict], None]: ...
def _retraverse_locked(url: str, content_type: str, query: str, job_id: int,
                       *, wait_timeout: float = 150.0) -> TraversalResult: ...
    # ALWAYS returns a TraversalResult (HTTP-lane errors become an honest
    # not-reached result, never None, never an exception).
```

Graph call sites after T4: `browser_traverse(url, content_type, query, trust_start_as_listing=..., job_id=job_id, heartbeat_fn=_traverse_heartbeat_writer(job_id, renew_lock=_lock_held) if job_id else None)` under `with _mcp_browser_lock(job_id) as _lock_held:`; the contamination branch calls `_retry = _retraverse_locked(url, content_type, query, job_id)`; a `wrong_site_abort` result triggers `result = _retraverse_locked(...)` BEFORE the `"MCP" in notes` check.

---

### Task 1: In-walk wrong-site abort (traversal)

**Files:**
- Modify: `experimental/nav_traversal/traversal.py` (TraversalResult dataclass ~:660-674; `browser_traverse` signature :1823-1835; main loop :1926-1974)
- Test: `tests/test_wave38_traversal_guards.py` (create)

**Interfaces:**
- Produces: `TraversalResult.wrong_site_abort`, `TraversalResult.wrong_site_url`; `browser_traverse(..., job_id=None, wrong_site_tolerance=2)`. T4's graph recovery consumes these.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_wave38_traversal_guards.py` with the shared fakes (used again by T2/T3) and the T1 cases:

```python
"""[wave-38] Shared-browser isolation: in-walk guards (T1 abort, T2 quiesce,
T3 capture filters).

The 412 bleed: the MCP one-shot session always lands on the context's OLDEST
tab, so a concurrent job's in-flight goto was read, judged is_listing=True,
and captured (its API + item links) — all upstream nets, nothing in-walk.
These tests pin the in-walk defenses. Run inside the docker suite:
  docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from experimental.nav_traversal import traversal as tv  # noqa: E402


class _Resp:
    def __init__(self, content):
        self.content = content


def _resp(obj):
    return _Resp(json.dumps(obj))


class _FakeTool:
    def __init__(self, name, fn):
        self.name, self._fn = name, fn

    def invoke(self, kwargs):
        return self._fn(kwargs)


def _surface(url, signals=None):
    return {"url": url, "title": "t", "clickables": [], "scroll_hint": False,
            "has_load_more": False, "signals": signals or {}}


WESTELM = "https://www.westelm.com.au/bath"
RTR = "https://www.renttherunway.com/collections"


def _make_tools(surfaces, network="[]", item_links="[]"):
    """One fake MCP toolset. surfaces[i] is the i-th _PAGE_STATE_JS read (an
    Exception instance = raise). Later reads clamp to the last entry."""
    reads = {"n": 0}

    def ev_fn(kwargs):
        js = kwargs.get("function", "")
        if "getEntriesByType" in js:
            return _Resp(network)
        if "commonPrefixDepth" in js:
            return _Resp(item_links)
        i = reads["n"]
        reads["n"] += 1
        s = surfaces[min(i, len(surfaces) - 1)]
        if isinstance(s, Exception):
            raise s
        return _resp(s)

    def noop(kwargs):
        return _Resp("ok")

    return [
        _FakeTool("playwright_browser_navigate", noop),
        _FakeTool("playwright_browser_click", noop),
        _FakeTool("playwright_browser_evaluate", ev_fn),
        _FakeTool("playwright_browser_wait_for", noop),
        _FakeTool("playwright_browser_snapshot", noop),
    ]


def _step(action="click", listing=False):
    def _fn(text, content_type, query, history):
        return {"is_listing": listing, "action": action,
                "target": "Shop", "reason": "test"}
    return _fn


_REAL_STABLE = tv.wait_for_stable_page  # captured pre-stub for T2's own tests


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No LLM, no network, no real capture, no quiesce ev-consumption:
    walks are pure state machines over the scripted surfaces. (The real
    wait_for_stable_page would CONSUME _PAGE_STATE_JS reads from the fake
    ev, shifting every scripted sequence — stub it; TestWaitForStablePage
    calls the captured _REAL_STABLE directly.)"""
    monkeypatch.setattr(tv, "_do_action", lambda *a, **k: "acted")
    monkeypatch.setattr(tv, "_capture_api_from_session", lambda *a, **k: None)
    monkeypatch.setattr(tv, "_extract_item_links", lambda *a, **k: [])
    monkeypatch.setattr(tv, "wait_for_stable_page", lambda *a, **k: True)
    monkeypatch.setattr(tv, "llm_step", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("llm_step must not run under test")))


def _walk(monkeypatch, surfaces, step, **kwargs):
    return tv.browser_traverse(
        RTR, "product", "dress", mcp_tools=_make_tools(surfaces),
        step_fn=step, max_actions=6, **kwargs)


class TestWrongSiteAbort:
    def test_two_consecutive_off_domain_reads_abort(self, monkeypatch):
        result = _walk(monkeypatch, [_surface(WESTELM), _surface(WESTELM)],
                       _step())
        assert result.reached is False
        assert result.wrong_site_abort is True
        assert result.wrong_site_url == WESTELM
        assert result.discovery["listing_reached"] is False

    def test_abort_notes_never_contain_mcp(self, monkeypatch):
        """Routing contract: 'MCP' in notes sends _invoke_navigation_traverse
        into the navigate_explore fallback — which drives the SAME dirty
        browser. The abort note must never say MCP."""
        result = _walk(monkeypatch, [_surface(WESTELM), _surface(WESTELM)],
                       _step())
        assert "MCP" not in (result.notes or "")
        assert "wrong-site" in (result.notes or "")

    def test_single_off_domain_read_then_on_domain_recovers(self, monkeypatch):
        """D3: one off-domain read (legit SSO/consent hop) must NOT abort —
        the counter resets on the first on-domain read."""
        result = _walk(monkeypatch, [_surface(WESTELM), _surface(RTR)],
                       _step(action="click", listing=False))
        assert result.reached is True  # second read judged listing
        assert result.wrong_site_abort is False

    def test_no_llm_call_consumed_by_the_aborted_read(self, monkeypatch):
        """The abort fires on the READ, before step() — the poisoned surface
        must not burn an LLM turn."""
        calls = []

        def counting_step(text, ct, q, history):
            calls.append(text)
            return {"is_listing": False, "action": "click",
                    "target": "x", "reason": ""}

        _walk(monkeypatch, [_surface(WESTELM), _surface(WESTELM)],
              counting_step)
        assert len(calls) <= 1, "second off-domain read must abort pre-step"

    def test_fields_default_off(self):
        """Existing positional constructions (10 call sites) must be
        untouched by the new fields."""
        r = tv.TraversalResult(False, None, ["u"], "unknown", None, {}, ["u"], [])
        assert r.wrong_site_abort is False
        assert r.wrong_site_url == ""

    def test_job_id_is_ignored_by_pure_walk(self, monkeypatch):
        result = _walk(monkeypatch, [_surface(WESTELM), _surface(WESTELM)],
                       _step(), job_id=412)
        assert result.wrong_site_abort is True
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_traversal_guards.py -q'`
Expected: FAIL — `TraversalResult` has no attribute `wrong_site_abort` / walk reaches budget exhaustion instead of aborting.

- [ ] **Step 3: Implement**

In `experimental/nav_traversal/traversal.py`:

(a) Append to `TraversalResult` (after `discovery`, keep keyword-with-default):

```python
    # [wave-38 W38-A1] Honest wrong-site abort (412): the walk observed the
    # shared tab on ANOTHER registrable domain for wrong_site_tolerance
    # consecutive reads and refused to judge/capture it. The graph recovers
    # via _retraverse_locked; notes deliberately never contain "MCP" (that
    # substring routes into the navigate_explore fallback — same dirty browser).
    wrong_site_abort: bool = False
    wrong_site_url: str = ""
```

(b) Extend `browser_traverse` signature (keyword-only, after `max_seconds`):

```python
    job_id: int | None = None,
    wrong_site_tolerance: int = 2,
```

(c) After the tool-resolution block (`if not nav or not ev or not snap:` return) and BEFORE the navigate `try:`, compute the anchor (D2 — job domain, not goal domain):

```python
    # [wave-38 W38-A1/D2] Anchor for every in-walk domain assertion: the JOB's
    # start_url registrable — never the live goal_url, which during a bleed IS
    # the wrong site.
    job_registrable = _registrable(start_url) if start_url else ""
    off_domain_reads = 0
```

(d) In the main loop, immediately after the empty-surface break (`if not surface: ... break`) and BEFORE `signals = surface.get("signals") or {}`, insert:

```python
        # [wave-38 W38-A1] In-loop domain assertion. The MCP one-shot session
        # always lands on the context's OLDEST tab (@playwright/mcp 0.0.78
        # _currentTab default), so a concurrent walk's leftover navigation can
        # leave us reading someone else's page (jobs 411/412). Two consecutive
        # off-domain reads → the tab is not ours; abort honestly BEFORE any
        # LLM judgment or capture instead of extracting their data.
        if job_registrable and wrong_site_tolerance > 0:
            _s_url = surface.get("url") or ""
            if _s_url and _registrable(_s_url) == job_registrable:
                off_domain_reads = 0
            elif _s_url:
                off_domain_reads += 1
                logger.warning(
                    "[W38-A1] off-domain read %d/%d (job=%s want=%s got=%s)",
                    off_domain_reads, wrong_site_tolerance, job_id,
                    job_registrable, _s_url[:120],
                )
                if off_domain_reads >= wrong_site_tolerance:
                    _abort_notes = (
                        f"wrong-site abort: browser tab on {_s_url} (expected "
                        f"{job_registrable}) after {off_domain_reads} "
                        "consecutive off-domain reads"
                    )
                    logger.error("[W38-A1] %s (job=%s)", _abort_notes, job_id)
                    return TraversalResult(
                        reached=False, goal_url=start_url, path=path,
                        mechanism="unknown", api=None, signals={},
                        visited=path, pruned=[], notes=_abort_notes,
                        discovery={"listing_url": None,
                                   "listing_reached": False,
                                   "pagination": None},
                        wrong_site_abort=True, wrong_site_url=_s_url,
                    )
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_traversal_guards.py -q'`
Expected: PASS (all six). Then the traversal regression file still passes: `python -m pytest experimental/nav_traversal/test_traversal.py -q` on the host from repo root (pre-existing file — any failures there must be triaged: fix regressions, never widen the abort).

- [ ] **Step 5: Full suite + commit**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'`
Expected: baseline green + 4 known reds.

```bash
git add experimental/nav_traversal/traversal.py tests/test_wave38_traversal_guards.py
git commit -m "feat(wave-38 T1): in-walk wrong-site abort — 2 consecutive off-domain reads refuse to judge/capture (412)"
```

---

### Task 2: Best-effort page quiesce at walk boundaries (traversal)

**Files:**
- Modify: `experimental/nav_traversal/traversal.py` (new helper near `_read_page_state_with_retry` :1686; call sites :1892-1894, :2002-2014, :2086)
- Test: `tests/test_wave38_traversal_guards.py` (append)

**Interfaces:**
- Produces: `wait_for_stable_page(ev, wait, *, timeout_s=20.0, stable_reads=2, poll_s=1.0) -> bool`. Consumed by T4's recovery walks (the quiesce runs at the END of every walk, so the lock handoff is clean).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_wave38_traversal_guards.py`)

```python
class TestWaitForStablePage:
    def _ev(self, surfaces):
        reads = {"n": 0}

        def ev_fn(kwargs):
            i = reads["n"]
            reads["n"] += 1
            s = surfaces[min(i, len(surfaces) - 1)]
            if isinstance(s, Exception):
                raise s
            return _resp(s)

        return _FakeTool("playwright_browser_evaluate", ev_fn)

    def test_true_when_url_settles(self):
        ev = self._ev([_surface(RTR), _surface(RTR)])
        assert _REAL_STABLE(
            ev, timeout_s=5.0, stable_reads=2, poll_s=0.01) is True

    def test_false_on_churning_urls_and_bounded(self):
        t0 = time.monotonic()
        ev = self._ev([_surface(RTR + "/a"), _surface(RTR + "/b")])
        ok = _REAL_STABLE(ev, timeout_s=0.3, stable_reads=2, poll_s=0.05)
        assert ok is False
        assert time.monotonic() - t0 < 5.0, "must respect its timeout"

    def test_tool_errors_are_false_not_raise(self):
        ev = self._ev([RuntimeError("Execution context destroyed")])
        assert _REAL_STABLE(ev, timeout_s=1.0, poll_s=0.01) is False

    def test_none_ev_is_false(self):
        assert _REAL_STABLE(None) is False

    def test_quiesce_runs_at_walk_boundaries(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            tv, "wait_for_stable_page",
            lambda *a, **k: calls.append(k.get("timeout_s")) or True)
        _walk(monkeypatch, [_surface(RTR)], _step(action="click",
                                                  listing=True))
        assert len(calls) >= 2, (
            "quiesce must run at walk start AND before the is_listing capture"
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_traversal_guards.py::TestWaitForStablePage -q'`
Expected: FAIL — `wait_for_stable_page` does not exist; boundary test finds 0 calls.

- [ ] **Step 3: Implement**

(a) New helper just above `_read_page_state_with_retry`:

```python
def wait_for_stable_page(
    ev, *, timeout_s: float = 20.0, stable_reads: int = 2,
    poll_s: float = 1.0,
) -> bool:
    """[wave-38 W38-A2] Best-effort quiesce: poll the page URL until it stops
    moving. The 412 bleed fired at lock HANDOFF — the previous job's goto was
    still in flight when our first read ran. This wait gives that navigation
    time to land BEFORE we read/capture, and runs again before we hand the
    lock back so the NEXT job starts clean. D5: never raises, never aborts —
    evaluate errors (including 'Execution context destroyed', the very
    condition this exists for) simply return False and the caller proceeds.
    """
    if ev is None:
        return False
    deadline = time.monotonic() + timeout_s
    last_url = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            raw = ev.invoke({"function": _PAGE_STATE_JS})
        except Exception as exc:
            logger.debug("wait_for_stable_page: evaluate error: %s", exc)
            return False
        data = _parse_mcp_json(raw) or {}
        url = data.get("url") or ""
        # An empty url means a navigation is mid-flight — that IS instability.
        if url and url == last_url:
            stable += 1
            if stable >= stable_reads:
                return True
        else:
            stable = 0
            last_url = url
        time.sleep(poll_s)
    return False
```

(b) Three call sites in `browser_traverse`:

Walk start — directly AFTER the navigate try/except (`nav.invoke` / `wait.invoke({"time": 3})`), before `history: list[dict] = []`:

```python
    # [wave-38 W38-A2] quiesce before the first read — the 412 bleed shape:
    # a prior job's in-flight goto lands after we start reading.
    wait_for_stable_page(ev)
```

Pre-capture — FIRST line inside the `if result.get("is_listing"):` branch, before `url = surface.get("url") or start_url`:

```python
            wait_for_stable_page(ev, timeout_s=10.0)
```

Release-quiesce — immediately BEFORE the final `return TraversalResult(reached=False, ...)` (covers every loop-exit: ceiling, empty page-state, stuck-breaker, done, budget):

```python
    # [wave-38 W38-A2] release-quiesce: never hand the lock back
    # mid-navigation — the next job's step-0 read must not see OUR goto.
    wait_for_stable_page(ev, timeout_s=10.0)
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_traversal_guards.py -q'`
Expected: PASS (T1 + T2 classes).

- [ ] **Step 5: Commit**

```bash
git add experimental/nav_traversal/traversal.py tests/test_wave38_traversal_guards.py
git commit -m "feat(wave-38 T2): best-effort page quiesce at walk start, pre-capture, and lock release"
```

---

### Task 3: Job-domain capture filters (traversal)

**Files:**
- Modify: `experimental/nav_traversal/traversal.py` (`_capture_api_from_session` :1095-1190; `_extract_item_links` :1782-1815; `browser_traverse` call sites :2014, :2020)
- Test: `tests/test_wave38_traversal_guards.py` (append)

**Interfaces:**
- Produces: `_capture_api_from_session(ev, goal_url, query, *, job_registrable="")`, `_extract_item_links(ev, *, job_registrable="")`. Backwards compatible: empty `job_registrable` = no filtering (today's behavior).

- [ ] **Step 1: Write the failing tests** (append)

```python
class TestCaptureFilters:
    def test_api_candidates_filtered_before_verify(self, monkeypatch):
        """D2: the westelm candidate must never be VERIFY-PROBED — the probe
        itself is a cross-domain request. Filter at _consider, pre-verify."""
        seen = []

        def fake_verify(cand, fetch, query):
            seen.append(cand.get("url") if isinstance(cand, dict) else cand)
            return {"url": "https://www.renttherunway.com/api/items",
                    "count": 3, "sample_keys": ["x"]}

        monkeypatch.setattr(tv, "verify_api", fake_verify)
        monkeypatch.setattr(
            tv, "api_from_network",
            lambda entries: [
                {"url": "https://www.renttherunway.com/api/items"},
                {"url": "https://www.westelm.com.au/api/items"},
            ])
        monkeypatch.setattr(tv, "_httpx_fetch", lambda *a, **k: {"ok": False})

        ev = _FakeTool("playwright_browser_evaluate",
                       lambda kw: _Resp("[]"))
        api = tv._capture_api_from_session(
            ev, WESTELM, "dress", job_registrable="renttherunway.com")
        assert api and "renttherunway.com" in api["url"]
        assert not any("westelm" in str(u) for u in seen), (
            "off-domain candidate must be dropped BEFORE verify_api probes it"
        )

    def test_no_job_registrable_keeps_everything(self, monkeypatch):
        """Backwards compat: empty anchor = today's behavior."""
        seen = []

        def fake_verify(cand, fetch, query):
            u = cand.get("url") if isinstance(cand, dict) else str(cand)
            seen.append(u)
            return {"url": u, "count": 1, "sample_keys": ["x"]}

        monkeypatch.setattr(tv, "verify_api", fake_verify)
        monkeypatch.setattr(
            tv, "api_from_network",
            lambda entries: [
                {"url": "https://www.renttherunway.com/api/items"},
                {"url": "https://www.westelm.com.au/api/items"},
            ])
        monkeypatch.setattr(tv, "_httpx_fetch", lambda *a, **k: {"ok": False})

        ev = _FakeTool("playwright_browser_evaluate",
                       lambda kw: _Resp("[]"))
        tv._capture_api_from_session(ev, WESTELM, "dress")
        assert any("westelm" in str(u) for u in seen)

    def test_item_links_filtered_by_job_domain(self, monkeypatch):
        ev = _FakeTool(
            "playwright_browser_evaluate",
            lambda kw: _Resp([
                "https://www.renttherunway.com/dresses/p/1",
                "https://www.westelm.com.au/p/2",
                "https://www.renttherunway.com/dresses/p/3",
            ]))
        out = tv._extract_item_links(ev, job_registrable="renttherunway.com")
        assert out == [
            "https://www.renttherunway.com/dresses/p/1",
            "https://www.renttherunway.com/dresses/p/3",
        ]

    def test_item_links_unfiltered_without_anchor(self, monkeypatch):
        ev = _FakeTool(
            "playwright_browser_evaluate",
            lambda kw: _Resp([
                "https://www.renttherunway.com/dresses/p/1",
                "https://www.westelm.com.au/p/2",
            ]))
        assert len(tv._extract_item_links(ev)) == 2

    def test_two_part_tld_anchor(self):
        """Same _registrable rule as T1.6 — .com.au subdomain matches its
        apex, so the JOB's own regional host is never its own off-domain."""
        assert tv._registrable("https://www.westelm.com.au/x") == \
            tv._registrable("https://westelm.com.au/y")
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_traversal_guards.py::TestCaptureFilters -q'`
Expected: FAIL — `_capture_api_from_session` has no `job_registrable` kwarg (TypeError); item-link filter absent (westelm link survives).

- [ ] **Step 3: Implement**

(a) `_capture_api_from_session(ev, goal_url, query, *, job_registrable: str = "")` — rewrite `_consider` to filter BEFORE `verify_api` (D2):

```python
    def _consider(api):
        if not api:
            return
        # [wave-38 W38-A3/D2] Filter BEFORE verify_api — the probe is itself
        # a cross-domain request. Anchor is the JOB's registrable (never
        # goal_url, which during a bleed IS the wrong site). Unparseable
        # registrables ("") are KEPT — don't drop what we can't classify.
        if job_registrable:
            _reg = _registrable(api.get("url") or "")
            if _reg and _reg != job_registrable:
                logger.info(
                    "browser_traverse: capture filter dropped off-domain API "
                    "candidate BEFORE verify: %s (job domain %s)",
                    str(api.get("url"))[:100], job_registrable,
                )
                return
        base = (api.get("url") or "").split("?")[0]
        if base and base not in seen:
            seen.add(base)
            candidates.append(api)
```

(b) `_extract_item_links(ev, *, job_registrable: str = "")` — filter just before the final log line:

```python
    # [wave-38 W38-A3] Item links feed product_analyzer samples + tester
    # URLs — an off-domain link here poisons TWO downstream agents.
    if job_registrable and out:
        _before = len(out)
        out = [u for u in out
               if _registrable(u) in ("", job_registrable)]
        if _before != len(out):
            logger.info(
                "_extract_item_links: dropped %d off-domain item link(s) "
                "(job domain %s)", _before - len(out), job_registrable,
            )
```

(c) `browser_traverse` passes the anchor at both call sites (:2014, :2020):

```python
            api = _capture_api_from_session(ev, url, query,
                                            job_registrable=job_registrable)
            item_links = _extract_item_links(ev,
                                             job_registrable=job_registrable)
```

- [ ] **Step 4: Run to verify pass**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_traversal_guards.py -q'`
Expected: PASS (T1 + T2 + T3 classes).

- [ ] **Step 5: Commit**

```bash
git add experimental/nav_traversal/traversal.py tests/test_wave38_traversal_guards.py
git commit -m "feat(wave-38 T3): job-domain capture filters — API candidates dropped pre-verify, item links filtered (D2)"
```

---

### Task 4: Lock hygiene + recovery wiring (graph)

**Files:**
- Modify: `webapp/agents/graph.py` (`_RELEASE_LOCK_LUA` area :3685, `_traverse_heartbeat_writer` :3765-3795, `_invoke_navigation_traverse` :3810-3990)
- Test: `tests/test_wave38_lock_and_recovery.py` (create)

**Interfaces:**
- Consumes: T1's `wrong_site_abort`/`wrong_site_url`; T2's release-quiesce (already inside `browser_traverse`).
- Produces: `_RENEW_LOCK_LUA`, `_traverse_heartbeat_writer(job_id, *, renew_lock=False)`, `_retraverse_locked(url, content_type, query, job_id, *, wait_timeout=150.0) -> TraversalResult`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_wave38_lock_and_recovery.py`:

```python
"""[wave-38] Graph-side lock hygiene + wrong-site recovery.

The forced re-traverse ran OUTSIDE the lock — a guaranteed bleed window and
the exact shape that made 412 possible. TTL 1500 < walk ceiling 3600 meant a
healthy long walk outlived its own lock. This pins the fix: ONE locked,
bounded-wait recovery helper, and progress-gated TTL renewal.
"""
from __future__ import annotations

import contextlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from langgraph.types import Command, RunnableConfig  # noqa: E402

import agents.graph as g  # noqa: E402
from experimental.nav_traversal import traversal as tv  # noqa: E402


def _tr(seed, **kw):
    """Honest TraversalResult via the same positional idiom graph uses."""
    return tv.TraversalResult(seed[0], seed[1], seed[2], seed[3], seed[4],
                              seed[5], seed[6], seed[7], seed[8], **kw)


NOT_REACHED = (False, None, ["https://a.example/"], "unknown", None, {},
               ["https://a.example/"], [], "didn't reach")
CLEAN = (True, "https://a.example/collections",
         ["https://a.example/collections"], "browser_llm", None,
         {"is_listing": True}, ["https://a.example/collections"], [],
         "LLM judged listing")
# item_links keep the node tail off its HTTP url_examples fallback (which
# would fetch the goal_url for real); discovery feeds the listing_reached pin.
CLEAN_KW = dict(
    item_links=["https://a.example/collections/p/1"],
    discovery={"listing_url": "https://a.example/collections",
               "listing_reached": True,
               "pagination": {"type": "page_param"}},
)


@contextlib.contextmanager
def _lock(acquired=True):
    yield acquired


class TestRetraverseLocked:
    def test_acquired_runs_browser_traverse_locked(self, monkeypatch):
        seen = {}

        def fake_bt(url, ct, q, **kw):
            seen.update(kw)
            return _tr(CLEAN)

        monkeypatch.setattr(tv, "browser_traverse", fake_bt)
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        out = g._retraverse_locked("https://a.example/", "product", "x", 7)
        assert out.reached is True
        assert seen.get("job_id") == 7
        assert seen.get("trust_start_as_listing") is False
        assert callable(seen.get("heartbeat_fn"))

    def test_not_acquired_falls_to_http_lane(self, monkeypatch):
        monkeypatch.setattr(tv, "browser_traverse", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not walk without the lock")))
        calls = []
        monkeypatch.setattr(tv, "traverse",
                            lambda *a, **k: calls.append(a) or _tr(CLEAN))
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(False))
        out = g._retraverse_locked("https://a.example/", "product", "x", 7)
        assert out.reached is True and calls

    def test_walk_exception_falls_to_http_lane(self, monkeypatch):
        monkeypatch.setattr(tv, "browser_traverse",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        monkeypatch.setattr(tv, "traverse", lambda *a, **k: _tr(CLEAN))
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        assert g._retraverse_locked(
            "https://a.example/", "product", "x", 7).reached is True

    def test_double_failure_returns_honest_not_reached(self, monkeypatch):
        monkeypatch.setattr(tv, "browser_traverse",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        monkeypatch.setattr(tv, "traverse",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom")))
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        out = g._retraverse_locked("https://a.example/", "product", "x", 7)
        assert out.reached is False
        assert "locked re-traverse failed" in (out.notes or "")
        assert out.discovery["listing_reached"] is False

    def test_contamination_branch_uses_the_helper(self):
        """The old bare browser_traverse at the T1.6 branch is the bleed
        window — source-pin its replacement."""
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py"),
                   encoding="utf-8").read()
        assert "_retry = _retraverse_locked(" in src
        assert "_retry = browser_traverse(" not in src


class TestWrongSiteRecovery:
    def _run(self, monkeypatch, tmp_path, first, second):
        calls = []

        def scripted(url, ct, q, **kw):
            calls.append(kw)
            return second if len(calls) > 1 else first

        monkeypatch.setattr(tv, "browser_traverse", scripted)
        monkeypatch.setattr(g, "_mcp_browser_lock",
                            lambda job_id, wait_timeout=900.0,
                            poll_interval=5.0: _lock(True))
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "_log_event_row", lambda *a, **k: None)
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        (tmp_path / "workspace" / "s").mkdir(parents=True, exist_ok=True)
        state = {"job_id": 0, "site_slug": "s", "url": "https://a.example/",
                 "page_type": "product", "input_mode": "navigation",
                 "search_criteria": "dress"}
        out = g._invoke_navigation_traverse(state, RunnableConfig())
        return out, calls

    def test_abort_triggers_exactly_one_locked_recovery(
            self, monkeypatch, tmp_path):
        abort = _tr(NOT_REACHED, wrong_site_abort=True,
                    wrong_site_url="https://www.westelm.com.au/bath",
                    notes="wrong-site abort: browser tab on "
                          "https://www.westelm.com.au/bath (expected "
                          "a.example) after 2 consecutive off-domain reads")
        out, calls = self._run(monkeypatch, tmp_path, abort,
                               _tr(CLEAN, **CLEAN_KW))
        assert len(calls) == 2, "initial walk + exactly ONE recovery walk"
        assert "MCP" not in (abort.notes or "")
        assert isinstance(out, Command) and out.goto == "product_analyzer"
        upd = out.update
        assert upd["navigation_analysis"]["discovery"]["listing_reached"] \
            is True

    def test_clean_first_walk_never_recovers(self, monkeypatch, tmp_path):
        out, calls = self._run(monkeypatch, tmp_path,
                               _tr(CLEAN, **CLEAN_KW), None)
        assert len(calls) == 1


class TestLockRenewal:
    def _writer_env(self, monkeypatch):
        evals = []

        class _FakeRedis:
            def eval(self, script, n, key, *args):
                evals.append((key,) + tuple(args))

        monkeypatch.setattr(g, "_traversal_redis", lambda: _FakeRedis())

        rows = []

        class _Obj:
            def filter(self, **k):
                return self

            def count(self):
                return 1

            def create(self, **k):
                rows.append(k)

        class _FakeSessionLog:
            ROLE_SYSTEM = "system"
            objects = _Obj()

        import scraper.models as sm

        monkeypatch.setattr(sm, "SessionLog", _FakeSessionLog)
        return evals, rows

    def test_progress_beats_renew_ttl(self, monkeypatch):
        evals, rows = self._writer_env(monkeypatch)
        w = g._traverse_heartbeat_writer(7, renew_lock=True)
        w({"step": 1, "elapsed_s": 1, "actions": 0, "reason": "step"})
        w({"step": 2, "elapsed_s": 2, "actions": 3, "reason": "step"})
        w({"step": 3, "elapsed_s": 3, "actions": 3, "reason": "step"})
        w({"step": 4, "elapsed_s": 4, "actions": 5, "reason": "step"})
        assert rows, "SessionLog rows still written first"
        assert len(evals) == 2, (
            "renewal ONLY on progress: beats with actions 0→3→3→5 renew twice"
        )
        assert evals[0][0] == g._TRAVERSAL_LOCK_KEY
        assert evals[0][1] == "7"
        assert int(evals[0][2]) == g._TRAVERSAL_LOCK_TTL

    def test_stalled_walk_stops_renewing(self, monkeypatch):
        """D4: TTL expiry is the hung-walk self-heal — a stalled walk must
        NOT keep its lock alive."""
        evals, _ = self._writer_env(monkeypatch)
        w = g._traverse_heartbeat_writer(7, renew_lock=True)
        w({"step": 1, "elapsed_s": 1, "actions": 2, "reason": "step"})
        w({"step": 2, "elapsed_s": 301, "actions": 2, "reason": "step"})
        w({"step": 3, "elapsed_s": 601, "actions": 2, "reason": "step"})
        assert len(evals) == 1

    def test_renew_off_by_default(self, monkeypatch):
        evals, _ = self._writer_env(monkeypatch)
        g._traverse_heartbeat_writer(7)({
            "step": 1, "elapsed_s": 1, "actions": 9, "reason": "step"})
        assert evals == []

    def test_renewal_errors_never_break_the_walk(self, monkeypatch):
        evals, rows = self._writer_env(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("redis down")

        monkeypatch.setattr(g, "_traversal_redis", boom)
        w = g._traverse_heartbeat_writer(7, renew_lock=True)
        w({"step": 1, "elapsed_s": 1, "actions": 1, "reason": "step"})
        assert rows, "the SessionLog row must survive a dead redis"
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_lock_and_recovery.py -q'`
Expected: FAIL — `g._retraverse_locked` does not exist; `_traverse_heartbeat_writer` rejects `renew_lock`; contamination branch still calls bare `browser_traverse`.

- [ ] **Step 3: Implement** (`webapp/agents/graph.py`)

(a) After `_RELEASE_LOCK_LUA` (:3685-3690):

```python
# [wave-38 W38-A5] compare-and-expire: renew ONLY if we still own the lock
_RENEW_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
```

(b) Replace `_traverse_heartbeat_writer` (:3765-3795) — keep the SessionLog row exactly as-is, add progress-gated renewal:

```python
def _traverse_heartbeat_writer(job_id, *, renew_lock: bool = False):
    """[wave-30 W30-8] SessionLog heartbeat writer for the browser_traverse
    walk.

    Prod 569: the MCP walk ran silent for the better part of an hour —
    nothing distinguished a healthy walk from a wedged one. The traversal
    calls this at most every 300s with step/elapsed info; each call becomes
    one ``[NAV-TRAVERSE]`` system row (agent-heartbeat idiom). Failures are
    swallowed: telemetry must never break the walk.

    [wave-38 W38-A5/D4] With renew_lock=True (the caller holds the traversal
    lock), a beat showing PROGRESS (actions increased since the previous
    beat) also renews the lock TTL. Stalled walks stop renewing → TTL expiry
    keeps today's crashed-holder self-heal; a healthy walk longer than the
    TTL (1500s) no longer silently outlives its own lock (ceiling is 3600s).
    """
    last_actions = 0

    def _write(info: dict) -> None:
        nonlocal last_actions
        try:
            from scraper.models import SessionLog

            seq = SessionLog.objects.filter(job_id=job_id).count()
            SessionLog.objects.create(
                job_id=job_id,
                role=SessionLog.ROLE_SYSTEM,
                agent="browser_traverse",
                content=(
                    f"[NAV-TRAVERSE] step {info.get('step', '?')} elapsed "
                    f"{int(info.get('elapsed_s') or 0)}s actions="
                    f"{info.get('actions', '?')} ({info.get('reason', 'step')})"
                ),
                seq=seq,
            )
        except Exception as exc:
            logger.debug("traverse heartbeat write failed (job %s): %s", job_id, exc)
        if renew_lock:
            try:
                _actions = int(info.get("actions") or 0)
                if _actions > last_actions:
                    last_actions = _actions
                    client = _traversal_redis()
                    client.eval(
                        _RENEW_LOCK_LUA, 1, _TRAVERSAL_LOCK_KEY,
                        str(job_id), _TRAVERSAL_LOCK_TTL,
                    )
            except Exception as exc:
                logger.debug(
                    "traverse lock renewal failed (job %s): %s", job_id, exc
                )

    return _write
```

(c) New helper directly after `_traverse_heartbeat_writer`:

```python
def _retraverse_locked(
    url: str, content_type: str, query: str, job_id: int, *,
    wait_timeout: float = 150.0,
):
    """[wave-38 W38-A1/A4] ONE recovery walk under a freshly-acquired lock.

    Serves both recovery arms: the W38-A1 wrong-site abort and the T1.6
    cross-domain contamination. The old forced re-traverse ran OUTSIDE the
    lock — a guaranteed bleed window, and the exact shape that made 412
    possible. The wait is bounded (~2.5 min, D5): a still-busy browser sends
    this job down the HTTP lane instead of stacking another 15-minute walker
    behind the very walk that poisoned it. ALWAYS returns a TraversalResult;
    never raises.
    """
    from experimental.nav_traversal.traversal import TraversalResult

    try:
        with _mcp_browser_lock(job_id, wait_timeout=wait_timeout) as acquired:
            if acquired:
                from experimental.nav_traversal.traversal import (
                    browser_traverse,
                )

                return browser_traverse(
                    url, content_type, query,
                    trust_start_as_listing=False,
                    job_id=job_id,
                    heartbeat_fn=(
                        _traverse_heartbeat_writer(job_id, renew_lock=True)
                        if job_id else None
                    ),
                )
    except Exception:
        logger.exception(
            "browser_traverse: locked re-traverse failed (job %s)", job_id
        )
    logger.warning(
        "browser_traverse: locked re-traverse unavailable (job %s) — HTTP lane",
        job_id,
    )
    try:
        from experimental.nav_traversal.traversal import traverse

        return traverse(url, content_type, query)
    except Exception as exc:
        logger.exception(
            "browser_traverse: HTTP re-traverse failed (job %s)", job_id
        )
        return TraversalResult(
            False, None, [url], "unknown", None, {}, [url], [],
            f"locked re-traverse failed: {exc}",
            discovery={"listing_url": None, "listing_reached": False,
                       "pagination": None},
        )
```

(d) In `_invoke_navigation_traverse`:

Main call (:3824-3838) — bind `acquired`, pass `job_id`, gate renewal on it:

```python
        with _mcp_browser_lock(job_id) as _lock_held:
            result = browser_traverse(
                url, content_type, query,
                # [wave-34 T34-2] a PDP-flipped job must never trust its seed
                # as a listing even if a stale route still lands here.
                trust_start_as_listing=(
                    _input_mode in ("list_page", "search_term")
                    and not state.get("pdp_seed_flip")
                ),
                # [wave-38 W38-A1] in-walk wrong-site assertion anchor.
                job_id=job_id,
                # [wave-30 W30-8] heartbeat rows every 300s + the
                # NAV_TRAVERSE_MAX_TIMEOUT hard ceiling (resolved inside
                # traversal.py) — the walk is otherwise invisible in
                # SessionLog (prod 569). [wave-38 W38-A5] the heartbeat
                # renews the lock TTL on progress when we hold the lock.
                heartbeat_fn=(
                    _traverse_heartbeat_writer(job_id, renew_lock=_lock_held)
                    if job_id else None
                ),
            )
```

Wrong-site recovery — insert immediately AFTER the `with` block, BEFORE the `if "MCP" in (result.notes or ""):` check (:3840-3842):

```python
        # [wave-38 W38-A1/D1] The walk aborted because the shared tab was on
        # another site. Recover with ONE locked+quiesced walk BEFORE any
        # generic fallback: the HTTP lane fails on JS listings, and the
        # navigate_explore fallback drives the SAME browser we just found
        # dirty. (The abort notes never contain "MCP" — routing contract.)
        if getattr(result, "wrong_site_abort", False):
            logger.error(
                "browser_traverse: wrong-site abort (job %s) — browser tab "
                "on %s; ONE locked re-traverse",
                job_id, (result.wrong_site_url or "")[:80],
            )
            result = _retraverse_locked(url, content_type, query, job_id)
```

Contamination branch (:3962-3964) — replace the unlocked walk:

```python
                _retry = _retraverse_locked(url, content_type, query, job_id)
```

(e) Scope guard: the wrong-site recovery block must sit BEFORE the MCP check so an abort never routes to the fallback, and the `wrong_site_abort` attribute must be read with `getattr(..., False)` so the HTTP-fallback result (plain TraversalResult from `traverse()`) passes through untouched.

- [ ] **Step 4: Run to verify pass**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_lock_and_recovery.py ../tests/test_wave38_traversal_guards.py -q'`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'`
Expected: baseline green + 4 known reds.

```bash
git add webapp/agents/graph.py tests/test_wave38_lock_and_recovery.py
git commit -m "feat(wave-38 T4): lock hygiene — progress-gated TTL renewal, bounded _retraverse_locked, wrong-site recovery wiring"
```

---

### Task 5: Remove `browser_tabs` from product_analyzer (allowlist + skill doc)

**Files:**
- Modify: `webapp/agents/tools/__init__.py:83-91` (product_analyzer allowlist — delete line 90)
- Modify: `.opencode/skills/playwright-navigation/SKILL.md` (the only place that teaches tab flows — replace the tab section with a one-line "tabs are not available to agents" note)
- Test: `tests/test_wave38_lock_and_recovery.py` (append)

**Interfaces:**
- Consumes: the fail-open allowlist filter (subagents.py:1374-1380) stays dormant only if every remaining name resolves to a real tool — pinned below.

- [ ] **Step 1: Write the failing test** (append to `tests/test_wave38_lock_and_recovery.py`)

```python
class TestProductAnalyzerAllowlist:
    def _names(self):
        import re

        src = open(os.path.join(ROOT, "webapp", "agents", "tools",
                                "__init__.py"), encoding="utf-8").read()
        m = re.search(r'"product_analyzer":\s*\[(.*?)\]', src, re.DOTALL)
        return re.findall(r'"(playwright_[a-z_]+)"', m.group(1))

    def test_browser_tabs_removed(self):
        assert "playwright_browser_tabs" not in self._names()

    def test_reader_tools_survive(self):
        for n in ("playwright_browser_navigate",
                  "playwright_browser_evaluate",
                  "playwright_browser_click",
                  "playwright_browser_wait_for"):
            assert n in self._names(), f"{n} must stay (product_analyzer reads pages)"

    def test_remaining_names_resolve_no_fail_open(self):
        """Fail-open filtering stays dormant only while every allowlisted
        name is a real tool name in playwright_tools.py."""
        pts = open(os.path.join(ROOT, "webapp", "agents", "tools",
                                "playwright_tools.py"),
                   encoding="utf-8").read()
        for n in self._names():
            assert f'"{n}"' in pts, f"{n} no longer exists — allowlist would fail open"
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_lock_and_recovery.py::TestProductAnalyzerAllowlist -q'`
Expected: FAIL — `playwright_browser_tabs` still in the allowlist.

- [ ] **Step 3: Implement**

Delete the `"playwright_browser_tabs",` line from the product_analyzer list (tools/__init__.py:90). In `.opencode/skills/playwright-navigation/SKILL.md`, replace the browser_tabs guidance with:

```markdown
## Tabs

Agents have no tab tools. The shared browser's active tab is managed by the
infrastructure (one walk at a time, traversal lock). Never plan multi-tab
flows.
```

- [ ] **Step 4: Run to verify pass** — same command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/agents/tools/__init__.py .opencode/skills/playwright-navigation/SKILL.md tests/test_wave38_lock_and_recovery.py
git commit -m "fix(wave-38 T5): drop browser_tabs from product_analyzer — agents must not retarget the shared walk tab"
```

---

### Task 6: Scrape-lane hardening (browser-service)

**Files:**
- Modify: `browser_service/scraper_runner.py:569-571` (port truth)
- Modify: `browser_service/browser_pool.py:505-534` (drop line 514 `--remote-allow-origins=*` from `_start_scraper_chrome` ONLY — the MCP Chrome's own flag at :428 STAYS)
- Modify: `browser_service/server.py:96` — **DECISION-GATED**, see DECISION-NEEDED D-1
- Test: `tests/test_wave38_lock_and_recovery.py` (append; source pins — browser_service is not importable in the django container)

**Interfaces:**
- Consumes: `SCRAPER_CDP_PORT` from `browser_pool` (single source of truth; scraper_runner already lazy-imports browser_pool at :199, no cycle).

- [ ] **Step 1: Write the failing tests** (append)

```python
class TestScrapeHardening:
    def _src(self, *parts):
        return open(os.path.join(ROOT, "browser_service", *parts),
                    encoding="utf-8").read()

    def test_runner_uses_env_port_not_hardcoded(self):
        src = self._src("scraper_runner.py")
        assert '"BROWSER_CDP_ENDPOINT"] = "http://127.0.0.1:9223"' not in src, (
            "hard-coded 9223 ignores compose's SCRAPER_CDP_PORT — attach dies "
            "ECONNREFUSED wherever the env differs"
        )
        assert "SCRAPER_CDP_PORT" in src

    def test_scraper_chrome_drops_wildcard_allow_origins(self):
        """--remote-allow-origins=* turns the unauthenticated CDP into a
        reachable-from-anywhere WS endpoint. The DEPRECATED scraper chrome
        loses it; playwright connect_over_cdp sends no Origin header, so
        attach still works."""
        pool = self._src("browser_pool.py")
        # scope the deprecated scraper chrome's launch fn (def → next def)
        i0 = pool.index("def _start_scraper_chrome")
        i1 = pool.find("\ndef ", i0 + 1)
        block = pool[i0:i1 if i1 != -1 else len(pool)]
        assert "--remote-allow-origins" not in block
        # scope guard: the MCP chrome keeps its (load-bearing) flag — the
        # file must retain EXACTLY ONE occurrence (MCP launch, browser_pool:428)
        assert pool.count("--remote-allow-origins") == 1

    def test_scrape_cap_default(self):
        """D-1: code default drops 2→1 (concurrent /scrapes share one Chrome
        with no mutual exclusion; _restart_scraper_chrome SIGTERMs the shared
        process). If the user picks keep-2, this pin changes to "2"."""
        server = self._src("server.py")
        assert 'SCRAPE_MAX_CONCURRENT", "1"' in server
```

- [ ] **Step 2: Run to verify failure**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave38_lock_and_recovery.py::TestScrapeHardening -q'`
Expected: FAIL — all three pins red.

- [ ] **Step 3: Implement**

(a) `scraper_runner.py` — replace the hard-coded endpoint (:568-571):

```python
    if _stealth != "cloak":
        from .browser_pool import SCRAPER_CDP_PORT

        env["BROWSER_CDP_ENDPOINT"] = f"http://127.0.0.1:{SCRAPER_CDP_PORT}"
    else:
        env.pop("BROWSER_CDP_ENDPOINT", None)
```

(b) `browser_pool.py` — delete the `"--remote-allow-origins=*",` line from `_start_scraper_chrome`'s args ONLY (:514).

(c) `server.py:96` (D-1 recommended option): `"2"` → `"1"`:

```python
SCRAPE_MAX_CONCURRENT = int(os.environ.get("SCRAPE_MAX_CONCURRENT", "1"))
```

With SCRAPE_MAX_QUEUE=0 this is admit-or-429; run_scraper's W8 ladder already parks and retries on 429/502/503/504, so concurrent scrapers are DELAYED, not failed — the cost is caller wall clock (tester's derived window), which is why this is a decision box and not a silent change.

- [ ] **Step 4: Run to verify pass** — same command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add browser_service/scraper_runner.py browser_service/browser_pool.py browser_service/server.py tests/test_wave38_lock_and_recovery.py
git commit -m "feat(wave-38 T6): scrape hardening — SCRAPER_CDP_PORT truth, drop wildcard allow-origins, scraper cap 1 (D-1)"
```

---

### Task 7: Concurrent two-site gate drive (the 411/412 shape) + evidence

**Files:**
- Modify: `docs/plans/wave38-mcp-isolation-plan.md` §5 (fill during execution)
- Ops only — no source changes.

- [ ] **Step 1: Build + restart the full stack** — `docker compose --profile full up --build -d`, then restart `celery-worker` + `django` (graph edits) and `browser_service` LAST (T6 edits). Fire restarts ONCE.

- [ ] **Step 2: Full suite + ruff green**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'` → baseline + 4 known reds; `docker compose exec django ruff check webapp/ src/ experimental/` → clean on changed files.

- [ ] **Step 3: Fire the gate.** Two sentinel jobs on two DISTINCT registrable domains (site A: westelm-class JS listing; site B: a different registrable), submitted so their traversals overlap (B fired while A is mid-walk — the lock forces A→B sequential handoff, which is exactly the 412 handoff shape). Use the wave-36 baseline-gate driver `scripts/run_wave36_baseline_gate.py` (docker exec, dead-seed sentinels, watch logger to `/tmp/gate_watch.log`) as the seeding idiom — extend it for a two-site concurrent submission. Fire ONCE.

- [ ] **Step 4: Assert the PASS criteria** (all must hold):

1. Both jobs COMPLETED with item_count > 0.
2. Celery logs contain ZERO of: `[W38-A1] wrong-site abort`, `CROSS-DOMAIN traversal result`, `dropping off-domain api capture`.
3. Each job's output JSON contains no match of the OTHER sentinel's registrable domain (the T1.6-veto-absent assertion — the nets exist as backstop; this wave asserts they never had to fire).
4. If D-1 adopted: any `scrape rejected (busy` 429 events appear as W8 park-and-retry rows in SessionLog, never as failed runs.
5. New quiesce/renewal markers present in logs (`[W38-A1]`-free but `wait_for_stable_page` debug lines and `[TRAVERSAL-LOCK]` release lines) — proof the boundaries ran.

- [ ] **Step 5: Record §5 evidence + update memory.** Fill §5 with the drive IDs, log excerpts, suite counts, and any stale-fixture repairs. Write the wave-38 shipped memory file + MEMORY.md index line. EB sync per standing rules (`sync-w38` branch → EB, fork main push with lease, merge-tree check, compare link handed to user — PR creation is the USER's action).

---

## §4 Verification Plan

1. **Per-task unit runs** — the exact pytest commands in each task, in order.
2. **Traversal regression file** — `python -m pytest experimental/nav_traversal/test_traversal.py -q` from repo root (host, no Docker) after T1/T2/T3: pre-existing fakes must not regress; failures = triage, never widen the abort tolerance.
3. **Full suite** — `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'` after T4 and again at T7; baseline is 3298 + the 4 named known reds. Any NEW red is triaged before proceeding (wave-37 lesson: stale fixtures get repaired preserving intent).
4. **Ruff** on changed files; format is not the gate.
5. **T7 gate drive** — the only real-network verification; supervised; PASS criteria in Task 7 Step 4.
6. **Restart discipline** — celery + django after T4/T5; browser_service after T6, LAST; fire once each.

---

## §5 Evidence Appendix (filled during execution)

- Suite counts (start / per-task / final): _TBD by executor_
- T7 gate drive: job IDs, sentinel domains, handoff timing, assertion outputs: _TBD by executor_
- Any stale-fixture repairs (file, intent preserved): _TBD by executor_
- Prod-relevant notes (deploy order honored, /api/version sha after deploy): _TBD by executor_

---

## DECISION-NEEDED

**D-1 — `SCRAPE_MAX_CONCURRENT` code default (server.py:96):**
- **(1) Recommended: 2→1 in code** (as written in T6). Concurrent `/scrape`s share one Chrome with no mutual exclusion, and `_restart_scraper_chrome()` SIGTERMs the shared Chrome — a sibling run dies mid-flight (audit hazard M2). With SCRAPE_MAX_QUEUE=0 the second caller gets 429 → run_scraper's W8 ladder parks and retries → delayed, not failed. Cost: caller wall clock (tester's derived window) when two browser scrapers genuinely race — rare, and correctness beats latency here.
- (2) Keep code 2, set `SCRAPE_MAX_CONCURRENT=1` as a Railway env var only — reversible without a deploy, but the code default stays unsafe for any fresh environment.
- (3) Keep 2 everywhere — accept M2; rely on T1-T4 only. NOT recommended.

**D-2 — if (2) or (3):** tell me and I'll adjust T6c + its test pin (`test_scrape_cap_default`) to match before execution starts.
