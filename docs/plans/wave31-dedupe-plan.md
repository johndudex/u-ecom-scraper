# Wave-31 — Duplicate-site guard: intake modal + partner API

**Status: PLAN READY — awaiting green light.** Designed by deep design agent, adversarially critiqued (verdict GO-WITH-CHANGES — all changes incorporated). No code written yet.

## Product spec

1. **/intake**: submitting a job for a site that has already been processed returns 409 with a duplicate-site payload; the page shows a modal listing prior job_ids (hyperlinked to `/intake/?job=<id>`) with a **Build anyway** escape hatch (`force=1` re-POST).
2. **Partner API**: `POST /api/v1/jobs` for an already-processed site fails 409 `site_already_processed` unless the body sets `"force": true`. Docs updated (sync_api.yaml, async_api.yaml, extractor-builder-api-guide.md).

## User-confirmed decisions (2026-09-14)

| Decision | Choice |
|---|---|
| Intake scope | **Team-wide** — any teammate's completed job triggers the modal |
| Failed attempts | **Completed only** — prior failures never gate (shown as context: `attempt_count`) |
| API scope | **Global across tenants** — any tenant's completed job refuses; job IDs of other tenants may appear in the 409 payload (accepted trade-off) |

## Design

### Identity: normalized host (not URL, not slug)
Reuse `src/seed_urls.normalize_host` (case-fold + strip one leading `www.`, subdomains distinct) — do NOT add a fourth normalizer (critique D3; existing copies: `views._url_slug`, old `site_host` sketch dropped). Not URL-exact (`Site.url`/`ScrapeJob.url` store sample item pages); not `Site.slug` (collision-prone, and the Site row may not exist at intake time).

### Tiers
- **Tier-1 (exists, unchanged)**: same exact URL + `pending`/`running` + owner-scoped → 409 (intake `views.py:2920-2931`; API `api/writers.py:145-149`). **`force` never bypasses tier-1.** Tier-1 payload gains `"scope": "url"` (informational).
- **Tier-2 (new)**: ≥1 prior job, same normalized host, `status="completed"` → gate. Intake: team-wide query. API: **global** query (no user filter — user decision). Insert after tier-1, before `transaction.atomic()` (`writers.py:152`), pure-read.
- Archived sites (`Site.archived_at` non-null) are **exempt** from tier-2 — an explicit archive is a deliberate "allow re-scrape" signal (critique D6; surfaced for green-light review).

### Query (`webapp/scraper/dedupe.py`, ~60 lines)
`site_processing_history(url)` → `{host, site_slug, processed, has_scraper, attempt_count, prior_jobs[]}`:
- **Empty-host guard first** (critique D4): intake accepts any non-empty string (`views.py:2906`); if `normalize_host` → `""`, return empty history (no gate).
- Prefilter `ScrapeJob.objects.exclude(url="").filter(url__icontains=host).order_by("-id")[:500]`, exact host refine in Python (icontains alone would match `jo.com` inside `jo.com.au`).
- **Perf honesty (critique D2)**: `ScrapeJob.url` is UNINDEXED — this is a full ~27k-row scan per submit, tolerable today (precedent: `intake_check_site` `views.py:2697`). No perf assertion in tests. If it ever matters: `site_host` column written at create, not a gate change.

### Intake UX (fetch-based, modal reuse)
- Intake POST is `postJSON` (`intake.html:2071-2073`); 409 payload is **additive** — keeps `error` so stale JS still renders text.
- New `#dup-modal` copying the tokens-modal skeleton (`intake.html:2517`, close wiring `:2667-2669`). `runBuild` branches on `data.duplicate && data.scope === "site"` before the `data.error` fallback; `forceOnce` flag re-POSTs same FormData + `force=1`.
- **Owner-scoped links (critique D1 — required)**: only the requesting user's own prior jobs are hyperlinked; teammates' jobs render as plain text + `owner_username` (non-owners get 404 from `_get_job` `views.py:35-38`, and the deep-link poller swallows the error → permanent spinner). Detection stays team-wide.
- Payload: `{error, duplicate: true, scope: "site", host, site_slug, has_scraper, attempt_count, prior_jobs: [{job_id, title, status, item_count, created_at, owner_username, job_url, is_own}]}`.
- `intake_check_site` "known site" panel is untouched (informational; no contradiction — critique D9).
- Force parsing (critique D5): `request.POST.get("force") == "1"` (mirror `dagster_enabled` `views.py:2973`).

### API contract
```
409 {"code": "site_already_processed",
     "message": "Site example.com was already scraped (job 481 completed 2026-09-02). Re-send with \"force\": true to create a new job.",
     "details": {"site_slug": "example-com", "prior_job_ids": [481, 455],
                 "latest_job_id": 481, "status_url": "/api/v1/jobs/481"}}
```
Envelope matches `api/errors.py:18-22`. `force` on `CreateJobRequest`: strict `body.get("force") is True` (critique D5 — string `"false"` must not force). Refused creates emit **no** `job.created` event (outbox gates on the row existing, `models.py:229-232`).

### Docs
- `docs/specs/sync_api.yaml`: behavioral-parity paragraph (:424-431), 409 two-example block (:528-546), error-code enum prose (:1756), `force` on `CreateJobRequest` (:1774-1900).
- `docs/specs/async_api.yaml:610`: refused create emits no event.
- `docs/extractor-builder-api-guide.md`: :39, :47, request-body table (:66-92), intake row (:92).
- `tests/test_api_docs_views.py`: spec-drift test (force property + both 409 codes present).

## File inventory
- **New**: `webapp/scraper/dedupe.py`; `webapp/tests/test_intake_site_dedupe.py`.
- **Modified**: `webapp/scraper/views.py` (insert tier-2 after :2931); `webapp/scraper/templates/scraper/intake.html` (runBuild branch, modal markup + close wiring); `webapp/scraper/api/writers.py` (tier-2 after :149, `force` parse near :34); `docs/specs/sync_api.yaml`; `docs/specs/async_api.yaml`; `docs/extractor-builder-api-guide.md`; `tests/test_api_create.py`; `tests/test_api_docs_views.py`.
- **Untouched (by design)**: `check_tracker` resume logic, `job_restart` (`views.py:662`), `site_scrape`/`site_rerun`, legacy `home` form (already gated `views.py:257-266`). **Stated plainly: the gate covers intake + partner-API creates only** — operator paths that write rows directly bypass it deliberately. No models, no migration.

## Test plan (TDD — failing test first; Django tests in `webapp/tests/`, API tests in root `tests/` — TWO test roots; partner fixture is at `tests/test_api_create.py:42`, not :52)

Identity/query: normalize_host semantics (www-fold, subdomain distinct, empty for garbage); completed-prior detected; failed-only → not processed but attempt_count>0; host-variant match (www vs bare, different item URL); substring trap (`jo.com.au` vs `jo.com` → no gate); prior_jobs cap; has_scraper from Site; empty-host guard (D4).

Intake gate: 1) 409 shape (`duplicate/scope/prior_jobs[0].job_id`) + no job row created + nothing dispatched; 2) `force=1` → creates; 3) tier-1 wins with `force=1` present (`scope=="url"`, no prior_jobs); 4) no-prior happy path 200; 5) every `job_url == "/intake/?job=<id>"`; 6) **team-wide** — second user's completed job gates; 7) `force=1` with no prior → still creates (negative).

API gate: 8) 409 code/details (`prior_job_ids`, `status_url`); 9) `force: true` → 202; 10) force does NOT bypass running-guard; 11) **global tenancy per user decision** — prior completed job from a DIFFERENT key DOES refuse (documents the chosen behavior); 12) failed-prior → 202; 13) prior `waiting_approval`/`captcha_blocked` → 202 (the realistic prod park state); 14) omitted flag defaults to refusal; 15) `force: "false"` (string) → refused (strict parse).

Docs: 16) spec-drift test. JS-side (D1 link scoping) asserted via payload shape (`is_own`) only.

## Critique verdict incorporated
GO-WITH-CHANGES → D1 owner-scoped links, D2 perf rationale rewritten + no perf test, D3 reuse `normalize_host`, D4 empty-host guard, D5 strict force parsing, D6 archived exemption, missing negatives (7/13/14/15), fixture line fix. Notes (no action): port-vs-slug divergence in `has_scraper` lookup (self-consistent today), IDN punycode hosts won't fold, concurrent same-host submits can race tier-2 (pre-existing class at tier-1).

## Risks
- **Operator re-drives**: every scripted re-drive of a completed site now needs `force=1` (intake form field / API body flag). The dispatch scripts in /tmp are session-local — habit change only.
- Same host + different intent (new search term on scraped site) → click-through; that's the requested behavior.
- url_list cross-domain lines: identity comes from the sample `url` field only (pre-existing).
