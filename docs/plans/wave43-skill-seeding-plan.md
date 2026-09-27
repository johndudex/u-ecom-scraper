# Wave-43 — Skill Seeding Plan (knowledge harvest, no runtime behaviour changes)

**Status:** DRAFT for owner approval. Nothing in this plan writes to any skills file.
**Date:** 2026-09-26 (critique applied same day)
**Author:** harvest orchestrator (Claude), from a 408-job production harvest
**Audit:** `_harvest/seeds/critique.md` — fresh-eyes agent, 59 live spot-checks, 0 auth failures

---

## 0. What this is

A 408-job production harvest (every successful prod job, none skipped) distilled into
13 seed drafts, audited by a fresh-eyes critique agent, corrected, extended with 2
post-critique seeds (s14, s15), and turned into THIS plan. The deliverable is knowledge
+ a plan. **No runtime behaviour is changed by this document.** Every seeding action
below is gated on owner approval (§7).

Harvest facts:

| Measure | Value |
|---|---|
| Jobs harvested | 408 / 408 (0 unreadable, 0 skipped, 0 with nothing reusable) |
| Harvest wall clock | ~2h31m for the fetch+distill phase (19:43-22:14), ~3h30m to the finished plan |
| Sites | 388 distinct |
| Distinct platform verdicts | 58 (top: sfcc 85, shopify 79, custom 67, headless-nextjs 47, magento 30) |
| Distinct technique strings | 1,794 |
| Lessons / pitfalls extracted | 1,160 / 1,194 |
| Critique spot-checks | 59 claims, **57 VERIFIED / 2 PARTLY / 0 WRONG** (12 random jobs, 20 prod fetches) |
| Reconciliation | PASS: queue == manifest == `jobs/` exactly; one cardinality deviation (376/408 strictly in the 1-3 lessons/pitfalls band; 32 files over-stuffed, 2 files with 0 pitfalls — never information lost) |

Artifacts (all under `_harvest/`):
`jobs/<id>.json` (408 files), `manifest.json`, `worker_notes.md`,
`agg/platforms.json`, `agg/techniques.json`, `agg/clusters.json`, `agg/index/*`,
`seeds/s01..s15.md`, `seeds/_source_jobs.json`, `seeds/coverage_report.json`,
`seeds/critique.md`.

---

## 1. How skills actually reach a prompt (the constraint that shapes everything)

Read the mechanics before the seed list — they decide what is worth seeding.

1. **Write path.** `src/skills_store.py:265 append_learned(name, title, source,
   applicability, body)` appends a `## Learned:` section. It (a) refuses if the skill
   is not already in the File Master ("seed first"), (b) refuses duplicate titles,
   (c) writes via `src/artifacts.py` → `FILE_MASTER_URL`, so writes survive redeploys.
   New skills need `create_skill(name, description, body)` (`skills_store.py:300`).
   `nav_skill_review` uses the same path via the `learn_skill` tool
   (`webapp/agents/tools/skill_tools.py:90`).
2. **Read path.** `render_learned_sections(name, cap=1500, newest=2)`
   (`skills_store.py:187`) returns only the **2 newest** sections that **fit whole**
   in 1500 chars. An older section that would bust the cap is skipped silently.
   Consequence: **appending a new section can evict an older one from the render
   window** on the same skill.
3. **Injection.** Only FIVE skills are live readers:
   `_PLATFORM_SKILLS = {shopify, sfcc, algolia, amazon, kibo}`
   (`webapp/agents/subagents.py:3478`), injected into the code-writer prompt by
   `_platform_distillation` (`subagents.py:3557`) as
   `render_learned_sections(_skill, cap=1500)`.
   Learned sections on the other 11 skills are **dead** unless an agent happens to
   call `load_skill` (agents whose toolset includes `load_skill` see a name/description
   list; the call itself is optional and rare).
4. **Job coverage of the live readers:** sfcc 92 jobs, shopify 90, amazon 11,
   algolia 8, kibo 2.

So the honest value ranking is:

- **Tier 1 — seeds on live readers** (sfcc-detection, shopify-detection,
  amazon-detection, kibo-detection): the writer sees them on every matching job,
  automatically, with zero code change.
- **Tier 2 — seeds on existing but dead readers** (navigation-patterns,
  jsonld-extraction, anti-bot-handling, proxy-config, playwright-navigation,
  akamai-detection): real knowledge, stored durably, but reachable only via an
  agent-initiated `load_skill`. Worth writing for the record + for agents that do
  load skills; do not expect automatic injection.
- **Tier 3 — proposed NEW skills** (magento-detection, output-schema-integrity,
  netsuite-detection): need `create_skill` first (append refuses to create), and remain
  dead readers unless a code change adds them to the injection map — which is a prod
  redeploy and an owner decision. (A fourth candidate, `job-economics`, was dropped at
  critique: advisory-only content on a non-injected skill reaches nobody.)

---

## 2. The seed list (post-critique: 13 of 15 seeds, verdicts applied)

Verdicts from `_harvest/seeds/critique.md`: **5 KEEP (s01, s02, s05, s09, s12), 6 FIX
(s03, s06, s07, s08, s10, s11 — all applied to the seed files), 2 DROP (s04, s13,
salvaged).** Two seeds were ADDED after the critique closed coverage holes: s14 (Kibo,
live-injected) and s15 (NetSuite SCA). Every body is ≤1200 chars and re-verified.

Formats: each seed file has distilled lessons with job citations, a proposed
`## Learned:` body, and honest gaps. Fixed citations were re-verified against
`_harvest/jobs/` before editing.

### Tier 1 — live readers (no code change, automatically injected)

| Seed | Target skill | Body chars | Verdict | One-line content |
|---|---|---|---|---|
| s01-sfcc-runtime-subpatterns | sfcc-detection | 939 | KEEP | SFCC's 3 shapes (SSR tiles `?start=` / JSON-LD `hasVariant[]` / SiteGenesis zero-JSON-LD), Mobify `__PRELOADED_STATE__` island, stale-availability trap (the canonical home for it) |
| s02-shopify-endpoint-reality | shopify-detection | 1153 | KEEP + Shopline trap added | Silent endpoint degradation (.json vs .js vs SSR), cents-vs-dollars unit mixing, Hydrogen `__remixContext` + `{'script:ld+json'}`, and the new Shopline corollary: `/products/{handle}.json` returning homepage HTML means it is NOT Shopify (549,142,296) |
| s03-amazon-runtime-notes | amazon-detection | 572 | FIX applied | Locale-specific wall: `amazon.sa` → **Amazon.es (757)**, falsy soft-block (200-with-zero-`/dp/`), `/dp/{ASIN}` + curl_cffi safari for .com.br; the per-pair captcha bullet now lives only in s07 |
| s14-kibo-preload-api | kibo-detection | 552 | NEW post-critique | `<script id="data-mz-preload-product">` + unauthenticated `/api/commerce/catalog/storefront/products/<id>`, both plain chrome-TLS HTTP (493); sale-vs-list offer orientation (404). kibo-detection has 0 sections today, so this is the only way a Kibo job sees harvest knowledge |

Verified source-job citations per seed (full lists in
`_harvest/seeds/_source_jobs.json`):

- s01 → 24 jobs: 121, 326-329, 380, 390, 399, 413, 469, 472, 496, 678, 785, 809,
  818, 826, 836, 855, 863, 872, 878, 882, 889, 967
- s02 → 23 jobs: 52, 55, 138, 217, 247, 259, 288, 297, 306, 310, 311, 331, 334,
  387, 399, 404, 453, 521, 651, 772, 883, 884, 969
- s03 → 4 jobs: 398, 591, 757, 924
- s14 → 2 jobs: 404, 493

### Tier 2 — existing skills, dead readers (no code change; `load_skill`-only)

| Seed | Target skill | Body chars | Verdict | One-line content |
|---|---|---|---|---|
| s05-headless-shell-discovery | navigation-patterns | 1054 | FIX applied | SPA shell ≠ listing; framework JSON goldmines (`__NEXT_DATA__`, `/_next/data`, `page-data.json`, `/__data.json`, RSC-escaped JSON-LD); anchor discovery structurally cannot work; + salvaged from s04: verify a captured "API" returns product fields before spending budget (173 UseInsider, 142/269 analytics beacons) |
| s06-jsonld-shapes-and-staleness | jsonld-extraction | 1165 | FIX applied | `hasVariant[]` walk, ItemList completeness split, single-script `@graph`, Elevar dataLayer, + Fanatics `offers[0].priceSpecification.price` nesting (545) and JSON-LD-free NFL Shop (792); stale-availability numbers now live only in s01 (live channel) |
| s07-blocks-are-per-request | anti-bot-handling | 859 | FIX applied | Blocks are per-request / per-(fingerprint,proxy) pair; cached `anti_bot:false` lies (332/335/336/338); byte-count verdicts untrustworthy; Turnstile IP-bound (292 vs 304); 202-empty; Hydro Flask re-cited **408** (was wrongly 485 = made-in-china) |
| s08-per-surface-transport-split | proxy-config | 889 | FIX applied | Transport is per surface (PDP vs listing); never hard-pin the probe rung (301, verified to the digit: 5916s, 0 records); pin `min_tier_floor` to measured (655, 797); `create_fetch_json` tuple re-cited to **611** and 914 dropped from it; Harvey Nichols (674) not "Calvin Klein" |
| s09-hydration-and-interaction-gated | playwright-navigation | 927 | KEEP | Both override directions (browser↔http), `networkidle` hangs Adobe Commerce (808: 420s → 15.4s with `domcontentloaded`), host-assertion guards, the honest-unfixable case |
| s10-akamai-ladder-collapse | akamai-detection | 656 | FIX applied | Ladder collapses to one survivor (403 domain-wide incl. JSON APIs); uncited brand list replaced with the real sites: 473 Next Ireland, 479 The North Face, 499 Brooks Brothers India; probe BOTH cloak and curl_cffi families; verdict hygiene |

Verified source-job citations per seed:

- s05 → 24 jobs: 173, 282, 284, 313, 331, 498, 502, 588, 589, 605, 697, 698, 787,
  815, 874, 913, 917-921, 923, 925, 967
- s06 → 11 jobs: 163, 259, 275, 280, 698, 904, 917, 922, 927, 954, 968
- s07 → 13 jobs: 292, 304, 332, 335-338, 398, 403, 408, 781, 809, 963, 970
- s08 → 14 jobs: 301, 320, 326-329, 611, 655, 665, 674, 797, 798, 805, 837, 918
- s09 → 8 jobs: 808, 903, 918, 919, 923, 926, 963, 966
- s10 → 14 jobs: 292, 332, 335-338, 403, 473, 479, 499, 781, 792, 797, 800, 809

### Tier 3 — proposed NEW skills (need `create_skill`; live only with a code change)

| Seed | Proposed skill | Body chars | Verdict | Justification |
|---|---|---|---|---|
| s11-magento-2-patterns | magento-detection | 819 | FIX applied (pre-condition: resolve the create-vs-append race FIRST — job 910's log says "Skills created: magento-detection", prod still shows 16 skills) | Magento is 30 jobs with no skill: `data-ui-id` tells, `span[data-price-type=finalPrice\|oldPrice]` in `.price-box`, PWA client-rendered listing trap (498/588/589/605), `xmlns:blc` = Broadleaf not Magento, + the new theme rule: read the theme from the static asset path (`…/frontend/default/hooya/…`), never from agent prose — job 910's tester said "Luma", the path said `hooya` |
| s12-output-schema-integrity | output-schema-integrity | 998 | KEEP with framing fix applied | The harvest's biggest cross-job finding, rewritten to carry ONLY the writer/tester workarounds (emit alias + canonical keys; treat `run_history.records` as the honest tally; never infer schema-completeness from PASS) + the folded s13 fact: step durations sum to ~2x `run_history.duration_seconds` because phase timers overlap |
| s15-netsuite-sca-api | netsuite-detection | 622 | NEW post-critique | Unauthenticated `/api/items?url={slug}&fieldset=details` returns displayname/price/stock over plain HTTP; the same endpoint does Phase-1 discovery via `fieldset=search` (658, 679 — two jobs, identical mechanism); HTML is a ~5.8KB shell |

Verified source-job citations per seed:

- s11 → 11 jobs: 160, 163, 166, 280, 498, 588, 589, 605, 910, 916, 952
- s12 → 25 jobs: 7, 136, 275, 469, 473, 474, 477, 479, 481, 482, 487, 498-500, 502,
  597, 601, 605-610, 783, 808, 960
- s15 → 2 jobs: 658, 679

### Dropped at critique (files kept for the audit trail)

| Seed | Verdict | Why, and where the content went |
|---|---|---|
| s04-algolia-runtime-notes | **DROP** | 416 chars on a 21,403-char baseline; the one new line was already taught by that baseline, and job 173 was misattributed. Salvage: the "verify the endpoint returns product fields before spending budget" line moved into s05 (173 re-cited as the UseInsider false-positive it actually was) |
| s13-job-economics | **DROP** as a seed | Advisory-only on a NEW non-injected skill reaches nobody, and the owner has ruled out wall-clock/writer-loop changes. Salvage: the phase-timer overlap fact folded into s12. Campaign intelligence worth keeping: queue wait dominates wall clock (597 ~7.4h, 607 ~7.75h, 808 ~7h47m of a 9436s job) and the slowest jobs (481/503/479/482/499/487) had zero-or-trivial anti-bot — that is operator/campaign intelligence, not skill content, so it lives here and in the critique rather than in a skill file |

---

## 3. How the seeding would be executed (option 1 — NO code change)

One-time File Master write from the django container, using the same append path the
agents use. No code change, no redeploy, nothing else touched.

```bash
docker compose exec django python manage.py shell -c '
from src.skills_store import append_learned
r = append_learned(
    "sfcc-detection",
    "SFCC runtime sub-patterns (3 shapes + Mobify island)",
    "408-job prod harvest 2026-09-26 (jobs 121,390,496,809,818,826,889,967...)",
    "Any Salesforce Commerce Cloud / Demandware site, incl. Nuxt and Mobify PWA fronts",
    open("/tmp/s01_body.txt").read(),
    actor="wave43-harvest",
)
print(r)   # expect {"ok": True, "appended": True, ...}
'
```

Rules the executor must respect:

1. **The body text comes from the seed files** (`_harvest/seeds/s*.md`, "Body:" block),
   not re-typed. Bodies are already ≤1200 chars.
2. **`append_learned` refuses to create a skill.** Tier 3 seeds (s11, s12, s15) need
   `create_skill(name, description, body)` first — and that is a bigger decision (see §4).
   For s11 specifically the create-vs-append question must be resolved FIRST: job 910's
   skill_learner log says `Skills created: magento-detection` while prod's
   `/learnt-skills/` still shows 16 skills, so a partial `magento-detection` may already
   exist in the FM under a different key. Check
   `read_skill("magento-detection")` before choosing the call — if it returns text,
   append; if it returns None, `create_skill`. (`create_skill` itself fails cleanly with
   `{"ok": False, "error": "skill 'magento-detection' already exists"}` if it does exist.)
3. **Duplicate titles are refused** (returns `{"ok": True, "appended": False,
   "note": "a learned section with this title already exists"}`) — if a title already
   exists, the executor must NOT vary the title to force it in; that is the dedupe
   guard working. Dry-run first: `from src.skills_store import read_skill;
   print("wave43" in (read_skill("sfcc-detection") or ""))` per target is enough to
   confirm none of these sections are already present. Appends are independent, so a
   partial run can simply be resumed with the remaining seeds.
4. **The render window, simulated exactly** (`render_learned_sections(cap=1500,
   newest=2)`, include-only-if-it-fits-whole, newest first). Reproduced from
   `_harvest/existing_skills.json`:

   | Target skill | Existing sections | Renders TODAY | After the append |
   |---|---|---|---|
   | sfcc-detection | 1 (PVH, 2467 ch) | **0 chars — nothing fits** | s01 (939) |
   | shopify-detection | 0 | 0 | s02 (1153) |
   | amazon-detection | 0 | 0 | s03 (572) |
   | kibo-detection | 0 | 0 | s14 (552) |
   | proxy-config | 0 | 0 | s08 (889) |
   | akamai-detection | 0 | 0 | s10 (656) |
   | anti-bot-handling | 2 (newest 1370 ch) | 1370 (UTF-8 mojibake) | s07 only (silent eviction) |
   | jsonld-extraction | 15 (newest 1429 ch) | 1429 (aggregateRating-array) | s06 only (silent eviction) |
   | navigation-patterns | 5 (newest 5955 ch) | **0 chars — nothing fits** | s05 (1054) |
   | playwright-navigation | 2 (newest 1934 + 2723 ch) | **0 chars — nothing fits** | s09 (927) |
   | magento/output-schema/netsuite (NEW) | none | 0 | the seed alone |

   (Sizes are section **body** lengths; `render_learned_sections` measures the whole
   section, which adds the `## Learned:` header plus Source/Applicability lines —
   roughly +100-250 chars each. That only reinforces the conclusions: nothing here is
   near the 1500 boundary in a way that changes a verdict.)

   Two things follow, and they change the tone of the usual "append evicts older
   sections" worry:

   - **sfcc-detection currently injects ZERO learned content into the code-writer**
     despite being the highest-volume live platform (92 jobs): its only learned
     section (PVH Corp, 2467 chars) is over the 1500 cap, so the render is empty.
     Appending s01 does not hide a visible lesson — it restores learned content to a
     skill that has none reaching the prompt. The same strict improvement applies to
     navigation-patterns and playwright-navigation (their newest sections are also
     over the cap). The PVH/Coveo/CDP texts stay in the FM record and on
     `/learnt-skills/`.
   - On anti-bot-handling and jsonld-extraction the append does displace the section
     that renders today (1370 / 1429 chars). Those two are `load_skill`-only readers
     anyway, and the displaced text remains retrievable; the net is +1 visible lesson,
     -1 visible lesson, both still on disk. **The jsonld case is the one that needs a
     decision**: that skill already carries 15 learned sections and the render can
     surface at most 2, so appending there is rotation, not accumulation — a
     curation pass would serve it better than a 16th section. The critique's
     alternative, adopted here, is that s06's most important fact (stale
     availability) already lives on live-injected s01.
   - **No two seeds in this batch share a target skill**, so there is no seed+seed
     collision anywhere (the hypothetical worst case, two ~1000-char seeds on one
     skill = >1500, never arises). In every case the post-append render is the seed
     alone, because no existing section is small enough to share a 1500-char window
     with it. If the owner wants two lessons visible at once, sections would have to
     be ≤~700 chars each — a content-size decision, not a code change.
5. **Order matters.** Seed Tier 1 first (highest value), then Tier 2, then Tier 3 only
   if approved. Per-seed action:

   | Seed | Target | Action |
   |---|---|---|
   | s01 | sfcc-detection | `append_learned` |
   | s02 | shopify-detection | `append_learned` |
   | s03 | amazon-detection | `append_learned` |
   | s14 | kibo-detection | `append_learned` |
   | s05 | navigation-patterns | `append_learned` |
   | s06 | jsonld-extraction | `append_learned` (see the rotation caveat in §3.4) |
   | s07 | anti-bot-handling | `append_learned` |
   | s08 | proxy-config | `append_learned` |
   | s09 | playwright-navigation | `append_learned` |
   | s10 | akamai-detection | `append_learned` |
   | s11 | magento-detection | check `read_skill` first, then `append_learned` **or** `create_skill` (§3.2) |
   | s12 | output-schema-integrity | `create_skill` |
   | s15 | netsuite-detection | `create_skill` |
6. **Verification after write (read-only):** re-fetch `/learnt-skills/` and confirm the
   section count went up by the number of appends and that each new title is present;
   then `manage.py shell -c 'from src.skills_store import render_learned_sections;
   print(render_learned_sections("sfcc-detection")[:400])'` to confirm the writer
   actually sees it.

---

## 4. What this plan deliberately does NOT do (and what a code change would buy)

Flagged for the owner; **any code change implies a prod redeploy**, which must wait for
the owner's quiet window. The owner has said prod must not break and has ruled out
wall-clock/writer-loop changes — none of the below touches those.

1. **Add Tier 3 skills to the injection map.** Making `magento-detection`,
   `output-schema-integrity` or `netsuite-detection` live readers means editing
   `_PLATFORM_SKILLS` (magento, netsuite) and/or adding a non-platform injection point
   (output-schema-integrity is cross-cutting). Redeploy required. s11 (magento) is the
   strongest candidate: 30 jobs, no skill at all today. Two details the edit must
   respect, from the harvest's own platform strings: the map is **substring**-matched
   (`key in platform.lower()`), and `"magento"` does appear inside non-Magento verdicts
   such as `custom (server-rendered ecommerce, no shopify/magento/sfcc markers)` — a
   naive key would inject Magento notes onto custom sites. And the learned notes are
   appended *after* the 2047-char distillation truncation (`subagents.py:3553-3557`),
   so a seed section reaches the writer in full rather than competing for the 2KB.
2. **Fix the `create_fetch_json` tuple contract** (jobs 611, 665, 798, 837): the
   generated-scraper helper returns a tuple, so `isinstance(payload, dict)` is always
   False and scrapers silently fall back to og tags — job 611's writer comment spells it
   out ("# tuple contract. The bare GET is direct-egress (proxies=None)"). CORRECTED by
   the critique: job 914 does NOT belong on this list — it shipped **GBP** correctly;
   the USD seen in its logs was Shopify Markets geo-localization of the egress, not the
   tuple contract, and it never reached the output. This is a template/code bug, not
   knowledge. Seeding the lesson (s08) is the workaround; filing the bug is the fix.
3. **Probe should report listing-body renderability, not URL reachability** (jobs 498,
   588, 589, 605, 963) — the probe returns 200 on SPA shells that contain zero item
   data, which misleads strategy selection.
4. **Tester field-coverage gate counts bookkeeping keys** (`url/src_url/
   status_code/scraped_at`) toward coverage, so PASS ≠ schema-complete (batches 05/07:
   ~20-24 of 34 jobs shipped without requested `product name`/`availability`).
   Also: tester verdicts do not gate execution (275, 808, 960 shipped at
   NEEDS_FIXES/FAIL).
5. **Cross-job contamination guards** — shared-browser session drift and `_trash`
   reuse put foreign-site data into shipped outputs (963, 959, 918 vs 919); the
   writers that added `document.title`/URL assertions caught it, the rest did not.
6. **`create_skill` creation race** (jobs 910/916 both tried to create
   magento-detection) — worth a look before any Tier 3 seeding.

None of these are in scope without owner approval; they are listed because the harvest
surfaced them repeatedly and they are cheap to fix relative to the wall-clock they burn.

---

## 5. Critique summary (fresh agent, not a harvester)

Full text: `_harvest/seeds/critique.md`. The critique agent had no part in harvesting.

**Reconciliation: PASS.** Queue ids == manifest == `jobs/` contents exactly (408, 0
missing, 0 extra, all parse, all 16 keys). One spec deviation, in the harmless
direction: 376/408 files are strictly inside the 1-3 lessons / 1-3 pitfalls band — 12
carry 4 lessons, 19 carry 4 pitfalls (over-stuffing, not loss), and 2 (jobs 296, 316)
carry 0 pitfalls while still holding 2 usable lessons each.

**Accuracy: 57 of 59 live-verified claims matched prod exactly (96.6%), 2 PARTLY, 0
WRONG**, across 12 random jobs (`random.seed(43)`), 20 prod GETs, 0 auth failures. The
2 PARTLY verdicts are harvest-field conventions, not false facts: job 665's
`platform_detected="shopify_hydrogen"` is a log-supported refinement of the API's
`shopify`, and job 947's `item_count=5` carries the larger of two disclosed numbers
(shipped file says 4). Honesty sweep across all 408: 50 jobs under-delivered their
`firstn` scope, 30 state it explicitly with the number, 20 imply it, **0 are silent**.

**Seed verdicts: 5 KEEP, 6 FIX (applied), 2 DROP (salvaged).** The FIX list was five
citation defects out of ~250 citations — job ids attached to the wrong fact: s03
(757 is Amazon.es, not amazon.sa), s05 (173 has no Gatsby on it), s07 (485 is
made-in-china; Hydro Flask is 408), s08 (914 shipped GBP — the USD was Shopify Markets
geo-currency, so the tuple-contract bullet now cites 611 where the writer comment
proves it; 674 is Harvey Nichols, not Calvin Klein), s10 (brand list uncited). The
critique's note on s08 is the one that matters most: two of the defects taught a wrong
*root cause*, which is worse than a wrong number. All of them are now fixed in the
seed files and re-verified against the local harvest.

**Biggest weakness per the critique — reach, not accuracy.** Only 5 skills are
auto-injected (`_PLATFORM_SKILLS`, hard-coded), and the richest content lands on dead
readers. This plan's Tier structure is the honest answer to that, and §4.1 is the
decision that would change it.

**Two extra findings the critique contributed:**
- sfcc-detection currently renders **nothing** (see §3.4) — so s01 is a strict
  improvement, not an eviction.
- Job 910's harvest got the theme right (`hooya`, from the static asset path) when the
  run's own tester called it "Luma" — now a rule in s11.

**Coverage holes closed by this plan:** Shopline folded into s02 (live reach),
Kibo seeded as s14 (live reach), NetSuite SCA seeded as s15. Left unseeded on purpose:
SAP/Hybris (needs a new skill AND has a genuinely distinct shape — microdata-not-JSON-LD
on 171, Spartacus SSR JSON-LD on 13; a candidate for a future wave, listed in §6),
BigCommerce/WooCommerce/WebSphere/Inditex (confirmations or <3 jobs), Fanatics-style
one-offs already owned by s07/s10.

---

## 6. Honest gaps in the harvest

1. **Technique vocabulary is uncontrolled.** Workers wrote descriptive technique
   strings (1,794 distinct), so technique-frequency counts are approximate; the
   cluster index (17 clusters) is a retrieval aid, not a taxonomy.
2. **Cardinality deviation, disclosed by the critique:** 376/408 files are strictly
   inside the 1-3 lessons / 1-3 pitfalls spec band (12 files carry 4 lessons, 19 carry
   4 pitfalls, 2 — jobs 296 and 316 — carry 0 pitfalls). Never in the direction of lost
   information, and job 296 still holds 2 usable lessons.
3. **Two structured-field conventions are imperfect:** `platform_detected` is sometimes
   a log-supported refinement rather than the live API value (job 665
   `shopify_hydrogen` vs `shopify`), and `output_quality.item_count` can carry the
   larger of two disclosed numbers (job 947: 5 vs the shipped file's 4). Anything
   joining the harvest to prod on those two fields should expect that.
4. **The 3 non-completed queue rows** (failed/cancelled/captcha) were harvested from
   their earlier SUCCESS, so their final-state failure detail is thinner than the
   completed jobs.
5. **`output_preview` is a preview** (3 records), not the full output file; item
   counts come from `run_history.records` / `output_preview.count`, which disagree
   with each other on some jobs (that disagreement is itself a documented finding, and
   is s12's theme).
6. **Per-surface transport claims** (PDP http vs listing browser) rest on the probe
   and execution logs of ~30 jobs — directionally strong, not exhaustively verified.
7. **Platforms still without a seed:** SAP/Hybris (7-10 jobs) is the real one —
   Spartacus serves SSR HTML with full Product JSON-LD despite being Angular (13), some
   Hybris storefronts carry NO Product JSON-LD but complete schema.org MICRODATA
   (171), Swarovski yields only to `uc_chrome` (150). It needs a new skill and
   inherits the reach problem, so it is a future-wave candidate, not a wave-43 seed.
   BigCommerce (7), WebSphere (5), Inditex (2), WooCommerce (2) were assessed by the
   critique and rightly skipped (confirmations, or <3 jobs, or already-owned lessons).
8. **Jobs in the harvest window themselves created skills** (batch-11 reports
   `magento-detection` ×2, `thg-ingenuity-detection`) that are NOT visible on prod's
   `/learnt-skills/` — either the creations failed, went to another store, or the page
   is not the FM truth. Unresolved; **this is now a hard pre-condition for s11** (§3.2).
9. **Thin seeds are thin on purpose:** s14 and s15 rest on 2 jobs each. They are
   included because the mechanism is stated identically in both jobs (and s14's skill
   has zero sections today); if the owner prefers a 3-job minimum, drop s15 first.

---

## 7. APPROVAL-REQUIRED

**Nothing has been written to any skills file. Nothing will be, until the owner
approves.**

Decision needed from the owner, in order:

1. **Tier 1 appends** (s01 sfcc-detection, s02 shopify-detection,
   s03 amazon-detection, s14 kibo-detection) — pure FM writes, no code change, no
   redeploy, and all four target skills are live-injected. Approve as-is, approve a
   subset, or drop.
2. **Tier 2 appends** (s05-s10) — same mechanics, but the knowledge is only reachable
   via `load_skill`. Approve for the record, or hold until an injection change exists.
3. **Tier 3 new skills** (s11 magento-detection, s12 output-schema-integrity,
   s15 netsuite-detection) — need `create_skill` + remain dead readers unless §4.1 is
   also approved. s11 additionally requires the FM create-vs-append pre-condition to be
   resolved first (§3.2). Highest value: s11.
4. **The render-window trade (§3.4)**: on sfcc/navigation-patterns/playwright-
   navigation the append strictly improves what reaches the prompt (today: nothing);
   on anti-bot-handling and jsonld-extraction it swaps the currently-rendered lesson
   for the seed. Accepting that swap is part of approving those two appends.
5. **§4 code changes** (injection map, tuple contract, probe renderability, tester
   coverage gate, contamination guards, creation race) — separate decisions, each
   implying a prod redeploy in a quiet window. The harvest only claims they are worth
   looking at; the evidence is cited per job.

Nothing is written until the owner says so. The executor's checklist, if approved:
bodies verbatim from `_harvest/seeds/s*.md` (**Body:** blocks), Tier 1 → Tier 2 →
Tier 3, verify each return value, then re-fetch `/learnt-skills/` and
`render_learned_sections(...)` per target to confirm what the writer will actually see.

---

## 8. Resume / re-run notes

- `_harvest/manifest.json` is the state of record (`done`/`unreadable`/`remaining`,
  reconciled to 408/0/0).
- `_harvest/tools/digest.py` (read-only prod fetch), `reconcile.py` (manifest) and
  `aggregate.py` (grouping) are rerunnable. **`tools/write_seeds.py` is NOT the seed
  source of truth any more** — it holds the pre-critique draft bodies, and s14/s15 do
  not exist in it. The seed files themselves (`_harvest/seeds/s*.md`, with the critique
  fixes applied in place) are the only thing an executor should read bodies from;
  `seeds/_index.json` and `seeds/_source_jobs.json` were regenerated after the fixes.
- The harvest and the critique were read-only against prod throughout: GETs only,
  `sleep 0.7` between requests, max 3 workers in flight, cookie jar never displayed,
  0 auth failures and 0 redirects logged.
