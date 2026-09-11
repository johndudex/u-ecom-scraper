# wave-29 plan — close the learn→reuse loop (skills + code_writer memory)

Status: v2 — revised after adversarial critique (2026-09-11). DRAFT, not approved.
Forensics basis: 2 exploration agents + 1 adversarial critique agent, 2026-09-11.
Critique findings folded in: flock requirement, B6 descope, B4 cap fix, C1
selection rule, URL-ban sanitizer, baseline queries, phase re-ordering.

## Problem (verified)

1. **Learned skills are never reused.** Write path severed since the 2026-08-19 FM
   migration: `nav_skill_review` is instructed to call `learn_skill` but
   `_get_tools_sync` (webapp/agents/subagents.py:1150-1275) never wires the write
   tools (they exist only in production-dead async `get_tools_for_agent`,
   tools/__init__.py:194-198). `shared-data/skills/_audit.jsonl`: 8 entries, all
   2026-08-19 tests, zero production writes since. Read path is frontmatter-only
   (src/skills_store.py:122-126) so `## Learned:` sections can never surface
   automatically; the only reuse consumer ever written was deleted with the
   deterministic-navigation refactor (subagents.py:2459-2470). ~65.7K chars of
   real learned content sits unread (jsonld-extraction 15 sections/33.4K chars;
   navigation-patterns 5 sections/21.8K).
2. **code_writer has zero cross-job memory.** Re-drive a failed site → fresh probe,
   fresh analyzers, wiped workspace (setup_workspace PRESERVE_FILES=∅).
   `_archive_failure_evidence` (graph.py:1484-1531) writes test reports nobody
   reads; `skill_learner` AND `nav_skill_review` both run only on SUCCESS
   (graph.py:7412-7424, 3847) so failed sites never contribute; the only cross-job
   writer fact is `_prior_count_line` (completed jobs only,
   subagents.py:3250-3270). De-facto memory = hand-pasted wave-gotcha strings.
   Cost is real: re-drives re-derive known fixes (job 524 burned 2×1800s writer
   turns re-researching transport).

## Design principles

- **Patterns, not the package.** deepagents docs contribute: scoped
  filesystem-backed memory (namespace = site_slug), progressive disclosure,
  consolidation-after-runs, shared-read-only/scoped-writable. NO `deepagents`
  dependency, NO `create_deep_agent` migration.
- **Deterministic injection > voluntary tool calls.** The one mechanism that ever
  changed writer behavior is the wave-20 T3 shape-conditional retry-section gotcha
  (subagents.py:2882-2929) — its comment documents the exact pathology this plan
  targets ("regenerated drafts carrying the same defect … because its retry
  context never stated the rule it was breaking"). All reuse paths below are
  deterministic; tool access is bonus.
- **Measured beats remembered — with teeth.** Memory is advisory. On probe
  disagreement (method/platform changed vs the entry's stored probe), strategy-
  flavored lines are DROPPED, not shown (mechanical rule, not convention).
- **Fixes/lessons are memory; URL-shape facts are contamination-adjacent.** The H3
  no-cross-run-URL-seeds rule stands. Lessons are mechanically sanitized (below).

## Step 0 — baseline queries (before any code; they are also the eval meters)

- **S0.1** `ToolCallLog.filter(tool_name="load_skill")` counts per agent (prod) —
  validates/invalidates the "voluntary load_skill ≈ 0" premise behind C1's value;
  also the after-metric for reuse. (ToolCallLog rows exist today; only the skill
  NAME column is blank — A2 fixes that for the future.)
- **S0.2** Cross-job repeat-`_remediation_fingerprint` rate, computed from the FM
  test reports already archived (`scrapers/*/analysis/test_report-*.json`) —
  direct measure of "re-drives re-derive the same fixes" and the primary
  before/after success metric.
- Secondary metrics (computable from existing data): re-drive success rate for
  sites with ≥1 prior FAIL; writer wall-clock per turn on re-drives; fix cycles
  to first PASS.

## Phase A — plumbing repairs (ship with Step 0)

- **A1** Wire write tools into the sync path: `_get_tools_sync` gains
  `needs_skill_write` so `nav_skill_review` actually receives
  `learn_skill`/`create_new_skill`. Test: assembled toolset contains them.
- **A2** Fix `load_skill` telemetry kwarg (`args.get('name')` → `skill_name`,
  graph.py:1709-1710). Framing: instrumentation/eval meter, not behavior change.
- **A3** Prompt/tool consistency: the `_append_skill_descriptions` skills blurb
  (subagents.py:737-744) attaches only when the toolset has `load_skill`
  (fixes code_tester/cleanup/dagster_converter false advertisement); reconcile
  site-analyzer.md:190 with the deliberate prohibition at subagents.py:1777.
- Deferred: A4 (admin "last used" UI — new build, no pattern to reuse).

## Phase B — per-site writer memory (store + retry-section surfacing)

**Storage:** `scrapers/{slug}/analysis/writer_memory.json` (FM-durable, no DB
migration):
```
{ "site": slug, "updated": ts,
  "probe_fingerprint": {method, platform},          # of the entry's job
  "entries": [ {job_id, ts, outcome, strategy, item_count,
                failure_class, remediation_fp, note(≤160 chars)} ],
  "lessons_digest": "≤500 chars, newest-first" }
```
Ring buffer: max 12 entries + per-failure-class cap 3 (one repeating class can't
flood the slots); same-fingerprint-different-outcome marks older `superseded_by`;
probe disagreement drops strategy-flavored lines from ALL reads.

**Concurrency (critique-corrected):** the running-sibling guard filters
`url=job.url`, NOT slug (tasks.py:371-379) — two URLs of one site CAN run
concurrently and `src/artifacts.py` write is a bare PUT. B-writes therefore reuse
the `src/skills_store.py:202-208` pattern verbatim: per-slug **flock** around the
read-modify-write, accepting the same residual django-vs-celery window
skills_store accepted. No new lock infrastructure.

**Write path (deterministic, best-effort, never fails a job):**
- **B1 failure capture** in `_archive_failure_evidence` (graph.py:1484-1531):
  upsert {strategy, failure_class, remediation_fp, outcome=failure, note} —
  note derived ONLY from whitelisted deterministic issue classes
  (crash/timeout/http_status/missing_fields), never free LLM text.
- **B2 success capture (minimal)** at the skill_learner finalize junction
  (graph.py:7406-7496): upsert {winning strategy, item_count, outcome=success,
  one-line note}. LLM phrasing deferred (E1).
- **B3 supersede** same-fingerprint-different-outcome (as above).
- **Sanitizer (all writes):** truncate any note at first `http` occurrence; strip
  code fences; URL/selector strings banned — keeps the H3 rule true mechanically
  and bounds the scraped-content prompt-injection channel (single-tenant risk:
  LOW, but the URL-leak variant was near-certain without this).

**Read path (two surfaces, both deterministic, caps fixed per critique):**
- **B4 first-attempt block (slim):** beside `_prior_count_line`
  (subagents.py:4525-4562): `SITE MEMORY — last job N: strategy X → outcome Y
  (n items); open lessons: <digest>` — **hard cap 800 chars** (critique: the old
  900+3×240 spec could not fit its own 1,200 cap). Value on first attempts is the
  one-line outcome/strategy, nothing more.
- **B5 fingerprint-match on fix cycles (the load-bearing surface):** when the
  current failure's `_remediation_fingerprint` (route_after_testing.py:1319-1355)
  matches a prior entry, render "This failure signature occurred in job N
  (strategy W); tried: …; outcome: …" in the wave-20 retry-section slot
  (subagents.py:2882-2929) — the one injection point with proven behavioral
  effect. Match tiers: exact (target+field+issue-set+exception), then degraded
  (target+issue-set, no field) so near-misses still surface. Cap 2 matches.
  Stale-guard: if the stored entry's probe method disagrees with the fresh probe,
  strategy-flavored words are dropped from the rendered line.
- **B6 is DESCOPED** (critique: net-negative): no `prior_attempts` mechanism into
  `scraper_analysis.json` (it is the wave-28 authority file; cross-run duplication
  of in-run `strategies_tried` with worse freshness; first entries would come from
  known-bad pre-wave-28 jobs). The strategy/outcome fact lives as one line in B4.

## Phase C — deterministic surfacing of existing learned skills

- **C1** When scraper_analysis detects platform P, render that skill's **2 newest**
  `## Learned:` sections — never split mid-section — hard cap 1,500 chars, into
  `_platform_distillation` (subagents.py:3172-3232, today zero skill content).
  Unit-test the cap against the real 33KB jsonld-extraction file. This is
  read-only, race-free, staleness-resistant (nav_skill_review's curation gate:
  "ZERO learnings is better than wrong ones"), and directly fixes headline
  problem #1. Runs AFTER Phase B per critique ordering (B is the efficacy loop;
  C is reach).
- **C2** `_get_skill_descriptions` appends "(+N learned notes)" — read from the
  3600s `descriptions_snapshot` cache, no extra FM reads. Only if free.

## Phase E — deferred (explicitly NOT in v1)

- **E1** Beat-driven LLM consolidation ("sleep-time compute"): compact entries into
  polished lessons; promote site-agnostic fixes (same signature fixed on ≥2
  distinct sites) into a global writer-patterns skill via
  `append_learned` (human-reviewable through /learnt-skills).
- **E2** Migrating hardcoded wave-gotchas into skills (churn on working text).
- **E3** Per-site memory editing UI (today only skills have replace/delete
  section helpers; writer_memory.json is job-attributed JSON, hand-editable).

## Must-not-break (critique-verified against code)

Seed truncation exemption (subagents.py:166 — every added char rides all turns;
caps above are load-bearing, 25KB ballooning precedent); draft freeze sha256; F821
gate; `_safe_splice_write`; step-budget accounting; template selection single
authority; CLI contract + anti-drift test; tool guards; wave-28 fingerprint
strategy semantics (B6 descoped specifically to protect the authority file); H3
no-cross-run-URL-seeds (sanitizer makes it mechanical); FM durability under
redeploys (artifacts.py purpose).

## Test plan

S0 baseline recorded first. Wire test (A1 toolset), telemetry test (A2),
prompt/tool consistency (A3), capture tests (failure → entry; success → entry;
ring buffer; per-class cap; supersede; sanitizer URL-ban), flock concurrency test
(parallel upserts, no lost update), injection tests (B4 ≤800; B5 exact + degraded
match; stale-guard probe-disagreement drop), C1 selection test (2-newest,
mid-section integrity, ≤1,500 against real jsonld file), wave-28 suite stays
green, full suite + ruff, one local e2e re-drive of a previously failed site:
verify writer seed carries job-N fingerprint line on the fix cycle.

## Sequencing (critique-decisive)

0. Baseline queries → 1. A1+A2+A3+B(store+B1+B2+B3+B4+B5) → 2. C1(±C2) →
3. (later, only if metrics justify) E1.
