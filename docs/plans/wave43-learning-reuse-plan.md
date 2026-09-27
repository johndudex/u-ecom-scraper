# Wave-43 — Learning & Reuse: findings + plan (DRAFT, awaiting green light)

> **SCOPE UPDATE 2026-09-26 (owner directive):** Track B (wall-clock/writer↔tester
> changes) and Track C (runtime strategy changes) are **DROPPED** — prod is stable and
> nothing may risk it. Wave-43 is re-scoped to a **knowledge harvest from ALL 408
> successful prod jobs** (orchestrator + worker agents, coverage-reconciled, none
> skipped) → critiqued skill-seeding plan. See `wave43-skill-seeding-plan.md` once
> written; the findings below remain the evidence base.

2026-09-26 · Evidence: 4 parallel audits (learning write paths, learning read paths,
wall-clock mining over 130 completed local jobs, prod outcome mining over 576 board
rows) + spot-checks of the load-bearing claims. Raw agent reports preserved in the
session transcript; the numbers below are theirs, spot-checked where cited.

---

## 1. The answer to "is the system learning from its 400+ successes?"

**Short answer: one thin memory works; every rich learning channel is broken, orphaned
or structurally dead. The system is not lying about what it does — it does almost
nothing, by construction.**

### What DOES flow back into a new job today (verified closed loops)

| Loop | Evidence | Reach |
|---|---|---|
| `writer_memory.json` per-site block + retry fingerprint | 19 FM files; 17 verified `SITE MEMORY` prompt injections; failure-fingerprint lines on retries | 19/66 local sites; content is thin by design ("completed with 3475 items via http_requests") |
| `_prior_count_line` | 53 injections / 22 jobs | One sentence: "a previous run extracted N items" |
| Field mapping (wave-36/41) | 26 jobs carry `field_mapping` blobs; consumed by `check_tracker._fields_changed` + intake cache | Per-site field names only |
| Probe cache (4h, prod) | Live `Last-Used` updates; skips proxy/transport escalation re-discovery | Domain-level, short horizon |
| FM analysis archive re-hydration | 57/65 sites have `analysis/`; `[RESUME-INVOCATION]` ×40 | Undermined: `site_analysis.json` preserved for only **9/65** sites → `skip_site_analysis` usually can't fire |

### What is broken, orphaned or dead (the gap)

| # | Channel | Status | Root cause (spot-checked) |
|---|---|---|---|
| 1 | `skill_learner` → `learning_report.json` | **WRITE-ONLY** | 32 rich lesson files (latest 09-23); **zero consumers**; prompt forbids applying (`subagents.py:5394`); `"applied_count"` fields are LLM self-report fiction |
| 2 | `nav_skill_review` (sole skill writer) | **DEAD since browser_traverse migration** | Its precondition `navigation_findings.json` is produced by `navigation_explore`, **commented out of the graph** (`graph.py:10844-48`). Fired once ever (job 24, Jul-16). Live worker log: bails every job. All 26 `## Learned:` sections in prod skills are hand-committed git files — zero agent-written |
| 3 | `LEARNED SKILL NOTES` (deterministic cross-site reader) | **0 injections ever** | Triple structural failure: (a) only sfcc has learned sections among the 5 `_PLATFORM_SKILLS`; (b) that section is 2498 chars vs a 1500-char **whole-section cap that skips, never truncates** (`skills_store.py:200-213`); (c) some platform strings don't contain the alias. The 23 sections that DO exist live in technique skills not in `_PLATFORM_SKILLS` at all |
| 4 | check_tracker selective skip flags | **Reachable, never fired locally** | Requires the Re-run/rescrape flow (`parent_job` null on all 340 local jobs). Campaign restarts on prod DO take the rescrape path (force_full only on explicit post — verified), but retried rows are never-succeeded → mostly no completed priors to reuse |
| 5 | Prior scraper as code_writer base | **DEAD** | 400+ finished scrapers sit in FM; writer always starts from pristine `templates/{file}` (`graph.py:7073-82`) |
| 6 | Cross-site strategy learning | **DOES NOT EXIST** | `strategies_tried` is job-local (`state.py:227`); no code path feeds prior outcomes into `_decide_strategy`. A "Magento PWA needs rendering" lesson has nowhere to land |
| 7 | Navigation reuse | **DEAD** | `browser_traverse` re-browses from the homepage every run; no skip flag, no prior-art gate (`graph.py:4119-4169`) |
| 8 | SCRAPER_CHECKPOINT_REUSE | **Flagged-off locally; prod ON since 09-26** | Even armed: checkpoint file excluded from preservation (`setup_workspace.py:256-57`) and deleted after success (`run_execution.py:2488`), and discovery re-runs `--fresh-discovery` unconditionally — it is a zero-rescue lane, not a reuse lane |

**Why jobs take hours (130 completed local jobs, 80h wall):** p50 28m / p90 69m.
**60% is the writer↔tester loop** (code_generation 38% + testing 22%); each extra fix
cycle costs 12–25m (1 writer run → 27m median job, 3 → 52m); dead LLM invocations
burn 1800–2700s each and restart cold (198 events across 14 jobs); 11% of wall is
post-success phases (skill_learning 6% + dagster 5%). Queue wait is nil. There is a
bitter irony here: the biggest time sink (writer fix-cycles) is exactly where prior
site knowledge — the lessons in #1, the scrapers in #5 — would save the most, and none
of it is consulted.

**Prod corroboration:** 70% completion; 33% of completions needed >1 attempt;
final successful runs faster than their failed predecessors in 5/6 comparable chains;
deep-retry successes 39.7m vs 59m first-try (small n, suggestive). Failure cohort:
42 walls / 48 hard-site multi-attempt / 112 never-retried; AU/NZ hosts fail 75.7%
vs 24.5% elsewhere.

---

## 2. Plan (ranked by evidence-backed value ÷ effort)

### Track A — make the EXISTING knowledge flow (mostly wiring, no new concepts)

- **A1. Fix the LEARNED SKILL NOTES reader (3 deterministic bugs, tiny).**
  Truncate-don't-skip the 1500-char cap; extend injection to technique skills that
  actually hold learned sections (jsonld-extraction, navigation-patterns,
  anti-bot-handling, playwright-navigation), keyed by phase not just platform; fix
  platform aliasing. This is the only deterministic cross-site channel — today it has
  never delivered one byte despite 23 sections sitting in FM.
- **A2. Revive the skill writer (medium).** Repoint `nav_skill_review`'s precondition
  from the dead `navigation_findings.json` to what browser_traverse actually
  produces (navigation_analysis.json / traversal result). Keep SUCCESS-only + audit
  trail. This is the only agent holding `learn_skill`/`create_new_skill`.
- **A3. Give `learning_report.json` a consumer (medium).** Inject the target site's
  (and same-platform sites') `potential_learnings` into code_writer's context slot
  next to SITE MEMORY, with source citations. The lessons already exist and are
  good; nothing opens the file.
- **A4. Stop leaking the artifact archive (small-medium).** Fix `site_analysis.json`
  preservation at finalize (9/65 today) so `skip_site_analysis` can fire; preserve
  `discovered_urls_checkpoint.json` so wave-40's checkpoint reuse has something to
  read; make force_full opt-in per restart, not a silent default anywhere.

### Track B — wall-clock (from the mining audit)

- **B1. Kill the dead-invocation bleed (medium, biggest single win).** Lower
  `CODE_WRITER_LLM_TIMEOUT` 1500→~450s and `WRITER_MAX_TIMEOUT` 2700→~1500s, and
  make the writer resume from its persisted draft instead of restarting cold.
  30–45m lost per event today.
- **B2. Move skill_learning + dagster off the critical path (small, zero-risk).**
  Post-success Celery dispatch; 3–6m back on ~half of all jobs.
- **B3. Fix-loop coverage exit (needs care).** Hard-cap 2 writer cycles with a
  coverage-based exit — but wave-17 taught us the stagnation guard must not kill
  converging runs; pair with honest finalize + checkpoint rescue. Design review
  required before build.

### Track C — the real prize, wave-44 candidate

- **C1. Cross-site strategy memory.** Persist (platform-class → strategies_tried →
  outcome) at finalize; feed `_decide_strategy` as a prior; let a learned
  "Magento PWA needs browser rendering" actually change the ladder. Needs its own
  design round — it is the first mechanism that would make site B benefit from
  site A's failure.

### Sequencing proposal

Wave-43 = A1 + A2 + A3 + A4 + B2 (all wiring/preservation, low risk) —
with B1 in the same wave if review goes fast. B3 + C1 → wave-44.
Everything flag-gated per house rules, evidence-before-enablement, TDD.

---

## 3. Open decisions for the owner

1. Green-light wave-43 scope as above, or cut?
2. A3 injection breadth: target site only, or same-platform too (bigger prompt, more
   transfer)?
3. B1 timeout numbers: the 450s/1500s proposals come from observed burns; happy to
   dial conservatively (600s/1800s) if the writer needs room on hard sites.
4. B3: attempt in wave-43 (bigger win, needs the careful design) or defer with C1?
