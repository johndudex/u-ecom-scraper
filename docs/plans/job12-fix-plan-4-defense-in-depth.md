# Job 12 fix plan — Planner 4: DEFENSE-IN-DEPTH / LAYERED VALIDATION

> Lens: every defect is caught at the earliest layer that can actually know, with
> later layers as backstops — and **layers never duplicate cost**. Evidence base:
> `docs/plans/job12-context-brief.md` (do not re-derive). All file:line verified on
> tree `36a91f0`.
>
> P4 (date-bomb) is out of this planner's lens — owned separately. Not covered here.

---

## 0. The lens verdict on job 12: three *structural* layer failures, not three bugs

Reading the failure chain as a layering problem produces a sharper diagnosis than
"the gate had a bad input":

**Structural defect A — evidence is destroyed at an artifact boundary.**
The strongest "is this a data API?" evidence in the whole system is the *response
body* that `verify_api()` already fetched and parsed at capture time
(`experimental/nav_traversal/traversal.py:472-502`: it returns `items`,
`sample_keys`, `count`, `items_per_page` — the GET is already paid for). The
descriptor survives into `_capture_api_from_session`'s ranking
(`traversal.py:1140-1144`, which scores on exactly `has_count` + `len(sample_keys)`),
and then **`webapp/agents/graph.py:2393-2398` throws `sample_keys` away** when it
writes `navigation_analysis.json`:

```python
"api_endpoint": (
    {"url": result.api["url"],
     "count": result.api.get("count"),
     "items_per_page": result.api.get("items_per_page")}
    ...
```

Every downstream layer — the strategy gate (`:3049-3058`), `validate_coverage`'s
bypass, `build_code_writer_message`, `build_product_analyzer_message` — is asked to
judge an endpoint from a URL string plus two ints, when the record keys were in hand
one frame earlier. This is the single highest-leverage finding of this plan.

**Structural defect B — a bypass keyed on an unvalidated input disarms a second
gate.** `webapp/agents/nodes/validate_coverage.py:150-158` computes *exactly* the
completeness predicate job 12 needed: `core = set(target_fields)`, then
`covered = extracted_fields & core`. With 1 of 6 fields that is 17%, far below
`MIN_COVERAGE = 0.80` — the gate would have fired. It did not, because
`:181-188` returns early on `if api_url:` — and the `api_url` present was the poison
one. **The P2 defect silently switched off the P3 defense.** Two independent layers
failed from one cause because the bypass condition shared the poisoned input.

**Structural defect C — the retry loop has a strategy channel but no endpoint
channel, and the strategy channel dead-ends at `internal_api`.**
`route_after_testing.classify_test_failure` (`route_after_testing.py:124-127`)
correctly said "strategy". The only correction channel into the deterministic
analyzer is `strategies_tried` + the escalation ladder
(`graph.py:2886-2931`), and that ladder is
`_ESCALATION = ["http_requests", "http_navigation", "playwright", "internal_api"]`
(`graph.py:2912`) — **`internal_api` is terminal**. `_ESCALATION[3+1:]` is empty, so
a failed `internal_api` escalates to *nothing*: `_derive_strategy` re-derives
`internal_api` from the same unmodified `navigation_analysis`, and cycle 3 burns
10 minutes / 49 tool calls / zero writes rebuilding the identical doomed scraper.
Meanwhile code_tester's *specific* diagnosis ("wrong API endpoint has no product
data") is unreachable: `remediation.target` is only consulted when the generic
classifier already said `"refine"` (`route_after_testing.py:679-685`) — in job 12 it
said `"strategy"`, so the most informed layer's answer was discarded. The failure
evidence ("0 items came from THIS URL") is never attributed to the endpoint, so the
endpoint is never invalidated.

Everything below follows from A/B/C: fix the evidence plumbing, un-share the
bypass input, and give the loop a channel that can name the real culprit.

---

## 1. P2 — Poison endpoint / strategy-gate trust

### 1.1 Layer map

| # | Layer | Evidence available there | Check it could own | Cost | False-reject risk |
|---|-------|--------------------------|--------------------|------|-------------------|
| L0 | **Capture** — `verify_api` / `_capture_api_from_session` (`traversal.py:472`, `:1066`) | **The response body, already fetched + parsed.** `items` (list of dicts), `sample_keys`, `count`, `items_per_page`, HTTP `status`, `final_url`, host. Also already has `_TELEMETRY_RE` (kills doubleclick) and `_NON_DATA_PATH_RE` (kills ketchcdn config). | Record-shape verdict: do the sampled record keys look like *records of this job's content type* rather than ambient/personalisation/config objects? | **~0 marginal** — the GET already happened; the verdict is a set intersection over `sample_keys`. | The real risk. Adversarial cases in §1.3. Mitigated by fail-open 3-valued verdict, never a veto. |
| L0.5 | **Artifact write** — `graph.py:2393` | The full descriptor (today truncated). | **Pass `sample_keys` through.** Not a check — an evidence-preservation fix that every later layer needs. | 1 line. | None. Purely additive. |
| L1 | **Synthesize** — `navigate_synthesize._best_api_endpoint` / `_ensure_api_endpoint_in_analysis` (`navigate_synthesize.py:128`, `:195`) | URL strings + query params only (`findings["api_endpoints"]`); **no body**. Also job-biased: `_score` requires `job\|search\|listing\|position\|vacanc\|posting` to return at all, so on a *product* site this path yields `{}`. | URL heuristics only (already exists as `url_looks_like_data_api`). Cannot own shape verification — it has no response. | Cheap but **duplicative** of L0. | High: any URL-token rule has the ketchcdn/useinsider tail. Do **not** add a new rule here. |
| L2 | **Strategy gate** — `_derive_strategy` (`graph.py:3049-3058`) | The (narrowed) descriptor + `rendering_verified` + `data_source` + probe method + the *fresh* `product_analysis`. | Require the L0 verdict to be present and not `"ambient"` before the `internal_api` override may fire. Plus a contradiction detector vs fresh `product_analysis`. | Free (already reading the dict). | Low if shaped as "override needs evidence" (fail-through to the existing rendering cascade) rather than "reject the endpoint". |
| L2.5 | **Coverage-gate bypass** — `validate_coverage.py:181` | Same descriptor. | Bypass only on an *evidence-carrying* endpoint. | Free. | None — strictly tightens an early `return`. |
| L3 | **Writer/analyst message** — `build_product_analyzer_message` (`subagents.py:1449-1485`), `build_code_writer_message` api_section (`:2676-2760`) | Descriptor + `_fetch_api_sample` (a *second* live GET — already spent per run). | Tone truthfulness: present an unverified endpoint as a **hint to verify**, not as "CRITICAL — do NOT drive a browser / No auth required / No proxy needed". | Free (text only). | None. Only changes prose, never routing. |
| L4 | **Tester** — `code_tester` + `route_after_testing` | Ground truth: the scraper actually ran and extracted 0 items from that endpoint. | Attribute the 0-item failure to the *endpoint*, and let `remediation.target` carry it. | Free. | None — it is post-hoc observation, not prediction. |
| L5 | **Loop feedback** — `_decide_strategy` (`graph.py:2886-2931`) | The observed failure + which URL produced it. | **Quarantine the endpoint** (not just the strategy) and repair the escalation dead-end. | Free. | None — quarantine only fires on an *observed* 0-item run. |

### 1.2 Chosen layers, and why the earlier ones can't be left alone

**Owns the verdict: L0.** It is the only layer with the body. Nothing later can
reconstruct record shape without paying a new HTTP GET, and a new GET at the gate is
precisely the cost duplication this lens forbids.

**Backstop 1: L2 (gate) — require evidence, don't guess.** The gate today accepts
`{url, count, items_per_page}` with no provenance. After L0/L0.5 the descriptor
carries `record_evidence`. Rule: the `internal_api` override may only fire when
`record_evidence.verdict == "data"`. If `verdict` is `"unknown"` (evidence absent —
pre-existing artifacts, or genuinely opaque keys) the override **does not fire** and
strategy falls through to the existing `rendering_verified` cascade. That is a
narrowing of an override, not a new decision: today's default (playwright /
http_navigation / http_requests) is what amn, lw, vistastaff, aya-pre-API and
abercrombie already succeed on. `internal_api` must *earn* its selection.

**Backstop 2: L4 + L5 (containment).** L0 can be wrong or silent. When it is, the
system must not burn 3 cycles. This is the defense-in-depth payload: **when the
verdict is not confidently `"data"`, do not merely permit the attempt — arm the loop
to correct in one cycle.** Concretely, an `internal_api` run started on a
non-`"data"` endpoint is marked `provisional`; its first 0-item test result
quarantines the endpoint immediately instead of escalating strategy.

Also fix, at L5, the dead-end: a failed `internal_api` must be able to fall *back
down* the ladder (to the strategy the rendering cascade would have picked), not be
re-picked. Order the fallback list by "most capable remaining", not a single
ascending array.

**Not chosen: L1.** Adding another URL-token heuristic there duplicates L0's job
with strictly worse information and a proven false-reject tail
(`url_looks_like_data_api`'s own docstring documents the ketchcdn lesson). Extend
`_NON_DATA_PATH_RE` opportunistically if a new class appears, but do not make L1
load-bearing.

**Not chosen at L3 for routing.** L3 changes prose only. But note L3 currently
*amplifies* the defect: `api_section` asserts "No auth/cookies/subscription key
required", "No proxy needed", "do NOT use Playwright" — unconditional absolutes
derived from an unverified URL. Downgrading that prose when `verdict != "data"` is
free and removes a second way the poison propagates (the writer is told the endpoint
is gospel).

### 1.3 Adversarial design of the L0 verdict (must not break the constraint-1 sites)

New pure function, `experimental/nav_traversal/traversal.py` (co-located with
`verify_api`, imported by `graph.py` the same way `url_looks_like_data_api` is):

```python
def classify_api_records(api: dict, target_fields: list[str], content_type: str) -> dict:
    """3-valued verdict on a verify_api descriptor: data | ambient | unknown.
    NEVER raises; NEVER returns 'ambient' on thin evidence."""
```

Signals, all already in the descriptor or derivable from the URL for free:

1. `host_is_first_party` — is the endpoint host a subdomain of the job URL's host,
   or a known CDN/API vendor (`*.algolia.net`, `api.amnhealthcare.io` pattern =
   *any* host containing the site's registrable domain, else third-party)?
   **Weight 0 — never veto alone.** Explicitly *not* a rule: the amnhealthcare case
   forbids domain-sameness as a gate.
2. `key_overlap` — fraction of `sample_keys` that hit a content-type-aware
   vocabulary: `src.content_types` core fields for the job's content type
   (`current_price`, `description`, `ratings`…) ∪ a small generic record vocabulary
   (`title`, `name`, `price`, `description`, `url`, `id`, `image`, `location`,
   `company`, `author`, `date`, `sku`, `brand`, `availability`, `currency`, …).
   Substring/segment matching (`productName` hits `name`+`product`), mirroring the
   word-boundary discipline already applied in `url_looks_like_data_api`.
3. `key_opacity` — are `sample_keys` dominated by short opaque tokens (`d`, `s`,
   `v`, `act`, `id`)? Insider-class responses are config/campaign payloads.
4. `select_option` rejection — already exists in `verify_api` (`_SELECT_OPTION_KEYS`);
   reused, not duplicated.

Verdict rules (fail-open by construction):

- `key_overlap >= 0.25` **or** ≥ 2 vocabulary hits → `"data"`.
- `key_overlap == 0` **and** `key_opacity` high **and** `count is None` → `"ambient"`.
- anything else → `"unknown"`.

Adversarial walk-through against every constraint-1 site:

| Site | Descriptor | Verdict | Correct? |
|------|-----------|---------|----------|
| **amnhealthcare** | cross-domain, `count:null`, real job API, keys incl. title/company/location | overlap high → **`data`** | ✅ override still fires |
| **aya** | `count=26955`, 90+ keys | **`data`** | ✅ |
| **lw.com (Coveo)** | `count=0` → the existing `_api_count != 0` guard already rejects; Coveo records carry `title`/`uri`/`raw` so overlap would be high anyway | **`data`** (moot — count guard wins first) | ✅ unaffected. Deliberately, the shape check is **ANDed with** the existing count guard, never a second independent veto. |
| **vistastaff (doubleclick)** | filtered by `_TELEMETRY_RE` upstream, never reaches L0 | n/a | ✅ |
| **ketchcdn** | `_NON_DATA_PATH_RE` rejects upstream | n/a | ✅ (yesterday's fix preserved) |
| **useinsider (job 12)** | personalisation array, `count:null`, config-shaped keys | **`ambient`** | ✅ override blocked; falls through to rendering cascade |
| **myntra / embedded-JSON sites** | `data_source` is `embedded_json`, not `api` — `verify_api` never involved | n/a | ✅ |
| **toscrape books/quotes/gutenberg** | no backend API at all | n/a | ✅ |
| **Hypothetical real API with fully opaque keys** (`{d:[…], s:1}`) | overlap 0, opacity high | **`unknown`** | ✅ fail-open: override blocked, but the endpoint is **not** pre-quarantined — the run proceeds and L4/L5 contain a failure in one cycle instead of three |

That last row is the design's centre of gravity: **a check that cannot be sure must
not veto; it must arm.** Rejecting `"unknown"` would be the false-reject that kills
a working site six months from now.

`record_evidence` schema written into the descriptor (additive, all optional keys):

```json
"record_evidence": {
  "verdict": "data|ambient|unknown",
  "key_overlap": 0.0,
  "sample_keys": ["..."],
  "checked_host": "pricelineau.api.useinsider.com",
  "signals": {"key_opacity": 0.8, "count": null}
}
```

### 1.4 Mechanisms + files

| Fix | File | Change |
|-----|------|--------|
| **P2-1 evidence preservation** | `webapp/agents/graph.py:2393-2398` | Pass `sample_keys` + `record_evidence` through alongside `url`/`count`/`items_per_page`. |
| **P2-2 verdict function** | `experimental/nav_traversal/traversal.py` (new fn near `verify_api`) | `classify_api_records()` per §1.3. Pure, deterministic, no I/O, no LLM. |
| **P2-3 call it at capture** | `traversal.py:1140-1144` (`_score`) and the `TraversalResult` build at `:1960` | Compute once per candidate; fold into ranking as a tiebreak *after* `(has_count, n_keys)` so existing rankings never change for `"data"` candidates. |
| **P2-4 gate requires evidence** | `webapp/agents/graph.py:3049-3058` | Add `and (_nav_api.get("record_evidence") or {}).get("verdict") in ("data", "unknown")` … i.e. only `"ambient"` blocks the override. `"unknown"` keeps today's behaviour (gate fires) but **sets `analysis["api_endpoint_provisional"] = True`**, which is what arms L5. Blocking only `"ambient"` is the conservative choice: it cannot change any site whose endpoint currently passes. |
| **P2-5 contradiction detector (backstop)** | `graph.py` `_derive_strategy`, after reading `_nav` | If fresh `product_analysis` (this run) contains ≥3 non-API field mappings (`method`/`selector` entries) or carries its own `data_source != "api"` while `_data_source == "api"` and the endpoint is not `verdict=="data"`, record `analysis["input_contradiction"] = {...}` + `logger.warning`. **Record only — no routing change** (deterministic analyzer stays deterministic; the recorded field is what L5 and humans read). |
| **P2-6 endpoint quarantine (the missing channel)** | `webapp/agents/state.py` (new `Annotated[list, operator.add]` field `quarantined_api_endpoints`), `graph.py:_decide_strategy`, `graph.py:_derive_strategy` | When `classify_test_failure(...)[0] == "strategy"` **and** the failed strategy was `internal_api`/`http_requests` **and** the endpoint that drove it is known, append `{url, reason, cycle}` to `quarantined_api_endpoints`. `_derive_strategy` then treats a quarantined endpoint as *absent*: `_nav_api = {}`, `_data_source` demoted to `"none"` for gate purposes, `strategy_justification` records the quarantine. This is the only mechanism that makes job 12's cycle 2 → cycle 3 transition corrective rather than repetitive. |
| **P2-7 escalation dead-end** | `graph.py:2912` | Replace the single ascending `_ESCALATION` list with a fallback map that includes reverse moves (`internal_api → http_navigation → playwright → http_requests`) so a failed `internal_api` has somewhere to go even without a quarantine entry. |
| **P2-8 truthful writer prose** | `webapp/agents/subagents.py:1449-1485`, `:2676-2760` | When `record_evidence.verdict != "data"`, swap the "CRITICAL / do NOT drive a browser / no auth / no proxy" absolutes for "candidate endpoint — VERIFY it returns your fields before committing; if the first response is not item data, fall back to the listing DOM." No routing change, no LLM cost. |
| **P2-9 let the tester's diagnosis through** | `webapp/agents/nodes/route_after_testing.py:679-685` | Also consult `remediation` when `_action == "strategy"`: if `remediation.target == "strategy"` and `reason` mentions the API/endpoint, prefer it for the *reason string* and set a flag `_endpoint_suspect = True` that `_decide_strategy` reads to quarantine. Text-matching is deliberately narrow (`api|endpoint|json url`) and only widens an already-taken `"strategy"` branch — it cannot change a `"scraper"`/`"mapping"` outcome. |

Cost accounting: one set intersection at capture (already-paid GET), a few dict
reads at the gate, one extra state list. **No new HTTP call, no new LLM call, no new
service.** Every layer reads evidence a cheaper layer already produced.

### 1.5 Failing tests first

1. `tests/test_api_evidence.py::test_useinsider_info_is_ambient` — fixture descriptor
   (url `https://pricelineau.api.useinsider.com/api/info/824.24`, `count: null`,
   `items_per_page: 12`, config-shaped `sample_keys`) → `verdict == "ambient"`.
2. `::test_amn_cross_domain_count_null_is_data` — cross-domain host, `count: null`,
   job-flavoured keys → `"data"`. **Regression guard for the amnhealthcare class.**
3. `::test_coveo_count_zero_still_blocked_by_count_guard` — `count: 0` + rich keys →
   gate does **not** select `internal_api` (existing `_api_count != 0` behaviour
   preserved) and the shape check is not what blocked it.
4. `::test_aya_count_26955_is_data`.
5. `::test_opaque_keys_are_unknown_not_ambient` — `{d, s, v}` → `"unknown"`, and
   `_derive_strategy` still selects `internal_api` but sets
   `api_endpoint_provisional`.
6. `tests/test_strategy_gate_evidence.py::test_gate_blocks_ambient_endpoint` —
   `_derive_strategy` with an `"ambient"` descriptor → strategy from the rendering
   cascade, not `internal_api`.
7. `::test_gate_unaffected_when_record_evidence_absent` — old-shape descriptor →
   behaviour byte-identical to today (rollback safety).
8. `tests/test_endpoint_quarantine.py::test_zero_item_internal_api_run_quarantines_endpoint`
   — job-12 replay: `strategies_tried` flow with `internal_api` + 0-item report →
   `_derive_strategy` on the next cycle returns a non-API strategy and
   `strategy_justification` mentions the quarantine.
9. `::test_escalation_has_a_target_after_internal_api` — a failed `internal_api`
   with no quarantine entry still yields a *different* strategy on the next cycle
   (would fail today — this is the cycle-3 test).
10. `::test_good_endpoint_never_quarantined` — a 3-item `internal_api` run (amn
    shape, `min_count=3` pass) leaves `quarantined_api_endpoints` empty.
11. `tests/test_codegen_fixes.py` (extend) — `sample_keys` survives
    `graph.py:2393`'s descriptor build.

### 1.6 Rollback

Each fix is independently revertible; order chosen so reverting later items never
reintroduces job 12:

- Kill-switch env `API_RECORD_EVIDENCE=off` → `classify_api_records` returns
  `{"verdict": "unknown", ...}` and the gate behaves exactly as today.
- `API_ENDPOINT_QUARANTINE=off` → the state field is never written and
  `_derive_strategy` ignores it.
- P2-1 (sample_keys passthrough) is additive and left in place under all switches —
  it changes no behaviour, only what is available.

---

## 2. P3 — Artifact completeness (validity ≠ completeness)

### 2.1 Where the truncation was first detectable — timeline

| T | Event | What was knowable | Was it used? |
|---|-------|-------------------|--------------|
| T0 | Stream cut mid-write; `write_file` got non-JSON content | `sanitize_json_content` returned `is_valid=False` + error (`filesystem_tools.py:52-95`) | Text note only; **not persisted**, no consumer reads it |
| T1 | Phase exit, `_fix_json_artifact(slug, "product_analysis.json")` (`graph.py:355`, wired `:2138`) | The repair note said exactly what was lost: *"pass 2: salvaged truncated object (valid prefix kept)"* / *"recovered N top-level keys"* | Logged, then **the salvaged artifact was published as authoritative with no marking** |
| T2 | `normalize_fields` (`nodes/normalize_fields.py:171`) | `analysis["fields"]` had 1 entry vs 6 `target_fields` | Merged + pruned silently; `_prune_to_schema` only *removes*, never reports shortfall |
| T3 | `validate_coverage` (`nodes/validate_coverage.py:150-158`) | **`core = set(target_fields)`; `covered = extracted & core` → 1/6 = 17% < 80%** | **The gate would have fired — but `:181` bypassed on the poison `api_url`** |
| T4 | `build_code_writer_message` | `state["target_fields"]` + the 1-field analysis | Prompt asks for all 6; no assertion |
| T5 | `code_tester` | Output had 1 field | Reported FAIL, but on strategy grounds |

**Answer to the brief's question:** the truncation was *detectable as an event* at
T1 (the salvage note is a complete description of the loss) and *detectable as a
contract violation* at T3 — where the correct predicate already existed and was
disarmed by P2's poison input. No new gate is needed; the existing one needs its
bypass fixed and its evidence marked upstream.

### 2.2 Layer map

| Layer | Evidence | Check | Cost | False-reject risk |
|-------|----------|-------|------|-------------------|
| **W — write time** (`write_file`/`edit_file`, `filesystem_tools.py:291`, `:353`) | `is_valid` + strict-parse error | Persist `is_valid` to a sidecar meta (today: text-only note) | Free | None — marking only |
| **R — repair time** (`_fix_json_artifact`, `graph.py:355`) | Which pass fired, how many keys survived, and (via `state`) the artifact contract | Record salvage provenance + compute the completeness delta vs `target_fields` | Free | **Must not refuse.** Renaming to `.corrupt` here would destroy the 9/10-keys recovery win. Mark, never delete. |
| **G — consumer gate** (`validate_coverage`) | `target_fields`, `content_type_config.core_field_names`, the parsed artifact | The predicate it already computes — enforced instead of bypassed when the endpoint is unverified | Free | Same as today, plus the bypass tightening (§2.3) which *reduces* silent-pass risk |
| **C — consumer contracts** (`normalize_fields`, `build_code_writer_message`, `code_tester`, `cleanup`, `_patch_scraper_output_filter`) | The artifact + its provenance sidecar | Each consumer decides: proceed-with-warning vs refuse | Free | Low — behaviour is keyed on an explicit flag, not on inference |

### 2.3 Chosen layers

**The gate is G.** It is the only layer that knows both the artifact and the job's
field contract, and it already computes the answer. Two changes:

1. **Un-share the bypass input (fixes structural defect B).**
   `validate_coverage.py:181` becomes:

   ```python
   api_ep = nav_analysis.get("api_endpoint")
   _ev = (api_ep or {}).get("record_evidence") if isinstance(api_ep, dict) else None
   _endpoint_verified = bool(api_ep) and (_ev or {}).get("verdict") == "data"
   if _endpoint_verified:
       # aya/amn class: fields map generically at scrape time via src.job_fields.
       ...existing early return, now also recording WHY...
   ```

   Effect: on job 12 (`verdict=="ambient"`) the gate runs, sees 17%, and interrupts
   with `low_coverage` — the human sees "1 of 6 fields" *before* any code is
   written. On aya/amn (`verdict=="data"`) behaviour is unchanged. On a
   pre-existing artifact with no `record_evidence` (`verdict` absent) the bypass
   also no longer fires — that is a deliberate tightening and the one place this
   plan could change behaviour for an existing working site, so it is covered by
   the explicit fallback in §2.6 (see `COVERAGE_BYPASS_UNVERIFIED=allow`).

2. **Make the bypass leave a trace.** Today the skip is a log line. Add
   `state_update["coverage_gate_bypassed"] = {"reason": "verified_api", "ratio": 0.17}`
   so SessionLog/`error_message` and any future consumer can see the gate was
   deliberately not enforced.

**R is the earliest *marking* layer, not a deciding layer.** `_fix_json_artifact`
already knows the pass that fired and the surviving key count; it must additionally
know the contract. Wire it as `artifact_fix_fn=lambda slug: _fix_json_artifact(
slug, "product_analysis.json", expect_fields=state.get("target_fields"))` at
`graph.py:2138` (and the `site_analysis` call site stays contract-free). On salvage
it writes a sidecar and logs a contract-aware warning:

```
_fix_json_artifact: product_analysis.json salvaged (pass 2) — 3 top-level keys;
target_fields coverage 1/6 [current_price] missing [description, previous_price,
ratings, remarks, scraped_at] — ARTIFACT IS A PARTIAL SALVAGE
```

Sidecar: `workspace/{slug}/.artifact_meta.json` — deliberately a **sidecar, not an
in-band key**, because `guard_json_bytes` (`filesystem_tools.py:108`) guarantees
valid artifacts pass through byte-identical and stable-diff cleanliness is a stated
design goal; mutating artifacts would break that.

**C — consumer contracts.** Explicit, minimal, and matching what each consumer
actually does:

| Consumer | Partial-artifact behaviour after this plan |
|----------|--------------------------------------------|
| `normalize_fields` | Proceeds (it is additive + prune-only). Emits `fields_extracted` — already does. |
| `validate_coverage` | **Refuses** below 80% unless the endpoint is evidence-verified (above). |
| `build_code_writer_message` | Proceeds, but when the sidecar says `salvaged: true`, injects one deterministic line: "product_analysis.json is a PARTIAL SALVAGE (1/6 requested fields) — map the remaining fields from the API sample / rendered page." No LLM cost; prevents the writer treating a 1-field map as complete. |
| `code_tester` | Unchanged — it judges output, not provenance. |
| `cleanup` / FM publish | **Warns and tags.** A salvaged `product_analysis.json` published to `scrapers/{slug}/analysis/` must not be silently re-hydrated as authoritative on the next run (this is the P5 bridge — see §4). Sidecar is copied alongside so the next job sees the flag. |
| `_patch_scraper_output_filter` (`graph.py:3537`) | Unchanged — operates on output records, not the analysis artifact. |

### 2.4 Failing tests first

1. `tests/test_artifact_completeness.py::test_salvaged_product_analysis_marks_sidecar`
   — truncated 1-of-6 artifact through `_fix_json_artifact` → sidecar records
   `salvaged`, `recovered_keys`, `target_field_coverage == {"covered": ["current_price"], "missing": [...5]}`.
2. `::test_valid_artifact_writes_no_sidecar` — a clean write produces no meta (no
   churn on the happy path).
3. `::test_salvage_is_never_refused` — the salvaged artifact is still written and
   still parses (guards the 9/10-keys win against regression). This is the test that
   would have caught a naive "rename to .corrupt" implementation.
4. `tests/test_quality_gate_targetfields.py` (extend) —
   `::test_low_coverage_not_bypassed_by_unverified_api` — `target_fields` of 6,
   analysis with 1 field, `navigation_analysis.api_endpoint = {url: ..., count: null}`
   and no `record_evidence` → `Command(goto="human_approval", interrupt_reason="low_coverage")`.
   **This test fails on today's tree** (the `:181` bypass returns early) — it is the
   direct job-12 replay.
5. `::test_low_coverage_still_bypassed_by_verified_api` — same but
   `record_evidence.verdict == "data"` → early return preserved (aya/amn guard).
6. `::test_bypass_records_reason` — `coverage_gate_bypassed` present in the update.
7. `tests/test_artifact_completeness.py::test_salvaged_flag_reaches_code_writer_message`
   — the writer message contains the partial-salvage line.
8. `::test_cleanup_copies_sidecar` — FM publish carries `.artifact_meta.json`.

### 2.5 Rollback

- `COVERAGE_BYPASS_UNVERIFIED=allow` (default `require_evidence`) restores the
  `:181` bypass for evidence-free endpoints — one env var reverts fix 1 to exact
  current behaviour.
- Sidecar writing is additive; `ARTIFACT_META=off` disables it. Nothing reads it
  except the writer-message line and cleanup's tag, both separately gated.
- The contract-aware `_fix_json_artifact` signature is keyword-only with a default
  of `None`, so the `site_analysis` call site and all existing tests are unaffected.

### 2.6 What could break

The bypass tightening is the risk. Sites where `data_source == "api"` is set but
`record_evidence` is absent (i.e. artifacts produced before this change, re-run
under resume) would now hit the coverage gate. Mitigation: the gate's existing
`MAX_COVERAGE_RETRIES` + "Continue anyway" interrupt path is exactly the intended
behaviour for a low-coverage analysis, and `skip_approvals` intake jobs auto-approve
— so worst case is one extra interrupt, not a hard failure. The env fallback covers
the remainder.

---

## 3. P1 — 429 / provider-error retry policy

### 3.1 Layer map

| Layer | What exists today | Handles | Gap |
|-------|-------------------|---------|-----|
| **L-call** (`llm.py:200-218` `_handle_retry`, `:88` `_backoff_delay`; `settings.py:217-221`) | Classified retry: `ratelimit_max=3`, `transient_max=2`, full-jitter exponential `base=1.5`, `cap=30s`, honours `Retry-After` when present | Sub-second to ~30s provider blips | Job 12: 4×429 in 8s, 3 attempts, sleeps 1.6/4.2/1.7s, exhausted in **~7.5s**. `Retry-After` was absent (code 1302 body), so the backoff ran blind. A tenant-level rate limit needs *minutes*, not 7 seconds. |
| **L-breaker** (`llm_breaker.py`, `settings.py:211-214`: threshold 4, cooldown 60s) | Per-model circuit breaker; 4 consecutive failures → fallback model for 60s | Sustained provider failure | 429s are recorded but the breaker's *cooldown* is not consulted by the backoff, and the fallback model for a ZAI 429 is **also** `glm-5-turbo` (`ZAI_FALLBACK_MODEL` default) — the same model that just got limited. The breaker cannot help. |
| **L-phase** (node / `_run_budgeted_agent`) | No provider-error awareness | — | An LLM exception inside a react node propagates straight out; there is no phase-level "provider down" classification. |
| **L-task** (`tasks.py:95-190` `run_scrape_task`) | `max_retries=1`; any exception → `STATUS_FAILED` + `error_message` tail | Code bugs, permanent failures | A *transient provider* condition is indistinguishable from a job bug. `self.retry` exists and is already used for the same-site collision (`tasks.py:133`) — the mechanism is present and unused here. |
| **L-job** (watchdog, human_approval) | Stuck-job watchdog (30 min); interrupt/resume machinery via `check_tracker` skip flags | Hung jobs | Never reached — the 429 pre-empted the retry-exhaustion → `human_approval` path the brief describes. |

### 3.2 Chosen layers — who owns what

**L-call owns *absorption*** (the first ~2 minutes of a rate-limit episode). It is
the only layer that sees the exception class and the `Retry-After` header.

- Raise `LLM_RETRY_RATELIMIT_MAX` 3 → **5** and `LLM_RETRY_BACKOFF_CAP` 30 → **120**.
  With full jitter this bounds worst-case added latency at ~5 min per call, but the
  *typical* case resolves in the first 2-3 attempts. Only for the `rate_limit`
  class — `transient_max` stays at 2 so genuine timeouts still fail fast (the
  900s wall in `subagents.py:89` is preserved: 5 × 120s worst case is only reachable
  when every attempt was rate-limited, which is exactly when spending the time is
  correct).
- **Spread the burst.** Job 12's four 429s landed within 8 seconds because
  `code_tester`'s react loop issued back-to-back calls. Add a **provider-pacing
  floor**: after any `RateLimitError`, record a monotonic `last_rate_limit_ts` on the
  LLM instance and enforce a minimum gap before the next `_generate` from that
  instance (deterministic `time.sleep`, no async, no new service). This is the
  cheapest available fix for the *burst* shape, which a per-call retry cannot see.
- **Fallback-model sanity:** when the limited model's configured fallback resolves
  to the same model string (job 12's case), skip the swap and log it, rather than
  "failing over" to the same endpoint.

**L-task owns *containment*** (the backstop when absorption fails). Rate-limit
exhaustion must not be a job failure.

- Classify at the task boundary: catch the exception in `run_scrape_task`, and if it
  is a provider-rate-limit exception (re-export a marker from `llm.py` — see below),
  `raise self.retry(exc=..., countdown=<jittered 300-900s>, max_retries=None)` with
  an explicit attempt ceiling (`LLM_RATELIMIT_REQUEUE_MAX`, default **2**) tracked on
  the job row. On ceiling exhaustion → `STATUS_FAILED` with a *provider*-attributed
  `error_message`.
- The job is genuinely resumable: `check_tracker`'s skip flags + `setup_workspace`'s
  preserve/re-hydrate set + the artifacts already on disk mean a re-dispatched job
  continues rather than restarts. No new infrastructure — this reuses the exact
  `self.retry` path already proven for the same-site collision at `tasks.py:133`.
- Marker plumbing: `llm.py` sets `exc.__llm_class__ = "rate_limit"` in
  `_handle_retry` before re-raising, so `tasks.py` needs no knowledge of
  `openai.RateLimitError` internals and no new import coupling.

**L-phase owns nothing new.** Deliberately: adding a provider-retry layer inside
`_run_budgeted_agent` would multiply with L-call (the same coupling the
`CODE_WRITER_LLM_TIMEOUT` comment at `settings.py:204-208` already warns about) and
would duplicate L-task's containment. This is the "layers must not duplicate cost"
rule applied to failure handling.

**L-job is the outer backstop, unchanged.** The watchdog bounds a re-dispatch loop
that misbehaves.

### 3.3 Failing tests first

1. `tests/test_llm_provider.py` (extend)
   `::test_rate_limit_backoff_respects_new_cap` — 5 attempts, delays ≤ 120s, ≥ 1
   attempt > 30s.
2. `::test_rate_limit_sets_pacing_floor` — after a 429, the second `_generate` is
   delayed by ≥ the floor (freeze `time.sleep`/`time.monotonic`).
3. `::test_rate_limit_exception_carries_marker` — the re-raised exception has
   `__llm_class__ == "rate_limit"`.
4. `::test_fallback_skipped_when_same_model`.
5. `::test_transient_budget_unchanged` — `transient_max` still 2 (guards against
   widening both classes).
6. `tests/test_task_rate_limit_requeue.py::test_rate_limit_exhaustion_requeues_not_fails`
   — simulate a node raising a `__llm_class__="rate_limit"` exception → task calls
   `self.retry`, job status is **not** `FAILED`.
7. `::test_requeue_ceiling_fails_with_provider_reason` — 3rd occurrence → `FAILED`,
   `error_message` mentions provider rate limit.
8. `::test_non_llm_exception_still_fails_immediately` — a `KeyError` in a node →
   `FAILED` on the first occurrence (no behaviour change for real bugs).

### 3.4 Rollback

Three independent env knobs, all defaulting to the new behaviour and all reverting
to exact current behaviour when unset/zeroed: `LLM_RETRY_RATELIMIT_MAX=3`,
`LLM_RETRY_BACKOFF_CAP=30`, `LLM_RATELIMIT_REQUEUE_MAX=0` (0 = never requeue →
current job-fatal semantics), `LLM_RATELIMIT_PACING_FLOOR=0` (disables pacing).
Each can be flipped independently from the Railway UI (constraint: web-UI-only
deploys).

---

## 4. P5 — Stale-artifact re-injection on resume

### 4.1 Layer map

| Layer | Evidence available | Check | Cost | Why not the owner |
|-------|--------------------|-------|------|-------------------|
| **Rehydration** (`setup_workspace._restore_from_archive`, `nodes/setup_workspace.py:81-129`) | The bytes, the filename, the M4 note | **Stamp provenance** (which job, FM vs fresh) | Free | Cannot see job config or the fresh probe — it can *mark*, never *decide*. |
| **Selector / skip flags** (`check_tracker._compute_rescrape_skip_flags`, `nodes/check_tracker.py:68-110`) | `prior.completed_at` (already queried), the fresh `target_fields`/`input_mode`, `state` | **Freshness + staleness ownership.** Today `skip_site = True` unconditionally (`:99`) — artifact age is never consulted. | Free (one datetime comparison on an already-fetched row) | ← **owner** |
| **Decision** (`_derive_strategy`) | The rehydrated `navigation_analysis` + the *fresh* `probe_result` + the fresh `product_analysis` | **Contradiction backstop.** Records (never resolves by guessing) when two authoritative inputs disagree. | Free | Correct as a backstop; wrong as the primary owner because by then the stale artifact has already been trusted to build the prompt. |

### 4.2 Chosen layers and mechanisms

**Ownership: skip-flag time.** It is the only place that has both the age signal
(`prior.completed_at`, already fetched at `check_tracker.py:82-88`) and the authority
to say "run site/navigation analysis again". Today the function's three inputs are
all *config* diffs (`target_fields`, `input_mode`, `search_criteria`); there is no
*evidence* dimension at all.

Add, deterministically:

- `analysis_age_days = now - prior.completed_at`. If > `ANALYSIS_FRESHNESS_DAYS`
  (default **30**), clear `skip_site` (and `skip_product`) so the site/navigation
  phases re-run — a 30-day-old `site_analysis` is not evidence, it is a memory.
- **Anti-bot fingerprint match.** If the fresh job's `site_type`/`input_mode` imply a
  different probe expectation, don't skip. (Cheap, config-only, no probe call.)
- Never auto-skip when the prior job **failed** — today `status == "complete"` gates
  the query, which is already correct; add a test to lock it.

**Stamping: rehydration time.** `_restore_from_archive` writes the sidecar from §2.3:

```json
{".artifact_meta.json": {
   "navigation_analysis.json": {"source": "fm", "job_id": 9, "rehydrated_at": "..."},
   "site_analysis.json":       {"source": "fm", "job_id": 9, "rehydrated_at": "..."}
}}
```

Guarded by `ARTIFACT_META=off`. Purely additive; no consumer changes meaning.

**Backstop: decision time.** In `_derive_strategy`, when a rehydrated
`navigation_analysis` (sidecar says `source: "fm"`, and `probe_result` was fetched
this run) asserts `rendering_verified` / `data_source` that *contradicts* the fresh
probe's `method_that_worked` (e.g. probe says `direct_http` + the fresh
`product_analysis` maps DOM selectors, while the FM artifact says
`data_source: "api"` + `rendering_verified: "browser"`), record
`analysis["input_contradiction"]` and prefer the **fresh** evidence. This is the
same field P2-5 writes — one mechanism, two triggers, no duplication. Consistent
with the brief's job-12 fact: *product_analysis had corrected toward
playwright/cx-state; the deterministic verdict overrode it* — the analyzer currently
has no notion that one input is 40 minutes old and the other is 40 minutes old but
from a *different, stale* job.

### 4.3 Failing tests first

1. `tests/test_rescrape_freshness.py::test_stale_prior_analysis_does_not_skip_site`
   — prior completed 45 days ago → `skip_site_analysis == False`. Fails today
   (`:99` is unconditional).
2. `::test_fresh_prior_analysis_still_skips` — prior completed 3 days ago, config
   unchanged → `skip_site_analysis == True` (no regression for the normal rescrape
   path).
3. `::test_rehydration_writes_provenance` — FM re-hydration populates
   `.artifact_meta.json`.
4. `::test_rehydration_of_corrupt_still_quarantines` — M4 behaviour preserved.
5. `tests/test_strategy_gate_evidence.py::test_fm_nav_contradicting_fresh_probe_is_recorded`
   — `input_contradiction` present on `scraper_analysis`, and the fresh probe's
   rendering signal wins the strategy.

### 4.4 Rollback

`ANALYSIS_FRESHNESS_DAYS=0` → the freshness check never fires (skip flags behave
exactly as today). `ARTIFACT_META=off` disables stamping. The contradiction recorder
is write-only telemetry with no routing effect and needs no switch.

---

## 5. Cross-cutting: the shared evidence object (why these layers don't duplicate cost)

One produced-once artefact serves four consumers:

```
verify_api() body ──► classify_api_records() ──► record_evidence{verdict, key_overlap, sample_keys}
                                                        │
   ┌───────────────────────────────────────────────────┼──────────────────────────────┐
   ▼                                   ▼               ▼                              ▼
graph.py:2393 passthrough   _derive_strategy gate   validate_coverage bypass     code_writer /
(keeps sample_keys)         (blocks "ambient")      (bypass needs "data")        product_analyzer prose
                                                        │
                                          _decide_strategy quarantine ◄── route_after_testing
                                          (observed 0 items)                remediation
```

Total added runtime cost across the whole plan: one set intersection over ≤15
strings, a handful of dict reads, one datetime comparison, one monotonic-timestamp
check. No new HTTP calls, no new LLM calls, no new services, no async.

## 6. Rollout order

1. **P1** (smallest blast radius, pure config + one classification marker; makes the
   next failure non-fatal while the rest is built).
2. **P2-1** (sample_keys passthrough — additive, enables everything below).
3. **P3 fix 1** (the `validate_coverage` bypass) — this alone converts job 12 from
   "42-minute silent failure" to "early `low_coverage` interrupt".
4. **P3 fixes 2-3** (sidecar + writer line).
5. **P2-2/3/4** (verdict function + gate).
6. **P2-6/7/9** (quarantine + escalation + tester channel) — last because it changes
   loop behaviour and needs the most regression scrutiny.
7. **P5** throughout; the freshness knob can land any time after P3's sidecar.

Every step ships with its failing tests already merged and green.

## 7. Constraint checklist

| Constraint | How this plan complies |
|-----------|------------------------|
| 1. No new per-run LLM cost | Every check is a set intersection, dict read, datetime compare, or prose swap. Zero LLM calls added. |
| 2. Must not break the working sites | §1.3 walks all 10; the shape check is ANDed with the existing `count != 0` guard, is 3-valued and fail-open, and `"ambient"`-blocking only *narrows an override* back to the rendering cascade those sites already succeed on. Explicit regression tests for amn/aya/lw/Coveo. |
| 3. Don't undo yesterday's fixes | `url_looks_like_data_api` and `_NON_DATA_PATH_RE` stay and still run upstream of the new check. The repair ladder is untouched — P3 *marks* salvages and explicitly forbids refusing them (test 2.4.3). Banded prior-count, word-boundary tokens, catalog guidance: all untouched. |
| 4. Streaming stays on | Untouched. No new parser assumptions. |
| 5. No async, no new services | `time.sleep` pacing only; requeue reuses the existing celery `self.retry`. Web-UI-only env knobs for every switch. |
| 6. `scraper_analyzer` stays deterministic | P2-4/5/6/7 are pure functions of state. The contradiction detector *records*; it never invokes an LLM and never guesses which input is right except by the deterministic "fresh beats stale" rule. |
| 7. Failing tests first | §1.5, §2.4, §3.3, §4.3 — each names the test that fails on today's tree. Baseline 719 pass / 2 fail (P4, out of lens) is preserved. |
