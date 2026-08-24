# Partner API Plans — Round-2 Adversarial Critique (Critic 3: the AMENDED SPECS + SECURITY)

**Scope:** what the fold (`api-plans-fold.md`, commit `1b22914`) turned the specs INTO,
not what they were. Every claim below was verified against `docs/specs/sync_api.yaml`
and `docs/specs/async_api.yaml` at commit `1b22914` (working tree identical —
`git diff 1b22914 -- docs/specs/ tests/` is empty), the two plan files, the fold, the
test file (re-run in-container), and the vendored AsyncAPI 3.1.6 parser bundle.

**VERDICT: NO-GO** — narrowly. The mechanical work is genuinely good: both specs parse,
all 222 internal `$ref`s resolve, the amended schemas use correct OpenAPI 3.1 typing,
and the AsyncAPI additions are valid 3.0 vocabulary. But the fold's GO rests on three
claims that are false or half-landed, and the amendment **moved the round-1 blockers
from the plans into the specs**: the token endpoint both async auth paths depend on is
defined by *neither* spec, the two specs now define the same-named `CallbackStatus`
schema differently while each claims to mirror the other, and the sync spec carries
both secret-storage policies for the same secret. All three are small, surgical edits —
this is a "fix and go", not a redesign.

---

## 1. MECHANICAL VALIDITY of the amendments

### 1.1 What checked out (verified, no action)

| Check | Result |
|---|---|
| `yaml.safe_load` both specs | PASS (`sync`: openapi/jsonSchemaDialect/info/servers/security/tags/paths/components; `async`: asyncapi/info/defaultContentType/servers/channels/operations/components) |
| Internal `$ref` resolution | PASS — **112 refs in sync, 110 in async, 0 unresolved, 0 external** |
| `nullable:` (3.0-ism) | **Zero occurrences in either spec** (only prose at `async_api.yaml:104` describing a Django column). The amendments correctly use `type: [string, "null"]` (`sync:1685,1688,1700`) and `type: ["null", object]` (`sync:1779`) |
| Other 3.0-isms | None — no boolean `exclusiveMinimum/Maximum`, no non-string `type` array members |
| Retry-block comment | Parses; `x-retry` loads as a clean mapping with `attempts: 6` and the 5-value backoff list |
| `GET`/`PATCH /callback` path block | Structurally valid OpenAPI 3.1: path-level `parameters` + `$ref jobId`, both operations carry `operationId`, `requestBody` on PATCH, response maps with `$ref`'d components |
| `CallbackUpdate` | Valid; `writeOnly: true` on a request-only schema is decorative but harmless |
| `JobStatus.callback` | Correct 3.1 null-typing; correctly absent from `required` |

### 1.2 MEDIUM — GET /callback's documented "null" body is unschemaable

`sync_api.yaml:1077` says "Absent registration → `callback: null`" and the 200
description (`:1080`) says "Current callback registration (or null)" — but the response
schema is a bare `$ref: CallbackStatus` (`:1082-1084`), i.e. `type: object`. **A JSON
`null` body does not validate against `type: object` in JSON Schema 2020-12.** A client
generated from this spec rejects the exact response the endpoint documents, and there is
no way to distinguish "no registration" (null) from "job not found" (404) in the
contract. The fix was known and applied 90 lines earlier — `JobStatus.callback` uses
`type: ["null", object]` (`sync:1779`) — it just wasn't applied at the response site:

```yaml
        "200":
          description: Current callback registration (or null).   # sync:1080
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/CallbackStatus"       # sync:1084 — cannot match null
```

**Fix:** `oneOf: [{$ref: CallbackStatus}, {type: "null"}]` (or drop null and return
404 / an `{callback: null}` envelope object).

### 1.3 MEDIUM — the four new error codes are not in the Error model's code list

`sync_api.yaml:1483` enumerates the stable codes
(`unauthorized, forbidden, not_found, not_ready, duplicate_running_job, validation_failed,
schema_invalid, output_not_found, not_cancellable, internal_error`). The amendments
introduced **four codes absent from that list**:
`invalid_page` (`sync:462`), `invalid_created_since` (`sync:455`),
`callback_already_active` (`sync:1140`), `invalid_callback_url` (`sync:1149`).
Plan A's spec-lock test #13 (`api-sync-implementation-plan.md:729`) asserts "the
implementation's state/error-code vocabulary equals the spec enums" — the vocabulary it
would lock against is now stale by exactly the codes the fold added. Register them.

### 1.4 MINOR — listJobs 422 description misstates the /output bounds

`sync:453-454`: "same bounds as /output pagination: page >= 1, **1 <= page_size <= 100**".
`/output` uses `pageSizeLarge`, max **500** (`sync:1340`). The sentence conflates the two
parameter objects; a partner reading it will conclude `/output` caps at 100. Say
"same rule shape as /output, with this endpoint's page_size max of 100."

### 1.5 NIT — unbalanced quote at `async_api.yaml:455`

`This corrects an earlier draft that said "hashed at rest.` — missing closing quote.
Inside a block scalar, so it parses; it is still a typo in the most
security-sensitive paragraph in the document.

---

## 2. CROSS-SPEC PARITY after amendment

### 2.1 BLOCKER — `POST /api/v1/ws-token` is defined by NEITHER spec (M13's fix is incomplete)

This is the check the round-2 brief flagged, and it fails.

- `async_api.yaml:55` specifies the endpoint: `POST /api/v1/ws-token (header: X-API-Key) -> 201 {token, expires_in: 300, connect_url: ...}`
- `async_api.yaml:62-63` disclaims ownership: *"This exchange endpoint belongs to the
  sibling OpenAPI spec; it is documented here because it is load-bearing for the WS server."*
- `async_api.yaml:152-153` (the **new** M13 text) makes the SSE browser flow depend on it
  too: "Browser clients authenticate with `GET /api/v1/jobs/{job_id}/events?token=...`
  using the SAME ws-token exchange"
- `async_api.yaml:499` (the **new** `eventTokenQuery` scheme): "Single-use 300s token
  from **POST /api/v1/ws-token**"

**The sync spec contains zero occurrences of `ws-token`** (grep: 0 hits across the whole
file). Its path inventory is 10 paths / 12 operations and none of them is a token
exchange. Plan A is the assigned route owner (plan B §7: "route mechanics = Planner A",
`api-async-implementation-plan.md:200`) and **plan A also has zero occurrences** — its
scope line still reads "9 paths / 10 operations" (`api-sync-implementation-plan.md:8`) and
its route table (`:257-266`) lists 10 operations with no token endpoint and no callback
GET/PATCH.

**Failure scenario:** the fold's stated bar is the spec-driven client. Such a client
reads `eventTokenQuery`'s description, is told the token comes from
`POST /api/v1/ws-token`, looks for that path in the OpenAPI document that owns HTTP
endpoints, and finds nothing. The M13 browser story — the thing the fold marked
"(done)" — has a consumer in the async spec and no producer in any spec. Same for the
WSS Phase-2 path. A one-path addition to `sync_api.yaml` (plus plan A's route table)
closes it; without it, B13/M13 is not resolved, it is *relocated*.

### 2.2 BLOCKER — the two specs now define `CallbackStatus` differently, while each claims to mirror the other

Round-1 B2 verdicted NO-GO on "contradictory data models ... for the same callback
concept." The fold's B3 resolution added a `CallbackStatus` to the sync spec — and the
two same-named schemas disagree at the field level:

| | `sync_api.yaml:1674-1701` | `async_api.yaml:745-756` |
|---|---|---|
| `required` | `[status, url, created_at]` | `[url, status]` |
| `last_failure` | **string \| null** — "Most recent delivery error summary (truncated)" | **`type: string, format: date-time`** — a timestamp |
| `disabled_reason` | `[string, "null"]` | `string` (not nullable — but a fresh active callback has no reason) |
| `delivered_count` / `pending_count` / `last_delivered_at` | present | absent |

`last_failure` is a **date-time in one spec and an error message in the other**, under
the same name, in schemas that each point at the same thing: async's description
(`:748-750`) says "State of the submit-time callback, echoed in **job.created and
readable via the sync API**" — i.e. it represents itself as the sync API's shape. And
this is not a dead corner: `data.callback` on `job.created` (`async:594-595`, example at
`:975-977`) uses the async shape, so **every partner job fires this object on its first
event**, and the same partner polls `JobStatus.callback` / `GET /callback` in the sync
shape. One verifier cannot serve both. Align them (one `$ref`-able source of truth or
identical definitions), and decide whether `last_failure` is a time or a string.

### 2.3 MAJOR — M6's terminality fix landed in sync only; async still calls `scraper_ready` "the terminal success state"

The fold claims "M6 → **both**: sync spec amended (done)". Verified:
- sync: DONE — the RARE EXCEPTION paragraph (`sync:1423-1428`) says `failed` may
  supersede `scraper_ready` and clients "must not stop listening/polling at
  `scraper_ready`."
- async: NOT DONE — `async:96-97` still reads "`scraper_ready` is **the terminal success
  state**", with no supersession caveat. `async:69-70` does allow `failed` "from any
  state", but nothing tells the event consumer — the party the caveat exists for, since
  only a listener can hear a `job.failed` that arrives *after* `job.scraper_ready` —
  that they must keep the subscription open past `scraper_ready`.

The two specs now tell different stories about the same state. One sentence in
`async:96-97` ("terminal success *unless superseded by `job.failed`* — keep listening
until `failed` or stream close") closes it.

### 2.4 MAJOR — the sync spec contains BOTH secret-storage policies (the fold fixed the async copy and left its own)

This is the C2 contradiction the round-1 critique proved, still present after the fold
claimed it corrected:

- `sync_api.yaml:1594-1595` (`CreateJobRequest.callback_secret`, pre-existing): *"Supplied
  ONCE at registration; **stored hashed (SHA-256) at rest** and NEVER returned by any
  endpoint."*
- `sync_api.yaml:1596-1599` (same field, three lines later): the HMAC formula
  `hmac_sha256("<t>." + raw_body, callback_secret)` — which **requires the raw secret**.
- `sync_api.yaml:1720` (`CallbackUpdate.callback_secret`, the amendment): *"**Stored raw
  at column level**, never returned by any endpoint, log, or admin view."*
- `async_api.yaml:451-455`: "the secret **CANNOT be stored hashed**".

So the sync spec now says hashed at create and raw at rotate, for the same secret, while
its own signature formula proves hashed is impossible — and human decision 2 chose raw.
The fold's B2 note "Async spec's 'stored hashed at rest' erratum corrected" is true of
the async file and false of the sync file's own copy at `:1594`. A partner reading the
create-time field to decide whether to send a high-entropy secret is told it will be
hashed.

**Same field, inconsistent bounds:** create allows `minLength 32 / maxLength 128`
(`sync:1590-1591`); rotate allows `minLength 32 / maxLength 256` (`:1717-1718`). A
200-char secret is legal via rotate and illegal via create. Pick one (128 or 256) for
both.

### 2.5 Verified OK (parity checks that passed)

- **The 4-state tables still match** — sync's derivation table (`sync:484-494`) and
  async's projection table (`async:75-82`) map the same 8 internal statuses to the same
  4 states, with the M6 caveat divergence noted in 2.3 above.
- **The disable/re-enable pointer is now resolvable**: async `:437` still says "PATCH the
  job's callback" without naming the path, but the amended x-retry comment (`:482-483`)
  says "see the PATCH re-enable endpoint in the sibling sync spec" and the endpoint now
  actually exists (`sync:1093`). Vague pointer, real target — acceptable, though naming
  `/api/v1/jobs/{job_id}/callback` in async `:437` would close it entirely.
- **`JobStatus.callback` tenant-scoping is sound** (see §3.6) — no cross-tenant oracle.
- Pre-existing (not an amendment, unlisted in round 1, flagging for completeness):
  **`input_mode` vocabulary fork** — async `JobCreatedData.input_mode` enum includes
  `navigation` (`async:592`); sync's `InputMode` deliberately excludes it (`sync:1439`,
  `:1445-1446`). A partner cannot round-trip the async enum through sync. Harmless today
  (no intake path produces `navigation`) but it violates async's own "vocabulary LOCKED
  to the sync spec" header (`async:6-8`). MINOR.

---

## 3. SECURITY REVIEW of the new surfaces

### 3.1 MAJOR — PATCH `reenable` has no cooldown, no cap, and an ambiguous blast radius (outbox churn loop)

The spec (`sync:1098-1102`) says `{"action":"reenable"}` "resets the delivery attempt
counter and clears `disabled_reason` so the next dispatcher sweep resumes delivery of
PENDING outbox rows." Three things are unspecified, and together they make a cheap
churn attack / self-DoS:

1. **Whose counter?** Retries are per-*event* (each outbox row carries
   `next_attempt_at`/attempt state — plan B §3, `api-async-implementation-plan.md:99-110`),
   but the PATCH text says "the delivery attempt counter" (singular, per-callback).
   Does one PATCH re-arm **N exhausted events** back to pending? If yes:
2. **The prune never collects them.** B's prune deletes only `delivered`/`exhausted` rows
   older than 30d (`api-async-implementation-plan.md:122`). A re-arm cycle
   (`exhausted → pending`) resets the clock on rows that would otherwise age out —
   a partner who re-enables on a loop keeps an unbounded, never-pruned outbox for a
   dead endpoint.
3. **No frequency bound.** Nothing in the spec limits re-enables. Even the rate limits
   the fold promised (10 req/s/key — §3.4, and they are not in the spec anyway) would
   not bound the blast radius: *one* PATCH re-arms the whole outbox, and each cycle
   costs `N_events × 6 attempts × 20s` (10s connect + 10s read, `async:486`) of
   events-worker time. A finished job leaves 30-60 outbox rows (round-1 B5 arithmetic,
   unchanged); a partner with 5 finished jobs and a cron'd re-enable parks ~`300 events
   × 20s` of delivery attempts per cycle against an endpoint that never answers.

**Decide and write down:** (a) re-enable resets per-event counters or only the callback
row's status; (b) a cooldown (e.g. re-enable at most once per N minutes, 429 otherwise)
or a hard cap on lifetime re-enables per job; (c) whether re-armed rows restart or
resume their 30d prune clock. Currently a partner can keep the outbox churning forever
on a endpoint that has been dead for a year.

### 3.2 MEDIUM — `rotate` has no in-flight/invalidation semantics (signature-mismatch window)

The spec is silent on every question a rotating partner will ask:
- **When does the old secret stop signing?** Deliveries already dequeued and mid-POST
  when the PATCH lands are signed with the old secret. The partner — who rotated
  *because* they believe the old secret is compromised — rejects them. Do those
  rejections burn the *event's* retry attempts (pushing it toward exhaustion and
  callback-disable) or are they retried under the new secret?
- **Is there a dual-validity grace window** (Stripe-style old+new for N minutes) or
  hard cutover? Hard cutover with in-flight retries means every rotation deterministically
  drops some deliveries into the retry ladder for a non-security reason.
- **No confirmation of which secret signs next.** The 200 returns `CallbackStatus`
  (`sync:1121-1126`), which contains no secret version/epoch — a partner cannot tell
  from the response when cutover happened, so they cannot bound their own accept-window.

One paragraph on the PATCH (`:1093`) fixes this: "rotation is a hard cutover at PATCH
time; in-flight deliveries signed with the previous secret will fail verification and
are retried under the new secret without consuming retry attempts" — or whatever is
actually intended.

### 3.3 MEDIUM — SSE `?token=`: the reconnect story is undefined, and single-use + EventSource auto-reconnect is a trap

The amendment (`async:150-154`) adds the browser auth path and stops there. What is not
specified:

- **The reconnect failure mode.** `EventSource` auto-reconnects on a mid-stream drop by
  re-requesting the *same URL with the same token*. The token is single-use
  (`GETDEL`-consumed on first connect). So: network blip → reconnect → token already
  consumed → server rejects. **The async spec defines no 401/403 behavior at all** —
  grep for `401|403` across `async_api.yaml` returns nothing. Per the WHATWG spec a
  non-200 on connect fails the connection permanently (readyState CLOSED, no retry), so
  the honest behavior is "one drop = dead stream", which means the promised browser story
  works exactly once per page load. That may be acceptable — but it must be written
  down, because the spec's own bridge notes say the opposite in spirit:
- **The documented reconnect strategy requires the nonexistent endpoint.**
  `async:163-165`: "Reconnect strategy: **re-GET the stream**, then reconcile via the
  sync job endpoint." Re-GETting requires a *fresh* token, which requires
  `POST /api/v1/ws-token` — which no spec defines (§2.1). The reconnect instruction is
  unimplementable as documented.
- **No re-mint policy.** How many tokens may a partner mint per job per minute? Can a
  token be minted for a job that is already terminal? Unspecified, and the fold's
  "1 stream/key" limit is not in the spec either (§3.4).

**Severity call:** MEDIUM as a spec gap, and it compounds the §2.1 blocker — M13's
"browser story documented" claim covers the happy path only.

### 3.4 MAJOR — the promised RateLimits section and 429 do not exist in the spec (fold-integrity)

Fold M11 (fold:92-94) records human decision 4 and states: *"spec gains a RateLimits
section + 429 in the error model."* Verified against `sync_api.yaml`:
- **No `429` anywhere** in either spec.
- **No RateLimits section** — no tag, no component, no prose.
- **No `Retry-After`** anywhere.
- The only "rate limit" text in the sync spec is `rate_limit_delay` (`sync:864`,
  `:1927`) — the scraper's polite-crawl delay in output metadata, unrelated.

The fold's implementation sequence includes "rate limiting" as a Phase-1a step
(fold:141), so the code will emit 429s **the spec never promises** — which is precisely
the failure class the fold itself fixed for M14 ("A invents response codes the spec does
not define"). Decision 4 was one of four human decisions; its spec half is simply not
there. This is a done-claim that is not in the file.

### 3.5 MINOR — the token-in-URL access-log caveat was not carried to the SSE text

`async:59-61` states the risk for WSS: "Query-param tokens can leak into access logs —
the 5-minute TTL and single-use nonce bound that exposure." The new SSE text
(`:150-154`) carries **no such caveat**, on a transport (HTTPS GET to Django/gunicorn
behind a Railway proxy) where access logging is at least as likely — and the SSE URL
additionally embeds the **job id** (`/api/v1/jobs/{job_id}/events?token=...`), so a
logged line discloses both the resource and the credential. Copy the caveat sentence.

### 3.6 Checked out — `JobStatus.callback` and the delivery counters are tenant-safe

`pending_count` / `delivered_count` live on `CallbackStatus`, reachable only through
`GET /api/v1/jobs/{job_id}` and `GET .../callback`, both scoped by the `jobId` parameter
plus the standard cross-tenant-404 rule (`sync:1362-1366`, `:1089-1091`). The counters
are per-job, the job is owned by the key's user, and `JobStatus.callback` is absent for
other tenants' jobs by construction (they 404 first). No cross-tenant oracle, no cheap
enumeration primitive. **No finding.**

### 3.7 Checked out — PATCH auth inheritance

The new path block declares no per-operation `security`, so it inherits the document
root `ApiKeyAuth` (`sync:128-129`) — same decorator, same tenancy helper, same 401/403
responses as every other operation. Correct.

---

## 4. FOLD-INTEGRITY AUDIT (claim → verified?)

Test claim re-run in-container:
`docker compose exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings celery-worker bash -c "cd /app && python -m pytest tests/test_api_docs_views.py -q"`
→ **14 passed** in 6.41s. The fold's "14/14" and "tests pass" claims are accurate.

### 4.1 Claims marked done IN THE SPEC

| Fold claim | Where | Verified |
|---|---|---|
| B3: `GET + PATCH /api/v1/jobs/{job_id}/callback` + CallbackStatus/CallbackUpdate + callback on JobStatus | fold:41-45 | **YES** — `sync:1065-1152`, `:1674-1720`, `:1778-1793`; 409/422 present |
| B3: "PENDING rows queue while disabled" | fold:44 | **YES** — `sync:1100-1102`, `:1695` |
| B2: async "stored hashed" erratum corrected to raw | fold:37-39 | **YES in async** (`async:451-455`); **NO in sync** — `sync:1594` still says hashed (§2.4) |
| M1: attempts 5→6 | fold:62-64 | **HALF** — `x-retry.attempts: 6` (`async:484`) yes; **prose at `async:424` still says "Retries: 5 attempts"** in the same operation's description. The amendment reintroduced/left the exact ambiguity M1/D10 existed to kill |
| M1: minimum-delay semantics + 30s sweep + ≥1m self-scheduled | fold:62-64 | **YES** — `async:478-483` |
| M6: sync scraper_ready-supersession caveat | fold:74-77 | **YES** — `sync:1423-1428`. **NO on the async half** ("both" claimed; `async:96-97` unchanged — §2.3) |
| M13: SSE `?token=` | fold:97-98 | **HALF** — the query-param auth text is in (`async:150-154`, scheme at `:495-499`), but the ws-token exchange endpoint exists in no spec (§2.1), the operation-level security omits the token scheme (§5.2), and "browser story documented" covers no reconnect behavior (§3.3) |
| M14: listJobs 422 (invalid_page/invalid_page_size/invalid_created_since) | fold:99-100 | **YES** — `sync:451-463`; all three named at `:452-456` |
| m2: per-item maxLength 1000 on item_urls + listing_urls | fold:109 | **YES** — `sync:1521`, `:1531` |
| m3: sample MUTABILITY paragraph | fold:110 | **YES** — `sync:678-682` |

### 4.2 Claims marked done/assigned to PLAN FILES — the failure cluster

The commit message states "Both plan files carry POST-CRITIQUE REVISION banners."
**Plan A carries no banner** (grep `POST-CRITIQUE` in `api-sync-implementation-plan.md`: 0
hits). Only plan B has one (`api-async-implementation-plan.md:3-9`, "THE FOLD WINS").
Everything below follows from that: plan A was partially patched (§2.2's JobCallback
revision at `:209-226`) and left stale everywhere else.

| Fold claim | What the file actually says | Verified |
|---|---|---|
| **B4** "delivery-time validation is **now in B's plan §4**" (fold:47-51) — a *blocker* resolution stated as landed | grep `ssrf\|ipaddress\|follow_redirects\|re-resolve\|re-validate` across `api-async-implementation-plan.md`: **0 hits**. B §4 (`:145-160`) contains no re-validation, no IP pinning, no redirect policy. The SSRF rebinding amplifier ships unchanged | **NO — false claim on a blocker** |
| **M11** "spec gains a RateLimits section + 429 in the error model" (fold:92-94, human decision 4) | No 429 / RateLimits / Retry-After anywhere (§3.4) | **NO** |
| **m6** "named in the sync spec's security notes — A adds the sentence" (fold:115-116) | Sync spec has **no security-notes section** and no such sentence (grep `oracle\|conscious\|cross-tenant` beyond the 404 note: nothing); plan A has no such sentence either | **NO — and the named target section doesn't exist** |
| **B1** "A's test #12 extended with a partner-shaped `sample_only=True` job asserting NO sample artifact is written at field_confirmation" (fold:29-32) | Test 12 (`api-sync-implementation-plan.md:723-727`) is unchanged: `_persist_partner_sample` unit test + mock call-assertion. No `sample_only` job, no field_confirmation lock | **NO** |
| **M7** "A's §4.2/§9 corrected" (fold:78-80) | Plan A `:353` and `:770` still say the job "self-resumes" / "jobs self-resume" — the exact assertion M7 falsified | **NO** |
| **M8** "Spec-lock test gains Phase-enum comparison" (fold:81-84) | Test 13 (`:729`) still locks only "state/error-code vocabulary" | **NO** |
| **M12** "B's contract written into A's plan" (fold:95-96) | grep `transaction.atomic\|events.emit\|on_commit` in plan A: **0 hits** | **NO** |
| **M10** "finalize-time page index replaces the streaming window reader" (fold:88-91) | Plan A `:762-763` still lists the page index as "the future optimization (not v1)" | **NO** |
| **M5** "Prune loop condition gains `and job.created_via != 'api'`" (fold:71-73) | The prune exists (`webapp/scraper/tasks.py:862-881`, keep-newest-5); neither plan contains the exemption | **NO** |
| **B5** dedicated `events` queue (fold:53-56) | grep queue-routing in plan B: only the beat sweeper and `deliver_callback.s()` — no `--queues=events`, no compose entry | **NO (assigned only, not folded into B)** |
| **M15** test extensions (fold:101-103) | B §9.8 (`:263` region) still drives `field_confirmation → cleanup` with the old sample assertion; no dispatch-overlap, cross-tenant SSE 404, or outbox-growth tests in either plan | **NO** |
| **M1** "legs ≥1m self-scheduled via `apply_async(countdown=…)`" (fold:62) | Plan B `:153` still **rejects** it: "do **not** use `autoretry_for`/`retry_backoff` … **the beat sweeper is the retry driver**" — plan B now contradicts the amended spec (`async:480-482`) with no banner covering §4 | **CONTRADICTED** |
| **M2** reconciler keys on `completed_at` + index (fold:65-66) | Plan B `:80` still says "terminal-status jobs **updated since** last sweep" — the nonexistent `updated_at` column M2's whole finding was about | **NO (assigned only)** |
| Plan A's operation inventory | `:8` "9 paths / 10 operations"; route table `:257-266` lists 10 — the spec now has **10 paths / 12 operations** (callback GET/PATCH added). Plan A is stale by exactly the amendment | **STALE** |
| Plan A internal contradiction | `:209` "callback registration lives in its own model — **NOT columns on ScrapeJob**" vs `:710` build step 1 "Migrations — ApiKey + ScrapeJob changes (… **callback_url/callback_secret**)" — both in the same file | **CONTRADICTORY** |

**Pattern, stated plainly:** every claim whose landing site was a *spec file* is real or
half-real; nearly every claim whose landing site was *plan A* or the *security/ops
content of plan B* is not in the file — including one blocker (B4) stated as
"now in B's plan §4" and one human decision (M11) whose spec half is absent. The fold
reads as if the plan files were revised to the same depth as the specs. They were not.

---

## 5. ASYNC-API-3.0 CONFORMANCE of the amendments

Verified structurally against the AsyncAPI 3.0 JSON Schemas embedded in the vendored
`docs/assets/asyncapi-web-component.js` (3.1.6 bundle — the same parser that round 0
proved renders the doc, and the same class of validator that previously rejected
`parameters.schema`).

### 5.1 `eventTokenQuery: type: httpApiKey, in: query` — VALID

The bundle's `APIKeyHTTPSecurityScheme` definition requires `{type, name, in}` with
`type enum: ["httpApiKey"]` and `in enum: ["header", "query", "cookie"]`. The amendment
(`async:494-499`) supplies `type: httpApiKey`, `in: query`, `name: token` — all three
required keys, all values in-enum. `httpApiKey` is AsyncAPI 3.0 vocabulary (shared with
OpenAPI), not an OpenAPI-only type. **The scheme will not be rejected.**

### 5.2 `security:` as a list of two bare `$ref`s — VALID shape, correct OR semantics, but inconsistently applied

- The 3.0 schema (`securityRequirements.json`, confirmed in the bundle) defines
  `security` as `array of oneOf [Reference, SecurityScheme]`. The doc's form —
  `- $ref: '#/components/securitySchemes/apiKeyHeader'` and
  `- $ref: '#/components/securitySchemes/eventTokenQuery'` (`async:155-157`) — is the
  correct 3.0 rendering (the v2 `{schemeName: [scopes]}` map form is deliberately absent).
- Semantics: each array item is an **alternative**, so the list expresses
  **header OR token** — which is exactly what is wanted (non-browser clients use the
  header; browser `EventSource` uses the token). Correct.
- **Finding (MEDIUM):** the amendment added `eventTokenQuery` to the *server*
  (`production-sse.security`, `async:155-157`) but **not to the operation**
  (`streamJobEventsSse.security`, `async:398-399`), which still lists only
  `apiKeyHeader`. In AsyncAPI 3.0 operation-level security overrides server-level
  security, so any tool computing *effective* security for
  `GET /api/v1/jobs/{job_id}/events` — the one operation the token exists for —
  resolves **header-only**. The WSS operations do this correctly —
  `receiveSubscriptions` (`:336-338`) and `sendJobEvents` (`:359-361`) each list
  `apiKeyHeader` **and** `wsTokenQuery`; the SSE operation is the only one short a
  scheme. Add the second `$ref` to `async:398-399`.

### 5.3 Everything else in the amendment parses

`x-retry` loads as a well-formed extension mapping; the YAML comments inside it are
legal; the SSE description block is a plain block scalar. Combined with §1.1
(yaml + refs) and §5.1-5.2 (vocabulary), **the amended async doc is structurally
conformant** — no `parameters.schema`-class rejection is waiting. Note for honesty: the
in-browser render of the *amended* file hasn't been re-verified in-repo (round 0's
browser verification predates `1b22914`); the structural analysis above is the evidence.

---

## 6. FINDINGS SUMMARY

| # | Sev | Finding | Fix size |
|---|---|---|---|
| 2.1 | **BLOCKER** | `POST /api/v1/ws-token` in neither spec; both async auth paths and the documented SSE reconnect strategy depend on it | 1 path in sync + plan A route table |
| 2.2 | **BLOCKER** | `CallbackStatus` defined differently in each spec (`last_failure`: date-time vs string; different `required`) while async claims to be the sync shape; fires on every `job.created` | align 2 schemas |
| 4.2 | **BLOCKER** | B4 fold claim "delivery-time validation is now in B's plan §4" is false (0 hits) — SSRF rebinding blocker unresolved | fold into B §4 |
| 2.4 | MAJOR | Sync spec carries both secret policies (`:1594` hashed vs `:1720` raw) + maxLength 128 vs 256 on the same field | 1-line edit each |
| 3.4 | MAJOR | RateLimits section + 429 + Retry-After absent from spec despite fold M11 / human decision 4 claiming them | 1 section + 1 response |
| 2.3 | MAJOR | M6 landed in sync only; async `:96-97` still calls `scraper_ready` "the terminal success state" | 1 sentence |
| 4.2 (M1) | MAJOR | `async:424` prose "5 attempts" contradicts `x-retry.attempts: 6` (`:484`) in the same operation | 1 word |
| 5.2 | MEDIUM | SSE operation-level security omits `eventTokenQuery`; effective security resolves header-only | 1 line |
| 1.2 | MEDIUM | GET /callback 200 "(or null)" unschemaable against `$ref CallbackStatus` | 1 `oneOf` |
| 1.3 | MEDIUM | 4 new error codes absent from the Error model's code list; breaks plan A test #13's premise | 1 line |
| 3.1 | MEDIUM | `reenable` has no cooldown/cap; ambiguous whether it re-arms N exhausted events; re-armed rows never prune | 1 paragraph |
| 3.2 | MEDIUM | `rotate` has no cutover/in-flight/invalidation semantics | 1 paragraph |
| 3.3 | MEDIUM | SSE `?token=` + EventSource auto-reconnect: no 401 behavior specified, no re-mint policy; documented reconnect needs the nonexistent endpoint | 1 paragraph |
| 3.5 | MINOR | Token-in-URL access-log caveat not carried from WSS (`:59-61`) to SSE (`:150-154`); SSE URL also leaks job_id | 1 sentence |
| 2.5 | MINOR | `input_mode` enum fork (`navigation` in async only) | 1 enum value |
| 2.2b | MINOR | `JobStatus.callback` duplicates the status enum inline instead of `$ref` | 1 line |
| 1.4 | MINOR | listJobs 422 text misstates `/output` bounds (100 vs 500) | 1 line |
| 1.5 | NIT | Unbalanced quote `async:455` | 1 char |
| — | NIT | Test's `token=` allowlist is a string-replace carve-out (`tests/test_api_docs_views.py:232-234`) — any future `events?token=` occurrence passes silently | tighten to a count |

### What checked out (for balance)

YAML parse + all 222 internal refs; correct 3.1 null-typing throughout the amendments
(no `nullable:` anywhere); valid AsyncAPI 3.0 `httpApiKey`-in-query vocabulary and
correct OR-shaped `security` list; B3/M6(sync)/M14/m2/m3 genuinely landed in the spec;
**14/14 tests re-run and passing**; `JobStatus.callback` and the delivery counters are
tenant-safe with no cross-tenant oracle; PATCH auth inheritance correct; the
disable/re-enable pointer now resolves to a real endpoint; the four fold decisions are
internally consistent with what did land.

---

## 7. RECOMMENDED PATH TO GO

Three blockers, all editorial. In order of cost:

1. **Add `POST /api/v1/ws-token` to `sync_api.yaml`** (auth: ApiKeyAuth; 201
   `{token, expires_in: 300, connect_url}`; 401; note the single-use/GETDEL semantics and
   the access-log caveat). Add it to plan A's route table and bump its counts to
   10 paths / 12 operations (callback GET/PATCH included).
2. **Make the two `CallbackStatus` schemas identical** (or have async `$ref` a copied
   definition), deciding `last_failure` = string-or-timestamp once. Recommend: string
   summary (sync's version) + a separate `last_failed_at` date-time in both.
3. **Actually fold B4 into plan B §4** (re-resolve + `ipaddress` before every attempt,
   `follow_redirects=False`, in writing) — or downgrade the fold's claim to "assigned,
   not landed" so the human knows the blocker is open.

Then the four majors (secret-at-rest line at `sync:1594`, 429/RateLimits, async M6
sentence, "5 attempts" at `async:424`) and the `async:398-399` security line. Everything
else can ride with implementation.
