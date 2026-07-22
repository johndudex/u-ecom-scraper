# Code Writer Prompt Simplification — Design Plan

> **Status:** APPROVED for implementation. A/B test before switching.
> Created 2026-07-21.

## Problem

`code_writer` is the dominant wall-clock bottleneck for complex sites. It hits
the 15-min per-phase cap on myntra, americaneagle, and dystaffing. Root causes:

1. **Prompt bloat**: 503-line system prompt + ~927-line message builder =
   ~1200-1600 line combined payload. The LLM reads all of this PLUS a
   1077-line template → context fills → truncation → scratchpad loss → retry.
2. **Redundancy**: the system prompt re-teaches what the templates already
   implement (error handling, rate limiting, pagination, output format,
   field extraction recipes). Two sources of truth → LLM wastes calls
   reconciling contradictions.
3. **Retry churn**: testing fails first attempt → regenerate from scratch →
   double the time. The self-test loop (Slice 4, already added) helps but
   can't compensate for the prompt's size.

## Current state

| Artifact | File | Lines |
|----------|------|-------|
| System prompt | `.opencode/agents/code-writer.md` | 503 |
| Message builder | `webapp/agents/subagents.py:build_code_writer_message` | ~927 |
| Combined payload per heavy job | — | ~1200-1600 |
| Template (read by code_writer) | `templates/http_navigation_scraper.py` | 1077 |
| Truncation budget (`_truncate_messages`) | `subagents.py:497` | 180,000 chars |

## Proposed structure (4 sections)

### Section 1 — Workflow (strict, ~10 lines)

```
## ⭐ Self-Test Loop (you have a run_scraper tool — use it)
After you write_file the scraper, immediately self-test:
run_scraper(args=["--sample", "--input", "input_urls.json"]). Read the output/traceback.
edit_file a targeted fix if it failed or returned 0 items. Max 3 runs.

## Workflow (strict, in order)
1. read_file the template named in §2.
2. write_file workspace/{slug}/scraper_draft.py — substitute the field map (§3)
   into the template's extraction functions. Keep the template's structure,
   waits, pagination, discovery, and output code intact.
3. run_scraper --sample. If output is empty or a traceback is shown,
   edit_file a targeted fix and re-run. Max 3 run_scraper calls.
4. Stop. Do not read analysis JSONs (§3 is complete). Do not read reference scrapers.
```

### Section 2 — Strategy Contract (deterministic, ~10 lines)

Pretty-printed from `scraper_analysis.json` (already computed by `_derive_strategy`):

```
## Strategy Contract
- Strategy: {strategy}
- Template: templates/{template_file}
- Data model: {single_phase | two_phase_browser | api_loop | embedded_json | ssr_div_list}
- Stealth: {none | cloak_via_navigate}
- Proxy: {none | datacenter | residential}
- Listing URL (if two_phase/ssr_div_list): {working_url}
```

### Section 3 — Field Map (already exists, ~6-7K chars)

Reuses `_summarize_product_analysis` (subagents.py:125-206) which already caps
to 15 fields (core + top non-core). Each field shows: `method`, `selector/path`,
`fallback`, `sample`.

```
## Field Map
title: method=jsonld, path=Product.name, fallback=h1, sample="Heart Throbber..."
price: method=css, selector=".price", sample="$89.99"
availability: method=jsonld, path=Product.availability, fallback=[data-stock]
... (≤15 fields)
```

### Section 4 — Output Contract (~8 lines)

```
## Output Contract
- Save scraper to: workspace/{slug}/scraper_draft.py
- Output JSON key: {output_key} — drop items missing title + a core field
- Required argparse: --input, --urls, --sample (5 items), --limit
- --sample MUST use URLs already in input_urls.json (skip Phase 1 discovery)
```

**Total new system prompt: ~150 lines. Total new message builder: ~250 lines.**

## What gets cut

### From the system prompt (`.opencode/agents/code-writer.md`)

| Section | Lines | Why safe to cut |
|---------|-------|-----------------|
| Template Selection table (144-194) | ~50 | Already deterministic via `_decide_strategy` + `template_hint` in the builder |
| HTTP Navigation Pattern + cloak notes (196-218) | ~25 | The template implements `_navigate()` + cloak. One line in §2 suffices |
| "Your Tasks 1-8" (254-411) | ~160 | Error handling, rate limiting, logging, output format, variants, field recipes — ALL already in the templates |
| input_urls.json section (413-426) | ~15 | Duplicates lines 43-86 |
| Code style + quality checks (439-492) | ~55 | Template demonstrates style; `ast.parse` enforces parseability |
| **Total cut from system prompt** | **~305** | **503 → ~150 lines (after keeping Self-Test + Workflow + Output Contract)** |

### From the message builder (`build_code_writer_message`)

| Section | Lines | Why safe to cut |
|---------|-------|-----------------|
| 8 conditional section builders (api_section, navigation_section, embedded_json_section, etc.) | ~600 | Collapse into the Strategy Contract dict (§2, pretty-printed) |
| SeleniumBase section (always injected) | ~50 | Only inject when `mechanism in (stealth_browser, seleniumbase_uc)` |
| Detailed two-phase discovery text | ~100 | One line: "data model: two_phase_browser, listing URL: {url}" |
| Detailed API pagination text | ~100 | One line: "data model: api_loop" |
| Anti-bot cloak playbook | ~50 | One line in §2: "stealth: cloak_via_navigate" |
| **Total cut from builder** | **~900** | **~927 → ~250 lines** |

## What stays

- The Self-Test Loop directive (Slice 4 — `run_scraper` before handoff).
- The Strategy Contract (deterministically computed, not LLM-guessed).
- The Field Map (`_summarize_product_analysis` — already lean, 15 fields max).
- The Output Contract (minimal — save path, output key, argparse, sample rule).
- Conditional sections that are TRULY conditional (SeleniumBase for UC-mode only;
  anti-bot cloak note only when detected).

## Why this helps speed

1. **Smaller context per LLM call** — ~150 lines (was 503) → the LLM processes
   less → each call is faster.
2. **Fewer contradictions** — one source of truth (the template) + one
   substitution map (the field map) = clear. Currently the prompt + template
   give conflicting instructions (e.g., prompt says "NO pagination" then admits
   "this applies to url_list only; navigation uses two-phase" — contradiction
   that wastes LLM calls).
3. **The template does the heavy lifting** — code_writer ADAPTS a template, not
   writes from scratch. The prompt should tell it WHAT to substitute (field map),
   not HOW to write a scraper (the template shows how).
4. **The self-test loop catches bugs inside the agent** — fewer cross-agent
   retries (code_tester → code_writer regeneration cycles).

## Risks + mitigations

| Risk | Mitigation |
|------|------------|
| LLM loses guidance for edge cases (soft-404, image extraction, variant handling) | The template already encodes these patterns. The LLM adapts the template; it doesn't need the prompt to re-explain them. |
| A strategy/template combo the prompt used to cover is now uncovered | The builder still injects conditional sections for rare cases (SeleniumBase, anti-bot). Only the constant/boilerplate is cut. |
| First-attempt quality drops (fewer rules = more mistakes) | The self-test loop (`run_scraper`) compensates: bugs are caught + fixed inside the agent before handoff. |
| Regression on currently-passing sites | Ship alongside the old prompt behind a flag; A/B test before switching (see below). |

## A/B test plan (before switching)

1. Keep the old prompt as `code-writer-v1.md` (rename the current file).
2. Write the new prompt as `code-writer.md`.
3. Add a settings flag `CODE_WRITER_PROMPT_VERSION` (default "v2", fallback "v1").
4. Run locumtenens + aya + adameve + dystaffing with both versions.
5. Compare: wall-clock time, retry count, first-attempt pass rate, output item count.
6. Switch to v2 only if it's faster + same-or-better quality on ALL four sites.

## Implementation steps (sequenced)

1. **Write the new `code-writer.md`** (~150 lines) based on the 4-section structure above.
2. **Refactor `build_code_writer_message`** (~250 lines): collapse the 8 conditional
   section builders into a pretty-printed Strategy Contract + the existing
   `_summarize_product_analysis` Field Map + minimal Output Contract.
3. **Rename old prompt** to `code-writer-v1.md`; keep as fallback.
4. **Add settings flag** `CODE_WRITER_PROMPT_VERSION`.
5. **A/B test** on locumtenens + aya + adameve + dystaffing.
6. **Switch** (or revert if regression).

## Expected impact

- **Wall-clock**: ~40-60% reduction in code_writer time (from ~400-880s to
  ~200-400s) due to smaller context + fewer LLM iterations.
- **Retry rate**: lower (self-test + clearer instructions = higher first-attempt
  quality).
- **Context budget**: ~150 lines (was 503) → the agent's own reads/writes have
  more room before truncation → fewer scratchpad-loss loops.
- **Sites unblocked**: dystaffing (code_writer cap), myntra (faster),
  americaneagle (faster), any future complex site.

## File references

- `.opencode/agents/code-writer.md` — current system prompt (503 lines)
- `webapp/agents/subagents.py:1909-2832` — `build_code_writer_message` (927 lines)
- `webapp/agents/subagents.py:125-206` — `_summarize_product_analysis` (reuse as-is)
- `webapp/agents/subagents.py:497` — `_truncate_messages` budget (180K)
- `webapp/agents/graph.py:2236` — `_invoke_code_writer` node
- `webapp/agents/tools/__init__.py:38-46` — code_writer tool set (includes run_scraper)
- `templates/http_navigation_scraper.py` — 1077-line template (the heavy lifting)
- `templates/requests_scraper.py` — simpler template
- `templates/api_scraper.py` — API template
- `.opencode/agents/code-writer.md:254-260` — Self-Test Loop (Slice 4, already added)
