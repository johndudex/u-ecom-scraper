# Partner API Plans — Round-2 Verifier (resolution verification)

**Date:** 2026-08-23 · **Lens:** the round-1 critique attacked the PLANS. This
round attacks the FIXES — every resolution in `api-plans-fold.md` is checked
against the actual code at the cited site, for implementability *exactly as
described*, hidden coupling, and second-order breakage.

**Inputs:** `api-plans-fold.md` (fold), `api-sync-implementation-plan.md` (A),
`api-async-implementation-plan.md` (B), `api-plans-critique.md` (round 1).
All citations re-verified on `file-master-artifacts` @ 1b22914.

---

## VERDICT SUMMARY

| Fix | Fold's claim | Verdict |
|-----|--------------|---------|
| **B1** hook at `_invoke_code_tester` after `_preserve_test_report` | THE hook | **FIXABLE-WITH-NOTES** — the site is right, but A's `_persist_partner_sample` as specified is structurally wrong in two ways (local-vs-FM path confusion + retry-cycle FAILED reports), and the `output_key` it reads is absent from `state` |
| **B2** JobCallback model, raw secret | one model, one policy | **SOUND** (with one spec-wording residue, see §B2) |
| **B3** PATCH/GET callback | spec amended | **SOUND** |
| **B4** delivery-time SSRF | B's hard requirement | **SOUND** (mechanism exists, nothing blocks it) |
| **B5** `events` queue + worker | compose entry + `--queues=events` | **FIXABLE-WITH-NOTES** — the compose/Railway half is unstated and has a real invisible-buildup gotcha the fold does not name |
| **M1** self-scheduled countdown ≥1m + 30s sweep | spec amended | **FIXABLE-WITH-NOTES** — the spec text landed, but the countdown is stated ONLY in the spec; B's plan still contains the exact rejection of it |
| **M2** `completed_at` + `(status, completed_at)` index | B's migration | **SOUND** (column exists, is set on every terminal path — one site sets it without `_publish_job_status`, which the reconciler is *for*) |
| **M3** on_commit + CAS-claim via `locked_until` | delivery state in the row | **FIXABLE-WITH-NOTES** — the state machine in B's plan is INSUFFICIENT: the `dispatch_pending_callbacks` beat task itself needs a lease/CAS or the same race survives in a new shape; no stale-lease sweeper is specified beyond the lease TTL |
| **M4** `created_via` gate | default `intake`, emit() no-ops | **SOUND** — with the explicit note that pre-migration jobs never emit (correct), and that 2 of 6 create sites are load-bearing for the invariant |
| **M5** prune exemption `and job.created_via != "api"` | one-line condition | **BROKEN as written** — the prune is SITE-keyed and iterates FM *keys*; there is no job in scope and no filename→job mapping. The one-line fix cannot be written |
| **M7** recursion approval routes through `skip_approvals` | mirror human_approval:116-124 | **BROKEN as a mirror; FIXABLE as a redesign** — `create_recursion_approval` is not an interrupt node; the human_approval pattern (return a state dict) is inapplicable, and the recursion error is genuinely fatal on the path where it fires |
| **M8** PHASE_MAP → `"browser_traverse"` + dedupe | cheapest, check UI | **SOUND** — UI label maps are key→label (they survive the change), but there is a *pre-existing* legacy-row migration the fold did not scope, and a **dead tasks.py PHASE_MAP** the fold's "check UI templates" instruction would miss |
| **M9** ONE global stream budget | Redis-counted, cap=2, 1 reserved | **SOUND** (mechanism is B's existing Redis-set counter widened — no coupling found) |
| **M10** finalize-time page index | worker writes `{offset,length}` once | **FIXABLE-WITH-NOTES** — FM supports the write trivially (plain PUT), but the write site must run in the *celery worker* after a point where the worker no longer has the file locally (it was just deleted), and **ownership is genuinely ambiguous** between A and B |
| **M11** Redis fixed-window 10 r/s, 429+Retry-After | spec gains RateLimits + 429 | **BROKEN as "done"** — the spec contains **zero** occurrences of `429`, `RateLimits`, or any rate-limit section. The fold's own decision 4 is not reflected in the artifact it claims to have amended |
| **M12** atomic create + `emit(job.created)` | written into A's plan | **BROKEN as "written into A's plan"** — A's plan §4.1 still specifies a plain `ScrapeJob.objects.create(...)` + dispatch with no `atomic()` and no `emit`; A's §8 sequencing step 5 is unchanged. The fold's claim is false against A's file |
| **M13** SSE `?token=` | resolved in spec (done) | **SOUND** (async_api.yaml:150-152 documents `events?token=`; ws-token machinery is already single-use/300s) |
| **M14** listJobs 422 | resolved in spec (done) | **SOUND** (verified at the listJobs responses) |
| **M15** test extensions | assigned | **SOUND as assignment** (unverifiable until written) |
| **m1–m8, n1–n8** | folded | mostly SOUND; two exceptions: **m4's REST-vs-event divergence is REAL and is now the live disagreement** (see NEW-1); **B's §2.3/§2.2 sample row still carries field_confirmation text** (see NEW-2) |

**Re-verified round-1 "checked out" claims:**

| Claim | Verdict |
|-------|---------|
| A's D4 (`sample_ready` = testing `completed_at` IS NOT NULL) vs the code_tester hook | **THEY DISAGREE, exactly in the m4 case** — see NEW-1 |
| The 4-state projection completeness | **SOUND** — every mapped status is producible, every producible status is mapped. No dead statuses, no unmapped statuses, no paused/timeout status exists. Detail below. |

---

## B1 — the sample hook (FIXABLE-WITH-NOTES)

**The site is correct.** `_invoke_code_tester` at graph.py:3379; `_preserve_test_report(slug)`
at graph.py:3425 (inside `if report:`); `_notify_phase(job_id, "code_tester", "done")`
at graph.py:3413 fires ~12 lines earlier. The ordering A describes (step flips to
done → sample file appears) holds.

`_preserve_test_report` (graph.py:586-603) copies `workspace/{slug}/test_report.json`
verbatim to `scrapers/{slug}/analysis/test_report.json` — **per-site, latest-job-wins**,
exactly as A's plan warns. It persists nothing job-attributed. So the per-job
sample must be a *new* write, which is what A specifies. Fine.

### Note 1 — the hook fires with FAILED and ZERO-item reports too. A's plan has no pass-gate.

`_invoke_code_tester` runs the code_tester LLM, then loads whatever report the
LLM wrote (graph.py:3415). The `if report:` branch executes `_preserve_test_report`
**unconditionally on the report's existence**, not on its assessment. The
pass/fail decision lives in a *different node* — `route_after_testing`
(route_after_testing.py:433-531: `assessment == "PASS" and confidence >= 0.85
and not high_severity and not _contract_bad`, plus the discovery-coverage and
ground-truth overrides at :517-549).

A's `_persist_partner_sample` sketch (A §6 step 1) picks the output file with
the **most items** under `output_key`. That is the right *file-selection*
heuristic (ported from route_after_testing.py:226-248, which A cites correctly).
But it means:

- **Retry cycle N fails with 1 item → hook fires → sample file written with 1 item.**
  Step(testing) has `completed_at` set (graph.py:879) → REST derives
  `sample_ready` → `GET /sample` returns **the failed attempt's 1 record**.
- The retry re-fires and overwrites with the better set (file existence
  monotone, contents mutate — A's own m3 note). So the partner can observe a
  1-record sample that later becomes a 5-record sample.

This is *tolerable* (the spec's new MUTABILITY paragraph at sync_api.yaml:678
covers it) **but A's plan does not say so** — A's §6 says "fires on every
testing completion (retry cycles included)" and frames that as a *benefit*
("so `sample_ready` and a fetchable sample appear together"). It is also a
benefit to the failure case: the spec's info block promises a best-effort
sample in `failed`. So the verdict is FIXABLE-WITH-NOTES, not BROKEN — but the
note is load-bearing: **if a partner builds "sample_ready ⇒ the extractor
passed", the retry-cycle-FAIL path violates it**, and neither the spec's state
table (sync_api.yaml:495-501, `running` + sample resolvable → `sample_ready`)
nor A's plan says the sample may come from a failed test cycle.

**Required amendment (small):** either (a) gate `_persist_partner_sample` on
`report.get("overall_assessment") == "PASS"`-ish (but then the `failed`-job
sample promise weakens), or (b) keep it ungated and add one sentence to the
spec's `sample_ready` row + A's §6: *the sample may be written during a test
cycle that later fails; `sample_ready` means "records exist", not "testing
passed".* (b) matches the code as specified and is the cheap fix.

### Note 2 — `output_key` is not in `state` in the shape A's call site reads.

A's call site (A §6) is:

```python
_persist_partner_sample(slug, job_id, output_key=state.get("content_type_config", {}).get("output_key", "products"))
```

`content_type_config` IS in state (tasks.py:570 seeds it from
`ct.output_schema`, src/content_types.py:49-61, which includes `output_key`).
So the read is correct — **but only when the key exists**. `_build_initial_state`
(tasks.py:476-498) sets `content_type_config = {}` when `get_config_for_page_type`
returns None, and route_after_testing's own reader defensively does
`ct_config.get("output_key", "products") if ct_config else "products"`
(route_after_testing.py:215, 233) — i.e. the codebase already knows this key
can be missing. A's sketch has the `.get("output_key", "products")` default
inline, so it is safe. Verified sound; listed only because Note 3 depends on it.

### Note 3 — WHERE the hook reads from: local workspace vs File Master. A's plan conflates two paths.

A's §6 step 1 says: *"Scan `workspace/{slug}/output_*.json`"* — the local
celery-worker disk. This is correct and matches the codebase's own ground-truth
reader (route_after_testing.py:224-248 reads local `workspace/{slug}/`, and
`run_scraper` deliberately persists browser_service output back to the local
workspace for exactly this reason — shell_tools.py:330-344:
*"browser_service returns output CONTENT (no shared FS). Persist it to the
local workspace so downstream … works"*).

But A's §6 step 4 then writes via
`artifacts.write_json(artifacts.scrapers_key(slug, "samples", f"sample-{job_id}.json"), …)`
— an FM PUT (src/artifacts.py:42-55). That is also correct (FM is the read side
for Django).

So the hook is: **read local, write FM.** Implementable exactly as described,
no hidden coupling — `artifacts.write` is a plain `httpx.put` with a 120s
timeout (artifacts.py:24, 44) and `_preserve_test_report` already does the
identical local→FM copy at the same site (graph.py:599-600). **One real hazard:
FM availability.** `_persist_partner_sample` is specified as best-effort/never
raises (A §6 docstring), so an FM blip at testing time silently drops the
per-job sample; the state fallback (test_report.json exists + testing done)
then reports `sample_ready` while `/sample` 409s — because the *fallback signal*
is `_preserve_test_report`'s per-site key, which uses the **same FM**. i.e.
**both the primary and the fallback signal fail together** on an FM outage at
testing time. A's §4.2 `sample_ready()` returns true if EITHER holds, so an FM
outage makes both false and the state degrades to `inprogress` — consistent,
not lying. Acceptable; worth one line in A's §9 risks.

### Note 4 — B's D4 revision is correct but B's §2.3 was NOT rewritten.

See NEW-2. The fold claims "B's plan file updated 2026-08-23" — the D4 row and
D9/D12 rows WERE rewritten (verified), but **§2.3 ("Prerequisite: sample
persistence") still instructs the implementer to write the sample at
field_confirmation and still cites `field_confirmation.py:280-313`** — the exact
dead code the critique falsified, with no supersession marker. Any implementer
working top-to-bottom through B's plan (§2.3 is titled "Prerequisite") hits the
old instruction before reaching the revised hook table.

---

## B2 / B3 — JobCallback + PATCH/GET (SOUND)

- `JobCallback` OneToOne with the fold's field list is a plain model; nothing
  in `webapp/scraper/models.py` conflicts (no existing `callback` related_name,
  no existing callback columns to drop — verified: `grep callback` on models.py
  returns nothing).
- Raw secret in a never-serialized CharField: implementable; A's test #14
  (scan all serialized payloads) is the right lock.
- **Residue:** `sync_api.yaml:1594` still says *"stored hashed (SHA-256) at
  rest and NEVER returned"* for `callback_secret` in `CreateJobRequest`, while
  `async_api.yaml:452-455` says the opposite (*"the secret CANNOT be stored
  hashed — it is stored … as a raw value"*). The fold's B2 bullet claims
  *"Async spec's 'stored hashed at rest' erratum corrected to the raw-policy
  wording (done, tests pass)"* — it was corrected in the **async** spec only.
  The sync spec's `callback_secret.description` retains the hashed-at-rest
  claim that decision 2 rejects. The existing spec test
  (`tests/test_api_docs_views.py:221-239`) checks only for the *presence* of
  `callback_secret`, not its storage wording, so this passes CI while the two
  specs still disagree on the same field — the exact class of defect B2 was
  filed to close.
- PATCH/GET `callback` paths, `CallbackStatus`, `CallbackUpdate`, 409
  `callback_already_active`, 422 `invalid_callback_url` all verified present at
  sync_api.yaml:1065-1155. SOUND.

---

## B5 — the `events` queue (FIXABLE-WITH-NOTES)

What EXACTLY must change (none of it is in either plan; the fold gives one line):

1. **Compose**: a new service block in docker-compose.yml mirroring
   `celery-worker` (docker-compose.yml:74-119) with
   `command: celery -A config worker -l INFO --concurrency=2 -Ofair --queues=events`
   (the `-Q`/`--queues` flag), plus the same env/volumes/depends_on/mem_limit.
   `profiles: ["full"]` to match.
2. **Routing**: either `CELERY_TASK_ROUTES` in settings.py (currently **zero**
   queue routing anywhere in the repo — verified by grep) or
   `@shared_task(queue="events")` on `dispatch_pending_callbacks` +
   `deliver_callback`. Routes is the safer choice (keeps the queue name in one
   place and lets the beat task be moved without touching decorators).
3. **Beat**: `CELERY_BEAT_SCHEDULE` gains `dispatch-pending-callbacks`
   (settings.py:150-163 pattern). **Critical:** because
   `CELERY_BEAT_SCHEDULER = django_celery_beat.schedulers:DatabaseScheduler`
   (settings.py:124), the `CELERY_BEAT_SCHEDULE` dict is loaded into the
   `django_celery_beat` **DB tables on first read** and thereafter the DB row
   is authoritative — schedule changes after the first boot may require
   deleting/re-syncing the PeriodicTask row, a known django-celery-beat
   behavior. Not a blocker; it has bitten this repo's class of deployment
   before (beat crash-looping until tables exist — docs/railway-migration.md:274).
4. **Railway**: a 9th service (the fold says "compose entry" only — Railway is
   the live deployment per memory/`docs/railway-migration.md`; 8 services
   green today). Same start command change, same shared variables
   (`PYTHONPATH` is load-bearing — railway-migration.md:204-207).

**The gotcha the fold does not name (invisible buildup):** with
`CELERY_TASK_ROUTES` set and a worker listenting only on `events`, the *default*
worker keeps consuming `celery` (default). That direction is safe. The dangerous
direction is the **deploy-order window**: if the `events` worker is not yet up
(or is crash-looping — note it needs `migrate` to have created the EventOutbox
table before its imports succeed, and compose's `celery-worker` does NOT run
migrate; only django and celery-beat do, docker-compose.yml:38-41 vs 124-126),
then `dispatch_pending_callbacks` fires on **beat**, enqueues to `events`, and
the messages accumulate in Redis **invisibly** — no worker drains them, no
error, no metric. Under redis's default no-max-memory policy this is bounded
only by disk; with `allkeys-lru` it would silently evict. Two mitigations,
either suffices: (a) make the events worker's command
`sh -c "python manage.py migrate --noinput && celery … --queues=events"`
(mirroring celery-beat's pattern) so the table always exists, and (b) the
fold's own m10 `outbox_pending_total` metric with an alert — which turns
invisible buildup into a visible one. Neither is assigned to a plan step.

Also: `dispatch_pending_callbacks` is specified to run **on beat** (B §4 task
1). Beat is `mem_limit: 128m` (docker-compose.yml:151) and currently runs only
trivial DB queries. The reconciler (B §2.4) — a scan over terminal jobs — plus
`select_for_update` row locking now live in that 128 MB container. Probably
fine at current scale; unexamined in both plans.

---

## M1 — self-scheduled countdown (FIXABLE-WITH-NOTES)

The spec amendment landed correctly (async_api.yaml:476-484):
*"Backoff values are MINIMUM delays … legs >= 1 m are additionally
self-scheduled with an exact countdown and are honored exactly"*, `attempts: 6`.

**But B's plan §4 task 2 still says the opposite**, verbatim:
*"do **not** use `autoretry_for`/`retry_backoff` despite the spec naming them;
the beat sweeper is the retry driver"*. There is no supersession marker on that
paragraph (only D4/D9/D12 carry "SUPERSEDED" tags). The fold's M1 bullet
assigns the fix to B but B's file was not edited for it. An implementer
following B §4 gets the 30s-quantized schedule the critique condemned, and the
spec they're conforming to says they're wrong. This is the same
document-inconsistency class as M12 — the fold's assignment was not executed in
the assigned file.

Mechanically, `apply_async(countdown=…)` for legs ≥ 1m + a 30s sweep for the
10s leg is implementable and standard. One real coupling: the countdown task
must target the `events` queue too (B5), or the ≥1m legs land on the scrape
workers — the exact starvation B5 exists to prevent. Neither plan connects
these two.

---

## M2 — reconciler on `completed_at` (SOUND)

`ScrapeJob` has `completed_at` (models.py:180). Verified set on every terminal
path: finalizer ladder (tasks.py:727, 927-934 → saved at 1065-1078),
`run_scrape_task` exception path (tasks.py:155, 163), resume-failure path
(tasks.py:421), stuck-job watchdog FAIL (tasks.py:1400), stuck-approved
watchdog FAIL (tasks.py:1196 region), `_auto_approve_stale_jobs`→FAILED path
(tasks.py:1395 region), captcha/akamai in-graph (graph.py:1238-1242).
`(status, completed_at)` index in B's migration is a plain
`models.Index(fields=[...])`. SOUND.

One nuance, not a defect: tasks.py:1196 and :1395 set `completed_at` and save
**without** calling `_publish_job_status` — that is precisely the unwrapped-site
class the reconciler exists to cover, so the design is self-consistent. The
reconciler must therefore run *after* those writes commit; since it runs on the
beat/dispatch task, not in the worker, it will. No issue.

---

## M3 — CAS-claim dispatch discipline (FIXABLE-WITH-NOTES)

**The row-state machine in B's plan is insufficient as written.** B §3 lists
`status ∈ [pending, no_callback, delivered, exhausted, skipped]` and
`locked_until` as "dispatch lease". The M3 fold resolution says *"delivery
state lives in the row (CAS-claim via `locked_until`), not the message"*. Read
together with B's dispatch task ("Sets `locked_until = now() + 5min`, enqueues
`deliver_callback.s(event_id)`"):

1. **The dispatch task itself is the remaining race.** `deliver_callback` must
   CAS-claim before POSTing — `UPDATE … SET locked_until = now()+lease WHERE
   event_id = ? AND (locked_until IS NULL OR locked_until < now())` and only
   POST if `rowcount == 1`. B's plan never states this claim inside
   `deliver_callback`; it only describes the *dispatcher* setting the lease.
   Without the claim in the deliver task, a stale re-enqueued message (lease
   expired, sweeper re-enqueued, original task finally runs) double-POSTs.
   The fold's wording ("CAS-claim via locked_until") implies it, but the
   implementing plan does not contain it. FIXABLE: one paragraph in B §4 task 2.
2. **No sweeper for stale leases is specified beyond the lease expiring.**
   The dispatcher's SELECT already includes
   `(locked_until IS NULL OR locked_until < now())` (B §4 task 1), which IS
   the stale-lease recovery — a row whose worker died mid-lease becomes
   eligible again after 5 min. So recovery exists implicitly. **The gap is
   `attempts`:** a row that repeatedly dies mid-lease has its lease expire and
   is retried forever, never incrementing `attempts`, never reaching
   `exhausted`, never disabling the callback. The disable-on-exhaustion policy
   (async_api.yaml:484 `on_exhaustion: disable_callback`) is keyed on attempt
   count. **Crash-looping deliveries therefore never disable** — an unbounded
   retry loop on a poison event, invisible to the policy. Required: increment
   `attempts` when the lease is *taken* (or on sweeper-requeue), not only on
   POST outcome. Not in B's plan.
3. **Missing state: `leased`.** With only `pending/delivered/…` + a nullable
   `locked_until`, a leased row still reads `status='pending'`. The dispatcher's
   predicate handles it, but any *other* reader (the SSE "most recent state
   envelope" query, the m10 admin view, `pending_count` on CallbackStatus —
   sync_api.yaml:1793 "Outbox rows waiting") must repeat the lease predicate or
   it will miscount. Cheapest fix: a `leased` status value. Cosmetic-to-minor.

`transaction.on_commit` for the enqueue: implementable and correct
(Django runs the callback immediately outside `atomic()`, which B §2.1 notes).
SOUND on that half.

---

## M4 — `created_via` gate (SOUND)

All six `ScrapeJob.objects.create` sites, verified:

| Site | Sets `user`? | Default `created_via="intake"` correct? |
|------|--------------|------------------------------------------|
| tasks.py:1292 (`_do_schedule_next_site`) | `user=None` (system) | yes |
| views.py:267 (home) | yes | yes |
| views.py:654 (re-scrape) | yes | yes |
| views.py:1548 (playground) | yes | yes |
| views.py:2572 (intake) | yes | yes |
| management/commands/scrape.py:58 (CLI) | **no** (`user` unset → null) | yes |

- **Does the default break anything?** No. A `CharField(max_length=10, choices=…,
  default="intake")` is additive; no reader exists today; Django migrations add
  the column with the default backfilled for existing rows. No query, form, or
  template reads a field that doesn't exist yet.
- **Do existing (pre-migration) jobs never emit?** Correct and right. Postgres
  `ADD COLUMN … DEFAULT` populates historical rows with `intake`, so every
  pre-existing job is correctly classified as internal. There is no
  partner-created job before the API exists, so nothing is lost. This is the
  desired behavior, not a gap.
- **One invariant worth a test (not in A's list):** the CLI path
  (management/commands/scrape.py:58) creates jobs with `user=None` **and**
  accepts `--auto-queue`. If a future operator runs the CLI for a partner, the
  `created_via` gate is what keeps it out of the outbox — good. But conversely,
  A's create endpoint must set `created_via="api"` **on the same row-creation
  call**, not in a follow-up save, or a crash between the two leaves an
  `intake`-classified partner job that silently never emits. A's §2.2 lists the
  column; A's §4.1 mapping table does not mention setting it. Small, but this
  is exactly the M12 seam class.

---

## M5 — prune exemption (BROKEN as written)

The fold's resolution is one line: *"Prune loop condition gains
`and job.created_via != "api"`"*. Reading the actual loop (tasks.py:862-881):

```python
if site_slug:
    _outs = [k for k in artifacts.list_keys(f"scrapers/{site_slug}/")
             if k.split("/")[-1].startswith("output_") and k.endswith(".json")]
    if len(_outs) > 5:
        for _old in sorted(_outs)[:-5]:
            artifacts.delete(_old)
```

Three structural problems with the one-line fix:

1. **The prune is SITE-keyed, not job-keyed.** It iterates File-Master *keys*
   under `scrapers/{site_slug}/`. There is no `job` in the loop body — the
   enclosing `_finalize_job(job)` job is the *finalizing* job, which is the
   one whose output was just written, not the jobs that own the five older
   files being considered for deletion. `job.created_via` tells you about the
   wrong job.
2. **The filename→job mapping is NOT recoverable from the filename.** Output
   files are `output_%Y-%m-%d_%H%M%S.json` — a second-resolution timestamp
   with no job id (templates/*.py:57-63, views.py:394; confirmed against
   `scrapers/rmwilliams-com-au/` which holds 13 `output_*.json` with no job
   attribution in the names). Two jobs on the same site within the same second
   collide. The only job→file link is `ScrapeJob.output_file` (set at
   tasks.py:858), so the mapping IS recoverable — but only by a
   **reverse lookup over `ScrapeJob.output_file`**, i.e. one query per
   candidate key (`ScrapeJob.objects.filter(output_file=_old).values_list("created_via")`).
   That is 5-or-fewer cheap indexed-ish queries per finalize (the column is
   not indexed today; A's migration doesn't add one) — implementable, but it
   is ~10 lines and a new query, not "gains `and job.created_via != 'api'`".
3. **Unowned files.** Any `output_*.json` with no matching `ScrapeJob.output_file`
   row (workspace rescued before `job.output_file` was repointed at tasks.py:818,
   legacy files, files from jobs deleted by CASCADE) maps to no job and therefore
   has no `created_via`. The exemption must decide: treat unowned as prunable
   (safe for retention, may delete a partner's file if the row was lost) or
   unprunable (unbounded growth). Neither plan decides.

**Correct minimal shape** (for the fold, not for me to implement): in the prune
loop, for each `_old` beyond the newest 5, look up
`ScrapeJob.objects.filter(output_file=_old).first()`; skip deletion when it
exists and `created_via == "api"`; delete otherwise (or per the unowned-file
decision). Note the retention semantics also change: "keep newest 5" becomes
"keep newest 5 *internal* + all partner", so the partner count is unbounded on
a shared site — which is the accepted decision 3, but the FM volume-growth
consequence (fold: "accept FM volume growth") lands on the *site* prefix, and
the M10 page index (below) will then be computed for files that may be months
old.

**Verdict: BROKEN as written / FIXABLE-WITH-NOTES once rewritten.** The
mechanism exists; the specified edit does not.

---

## M7 — recursion approval + `skip_approvals` (BROKEN as a mirror)

The fold: *"create_recursion_approval routes through skip_approvals
(auto-approve unattended, mirroring human_approval.py:116-124)"*. Reading both
sites:

**human_approval.py:116-124** works because `human_approval` is a *graph node*
that returns a state dict. Under `skip_approvals` it skips the `interrupt()`
and returns `{"human_response": {"decision": "approve", …}}` — the graph
continues executing in the same `graph.invoke()` call. No resume is ever
needed.

**`create_recursion_approval` (services.py:413-455)** is not a node and there
is no interrupt to skip. It is called from an `except` handler after
`graph.invoke()` has **already raised `GraphRecursionError`** (services.py:258,
tasks.py:409-416). The graph is dead; there is nothing to "auto-approve"
*into*. So the mirror is category-confused. What auto-approving would actually
require:

- Create the Approval row APPROVED (or skip creating it), then **re-dispatch**
  `resume_scrape_task` / re-invoke the graph — i.e. re-run `graph.invoke()`
  from the checkpoint. That is `_auto_approve_stale_jobs`' shape
  (tasks.py:1450-1459), not human_approval's.
- And `resume_scrape_task` (tasks.py:293-440) resumes **interrupts**
  (`Command(resume=resume_value)`); a recursion error left no interrupt, so a
  resume dispatch would call `graph.invoke(Command(resume=…))` on a checkpoint
  whose next step is the same node that just exceeded its budget — **it
  re-raises immediately.** This is the second half of the question asked, and
  the answer is: *no, auto-approving does not make sense for this error class,
  because the recursion budget is per-`invoke()` (config `recursion_limit`,
  graph.py AGENT_RECURSION_MAP:606-618 / services.py:243-246) and re-invoking
  with the same limit deterministically fails again.* The docstring's own
  justification ("often succeeds with LLM variance") is about *human-initiated*
  retries after a model/temperature change, not a mechanical re-run.

**What actually fixes M7 for partner jobs:** fail the job. A GraphRecursionError
on a `skip_approvals=True` job should set `status=failed` +
`error_message` (the `run_scrape_task` except-ladder at tasks.py:141-164 already
does exactly this for any *other* exception — the recursion branch at
services.py:258 is the one place that diverts to waiting_approval). That is a
~5-line change in `create_recursion_approval` (or its two call sites), not a
mirror of human_approval. It also makes A's §4.2 table honest: the
`waiting_approval → inprogress` row's parenthetical ("job self-resumes") is
false for the recursion path today and stays false under the fold's fix.

**Second-order check:** does routing recursion→failed break *internal* jobs?
No — internal jobs that exceed recursion budgets currently sit in
`waiting_approval` until a human acts, which the round-1 critique already
documented as a hang the watchdogs explicitly skip
(`cleanup_stuck_jobs` docstring: "Jobs in WAITING_APPROVAL are untouched",
tasks.py:1140-1141). Failing fast is strictly better for them too, but that is
a behavior change to the internal product and should be called out as such
(A's §9 does not).

---

## M8 — PHASE_MAP normalization (SOUND, with unscoped legacy rows)

`graph.py:724` is the only site that maps `"browser_traverse" → "Browser
Navigation"` for `_notify_phase` (graph.py:871 `phase = PHASE_MAP.get(node_name,
node_name)`). Changing the value to `"browser_traverse"` makes `_notify_phase`
upsert `Step(phase="browser_traverse")` — which is exactly what
`_seed_pipeline_steps` (tasks.py:193-213) already seeds. The dedup the fold
asks for then happens **automatically for new jobs**: one row, matching the
seeded one.

**UI templates — verified safe, because every map is key→label:**

| Template | Map | Effect of the change |
|----------|-----|----------------------|
| `intake.html:1797-1804` `PHASE_TO_LABEL` | `browser_traverse:'Browser Navigation'` | none — the KEY is already the enum token; the label stays human |
| `intake.html:1905` | `PHASE_TO_LABEL[s.phase]\|\|s.phase` | none |
| `job_detail.html:361-366` `phaseLabels` | keys are enum tokens | none |
| `job_detail.html:97` | `{{ step.get_phase_display }}` | **this is the one that changes** — Django renders the `PHASE_CHOICES` verbose name, `models.py:228 ("browser_traverse", "Browser Navigation")`, so the display stays "Browser Navigation" for free |
| `job_detail.html:427` | `phaseLabels[phase]\|\|phase` | none |

`Step.PHASE_CHOICES` (models.py:225-243) already contains
`("browser_traverse", "Browser Navigation")` — the *stored value* is the enum
token and the *display* is the human string. So the spec's Phase enum
(sync_api.yaml:1630-1648) already matches `PHASE_CHOICES`, and the bug is
purely that `graph.py:724` writes the *verbose* string instead of the *value*.

**Unscoped consequence the fold missed — legacy rows.** Every navigation job
run to date has TWO rows: seeded `browser_traverse` (pending forever) and live
`"Browser Navigation"` (`_notify_phase` upsert). After the change:

- **Old jobs** keep both rows forever. The API's `phases[]` for a historical
  navigation job still contains the out-of-enum `"Browser Navigation"` —
  **the spec violation persists for all pre-existing jobs**. If partners can
  query historical jobs (listJobs has no created-before cutoff; `created_since`
  only filters forward), a data migration is needed:
  `Step.objects.filter(phase="Browser Navigation").update(phase="browser_traverse")`
  — which then **collides with the seeded pending row** (two rows, same
  `(job, phase)`) and needs a dedupe-merge (keep the live row's status/
  timestamps, delete the seeded pending one). Not hard, but it is a real
  migration step with a real conflict case; the fold's "Also dedupe the seeded
  `browser_traverse` step vs the live `"Browser Navigation"` row" acknowledges
  it in one clause without noting the unique-together collision or that it
  applies to *historical* rows, not just the seeded/live pair on a running job.
- `_ordered_steps` (views.py:142-155) sorts by `PIPELINE_PHASES` index with
  unknown→999: `"Browser Navigation"` sorts last today; after normalization
  it sorts at index 2. Internal UI ordering changes for live jobs — cosmetic
  and an improvement.

**Dead code the fold's "check UI templates" instruction would miss:**
`tasks.py:35-50 PHASE_MAP` (a *second*, different PHASE_MAP: maps phase-token
→ verbose label, includes `"testing": "Testing Loop"`) has **zero readers** —
grep shows only its definition; only `AGENT_PHASE_MAP` (tasks.py:52-62) is
referenced elsewhere in that file's own imports… and in fact `AGENT_PHASE_MAP`
also has no reader in tasks.py (it feeds nothing; `_upsert_step_from_event`
in services.py uses `NODE_PHASE_MAP`, services.py:476-486). These are
pre-existing dead maps. Irrelevant to M8's correctness, but if M8's
implementer greps `PHASE_MAP` to find every site to change, they will find
three (graph.py:717, tasks.py:35, services.py:476-as-NODE_PHASE_MAP) and must
touch exactly one. Worth naming so nobody "helpfully" normalizes the dead one
and invents a regression.

The existing test `test_phase_map_includes_browser_traverse`
(webapp/tests/test_browser_traverse_integration.py:101-105) asserts only key
presence — survives the value change. A's new Phase-enum spec-lock test is the
real guard. SOUND overall.

---

## M10 — finalize-time page index (FIXABLE-WITH-NOTES; ownership ambiguous)

**Does FM support it?** Yes, trivially — FM is a dumb key→bytes store
(file_master/app.py:55-66 PUT; verified no business logic, no sidecar, no
metadata beyond Content-Length on HEAD). Writing
`scrapers/{slug}/index/{output_filename}.json` via the existing
`artifacts.write_json` is a plain PUT. **No new FM endpoint is needed.** The
fold's open item ("coordinate with file-master contract (no mtime on HEAD
remains true)") is accurate — nothing about the index requires mtime.

**Where must the write happen, and what's the trap?** The index needs
`{offset, length}` per page — i.e. someone must parse the output file's item
array once. Two candidate sites:

1. **In `_finalize_job` (celery worker).** The worker has the file **locally**
   at `workspace/{slug}/output_*.json`… but look at the order (tasks.py:804-856):
   outputs are published to FM at :808-819, then `shutil.rmtree(ws)` at :855
   **deletes the local workspace**. So an index write added *after* the publish
   block must read from **FM** (`artifacts.read` — a 101 MB download back into
   the worker, artifacts.py:58-71 buffers the whole body) or be inserted
   **between** publish and rmtree while the local file still exists. The latter
   is the right insertion point and is cheap (local read, parse once, PUT a
   few-KB index). Neither plan names the insertion point; an implementer
   putting it "in the finalizer" near the FM publish loop (B's §2.2 hook row
   cites `tasks.py:806-811`) naturally lands *after* :811 and must either
   re-download or discover the rmtree.
   **Second trap at the same site:** the schema prune
   (`_prune_output_to_schema`, tasks.py:962-967) runs *after* the publish+rmtree
   and **rewrites the FM artifact in place** (tasks.py:650
   `artifacts.write_json(output_file, data)`) — dropping disallowed keys can
   change record *sizes* and therefore every byte offset. **An index computed
   before the schema prune is wrong after it.** The index must be written
   after :967 (or the offsets must be item-index-based, not byte-offset-based —
   but then the API's window reader can't seek and the optimization collapses).
   This ordering hazard is in neither plan and is the single most likely way
   M10 ships subtly broken.
2. **In the API view on first miss** — defeats the point (first page still
   O(file)).

**Whose plan owns it?** Genuinely ambiguous, and the fold's assignment is
self-contradictory on this point: M10 is assigned to **A** ("finalize-time
page index … written once per job by the worker"), but the *worker-side
finalize hooks* are B's territory (B owns `_finalize_job` hooks — §2.2 has
three of them). A's plan §1.4/§4.7 describes the *reader*; A's §9 lists the
finalize-time index as "the future optimization (not v1)" — **A's own file was
not updated to promote it into scope.** So the M10 resolution lives only in
the fold. Same document-lag class as M1/M12. The reader (A) and the writer
(B-side finalizer) are in different plans with no interface contract between
them — the exact seam failure mode the round-1 critique flagged as B1/M12.
Required: one paragraph in each plan naming the index key schema
(`scrapers/{slug}/index/{basename}.json`), the writer site (tasks.py between
publish and rmtree, after schema prune), and the invalidation rule (regenerate
whenever the artifact is rewritten).

The LRU (128 MB/worker) + 5s FM timeout + fail-fast 503: all implementable,
no coupling. `artifacts.read` already accepts a `timeout` override
(artifacts.py:58-65 — skills_store passes 5s for exactly this reason), so the
short-timeout primitive exists.

---

## M11 — rate limiting (BROKEN as "resolved in spec")

Decision 4: *"Redis fixed-window per key: 10 req/s, burst 30; 1 concurrent
stream/key (SSE), 60 creates/hour/key; 429 + Retry-After; **spec gains a
RateLimits section + 429 in the error model**."*

Verified against the amended specs:

- `grep -c 429 docs/specs/sync_api.yaml` → **0**
- `grep -c 429 docs/specs/async_api.yaml` → **0**
- No `RateLimits` section, no `x-rate-limits`, no `retry_after` beyond the
  pre-existing `not_ready` hints (sync_api.yaml:789, 921, 1058, 1394 — those
  are `details.retry_after_seconds` on 409s, unrelated).

The fold's M11 line reads "**resolved by decision 4**" and lists the spec
change as part of the resolution, with no "(done)" marker and no owner for the
spec edit (unlike m2/m3/M13/M14, which are marked "done in spec"). So this is
either (a) an unexecuted step with no owner, or (b) an executed step that
didn't land. Either way the artifact partners code against does not contain
the limits decision 4 chose, and the spec-lock test
(tests/test_api_docs_views.py:221) would not catch the omission. The Redis
mechanism itself is implementable and unremarkable. **Verdict: the code half
is SOUND, the spec half is BROKEN (absent), and the fold's bookkeeping doesn't
distinguish them.**

---

## M12 — atomic create + `emit(job.created)` (BROKEN as "written into A's plan")

The fold: *"M12 → A: create endpoint wraps create+dispatch in
`transaction.atomic()` and calls `events.emit(job.created)` inside it (B's
contract written into A's plan)."*

A's plan, verified after the fold:

- **§4.1 (create.py)** — the mapping table and dispatch paragraph
  (*"`run_scrape_task.delay(job.id, rescrape=False)`; store `celery_task_id`"*)
  contain **no** `transaction.atomic` and **no** `events.emit`. The only
  `atomic` in A's file is none — `grep -n atomic` on A's plan returns nothing.
- **§8 step 5** — *"create.py + api_create_job — mapping table, cross-field
  validation, duplicate 409, dispatch; ssrf.py…"* — unchanged, no emit.
- **§9 risks** — no mention of the emission obligation.

So the fold's parenthetical "(B's contract written into A's plan)" is **false
against A's file as it stands.** This is not a design problem — the mechanism
is trivial (`with transaction.atomic(): job = ScrapeJob.objects.create(…,
created_via="api"); JobCallback.objects.create(…);
events.emit(job.id, "job.created", …); run_scrape_task.delay(...)` — note
`delay` inside `atomic` is safe *because* the task re-reads the DB, but the
fold/B should say whether the dispatch goes inside or outside the block; B's
own on_commit discipline argues for inside-with-on_commit or just outside) —
but it is precisely the failure mode round 1 named for B1: *"The handoff note
cannot live in only one document."* It currently lives in only the fold.

**One additional ordering hazard neither plan notes:** A's create also writes
`input_urls.json` straight to FM (A §4.1: *"persisted directly to
`artifacts.scrapers_key(slug, 'input_urls.json')`"*). An FM write inside a DB
transaction is not transactional — on rollback the artifact outlives the job.
Pre-existing intake behavior (views.py:2599-2603), so parity is defensible,
but the new `atomic()` block makes the non-atomicity *visible* and worth one
line. Also `run_scrape_task`'s own same-site requeue (tasks.py:128-137,
`raise self.retry(countdown=60, max_retries=None)`) can outlive the HTTP
response; combined with the 409 duplicate guard this is bounded, as A notes.

---

## Re-verification 1 — A's D4 vs the new hook: do REST and events agree? (they do NOT, in exactly the m4 case)

This is the sharpest live inconsistency in the folded plans.

**REST side (A §4.2):**
```python
def sample_ready(job, steps) -> bool:
    # primary: per-job sample file exists
    # fallback: any Step(phase="testing").completed_at is not None
    #           AND scrapers/{slug}/analysis/test_report.json exists
```
The fallback fires whenever the testing step ever completed AND the per-site
test_report exists.

**Event side (B §2.2 revised row):** `job.sample_ready` is emitted at
`_invoke_code_tester` after `_preserve_test_report`, gated on first completion
via dedupe key `sample:{job_id}`.

**The finalize step-close block (tasks.py:1052-1063)** sets
`status=DONE, completed_at=now()` on **every RUNNING and PENDING** step of a
finalizing job — unconditionally, including `testing`, including jobs that
never reached code_tester.

Divergence cases:

1. **Job fails in site_analysis, finalizes FAILED.** Finalizer closes the
   `testing` step with `completed_at` set (m4). `test_report.json` — per-site,
   latest-job-wins — **exists from a previous job on the same site**
   (`_preserve_test_report` wrote it there, graph.py:599-600). REST: primary
   sample file absent (hook never ran), fallback TRUE (testing completed_at set
   + per-site test_report exists) → `sample_available` computed… A's §4.4 says
   `sample_available` = per-job file OR (`sample_ready()` AND test_report
   exists) OR (terminal AND output resolvable). For this FAILED job with no
   output: per-job file ✗, `sample_ready()` ✓ (spuriously), test_report ✓ →
   **REST reports `sample_available: true` while `state` is `failed`** — and
   `GET /sample` hits A's §4.6 resolution order: per-job file ✗ → "terminal
   and `_resolve_job_output` resolves" ✗ → **409 `not_ready`**. REST just said
   it was available. Self-inconsistency inside A's own plan, no events involved.
   The state table happens to mask it (terminal short-circuits `state`), which
   is exactly m4's "the invariant is accidental."
2. **Same job, events:** the hook never ran → no `job.sample_ready` event →
   **correct**. So in this case REST lies and events tell the truth → *the
   two disagree*, which is B1's failure signature in a new location.
3. **Retry-cycle FAIL then PASS (the common case):** hook fires on the FAIL
   cycle too (see B1 Note 1) → event emitted (dedupe key = first emission
   wins) → REST and events agree. Fine.

**Conclusion:** the fold's m4 disposition ("A adds the 'testing-completed
consulted only while running' test lock; no code change Phase 1") does NOT
close this, because the divergence is between the *fallback signal* and the
*hook*, and the test lock only pins REST's behavior — it doesn't make REST and
the event stream agree. **The clean fix is to drop or harden the fallback**:
either (a) make the fallback require `job.status == running` (A's §4.2 already
implied state-dependence but implements it as a free function of `(job, steps)`),
or (b) make the per-site `test_report.json` fallback per-job (A already notes
the per-site hazard for *contents* but still uses it for the *state* signal).
(a) is a one-line change to the derivation and eliminates case 1 entirely.
Recommend the fold adopt it; as written, Phase 1 ships a partner-visible
contradiction (`sample_available: true` + `GET /sample` 409) that no test in
A's list catches (A's #4 covers monotonicity, #7 covers happy paths).

---

## Re-verification 2 — the 4-state projection (SOUND; no dead or unmapped statuses)

`ScrapeJob.STATUS_CHOICES` (models.py:108-117) — 8 values. Every status the
projection maps is producible; every producible status is mapped.

| Status | Producer (verified) | Projection | OK |
|--------|---------------------|------------|----|
| `pending` | model default (models.py:130) | inprogress | ✓ |
| `running` | tasks.py:223, 330 | inprogress/sample_ready | ✓ |
| `waiting_approval` | tasks.py:260/275/404/435, services.py:433 | inprogress | ✓ (but see M7 — for recursion-origin pauses the job never self-resumes) |
| `completed` | tasks.py:934 | scraper_ready | ✓ |
| `failed` | tasks.py:153/161, 419, 725, 930, 932, 1196, 1395 | failed | ✓ |
| `cancelled` | tasks.py:927; views.py:462 | failed/cancelled | ✓ |
| `captcha_blocked` | graph.py:1238 | failed | ✓ |
| `akamai_blocked` | graph.py:1238 | failed | ✓ |

- **No dead statuses** — all 8 are written by live code paths.
- **No unmapped statuses** — there is no `paused`, no `timeout`, no `partial`
  anywhere in the model or in any write site (grep over all `job.status =`
  assignments confirms only the 8 constants appear).
- The one soft spot is `waiting_approval` → `inprogress`: reachable for
  partner jobs via `create_recursion_approval` (services.py:433 sets it with no
  `skip_approvals` check — the M7 bug) AND via every genuine interrupt path —
  but all genuine interrupt call sites (`check_tracker.py:233/279`,
  `field_confirmation.py:380`, `human_approval.py:124`) already guard on
  `skip_approvals` (check_tracker.py:165/167, field_confirmation.py:358,
  human_approval.py:116). **So for `skip_approvals=True` partner jobs the ONLY
  route into `waiting_approval` is the recursion path** — which strengthens
  M7's importance: it is not one of several leaks, it is the *single* leak, and
  the fold's chosen fix (mirror human_approval) is the one that doesn't apply.
  A's §4.2 parenthetical "budget-escalation pauses set this … job self-resumes"
  remains unsupported by any mechanism for that path.

---

## NEW FINDINGS (not in round 1)

**NEW-1 (major) — REST `sample_available` vs `GET /sample` self-contradiction + REST/event divergence on failed-before-testing jobs.**
Full analysis in Re-verification 1. Root cause: the per-site
`test_report.json` fallback signal combined with the finalize step-close block
(tasks.py:1052-1063). Fix is one line (state-gate the fallback). Owner: A.

**NEW-2 (major) — B's plan §2.3 was never rewritten and still prescribes the dead hook.**
`api-async-implementation-plan.md:71-73` ("§2.3 Prerequisite: sample
persistence") still says to write the sample at field_confirmation and cites
`field_confirmation.py:280-313`, with no supersession marker. Only D4/D9/D12
carry "SUPERSEDED". An implementer reading B top-down executes the B1 bug.
The fold's claim "B's plan file updated 2026-08-23" is true for the D4 row and
false for §2.3. Owner: B (one-paragraph replacement).

**NEW-3 (major) — M10's index has a write-ordering hazard: the schema prune rewrites the artifact AFTER publish, invalidating any offsets.**
tasks.py:962-967 (`_prune_output_to_schema`) rewrites `job.output_file` in FM
at tasks.py:650; it runs after the publish block and after the workspace
rmtree. Any byte-offset index written between publish (:811) and rmtree (:855)
is stale by :967. Also the workspace is deleted at :855, so a late index write
must re-download from FM. Neither plan names the insertion point. Owner: B
(writer) + A (reader contract).

**NEW-4 (moderate) — M5's "keep newest 5" redefinition is unbounded per-site and interacts with M10.**
Exempting partner outputs converts "5 per site" into "5 internal + N partner
per site" on a *shared* prefix. A partner site scraped 50 times accumulates 50
indexable outputs + 50 indexes. Decision 3 accepted FM growth, but the M10
index multiplies it (one more artifact per output) and nothing prunes
*indexes* when their output is (eventually) deleted — the existing prune loop
only matches `output_*` prefixed keys (tasks.py:868), so orphaned index files
accumulate silently. Owner: whoever lands M10.

**NEW-5 (moderate) — M3's `attempts` never increments on crash-loop, so disable-on-exhaustion never fires for poison events.**
See M3 item 2. The spec's `on_exhaustion: disable_callback`
(async_api.yaml:484) is keyed on attempt count; a delivery task that dies
mid-lease forever (OOM, worker recycle at `CELERY_WORKER_MAX_TASKS_PER_CHILD=10`,
settings.py:138-140 — which recycles workers *routinely*, making mid-task death
a normal event, not an anomaly) never exhausts. Owner: B.

**NEW-6 (moderate) — B5's events worker must run migrations or it crash-loops; and beat runs the dispatcher + reconciler inside a 128 MB container.**
docker-compose.yml:151 (`celery-beat mem_limit: 128m`), :124-126 (beat is the
only celery service that runs `migrate`). The new events worker importing
`scraper.tasks`/models before the EventOutbox table exists will crash-loop;
invisible Redis buildup follows. See B5. Owner: B + deploy docs.

**NEW-7 (minor) — the `waiting_approval` projection row in A's §4.2 and the spec cites a self-resume that does not exist for the only reachable path.**
See Re-verification 2. With every genuine interrupt guarded by
`skip_approvals`, the recursion path is the sole remaining producer of
`waiting_approval` for partner jobs, and it never resumes. A's table text and
sync_api.yaml's `waiting_approval` row both need the correction once M7 lands
as fail-fast.

**NEW-8 (minor) — M8 needs a data migration with a collision case, not just a constant change.**
Historical `Step(phase="Browser Navigation")` rows violate the enum forever
without one, and the migration collides with the seeded pending
`browser_traverse` row on the same `(job, phase)` — needs merge-and-delete, not
a blind `update()`. Also `tasks.py:35-50 PHASE_MAP` and `tasks.py:52-62
AGENT_PHASE_MAP` are dead (no readers) and will confuse the implementer's
grep. See M8.

**NEW-9 (minor) — B2 residue: sync_api.yaml:1594 still says "stored hashed (SHA-256) at rest" for `callback_secret`.**
Decision 2 chose raw. The async spec was corrected (async_api.yaml:452-455);
the sync spec's field description was not. The two specs disagree on the same
field — the exact class B2 was filed to close. One-line spec edit + the
existing cross-spec test extended to lock the storage wording.

**NEW-10 (nit) — A's create writes `input_urls.json` to FM inside the new atomic block; FM writes are not transactional.**
On rollback the artifact outlives the job. Pre-existing intake parity, but the
new `transaction.atomic()` makes it worth one sentence (and arguably moving
the FM write after commit).

---

## What checked out (for balance)

- **B1's site choice** — `_invoke_code_tester` after `_preserve_test_report`
  (graph.py:3413/3425) is the right hook; `_notify_phase(done)` at :3413
  precedes it; the local workspace holds real output files there (run_scraper
  persists browser_service output locally precisely for this consumer,
  shell_tools.py:330-344).
- **M2** — `completed_at` exists, is set on every terminal path, index is trivial.
- **M4** — `created_via` default is safe at all six create sites; pre-migration
  jobs correctly classify as `intake`.
- **M8's core** — every UI phase map is key→label and survives the value
  change; `get_phase_display` keeps the human label for free.
- **M13/M14/m2/m3** — all four verified present in the amended specs
  (async_api.yaml:150-152 `events?token=`; listJobs 422 with
  `invalid_page`/`invalid_created_since`; per-item `maxLength: 1000` on
  `item_urls` and `listing_urls` at sync_api.yaml:1507-1524; MUTABILITY at
  :678).
- **M9** — widening B's Redis-set stream counter to count internal streams is
  mechanical; the internal SSE view's channel (`job:{id}`, services.py:469)
  and the partner channel (`job:{id}:envelope`) are separable, so a shared
  counter does not conflate the two feeds.
- **B4** — nothing in the delivery path prevents re-running A's
  `validate_callback_url` per attempt; `httpx.post(..., follow_redirects=False)`
  is the default in httpx and pinning it explicitly is free.

---

## Required before implementation (ordered)

1. **M5 rewritten** as a reverse-lookup exemption (fold text + A/B plan step)
   — the one-line condition is unwritable. [BROKEN]
2. **M7 redesigned** as fail-fast for `skip_approvals` jobs, not a
   human_approval mirror; spec + A's §4.2 row corrected. [BROKEN]
3. **M11's spec half executed** (RateLimits section + 429 in the error model)
   or explicitly re-assigned with an owner. [BROKEN]
4. **M12's contract actually written into A's plan** (§4.1 + §8 step 5), and
   M1's countdown written into B's §4 (currently both plans still contradict
   the fold). [BROKEN-as-claimed]
5. **B's §2.3 superseded/rewritten** (NEW-2). [BROKEN]
6. **A's `sample_ready` fallback state-gated** (`status == running`) to close
   the REST/event divergence (NEW-1) — one line + one test.
7. **M10's write site + invalidation rule specified** in both plans (NEW-3),
   and index pruning decided (NEW-4).
8. **B5's deploy steps** (compose service, Railway service, routing,
   migrate-on-boot, outbox-depth alert) written down (NEW-6).
9. **M3's CAS-claim + attempts-on-lease-take** added to B §4 task 2 (NEW-5).
10. **sync_api.yaml:1594 secret wording** corrected (NEW-9); M8 data migration
    scoped (NEW-8); B1 pass-gate-or-document decision recorded (B1 Note 1).
