# Wave-49 — Honest recording: the job-468 class (COMPLETED/0 with good rows on disk)

Owner directive (2026-09-29): "do rca on job 468 and comeup with a plan and then do a full e2e on all
the 5 sites". Two parallel RCA subagents (recording path + rescue path) produced the findings below;
every claim was verified against the persisted postgres checkpoint, celery logs, and the live FM.

## What actually happened to job 468 (tatcha-com, list_page, firstn/5)

The graph did everything right. The final checkpoint (thread `job-468`, written 0.7s before finalize
read it) says `execution_status='SUCCESS'`, `product_count=5`,
`output_file=workspace/tatcha-com/output_...003721...json` — run_execution's F16 selection and
`_count_products` were correct; the wave-40 T9 rescue was **correctly refused** (`_rescue_fires`
clause 3: the state does not claim zero). Both "missing" log lines are correct behavior.

The zero was written by **one** line at finalize:

```python
# webapp/scraper/tasks.py:2442-2444 (ground-truth override)
successful = [prod for prod in items
              if has_substantive_field(prod) and prod.get("status_code", 0) > 0]
job.product_count = len(successful)          # overwrites the correct 5 with 0
```

All 5 rows are tatcha's genuine $0.00 1-ml sample PDPs (`{"title": ..., "price": 0.0,
"status_code": 200}`). `has_substantive_field` (`src/content_types.py:499-513`) excludes `title` via
`_ALWAYS_PRESENT_FIELDS` (`:496`) and tests every other field with **bare truthiness** — so a row
whose only payload field is a legitimate numeric zero scores "not substantive". Five real rows →
`successful=[]` → COMPLETED/0.

## Root causes

| # | Defect | Site |
|---|--------|------|
| RC1 | `has_substantive_field` zero-blindness: bare `v` truthiness classifies a typed numeric **0** as "no data". Legitimate zero is data, not absence. | `src/content_types.py:513` |
| RC2 | `_scope_shortfall_row`'s `count <= 0` exemption silences the honesty marker for exactly the asked-N-shipped-**0** shape RC1 manufactures. Its rationale ("a zero-item run already fails loudly through the zero-yield gates") is the premise job 468 falsifies — the zero-item run was recorded COMPLETED. | `webapp/scraper/tasks.py:100` |
| RC3 | `_real_items_evidence` FM source (c) admits output keys on **name epoch alone** — no window comparison — and the finalize prune **never deletes** partner-owned keys, so a prior job's stale output is a permanent phantom supply. Verified live: a future zero-claim tatcha job would be rescued COMPLETED with 86 phantom rows (81 from a Sep-16 file + 1 + 4), locator pointing at the Sep-16 key. | `webapp/scraper/tasks.py:2011-2012`; prune exemption `:2524-2540` + `webapp/scraper/api/output_index.py:252-263` |

Side finding (aggravator, folded into F1b): the override's `prod.get("status_code", 0) > 0` treats a
**missing** `status_code` key as a failed fetch — any template that omits the field would be zeroed
the same way. Absence of evidence of fetch failure is not fetch failure.

## Fixes (no env gates — the reviewed merge is the switch)

### F1 — a typed numeric zero is a value (`src/content_types.py`)

Add a `_field_has_value(v)` helper used by `has_substantive_field`:

- `bool` → its truthiness (bool IS int; exclude explicitly),
- `int`/`float` → **always** a value (0/0.0 is real data),
- everything else → truthiness as today.

Blast radius: exactly two non-test call sites (`tasks.py:1804` `_good_rows` fallback when the job
declares no filter fields; `tasks.py:2442` the override). Fixtures in
test_f15/job71/job73 use only empty-string/brand/title rows — verified unaffected.
F1b (same block): the override's fetch filter becomes
`prod.get("status_code", 1) > 0` — missing key defaults to OK; a written `0` still rejects.

Invariant: *the override may lower the state's count only when the file demonstrably holds fewer
records, never when its rows are real-but-poor.*

### F2 — the shortfall marker fires at zero (`webapp/scraper/tasks.py`)

Drop `count <= 0` from `_scope_shortfall_row`'s guard (`target <= 0 or count >= target` remain).
`asked 5, shipped 0` becomes visible on the job record. FAILED zero-item jobs also gain the row —
harmless and more honest; the wave-48b H5 retry strip removes it on re-fire.

Pinned contract reversed: `TestScopeShortfallRow::test_zero_shipped_no_row` asserted `""` for
count=0; its comment's premise is falsified by job 468. Test updated to expect the row, citing this
plan.

### F3 — the FM rescue path gets a window (the mis-credit guard)

In `_real_items_evidence` source (c), per FM output key, compute a **payload-first freshness**:

```
ts  = parse(site.scraped_at)  else  max(parse(row.scraped_at))  else  None
fresh = ts if ts is not None else name_epoch
admit = (fresh is not None) and fresh >= floor
```

An in-payload timestamp **outranks the filename in both directions**: `ts < floor` excludes (no
fallback to the name — that is the phantom bleed), `ts >= floor` admits even when the *name* is
older. Fall back to the name epoch only when the payload carries no parseable timestamp (then an
unstamped name-less key is excluded, as today).

prod-770 safety: the 770 class is "same-run publish with an earlier-stamped name" — every generated
template stamps `site.scraped_at` at write time (verified in http_navigation/api/navigation/
playwright templates), so same-run publishes admit on their payload. The existing 770 test's fixture
predates payload-stamping awareness (it models an unstamped payload + old name — the documented
honest-under-credit case); its payload gains `"site": {"scraped_at": ...}` inside the job window,
assertion unchanged, with a comment citing this plan.

Deliberately NOT changing:

- **Summing across keys** (`fm_total`) — pinned by the wave-40 762 test (`n==4` from four 1-row
  files): a job's earlier-cycle slices sum by design. Once the window gate admits only this job's
  keys, summing is safe again. `rescue_count` may still exceed the rows in the single named
  `rescue_file`; that incoherence is pre-existing pinned behavior, not this wave's defect.
- **`_good_rows` fields-mode strictness** (`any(r.get(f) for f in fields)`) — stays bare-truthy on
  user-declared filter fields. It is the anti-fabrication gate; under-crediting is the safe
  direction, and post-F1 the state count is honest so the rescue is moot for the 468 class.
- **Prune `partner_owned_keys` semantics** — the payload gate is the single freshness authority for
  the rescue; the prune's partner exemption is a separate feature.

## TDD tasks

- T1 (RED→GREEN): `has_substantive_field` numeric-zero acceptance — zero-priced row with
  `status_code 200` is substantive; `price: ""`, title-only, bool-False rows unchanged; the
  finalize override records 5 for a 5×`price:0.0` payload (the 468 replay) and still rejects
  stub rows / written `status_code: 0`; missing `status_code` no longer zeroes.
- T2 (RED→GREEN): `_scope_shortfall_row("firstn","10",0)` returns the row (reverse the wave-48 pin,
  cite this plan); target≤0/garbage/non-firstn unchanged.
- T3 (RED→GREEN): FM source (c) window — a stale-payload key is excluded even with a fresh name; a
  fresh-payload key with an old name is admitted (770 shape, fixture updated); unstamped payload +
  old name excluded (honest under-credit); name-only keys keep working when fresh.
- T4: existing pins stay green — `test_prior_job_output_never_rescues` (local source (a), untouched),
  `test_762_shape` (sum semantics, untouched), wave-40 rescue suite, wave-48 suite (minus the
  deliberately reversed T2 pin).
- T5: full suite + `ruff check webapp/ src/`, restart celery-worker + django, then the e2e below.

## E2e acceptance (task: re-fire 5 sites + tatcha regression = 6 jobs, firstn/5, force)

1. **tatcha (the regression target)**: the recorded `product_count` equals the row count of the
   published output file — never COMPLETED/0 with rows on disk. (The first 5 of /collections/all
   are legitimately $0.00 samples; an honest "5" with $0 prices is the PASS. A wrong-price tester
   flag may persist — scraper quality, not recording.)
2. **books / briscoes / jomashop / westelm / pandora**: full yield preserved, zero wave-48/49 false
   trips (`kyg: null`, no stamp where the probe measured real anchors), no stale-FM mis-credit
   anywhere (no rescue line crediting a pre-campaign file).
3. Any short-shipped job carries `[SCOPE-SHORTFALL] asked N, shipped M` (including M=0) and it is
   stripped by a restart re-fire.
4. Grinder: cancel at 120 min via `POST /api/v1/jobs/<id>/cancel` (key at `/tmp/w48b_key`
   in-container; fire via a volume-mounted script — heredocs hit E2BIG last time).
