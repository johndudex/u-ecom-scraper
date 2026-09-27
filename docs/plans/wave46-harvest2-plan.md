# Wave-46 — Harvest-2: correcting and completing the wave-43 extraction

Status: PLAN, awaiting owner approval. Nothing has been written to any FM.
Date: 2026-09-28. Supersedes nothing; extends wave-43's 13 seeds.

## 0. What this is

Three read-only audit agents (funnel census, band-cap source audit, family
prospectors) re-examined all 408 harvested jobs to answer the owner's
challenge: "out of 400+ jobs you got only so few skills". Verdict: the
concern was **justified, but the loss was in the funnel, not the cap**.

Measured funnel: 408 jobs → **2,354 distilled entries** (1,160 lessons +
1,194 pitfalls; clustering dropped nothing) → 13 seeds citing 134 jobs →
**~1,584 entries (67%) unrepresented**. Orphan rate is FLAT across job
richness bands (52% inside over-cap files vs ~53% in the control), so the
1-3/job band was not the mechanism: the 13 seeds were each scoped to one
platform or one theme, and six whole clusters had no home at all.

Recoverable, evidence-backed, behavior-changing findings after dedup:
**~45-50 distinct items**, of which the highest-value are corrections to
knowledge that is LIVE and WRONG today.

## 1. Highest priority — live File Master guidance is wrong

These teach active harm and are one `append` each (no code, no deploy):

| # | Live teaching today | Wrong because | Evidence |
|---|---|---|---|
| C1 | jsonld-extraction learned section: `if isinstance(offers, list): offers = offers[0]` generalized to offers | `offers[0]` is ARBITRARY when offers are per-variant; ships default-variant price / joined strings / CTA text | 676, 687, 256, 591 |
| C2 | navigation-patterns soft-404 section scans body for "unavailable"/"discontinued" phrases | commerce PDPs legitimately contain "sold out" copy; live products get classified dead | 678, 607 |
| C3 | s06 seed (now in prod FM + repo): "Microdata (itemprop) appears only on SFCC commerce layers" | 12 jobs show microdata on Hybris, WebSphere, PHP-custom, more | 171, 542, 692, 858, 892 |
| C4 | s01 seed: stale-availability quote attributed to job 967 | the text belongs to job **954** (chloe-com); 967 has no such claim | s01 vs 954/967 |

C1/C2 = corrective appends to prod FM + local FM + repo baselines.
C3/C4 = fix the seed source text, then the same three copies.

## 2. New cross-cutting skill — `variant-availability` (strongest family)

~50 distinct jobs, 8+ platforms, one coherent rule the writer never sees:
**availability/price/size are per-VARIANT fields; single scalars are lossy
summaries, and every cheap proxy lies.** Key content (job ids in
_harvest/seeds/ drafts to be written): per-variant scoping (926, 713, 122),
source-rank by distance from the commerce layer (954, 716, 705, 908),
conflict is normal — resolve by documented precedence (408, 601, 358),
"out of stock" is real data — never scrub (678, 607, 912), variant identity
lives in the URL incl. query string (908, 122, 137, 873, 800).
Proposed injection: **second cross-cutting reader** alongside
output-schema-integrity (code change, rides the merge; see §6).

## 3. Price/currency contract — sections, NOT a new skill

A standalone price skill would be a grab-bag; split by audience:
- **output-schema-integrity section A (live reader):** currency is an
  independent output field; TLD/path/locale do not determine it — read a
  named node and verify by recomputation (968 USD-vs-IDR, 716 GoPro,
  699 amazon.co.jp, 309/318 SFCC `?lang=`, 679 recompute MSRP×1.1).
- **output-schema-integrity section B (live reader):** emit price as a
  NUMBER with currency separate (25 jobs shipped `'$59.95'` strings past a
  PASSing tester); which-node-is-current precedence (685, 897, 640);
  zero/placeholder guards are load-bearing (362, 405, 920).
- **s02 body gets one generalized line:** price unit is per-NODE not
  per-payload (non-Shopify instances: Hybris 269, SFCC 785, DoorDash 960).
- A5 is half a harness defect: the wave-25 `_norm_price` normalizer reaches
  `price` but not `original_price` (509 shipped both forms in one row) —
  filed in §7, not seeded.

## 4. New platform skills (the homeless families)

| skill | jobs | anchor evidence |
|---|---|---|
| `sap-hybris-detection` | 8 core +2 | 269 `__SRG_PRODUCT_DATA__` + `$0.01 priceType:FROM` placeholder; 171 microdata-only; 150/328 uc_chrome-only + cloak on both surfaces; 603 stale promo JSON-LD |
| `bigcommerce-detection` | 7 | 664 zero-JSON-LD → OG+`BCData`; 64 `search.php?section=product` full-catalog shortcut; 284/852 headless → RSC-escaped JSON-LD; `cdn11.bigcommerce.com` tell |
| `thg-ingenuity-detection` (RECREATE) | 3 | 915 Dermstore `static.thcdn.com`+`csp.thehut.net`; `hasVariant[].offers` lost to stale VERIFIED-EMPTY flag |
| `fanatics-commerce-detection` (RECREATE) | 3 | 792 PrivacyWall interstitial + same-rung-blocked-then-passed; 545 `offers[0].priceSpecification.price`; 747 cloak-no-proxy |
| Websphere/Aurora | 5 | SECTION in navigation-patterns: `/wcsstore/*StorefrontAssetStore`, dead-products-ship-200 (810), `invAvailablityJSON` per-size truth (601, 542) |
| Inditex | 2 | SECTION in akamai: itxrest grammar + `pelement` (246, 873) |

Note: THG + fanatics were minted by the runtime during the harvest window
and **do not exist on prod FM today** (see §8). Injection-map additions:
`sap|hybris|spartacus`, `bigcommerce|stencil`, `thg`, `fanatics` keys.

## 5. Runner-integrity — output-schema-integrity section C (live reader)

From the band-cap audit's highest-confidence orphans (≥20 jobs affected):
record.url must equal the requested item (607 shipped a gift card for a
pajamas PDP); the delivered artifact must be the one the tester validated
(675/679/610); strategy label must carry the verified rung (682/283/320);
browser 429 is infra, never a strategy-ladder trigger (676/675/609);
`site_analysis.json` absence must hard-fail the step (682/283/293, one job
lost 846s); re-verify "no pagination" under the real scraper (542: probe
said carousel-only, live run found 148 URLs over `?No=`).

## 6. Injection reach (the structural ceiling, needs a merge decision)

Live writer channels today: 8 platform keys + output-schema-integrity.
Tier-2 skills (navigation-patterns, jsonld-extraction, anti-bot,
proxy-config, playwright-navigation, akamai) are `load_skill`-only —
everything seeded there is a durable record with near-zero behavior reach.
Proposed code change (TDD, rides this merge): cross-cutting reader list
becomes `{output-schema-integrity, variant-availability}` + **mode-gated
navigation-patterns** (inject only when input_mode ∈ navigation/list_page/
search_term). Alternative: accept record-only for Tier-2. Default rec:
make the change; it is ~15 lines + tests in `_platform_distillation`.

## 7. Harness defect ledger (code fixes; each its own decision + deploy)

1. Analyzer regex-in-JSON escape corruption (`\.`) invalidated
   product_analysis.json on **16 jobs** — validate/normalize artifact writes.
2. Enforced `from src.discovery import` spliced inside parens → SyntaxError
   (8 jobs: 137, 332, 333, 363, 375, 399, 405, 411).
3. `src.page_analysis` import drags Playwright into pure-HTTP dispatch →
   crash (217, 230).
4. Template default `Accept-Encoding` 307/403s TLS rungs (650) — one line.
5. cwd-relative `--input` (409, 411, 663).
6. `_norm_price` not applied to `original_price` (509, 260).
7. Strategy ladder re-picks the identical failed strategy (609).
8. Writer/tester workspace interleave muddies evidence (676, 310/311
   cross-job bleed variant).
9. Later 0-record run clobbers a completed run's output pointer (320).
10. `--discover-only` runs the full Phase-1 walk before any capped probe →
    600s timeout, invocation lost (597).
11. Empty `platform_detected` silently disables injection (300) — pair
    with the wave-45 URL-evidence hints.

## 8. RCA — VERIFIED 2026-09-28: nothing vanished; nothing was ever created

Prod ToolCallLog for jobs 915 and 792 (read-only `/jobs/<id>/tool-calls/`)
contains **zero** `create_new_skill` / `learn_skill` rows and **zero**
`nav_skill_review` agent rows (both are url_list/PDP jobs — the review
node that carries the skill-write tools doesn't run for them). The
"Skills created: …" text is **skill-learner prose** inside
`learning_report.json` — skill_learner deliberately has NO skill-write
tools (only nav_skill_review carries `get_skill_write_tools`); its report
is a PROPOSAL document whose phrasing impersonates an action. The
proposal itself DOES persist: `_invoke_skill_learner` copies
`learning_report.json` → FM `scrapers/{slug}/analysis/` on SUCCESS
(graph.py:9865). The harvest distiller trusted report prose over the
ToolCallLog — same error class as "never credit product_count=0".

Consequences: no write-path bug exists; §4's "RECREATE" framing stands
(the skills genuinely don't exist and wave-46 creates them deliberately);
harvest rule going forward — only ToolCallLog rows count as mints;
wave-44's used/written/proposed Skills panel disambiguates exactly this
but is local-only, undeployed. Optional follow-up (not wave-46): teach
the skill_learner prompt to write "proposed", never "created".

## 9. Execution order (after approval)

1. §1 corrections C1-C4 (FM appends + source fixes; local immediately,
   prod via the proven UI path).
2. §2 `variant-availability` skill + §4 four platform skills + §3/§5
   sections — seed drafts written from job evidence, then local FM, then
   repo baseline (same carriers as wave-45c).
3. §6 injection change TDD'd + §7 ledger triaged with owner.
4. §8 RCA as its own investigation.
5. Full suite + prod FM verification; deploy rides the next owner merge.
