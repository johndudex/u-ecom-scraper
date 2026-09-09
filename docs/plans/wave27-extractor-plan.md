# Wave-27 — Extractor CRUD: update, delete/archive, field instructions, field order

_Status: PLAN (awaiting green-light). Source: the API audit (docs/extractor-builder-api-guide.md) + the team's four asks. All items TDD; phases ship independently._

## Context — what the audit found

The team asked for: (1) update an extractor's config, (2) delete or archive one, (3) per-field instructions, (4) field order. Today:

1. **Update** — job-level only (`job_update`/`job_restart`); Site-row edit route is narrow and the shipped Edit form **POSTs to the wrong URL** (`site_form.html:22` → `site_add`, so UI edits always fail duplicate-URL validation). No partner-API endpoint.
2. **Delete** — `POST /sites/<id>/delete/` exists: bare hard delete, no confirm, no ownership check, no UI link, orphans `scrapers/<slug>/` artifacts. **Archive: does not exist** (no column, no route). Saved-job library has no remove action either (only rename/open).
3. **Instructions** — fields are bare name strings end-to-end (chips → `target_fields` → validator keeps `name`+`type` → `Site.output_schema.fields=[{name}]`). Job-level `notes` is the only guidance channel that reaches agents.
4. **Order** — config order IS preserved (`target_fields`, schema document order). But `prune_record_to_schema` iterates `record.items()` — **output key order = scraper emission order**, not the user's chosen order. No reorder UI.

## Items

### W27-1 (P0 bug) — Fix the Edit form action
`site_form.html:22`: render `action` conditionally — edit mode → `{% url 'site_edit' site.id %}`, add mode → `site_add` (keep the hidden `site_id` or drop it).
- Tests: GET `/sites/<id>/edit/` renders form whose action is the edit URL; GET `/sites/add/` still posts to add; POST to `/sites/<id>/edit/` saves and redirects to detail.

### W27-2 (P0 hardening) — Delete guard
`site_delete` (views.py:1847): require **superuser** (403 otherwise) and a POST param `confirm=<slug>` (mismatch → no-op redirect). Keep hard delete; artifacts intentionally remain (documented — File-Master artifacts are never auto-destroyed). Add a Delete control on `site_detail` **for superusers only**, with a JS confirm that echoes the slug.
- Tests: non-superuser blocked; missing/wrong `confirm` no-ops; correct call deletes row, jobs untouched (no FK), redirects to list.

### W27-3 (P1) — Archive (the supported removal path)
Migration **0040** (one migration, two fields):
- `Site.archived_at = DateTimeField(null=True, blank=True)` + `is_archived` property. No data backfill.
- `ScrapeJob.field_notes = JSONField(default=dict, blank=True)` (consumed by W27-4).

Behavior:
- `POST /sites/<id>/archive/` and `/unarchive/` (login required). Archived shows a badge + banner; `site_list` excludes archived by default (`?archived=1` to show).
- **Guards:** `site_scrape` and `site_rerun` refuse archived sites (redirect + message, one-click Unarchive offered).
- **Explicit user intent wins:** when the pipeline's `check_tracker` (models/tasks auto-create path, check_tracker.py:254) encounters an archived Site for a newly submitted job, it **auto-unarchives** and logs. No job is ever blocked by a stale archive.
- **Intake library (the screen the team actually means by "Extractor list"):** add "Remove from Saved" — posts the existing `job_update` with `is_saved=0`. Saved jobs are not Sites; un-save is their delete. No job hard-delete (jobs are the audit trail).
- Tests: archive/unarchive round-trip; list filtering; scrape/rerun guard; auto-unarchive on new job; un-save via UI action.

### W27-8 (P1) — Rerun lineage (`is a rerun` + original job id)
Team decision: two-object model stands (Site + saved job), but jobs must **know they're a rerun and of what**. Four job-creation paths exist (`home` views.py:267, `job_restart` :661, `site_scrape` :1613, `intake_create_job` :2860) — lineage applies ONLY where the user explicitly re-runs: **`job_restart`** (the intake "Re-run" / job_detail button). `site_scrape` and API creates are fresh scrapes (site history is already visible on site_detail); they stay unlinked.
- Migration **0040** (already open for W27-3 — carries these too):
  - `ScrapeJob.parent_job = FK('self', null=True, blank=True, on_delete=SET_NULL, related_name='reruns')` — the immediate source job.
  - `ScrapeJob.origin_job = FK('self', null=True, blank=True, on_delete=SET_NULL, related_name='+')` — the chain root, so "the original job id" is one read, not a walk (restart-of-restart chains: A→B→C, C.origin=A).
- `job_restart` sets `parent_job=job`, `origin_job=job.origin_job or job` on the clone. Nothing else mutates them.
- **API**: `JobStatus` and `JobSummary` gain `rerun_of` (origin id or null — `is_rerun` is just `rerun_of != null`, no separate flag); sync_api.yaml updated. `intake_jobs` JSON gains `rerun_of` for the library.
- **UI**: "Re-run of #A" link on job_detail header, job_list rows, and intake library entries.
- Tests: restart sets both fields; nested chain roots at A; terminal-source restart carries origin; API/list JSON expose `rerun_of`; no lineage on the three fresh-create paths (pin).

### W27-4 (P1) — Per-field instructions, end-to-end
- **Validator** (`src/schema_validation.py`, backward-compatible): internal dialect `{fields:[{name, type?, description?}]}` and standard JSON-Schema `properties.<f>.description` — accept, cap each at 300 chars (issue `DESCRIPTION_TOO_LONG` severity=warning, truncate), carry into `normalized.fields` as optional `description`. `derived_fields` unchanged (pure strings). New helper `extract_field_notes(doc) -> dict[str, str]`.
- **Create flows** populate `ScrapeJob.field_notes`: intake_create_job (schema descriptions + explicit form field), API `create_job` (new optional `field_instructions: {name: text}` merged over schema descriptions), `job_restart`.
- **Prompts**: product_analyzer + code_writer message builders render a bounded `### Field guidance` section (`name: description` lines; ≤100 fields) so agents extract what the user meant — the actual payoff of the feature.
- **Site persistence**: tasks.py Site-write (`Site.output_schema.fields`) carries `description` through, so the saved extractor remembers guidance.
- **UI**: ✎ note affordance on intake chips.
- **validate-schema** (partner + intake): response gains `fields: [{name, description?}]` alongside the unchanged `derived_fields`.
- Tests: validator accept/cap/absent (identical normalized shape to today when no descriptions — pin), create flows store notes, writer message contains the guidance section, Site persistence, `job_update` can edit `field_instructions`.

### W27-5 (P2) — Field order made real
- `prune_record_to_schema` (src/content_types.py:374): emit keys in `allowed` (target_fields) order, bookkeeping fields appended in fixed order. JSON-semantics-neutral byte-layout change; note in changelog.
- **UI**: up/down arrows on intake chips (splice reorder in `fieldsArr`; no drag-drop dependency).
- **Doc contract** (sync_api.yaml + guide): "`target_fields` order is the output record key order."
- Tests: order pin on prune; UI reorder reflected in submitted `target_fields`.

### W27-6 (P2) — Partner-API extractor resource (`webapp/scraper/api/extractors.py`)
- `GET /api/v1/extractors` — paginated list of Sites **scoped to the key's own jobs** (distinct Site rows whose `url` matches the partner's ScrapeJobs, iexact — same join `site_detail` uses). Preserves the tenancy boundary without a `Site.owner` migration.
- `GET /api/v1/extractors/{slug}` — url, name, slug, site_type, platform, scraping_method, has_scraper, product_count, last_scraped_at, archived_at, input_urls count, `fields: [{name, type?, description?}]`.
- `PATCH /api/v1/extractors/{slug}` — strict allowlist: `name`, `site_type`, `sample_url`, `currency`, `input_urls`, `field_notes`. Unknown key → 422 `validation_failed`. Reuses SiteForm cleaners (URL normalization, input_urls JSON-array validation, shrink-guard).
- `POST /api/v1/extractors/{slug}/archive` | `/unarchive`. **No partner DELETE in v1** — archive is the removal path (per the "delete is dangerous" agreement); hard delete stays a superuser UI action.
- Cross-tenant slug → 404 (non-oracle). Standard ApiError shapes + rate limits.
- Tests: tenancy scoping (other partner's slug 404s), allowlist rejection, archive round-trip, PATCH validation parity with SiteForm.

### W27-7 (P2 docs) — Both API specs kept in lockstep with the code
**sync_api.yaml**:
- new extractor resource paths (W27-6): GET list/detail, PATCH allowlist, archive/unarchive; no DELETE (documented as deliberate).
- `JobStatus` + `JobSummary` gain `rerun_of` (integer|null) with the lineage semantics (W27-8).
- `CreateJobRequest` gains `field_instructions` (object, name→text, values ≤300 chars, ≤100 entries); `SchemaValidationResult` gains `fields: [{name, description?}]` alongside the unchanged `derived_fields` (W27-4).
- `target_fields` description updated to the order contract: "target_fields order is the output record key order" (W27-5).
- check-site drift fix: describe the shipped metadata-only contract instead of "deliberately NOT exposed".

**async_api.yaml**:
- `JobCreatedData` gains `rerun_of: integer|null` — emitted on `job.created` (implementation touchpoint: the `emit(job, "job.created", …)` payload in `api/writers.create_job`, W27-8) so partners learn a job is a re-run at creation time, not on first poll. The event envelope itself is unchanged (`additionalProperties:false` envelope untouched — only the data schema grows).
- **No new event types.** The four state events stand; archive/unarchive, field-instruction edits, and reorder are NOT events in v1 — the async surface stays job-scoped. (Extractor-level events, e.g. `extractor.archived`, are a future extension; noted in the spec's backlog section.)

Refresh `docs/extractor-builder-api-guide.md` to match both.

## Phasing

| Phase | Items | Ships |
|---|---|---|
| **27a — correctness + archive + lineage** | W27-1, W27-2, W27-3, W27-8 | the team's ask #2 done safely + rerun tracking |
| **27b — instructions + order** | W27-4, W27-5 | asks #3 + #4 |
| **27c — partner extractors + docs** | W27-6, W27-7 | asks #1 + #2 over the API |

Each phase: full suite (2,350 green baseline) + ruff, EB sync per standing rules (wave branch **and** fork main + merge-tree check).

## Decision points (recommendations — override any)

1. **Delete permission**: superuser-only now; per-user Site ownership is a separate product change. ✅rec
2. **New job on archived site**: auto-unarchive + log (explicit submission beats a stale archive). ✅rec
3. **Partner DELETE**: not in v1 — archive/unarchive only. ✅rec
4. **Extractor tenancy**: derive-from-own-jobs scoping, no `Site.owner` FK this wave. ✅rec
5. **Output key reorder**: do it — target_fields order, bookkeeping appended. ✅rec
6. **Lineage granularity**: store BOTH `parent_job` (immediate) and `origin_job` (chain root); expose `rerun_of` (origin) — one read answers "the original job id", the walk stays available via `parent_job`. ✅rec

## Risks / notes
- Migration 0040 adds two nullable/defaulted fields only — trivial deploy, no backfill.
- Output byte-layout changes (W27-5) — semantically neutral for JSON consumers; changelog note.
- `field_notes` is the only new prompt surface — bounded (≤100 × 300 chars ≈ ≤30 KB worst case; typical ≪1 KB).
- Suite is green at 2,350; every backward-compat pin (validator shape unchanged without descriptions) is itself a test.
