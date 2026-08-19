# Plan — Skills persistence via File Master + log-scroll UX (v2, post-critique round 1)

> Status: **v2 — critique round 1 complete (2 agents). Round 2 pending.** v1's two false premises corrected: (1) nav_skill_review runs AFTER cleanup, not mid-pipeline (graph.py:4236-4239) — no same-job freshness requirement; (2) the worker is NOT the single FM writer (django writes FM at views.py:1558+; worker runs --concurrency=2) — read-modify-write needs a lock.
> Branch: `file-master-artifacts`. Both maps verified with file:line citations in the agent reports.

## Part 1 — Skills on the File Master

### Goal
Learned skills (the "## Learned:" knowledge) must (a) survive redeploys, (b) never require git commits, (c) stay readable by agents at build/call time, (d) keep working in local compose dev.

### Design (v1): FM is source of truth; image carries a seed; a local mirror caches

**A. New key namespace + seed**
- FM keys: `skills/{skill-name}/SKILL.md` (+ any sibling files the skills need — v1: SKILL.md only; the 15 pipeline skills have no other files verified as read).
- **Seed step** (one-way, idempotent): on worker boot (or first skills read), for each skill in the image's `.opencode/skills/` (minus the 2 UI skills): if `not artifacts.exists(f"skills/{name}/SKILL.md")` → `write_text` it. Seed source stays in git forever — git = the *baseline* skills, FM = baseline + learned. Re-seeding never overwrites (exists-check), so learned content is never clobbered.
- **The 2 UI-authoring skills (`impeccable`, `ui-ux-pro-max`) stay image-only** and are **excluded from `_get_skill_descriptions()`** — they're 91% of bytes and irrelevant to scraping agents. Free system-prompt win.

**B. Read path (single source of truth)**
- Consolidate the 3 duplicate `_resolve_skills_dir` implementations into **one** `src/skills_store.py` (pure-python, mirrors src/artifacts.py conventions):
  - `list_skills() -> list[str]` — FM `list_keys("skills/")` (cached 60s)
  - `read_skill(name) -> str` — FM read; FileNotFoundError → error string for the LLM (existing behavior)
  - `append_learned(name, section_text)` / `write_skill(name, text)` — FM read-modify-write (see D)
- `skill_tools.load_skill/list_skills` → call the store.
- `subagents._get_skill_descriptions()` → store-backed, with a **60s TTL cache** (it runs per node-build; FM latency ~ms on the private net but the scan is O(17 files)).
- **Fallback**: if `FILE_MASTER_URL` unset OR FM unreachable → fall back to the image's `.opencode/skills/` (read-only). This keeps the playground/tests/odd-envs working.

**C. Dockerfile change**
- Split the COPY: keep `.opencode/agents/` (11 system prompts — must stay image-resident), keep `.opencode/skills/` as the **seed** (still copied — it IS the seed + fallback). No Dockerfile deletion; the seed must ship.

**D. Write path (the hard part) — new skill-specific tools, not generic fs tools**
- Add **`learn_skill(skill_name, title, body)`** tool (skill-specific, append-only by construction):
  - Server-side enforcement of the format the prompts currently only *ask* for: builds the `## Learned: {title}\n**Source:** …\n**Applicability:** …` block itself.
  - Implementation: read FM → append at end → write FM → invalidate cache. Atomic enough (FM write is tmp+replace server-side; single worker writer — the documented invariant).
  - **Code-level append-only guard**: refuses to touch anything above the last existing `## Learned:` marker or the frontmatter; only appends.
- **`create_new_skill(name, description, body)`** tool for skill_learner's rare new-skill path (validates name, writes frontmatter + body).
- **Remove/limit generic write paths to skills**: filesystem_tools' sandbox (`_enforce_root`) currently allows `.opencode/skills/**`. Change: **deny-list `.opencode/skills`** in write_file/edit_file (reads still fine). This forces all skill writes through the typed tools — closing the "agent improvises a local write that goes nowhere" failure mode entirely.
- Update the 2 prompt files (`nav-skill-review.md`, `skill-learner.md`) + the 2 message builders to instruct the new tools (mechanical wording swap).

**E. Dev (compose) behavior**
- Same code path: FM is running in compose too (`file-master` service). Local runs write to FM, not the repo checkout → **the git tree stops receiving learned noise** (the user's explicit wish). The repo copy is only ever the seed.
- `.gitignore`: add `.opencode/skills/**/SKILL.md` learned-section drift? **No** — keep git clean by construction (nothing writes there anymore), no ignore needed.

**F. What lives where (summary)**
| Thing | Location |
|---|---|
| Agent system prompts (11) | image (git) — unchanged |
| Seed skills (15 baseline SKILL.md) | image (git) → seeded to FM once |
| Live skills (baseline + learned) | **FM `skills/` namespace** |
| UI-authoring skills (2) | image only, excluded from prompts |
| Learned writes | typed tools → FM only |

## Part 2 — Log-scroll stick-to-bottom

### Design (v1): single guard at the `addLogs` choke point + shared helper

**A. The stick-to-bottom flag (job_detail.html)**
```js
var logPinned = true;                      // start pinned
var LOG_PIN_TOLERANCE = 40;                // px — subpixel-safe
logContainer.addEventListener('scroll', function() {
    logPinned = (logContainer.scrollHeight - logContainer.scrollTop
                 - logContainer.clientHeight) <= LOG_PIN_TOLERANCE;
});
```
- In `addLogs`: replace line 472's unconditional scroll with `if (logPinned) logContainer.scrollTop = logContainer.scrollHeight;` — **instant scroll, not smooth** (2s batch cadence queues smooth animations).
- One guard covers all four callers (initial load passes with pinned=true default; SSE named+unnamed; Refresh) since all funnel through `addLogs`.
- **Re-pin affordances**: the existing jump-to-bottom button (line 184) also sets `logPinned = true`; `scrollToAgent` (line 431) sets it false (user navigated); page reload resets naturally.
- **"N new below" pill** (optional v1.5): when unpinned and new logs arrive, show a small pill on the existing button (`+12 ↓`); clears on re-pin. Cheap, high value.
- Same treatment for `addSyslog` (line 387) with its own flag.

**B. intake.html** — the replace-then-scroll variant (line ~1890): same flag pattern on `#agent-log`; reset `agentLogPinned = true` in `renderResults()`/`subscribeToJob`'s clear path (the agent's report flags this as a required reset point).

**C. Shared helper** — introduce the project's first shared static? v1 says **inline per template** (2 templates, ~10 lines each; introducing a static-files pipeline for this is scope creep). Revisit if a third consumer appears.

**D. DOM growth cap (do it now, it's 5 lines)** — apply the syslog panel's existing 500-line prune pattern to `#log-container` (job_detail): after append, `while (container children > 1000 entries) remove first`. 1000 not 500 (agent jobs are longer than syslog). intake already caps at 200 by replacement.

## Out of scope (flagged, not planned)
- **Singular `{"type":"log"}` SSE publisher mismatch** (services.py:114 vs plural handler): the Redis pub/sub path currently delivers NO live agent lines to job_detail (polling path does the work). Worth fixing separately — wrap as one-element batch in the relay — but independent of scroll.
- FM backup/restore story for the skills namespace (FM has volume persistence; backups = FM backups).
- Multi-worker write contention (single-worker invariant holds today).

## Open questions for critique
1. Seed timing: boot-time (worker startup adds ~1s) vs lazy-first-read? Boot-time in the worker's Django app startup vs celery worker_ready signal?
2. Is 60s TTL cache on descriptions right, or should cache invalidate on learn_skill only (write-through) with no TTL?
3. Deny-listing `.opencode/skills` in generic write_file — does anything else legitimately write there (dagster? cleanup scripts?) — verify zero other writers.
4. Does the skills fallback-to-image path create a trap where FM is down and agents silently read stale skills? (Acceptable? log a warning?)
5. `learn_skill` tool exposure: which agents get it — nav_skill_review + skill_learner only, per current write authorization?
6. Scroll: is container-bottom pinning acceptable given accordion grouping (bottom ≠ newest-entry), or is per-entry scrollIntoView worth the cost?

## Test plan (draft)
- Skills store unit tests: seed idempotence, exists-check no-clobber, append-only guard, fallback-to-image, TTL cache invalidation on write.
- Tool-level: learn_skill format enforcement; write_file deny-list on skills path.
- Scroll: manual QA matrix (pinned/unpinned × SSE/poll/refresh/reload) + a small JS-free smoke (template contains the guard).


---

# v2 AMENDMENTS (from critique round 1 — verified)

## Corrections to v1's premises
1. **No same-job freshness needed**: nav_skill_review runs post-cleanup (graph.py:4236); dagster_converter (next) has no skill tools. The TTL cache was solving a non-problem → **no TTL**; boot snapshot + write-through invalidation on create_new_skill only.
2. **Single-writer invariant is FALSE**: django writes FM (views.py:1558,1586,1636,1669,1823,2579); worker is prefork --concurrency=2 (compose:78) → two nav_skill_review agents can race read-modify-write. **Mandatory: `flock` lock file around the RMW in skills_store** (works across prefork children).
3. **The REAL perf bug**: _get_skill_descriptions reads FULL files (~2.9MB) per agent build to extract one frontmatter field (subagents.py:416). Fix = exclude the 2 UI skills from the scan (kills 2.7MB/build) + boot snapshot. (Prompt-section saving is ~945 chars ≈ 25% of the skills block, not 91% — that figure was disk bytes. Honest number.)
4. **skill-learner.md §5 contradicts its message builder** (:135-139 "apply right away with write_file/edit_file" vs subagents.py:3461 "Do NOT modify"). Today the message wins → nav_skill_review is the ONLY writer. **learn_skill goes to nav_skill_review ONLY**; skill-learner.md §5 MUST be rewritten in the same PR (else it fights the deny-list). Also fix its stale refs (search_content it doesn't have; mkdir via bash it doesn't have).
5. **run_bash bypasses the deny-list** (shell_tools.py:113-141) — v1's "closes the failure mode entirely" is overstated. Accept + document.

## New requirements
- **Seed**: at celery `worker_ready` signal, env-guarded — NOT AppConfig.ready() (runs in beat/flower which lack FILE_MASTER_URL → boot crash; also runs under pytest). NOT lazy (clobber race with a live learn). Version-stamp `skills/{name}/.seed.yaml` {sha256, git SHA, seeded_at}: reseed if FM copy is byte-equal to the old seed; skip+log if it has a learned tail. One-time decision: strip the 25 committed learned sections from git (tag first) or declare them baseline.
- **Fallback classification (sharpest edge)**: per-key, not per-connection. `except (RuntimeError, httpx.HTTPError)` → image fallback + WARNING log w/ skill name + classification. **404 must NOT fall back** (masks failed create_new_skill) — except for the 2 UI skills which stay image-only by design. `learn_skill`/`create_new_skill` NEVER fall back (fail loudly).
- **Playground gate**: learn_skill requires job_id != 0, OR append-only `skills/_audit.jsonl` (who/when/what). Playground has no execution_status gate (models.py:469-479 exposes nav_skill_review) — an un-gated learn pollutes FM permanently with no undo.
- **Observability**: add `_check_file_master` to health_api (views.py:2100-2118 lacks it — FM-down currently degrades invisibly to the frozen image copy).
- **`_summarize_tool_args` entries** for learn_skill/create_new_skill (graph.py:1081-1096) — else raw-json noise in agent logs.
- **prompts.load_skill is dead code → delete** (don't consolidate). prompts._skills_dir dies with it.
- **Dockerfile nuance**: under compose the "seed" is the repo checkout (bind mount); FM dev data lives in gitignored ./shared-data → dev redeploys keep skills; deleting the dir is the only dev data loss.

## Scroll amendments
- **Per-entry `scrollIntoView({block:'nearest'})` when pinned** — NOT container-bottom (accordions group by first appearance; a late system-line lands near the TOP while bottom shows the last agent's section). `nearest` also avoids yanking page ancestors. addLogs already holds the appended node.
- **Prune is pair-aware + seen-seq**: container children alternate header/body divs — remove header+body PAIRS or whole groups; keep a JS seen-seq Set so Refresh-path re-feeds don't re-append pruned entries out of order.
- scrollToAgent → logPinned=false; jump button → true (+ clear pill). Instant scroll (no smooth queueing under 2s batches).
- Out-of-scope confirmed: the singular `{"type":"log"}` publisher mismatch (services.py:114) — separate fix.

## Implementation order (reranked)
1. Read path only (skills_store read-only; point skill_tools + _get_skill_descriptions at it; delete prompts dead code; UI-skill scan exclusion; per-key fallback). Zero write risk, deployable alone.
2. Seed + version stamp at worker_ready + git-baseline decision.
3. Write path (learn_skill/create_new_skill, append-only guard, flock RMW, audit/gate, _summarize entries).
4. Deny-list .opencode/skills in write_file/edit_file (resolved-path check) — only after 3 proven.
5. Prompt alignment (nav-skill-review swap; skill-learner §5 rewrite + stale-ref fixes; stale graph-position claims).
6. health_api FM check.
7. Scroll (independent): flag + per-entry nearest + pair-aware prune.

## Round-2 open questions
1. flock vs FM-side POST /append — which for v1? (flock = no FM change; /append = cleaner but touches file_master service + its Dockerfile path)
2. Audit log vs job_id gate for playground — both? neither?
3. Git baseline: strip the 25 learned sections (tag `skills-baseline-pre-strip`) or keep as seed baseline?
4. skills/.version HEAD-check (1 request/build vs boot-snapshot-only across prefork siblings) — needed for create_new_skill cross-process visibility?
