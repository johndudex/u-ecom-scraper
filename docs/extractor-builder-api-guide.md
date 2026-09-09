# Extractor Builder — API Guide

_Updated 2026-09-09 for the wave-27 release (extractor management + per-field instructions + field order). Every contract below cites the file that implements it. Canonical machine-readable specs: `docs/specs/sync_api.yaml` (OpenAPI 3.1) and `docs/specs/async_api.yaml` (events), both also rendered at `/docs/sync_api` and `/docs/async_api` on the deployment._

## 0. TL;DR — the four requested capabilities

There are **two distinct API surfaces** (see §1): the partner API (`/api/v1/*`, key-auth) and the screen-backing session endpoints (`/intake/*`, `/jobs/*`, `/sites/*`, browser login). All four asks shipped in wave-27:

| # | Team's ask | Shipped (wave-27) |
|---|-----------|-------------------|
| 1 | **Update an existing extractor's configuration** (Extractor detail / Edit screen) | **Edit form fixed + partner PATCH.** The UI Edit form now posts to `/sites/<id>/edit/` (the old wrong-action bug is gone). New partner resource: `PATCH /api/v1/extractors/{slug}` — strict allowlist `{name, site_type, sample_url, currency, input_urls, field_notes}`, unknown keys → 422, validation parity with SiteForm (§4.1). |
| 2 | **Delete or archive an extractor** (Jobs & Saved / Extractor list) | **Archive shipped; delete hardened.** `Site.archived_at` + `POST /sites/<id>/archive|/unarchive` + `POST /api/v1/extractors/{slug}/archive|/unarchive`. Site list hides archived by default (`?archived=1` shows all); `site_scrape`/`site_rerun` refuse archived sites; a NEW job on an archived site auto-unarchives it (explicit submission beats a stale archive). Hard delete is now **superuser-only** with a typed `confirm=<slug>` gate; jobs are never touched (no FK). Jobs themselves have no delete — "Remove from Saved" (`is_saved=0`) is the removal path there (§4.2). |
| 3 | **Add instructions for each field** (New extraction + Edit configuration) | **End-to-end.** Schema `description` strings and the intake chip ✎ notes become `ScrapeJob.field_notes`; partner create takes `field_instructions` (≤100 × 300 chars, longer → 422). Notes are rendered as a bounded **"### Field guidance"** section in the product_analyzer + code_writer prompts, persisted into `Site.output_schema.fields[].description` on completion, and returned by both validate-schema endpoints as `fields: [{name, description?}]` (§4.3). |
| 4 | **Change field order** (same screens) | **Real + documented.** The finalizer now emits output record keys in `target_fields` order (bookkeeping `url, src_url, scraped_at, status_code` appended) — enforced at the deterministic prune, both the nested and flat paths. The intake chips gained ▲/▼ reorder controls; the order contract is in both specs (§4.4). |

Re-run lineage (ask-adjacent, shipped alongside): every re-run carries `parent_job` (immediate source) + `origin_job` (chain root); partner `JobStatus.rerun_of` / `JobSummary.rerun_of` expose the original job id, and the async `job.created` event data carries `rerun_of` too. The jobs table shows a `↻ #id` chip and job pages link "Re-run of #id".

---

## 1. The two API surfaces

| | **A. Partner API v1** | **B. Screen-backing endpoints** |
|---|---|---|
| Base | `/api/v1/*` | same host, UI routes |
| Auth | `X-API-Key` header (SHA-256-hashed key; maps 1:1 to a service-account user) | Django session (login) + CSRF; AJAX endpoints additionally require `X-Requested-With: XMLHttpRequest` |
| Tenancy | Every job scoped to the key's user; extractors scoped to Sites reachable from the key's jobs; cross-tenant reads are **404** (non-oracle) | Superusers see all; regular users own their jobs; Sites are global |
| Rate limit | 10 req/s sustained, burst 30, per key → `429` + `Retry-After` | none |
| Specs | `docs/specs/sync_api.yaml`, `async_api.yaml` | — |
| CORS/CSRF | CSRF-exempt | CSRF enforced |

Key management: `/intake/tokens/` (list), `/intake/tokens/create/` (raw key shown once), `/intake/tokens/<id>/revoke/`.

**Error shape (partner API, all endpoints):**

```json
{ "code": "validation_failed", "message": "...", "details": { } }
```

Statuses: 401 `unauthorized`, 403 `forbidden`, 404 `not_found`, 405 (`method_not_allowed` — the extractor DELETE answer), 409 (`duplicate_running_job`, `not_cancellable`, `callback_already_active`), 422 (`validation_failed`, `schema_invalid`, `invalid_callback_url`, `invalid_page`, `invalid_page_size`), 429 `rate_limited`, 500 `internal_error` (body carries a `trace_id`). (`webapp/scraper/api/errors.py`)

## 2. Partner API v1 — endpoints

Job state model (sync spec): `inprogress` → `sample_ready` → `scraper_ready` | `failed`. Terminal `failed` does **not** imply no data — sample/output still resolve if earlier phases produced records.

| Method & path | Purpose |
|---|---|
| `POST /api/v1/jobs` | Create + dispatch a job. **202** + `{job_id, state, created_at, status_url, sample_url, output_url, output_download_url, scraper_code_url}` + `Location` header. 409 if an identical-URL job is already pending/running for this key. Body gains **`field_instructions`** (wave-27). |
| `GET /api/v1/jobs` | Paginated list (`page`, `page_size`) of this key's jobs — `{jobs: [JobSummary], page, page_size, total_items, total_pages}`. Rows carry `rerun_of`. |
| `GET /api/v1/jobs/{id}` | Full status: `state`, `internal_status`, `current_phase`, `phases[]`, availability flags, `item_count`, `failure`, `callback`, timestamps, **`rerun_of`** (chain-root job id; null for fresh jobs). |
| `POST /api/v1/jobs/{id}/cancel` | Cancel a pending/running/waiting-approval job → `{state:"failed", failure:{code:"cancelled"}}`. |
| `GET /api/v1/jobs/{id}/sample` | Up-to-5 sample records once testing passed (`{job_id, records, record_count}`); 404 `not_ready` before. |
| `GET /api/v1/jobs/{id}/output?page=&page_size=` | Paginated full output over the record array. Record keys follow `target_fields` order, bookkeeping appended (wave-27). |
| `GET /api/v1/jobs/{id}/output/download` | Raw file stream (`Content-Disposition: attachment`). |
| `GET /api/v1/jobs/{id}/scraper-code?format=json\|raw` | Generated Python source (`{code, filename, size_bytes}` or `text/x-python`). |
| `GET /api/v1/jobs/{id}/callback` | Callback registration health: `{status, url, disabled_reason, last_failure, delivered_count, pending_count, …}`. |
| `PATCH /api/v1/jobs/{id}/callback` | `{"action":"reenable"}` (60 s cooldown) or `{"action":"rotate","callback_url":…,"callback_secret":…}`. |
| `GET /api/v1/jobs/{id}/events` | SSE stream of job events. |
| `GET /api/v1/ws-token` | Short-lived token for the AsyncAPI WebSocket channel. |
| `POST /api/v1/validate-schema` | `{"schema": <object>}` → **200 always** `{valid, issues, derived_fields, detected_content_type, fields: [{name, description?}]}`. `fields` carries per-field instructions (wave-27). Pure function, no writes. |
| `GET/POST /api/v1/check-site` | Metadata-only site recognition: `{known_site, platform, site_type, site_name, scraping_method, last_scraped_at}`. Deliberately does **not** return known fields (cross-tenant leak guard). |
| `GET /api/v1/extractors` | Paginated list of Sites reachable from THIS key's jobs (trailing-slash tolerant), most recently updated first — `{extractors: [ExtractorSummary], page, page_size, total_items, total_pages}`. Other partners' sites and job-less sites never appear (no oracle). |
| `GET /api/v1/extractors/{slug}` | Detail: summary fields + `platform`, `scraping_method`, `input_urls_count`, `fields: [{name, type?, description?}]` (the stored output schema). Cross-tenant slug → 404. |
| `PATCH /api/v1/extractors/{slug}` | Strict allowlist `{name, site_type, sample_url, currency, input_urls, field_notes}`; unknown key → 422 and nothing is applied. `field_notes` merges into `fields[].description` by name (unknown names appended). Returns the updated detail. |
| `POST /api/v1/extractors/{slug}/archive` / `/unarchive` | Idempotent soft-remove / restore → `{slug, archived_at}`. **No partner DELETE** — `DELETE /api/v1/extractors/{slug}` answers **405 by design**; hard delete stays a superuser UI action. |

### `POST /api/v1/jobs` request body

Required: **`url`** (a sample *item* page, not the homepage) and **`input_mode`** ∈ `url_list | list_page | navigation | search_term`.

| Field | Notes |
|---|---|
| `item_urls[]` | required for `url_list` (≤10 000, ≤1000 chars each; deduped; persisted to `scrapers/<slug>/input_urls.json`) |
| `listing_urls[]` | required for `list_page` (≤50; newline-joined into search criteria) |
| `search_keywords` | required for `search_term` (`search_criteria` accepted as legacy alias) |
| `search_url` | optional for `search_term` — a known search-results page |
| `content_type` | `product` (default) — registry also covers `article`, `job_posting`, `forum_thread`, `serp`, `page_content` |
| `target_fields[]` | **authoritative** output schema — finalizer prunes records to these (+ `url`, `src_url`, `scraped_at`, `status_code`). **Order contract:** record key order follows `target_fields` order, bookkeeping appended. Omit = extract everything found. |
| `schema_text` | raw JSON Schema (standard 2020-12, internal `{fields:[{name,type?,description?}]}`, flat `{field:type}` map, or bare `["a","b"]` array). Validated; invalid → 422 with issue list; derived names become `target_fields`; per-field `description`s become instructions. Limits: 256 KiB, 100 fields, name ≤64 chars, description ≤300 chars (longer → `DESCRIPTION_TOO_LONG` warning + truncation, never a block), nesting ≤5, local `$ref` only. |
| `field_instructions` | `{field_name: instruction}` — ≤100 entries, each ≤300 chars (violations → 422). Merged OVER schema descriptions. Reaches the agents as the "Field guidance" prompt section and persists into `Site.output_schema.fields[].description`. |
| `scope` / `scope_value` | `all` (default) / `firstn` (value = N, → `--limit`) / `filter` (advisory match expression) |
| `notes` ≤4000 | free-text guidance to the analysis agents (advisory) |
| `title` ≤200 | display name |
| `dagster_enabled` | opt-in Dagster asset generation |
| `callback_url` + `callback_secret` | optional webhook (async_api.yaml): HTTPS only, SSRF-guarded, secret 32–256 chars. Delivery is HMAC-signed: `X-Scraper-Signature: t=<unix>,v1=<hmac_sha256("<t>."+body, secret)>`, events `job.created` (now with `rerun_of`), `job.sample_ready`, `job.scraper_ready`, `job.failed`. |

## 3. Screen-backing endpoints (surface B) — what each screen actually calls

All `@login_required`; AJAX ones require `X-Requested-With: XMLHttpRequest`; POSTs need CSRF.

| Screen | Endpoints |
|---|---|
| **New extraction** (`/intake/`) | `POST /intake/check-site`; `POST /intake/validate-schema` (response includes `fields` with descriptions); `POST /intake/discover-fields`; `POST /intake/create-job` (form: `url`, `nav_method`, `content_type`, `target_fields` (comma list — chip order), `field_notes_json` (chip ✎ notes; merged over schema descriptions), `scope`, `scope_value`, `notes`, `listing_urls`/`search_keywords`+`search_url`/`list_urls`, `schema_text`/`schema_file`, `dagster_enabled` → 200 `{job_id, …}`; 409 on duplicate live job). `GET /intake/jobs` → team job library JSON (rows carry `field_notes`, `rerun_of`). |
| **Jobs & Saved / Extractor list** | `GET /jobs/`; `GET /intake/jobs?user=` (saved = `is_saved:true`). Row actions: View / Cancel / Re-run / **Remove from Saved** (`POST /jobs/<id>/update/` with `is_saved=0`). Jobs table shows `↻ #origin` chips for re-runs. |
| **Extractor detail** | `GET /sites/<id>/` (+ jobs, live `scraper.py`, outputs, version archive); `POST /sites/<id>/scrape/`; `POST /sites/<id>/rerun/`; `POST /sites/<id>/sync-urls/`; scraper-code / output / archive downloads. Detail page now has **Archive / Unarchive** buttons and a superuser-only Delete (typed `confirm=<slug>`). |
| **Extractor list** | `GET /sites/` hides archived sites; `GET /sites/?archived=1` shows all with an Archived badge. |
| **Edit configuration (job-level)** | `POST /jobs/<id>/update/` — AJAX allowlist now: `title`, `target_fields`, **`field_notes_json`**, `scope`, `scope_value`, `notes`, `search_criteria`, `search_url`, `is_saved` → JSON echo (includes `field_notes`). |
| **Edit configuration (re-run with changes)** | `POST /jobs/<id>/restart/` — accepts `prompt`, `force_full`, `target_fields`, `schema_text`, `dagster_enabled`, `search_criteria`, `search_url`, `scope`, `scope_value`; clones into a NEW job with `parent_job`/`origin_job` lineage set; `field_notes` copy from the source job. |
| **Edit screen (Site row)** | `GET/POST /sites/<id>/edit/` — Django `SiteForm`: `url`, `sample_url`, `currency`, `site_type`, `input_urls_json`/`input_urls_file`. **The form action bug is fixed** (edit mode posts to `/sites/<id>/edit/`). |

## 4. The four asks — where they landed

### 4.1 Update an existing extractor's configuration

- **UI:** the Edit form posts back to `/sites/<id>/edit/` and saves (was the §5.1 bug).
- **Partner API:** `PATCH /api/v1/extractors/{slug}` — a thin wrapper over `Site` (NOT a new persistence concept, as recommended): strict allowlist, SiteForm-parity validation (JSON-array `input_urls`, choice `site_type`, URL normalization), 404 cross-tenant, and `field_notes` merging into the stored schema's `fields[].description`.

### 4.2 Delete / archive an extractor

- **Archive (the removal path):** `Site.archived_at` (null = active). UI: archive/unarchive buttons on the detail page, list filter, "Remove from Saved" for jobs. Partner: `/archive` + `/unarchive`, idempotent. Archived sites refuse `site_scrape`/`site_rerun`; a new job on the same URL auto-unarchives (logged). `check_tracker` matches both slash variants of the stored URL (the wave-27a trailing-slash fix).
- **Delete:** superuser-only, POST `confirm=<slug>` required (anything else is a no-op redirect), JS confirm on top. Jobs are untouched (no FK — they join by URL); File-Master artifacts intentionally remain.
- **Partner DELETE: none, deliberately** — the endpoint answers 405 with an explanatory envelope; archive is the removal path.

### 4.3 Per-field instructions

The promoted dialect is exactly what §4.3 of the previous audit recommended: `{name, type?, description?}` end-to-end —

```
schema description / chip ✎ note
  → validate (DESCRIPTION_TOO_LONG warning + truncation at 300; never blocks)
  → ScrapeJob.field_notes {name: instruction}   (≤100 × 300, enforced at create)
  → "### Field guidance" in product_analyzer + code_writer prompts
  → completion: Site.output_schema.fields[].description (merge_field_notes)
  → re-runs copy field_notes; partner PATCH field_notes edits the stored schema
```

Readers: `extract_field_notes()` (`src/schema_validation.py`) is the shared schema→notes reader used by intake + partner create; validate-schema responses expose `fields` so clients can show which fields carry instructions before submitting.

### 4.4 Field order

Order was already preserved through storage; wave-27 made it matter at the output and gave it UI:

- **Output contract:** `prune_record_to_schema(..., order=target_fields)` emits keys in `target_fields` order, remaining (bookkeeping) keys in record order. Both prune paths (nested + flat comprehension) route through it, and a pure reorder now counts as a change worth rewriting the artifact for. Spec'd on `CreateJobRequest.target_fields` and `ScrapedRecord`.
- **UI:** chip ▲/▼ controls splice `fieldsArr` in place; the create/save/re-run POSTs carry the reordered list.

## 5. Defects found while writing the original guide — all fixed in wave-27

1. ~~Edit form posts to the wrong endpoint~~ → `site_form.html` action is conditional on `site.id` (fixed).
2. ~~`site_delete` is dangerous as shipped~~ → superuser-only + typed confirm + UI wiring (fixed).
3. ~~Spec/code drift on `check-site`~~ → sync_api.yaml now describes the shipped metadata-only contract (fixed).

## 6. Pointers

- Live rendered specs on any deployment: `/docs/sync_api`, `/docs/async_api` (`webapp/scraper/urls.py`).
- Source of truth: `webapp/scraper/api/` (partner API — `extractors.py` is the wave-27 extractor resource), `webapp/scraper/views.py` (screen endpoints), `src/schema_validation.py` (schema + description contract), `src/content_types.py` (`prune_record_to_schema` order, `merge_field_notes`), `webapp/scraper/models.py` (`Site.archived_at`, `ScrapeJob.field_notes/parent_job/origin_job`).
- Tests pinning every contract above: `tests/test_wave27_extractor_mgmt.py` (27a), `tests/test_wave27b_instructions_order.py` (27b), `tests/test_wave27c_partner_extractors.py` (27c).
