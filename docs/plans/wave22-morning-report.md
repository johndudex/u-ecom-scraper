# Wave-22 Morning Report — 2026-09-05

Overnight directive (2026-09-04): stop all crons → RCA everything that failed
(≥8 deep agents) → critique (≥8 agents) → fix at root cause → local e2e →
working solution ready on wake. **All five steps done.** Nothing deployed,
nothing merged — every step that touches prod waits for your click.

## 0. TL;DR

- All crons stopped (durable `3ee81b00` deleted, session jobs swept).
- 10 RCA agents + 10 critique agents ran; synthesis + critique-amended plan
  in `docs/plans/wave22-overnight-fix-plan.md`.
- **5 commit boundaries**, all green: `29ba121` (W1) → `871c9c3` (W2) →
  `85a68ef` (W3) → `570c121` (W4) → `bc38a08` (W5).
- Final counts: **2101 tests green** (root) + **182 webapp** + ruff clean
  (`webapp/ src/` wave-20 policy).
- Local e2e drives: **3-for-3 GREEN** — job **339** michaelhill 9/9 priced,
  **340** ego 10/10, **341** myhouse 10/10 (§4 + addenda).
- **Nothing is deployed.** Waves 20/21/22 all still local. Deploy runbook §6.

## 1. What was failing (RCA synthesis)

| Family | Root cause (verified) |
|---|---|
| 329-class terminal lies | Zombie writer thread edited the draft AFTER the tester's verdict → cascade terminated on a verdict about a draft that no longer existed |
| 359/360 class | Tester isolated the defect (`remediation.target` + field) but the exhausted retry ladder terminated with the fix never attempted; and writer self-edits introduced NameErrors the F821-less gate let through |
| 371/372 class | No-verdict tester cycles never advanced `test_retry_count` → writer→tester loop while soft-limit killed the task |
| browser draft probes always inconclusive | `scraper_runner` returns `output_content` "caller persists it" — nobody persisted it; probe output selection by mtime window stole identities (tester's own output) |
| Wall-clock economics | Every agent invocation dialled MCP tools with unbounded connects, stale pooled sessions, no retry; writers died at walls with no honest terminal record |
| T3.13e downgrade lied | `tested=="empty"` stripped coverage even when the render had escalated to a different access rung or was silently truncated |

## 2. What was fixed (commit by commit)

**W1 `29ba121`** — A1 knobs (`CODE_WRITER_LLM_TIMEOUT`, task soft/hard limits),
A6 counter, B4 crash-arm honesty, B5 terminal-verdict cascade logging, C4
tester prompt line, D3 `/api/version/` deploy probe.

**W2 `871c9c3`** — A2 no-report retry counter (kills the 371/372 loop), A3
task-budget clamp (refuses invocations with no wall clock left, honest
`WallClockTimeout`), A4 **MCP pool deleted** (per-call one-shot sessions,
bounded connects, one retry on timeout, `browser_network_requests` 240 s),
A5 trimmed fast-fail (product_analyzer + tester wall-clock deaths terminate
in minutes with named detail instead of burning the ladder).

**W3 `85a68ef`** — B1: `write_file`/`edit_file` F821 gate for code_writer in
the workspace — drafts with undefined names are REJECTED before bytes hit
disk (REAL ruff, F821-only, falls OPEN on checker malfunction).

**W4 `570c121`** — C1: probe output identity binding — pre-probe FS snapshot,
new-or-changed = owned, >1 candidate = inconclusive, blank coverage =
`inconclusive` never `0`, browser branch persists `output_content` to
`probe_output_*.json` (invisible to `run_execution`'s selector);
`listing_yield_failure`: unknowable ≠ dead, `phase1_skipped` stays dead.

**W5 `bc38a08`** — B2 forced re-test: every terminal verdict checks
`sha1(draft) ≠ last_tested_draft_fp` and spends ONE capped tester pass first
(per-job cap on `forced_retest_count`, consumed by the tester on the mismatch
signature; wall×2 and fast-fail arms exempt). B3 remediation grace: an
exhausted ladder with a structurally NEW remediation fingerprint
(sha256 of target+field+issue-type-set+exception — never the prose) earns ONE
writer cycle; writer records seen fingerprints + consumes the allowance.
C2 verify provenance: `tested=="empty"` costs coverage ONLY on full-fidelity
proof (answered rung == requested, `html_truncated is False`); degraded or
unknown downgrades to "skipped" (credit kept) + stamps `render_provenance`.

## 3. Test evidence

Every boundary = full root suite + webapp suite + ruff green before commit.
New tests this wave: 13 MCP one-shot, 12 fast-fail, 13 F821 gate, 16 probe
identity, 11 forced-retest, 15 grace, 11 render provenance + anchor updates.

## 4. Local e2e (job 339, michaelhill 338-shape)

Restarted all four services first (bind-mounted code; probe.py/server.py
changes made the browser_service restart load-bearing). Submitted via the
intake path (list_page, PDP seed, listing search_criteria, skip_approvals).

Green criteria checklist (ALL VERIFIED in SessionLog + output files):

- [x] Probe: PDP + listing both reachable via `fingerprint_chrome_none`
      (listing 1.1 MB — the "zero HTTP anchors" symptom from 338 did not
      reproduce this run)
- [x] B1 F821 rejection visible in SessionLog — fired exactly ONCE:
      `REJECTED — NOT applied; file unchanged. Undefined name(s): F821
      Undefined name '_normalize_availability' … a later def does not fix a
      module-level call`. The writer defined it properly on the next pass
      (final `scraper.py:200`) and never repeated the rejection.
- [x] Terminal = **COMPLETED** — `product_count=9`, empty `error_message`,
      100.4 min wall (soft limit 216 min)
- [x] No `retry=0/2` no-report loop signatures (0 hits in SessionLog)
- [x] No repeated B1-rejected writes of the SAME undefined name (1 total)
- [x] marimekko class stays COMPLETED (wave-21 tests cover; no drive needed)

**Outcome:** see §4-addendum.

### Wave-22 mechanisms exercised LIVE during the drive

| Mechanism | Evidence in job 339 |
|---|---|
| **B1 F821 gate** | 1 rejection (`_normalize_availability` used before def) — draft never hit disk broken |
| **B3/B2 retest flows** | Writer self-labelled: `Code Writer adapted (retest cycle 2 — HIGH availability fix)` in the finalized scraper |
| **Execution recycle** | First execution = 0 items / `navigate_error` → recycled once (1/1) → second execution pulled all 9 |
| **B5 cascade logging** | `[CASCADE] action=refine-fix retry=1/2 strategy=playwright remaps=0 reason=NEEDS_FIXES confidence=0.82` |
| **Honest terminal** | Final `remarks` field documents availability provenance instead of faking a value |

### Critique-9 diagnostic (Constructor.io)

Ran the requested `curl ac.cnstrc.com` from BOTH containers during the drive:
**DNS + egress are healthy everywhere** (HTTP 404/400 = API-level answers).
Critique 9's "connect failure at egress" hypothesis is REFUTED for this
stack; if the 338 fallback produced nothing, the cause is the embedded
constructor key/endpoint shape in the draft, not the network. No env fix
needed.

## 5. Known loose ends

1. **`browser_service/probe.py` is NOT committed** (your standing WIP-file
   boundary — honoured). The working-tree diff is now 100% wave-22 C2
   (verified: `git diff browser_service/probe.py` contains only the
   `html_truncated` emission — the old ruff residue is gone). Until you stage
   it, a fresh checkout of `bc38a08` lacks ONLY the probe-side half of C2;
   the e2e ran on the working tree so it exercised the full fix. Your call at
   PR time.
2. Same boundary still holds (untouched, unstaged): `src/discovery.py`,
   `webapp/agents/nodes/run_execution.py`, `webapp/agents/tools/shell_tools.py`
   — pure ruff residue, verified this session-pair.
3. User WIP files that WERE clean of residue and got committed during the
   wave boundaries: `templates/*.py`, `tests/test_job78_*`,
   `webapp/agents/tools/probe_tools.py`, `browser_service/server.py`.

## 6. Deploy runbook (when you're ready — your click)

1. **Railway env** (both django+celery services; browser_service too for
   `SCRAPER_SOFT_BLOCK_MIN_BYTES`):
   - `CELERY_TASK_SOFT_TIME_LIMIT=12960`, `CELERY_TASK_TIME_LIMIT=13320`
   - `WAVE_TAG=wave-22`
   - `CODE_WRITER_LLM_TIMEOUT=1200`
   - `SCRAPER_SOFT_BLOCK_MIN_BYTES=20000` (verify set on BOTH services —
     wave-19 item, still pending)
   - verify `PROXY_*` present on both celery services
2. **Deploy order**: django + celery image FIRST, browser_service AFTER
   (C2's unknown-provenance path keeps old browser_service working).
3. Verify `/api/version/` shows wave-22 and `/api/health/` green.
4. Tree-sync → PR (main ← file-master-artifacts) → merge → Railway, per the
   standing topology. Do NOT delete aimleap-side artifacts.
5. You unlock the maintenance lock (never mine to lift).
6. Re-drive campaign: restart ONE job first (`nav_method=list +
   list_urls=<PDP> + scope=all`), then release the rest. Restart GETs spawn a
   NEW job per call — fire ONCE.

## 7. Deferred (wave-23 backlog)

- f8_f16 test leak (`sys.modules.setdefault` fake `src.artifacts` module
  bleeds into job77 when run order aligns — subset-only, full suite immune)
- `_SAMPLE_CAP=5`, `fields_extracted` dead key, writer blind-offset
  `read_file`, getattr knob sweep, C3 raw-length detector (5% threshold
  under-fires on 2M-char Nuxt)
- B2's guarantee is per-job-1: in FAIL→regenerate flows the first
  regeneration re-test consumes the allowance (documented tradeoff; the 329
  PASS-then-edit shape — the one that mattered — is fully covered)

## 4b-addendum: drives 2+3 — ego 340 & myhouse 341 (2026-09-06, both GREEN)

User asked for two more drives on sites that failed on Railway. Both
COMPLETED on the wave-22 stack, sequential:

| | ego.co.uk (340) | myhouse.com.au (341) |
|---|---|---|
| Railway death | job 325: identical draft twice | jobs 324/372: no usable URLs + wall-clock kill |
| Result | **COMPLETED 10/10, 36.4 min** | **COMPLETED 10/10, 48.1 min** |
| Fields | 10/10 titled/priced(AUD→GBP)/currency/**availability in_stock** | 10/10 titled/priced(AUD)/currency/**availability in_stock** |
| Tester | PASS 0.88 first try, 0 high/0 med | PASS (single cycle) |
| B1 F821 gate | 0 rejections (clean drafts) | **2 rejections, different names each** — writer adapted, never repeated |
| Retry loops | none (0 CASCADE, 1 execution) | none (0 CASCADE, 0 no-report) |
| Discovery | 40 URLs, clean Tier-1 stop | 10 items off `sale-clearance` listing |

**B1 live-fire (341):** rejected undefined `ProxyConfig`, then
`DISCOVERY_STOP_DELAY_s`+`phase2_instant_fail` — two broken drafts never
touched disk; two distinct names = adaptation, not a loop. On Railway this
was the site-killing shape ("identical draft twice after failed tests").

**Verdict: 3-for-3 local e2e on the wave-22 stack** (michaelhill 339 +
ego 340 + myhouse 341), each green on the exact failure shape that killed
it on Railway. Sync commit `e63e425` pushed to al-johnf/main (user-approved,
incl. probe.py worktree = wave-21 T4/T5 + C2); PR merge = deploy trigger,
user's click.

## 4-addendum: job 339 outcome — E2E GREEN

**COMPLETED. 9 real products, 100.4 minutes, empty error_message.** (338,
same site/shape: FAILED with NameError churn and untested exhaustion.)

Final output `scrapers/michaelhill-com-au/output_2026-09-06_031000_774340_9157.json`
(10 output files total across the two execution attempts — the navigate_error
zero-item one is `…_021834_…`, the recycle-succeeded final is `…_031000_…`).

Field quality vs 338's gap (empty price/availability):

| Field | 339 result |
|---|---|
| title | **9/9** real |
| price | **9/9** real numbers (189.0, 219.0 AUD … — 338 had none) |
| currency | **9/9** `AUD` |
| availability | 9/9 `"unknown"` — **honest, documented**: the site's JSON-LD
`offers.availability` is a STATIC default and the DOM stock line is "Select a
size to see stock availability"; the writer handled both and wrote the
provenance into `remarks` per item |
| bonus fields | original_price, sku, mpn, material, color, specs, rating,
review_count, images, category — 20+ fields per item |

Drive timeline: probe fingerprint_chrome_none (PDP + 1.1 MB listing) →
tester cycle-1 PASS 0.88 (9 samples) → execution #1 zero-item
`navigate_error` → **recycle 1/1** → writer cycle-2 burned budget honestly
(2× "need more steps", blind-offset read_file — wave-23 shape, documented §7)
→ tester cycle-3 PASS (Phase 1: 9 URLs; Phase 2: 9/9) → execution #2 → 9/9
priced → cleanup → skill_learner → COMPLETED.

Critique-9 (Constructor.io egress): diagnostic run mid-drive — `curl
ac.cnstrc.com` healthy from BOTH containers (404/400 = API-level answers).
Connect-failure hypothesis REFUTED; no env change needed.

**Verdict: the wave-22 fix set survives a full unsupervised pipeline run on
the exact shape (michaelhill/Nuxt/CSR listing) that failed as 338.**
