# Plan — Preserve Nested Schema Structure (designed → critiqued)

> Status: **Planned, critiqued; not implemented.** 3 design agents + 3 adversarial critique agents.
> Scope (user-confirmed): when a JSON schema is given, preserve nested **structure** in the output (objects/arrays stay nested, not flattened); recursive prune to schema; **NO** type/required enforcement; **no** null-fill; manual field-chip jobs stay flat.
> Supersedes the "no nesting" caveat in [`intake-schema-upload.md`](./intake-schema-upload.md).

## TL;DR — honest verdict from the critique

**Nested preservation is achievable but BEST-EFFORT, not a guarantee.** The pipeline is a flat-field-name engine by documented design (`src/schema_validation.py:1-8,36-37`). The deterministic prune can only **filter** existing structure — it cannot **construct** nesting the scraper didn't emit. So nesting survives **only when the LLM (`code_writer`) emits it**; if it flattens (which the templates prime it to do — `templates/navigation_scraper.py:678`, `shopify_scraper.py:93`), the nested fields are lost — **the same data-loss that already happens today** under the flat prune. The change does not make this worse, but it does not fix it either.

With the critique fixes below, the change is **safe** (no new data loss, no crashes, byte-identical backward compat). Without the bigger enablers (nested analyzer step, nested-emitting templates, shape gate), nesting for **arbitrary** nested fields (address, specs) is unreliable — reliable only for `variants`-like fields that already have a dedicated analyzer section.

## The design (synthesized)

1. **`parse_nested_schema(raw)`** — new in `src/schema_validation.py` (beside `validate_user_schema`, which stays unchanged). Returns a nested tree (mirrors the internal `{name,type,…}` field shape, extended with `items`/`fields` for children). **CRITIQUE FIX:** must be **strict** — return `None` for *anything* flat (primitive properties, array-of-scalars, object-without-properties, internal/flat_map/array shapes, `$ref`). Only a true nested object/array-of-objects → tree. This is what backward compat hangs on.
2. **`ScrapeState.nested_schema`** — new optional field (`state.py:47`). No migration (checkpointer is `JsonPlusSerializer` blob; old resumed jobs get `None` → flat). Hydrated from `job.schema_text` in `_build_initial_state`.
3. **`tasks.py:513-519` branch** — **CRITIQUE FIX:** gate on `nested_schema **and** _target_fields` (schema-text-without-chips must NOT enter the nested branch). When nested → `content_type_config["fields"]` preserves types/children (do **not** force `type:"text"`); else → today's byte-identical flat. **CRITIQUE FIX:** keep `required:True` on leaf fields (or change `_build_content_type_context`'s "Core fields" filter at `subagents.py:140-145`) so the "Core fields to expect" prompt line doesn't disappear for nested jobs.
4. **`code_writer` surfacing** — new `_nested_schema_section` in `build_code_writer_message` (`subagents.py:~3081`) + one paragraph in `.opencode/agents/code-writer.md` ("emit each field in the shape shown; don't flatten children into top-level keys"). `product_analyzer` stays flat (correct — it maps top-level names).
5. **Recursive prune** — `prune_record_to_schema` + `_prune_value` in `src/content_types.py` (leaf module; no import cycle). `_prune_output_to_schema` gains `nested_schema=None` → recursive when present, flat when absent. Applied at finalization (`tasks.py:868`, **timing-safe** — post `route_after_testing`/`code_tester`/`field_confirmation`) + sample (display-only **copy** in `_format_output_products`, never mutates the on-disk file).
6. **Unchanged**: `normalize_fields`, `validate_coverage`, `route_after_testing`, the variants allowlist, `target_fields` (still the flat top-level enforcement driver).

## Critique findings that MUST be fixed in-PR

| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| C1 | BLOCKER | `_build_content_type_context` "Core fields to expect" line disappears (nested sets `required:False`) | keep `required:True` on leaves, or filter "all schema fields when explicit schema" |
| C2 | BLOCKER | `parse_nested_schema` must be **strict** (flat→None) or backward-compat breaks | strict discriminator: only nested object/array-of-objects → tree |
| C3 | HIGH (silent) | type-mismatch crash: schema=object, value=`"N/A"` → `AttributeError` swallowed by `except` → enforcement silently off | `isinstance(value, dict\|list)` guards in `_prune_value`; **log** on the except, don't swallow silently |
| C4 | HIGH | two admission sources drift (`nested_schema` vs flat `target_fields`) on dashboard-edit/re-run | gate nested branch on `nested_schema AND target_fields`; top-level admission stays `target_fields`-driven (single source of truth) |
| C5 | MEDIUM | empty `{}` records kept as ghosts; `Site.fields_extracted` first-record-only | decide empty-record policy explicitly; all-records change-detection for the `pruned` rewrite |
| C6 | LOW | tuple-form `items` falls through to leaf | guard `items if isinstance(items, dict) else None` |

**Confirmed safe by critique:** record count stable (only keys/branches filtered, never records); bookkeeping survives at top level (if threaded); idempotent; sample-vs-final no conflict; no checkpoint migration; no import cycle; pre-prune readers (`route_after_testing`, `code_tester`) unaffected.

## Honest limitations (retain)

- **Best-effort nesting**: preserved + inner-pruned when the LLM emits nested; **lost** when `code_writer` flattens (same as today's flat prune). Not a guarantee.
- **Arbitrary nested fields unreliable**: `product_analyzer` has no per-child analysis step for address/specs (only `variants`). `code_writer` gets the shape but not child selectors → shape-correct-but-thin output for non-variants nesting.
- **No type/required enforcement**; **no null-fill** (absent fields omitted).

## Two implementation paths

**Path A — Best-effort nesting (contained, ~the design above + fixes C1–C6).**
Safe, no new data loss, backward compatible. Nesting works when the LLM cooperates (reliable for variants-class fields; unreliable for arbitrary). Ship behind the existing behavior (no flag needed — manual/flat jobs byte-identical).

**Path B — Reliable nesting (larger effort).**
Add the enablers the critique identified as prerequisites for a *guarantee*: (1) a nested analyzer step in `product-analyzer.md` (per-child selectors for arbitrary nested fields, not just variants); (2) templates that emit nested structures; (3) a **shape-assertion gate** in `code_tester` (reject/fix a flattened scraper before handoff); (4) reconcile `target_fields` vs `nested_schema` across all 5 consumers. This is the "8–10 coordinated changes" `intake-schema-upload.md` warns about.

## Recommendation

Ship the **safe field-name sample conformance first** (the Phase-1 flat design: display-only, in-memory prune of the approval sample so it matches the final output at the field-name level — zero data-loss risk, contained to `field_confirmation.py`). That directly answers "does the sample conform to the schema" safely.

Then pursue nesting deliberately: **Path A** if best-effort is acceptable (with fixes C1–C6, clearly labeled best-effort); **Path B** if you want it reliable. Do **not** ship the raw recursive prune without C1–C4 — it would silently disable enforcement on type-mismatches and drift on edits.

## Verification (when implemented)
- Unit tests: `parse_nested_schema` strict-discriminator (all flat shapes → None); `_prune_value` type guards (object/scalar, array/scalar, tuple items); record-count stability; bookkeeping survival; empty-schema → flat.
- E2e: a `variants`-class schema job → output preserves `variants:[{…}]` + inner-prunes to schema; a manual-fields job → byte-identical to today; a flat-pasted schema → flat (None).
- Regression: adameve/books/wikipedia-style jobs unaffected.
