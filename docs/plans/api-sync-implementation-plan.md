# Partner Sync API (`/api/v1/*`) — Implementation Plan

> **POST-CRITIQUE REVISIONS (2026-08-23, rounds 1+2):** per
> `api-plans-fold.md` and `api-plans-fold-r2.md`. Callback registration
> is the `JobCallback` model (NOT ScrapeJob columns — §2.2, §4.1, §8 step
> 1 updated); create uses atomic + on_commit dispatch + events.emit
> (M12/R2); rate limits ARE in v1 (decision 4 — 429 + Retry-After, spec
> x-rate-limits); recursion-approval jobs FAIL-FAST (M7/R2 — no
> self-resume); the sample hook carries a pass-gate; Phase-enum lock
> requires a data migration. Where this file and the folds disagree,
> THE FOLDS WIN.

Planner A (sync half). Contract: `docs/specs/sync_api.yaml` (OpenAPI 3.1, DRAFT v0.1) —
**the spec wins for behavior**; every spec-vs-code disagreement is flagged in §1.3 and
resolved in §8 (Open questions). All file:line citations verified on branch
`file-master-artifacts` (2026-08-23).

Scope: X-API-Key auth, 9 paths / 10 operations, sample persistence, job-creation
mapping, SSRF validation, testing. Callback **delivery** (HMAC send, retries, event
payloads) is Planner B's — this plan only *stores* `callback_url`/`callback_secret`.

---

## 1. Decisions (with evidence)

### 1.1 D1 — Plain Django views, **no DRF**

Evidence:
- `webapp/requirements.txt` (24 lines) contains no DRF, no drf-spectacular, no
  pydantic. Adding DRF = new dependency + `INSTALLED_APPS` + settings for zero
  leverage.
- The spec's error model is a **custom envelope**, not RFC 7807 problem+json:
  `{code, message, details?}` (`sync_api.yaml:1363-1384`), with endpoint-specific
  `details` payloads (409 `existing_job_id`, 422 `issues[]`, `retry_after_seconds`).
  DRF's exception handler would have to be overridden anyway.
- No content negotiation: every response is `application/json` except one
  `text/x-python` variant on `scraper-code?format=raw` (`sync_api.yaml:1018-1020`).
- Every endpoint is a thin wrapper over existing helpers
  (`_resolve_job_output` views.py:914-966, `validate_user_schema`
  src/schema_validation.py:93, `scraper_code` precedence views.py:499-522) that
  already return plain data — DRF serializers would just re-describe them.
- House style is plain views + `JsonResponse` (e.g. `intake_create_job`
  views.py:2503-2629, `health_api` views.py:2119).

Structure: a sub-package `webapp/scraper/api/` inside the existing `scraper` app
(migrations stay in `scraper`; no new `INSTALLED_APPS` entry).

```
webapp/scraper/api/
  __init__.py
  auth.py          # ApiKey resolution + @api_auth decorator + tenancy helper
  errors.py        # ApiError exception + json_error() envelope
  state.py         # derive_partner_state(), derive_failure_code() — pure functions
  create.py        # create-job request validation + ScrapeJob mapping + SSRF
  ssrf.py          # validate_callback_url()
  output_stream.py # streaming window reader for the paginated output endpoint
  views.py         # the 10 view functions
  urls.py          # path("api/v1/", include(...)) — mounted from scraper/urls.py
```

### 1.2 D2 — Auth: decorator (`@api_auth`), not middleware

Evidence/reasons:
- CsrfViewMiddleware is active (`webapp/config/settings.py:48-58`) and would 403
  key-authenticated POSTs. The per-view fix is `@csrf_exempt` — the spec explicitly
  makes the namespace CSRF-exempt (`sync_api.yaml:228-229`). A middleware cannot
  cleanly grant that exemption (`csrf_exempt` must be an attribute of the view
  callback), so views carry `@csrf_exempt @api_auth` stacked. One decorator, applied
  at the URL layer via a tiny `api_view(fn)` wrapper, keeps call sites 1 line.
- Only `/api/v1/*` needs this; a global middleware would need path-prefix sniffing
  plus careful ordering against `AuthenticationMiddleware` (settings.py:54) and
  `DebugAutoLoginMiddleware` (settings.py:55). A decorator is local and testable.
- `login_required` is **not** used on API views; `request.user` is overwritten by
  the decorator with the key's owner.

Auth outcome table (spec `sync_api.yaml:1186-1194`):

| Condition | HTTP | `code` |
|---|---|---|
| Header missing | 401 | `unauthorized` |
| `sha256(key)` matches no row | 401 | `unauthorized` |
| Key row has `revoked_at` set | 403 | `forbidden` |
| **Owner `is_superuser`** (code-level mandate, spec:1189-1194) | 403 | `forbidden` |
| Owner `is_active == False` | 403 | `forbidden` |
| OK | — | `request.user = key.user; request.api_key = key` |

**Tenancy rule (hard):** API views NEVER call `_get_job` / `_user_jobs`
(views.py:34-46) — those superuser-bypass helpers are exactly the leak the spec's
mandate closes. API code uses its own `_api_get_job` (§4.2) and filters by
`user=request.user` only.

Key format: `sk_live_` + 43 chars `secrets.token_urlsafe(32)` (256-bit).
Lookup = `hashlib.sha256(raw.encode()).hexdigest()` against `ApiKey.key_hash`
(spec: "looked up by SHA-256", `sync_api.yaml:1183`). High-entropy keys make an
unsalted digest acceptable (same trade-off as GitHub tokens).

`last_used_at` update is throttled to one write per key per 5 minutes
(module-level timestamp dict) — polling partners would otherwise write on every GET.

### 1.3 Spec-vs-code conflicts found (spec wins; changes listed)

| # | Conflict | Spec says | Code says | Resolution |
|---|---|---|---|---|
| C1 | **Sample persistence point.** Spec header + §3 say persist "when field_confirmation runs… the records are already in hand" (`sync_api.yaml:676-681`, cites field_confirmation.py:510-527) | Sample records persisted at field_confirmation | **field_confirmation short-circuits for ALL partner jobs.** `"sample_only": not job.full_extraction` (tasks.py:564) and `full_extraction=False` on every intake-style create (views.py:2584) → `if state.get("sample_only", False): … return Command(goto="run_execution")` at field_confirmation.py:239-256 fires **before** the sample-run block (field_confirmation.py:265-331). The block at 288-313 the task cited is dead code for partner jobs. | **Hook moves to `_invoke_code_tester`** in graph.py (§6). Spec text should be amended to cite the code_tester hook (spec-change, docs-only). |
| C2 | **`callback_secret` storage.** "stored hashed (SHA-256) at rest" (`sync_api.yaml:1477-1479`) but deliveries are signed `hmac_sha256("<t>." + body, callback_secret)` (`sync_api.yaml:1480-1483`) | Hashed at rest | **HMAC needs the raw secret at send time** — a SHA-256 digest cannot produce the MAC. Contradiction inside the spec itself. | **Recommend: store raw in a never-returned column** (`ScrapeJob.callback_secret`, CharField, excluded from every serializer; model docstring + test locks "never returned"). Alternative (not v1): add `cryptography` + FERNET key env var. Needs sign-off — see §8 Q1. |
| C3 | `listing_urls` maxItems 50 (`sync_api.yaml:1416`) → newline-joined into `search_criteria` | — | `ScrapeJob.search_criteria = CharField(max_length=500)` (models.py:128). 50 URLs ≫ 500 chars → `DataError` → 500. intake_create_job joins the same way (views.py:2531-2532) but the form textarea rarely exceeds it. | **Migration: `search_criteria` → `TextField`** (like `notes`, models.py:167). Postgres has no length semantics for TEXT; no consumer breaks (string compares only, check_tracker.py:96). |
| C4 | `url` `maxLength: 1000` (`sync_api.yaml:1394`) | — | `ScrapeJob.url = models.URLField()` → `max_length=200` (models.py:119). `product_url` is already 1000 (models.py:120); home view guards >200 (views.py:214). | **Migration: `url` → `URLField(max_length=1000)`.** Matches `product_url`, satisfies spec. |
| C5 | Content-type/input-mode matrix | Spec's own `searchWithSchema` example is `job_posting` + `search_term` (`sync_api.yaml:264-286`) | Registry `input_modes` for `job_posting` = `("url_list", "navigation")` (content_types.py:199) — excludes `search_term`; `serp` = `("search_term",)` only, `forum_thread`/`page_content` = `("url_list",)` (content_types.py:222-258). Enforcing the registry would **400 the spec's own example**. | **Do NOT enforce `ContentTypeConfig.input_modes`.** Enforce only the spec's field-presence rules (§4.1). Registry matrix is stale vs. reality (aya runs search_term job_posting); note for a later registry fix. |
| C6 | Cancel semantics | 200 description "Job cancelled (or was already terminal — idempotent)" **and** 409 `not_cancellable` "already terminal" (`sync_api.yaml:1060-1091`) | — | Reading adopted: **already `cancelled` → 200** (idempotent); `completed`/`failed`/`captcha_blocked`/`akamai_blocked` → **409 `not_cancellable`** with `details.state`. Matches both clauses; locked by test. |
| C7 | `code_review` phase | Spec exposes it and warns "seeded-but-inert … stays pending forever" (`sync_api.yaml:1538`) | Confirmed: PHASE_MAP has no code_review mapping (graph.py:717-731); PIPELINE_PHASES seeds it (tasks.py:199). | No change — spec already documents the warts. Just pass Step rows through in canonical order. |
| C8 | `intake_check_site` leak | Spec: expose only a scope-limited check-site (`sync_api.yaml:148-189`) | Confirmed leak: reads **all users'** `target_fields` for the host (views.py:2331-2337). | New `api_check_site` returns Site-level metadata only (`known_site`, `platform`, `content_type`); never fields, never per-user data. Internal view untouched. |

### 1.4 D3 — Output pagination: streaming window reader + bounded LRU

The spec makes this **normative**: "the view MUST either stream-parse the item array
or cache parsed pages keyed by file key + mtime; `json.load` of the whole file per
request is NOT acceptable" (`sync_api.yaml:806-812`), because full outputs reach
101 MB and full parses have OOM'd 1 GB containers.

Facts that constrain the design:
- `artifacts.read()` downloads the whole body (src/artifacts.py:58-65); `exists()`
  is a cheap HEAD (artifacts.py:82-84); FM's HEAD exposes only `Content-Length`,
  **no mtime/ETag** (file_master/app.py:80-88) → an mtime-keyed cache is not
  directly implementable; key + Content-Length is (files are named
  `output_%Y-%m-%d_%H%M%S.json`, immutable once written).
- No `CACHES` in settings (0 matches) → Django would default to per-process
  LocMem; gunicorn runs 2 workers (Dockerfile:41) → 2 independent caches. Acceptable.
- Holding the parsed aya array (~26,742 records) as Python objects costs several
  hundred MB — that's the OOM the spec forbids.

**Design:** `webapp/scraper/api/output_stream.py`

```python
def read_output_window(fm_key: str, output_key: str, page: int, page_size: int)
    -> dict  # {site, metadata, items, total_items}
```

1. `httpx.stream("GET", artifacts.stream_url(fm_key))` — one sequential pass over
   FM's `/stream/{key}` (file_master/app.py:113-128, 64 KiB chunks).
2. A small structural scanner (`json.JSONDecoder().raw_decode` over a sliding
   buffer) walks the top-level object: captures `site`, locates the member named
   `output_key`, then decodes array elements **one at a time**, materializing only
   indices `[start, start+page_size)`; continues counting to the array end for
   `total_items`; captures `metadata`.
3. Memory O(page) — a 500-record page ≈ ≤2 MB regardless of file size.
4. On top: a module-level `OrderedDict` LRU of **decoded windows** keyed
   `(fm_key, size_from_head, page, page_size)`, max 64 entries (~128 MB ceiling at
   worst-case 2 MB windows; typical pages are a few hundred KB). Interactive paging
   back/forth hits the LRU; a fresh page costs one FM pass (same private network,
   seconds for 101 MB).
5. Defensive guard: if the streamed bytes exceed a hard cap (e.g. 512 MB) the
   scanner aborts with 500 `internal_error` rather than OOM-ing the worker.

`/output/download` streams: `httpx.stream` → `StreamingHttpResponse(gen,
content_type="application/json")` + `Content-Disposition: attachment; filename=...`
(spec:932-936 mandates streaming; the internal `job_output_download` at
views.py:607-626 buffers via `artifacts.read()` — do NOT reuse it).

### 1.5 D4 — `sample_ready` monotonicity

Derive from **"a Step(phase='testing') row has EVER completed"**, i.e.
`Step.completed_at IS NOT NULL` — not current status (`sync_api.yaml:495-501`).
Verified sound: `_notify_phase` sets `completed_at` on every `done`
(graph.py:879) and **never clears it** on a later `running`
(`elif status == "running" and not step.started_at`, graph.py:880-881), and retry
cycles re-fire `testing → running → done` (graph.py:3382/3413). So
`completed_at IS NOT NULL` is exactly the monotone "ever completed" signal. This
invariant gets a dedicated regression test (§7).

Primary sample signal = the per-job file `scrapers/{slug}/samples/sample-{job_id}.json`
written by the new hook (§6). Secondary (spec's fallback #2, exists today):
`Step(testing).completed_at` AND `scrapers/{slug}/analysis/test_report.json`
(published by `_preserve_test_report`, graph.py:586-603 — note this key is
**per-site, latest-job-wins**, not per-job; only used as the state fallback, never
for record contents).

---

## 2. Data model (exact fields)

### 2.1 New model `ApiKey` — `webapp/scraper/models.py` (migration `0033_apikey_...`)

```python
class ApiKey(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_key"
    )
    name = models.CharField(max_length=100, blank=True, default="")   # display label ("Acme prod")
    prefix = models.CharField(max_length=12, db_index=True)            # first 8 chars of raw key, for admin display
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)   # sha256 hex of raw key
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
```

Notes:
- `callback_secret` lives on **ScrapeJob** (spec puts it in `CreateJobRequest`,
  i.e. per-job — `sync_api.yaml:1471-1483`), not on ApiKey.
- Owner must be a **non-superuser** service account; enforced at auth time (§1.2)
  and at issuance in the management command.
- No FK to jobs needed — tenancy flows through `ScrapeJob.user` (models.py:156-159).

### 2.2 `ScrapeJob` additions/changes — same migration

```python
    url = models.URLField(max_length=1000)          # CHANGED: was URLField() (200) — C4
    search_criteria = models.TextField(blank=True, default="")  # CHANGED: was CharField(500) — C3
    # NEW — partner API (M4: provenance gate for the event outbox)
    created_via = models.CharField(max_length=10, choices=[("intake","intake"),("api","api")], default="intake")
```

**REVISED (fold decision 1):** callback registration lives in its own model —
NOT columns on ScrapeJob. Planner B's dispatcher and the PATCH/GET surface share it:

```python
class JobCallback(models.Model):
    job = OneToOneField(ScrapeJob, related_name="callback", on_delete=CASCADE)
    url = models.URLField(max_length=1000)
    secret = models.CharField(max_length=256)   # RAW (fold decision 2), never serialized — test-locked
    status = models.CharField(choices=[("active","active"),("disabled","disabled")], default="active")
    disabled_reason = models.TextField(blank=True, default="")
    last_failure = models.TextField(blank=True, default="")
    delivered_count = models.IntegerField(default=0)
    last_delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

Same migration as ApiKey; indexes: `(status,)` on JobCallback, plus B's
`(status, completed_at)` on ScrapeJob for the reconciler (M2).

Existing fields consumed as-is: `page_type` (stores content_type key, models.py:122),
`input_mode` (models.py:123-127), `target_fields` JSONField (models.py:164), `scope`
/`scope_value` (models.py:165-166), `notes` (167), `schema_text` (172),
`search_url` (173-175), `title` (153), `skip_approvals` (150), `full_extraction`
(146), `user` (156-159), `output_file` (139), `scraper_file` (143),
`product_count` (138), `error_message` (177).

### 2.3 No schema changes needed

`Step` (models.py:212-258), `Site` (models.py:382-422), `SessionLog` (326-356) are
used read-only by the API.

---

## 3. URL layout & mounting

`webapp/scraper/urls.py` (append; no collision with `api/health/` at line 113):

```python
from django.urls import include, path
urlpatterns += [
    path("api/v1/", include("scraper.api.urls")),
]
```

`webapp/scraper/api/urls.py` — 9 paths / 10 operations, names `api_*`:

| Path | Method | View function | operationId |
|---|---|---|---|
| `/api/v1/check-site` | POST | `api_check_site` | checkSite |
| `/api/v1/jobs` | POST | `api_create_job` | createJob |
| `/api/v1/jobs` | GET | `api_list_jobs` | listJobs |
| `/api/v1/jobs/{job_id}` | GET | `api_job_status` | getJobStatus |
| `/api/v1/jobs/{job_id}/sample` | GET | `api_job_sample` | getJobSample |
| `/api/v1/jobs/{job_id}/output` | GET | `api_job_output` | getJobOutput |
| `/api/v1/jobs/{job_id}/output/download` | GET | `api_job_output_download` | downloadJobOutput |
| `/api/v1/jobs/{job_id}/scraper-code` | GET | `api_job_scraper_code` | getJobScraperCode |
| `/api/v1/jobs/{job_id}/cancel` | POST | `api_job_cancel` | cancelJob |
| `/api/v1/validate-schema` | POST | `api_validate_schema` | validateSchema |

All views: `@csrf_exempt` + `@api_auth` via a combined `api_view(method)` wrapper
that also enforces the HTTP method (405 otherwise) and wraps the body in a
try/except that converts `ApiError` → the spec envelope and unexpected exceptions →
500 `internal_error` with a `trace_id` (uuid4, also logged) per
`sync_api.yaml:1287-1297`.

---

## 4. Endpoint-by-endpoint implementation notes

Shared helpers first.

### 4.1 `create.py` — `POST /api/v1/jobs`

**Body → `ScrapeJob` mapping** (spec table at `sync_api.yaml:203-218`; every
intake-form equivalent cites views.py):

| API field | ScrapeJob field | Intake equivalent | Wrinkle |
|---|---|---|---|
| `url` (required, ≤1000) | `url` **and** `product_url` | `url` (views.py:2514) | Same value into both (intake parity, views.py:2573-2574). Must be absolute http(s) — reject otherwise (400). Homepage-ish check (path non-empty) is advisory only; intake_discover_fields rejects homepages (views.py:2422-2426) but create-job does not — keep create permissive, it's a lint not a contract. |
| `input_mode` (required) | `input_mode` | derived from `nav_method` radio via `_INTAKE_NAV_TO_INPUT_MODE` (views.py:2223-2227) | API takes the canonical value directly. Enum `url_list\|list_page\|search_term`; `navigation` rejected (spec InputMode, sync_api.yaml:1323-1332). |
| `content_type` (default `product`) | `page_type` | `content_type` (views.py:2526) | Must be a key in `CONTENT_TYPES` (content_types.py:153-247): `product, article, job_posting, forum_thread, serp, page_content`. Unknown → 400. Registry `input_modes` NOT enforced (C5). |
| `item_urls` (≤10000) | — | `list_urls` newline blob → `_parse_url_lines` (views.py:2592-2603) | Array in, deduped (spec:1408), persisted **directly** to `artifacts.scrapers_key(slug, "input_urls.json")` as `{"urls": [...]}` — byte-identical to intake's write (views.py:2599-2603), bypassing `Site.save`'s shrink-guard (models.py:45-78) exactly as intake does. Required when `input_mode=url_list`; forbidden otherwise. Each entry must be absolute http(s) (400). |
| `listing_urls` (≤50) | `search_criteria` = `"\n".join(listing_urls)` | views.py:2531-2532 | **Server-side join** (spec:214). CharField(500) overflow → migration C3. Required when `input_mode=list_page`; forbidden otherwise. |
| `search_keywords` (≤500) | `search_criteria` | views.py:2534 | Required when `input_mode=search_term`; forbidden otherwise. Note the ≤500 spec cap aligns with the *old* CharField — now moot after C3. |
| `search_url` | `search_url` | views.py:2535 | Optional; only meaningful with `search_term` (ignore silently otherwise — intake does). |
| `target_fields` (≤100) | `target_fields` | comma-split (views.py:2550-2554) | Array in, no comma-splitting. |
| `schema_text` | `schema_text` + derived `target_fields` | views.py:2560-2570 | Validate with `validate_user_schema` (src/schema_validation.py:93). Invalid → **422** `schema_invalid` with `details.issues[]` using `SchemaIssue{code,message,severity,path}` (dataclass at schema_validation.py:66-73 — identical shape to spec `SchemaIssue`). If valid and `target_fields` absent → `target_fields = result.derived_fields` (spec:1439-1442). Enforce `MAX_SCHEMA_BYTES` 256 KiB (schema_validation.py:31) → 400 when exceeded. |
| `scope` (default `all`) | `scope` | views.py:2518 | Enum `all\|firstn\|filter`. Only `firstn` is enforced downstream (`--limit`, run_execution.py:421-432); `filter` advisory (spec Scope). |
| `scope_value` (≤200) | `scope_value` | views.py:2519 | Kept string (spec:1449; models.py:166). If `scope=firstn`, must parse as positive int → else 400. |
| `notes` (≤4000) | `notes` | views.py:2520 | — |
| `title` (≤200) | `title` | set post-hoc in UI | Set at creation (models.py:153 exists). |
| `callback_url` | `JobCallback.url` (created with the job) | none | SSRF gate §5. |
| `callback_secret` (32-256) | `JobCallback.secret` | none | RAW, never serialized (fold decision 2); rotation via PATCH /callback. |

**M12/R2 emit contract (build AFTER B's emitter exists — see fold-r2
sequence):** `with transaction.atomic(): job = create(...); JobCallback.create(...); events.emit(job, "job.created", ...)` then
`transaction.on_commit(lambda: run_scrape_task.delay(job.id))` — dispatch
OUTSIDE the transaction body (on_commit) so the worker never reads an
uncommitted row (Critic-2's M3-recurrence).

Fixed creation flags (spec: "Behavioral parity", sync_api.yaml:220-225):
`full_extraction=False` (views.py:2584), `skip_approvals=True` (views.py:2585),
`user=request.api_key.user`.

**Cross-field validation → 400 `validation_failed`** (`details.fields: [...]`,
spec:310-320):
- `url` missing / not absolute http(s)
- `input_mode` not in enum; `content_type` not in registry
- mode-required array missing, or a mode-inappropriate nav field present
  (spec: "input_mode incompatible with the provided navigation fields")
- `item_urls`/`listing_urls` entries not absolute http(s); maxItems exceeded
- `scope=firstn` with non-integer `scope_value`
- `callback_url` fails SSRF (message names the failed rule)

**Duplicate guard → 409 `duplicate_running_job`** with `details.existing_job_id`
(mirror views.py:2537-2548): same `url` + `user` + `status in (pending, running)`.

**Dispatch**: `run_scrape_task.delay(job.id, rescrape=False)`; store
`celery_task_id` (views.py:2611-2615 identical). Note the task's own same-site
serialization (tasks.py:117-130) requeues on a running sibling — the 409 guard
makes that rare for a single tenant.

**Response 202** with `Location: /api/v1/jobs/{id}` header and `JobCreated` body:
`job_id, state:"inprogress", created_at, status_url, sample_url, output_url,
output_download_url, scraper_code_url` (spec:1485-1507).

**Body parsing**: `json.loads(request.body)`; malformed → 400 `validation_failed`
("request body is not valid JSON"). Size bounded by Django's
`DATA_UPLOAD_MAX_MEMORY_SIZE` (2.5 MB default) — comfortably above worst-case
legal bodies (10k URLs ≈ 1.2 MB).

### 4.2 `state.py` — the 4-state projection (pure; build + test FIRST)

```python
TERMINAL = {"completed", "failed", "cancelled", "captcha_blocked", "akamai_blocked"}

def sample_ready(job, steps) -> bool:
    # primary: per-job sample file exists  (artifacts.exists — cheap HEAD)
    # fallback: any Step(phase="testing").completed_at is not None
    #           AND scrapers/{slug}/analysis/test_report.json exists
def derive_partner_state(job, steps) -> str   # inprogress|sample_ready|scraper_ready|failed
def derive_failure_code(job, steps) -> str | None
```

Table (spec `sync_api.yaml:471-481`, statuses from models.py:99-117):

| `job.status` | condition | state |
|---|---|---|
| `pending` | — | `inprogress` |
| `running` | `sample_ready(...)` | `sample_ready` else `inprogress` |
| `waiting_approval` | — | `inprogress` (budget-escalation pauses set this unconditionally, services.py:413-451 — reachable even for skip-approvals jobs; R2/M7: recursion-error jobs FAIL-FAST instead of waiting — no auto-resume exists) |
| `completed` | — | `scraper_ready` |
| `failed` | — | `failed` |
| `cancelled` | — | `failed` (`failure.code="cancelled"`) |
| `captcha_blocked` | — | `failed` (`captcha_blocked`) |
| `akamai_blocked` | — | `failed` (`akamai_blocked`) |

`derive_failure_code` ladder (FailureCode enum, spec:1349-1360) for
`status=failed`: first **failed** Step in canonical order maps
`code_generation`→`code_generation_failed`, `testing`→`validation_failed`,
`execution`→`execution_failed`, else `no_output_produced` when the job ended with
no resolvable output, else `internal_error`. (Ladder is deterministic and
documented; exact stage attribution is inherently approximate — noted in
Open questions.)

### 4.3 `auth.py`

```python
def resolve_api_key(request) -> ApiKey           # raises ApiError(401/403)
def api_auth(view):                              # decorator: resolve + inject user/api_key
def _api_get_job(request, job_id) -> ScrapeJob   # get_object_or_404(pk, user=request.user) — 404, never 403
def _api_jobs(request) -> QuerySet               # ScrapeJob.objects.filter(user=request.user)
```

Superuser-owner check is **code-level in `resolve_api_key`** — not prose, not a
docs note (spec:1189-1194). Test locks it (§7).

### 4.4 `api_job_status` — `GET /api/v1/jobs/{job_id}`

Builds `JobStatus` (spec:1558-1629):

- `job_id, state (derive_partner_state), internal_status (job.status), url,
  input_mode, content_type (job.page_type), title, site_name, platform,
  scraping_method` — all direct columns (models.py:135-137, 122-127).
- `phases[]`: `_ordered_steps(job)` reused (views.py:142-155) →
  `{phase, status, started_at, completed_at}` (Step models.py:245-252). Canonical
  order via `PIPELINE_PHASES` (tasks.py:193-208); dynamically-created phases sort
  last — matches the existing UI.
- `current_phase`: the step with `status == "running"`; else the failed one on
  `failed`; else `null` when terminal.
- Availability flags — each means "GET returns 200 right now":
  - `sample_available`: per-job sample key exists OR (`sample_ready()` AND
    test_report exists) OR (terminal AND output resolvable → execution-source
    sample).
  - `output_available`: `_resolve_job_output(job)` truthy — reuse
    views.py:914-966 verbatim (it already encodes the authoritative
    `job.output_file`-first, run-window-fallback precedence the spec demands at
    sync_api.yaml:799-805).
  - `scraper_available`: `_resolve_stored_path(job.scraper_file)` OR any
    `scrapers/{slug}/scraper.py` candidate — mirror the precedence in
    `scraper_code` (views.py:499-522) / `_slug_candidates` (views.py:485-496).
- `item_count`: `job.product_count` when terminal else `None` (spec:1606-1608;
  product_count is ground-truth-counted at tasks.py:769-783).
- `output_filename`, `output_download_url`, `scraper_code_url` when resolvable.
- `failure: {code, message}` when `state == failed` (message =
  `job.error_message`, truncated — spec:1556).
- `created_at/started_at/completed_at` ISO-8601 (`.isoformat()` as in
  `job_api`, views.py:1073-1077), `duration_seconds` when both timestamps exist.
- 404 envelope for foreign/missing ids (spec `NotFound`, indistinguishable by
  design).

### 4.5 `api_list_jobs` — `GET /api/v1/jobs`

`_api_jobs(request)` (never `_user_jobs`), newest first
(`ScrapeJob.Meta.ordering`, models.py:182-183 → `["-created_at"]`).

Params: `page` (≥1), `page_size` (1-100, default 20), `state`, `input_mode`,
`created_since` (ISO-8601; unparseable → 400).

`state` filtering strategy (state is derived, not stored):
1. Pre-filter by status class (`inprogress` ⊂ {pending, running,
   waiting_approval}, `failed` ⊂ 4 statuses, `scraper_ready` = completed).
2. For `sample_ready` (a subset of `running`): fetch the running-class rows
   (bounded — active jobs per tenant are few), compute `derive_partner_state`
   per row, filter in Python, paginate in Python. `total_items`/`total_pages`
   computed on the filtered set. Documented approximation cost: one
   `artifacts.exists` HEAD per running row.
3. `input_mode`, `created_since` are plain DB filters.
4. Out-of-range `page` → 422 per spec:903-913.

Rows are `JobSummary` (spec:1631-1659) — same fields minus phases/flags.

### 4.6 `api_job_sample` — `GET /api/v1/jobs/{job_id}/sample`

Never paginated (spec:668-669; ≤5 records).

Resolution order:
1. Per-job sample file `scrapers/{slug}/samples/sample-{job_id}.json` →
   `source: "testing"`, `state` = derived state at fetch time.
2. Else if terminal and `_resolve_job_output` resolves → first
   `FIELD_CONFIRMATION_SAMPLE_COUNT = 5` (field_confirmation.py:23) items from
   the output window reader, `source: "execution"` (spec SampleResponse.source,
   sync_api.yaml:1761-1764).
3. Else → **409 `not_ready`** with `details.state`, `details.current_phase`,
   `details.retry_after_seconds: 60` (spec:758-770; the constant is a floor hint,
   not an estimate).

Body: `{job_id, state, source, output_key, item_count, items,
field_coverage, generated_at}`. `output_key` via
`get_output_key_label(job.page_type)[0]` (src/content_types.py:294-306).
`field_coverage` + `generated_at` are read from the **sample file** (the hook in
§6 embeds them, sourced from `results.field_coverage` of the test report —
verified real-world shape `{field: {count, coverage, status, quality}}` in
scrapers/vistastaff-com/analysis/test_report.json, matching spec:1777-1790;
`quality` passes through as an extra key, which the spec's open
`additionalProperties` permits).

### 4.7 `api_job_output` — `GET /api/v1/jobs/{job_id}/output`

1. `_api_get_job` → 404 envelope.
2. Resolve file: `_resolve_job_output(job)`; `None` + non-terminal → 409
   `not_ready` (`retry_after_seconds: 120`); `None` + terminal → 404 with code
   **`output_not_found`** (spec:876-889 distinguishes from `not_found`).
3. Params `page` (default 1), `page_size` (default 100, max 500). Violations →
   422 `validation_failed` (`details.fields`, `details.total_pages`) per
   spec:903-913.
4. `read_output_window(...)` (§1.5) → `OutputPage` body (spec:1795-1825):
   `job_id, state, output_key, output_filename, total_items, page, page_size,
   total_pages, site (verbatim), metadata (verbatim), items`. Plus
   `X-Total-Count` response header (spec:819-822).

### 4.8 `api_job_output_download` — `GET /api/v1/jobs/{job_id}/output/download`

Same resolution + 404/409 ladder, then `httpx.stream` → `StreamingHttpResponse`
with `Content-Disposition: attachment; filename="{basename}"` (§1.4). Do NOT
reuse `job_output_download` (buffers whole file, views.py:607-626 — the exact
anti-pattern the spec calls out).

### 4.9 `api_job_scraper_code` — `GET /api/v1/jobs/{job_id}/scraper-code`

Precedence copied from `scraper_code` (views.py:499-522):
1. `_resolve_stored_path(job.scraper_file)` (per-job
   `scrapers/{slug}/jobs/scraper-{job_id}.py`, written unconditionally whenever a
   draft exists — `_promote_scraper` graph.py:973-1028) → `source: "per_job"`.
2. Else `scrapers/{slug}/scraper.py` via `_slug_candidates` → `source: "production"`.
3. Else: non-terminal → 409 `not_ready`; terminal → 404.

`format=json` (default): `ScraperCode` body `{job_id, state, filename:
"{slug}_scraper.py", language: "python", strategy: job.scraping_method, source,
code}`. `format=raw`: bare `text/x-python` + attachment header (mirror
views.py:510-512).

### 4.10 `api_job_cancel` — `POST /api/v1/jobs/{job_id}/cancel`

Mirror `job_cancel` (views.py:454-473) minus the redirect: for
`pending|running|waiting_approval` → `status=cancelled`,
`save(update_fields=["status"])`, revoke `run_scrape_task.AsyncResult(
celery_task_id).revoke(terminate=True)` (best-effort, warn on failure). The
post_save hook supersedes open approvals (models.py:539-558). Respond 200 with
the full `JobStatus` body. Already `cancelled` → 200 (idempotent, C6). Other
terminal → 409 `not_cancellable` + `details.state`.

### 4.11 `api_check_site` — `POST /api/v1/check-site`

Body `{url}` (required, absolute http(s) → else 422 per spec:189 — note: this
endpoint's validation failure is **422**, not 400; spec component
`ValidationError`).

Lookup: `slug = _generate_slug(url)` (from `.tasks`, as intake does,
views.py:2308-2311) → `Site.objects.filter(slug=slug).first()`. Response
`{known_site: bool, platform: str|null, content_type: str|null}` where
`content_type` = `site.output_schema.get("content_type")` (same source as
views.py:2323-2324). **Never** return `fields`, `scraping_method` beyond the spec,
or anything per-user (C8). No probe traffic, no writes.

### 4.12 `api_validate_schema` — `POST /api/v1/validate-schema`

Body `{schema_text}` (required, string → else 400, spec:1158-1159). Call
`validate_user_schema(raw)` (src/schema_validation.py:93) and return **200 with
`valid: false` when invalid** (deliberate convention, spec:1108-1110):
`{valid, issues: [{code, message, severity, path}], derived_fields,
detected_content_type}` — the exact mapping already used by
`intake_validate_schema` (views.py:2393-2401). Pure function: no DB writes, no
site traffic.

---

## 5. SSRF validation for `callback_url` (spec:1459-1471)

New `webapp/scraper/api/ssrf.py`:

```python
BLOCKED_HOSTS = frozenset({
    "localhost", "browser_service", "browser-service", "file_master",
    "file-master", "postgres", "redis", "django", "celery-worker",
    "celery_worker", "celery-beat", "flower", "0.0.0.0", "metadata.google.internal",
})
BLOCKED_SUFFIXES = (".railway.internal", ".internal", ".local")

def validate_callback_url(raw: str) -> list[str]:
    """Return a list of violation messages ([] = valid). Pure, no raises."""
```

Rules (each failure → 400 `validation_failed`, `details.fields: ["callback_url"]`):
1. `urlparse` — scheme MUST be `https` (no http).
2. Port MUST be empty (→443), `443`, or `8443`.
3. Hostname lowercased: reject exact membership in `BLOCKED_HOSTS`; reject any
   `BLOCKED_SUFFIXES` suffix.
4. If the hostname is a literal IP → reject if `ipaddress.ip_address(...)` has
   `is_private | is_loopback | is_link_local | is_reserved | is_multicast |
   is_unspecified`. Also reject IPv4-mapped IPv6.
5. DNS resolution: `socket.getaddrinfo(hostname, None)` — **every** returned
   address must pass the same `ipaddress` tests (defeats hostnames that resolve
   private). Resolution failure → reject ("callback_url host does not resolve").
   (Stdlib only; no new dependency.)
6. Length ≤ 1000 (URLField max_length, models.py addition).

Re-run the same validator **at delivery time** is Planner B's half — flag in the
handoff: DNS rebinding between create and send means B should re-validate or pin
the resolved IP.

---

## 6. Sample persistence (the "~15-line backend addition")

**Correction to the original assumption** (conflict C1 — this is the most important
finding): hooking `field_confirmation.py:288-313` would never fire for partner
jobs. `full_extraction=False` → `"sample_only": True` (tasks.py:564) → the
early-return at field_confirmation.py:239-256 jumps straight to `run_execution`
*before* the sample-run block (265-331) and before
`_persist_field_confirmation_sample` (354). The records ARE, however, already on
the celery worker's disk at testing time: code_tester's runs write
`workspace/{slug}/output_*.json`, which `route_after_testing` reads as ground
truth (route_after_testing.py:216-248, "best output file" = max real items across
the last 5 files).

**Hook location**: `webapp/agents/graph.py`, inside `_invoke_code_tester`,
immediately after `_preserve_test_report(slug)` (graph.py:3425). At that point
`_notify_phase(job_id, "code_tester", "done")` has already fired (graph.py:3412) —
i.e. the file exists from the exact moment the state flips to `sample_ready`.

```python
def _persist_partner_sample(slug: str, job_id: int, output_key: str = "products") -> None:
    """Publish the testing-phase records as the partner-API sample artifact.

    Best-effort + failure-safe (mirrors _preserve_test_report): never raises.
    Overwrites on code_tester retry cycles — file existence is monotone, which is
    all the sample_ready derivation needs.
    """
```

Implementation sketch (~35 lines incl. coverage attach):
1. Scan `workspace/{slug}/output_*.json`, pick the file with the most items under
   `output_key` (port the "best file" loop from route_after_testing.py:226-248 —
   newest is the *worst* result there; keep max, not last).
2. Take the first `FIELD_CONFIRMATION_SAMPLE_COUNT` (5) items.
3. Attach `field_coverage` from `workspace/{slug}/test_report.json`
   (`results.field_coverage`) + `generated_at` from its `test_timestamp`.
4. `artifacts.write_json(artifacts.scrapers_key(slug, "samples", f"sample-{job_id}.json"), {...})`:

```json
{
  "job_id": 481,
  "output_key": "products",
  "source": "testing",
  "generated_at": "2026-08-20T19:22:10Z",
  "field_coverage": {"title": {"count": 5, "coverage": "100%", "status": "CORRECT", "quality": "excellent"}},
  "items": [ ...up to 5 records... ]
}
```

Call site:
```python
            update["test_report"] = report
            ...
            _preserve_test_report(slug)
            _persist_partner_sample(slug, job_id, output_key=state.get("content_type_config", {}).get("output_key", "products"))
```

Why this satisfies the spec's intent better than the field_confirmation hook:
fires on every testing completion (retry cycles included), runs in the celery
worker where the workspace files live, and lands before `run_execution` starts —
so `sample_ready` and a fetchable sample appear together.

Backfill note (optional, one-off): for already-completed partner-visible jobs the
execution-source fallback (§4.6 step 2) serves samples from the full output, so no
backfill migration is required.

---

## 7. Testing plan

Pattern: `tests/test_api_docs_views.py` style — standalone pytest file, manual
`django.setup()` after PYTHONPATH insertion, `RequestFactory` + direct view calls,
`db` fixture from pytest-django, manual user create/delete in try/finally.
File-Master mocking: the `sys.modules["src.artifacts"]` stub pattern already used
by `tests/test_f8_f16_output_selection.py:46-54` (module stub + per-test lambdas
for `exists/read/read_json/list_keys/latest_output_key/stream_url`). Celery:
monkeypatch `run_scrape_task.delay` (no broker in unit tests).

`tests/test_partner_api.py` — lock list (roughly in build order):

1. **Auth (D2, the code-level mandate)**
   - no header → 401 `unauthorized`; garbage key → 401
   - revoked key → 403 `forbidden`
   - **superuser-owned key → 403 `forbidden`** (the mandate — spec:1189-1194)
   - inactive owner → 403
   - valid key → `request.user` is the owner; `login_required` never triggers;
     POST succeeds without CSRF token (csrf_exempt verified)
2. **Tenancy** — user A creates a job; user B's key GETs it → **404 (never 403)**;
   list under B does not include A's job; cancel under B → 404.
3. **Error envelope** — every non-2xx response parses as
   `{code: str, message: str}` (+ optional `details`); JSON content type.
4. **State derivation (pure, table-driven)** — each row of the §4.2 table;
   plus the **monotonicity regression**: Step(testing) done → step flipped back to
   `running` (retry cycle re-fire) → state stays `sample_ready` (asserts
   `completed_at` survives, graph.py:879-881 invariant).
5. **Create** — the spec's three examples verbatim (`urlListProduct`,
   `listingPage`, `searchWithSchema`, sync_api.yaml:236-286) → 202 + `Location` +
   correct persisted columns (page_type, input_mode, joined search_criteria,
   input_urls.json written, schema-derived target_fields);
   400s (missing url, bad scheme, mode/field mismatch, `navigation` rejected,
   unknown content_type, 10,001 item_urls); 409 duplicate (with
   `existing_job_id`); 422 schema_invalid with `issues[]`;
   `skip_approvals=True`/`full_extraction=False` asserted on the row.
6. **SSRF matrix** — `http://` ✗; `https://localhost` ✗; `https://10.0.0.1` ✗;
   `https://x.railway.internal` ✗; `https://file-master:443` ✗;
   `:8080` ✗; `:8443` ✓; `https://partner.example.com` ✓ (DNS-mocked);
   unresolvable host ✗.
7. **Sample** — 200 from a stubbed per-job sample file (items ≤ 5, coverage
   passthrough); 409 `not_ready` with `retry_after_seconds`; execution-source
   fallback for a terminal job with output but no sample file.
8. **Output pagination** — window slicing vs a fixture file (correct page slice,
   `total_items`, `X-Total-Count`); 422 `page > total_pages`; 422
   `page_size > 500`; 409 not_ready while running; 404 `output_not_found` on a
   file-less terminal job; stream-reader unit tests on synthetic JSON of varied
   key order/whitespace.
9. **scraper-code** — per-job precedence, then production fallback,
   `source`/`strategy` fields, `format=raw` content-type + disposition.
10. **Cancel** — running → 200 + status=cancelled + revoke called; already
    cancelled → 200; completed → 409 `not_cancellable` (C6 lock).
11. **check-site / validate-schema** — scope-limited payload (assert **no**
    `fields` key — the C8 leak lock); 200-with-`valid:false` convention; 400 on
    missing `schema_text`; parity with `validate_user_schema` on N fixtures.
12. **Graph hook** — `_persist_partner_sample` unit test: picks max-item output
    file, caps at 5, embeds coverage, never raises on missing files; and an
    integration-ish assertion that `_invoke_code_tester` calls it after
    `_preserve_test_report` (mock).
13. **Spec lock (mirrors `TestSpecFilesStructural`)** — assert every path in
    `docs/specs/sync_api.yaml` `paths` has a resolving Django route
    (`reverse("api_...")`), and that the implementation's state/error-code
    vocabulary equals the spec enums (JobState, FailureCode, Error.code) — a
    spec edit or a code drift both fail here.
14. **Model locks** — `callback_secret` never appears in any API response body
    (scan all serialized payloads in tests); ApiKey stores only `key_hash`
    (assert no raw-key column content).

Run command (mirror the header comment in test_api_docs_views.py):
`docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings celery-worker bash -c "cd /app && python -m pytest tests/test_partner_api.py -v"`

---

## 8. Sequencing (build order)

Each step lands green before the next; steps 1-3 are dependency-free foundations.

1. **Migrations** — `ApiKey` + `JobCallback` + `ScrapeJob` changes (url 1000,
   search_criteria TextField, created_via; R2: plus Step phase data-migration
   merging "Browser Navigation" rows into `browser_traverse`, and
   `completed_at` backfill for cancelled/failed jobs with NULL — reconciler
   keys on it). Migration `0033_apikey_jobcallback_job_fields`. Events
   schema (B's EventOutbox) may share this migration or follow as 0034.
2. **API skeleton** — `scraper/api/` package: `errors.py`, `auth.py`,
   `urls.py`, mount in `scraper/urls.py`; `api_view` wrapper (csrf + auth + error
   envelope). Ship with a trivial 404-proof (no endpoints yet).
3. **`state.py` + tests** — pure derivation functions (§4.2) incl. monotonicity
   test. No I/O beyond injectable predicates.
4. **`api_validate_schema` + `api_check_site`** — smallest, no side effects.
5. **`create.py` + `api_create_job`** — mapping table, cross-field validation,
   duplicate 409, dispatch; `ssrf.py` with its matrix test.
6. **`api_job_status` + `api_list_jobs`** — projection + filters/pagination.
7. **Sample hook + `api_job_sample`** — `_persist_partner_sample` in graph.py
   (C1 relocation), then the endpoint.
8. **`output_stream.py` + `api_job_output`** — streaming reader, LRU, 422s.
9. **`api_job_output_download` + `api_job_scraper_code`** — streaming proxy +
   precedence.
10. **`api_job_cancel`**.
11. **Management command** `webapp/scraper/management/commands/create_api_key.py`
    (`--username`, `--name`; creates non-superuser user if absent; prints the raw
    key once; refuses superusers) + `ApiKey` admin registration.
12. **Spec-lock tests + docs** — §7.13-14; amend the spec's field_confirmation
    citations to the code_tester hook (C1) once the hook lands.

---

## 9. Risks / Open questions

**Needs a decision before/during build**

- **Q1 (C2) — `callback_secret` at rest.** Spec self-contradicts (hash-at-rest vs
  HMAC-with-raw). Recommendation: store raw, never return, lock with a test;
  document in the spec. Alternative: `cryptography` + FERNET env key (new dep +
  new secret to manage on Railway). Planner B is blocked on this for delivery.
- **Q2 (C3/C4) — shared-model migrations.** Widening `url` to 1000 and
  `search_criteria` to TextField touches the internal UI's models. Low risk
  (Postgres TEXT is free; `URLField` validators unaffected) but should be
  announced. Home view's >200 guard (views.py:214) can stay (stricter client).
- **Q3 — Failure-code attribution.** The stage→FailureCode ladder (§4.2) is
  heuristic; Step rows don't always carry the failing stage (the finalize ladder
  bulk-closes steps, tasks.py:1054-1063). Acceptable for v1; revisit if partners
  branch on it.
- **Q4 — `state=sample_ready` list filter cost.** One FM HEAD per running job per
  filtered list call. Bounded by the tenant's active-job count; fine for v1. If
  it ever matters, persist a `sample_published_at` column at hook time instead.

**Noted risks (accepted/mitigated in design)**

- **Polling write amplification** — `last_used_at` throttled to 1 write / 5 min /
  key (§1.2).
- **2 gunicorn workers × LocMem LRU** — duplicate caches, ≤2× memory ceiling
  (~128 MB/worker worst case). Redis is present (`REDIS_URL`, settings.py:117) if
  a shared cache is ever needed; not worth it for window caching.
- **Streaming scan is O(file) per uncached page** — a partner walking all 268
  pages of a 101 MB file pays 268 FM passes. LRU covers back/forth; a finalize-time
  precomputed index is the future optimization (not v1).
- **`DebugAutoLoginMiddleware`** (settings.py:55) only fires under
  `DEBUG + DEBUG_AUTO_LOGIN` and targets session auth; the API decorator sets
  `request.user` after middleware regardless — verified no interference, but the
  combination gets one test when `DEBUG_AUTO_LOGIN` is on.
- **`waiting_approval` really is reachable for API jobs** (budget-escalation
  pauses don't check `skip_approvals`, services.py:413-451) — mapped to
  `inprogress` per spec:476; jobs self-resume; no partner action needed.
- **Same-site serialization requeue** (tasks.py:117-130): a second partner job for
  the same URL waits 60s and retries — invisible to the API (the 409 guard usually
  prevents it within one tenant).
- **`test_report.json` is per-site, latest-job-wins** (graph.py:586-603) — used
  only as the *state* fallback, never for record contents; the sample endpoint's
  coverage comes from the per-job sample file, so cross-job bleed is impossible.
- **Rate limits ARE in v1** (human decision 4, 2026-08-23): Redis
  fixed-window per key — 10 req/s burst 30, 60 creates/h, 1 concurrent
  stream/key, ws-token 10/min; 429 + Retry-After. Published in the spec
  (`x-rate-limits` + the RateLimited response). Enforced in `api_view`
  before any handler logic.
