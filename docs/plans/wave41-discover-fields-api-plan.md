# Wave-41 — Field-Discovery Partner API (`POST /api/v1/discover-fields`)

**Status:** DRAFT — awaiting green light
**Date:** 2026-09-24
**Requester:** Extractor Builder team (relayed by John)

> Numbering note: memory had "template Phase-2 extraction-gap" parked as a wave-41
> candidate. This team-facing request takes wave-41; the extraction gap bumps to
> wave-42 candidate.

## The ask (team's words)

> The Extractor Builder needs field-discovery support to show available fields like
> title, price, description, and availability. Currently: POST /api/v1/discover-fields
> is missing from the Swagger documentation and returns 404. POST /api/v1/check-site
> works, but doesn't return the available fields. Could you please add the
> discover-fields endpoint to the Swagger documentation, or include a fields array in
> the check-site response?

## What actually exists today (explored, verified)

- `/api/v1/discover-fields` **does not exist anywhere** — that's why the 404. What
  exists is the **intake UI endpoint** `POST /intake/discover-fields/`
  (`webapp/scraper/views.py:2958`, `@login_required` + AJAX header gate). It is not
  reachable with an API key and was never meant to be public.
- The discovery machinery is real and battle-tested:
  - `intake_discover_fields` (views.py:2958-3053): browser_service `POST /navigate`
    (stealth `cloak`, `return_what: all`, `wait_until: domcontentloaded`, 25s) →
    blocked-detection → html floor (500 chars) → `discover_fields_from_html(...)`.
  - `src/field_discovery.py:219` `discover_fields_from_html(*, url, html, title="",
    llm_timeout=20)` — never raises; one-shot small-LLM → validated field names
    (`^[a-z_][a-z0-9_]*$`) → deterministic JSON-LD fallback (`_fallback_jsonld:182`
    maps `@type` → content-type core fields). Returns
    `{fields: list[str], json_schema: dict|None, source: "llm"|"jsonld"|"none", content_type: str}`.
- Swagger is a **hand-written YAML** (`docs/specs/sync_api.yaml`, OpenAPI 3.1, served
  at `/docs/sync_api`) — no drf-spectacular. Documenting = editing the `paths:` block;
  structural parity is enforced by `tests/test_api_docs_views.py` (`_check` resolves
  every `$ref`; per-endpoint doc tests like `test_ws_token_documented_in_sync:233`).
- Partner-API plumbing to reuse as-is:
  - `@api_view(["POST"])` decorator (`webapp/scraper/api/views.py:23-52`) =
    csrf_exempt + method pin + `X-API-Key` auth (`api/auth.py:28-41`) + rate limit +
    `ApiError` envelope (`{code, message, details}`).
  - SSRF: `webapp/scraper/api/ssrf.py` — public-unicast-only literal IPs, every
    A/AAAA record public, resolver injectable for tests (`_ssrf._resolve` pattern used
    in `writers.py:176-179`). A partner-supplied URL is fetched by browser_service
    from *inside* the network, so this gate is mandatory, not optional.
  - Rate limiter (`api/ratelimit.py`) is prefix-keyed — a second, tighter window for
    this endpoint slots in without touching the shared 10 r/s / burst-30 gate.

## Decisions (made in this plan — flag anything you disagree with)

### D1 — Build `POST /api/v1/discover-fields`; leave `check-site` untouched

The team offered either option, but `check-site`'s fieldless response is a
**documented security invariant**, not an oversight: module docstring
(`api/readers.py:3-6`), spec description (`sync_api.yaml:170-179`), and a pinning
test (`tests/test_partner_api.py:251` asserts `"fields" not in body`). Sites are
global/cross-tenant; `check-site` deliberately reports only non-sensitive platform
metadata. Adding another tenant's extracted-fields history there would be a leak.

The new endpoint has **no cross-tenant surface at all**: it probes the URL the caller
supplies and returns what's on that page — no DB reads. That satisfies the Extractor
Builder's actual need (show available fields for a site) without opening the
check-site invariant.

### D2 — Shared probe core, two thin callers

Extract the navigate→discover body of `intake_discover_fields` into one function and
have both the intake view and the new partner endpoint call it. One code path; the
intake UI's behavior is frozen by its existing tests.

### D3 — Status-code mapping (partner envelope, not intake's 200-always)

Intake returns 200 with inline `error`/`message` strings because it feeds a modal.
The partner API uses `ApiError`. Mapping:

| Case | Status | `code` |
|---|---|---|
| Discovery completed (even 0 fields) | 200 | — `fields: []`, `source: "none"` is honest data |
| Missing/non-JSON body, blank url | 400 | `validation_failed` |
| Non-absolute/bad-scheme URL | 422 | `invalid_url` (parity with check-site) |
| Homepage (no path) — discovery needs a sample item page | 422 | `homepage_url` |
| Private/reserved address (SSRF gate) | 422 | `blocked_host` |
| Target blocks automation (cloak blocked) | 502 | `site_blocked` |
| browser_service unreachable | 503 | `discovery_unavailable` |
| Navigate/read timeout | 504 | `discovery_timeout` |
| Rate limit (global or dedicated) | 429 | `rate_limited` + `Retry-After` |

(Provisional — exact codes settle at T2 review, but the shape is: honest 200 for
"page analyzed, nothing found", real errors for infrastructure.)

### D4 — Dedicated cost guard on top of the shared limiter

One call ≈ one browser slot (browser_service `NAVIGATE_SEMAPHORE=3`) + one small-LLM
call (~20s). That's ~1000× the cost of `check-site`. Guard:
1. Dedicated fixed-window limiter: **6 req/min per key** (429 + `Retry-After`).
2. In-flight lock per key: Redis `SET NX EX 45` → concurrent same-key request gets
   429 `rate_limited` instead of silently stacking LLM calls.
The global 10 r/s gate stays upstream of this, unchanged.

### D5 — Request/response contract

Request: `{"url": "<absolute https(s) URL of a sample item page>"}` — nothing else.
No `page_type` param: `discover_fields_from_html` infers `content_type` itself.

Response 200:
```json
{
  "url": "https://example.com/p/123",
  "fields": ["title", "price", "availability", "currency"],
  "json_schema": {"type": "object", "properties": {"...": {}}},
  "source": "llm",
  "content_type": "product"
}
```
`json_schema` is `null` when `source` is `"none"`. No DB write, no job, no Site
mutation.

### D6 — Swagger = YAML edit + parity test

Add `paths: /api/v1/discover-fields` (operationId `discoverFields`, `security:
ApiKeyAuth`, `tags: [jobs]` like check-site), a `DiscoverFieldsResponse` schema, an
entry in the published `x-rate-limits:` block, and the standard structural test.

## Tasks (TDD — test first, each lands with its tests green)

**T1 — Spec + parity test.** `tests/test_api_docs_views.py::test_discover_fields_documented_in_sync`
(path present, operationId present, ApiKeyAuth security, response `$ref` resolves —
the `_check` helper already validates refs globally). Then
`docs/specs/sync_api.yaml`: path + schema + `x-rate-limits` entry. Red→green.

**T2 — Endpoint skeleton: auth, validation, SSRF.** New module
`webapp/scraper/api/discovery.py`: `@api_view(["POST"]) def discover_fields` with
body parsing, URL shape checks, homepage rule, and the `ssrf.py` gate (resolver
injection for tests). Route in `api/urls.py`. Tests `tests/test_api_discover_fields.py`:
401 (no/revoked key), 400 (non-JSON, missing url), 422 (bad scheme, homepage,
private IP via injected resolver), using the existing `api_request` fixture pattern
from `test_partner_api.py`.

**T3 — Probe wiring behind a seam.** Extract `probe_and_discover(url, *,
navigate_timeout=25, llm_timeout=20) -> dict` (raises typed exceptions for
unreachable/timeout/blocked) hosting the current intake logic. Endpoint calls it;
tests mock `httpx.post` + `discover_fields_from_html` for: llm success, jsonld
passthrough, honest zero (source "none"), blocked → 502, connect error → 503,
read timeout → 504.

**T4 — Dedicated limiter + in-flight lock.** `_check_discovery_rate(key_hash)` (6/min
fixed window, `ratelimit.py` pattern) + `SET NX EX 45` in-flight key. Tests: 7th
call in a minute → 429 with `Retry-After`; held lock → 429; lock released after
success/failure.

**T5 — Intake refactor onto the shared core.** `intake_discover_fields` slims to
request-shape validation + `probe_and_discover` + UI envelope. Behavior frozen:
existing intake tests stay untouched and green.

**T6 — Guide + spec docs.** `docs/extractor-builder-api-guide.md` endpoint inventory
(+curl example), confirm `/docs/sync_api` renders the new path (spec is served
verbatim — no code change).

**T7 — Closeout.** Full suite (`pytest ../tests .` from `/app/webapp`), lint gate
(`docker compose exec -T -w /app django ruff check webapp/ src/`), recreate django
(`docker compose up -d django` — restart doesn't re-read env), hand to user.
Deploy is django-only; browser-service untouched (order rule trivially satisfied).

## Out of scope (explicit)

- No change to `check-site` (D1). If the team later wants static registry fields
  (`content_types` `output_schema`) there, that's a separate decision against the
  documented invariant.
- No caching of results (ProbeCache stays unused here) — D4 bounds the cost first;
  caching by host+page_type is a cheap follow-up if traffic justifies it.
- No async/queued discovery — sync ≤ ~45s bounded by navigate 25s + LLM 20s.
- No intake UI changes.

## Risks

- **Sync view ties a Django worker ~45s/call.** Mitigated by D4 (6/min/key, 1
  concurrent/key). Worst case a burst of distinct keys queues on
  `NAVIGATE_SEMAPHORE=3` and late callers get 504 — honest, bounded.
- **LLM cost per call.** One small-model call per request; the dedicated limiter is
  the budget control.
- **SSRF**: partner URL is fetched from inside the network → `ssrf.py` gate is
  mandatory in T2 before any probe wiring exists (T2 lands before T3 by design).

## Verification gates

1. `tests/test_api_discover_fields.py` all green; existing
   `test_partner_api.py::test_check_site_known` (the no-fields pin) still green.
2. Spec parity suite green (`test_api_docs_views.py`).
3. Full suite at baseline (known reds only) + lint clean.
4. Optional live smoke (user's click): one curl with a real key against any sample
   product URL — I don't hand-probe target sites myself.
