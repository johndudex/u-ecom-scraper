# Wave-37 — Honest Finalization, Infra-Aware Retries, Intake Coercion & Draft Repair — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the four prod failure classes from the 09-15→09-18 scan that waste completed work or cascade budget on infrastructure failures — honest finalization under soft-limit death, no retry consumption on browser-service unavailability, PDP-as-list_page intake coercion with on-domain degrade, and one bounded draft-repair arm per defect class — plus an opt-in proactive memory-recycle lever.

**Architecture:** Graph-side (webapp) fixes ride existing seams: the wave-16 B3 park (`_park_browser_unavailable`, graph.py:4804), the wave-22 force-FAIL arms inside `_invoke_code_tester` (graph.py:8109+), the wave-19 T1.1 ladder gate (`webapp/agents/draft_safety.py`), and `_finalize_job_failed` (tasks.py). Browser-service containment (T33-3/4/5) is verified ALREADY SHIPPED — no rewrite; only an opt-in sustained-pressure recycle is added, flag-default-off.

**Tech Stack:** Django + Celery (webapp/scraper), LangGraph nodes (webapp/agents), FastAPI maintenance loop (browser_service/server.py), pytest (root `tests/`).

**Spec:** This plan IS the spec — its §2 evidence section records the prod scan (09-15→09-18, 101 jobs, 33 terminal-failed) with scan-claim-vs-code corrections. Format precedent: `docs/plans/wave36-field-mapping-plan.md`.

## Global Constraints

- **Deploy order (standing rule): django + celery BEFORE browser-service.** Violating order strands graph-side parks against an old gateway.
- **Baseline:** wave-36 at u-ecom `365d4b1` (branch `file-master-artifacts`). Wave-37 builds directly on it; wave-36 is NOT yet deployed to prod (prod = `dd7fe80`, waves 33+34) — both waves ride the next PR.
- **Never** park/finalize over non-terminal resumable statuses the checkpoint pair needs: `captcha_blocked`, `akamai_blocked`, `browser_unavailable` parks are RESUMABLE; only finalize them via the explicit exhausted-budget arm (Task 2).
- **Never credit a COMPLETED with product_count=0** — Task 4's sanitizer must preserve this invariant (a force-COMPLETED job still needs real items on disk).
- Prod is READ-ONLY during development: reads via Railway GraphQL / prod APIs only; no re-drives, no restarts, no target-site fetching outside the pipeline; never print secret values (token names only).
- Ruff clean (`E4/E7/E9/F/I/UP` per `ruff.toml`). `ruff format` is NOT the gate.
- Suite invocation: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'`. Known pre-existing reds (NOT ours to fix): wave16 park pin, `test_truncation` non_seed_message_IS_capped, `test_views` regular_intake_jobs_lists_own_only, `test_filesystem_tools` oversized_file_mentions_offset.
- Routing functions cannot mutate state (they receive `state`, return strings) — state writes go in nodes (`Command(update=...)` or returned dicts). Declared-state rule (wave-36 round-2 B3): every new counter/flag a router reads MUST be declared in `ScrapeState` (webapp/agents/state.py) or it is silently stripped.
- Restart celery-worker + django after graph edits (standing gotcha); fire drive/restart actions ONCE.

---

## §1 Scope summary (from the 09-15→09-18 prod scan)

| Item | Class | Prod evidence | Fix shape |
|---|---|---|---|
| W37-3 | infra | 593 (all steps done, count=10, marked failed), 630/632/633/634/635 | artifact-evidence finalize + explicit SoftTimeLimit catch + parked-budget exhaustion |
| W37-NEW-A | infra | 671/672 (4 retest arms burned on BROWSER_SERVICE_UNAVAILABLE 04:43–06:08Z, died without execution) | report-level infra classifier → park, arms not consumed |
| W37-NEW-B | graph | 672 ended on PASS conf 0.25 with no execution | PASS-with-zero-real-items force-FAIL sanitizer |
| W37-NEW-C | graph | 626/625/622/620/614/602/598 (7 jobs: PDP URLs as `list_page` → cross-domain double-refusal death) | intake PDP coercion + traverse degrade-to-on-domain |
| W37-NEW-D | graph | 628/623/600 (writer stripped proxy ladder, honest fail) | one bounded auto-repair pass before honest-fail |
| W37-NEW-E | graph | 649/642/637 identical-draft loops; 671/672 "playwright→playwright" remap logs | verify `strategies_tried` append coverage; fix the specific leak |
| W37-NEW-F | minor | 652 goodreads sign-in redirect crashed Phase-1 discovery | auth-wall → site-side stop_reason |
| W37-OPS | ops | 3 memory saturations in 3 days (99.2% @28.8h 09-17; 1.00 @38.7h 09-18) | DECISION-NEEDED — opt-in recycle behind flag; Railway 6→8GB lever |

## §2 Evidence corrections — scan claims vs code (verified 2026-09-18, `365d4b1`)

The scan report inherited "wave-33 leftovers open" framing from the incident history. Code says otherwise:

1. **"Immediate per-PID kill deferred ~300s" — SHIPPED.** The navigate timeout arm calls `_ephemeral_abandon(call_id)` (server.py:3487-3489) which SIGKILLs starttime-verified tree roots immediately (server.py:899-938); the `NAVIGATE_ACTIVE_PIDS` age cap (server.py:783, 818-847) only stops *protecting* old entries from the orphan killer — it is not the kill path. The pgrep+starttime sweep exists (server.py:1726-1760). **No build item.**
2. **"Poisoned /navigate thread recycle missing" — SHIPPED.** T33-3 `_swap_executor_pool` + `_maybe_swap_poisoned_pools` (server.py:198-264) is wired into the maintenance loop (server.py:1489). **No build item**; Task 9 only adds the observability rider.
3. **"SoftTimeLimit kills without honest finalize" — PARTIALLY SHIPPED.** `run_scrape_task` catches `Exception` → `_finalize_job_failed` (tasks.py:406-415), and `SoftTimeLimitExceeded` is an `Exception`. The 593 defect is narrower: the kill lands *between* artifact writes and the status write (or inside finalize itself), so evidence says COMPLETED while the row says failed. Also `resume_scrape_task` resets the deadline fresh (tasks.py:574-580, wave-22 A3), so unbounded park→resume→park flapping — not one long task — is the budget hole. Task 1/2 target exactly these.
4. **"Tester park arm missing" — EXISTS.** Preflight skip stamps `browser_unavailable_detail` (graph.py:8195-8226) and the router parks ABOVE the no-report arms (route_after_testing.py:1454-1460). The 671/672 burn is a different entry: BROWSER_SERVICE_UNAVAILABLE arrives *inside a written test report* (born at `webapp/agents/tools/shell_tools.py:694` when run_scraper's /scrape 429s), which reads as a normal NEEDS_FIXES/CRASH report and consumes cascade arms. Task 3 targets report-level classification.
5. **"No-op ladder remap playwright→playwright" — PLAUSIBLE BUT UNPROVEN.** `_decide_strategy` already reads `strategies_tried` (graph.py:4559) and hands `_all_tried` to `_escalate_strategy` (graph.py:4607, 4751-4795: "never re-pick the tried+failed strategy"). The observed remap therefore implies an append gap on some path (e.g. the tier-axis arm at graph.py:~4615-4680 or strategy-name normalization). Task 7 is verification-first: derive the failing test from the 671/672 SessionLog signature before touching code.

## §3 Non-goals

- No wave-36 rework; no changes to field mapping, soft-block JSON carve-out, or the shortfall gate beyond what Task 4's sanitizer touches (verdict honesty only).
- No Railway infra changes in code. The 6GB→8GB raise and any service-recycle cadence are user-owned ops levers (§DECISION-NEEDED).
- No park/resume redesign beyond the cumulative-parked budget (Task 2): the checkpoint-backing park statuses and the beat resumer stay as-is.
- No new browser leaks fixes: T33-3/4/5 verified shipped (§2.1-2.2).
- The four pre-existing red tests stay untouched.

## Interfaces (cross-task contract)

- `scraper.tasks.finalize_from_artifacts(job_id, fallback_message) -> str` — Task 1 produces; Task 2's exhausted-budget arm consumes; the `task_failure` handler (tasks.py:165-206) may adopt it.
- `webapp.agents.tools.browser_http.report_is_infra_blocked(report: dict) -> str | None` — Task 3 produces (returns the infra reason string or None); the router park arm (route_after_testing.py:1454) consumes.
- `scraper.tasks.park_job_for_browser_service` (browser_http.py:486, existing) — Task 2 extends the beat resumer, not this function.
- `webapp.agents.draft_safety.repair_ladder_violation(text: str, strategy: str) -> str | None` — Task 8 produces; the route_after_testing arm at 1717-1733 consumes.
- `src.intake_coerce.coerce_pdp_intake(url: str, nav_method: str) -> tuple[str, str | None]` — Task 5 produces; `intake_create_job` (views.py:2969) consumes. Returns `(effective_nav_method, note)`.

---

### Task 1: Artifact-evidence honest finalize (W37-3a)

Prod 593 died with every Step `done`, `product_count=10`, and outputs on disk — the soft-limit kill landed between the last artifact write and the status write, and the `except Exception` fallback (tasks.py:406-415) marked FAILED unconditionally. Finalize must look at what actually exists before choosing the verdict.

**Files:**
- Modify: `webapp/scraper/tasks.py` (new `finalize_from_artifacts` near `_finalize_job_failed`; adopt in `run_scrape_task`'s except at :406-415)
- Test: `tests/test_wave37_honest_finalize.py`

**Interfaces:**
- Consumes: `ScrapeJob`, `JobListing`, `Step` models; `_finalize_job_failed` (tasks.py) as the failure path.
- Produces: `finalize_from_artifacts(job_id: int, fallback_message: str) -> str` returning the final status string ("completed" | "failed"); Task 2 calls it for budget exhaustion.

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-3a] Artifact-evidence honest finalize.

Prod 593: every step done, product_count=10, outputs on disk — the
SoftTimeLimit kill landed before the status write and the generic
except-Exception finalize marked the job FAILED. finalize_from_artifacts
resolves the verdict from what exists: real items in the job's output
artifact → completed (count preserved); anything else → the honest
failure path. Never invents a COMPLETED with 0 items.
"""
from __future__ import annotations

import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.test import TestCase  # noqa: E402

from scraper.models import JobListing, ScrapeJob  # noqa: E402
from scraper.tasks import finalize_from_artifacts  # noqa: E402


class HonestFinalizeTests(TestCase):
    def _job(self, **kw) -> ScrapeJob:
        return ScrapeJob.objects.create(
            url="https://example.com/p/1", site_name="example-com",
            status=ScrapeJob.STATUS_RUNNING, **kw,
        )

    def test_outputs_with_real_items_finalize_completed(self):
        job = self._job(product_count=10)
        JobListing.objects.create(job=job, url="https://example.com/p/1", data={"title": "x"})
        status = finalize_from_artifacts(job.id, "SoftTimeLimitExceeded")
        job.refresh_from_db()
        assert status == ScrapeJob.STATUS_COMPLETED
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "SoftTimeLimitExceeded" not in (job.error_message or "")

    def test_no_items_finalize_failed_with_cause(self):
        job = self._job()
        status = finalize_from_artifacts(job.id, "SoftTimeLimitExceeded mid-graph")
        job.refresh_from_db()
        assert status == ScrapeJob.STATUS_FAILED
        assert "SoftTimeLimitExceeded" in (job.error_message or "")

    def test_never_overwrite_terminal_or_parked_rows(self):
        job = self._job(status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE)
        assert finalize_from_artifacts(job.id, "x") == ScrapeJob.STATUS_BROWSER_UNAVAILABLE
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_BROWSER_UNAVAILABLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_honest_finalize.py -q'`
Expected: FAIL with `ImportError: cannot import name 'finalize_from_artifacts'`.

- [ ] **Step 3: Implement**

In `webapp/scraper/tasks.py`, next to `_finalize_job_failed`, add:

```python
def finalize_from_artifacts(job_id: int, fallback_message: str) -> str:
    """[wave-37 W37-3a] Resolve the final status from on-disk evidence.

    Prod 593 died AFTER execution wrote real items but BEFORE the COMPLETED
    write; the generic failure finalize then lied about the run. Evidence
    order: (1) ≥1 JobListing row for the job → completed (count = actual
    rows, never the stale counter); (2) otherwise the honest failure path
    with the original cause. Parked/resumable and other non-RUNNING rows
    are never touched (same guard as _finalize_job's ladder).
    """
    job = ScrapeJob.objects.filter(pk=job_id).first()
    if job is None:
        return ""
    if job.status != ScrapeJob.STATUS_RUNNING:
        return job.status
    items = JobListing.objects.filter(job=job).count()
    if items > 0:
        job.status = ScrapeJob.STATUS_COMPLETED
        job.product_count = items
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "product_count", "completed_at"])
        logger.warning(
            "Job %d: finalize_from_artifacts — %d listing row(s) on disk after a "
            "mid-flight kill; finalized COMPLETED from evidence (was RUNNING)",
            job_id, items,
        )
        _close_lingering_steps(job)  # same helper _finalize_job uses
        return job.status
    _finalize_job_failed(job_id, fallback_message)
    return ScrapeJob.STATUS_FAILED
```

Then change `run_scrape_task`'s tail (tasks.py:406-415) so process-context kills route through it:

```python
    try:
        _run_graph_job(job, rescrape=rescrape, force_full=force_full)
    except SoftTimeLimitExceeded:
        logger.error("Job %d: SoftTimeLimitExceeded — artifact-evidence finalize", job_id)
        finalize_from_artifacts(job_id, "Soft time limit exceeded mid-graph")
    except Exception as exc:
        logger.exception("Scrape job %d failed: %s", job_id, exc)
        _finalize_job_failed(job_id, str(exc))
```

Import `SoftTimeLimitExceeded` from `billiard.exceptions` (celery re-exports it; match the existing billiard import block at tasks.py:148-157) and `timezone` from `django.utils` if absent. Mirror the same two-armed except in `resume_scrape_task` (tasks.py:556+) — a resume that dies mid-finalize is the same 593 shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_honest_finalize.py -q'`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add webapp/scraper/tasks.py tests/test_wave37_honest_finalize.py
git commit -m "feat(wave-37 W37-3a): artifact-evidence honest finalize under soft-limit death (prod 593)"
```

---

### Task 2: Cumulative parked-time budget for resume flapping (W37-3b)

The beat resumer (`resume_browser_unavailable_jobs`, tasks.py:2370-2440) re-dispatches every parked row whenever /health is ok; a job can park→resume→park indefinitely during a saturation window, each round-trip re-burning discovery/tester wall-clock. Give every job a cumulative parked budget; past it, finalize honestly (the resume checkpoints exist to survive SHORT outages, not to fund unbounded retries).

**Files:**
- Create: `webapp/scraper/migrations/0044_scrapejob_parked_seconds.py`
- Modify: `webapp/scraper/models.py` (`ScrapeJob.parked_seconds`), `webapp/agents/tools/browser_http.py:486` (`park_job_for_browser_service` accumulates), `webapp/scraper/tasks.py:2370-2440` (resumer budget gate), `webapp/config/settings.py` (knob)
- Test: `tests/test_wave37_parked_budget.py`

**Interfaces:**
- Consumes: `finalize_from_artifacts` (Task 1).
- Produces: `ScrapeJob.parked_seconds: PositiveIntegerField(default=0)` + `last_parked_at: FloatField(null=True)`; knob `PARKED_TIME_BUDGET_S` (default `7200`); `park_job_for_browser_service` gains the accumulation side-effect (signature unchanged); the resumer's return dict gains `"exhausted": [{"job_id", "parked_seconds"}]` alongside the existing `"resumed"`.

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-3b] Cumulative parked-time budget.

The wave-16 B3 resumer re-dispatches parked rows on recovery with no cap;
during a saturation window a job can flap park→resume→park forever,
re-burning wall-clock each round. past PARKED_TIME_BUDGET_S the resumer
finalizes the job honestly (browser_unavailable exhausted) instead of
re-dispatching. Parked rows themselves stay untouched (checkpoint-backed).
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from django.test import TestCase
from unittest import mock

from scraper.models import ScrapeJob
from scraper.tasks import resume_browser_unavailable_jobs
from scraper import tasks as st


class ParkedBudgetTests(TestCase):
    def _parked(self, seconds: int) -> ScrapeJob:
        return ScrapeJob.objects.create(
            url="https://example.com/", site_name="example-com",
            status=ScrapeJob.STATUS_BROWSER_UNAVAILABLE,
            parked_seconds=seconds,
        )

    @mock.patch.dict(os.environ, {"PARKED_TIME_BUDGET_S": "7200"})
    def test_over_budget_job_finalizes_not_redispatched(self):
        job = self._parked(seconds=7201)
        with mock.patch.object(st, "dispatch_scrape_job") as disp, \
             mock.patch.object(st, "finalize_from_artifacts", return_value="failed") as fin:
            out = resume_browser_unavailable_jobs()
        disp.assert_not_called()
        fin.assert_called_once()
        self.assertIn(job.id, {j["job_id"] for j in out.get("exhausted", [])})

    @mock.patch.dict(os.environ, {"PARKED_TIME_BUDGET_S": "7200"})
    def test_under_budget_job_still_resumes(self):
        job = self._parked(seconds=60)
        with mock.patch.object(st, "dispatch_scrape_job") as disp:
            out = resume_browser_unavailable_jobs()
        disp.assert_called_once()
        self.assertEqual(out["resumed"], 1)

    def test_park_accumulates_seconds(self):
        from webapp.agents.tools.browser_http import park_job_for_browser_service
        job = self._parked(seconds=0)
        job.status = ScrapeJob.STATUS_RUNNING; job.save(update_fields=["status"])
        with mock.patch("webapp.agents.tools.browser_service_client.time.time",
                        side_effect=[1000.0, 1300.0]):
            park_job_for_browser_service(job.id, "down again")
        job.refresh_from_db()
        self.assertEqual(job.parked_seconds, 300)
```

(Adjust the `time.time` patch target to the module the implementation actually imports it from — keep the accumulation arithmetic monotonic-last-parked-at based; store `last_parked_at` as an epoch float on the row only if a column is cheaper than parsing notes. Implementation may use two columns `parked_seconds` + `last_parked_at: FloatField(null=True)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_parked_budget.py -q'`
Expected: FAIL — `parked_seconds` is not a field (`FieldError` / `TypeError`).

- [ ] **Step 3: Implement**

Migration `0044_scrapejob_parked_seconds` (depends on `0043_field_mapping`):

```python
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scraper", "0043_field_mapping")]
    operations = [
        migrations.AddField(
            model_name="scrapejob",
            name="parked_seconds",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scrapejob",
            name="last_parked_at",
            field=models.FloatField(null=True, blank=True),
        ),
    ]
```

Model fields on `ScrapeJob` next to `field_mapping` (models.py:218). In `park_job_for_browser_service` (browser_http.py:486), before writing the park status: if the row was RUNNING, `parked_seconds += now - last_parked_at` (set `last_parked_at=now` when it was None); on resume, the resumer clears `last_parked_at=None` after flipping to pending. In the resumer loop (tasks.py:2417-2440), before the claim-by-rowcount flip:

```python
budget = int(os.environ.get("PARKED_TIME_BUDGET_S", "7200"))
exhausted: list[dict] = []
for job_id in parked:
    row = ScrapeJob.objects.get(pk=job_id)
    if (row.parked_seconds or 0) >= budget:
        finalize_from_artifacts(
            job_id,
            f"Browser-service park budget exhausted "
            f"({row.parked_seconds}s cumulative across outages; budget {budget}s). "
            f"Finalized honestly — re-drive manually when the service is stable.",
        )
        # claim-by-rowcount already keeps this race-safe: the finalize only
        # lands on a still-parked row.
        exhausted.append({"job_id": job_id, "parked_seconds": row.parked_seconds})
        continue
    ...existing re-dispatch...
return {"resumed": n, "reason": ..., "exhausted": exhausted}
```

Keep the existing per-tick cap (tasks.py:2387 "capped oldest first") — exhausted rows don't count against it.

- [ ] **Step 4: Run test to verify it passes** — same command, expect PASS. Then `docker compose exec django python manage.py makemigrations --check --dry-run` to prove no drift.

- [ ] **Step 5: Commit**

```bash
git add webapp/scraper/migrations/0044_scrapejob_parked_seconds.py webapp/scraper/models.py webapp/scraper/tasks.py webapp/agents/tools/browser_http.py webapp/config/settings.py tests/test_wave37_parked_budget.py
git commit -m "feat(wave-37 W37-3b): cumulative parked-time budget — resumer finalizes flapping jobs honestly"
```

---

### Task 3: Infra-blocked test reports park instead of consuming cascade arms (W37-NEW-A)

Prod 671/672 burned all 4 retest arms 04:43–06:08Z on reports whose real defect was `BROWSER_SERVICE_UNAVAILABLE` (born at shell_tools.py:694 when the tester's run_scraper 429'd). The preflight park (route_after_testing.py:1454-1460) only catches the no-report shape. Classify report-embedded infra signatures and route them to the existing park — without incrementing `test_retry_count`.

**Files:**
- Modify: `webapp/agents/tools/browser_http.py` (new `report_is_infra_blocked`), `webapp/agents/nodes/route_after_testing.py` (park arm at :1454-1460 extended)
- Test: `tests/test_wave37_infra_reports.py`

**Interfaces:**
- Consumes: `_park_browser_unavailable` (graph.py:4804) via the router's existing arm.
- Produces: `report_is_infra_blocked(report: dict) -> str | None` — returns a human reason when the report's failures are all infra-signature, else None.

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-NEW-A] Infra signatures inside test reports must park, not
consume cascade retest arms. Prod 671/672: 4 retest arms burned on
BROWSER_SERVICE_UNAVAILABLE (run_scraper /scrape 429s) → failed without
execution. The report classifier recognizes infra-only failure sets;
partial-infra reports (a REAL defect plus an infra error) stay normal.
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from webapp.agents.tools.browser_http import report_is_infra_blocked


def _report(issues, crash="", stop_reason=None):
    rep = {"issues": issues, "crash_error": crash,
           "discovery_coverage": {"stop_reason": stop_reason} if stop_reason else {}}
    return rep


def test_browser_service_unavailable_issue_is_infra():
    rep = _report([{"issue_type": "crash",
                    "message": "BROWSER_SERVICE_UNAVAILABLE: the browser-service "
                               "gateway is unreachable or refused the scrape"}])
    reason = report_is_infra_blocked(rep)
    assert reason and "BROWSER_SERVICE_UNAVAILABLE" in reason


def test_navigate_throttled_stop_reason_is_infra():
    assert report_is_infra_blocked(
        _report([], stop_reason="navigate_throttled")
    )


def test_429_in_crash_is_infra():
    assert report_is_infra_blocked(
        _report([], crash="RuntimeError: HTTP 429 after 3 attempt(s) — backpressure")
    )


def test_real_defect_is_not_infra():
    assert report_is_infra_blocked(
        _report([{"issue_type": "wrong_type", "message": "price is a string"}])
    ) is None


def test_mixed_report_is_not_infra():
    rep = _report([
        {"issue_type": "wrong_type", "message": "price is a string"},
        {"issue_type": "crash", "message": "BROWSER_SERVICE_UNAVAILABLE: gateway"},
    ])
    assert report_is_infra_blocked(rep) is None


def test_infra_report_has_no_extracted_items():
    rep = _report([{"issue_type": "crash", "message": "BROWSER_SERVICE_UNAVAILABLE"}])
    rep["items_extracted"] = 5
    assert report_is_infra_blocked(rep) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_infra_reports.py -q'`
Expected: FAIL with `ImportError: cannot import name 'report_is_infra_blocked'`.

- [ ] **Step 3: Implement**

In `browser_http.py` (near `park_job_for_browser_service`):

```python
_INFRA_MARKERS = (
    "BROWSER_SERVICE_UNAVAILABLE",
    "navigate_throttled",
    "memory gate tripped",
    "Errno 11",
)
_INFRA_CRASH_MARKERS = _INFRA_MARKERS + ("HTTP 429", "Too Many Requests")


def report_is_infra_blocked(report: dict) -> str | None:
    """[wave-37 W37-NEW-A] True reason string when EVERY failure signal in the
    report is browser-service infrastructure (429 backpressure, unavailable
    gateway, memory gate), not the draft. A report that ALSO carries a real
    draft defect, or that extracted real items, is not infra — the cascade
    must keep judging it."""
    if not isinstance(report, dict):
        return None
    if int(report.get("items_extracted") or 0) > 0:
        return None
    signals: list[str] = []
    for issue in report.get("issues") or []:
        if isinstance(issue, dict):
            signals.append(str(issue.get("message") or ""))
    signals.append(str(report.get("crash_error") or ""))
    cov = report.get("discovery_coverage")
    if isinstance(cov, dict):
        signals.append(str(cov.get("stop_reason") or ""))
    real = [s for s in signals if s and not any(m in s for m in _INFRA_MARKERS)]
    if real:
        return None
    for s in signals:
        if any(m in s for m in _INFRA_CRASH_MARKERS):
            return s[:200]
    return None
```

In `route_after_testing.py`, inside the existing park arm (1454-1460), extend the condition:

```python
    from ..tools.browser_http import report_is_infra_blocked
    _infra = report_is_infra_blocked(state.get("test_report") or {})
    if state.get("browser_unavailable_detail") or _infra:
        if _infra and not state.get("browser_unavailable_detail"):
            # routing functions cannot mutate state — _park_browser_unavailable
            # recovers the detail from browser_unavailable_detail OR error_message;
            # log loudly here so the SessionLog carries the classification.
            logger.warning(
                "route_after_testing: report is INFRA-blocked (%s) — parking "
                "without consuming a cascade arm (retry_count stays %s)",
                _infra[:120], state.get("test_retry_count"),
            )
        return "park_browser_unavailable"
```

(Match the arm's exact current spelling of the destination string — read the code first; graph registers the park node under the name `_park_browser_unavailable` is wired with.)

- [ ] **Step 4: Run test to verify it passes** — targeted file PASS, then confirm the router still routes real-defect reports exactly as before: `pytest ../tests/ -k "route_after_testing or cascade" -q` shows no new failures beyond the 4 known reds.

- [ ] **Step 5: Commit**

```bash
git add webapp/agents/tools/browser_http.py webapp/agents/nodes/route_after_testing.py tests/test_wave37_infra_reports.py
git commit -m "feat(wave-37 W37-NEW-A): infra-blocked test reports park without consuming cascade arms (prod 671/672)"
```

---

### Task 4: PASS-with-zero-real-items verdict sanitizer (W37-NEW-B)

Prod 672's last report read `overall_assessment: PASS` (conf 0.25) yet the exhausted arm correctly refused execution — because `_scraper_has_real_items(state, min_count=1)` (route_after_testing.py:1297) was false, the working-scraper-rescue arm (route_after_testing.py:1290-1309) never fired and the job died on a PASS. The honesty fix belongs at the verdict boundary: a PASS over zero real extracted items is not a PASS. With the sanitizer in place, any surviving PASS implies real items → the existing rescue arm routes to `run_execution`.

**Files:**
- Modify: `webapp/agents/graph.py` (`_invoke_code_tester`, force-FAIL block pattern at :8589-8598)
- Test: `tests/test_wave37_pass_sanitizer.py`

**Interfaces:**
- Consumes: the report-dict force-FAIL idiom (phase2_confidence preservation, wave-22 B4).
- Produces: invariant "report.overall_assessment == 'PASS' ⇒ real items extracted" consumed implicitly by route_after_testing.py:1290.

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-NEW-B] A PASS over zero real extracted items is not a PASS.

Prod 672 ended on PASS conf 0.25 with nothing extracted; the exhausted arm
refused execution (correctly — no items) and the job died on a passing
verdict. The tester's report boundary force-FAILs that shape (same idiom as
the discovery-probe force-FAIL arms, wave-22 B4 confidence preservation).
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from webapp.agents.graph import _sanitize_pass_verdict


def test_pass_with_zero_items_force_failed():
    rep = {"overall_assessment": "PASS", "confidence_score": 0.25,
           "items_extracted": 0}
    out = _sanitize_pass_verdict(rep)
    assert out["overall_assessment"] == "FAIL"
    assert out["confidence_score"] == 0.0
    assert out["phase2_confidence"] == 0.25          # wave-22 B4 preserved
    assert out["ready_for_execution"] is False
    assert any("PASS" in str(i.get("message", "")) for i in out["issues"])


def test_pass_with_items_untouched():
    rep = {"overall_assessment": "PASS", "confidence_score": 0.9,
           "items_extracted": 10}
    assert _sanitize_pass_verdict(rep) is rep


def test_fail_reports_untouched():
    rep = {"overall_assessment": "FAIL", "items_extracted": 0}
    assert _sanitize_pass_verdict(rep) is rep
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_pass_sanitizer.py -q'`
Expected: FAIL with `ImportError: cannot import name '_sanitize_pass_verdict'`.

- [ ] **Step 3: Implement**

In `webapp/agents/graph.py` above `_invoke_code_tester` (8109):

```python
def _sanitize_pass_verdict(report: dict) -> dict:
    """[wave-37 W37-NEW-B] A PASS over zero real extracted items is not a PASS.

    Prod 672: tester emitted PASS conf 0.25 with nothing extracted; the
    exhausted arm refused execution (correct) and the job died ON A PASS.
    Force-FAIL the shape at the report boundary (wave-22 B4 idiom: keep the
    tester's own confidence in phase2_confidence so routing can tell a
    tester PASS from a sanitizer override). The working-scraper-rescue arm
    (route_after_testing._terminal arm, real-items gate) then behaves:
    PASS ⇒ items exist ⇒ rescue routes to run_execution.
    """
    if not isinstance(report, dict):
        return report
    if str(report.get("overall_assessment") or "").strip().upper() != "PASS":
        return report
    items = int(report.get("items_extracted") or 0)
    if items > 0:
        return report
    report["phase2_confidence"] = report.get("confidence_score")
    report["overall_assessment"] = "FAIL"
    report["confidence_score"] = 0.0
    report["ready_for_execution"] = False
    report.setdefault("issues", []).insert(0, {
        "issue_type": "false_pass",
        "message": (
            "PASS verdict carried zero extracted items — sanitizer forced FAIL "
            "[wave-37 W37-NEW-B]; fix the extraction or the emission filter."
        ),
    })
    return report
```

Call it in `_invoke_code_tester` immediately after the report is loaded/normalized and before the existing force-FAIL arms (search for the first `report["overall_assessment"] = "FAIL"` in the function, ~:8595) and again after each existing force-FAIL arm cannot fire — simplest correct placement: wrap the report-load helper's return so every downstream consumer sees the sanitized dict. The tester's `items_extracted` key: confirm the report contract's real item-count key (grep `items_extracted|extracted_count|item_count` in `build_code_tester_message` / report parse) and use THAT key — the tests above must be updated to the actual key name; do not invent a second count field.

- [ ] **Step 4: Run test to verify it passes** — targeted PASS; then the wave-34 F4 ladder tests (`pytest ../tests -k "f4 or ladder" -q`) stay green (the sanitizer must not flip genuine FAIL flows).

- [ ] **Step 5: Commit**

```bash
git add webapp/agents/graph.py tests/test_wave37_pass_sanitizer.py
git commit -m "feat(wave-37 W37-NEW-B): PASS-with-zero-items sanitizer at the tester report boundary (prod 672)"
```

---

### Task 5: Intake PDP coercion (W37-NEW-C part 1)

7 of 33 failures (626/625/622/620/614/602/598) submitted item URLs (`/products/…`, `/p/…`, `/items/…`) with `nav_method=listing` (`input_mode=list_page`); the traverse then harvests recommendation carousels and dies on the cross-domain guard. Coerce at intake — the same place the wave-32 B3 host gate lives (views.py:3003-3036) — so the pipeline gets the mode it can actually run.

**Files:**
- Create: `src/intake_coerce.py`
- Modify: `webapp/scraper/views.py:2991` (`intake_create_job`)
- Test: `tests/test_wave37_intake_coerce.py`

**Interfaces:**
- Produces: `coerce_pdp_intake(url: str, nav_method: str) -> tuple[str, str | None]` — `(effective_nav_method, note)`; `note` is None when no coercion happened. Consumed by `intake_create_job` before `_INTAKE_NAV_TO_INPUT_MODE` mapping.

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-NEW-C] PDP-shaped URLs submitted as listing pages coerce to
PDP mode at intake. Prod 626-class (7 jobs): /products/, /p/, /items/ URLs
with nav_method=listing → the traverse treats the PDP as a category page,
harvests off-domain recommendation carousels, and the cross-domain guard
kills the job twice over. Intake knows the URL shape cheapest.
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from src.intake_coerce import coerce_pdp_intake


def test_pdp_path_coerces_listing_to_url_list():
    mode, note = coerce_pdp_intake(
        "https://www.childrensplace.com/us/p/Girls-Active-Short-Sleeve-Top", "listing"
    )
    assert mode == "pdp"
    assert note and "/p/" in note


def test_pdp_path_coerces_search_too():
    mode, _ = coerce_pdp_intake("https://example.com/products/blue-shirt-12345", "search")
    assert mode == "pdp"


def test_category_path_untouched():
    mode, note = coerce_pdp_intake("https://example.com/collections/all", "listing")
    assert mode == "listing"
    assert note is None


def test_homepage_untouched():
    mode, note = coerce_pdp_intake("https://example.com/", "listing")
    assert mode == "listing"


def test_url_list_nav_untouched():
    mode, note = coerce_pdp_intake("https://example.com/p/x", "url_list")
    assert (mode, note) == ("url_list", None)


def test_multi_segment_pdp_paths():
    for path in ("/shop/items/girls-top-88231", "/store/products/widget"):
        mode, _ = coerce_pdp_intake(f"https://example.com{path}", "listing")
        assert mode == "pdp", path


def test_shop_category_listing_not_coerced():
    # "shop"/"store" are category-page prefixes, not PDP markers —
    # a genuine listing under /shop/ must keep its mode.
    for path in ("/shop/all", "/store/collections/kitchen"):
        mode, note = coerce_pdp_intake(f"https://example.com{path}", "listing")
        assert mode == "listing", path
        assert note is None


def test_generic_deep_path_is_not_pdp():
    # deep non-merch paths (articles, help pages) must not coerce
    mode, note = coerce_pdp_intake("https://example.com/blog/how-to-choose", "listing")
    assert mode == "listing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_intake_coerce.py -q'`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.intake_coerce'`.

- [ ] **Step 3: Implement**

`src/intake_coerce.py`:

```python
"""[wave-37 W37-NEW-C] Intake-mode coercion for PDP-shaped URLs.

Prod evidence 09-15→09-18: 7 jobs (626/625/622/620/614/602/598) submitted
item URLs as listing pages; every one died on the cross-domain guard after
the traverse mined the PDP's recommendation carousels. The URL shape is
decidable at intake for free.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Merch-path evidence from the prod failures + the platform defaults
# (Shopify /products/, SFCC /p/, Magento /items|product/). "shop"/"store"
# are deliberately EXCLUDED — they are common CATEGORY-page prefixes
# (/shop/all) and would coerce genuine listings. Matched against ANY
# non-final path segment: prod 666's PDP is /us/p/Girls-Active-… — a
# locale prefix sits before the merch segment.
_PDP_SEGMENTS = frozenset({
    "products", "product", "p", "items", "item", "dp",
})


def coerce_pdp_intake(url: str, nav_method: str) -> tuple[str, str | None]:
    """Return the effective nav method, coercing PDP-shaped listing/search
    submissions to ``pdp`` (the pipeline's url_list single-item mode)."""
    if nav_method not in ("listing", "search"):
        return nav_method, None
    path = (urlparse(url).path or "").strip("/")
    segs = [s.lower() for s in path.split("/") if s]
    if len(segs) >= 2 and any(s in _PDP_SEGMENTS for s in segs[:-1]):
        hit = next(s for s in segs[:-1] if s in _PDP_SEGMENTS)
        return "pdp", (
            f"URL looks like an item page ('/{hit}/…') — coerced from "
            f"'{nav_method}' to PDP mode [wave-37 W37-NEW-C]."
        )
    return nav_method, None
```

Wire into `intake_create_job` (views.py:2991) BEFORE the mode map:

```python
    from src.intake_coerce import coerce_pdp_intake

    effective_nav, coerce_note = coerce_pdp_intake(url, nav_method)
    if coerce_note:
        notes = f"{notes} | {coerce_note}".strip(" |")
    input_mode = _INTAKE_NAV_TO_INPUT_MODE.get(effective_nav, "url_list")
```

(Confirm `_INTAKE_NAV_TO_INPUT_MODE` has a `pdp` entry or maps it to `url_list` — read views.py:2648-2660; if absent, add `"pdp": "url_list"` with a comment so the intake radio and coercion share one vocabulary.)

- [ ] **Step 4: Run test to verify it passes** — targeted PASS; then `pytest ../tests -k intake -q` (the W31 duplicate-gate tests) stay green.

- [ ] **Step 5: Commit**

```bash
git add src/intake_coerce.py webapp/scraper/views.py tests/test_wave37_intake_coerce.py
git commit -m "feat(wave-37 W37-NEW-C): coerce PDP-shaped listing/search intakes to PDP mode (prod 626-class, 7 jobs)"
```

---

### Task 6: Traverse degrade-to-on-domain instead of double-refusal death (W37-NEW-C part 2)

When a genuine listing submission DOES harvest off-domain noise (klaviyo static forms, vinted off-domain rows), graph.py:3892-3927 aborts the job after ONE forced re-traverse still reads contaminated. When the contamination is a minority of links, the honest move is to drop the off-domain rows and continue with the clean remainder — the abort stays only for majority-off-domain or off-domain `api.url` (the wave-34 F3 veto semantics at graph.py:3588-3610).

**Files:**
- Modify: `webapp/agents/graph.py` (`_nav_result_contamination` at ~:3522-3610 + the double-refusal arm at :3890-3927)
- Test: `tests/test_wave37_traverse_degrade.py`

**Interfaces:**
- Consumes: `_nav_result_contamination`'s reason strings; `src.registrable.registrable_of` (same comparator as the intake host gate).
- Produces: `_strip_off_domain_links(result, job_url) -> (clean_result, dropped_n)` used before the abort arm.

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-NEW-C part 2] Minority off-domain contamination in a
traversal result degrades (drops the bad links, keeps the clean run)
instead of killing the job after one forced re-traverse (prod 626-class).
Majority off-domain / off-domain api.url still aborts — the wave-34 F3
veto semantics are unchanged.
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from webapp.agents.graph import _strip_off_domain_links


class _R:
    def __init__(self, links, goal_url=None, api_url=None):
        self.item_links = links
        self.goal_url = goal_url
        self.api_url = api_url


JOB = "https://www.example.com/shop"


def test_minority_off_domain_links_dropped():
    links = ["https://www.example.com/p/1", "https://www.example.com/p/2",
             "https://static.klaviyo.com/form", "https://www.vinted.net/x"]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert clean.item_links == ["https://www.example.com/p/1",
                                "https://www.example.com/p/2"]
    assert dropped == 2


def test_majority_off_domain_not_strippable():
    links = ["https://a.com/x", "https://b.com/y", "https://www.example.com/p/1"]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert clean is None and dropped == 2


def test_off_domain_api_url_never_stripped():
    links = ["https://www.example.com/p/1"]
    clean, dropped = _strip_off_domain_links(_R(links, api_url="https://cdn.no/x"), JOB)
    assert clean is None


def test_clean_result_passes_through():
    links = ["https://www.example.com/p/1"]
    clean, dropped = _strip_off_domain_links(_R(links), JOB)
    assert dropped == 0 and clean.item_links == links
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_traverse_degrade.py -q'`
Expected: FAIL with `ImportError: cannot import name '_strip_off_domain_links'`.

- [ ] **Step 3: Implement**

In graph.py next to `_nav_result_contamination`:

```python
def _strip_off_domain_links(result, job_url: str):
    """[wave-37 W37-NEW-C] Drop minority off-domain item_links; keep the run.

    Returns (clean_result, dropped_count); (None, total) when stripping
    cannot save the run: majority off-domain, or an off-domain api.url
    (wave-34 F3: identity-bearing, never silently rewritten).
    """
    links = list(getattr(result, "item_links", []) or [])
    if not links:
        return result, 0
    job_reg = registrable_of(job_url)
    clean = [u for u in links
             if registrable_of(u) == job_reg]
    dropped = len(links) - len(clean)
    if getattr(result, "api_url", None) and registrable_of(result.api_url) != job_reg:
        return None, dropped or len(links)
    if not clean or dropped > len(clean):
        return None, dropped
    result.item_links = clean
    return result, dropped
```

(Import `registrable_of` from `src.registrable` — same module the intake gate uses at views.py:3010.) In the double-refusal arm (:3890-3927), BEFORE the first `_bad` check's abort branch: attempt the strip; if it returns a clean result, log `browser_traverse: degraded … dropped N off-domain link(s)` and proceed with it; keep today's abort exactly for the `(None, …)` case and for the still-contaminated-after-re-traverse path.

- [ ] **Step 4: Run test to verify it passes** — targeted PASS; `pytest ../tests -k "f3 or cross_domain or contamination" -q` stays green (veto semantics preserved).

- [ ] **Step 5: Commit**

```bash
git add webapp/agents/graph.py tests/test_wave37_traverse_degrade.py
git commit -m "feat(wave-37 W37-NEW-C): traverse degrades minority off-domain noise instead of double-refusal death"
```

---

### Task 7: No-op strategy remap — verification-first (W37-NEW-E)

Scan claims "analyzer re-picks the same strategy (playwright→playwright)" on 671/672, but `_decide_strategy` already feeds `strategies_tried` into `_escalate_strategy`, whose docstring forbids re-picking tried strategies (graph.py:4751-4795). Either the scan misread the logs, or an append path leaks (the tier-axis arm at graph.py:~4615-4680 appends `_new_tried` entries whose `strategy` key shape may not match `_all_tried`'s extraction at :4602-4606). DO NOT write the fix before the failing signature exists.

**Files:**
- Test: `tests/test_wave37_strategy_history.py`
- Modify (conditional): `webapp/agents/graph.py:4545-4680`

**Interfaces:**
- Consumes: `ScrapeState.strategies_tried` (`Annotated[list, operator.add]`, state.py:200).
- Produces: invariant "after every failed testing cycle, `_all_tried` contains every strategy that failed" — enforced by test.

- [ ] **Step 1: Extract the observed signature (no code changes)**

Pull 671/672 SessionLog rows + the [CASCADE] log lines from prod (read-only API) and quote the exact strategy sequence into this plan file under §5 (append a "Task 7 evidence" subsection). If the logs show playwright→playwright across cycles, capture which arm wrote each `strategies_tried` entry (the [CASCADE] log prefix names the arm).

- [ ] **Step 2: Write the failing test**

```python
"""[wave-37 W37-NEW-E] Strategy history must accumulate on EVERY failed
cycle so the deterministic re-derivation can never re-pick a tried+failed
strategy (the scan's playwright→playwright no-op remap, prod 671/672).
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from webapp.agents.graph import _decide_strategy


def test_second_cycle_escalates_past_failed_playwright():
    state = {
        "strategies_tried": [{"strategy": "playwright", "reason": "test FAILED"}],
        "site_analysis": {"method_that_worked": "browser_datacenter",
                          "scraping_method": "playwright"},
        "skip_approvals": True,
        "input_mode": "url_list",
    }
    update = _decide_strategy(state)
    chosen = (update.get("scraper_analysis") or update).get("strategy")
    assert chosen != "playwright", (
        f"re-picked tried+failed strategy: {chosen!r}"
    )
```

(Adapt the state keys to what `_derive_strategy` actually reads — read graph.py:5189-5250 first and construct minimal evidence so the FIRST-cycle derivation would choose playwright, then assert cycle 2 escalates.)

- [ ] **Step 3: Run it** — if it PASSES at clean HEAD, the no-op remap does NOT reproduce in `_decide_strategy`; record that in §5 and trace the 671/672 signature to its actual arm (tier-axis append shape or the route_after_testing fix-cycle path skipping scraper_analyzer entirely — check whether the writer-fix route re-enters `_decide_strategy` at all). Only when the failing signature is pinned does Step 4 apply.

- [ ] **Step 4: Fix the pinned leak** — expected shape (adjust to the pinned path): normalize the strategy key when appending in the tier-axis arm so `_all_tried`'s extraction (graph.py:4602-4606) matches; or append `strategies_tried` from route_after_testing's failed-cycle exits via a node-side update (routers cannot mutate state).

- [ ] **Step 5: Run suite subset** — `pytest ../tests -k "strategy or escalate" -q`; expect the new test green, no regressions.

- [ ] **Step 6: Commit**

```bash
git add tests/test_wave37_strategy_history.py webapp/agents/graph.py
git commit -m "fix(wave-37 W37-NEW-E): strategy history accumulates on every failed cycle — no more no-op remaps"
```

---

### Task 8: One bounded ladder auto-repair before honest-fail (W37-NEW-D)

`ladder_preservation_violation` (draft_safety.py:153) currently hard-fails the draft (route_after_testing.py:1717-1733, graph.py:8799). For 628/623/600 the drafts were otherwise sound — only the proxy ladder wiring was stripped. Give the router ONE deterministic repair attempt: re-inject the shared-ladder import + `fetch_page` construction from the wave-19 T1.1 contract, re-test the repaired draft; a second violation (or any repair failure) takes today's honest-fail path.

**Files:**
- Modify: `webapp/agents/draft_safety.py` (new `repair_ladder_violation`), `webapp/agents/nodes/route_after_testing.py:1717-1733` (repair arm), `webapp/agents/state.py` (declare `ladder_repairs`)
- Test: `tests/test_wave37_ladder_repair.py`

**Interfaces:**
- Consumes: `ladder_preservation_violation(text, strategy) -> list[str]` (existing), `src.http_fetch` contract (`create_fetch_page`, `_LADDER_CALLEES` at draft_safety.py:38-45).
- Produces: `repair_ladder_violation(text: str, strategy: str) -> str | None` — repaired source or None when unrepairable; state key `ladder_repairs` (declared, budget 1).

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-NEW-D] One deterministic ladder-repair arm before the
honest-fail. The repair is textual and boring: inject the src.http_fetch
import + create_fetch_page wiring when the draft fetches with bare
requests but is otherwise parseable. A draft that cannot be repaired (or a
second violation) keeps today's fail path.
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

import ast

from webapp.agents.draft_safety import (
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

SESSION = __import__("requests").Session()

def scrape(url):
    fetch_page = create_fetch_page(SESSION)
    r = fetch_page(url)
    return r.get("text", "")
'''


def test_stripped_draft_violates_then_repairs():
    assert ladder_preservation_violation(STRIPPED, "http_navigation")
    repaired = repair_ladder_violation(STRIPPED, "http_navigation")
    assert repaired is not None
    ast.parse(repaired)  # repair must produce parseable source
    assert not ladder_preservation_violation(repaired, "http_navigation")


def test_wired_draft_repairs_to_none():
    assert repair_ladder_violation(WIRED, "http_navigation") is None


def test_exempt_strategy_repairs_to_none():
    assert repair_ladder_violation(STRIPPED, "playwright") is None


def test_unparseable_draft_unrepairable():
    assert repair_ladder_violation("def broken(:", "http_navigation") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_ladder_repair.py -q'`
Expected: FAIL with `ImportError: cannot import name 'repair_ladder_violation'`.

- [ ] **Step 3: Implement**

In `draft_safety.py`:

```python
_LADDER_REPAIR_IMPORT = (
    "from src.http_fetch import create_fetch_page  # [wave-37 W37-NEW-D] re-injected"
)
_LADDER_REPAIR_FACTORY = (
    "\n\n_LADDER_FETCH_PAGE = create_fetch_page(__import__('requests').Session())\n"
)


def repair_ladder_violation(text: str, strategy: str) -> str | None:
    """[wave-37 W37-NEW-D] Deterministic ladder re-injection. Returns the
    repaired source when the draft (a) parses, (b) is strategy-non-exempt,
    (c) fetches via bare requests/session calls the gate recognizes, and
    the injection satisfies the gate afterwards. Anything else → None
    (caller keeps the honest-fail)."""
    if strategy in LADDER_EXEMPT_STRATEGIES:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    if not any(
        isinstance(n, ast.ImportFrom) and n.module == "src.http_fetch"
        for n in ast.walk(tree)
    ):
        text = _LADDER_REPAIR_IMPORT + "\n\n" + text
    if "create_fetch_page(" not in text:
        # insert the factory after the last top-level import
        lines = text.splitlines(keepends=True)
        idx = max(
            (i for i, ln in enumerate(lines)
             if ln.startswith(("import ", "from "))), default=-1,
        )
        text = "".join(lines[: idx + 1]) + _LADDER_REPAIR_FACTORY + "".join(lines[idx + 1:])
    try:
        ast.parse(text)
    except SyntaxError:
        return None
    if ladder_preservation_violation(text, strategy):
        return None
    return text
```

In route_after_testing.py's ladder arm (1717-1733): when `ladder_repairs` budget (declare in state.py near the other counters, default 0, `Annotated`-free int) is unspent, write the repaired source back to `workspace/{slug}/scraper_draft.py`, increment the counter via the tester node's update (routers can't — mirror the `forced_retest_count` consumption pattern: set the flag in the router's return string, consume in `_invoke_code_tester`), and route to `code_tester`; the second violation fails honestly as today.

- [ ] **Step 4: Run test to verify it passes** — targeted PASS; `pytest ../tests -k "ladder or t1_1" -q` stays green.

- [ ] **Step 5: Commit**

```bash
git add webapp/agents/draft_safety.py webapp/agents/nodes/route_after_testing.py webapp/agents/state.py tests/test_wave37_ladder_repair.py
git commit -m "feat(wave-37 W37-NEW-D): one bounded ladder auto-repair arm before honest-fail (prod 628/623/600)"
```

---

### Task 9: Auth-wall stop_reason + saturation observability (W37-NEW-F + W37-OPS rider)

**Files:**
- Modify: `webapp/agents/graph.py` (browser_traverse result handling, near the Phase-1 discovery crash path 652 exercised), `browser_service/server.py` (maintenance loop gauge)
- Test: `tests/test_wave37_auth_wall.py`

**Interfaces:**
- Produces: discovery `stop_reason: "auth_wall"` (site-side, honest) recognized by the existing zero-discovery verdict normalization (graph.py:5056 "navigate_unavailable" precedent).

- [ ] **Step 1: Write the failing test**

```python
"""[wave-37 W37-NEW-F] A sign-in redirect during discovery is a SITE-SIDE
stop (auth_wall), not a draft traceback. Prod 652 goodreads: Phase-1
discovery crashed on the login redirect and the job burned cascade arms
"fixing" an unfixable wall.
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()

from webapp.agents.graph import classify_discovery_failure


def test_signin_redirect_is_auth_wall():
    assert classify_discovery_failure(
        "Sign in to Goodreads — https://www.goodreads.com/user/sign_in?redirect=…"
    ) == "auth_wall"


def test_create_account_prompt_is_auth_wall():
    assert classify_discovery_failure("Please log in or create an account to continue") == "auth_wall"


def test_plain_500_is_not_auth_wall():
    assert classify_discovery_failure("HTTP 500 Internal Server Error") != "auth_wall"


def test_empty_is_none():
    assert classify_discovery_failure("") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests/test_wave37_auth_wall.py -q'`
Expected: FAIL with `ImportError: cannot import name 'classify_discovery_failure'`.

- [ ] **Step 3: Implement**

`classify_discovery_failure(text: str) -> str | None` in graph.py near the discovery-coverage verdict code: match `sign_in`/`sign in to`/`log in`/`create an account`/`password` prompt markers against the Phase-1 failure text (the 652 traceback body). Wire at the point where a Phase-1/discovery exception is recorded: when it returns `"auth_wall"`, write `discovery_coverage.stop_reason = "auth_wall"` + a user-facing honest message ("site requires sign-in; not scrapeable anonymously") and route to cleanup-fail — never into the writer fix cycle. Extend the zero-discovery verdict normalization (graph.py:5056 region) with the new reason.

Observability rider (W37-OPS, no behavior change): in server.py's maintenance loop, log the memory ratio + poison-snapshot + ephemeral-call count every cycle at INFO (today only the gate trip logs), so the next saturation RCA has the climb curve from service logs alone.

- [ ] **Step 4: Run test to verify it passes** — targeted PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/agents/graph.py browser_service/server.py tests/test_wave37_auth_wall.py
git commit -m "feat(wave-37 W37-NEW-F): discovery auth-wall is a site-side stop_reason (prod 652) + saturation gauges in maintenance loop"
```

---

### Task 10 (DECISION-NEEDED, flag-default-off): sustained-pressure proactive recycle (W37-OPS)

Memory re-saturates within ~24h under batch load (3 events in 3 days: 99.2% @28.8h on 09-17, 1.00 @38.7h on 09-18). The reactive gate (429s at 0.90) works but sheds live jobs. OPTIONAL containment: when the cgroup ratio stays ≥ `BROWSER_RECYCLE_RATIO` (default 0.75) for `BROWSER_RECYCLE_SUSTAINED_S` (default 600s), the maintenance loop recycles the SCRAPER Chrome only (never MCP — analyzer/tester sessions ride it) between navigations, behind `BROWSER_PROACTIVE_RECYCLE=0` default-off.

- Ship only after user approves the tradeoff (recycling under load costs in-flight scraper runs; the env flag keeps prod behavior identical until enabled).
- **Implementation is the same shape as `_maybe_recycle_scraper_chrome` (server.py:1216)** — extend its trigger with the sustained-ratio condition, tracking `sustained_high_since` in module state; add 2 tests in `tests/test_wave37_proactive_recycle.py` for the trigger arithmetic (ratio below threshold resets the clock; sustained-above fires; flag-off never fires). Follow Task 1's TDD steps; commit as `feat(wave-37 W37-OPS): opt-in sustained-pressure scraper-chrome recycle`.

---

## §4 Verification plan

1. **Full suite:** `docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'` — expect 3231+green baseline plus new wave-37 tests, with exactly the 4 known pre-existing reds.
2. **Migration:** `makemigrations --check --dry-run` clean; `migrate` applied locally; celery-worker + django restarted ONCE after graph edits.
3. **Dead-seed e2e (existing sentinel-twin harness):** re-drive 2 gate sites (658 westelm + 498 briscoes) via `scripts/run_wave36_baseline_gate.py drive` to prove no wave-37 regression on the wave-36 happy path (both must PASS as in §10 of the wave-36 plan).
4. **Class-replay checks (local, no prod writes):** Task 5's coercion + Task 6's degrade against the 7 prod URLs' path shapes (unit-level, no target-site fetches); Task 3's classifier against the 671/672 report text quoted in §5.
5. **Wave-36 full-pipeline e2e (running in parallel, separate agent):** results append to §5 as the baseline sanity check — wave-37 merges only if that e2e shows no wave-36 regression; the e2e is not blocked on wave-37 tasks.
6. **Ruff:** `docker compose exec django ruff check webapp/ src/` clean.
7. **Sync + PR:** tree-sync to ExtractorBuilderAi (`sync-w37`), push branch + fork main, `merge-tree` clean vs upstream, hand the compare link (user opens/merges the PR — standing rule). Deploy order: django+celery BEFORE browser-service. Prod watch item after deploy: the memory saturation curve (flattening within 24h of batch load = Task 10 / ops lever working or needed).

## §5 Evidence appendix (filled during execution)

### Task 7 evidence (pinned 2026-09-18, prod SessionLog via /jobs/<id>/logs/)

Job 671 (cabelas-com), seq 170→171 — the no-op remap, verbatim:

```
[170] route_after_testing [CASCADE] action=strategy-ladder retry=0/2 strategy=playwright remaps=0
      reason=no items extracted — likely wrong strategy
[171] scraper_analyzer   [CASCADE] action=code-fix (strategy rerun) strategy=playwright→playwright
      reason=scraper_analyzer re-picked the same strategy
```

Job 672 (cb2-com), seq 126→127 — identical pair. Surrounding 671 context: seq 119/142/286/317
`action=retest … reason=rate limited (429) — back off and re-test (BROWSER_SERVICE_UNAVAILABLE: …)`
— cycles 1–2 were 429 RETESTS (correctly: unproven coverage, no history append), then seq 170's
cycle died on a plain zero-item read and the ladder fired into an empty `strategies_tried`.

**Reproduction (unit, at pre-fix HEAD):** `_decide_strategy` with
`strategies_tried=[{strategy: http_navigation}]` (recorded in an earlier cycle),
`scraper_analysis={strategy: playwright}` (escalation's pick),
`test_report={overall_assessment: PASS, confidence_score: 0.25, successful_extractions: 0}`
→ gate `_prior_report.get("overall_assessment") not in (None, "PASS")` CLOSED (false PASS),
playwright never recorded, `_all_tried={http_navigation}` → derive http_navigation → tried →
escalate → **playwright again, goto=code_writer** — the exact `playwright→playwright` rerun.
(All `browser*` probe methods derive http_navigation — prod's playwright WAS the escalation,
so the history-shape above is the true minimal repro, not a fresh playwright derivation.)

**Fix applied:** `_decide_strategy` now counts a PASS with zero extracted items
(`report_extracted_items`) as a failed cycle for the tried-append gate → playwright enters
history → escalation lands on exhausted (cleanup/human_approval) instead of the no-op rerun.
T4's sanitizer is the upstream leg (tester node flips the report before routing); this is the
belt at the decision node itself (resume paths can deliver raw state).

- Task 3/4: code-tester report items-count keys are `successful_extractions` /
  `extracted_items` / `item_count` (top-level AND nested under `results`) — there is NO
  `items_extracted` key; both gates read the shared `report_extracted_items` ladder
  (browser_http). The 671/672 outage banner text (verbatim, from the retest reasons above):
  `BROWSER_SERVICE_UNAVAILABLE: the browser-service gateway itself cannot serve this run …`
- Task 3/4: 671 report keys PINNED from prod SessionLog [TOOL] rows (2026-09-18 read): the
  items-count evidence lives at the NESTED `results.successful_extractions` (0) — `"results":
  {"successful_extractions": 0, "failed_extractions": 1, "skipped_dead_urls": 0,
  "discovered_urls_phase1": 0, "phase1_stop_reason": "empty_render", ...}`; no
  `extracted_items`/`item_count` key appears anywhere in the 671 log. The outage banner the
  writer relayed as an issue message (seq 67, verbatim): `BROWSER_SERVICE_UNAVAILABLE: the
  browser-service gateway itself cannot serve this run (browser_service throttled the scrape
  (HTTP 429) after 3 attempt(s) — backpressure, re-run may succeed). This is an infrastructure
  outage, NOT a site or scraper problem — do not strategy-switch and do not rewrite the
  scraper; record the phase as not testable due to the outage.` NOTE the banner itself carries
  `HTTP 429` — the exact text the narrow-issue/wide-crash channel split exists for (a
  narrow-only filter would have called this banner a "real defect" and excused nothing).
- Task 9 (auth-wall) shipped: `classify_discovery_failure` in graph.py; tester probe crash arm
  routes `auth_wall` straight to cleanup-fail (FAILED + stop_reason=auth_wall, F13 Command
  route); `_route_after_execution` normalizes an auth_wall zero-item execution to honest FAILED
  cleanup, never the ladder.
- Task 10 (W37-OPS) shipped FLAG-OFF: `browser_service/recycle_policy.py`
  (SustainedPressureTracker) + second trigger in `_maybe_recycle_scraper_chrome` behind the
  scrape guard; `BROWSER_PROACTIVE_RECYCLE` default 0 — enablement stays a user decision after
  the next saturation event's data. Saturation gauges (T9 rider) log one INFO line per liveness
  cycle: `saturation gauge: mem_ratio=… poisoned_threads=N poison_events_1h=N ephemeral_calls=N`.
- Wave-36 e2e baseline results (2026-09-18, full-pipeline via `POST /intake/create-job/` force=1, one fire each): **3/3 PASS** — 411 westelm 10/10 in 79min (all 5 alias/LLM chips resolved canonical, discovered=50, 0 escalations); 412 renttherunway 10/10 in 108min (no-chips identity lane: no FIELD-MAP row, zero renames, 20-field raw shape intact, one legit tester→writer fix cycle); 413 au-yotoplay 1/1 scope-satisfied in 25min (all 5 chips canonical; delta vs sentinel's 5/5 fully explained by the wave-34 T34-2 `[INTAKE-PDP]` flip coercing the PDP-shaped seed to url_list — mode coercion, not extraction loss). Cross-checks: alias→canonical ✓ both legs exercised, identity lane ✓, JSON soft-block escalations 0 everywhere, zero shortfall-gate misfires, no gate bypasses, no COMPLETED-with-zero. §4.5 merge gate: SATISFIED (no wave-36 regression under full-pipeline conditions).
- NEW candidate observed during e2e (→ wave-38 candidate, NOT a wave-37 task): **cross-job network-capture bleed in the shared browser-service** — while 411/412 ran concurrently, job 412's traverse captured `https://www.westelm.com.au/api/items` (411's site): `dropping off-domain api capture (job 412)` + `CROSS-DOMAIN traversal result (item_links 12/12 off-domain) — ONE forced re-traverse` (13:21:12Z). Contained by the wave-32 B3 host gate, but capture attribution across concurrent jobs sharing one Chrome is a real defect class.
- Dead-seed re-drive of the 2 gate sites (§4 verification item 3, via `scripts/run_wave36_baseline_gate.py drive`, sentinels 414/416, drives 415/417, 2026-09-18): **2/2 PASS** — 415 westelm-com-au completed 10/10 (discovered=70, FIELD-MAP 4 canonical/1 custom, 0 escalations); 417 briscoes-co-nz completed 9 + 1 site-side-empty NOTE (discovered=1426, FIELD-MAP 5 canonical/0 custom, soft_block_escalations=None — gate counts count+failed covering the window). No wave-37 regression on the wave-36 dead-seed happy path.

## DECISION-NEEDED (user)

1. **W37-OPS (Task 10):** enable proactive sustained-pressure scraper-Chrome recycle? Recommended default: build it flag-off, decide enablement after the next saturation event's data (the gate+429 path currently degrades safely; the recycle trades in-flight runs for headroom).
2. **Railway memory limit 6GB→8GB** (user-owned ops lever, no code): cheapest mitigation for the ~24h saturation trend; can be done independently of this wave.
3. **Scope:** Task 7 is verification-first and may end in "no code change" — confirm that's an acceptable wave outcome if the remap claim doesn't reproduce.
