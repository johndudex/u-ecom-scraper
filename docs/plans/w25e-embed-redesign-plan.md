# W25-e Plan — writer embed redesign (bounded draft embed + draft map + failure pre-seed)

**Status: PLAN rev-3 — FINAL. Round 1 (1 BLOCKER + 6 MAJOR, D5-overlap adjudicated as RESHAPE) +
round 2 (cross-plan interference: step-2 seed-reservation fix, call-site parity scope, blast-list
corrections) incorporated. No code before approval. Deferred from wave-25, re-prioritized
by the job-587 RCA (2026-09-14).**
**Evidence base: working-tree read of `webapp/agents/subagents.py`, `webapp/agents/graph.py`,
`webapp/agents/tools/filesystem_tools.py`, `webapp/config/settings.py`, `.opencode/agents/code-writer.md`,
`templates/*.py`; live in-container measurement of the writer system prompt (django container, this branch);
langgraph 1.2.11 / langgraph-prebuilt 1.1.0 source inspection inside the container.**
**Companion to `docs/plans/wave32-fix-plan.md` — its D-W4/D-W1/D-W8 are the trigger; this plan implements
what wave-32 §4 explicitly deferred ("D-W4 context diet … must pair with D1's extra rounds").**

---

## 1. The mechanism as it stands (measured, not assumed)

### 1.1 Where the embed is built and what it costs

`_embed_template` (`webapp/agents/subagents.py:975-1002`) appends the **entire** `template_code` string to
the writer system prompt, behind a 6-line instruction frame plus a do-not-read note (W24-6). The only call
site is `_build_agent` (`subagents.py:1029`), reached via `create_code_writer(site_slug, template_code)`
(`subagents.py:785-786`). In-container measurement of the real assembled prompt:

| component | chars |
|---|---|
| `.opencode/agents/code-writer.md` (338 lines) | 18,871 |
| skill blurb (`_append_skill_descriptions`, 17 skills, gated on toolset) | 3,511 |
| frame + note around the embed | ~640 |
| **shopify template (17,763 B)** → full system prompt | **38,748** |
| **navigation template (53,453 B)** → full system prompt | **74,147** |
| **http_navigation template (85,850 B)** → full system prompt | **104,756** |
| **587's 119,420 B draft** → full system prompt | **~142,400** |

Full template sizes on disk (`templates/`): dagster 7,033 · ssr_div_list 15,939 · shopify 17,763 ·
undetected_chromedriver 20,877 · api 24,055 · requests 30,259 · playwright 38,399 · navigation 53,453 ·
http_navigation 85,850 bytes. **7 of 9 templates are already below 40K; only the two navigation families
exceed it, and they are exactly the families whose drafts reach 100K+.**

> **Rev-2 correction (critique finding 10):** the table's assembled totals sat below the component sum
> (`code-writer.md` is **19,167** chars on disk, not 18,871). Conclusions unchanged; during E0's
> implementation the table is re-measured once in-container and the row base published as a single
> consistent number rather than trusting rev-1's arithmetic.

### 1.2 Why the budget never sees it (D-E1)

The writer is the `create_react_agent` path (`subagents.py:1077-1079`):

```python
agent = create_react_agent(llm, tools=tools, prompt=system_prompt, pre_model_hook=_truncate_messages)
```

Verified in-container against langgraph-prebuilt 1.1.0 `_get_prompt_runnable`: a **string** `prompt` is
turned into a `SystemMessage` and prepended by a `RunnableCallable` that runs **after** `_get_model_input_state`
— i.e. after `pre_model_hook`. So `_truncate_messages` (`subagents.py:843-972`) only ever sees `messages`;
its step-2 budget arithmetic (`:934`, `budget = max_chars - sum(system_msgs)`) subtracts a list that is
**always empty** on this path. `LLM_TRUNCATION_MAX_CHARS` = 180,000 (`settings.py:312`) therefore bounds the
conversation while a ~142K system prompt rides **outside** it on every turn, untrimmed and uncounted. A
587-shaped cycle runs its first turn ~80% consumed before the writer reads a single tool result.

### 1.3 The embed rides four windows per cycle (D-E2)

One "cycle" is not one invocation. All of these re-send the same system prompt:

| window | site | tries |
|---|---|---|
| main writer | `graph.py:5781` → invoke `:5794-5797` | 1 |
| syntax fixer | `_fix_scraper_syntax` `graph.py:5172-5228`, invoke `:5218` | up to 3 |
| CLI-contract fixer | `_enforce_cli_contract` `graph.py:5286-5350`, invoke `:5337` | up to 2 |
| draft finisher (W30-2) | `_run_draft_finisher` `graph.py:5410-5528`, embed at **`:5470`**, invoke `:5475-5483` | 1 |

The two fixers reuse the **same agent object** passed in from the main window, so they inherit whatever
embed mode the main invocation got — good news: fixing the main path fixes them for free. The finisher is a
**separate** `create_code_writer` call and is the *most* budget-starved window of the four (`allow_activity_extension=False`,
fixed `WRITER_FINISH_TIMEOUT`); it currently embeds the full draft text like every other window.

### 1.4 Why growth is structurally favored (D-E3)

Edit-over-write (`graph.py:5736-5755`) re-reads the whole on-disk draft into `_template_code` whenever
`last_writer_template` matches and `draft_parses` holds. The writer's in-context copy is then frozen at
invocation start while three deterministic patchers (`_patch_scraper_output_filter` `graph.py:538-634`,
`_enforce_discovery_import` `:637-692`, `_enforce_env_discovery_gate` `:693-747`), `_safe_splice_write`
(`:489`) and `_fix_scraper_syntax` mutate the file **after** the window closes. On the next turn `edit_file`
demands an exact, unique `old_string` (`filesystem_tools.py:505-516`) against a copy that may be stale and
(in bounded mode) partially unseen — so the reliable way to "make the edit land" is a full `write_file`.
Full rewrites of an 86K base are what grew 587's draft 107→119→124KB. Both existing progress gates
(byte-identity `graph.py:6120-6155`, step-death-AND-unchanged via `_noop_should_escalate` `:6156-6164`)
are blind to pure growth, which is wave-32 D-W8/D4's finding, not re-litigated here.

### 1.5 What the writer has to navigate with today

- `read_file` untargeted on a large file → head+tail 20K+20K + SNIPPED notice (`filesystem_tools.py:44-45`, `:403-423`).
- `read_file(line=, num_lines=)` → bounded window, default 400 lines / max 1200 (`:345-387`), MAX_READ_CHARS 50,000 (`:341`).
- `search_content` → regex hits with line numbers (`:582`+).
- No structural index of the draft exists anywhere. The writer must spend a read (24K in-context each, per
  `_tool_msg_cap`, `settings.py:318`) to learn what the file contains.
- `.opencode/agents/code-writer.md:40` still instructs: "`read_file` the template named in the Strategy
  Contract" — the exact move W24-6's embed note exists to prevent, and which a bounded embed makes
  actively harmful.

### 1.6 Rounds math (co-design constraint with wave-32 D1)

`AGENT_RECURSION_MAP["code_writer"] = 120` (`graph.py:1044-1047`) ÷ 3 super-steps/round (pre_model_hook →
agent → tools) ≈ **40 tool rounds**. Wave-32 D1 raises to `_WRITER_ROUNDS * 3 + 2` with `_WRITER_ROUNDS = 50`.
A writer that must *read to navigate* spends 1 round per 400-line window; a writer holding a map + a
failure-targeted excerpt spends ~0-1. The diet and the round raise are the same budget viewed from two sides:
**D1 buys rounds; W25-e removes the need to spend them.** Shipping the diet without D1 risks trading
round-blindness for read-starvation; shipping D1 without the diet just makes longer read-spirals possible.

---

## 2. Defect inventory (W25-e scope only; wave-32 IDs referenced, not duplicated)

| ID | One-line statement | Anchor |
|----|--------------------|--------|
| D-E1 | The template/draft embed lives in the system prompt, which `pre_model_hook` never sees — `LLM_TRUNCATION_MAX_CHARS` bounds the conversation but not the ~142K prompt; langgraph-prebuilt 1.1.0 applies a string `prompt` *after* the hook | `subagents.py:975-1002, 1029, 1077-1079`; `subagents.py:843-972` (`:934` budget math) |
| D-E2 | The same full embed is re-sent by four windows per cycle (main ×1, syntax ×3, CLI ×2, finisher ×1) — up to 7 full-price prompts per cycle | `graph.py:5781, 5218, 5337, 5470` |
| D-E3 | Edit-over-write re-reads the entire draft into the embed (`:5736-5755`), so embed size tracks draft size; combined with exact-unique `old_string` against a stale in-context copy, full rewrite is the locally-reliable move → growth is structurally favored | `graph.py:5736-5755`; `filesystem_tools.py:505-516`; patchers `graph.py:538-747` |
| D-E4 | No structural index of the draft exists; the writer pays a 24K read to learn what the file contains, and head+tail gives it 40K of undifferentiated text | `filesystem_tools.py:403-423`; `settings.py:318` |
| D-E5 | Failure evidence that already exists in state (`test_report.remediation.target/fields`, `subagents.py:2305-2308`) is never used to point the writer at the relevant draft region — every cycle re-derives "where do I edit" from scratch | `subagents.py:2305-2308`; `graph.py:5765-5779` (signal stamped, nothing injected) |
| D-E6 | Three text contracts promise a full in-prompt template and will become lies under a bounded embed: `code-writer.md:40` ("read_file the template"), `graph.py:5242` ("the full template is in your system prompt"), `graph.py:5275` ("VERBATIM from the template in your system prompt") | `.opencode/agents/code-writer.md:40`; `graph.py:5242, 5275` |
| D-E7 | No measurement: neither the assembled system-prompt size nor per-turn input size is logged anywhere, so the prompt-cache question (§6 Q1) is unanswerable from prod logs today | no log site exists in `subagents.py:_build_agent` / `_truncate_messages` |
| D-E8 | **Refuted / not a defect:** `_fix_scraper_syntax` and `_enforce_cli_contract` share the main invocation's agent object, so they do **not** re-build the embed. No separate fix needed — they inherit whatever mode the main path chose. Listed so critique doesn't re-derive it. | `graph.py:5219, 5338` (same `agent`) |

---

## 3. The design

### 3.1 Embed shape (three modes, one renderer)

New module `webapp/agents/draft_context.py`, pure functions, no Django imports (unit-testable without setup):

```
render_writer_embed(code: str, *, mode: "full"|"bounded") -> str
build_draft_map(code: str) -> str          # ast-based index; "" when unparseable
classify_embed(code: str, *, eow_active: bool) -> "full"|"bounded"
```

> **Rev-2 ownership split (critique adjudication: RESHAPE).** The module, `render_writer_embed`, the
> head/tail carve, the `embed_mode=` kwarg and the EOW-conditional wiring at both call sites land in
> **wave-32 32-b (W32-D5, reshaped)** — with `CODE_WRITER_EMBED_MODE` defaulting to `"full"` so D5 stays
> genuinely `[SAFE]`. **W25-e E1 only extends the module** (map + classify + staleness contract text),
> and **E2a flips the default** to `"bounded"` behind its e2e gate. Nothing in this plan re-creates
> what D5 ships; any inline carve left in `subagents.py` by D5's implementation is deleted when E1 lands
> (module = the single renderer).

**full** = today's `_embed_template` output byte-for-byte (kept as the small-input path so
`tests/test_wave24_writer_prompt.py` stays green — it calls `_embed_template` directly and pins the note text;
`_embed_template` becomes the full-mode renderer behind `render_writer_embed`).

**bounded** (the new mode) =
1. the same 6-line instruction frame + do-not-read note (reused from `_embed_template`, so the
   "do NOT read_file templates/*.py" contract holds in both modes), plus
2. **`_EMBED_HEAD` (20,000 chars)** of the code, mirroring `filesystem_tools.py:44` — the imports, config
   constants, and transport helpers, which is where the site-specific work mostly lives,
3. **`_EMBED_TAIL` (20,000 chars)**, mirroring `filesystem_tools.py:45` — for every template family this
   covers `main()` and the argparse/CLI-contract surface,
4. **`_EMBED_MAP_MAX_CHARS` (6,000) ast map**: every top-level `def`/`class`/ALL-CAPS assignment as
   `start_line-end_line  name(signature)  — first docstring line (≤80 chars)`, in file order, with the
   snipped-byte accounting ("chars 20,000-61,850 not shown; lines 512-1,511 indexed above"),
5. an explicit **staleness + navigation contract**: "the middle is NOT in your context. To edit it:
   `search_content` for the symbol, then `read_file(path, line=<hit line>)`. If `edit_file` reports
   `old_string not found`, your copy is stale — re-read the window, do not rewrite the file."
   (`line=` is a LINE number — the W26-4 contract, `filesystem_tools.py:345-387`; wave-32 A4 adds
   `line=` to the tool-args trail so these reads become visible.)

Bounded budget worst case ≈ 20K + 20K + 6K + 0.7K frame ≈ **47K chars**, vs 142K for 587 — a 3× cut on the
exact job that triggered this, and a 27K *increase* for nothing smaller than a 47K draft/template.

**Degradation (the 587 trap).** `build_draft_map` runs `ast.parse`; on `SyntaxError` (or any exception) it
returns `""` and the renderer emits head+tail raw with a one-line "map unavailable (draft does not parse —
fix the parse error first; the syntax fixer window will carry the exact line)". It must never raise: an
unparseable draft is a supported input state, not a crash. Note EOW (`graph.py:5745`) already gates on
`draft_parses`, so a broken draft reaches the *pristine-template* path only — but the degradation is
unconditional anyway.

### 3.2 Threshold: 40,000 chars, measured

`CODE_WRITER_EMBED_FULL_MAX_CHARS = 40_000` (`settings.py`, new). Below it → full embed; at/above → bounded.

- 7 of 9 templates fall through to full (§1.1 table): a 17K shopify draft stays fully visible, as the brief
  requires.
- Only navigation (53K) and http_navigation (86K) go bounded on a first cycle — and those are the only two
  families that produce 100K+ drafts under EOW, so the bounded path is where the money is.
- 40K sits just above the largest sub-40K template (playwright, 38,399) so patch growth
  (`_patch_scraper_output_filter` adds ~0.9K) can't push a "small" template into bounded mode after one
  deterministic patch. Flipping at 40K rather than 47K also keeps the *full* path's worst case (40K + 640
  frame ≈ 41K) comfortably inside a single turn.

### 3.3 Making the embed count against the budget — extend truncation, don't move the embed

Two options were analyzed; **Option B (extend truncation to the system embed) is chosen.**

- **Option A — move the embed into a message.** Putting it in the *first* HumanMessage is a no-op for the
  budget: `_truncate_messages` explicitly exempts the seed (`subagents.py:881`, `:894-895` — "NEVER cap the
  seed (task/strategy/field-map)"), so the embed would ride a different exemption instead. Putting it in a
  *second* message makes it droppable by step-2's oldest-drop (`:932`) mid-invocation — the writer silently
  loses its base — and it breaks the "always present in full" property `_embed_template`'s docstring
  documents as the reason the embed lives where it does. It also re-prices provider prefix caching: as a
  system-prompt suffix the embed sits at a fixed byte offset for every window in the cycle; as message N it
  shifts with the seed size, which differs between the main window and each fixer/finisher window.
- **Option B — per-agent pre-model hook.** `_build_agent` already knows `template_code`; replace the shared
  `pre_model_hook=_truncate_messages` (`subagents.py:1078`) with a **closure built at agent-build time**:
  `pre_model_hook=_make_pre_model_hook(embed_chars=len(embed_block))`. The hook subtracts `embed_chars`
  from the step-2 budget and includes it in the log line. No ContextVar is needed — and none would be
  reliable here: the existing ContextVar signals (`_writer_fix_cycle`, `subagents.py:1375-1377`,
  `_writer_codefix_cycle`, `:1531-1533`) are read at *agent-build* time precisely because per-call reads
  inside the invoke thread are not guaranteed to inherit the node thread's context.

Signature change is backward-compatible: `_make_pre_model_hook(embed_chars=0)` returns a callable with the
identical behavior, and `_truncate_messages(input_dict)` keeps its current single-arg contract —
`tests/test_tool_message_truncation.py:45` and `webapp/tests/test_truncation.py:15` both call it positionally
and must stay green.

### 3.4 Failure-targeted pre-seed (retry cycles only)

On an EOW fix cycle, deterministically inline the relevant draft region into the **seed message** (not the
system prompt — the seed is the per-cycle task spec, and the two fixer sub-windows reuse the same agent
without re-running this logic):

- **mapping failures**: `test_report.remediation.target == "mapping"` with `fields` (`subagents.py:2305-2308`
  already parses exactly this) → locate the extraction region by ast: the function(s) whose body mentions the
  failed field names, plus the `EXTRACT_*`/`_populate_from_jsonld`-class constants they feed.
- **discovery / Phase-1 failures**: `remediation.target == "discovery"`, or issues/discovery-coverage
  naming discovery/pagination → the `discover_*` / `_extract_item_links` / `_get_next_page_url` functions
  plus `main()`'s argparse block (the region the CLI contract and the no-source force-FAIL path both point at).
- Cap: `CODE_WRITER_PRESEED_MAX_CHARS = 12_000` per cycle, rendered with file line ranges so the writer can
  `read_file(line=)` around it. Deterministic selection from the existing report vocabulary; no LLM call.
  Accounting note: this rides the truncation-exempt seed on purpose — 12K bounded replaces 1-3 × 24K
  read_file round-trips, and it is the mechanism that makes *bounded* embeds safe rather than blind.

### 3.5 Touchpoints with existing gates (cited, not re-designed)

- **W24-4 codefix write guard** (`subagents.py:1546-1598`): unchanged. A bounded embed *reduces* bare
  full-rewrite attempts (the writer can't regenerate what it can't see); the guard stays as the mechanical
  backstop. `full_rewrite_reason` remains the only legal escape.
- **W24-5 draft-first nudge** (`subagents.py:1402-1446`): unchanged; fix cycles stand down exactly as today.
- **W25-b anti-read nudge** (`subagents.py:1478-1517`): threshold 10 stays. Bounded embeds plus pre-seed
  should keep real reads at 0-3; the nudge remains the tripwire if a writer pages anyway.
- **W25-c step-death accounting** (`graph.py:5393-5407`, `:5812-5819`) and **W26-5 escalation**
  (`:6156-6164`): unchanged; this plan is upstream prevention for the deaths they count.
- **W24-4/A2 no-op byte-identity gate** (`graph.py:6120-6155`): mechanically untouched — it hashes the
  on-disk draft against `last_tested_draft_fp`, neither of which the embed affects.
- **W30-2 finisher** (`graph.py:5410-5528`): its `create_code_writer(..., template_code=_draft_text)` at
  `:5470` goes through the same bounded renderer (`W25-E4`). Its seed already says the draft is "already in
  your context as the base" (`:5457-5459`) — under a bounded embed that sentence must say "head, tail and map".
- **wave-32 D3 known-good freeze** (`graph.py:5859-5877` snapshot; `draft_safety.py:179-215` restore): no
  interaction — the embed is rebuilt from disk at each `create_code_writer`, so a D3 restore is picked up
  next cycle by construction. Recorded so nobody adds a cache.
- **wave-32 D4 `[DRAFT-BLOAT]`** (log-only): W25-E0's embed-size row is the per-invocation half of the same
  story; both read from the same measured sizes.
- **wave-32 A4** (`line=`/`offset=` in tool-args summaries): the map's line numbers are only actionable if
  the trail shows them — sequencing dependency, not a code dependency.

---

## 4. The fixes

**Conventions** per `wave32-fix-plan.md` §3: failing test FIRST, then minimal implementation, then the named
regression set. `[SAFE]` = additive/strictly-narrowing; `[RISKY]` = changes what the model sees (decision
boundary) → extra audit step. Tests in `tests/` (root, run from `/app`).

### W25-E0 `[SAFE]` Measure the embed and the per-turn input (lands FIRST; ships inside 25e-a)

- Change: (a) `_build_agent` logs one line when an embed is present:
  `[WRITER-EMBED] agent=code_writer mode=full|bounded template={file|draft} embed={n:,} system_prompt={n:,} chars`;
  (b) `_make_pre_model_hook` (E3's closure, but E0 can log from `_truncate_messages` alone) logs at INFO the
  per-turn total: messages chars + `embed_chars` + count.
- **SessionLog row — plumbing (rev-2):** the per-invocation row is written **by the node, after the
  invoke returns** — `job_id` comes from `state` (`graph.py:5533`, where `_invoke_code_writer` already
  reads it), the sizes come from the same `_build_agent`-computed numbers passed back on the agent
  object (attribute set at build time), and the write goes through the same SessionLog helper the
  phase/heartbeat rows use. Nothing is scraped from thread ContextVars inside the agent thread — that
  path is exactly what wave-32 D2 proved unreliable.
- RED: `tests/test_wave25e_embed_redesign.py::test_embed_size_is_logged_and_persisted` (fails: no such row/log today).
- Blast: none — additive logging. `tests/test_wave24_writer_prompt.py`, `tests/test_draft_first_nudge.py` unaffected.
- Rollback: delete the log lines + the node-side row write.
- **Why first:** it answers §6 Q1 (prompt caching) on the very next drive *before* the risky change,
  and it is the baseline E2 is judged against. §7.1's "on prod before E2" is amended for tonight:
  **this is a LOCAL-ONLY wave** (no deploy while the wave-31 PR is pending), so E0 lands with 25e-a
  and the measurement accrues from the local e2e drives + the next prod deploy.

### W25-E1 `[SAFE]` `draft_context.py` — ast map + classification + staleness contract (extends W32-D5's module)

- Change: the module now ships in wave-32 32-b (see §3.1 rev-2 note). E1 **extends** it:
  `build_draft_map` (ast map per §3.1.4, `CODE_WRITER_EMBED_MAP_MAX_CHARS=6_000` — the only new
  setting), `classify_embed(code, eow_active=...)` factoring the threshold decision D5 inlined at the
  call sites (call sites switch to the helper — no behavior change), and the bounded renderer gains
  the map block + the staleness/navigation contract text (§3.1.5). Any carve logic D5 left inline in
  `subagents.py` is deleted here — the module is the single renderer. Map degradation: `""` on
  SyntaxError, renderer prints the one-line "map unavailable" note; never raises.
- RED: `::test_map_lists_top_level_defs_with_line_spans`, `::test_unparseable_code_drops_map_keeps_head_tail`
  (SyntaxError input → no raise, no map, head+tail present), `::test_bounded_embed_carves_head_tail_and_map`,
  `::test_classify_embed_matches_d5_call_site_behavior` (golden parity at **both** call sites —
  round-2 finding B1: the main site's condition is `_eow_active and len>=40K`, the finisher's is
  `len>=40K` alone, per wave-32 D5's rev-4 wiring),
  `::test_bounded_embed_names_staleness_contract` (search_content → read_file(line=) → do-not-rewrite text).
  (The carve/byte-equality/degradation RED tests themselves are D5's and land in 32-b.)
- Blast: none behavioral (default is still `"full"` until E2a flips it).
  `tests/test_wave32_eow_diet.py` (D5's tests) must pass unmodified.
- Rollback: revert to D5's renderer (head+tail only).

### W25-E2 `[RISKY]` Activate the bounded embed — staged in two sub-steps

> **Rev-2 (adjudication):** the *wiring* is W32-D5's (module + `embed_mode=` kwarg + EOW-conditional
> call sites, kill-switch defaulting to `"full"`). E2 no longer builds any of that — it is a **flip**
> item plus the audit loop around it.

- **E2a (retry cycles):** flip `CODE_WRITER_EMBED_MODE` default `"full"` → `"bounded"` in `settings.py`
  (one line + RED test). With the flip live, bounded mode applies exactly when D5's condition holds —
  `_eow_active` **and** `len(code) >= 40_000` — i.e. retry cycles on large drafts. First-cycle pristine
  templates keep today's full embed: the writer adapting an 86K template it has never seen is the one
  scenario where bounded mode could genuinely cost quality, deferred to E2b behind evidence. The
  kill-switch is read **lazily** (`getattr(settings, "CODE_WRITER_EMBED_MODE", "full")` at call time,
  not an import-time constant) so an env flip acts without a code change or deploy-order coupling.
  Gate: 25e-a green + the §7.4 e2e (run with `CODE_WRITER_EMBED_MODE=bounded` exported — the gate
  validates the behavior, the flip then makes it the default).
- **E2b (all templates ≥ threshold):** change D5's condition from `_eow_active and len>=40K` to
  size-only. Gated on E0's measurement + one local e2e of an `http_navigation` site completing
  first-cycle-bounded.
- RED: `::test_embed_mode_default_is_bounded_after_flip` (E2a — **the sole default-asserting test,
  round-2 finding B6**: D5's `test_embed_mode_kill_switch_lazy_read` pins lazy-read mechanics only
  and must never pin the default value, so the flip cannot break it),
  `::test_small_eow_draft_still_full_embed` (17K draft → full),
  `::test_first_cycle_still_full_embed` (E2a boundary).
- Blast: `tests/test_wave32_eow_diet.py` (D5's wiring tests — must pass unmodified; they pin explicit
  `embed_mode=`, not the default), `tests/test_wave30_draft_finisher.py:106-145` (seeding assertions),
  `tests/test_draft_first_nudge.py:200-220` (node stamping region of `_invoke_code_writer`).
- Audit step before E2b: confirm via E0 rows that bounded-mode cycles' read_file counts stay ≤3/invocation
  on real jobs.
- Rollback: `CODE_WRITER_EMBED_MODE=full` (env) → zero code path change; reverting the one-line default
  restores pre-wave behavior exactly.

### W25-E3 `[SAFE]` Embed-aware truncation budget (rev-2: BLOCKER fix — both gates consult the embed)

> **Rev-2 (critique BLOCKER):** rev-1 only subtracted `embed_chars` from **step-2's** budget. Step-1
> (`subagents.py:~928` — the early-return "everything fits" check) compares the message total against
> raw `max_chars` and returns **before** the embed is ever subtracted: 150K of messages + a 47K embed
> = 197K actual prompt sails through step-1 untouched under rev-1's spec. The gate that enforces
> nothing enforces nothing.

- Change: `subagents.py` — `_make_pre_model_hook(embed_chars: int = 0)`; `create_react_agent(...,
  pre_model_hook=_make_pre_model_hook(embed_chars))` at `:1078`. Inside:
  - **step-1's early-return gate (`:921` gate, `:927` return) becomes
    `total <= max_chars - embed_chars`** (clamped at `max(max_chars - embed_chars, 0)`) — the embed
    counts before the "fits" decision, not after;
  - step-2 budget becomes `max(max_chars - embed_chars - sum(system_msgs) - _clen(seed), 0)`
    — **round-2 finding C1: today's code already reserves the seed at `:936`
    (`budget -= _clen(seed)`); the fixture's GREEN depends on preserving that reservation**
    (without it, 133K budget vs ≤133K of non-seed messages drops nothing and the "must TRIM"
    assertion fails post-implementation);
  - both log lines (and step-1's "still under budget" message) carry `embed_chars` and the combined
    total, so the 180K budget is visibly respected end-to-end.
  `_truncate_messages` itself is kept as the zero-embed instance (`_make_pre_model_hook(0)`) so both
  existing test call sites stay valid.
- RED: `::test_under_budget_gate_subtracts_embed` (**the blocker**: 150K of messages + 47K embed must
  TRIM — under rev-1's spec it early-returns untrimmed), `::test_budget_subtracts_embed_chars`,
  `::test_zero_embed_matches_legacy_truncation` (golden-path equality with today's output),
  `::test_truncate_messages_single_arg_still_works` (pins the existing tests' contract).
- Blast: `tests/test_tool_message_truncation.py`, `webapp/tests/test_truncation.py` — **must pass
  unmodified** (every existing fixture runs at `embed_chars=0`, where the new gate degenerates to the
  old one). Acknowledged boundary change: `webapp/tests/test_truncation.py:44
  test_under_budget_keeps_everything` pins "under budget → untouched"; with an embed present the same
  input can now trim, which is the intended fix — pinned by the new blocker test, not by editing the
  old one.
- Rollback: revert the `pre_model_hook=` argument to `_truncate_messages`.

### W25-E4 `[SAFE]` Finisher seed honesty under bounded embeds (wiring already D5's)

- Change: the finisher's `embed_mode=` wiring lands with W32-D5 (`graph.py:5470`, second call site).
  E4 is the **text + parity residue**: (a) the finisher seed at `:5457-5459` stops saying the draft is
  "already in your context as the base" and instead names head/tail/map **only when the embed mode is
  bounded** (mode-agnostic phrasing — under `"full"` the old sentence stays true); (b) no second
  mechanism: E4 must not add its own embed selection — anything the finisher needs routes through the
  same `embed_mode=` kwarg D5 introduced; (c) `_fix_scraper_syntax`/`_enforce_cli_contract` need no code
  change (same agent object, D-E8) — only the two text contracts in E6.
- **Pinned seed tokens (rev-2):** `tests/test_wave30_draft_finisher.py:122-145` pins the finisher seed
  to carry `http_navigation` (current strategy), `http_requests` (strategies_tried), `FINISH` (framing),
  `403` (tester feedback) and `adapt` (switch-or-justify order). The rewording preserves every one of
  these tokens — only the "already in your context" clause changes.
- RED: `::test_finisher_uses_same_embed_mode_as_main_path` (parity assertion over the kwarg D5 shipped),
  `::test_finisher_seed_names_map_not_full_context` (bounded mode), `::test_finisher_seed_tokens_survive`
  (the five pinned tokens present after rewording).
- Blast: `tests/test_wave30_draft_finisher.py` (seed-text assertions at `:122-145`).
- Rollback: revert the seed string; the kwarg wiring is D5's rollback.

### W25-E5 `[SAFE]` Failure-targeted region pre-seed on EOW fix cycles

- **Splice point (rev-2):** the region block is **spliced into `messages[0].content`** (the seed
  HumanMessage) before invoke — NOT appended as a new message. The seed is the per-cycle task spec,
  it is the truncation-exempt element (`subagents.py:881`), and `_invoke_code_writer` is the only
  writer of it, so a marker-guarded splice is idempotent. An appended trailing message would (i) land
  after the final tool result, reading as a stray instruction mid-conversation, and (ii) be re-added
  on every retry construction of the same state.
- **Real triggers (rev-2 — rev-1 invented a `"discovery"` target that does not exist).** The
  remediation vocabulary in the code is exactly `{"mapping", "strategy", "scraper"}`
  (`route_after_testing.py:2021-2023` selects between `mapping` and `strategy`;
  `subagents.py:2307` reads `mapping`; `:2991` reads `scraper`):
  - **mapping arm:** `remediation.target == "mapping"` with `fields` (`subagents.py:2305-2308` already
    parses exactly this) → ast-locate the function(s) whose body mentions the failed field names plus
    the `EXTRACT_*`-class constants they feed.
  - **discovery-class arm:** `remediation.target == "strategy"`, or a deterministic zero-yield /
    discovery-coverage signal in the test report (the same conditions wave-32 E1/C1 route on) → the
    `discover_*` / `_extract_item_links` / `_get_next_page_url` functions plus `main()`'s argparse
    block. `target == "scraper"` gets no pre-seed (transport-level; the region is the whole file).
- Cap: `CODE_WRITER_PRESEED_MAX_CHARS = 12_000` per cycle, rendered with file line ranges so the writer
  can `read_file(line=)` around it. Deterministic selection from the existing report vocabulary; no LLM
  call. Empty report → empty block (first cycles unaffected). Accounting note: this rides the
  truncation-exempt seed on purpose — 12K bounded replaces 1-3 × 24K read_file round-trips, and it is
  the mechanism that makes *bounded* embeds safe rather than blind.
- RED: `::test_mapping_remediation_preseeds_field_region`, `::test_strategy_target_preseeds_discovery_region`,
  `::test_scraper_target_preseeds_nothing`, `::test_no_remediation_preseeds_nothing`,
  `::test_preseed_is_capped`, `::test_preseed_splice_is_idempotent` (double-invoke doesn't duplicate).
- Blast: `tests/test_wave24_cascade_honesty.py` (constructs `test_report` fixtures that flow into
  `_invoke_code_writer`'s seed via `build_code_writer_message` at `:124`/`:142`; assertions are about
  routing, but the seed content changes). **NOT in the blast set (round-2 verified):**
  `tests/test_wave21_remap_sample_swap.py` (rev-1 wrongly listed it — its fixtures never reach
  `_invoke_code_writer`'s seed) and `tests/test_wave26_cascade_honesty.py` (round-2 finding B9 —
  it is a source-contract grep test, no seed construction).
- Rollback: setting `CODE_WRITER_PRESEED_MAX_CHARS=0` disables (kill-switch), then revert.

### W25-E6 `[SAFE]` Stale-copy and text-contract honesty

- Change: (a) `filesystem_tools.py:507-510` — the `old_string not found` reply gains one sentence:
  "if this file was provided head+tail in your prompt, your copy may be stale — search_content, then
  read_file(path, line=…) the window, and retry the edit (do NOT rewrite the file)"; (b) the bounded
  embed's staleness clause (§3.1.5) is the same vocabulary; (c) `graph.py:5242` and `:5275` — the
  **final literal strings (rev-2) are mode-agnostic and never name a `templates/` path**: the fixer
  messages stop asserting "the full template is in your system prompt" (a lie under bounded) and stop
  implying the writer can open `templates/{file}` (it cannot — the writer container has no repo), and
  instead say the template's *shape* — imports, constants, the argparse/CLI surface — is in the prompt,
  with "re-read the specific window with `read_file(line=)` on the workspace draft when a region you
  need is elided". One string pair serves both modes (full embed = everything genuinely is in the
  prompt; bounded = the sentence still directs correctly); (d) `.opencode/agents/code-writer.md:40` —
  replace "read_file the template named in the Strategy Contract" with "the template is already in your
  prompt (in full below the threshold; head+tail+map above it); do not read it back".
- RED: `::test_edit_file_not_found_names_stale_copy`, `::test_contract_fix_message_does_not_promise_full_template`,
  `::test_fixer_messages_never_reference_templates_path`, `::test_writer_prompt_does_not_order_template_readfile`.
- Blast: `tests/test_wave24_writer_prompt.py`; F821 rejection-text tests in
  `tests/test_wave25_root_causes.py:316+` (shared filesystem_tools response vocabulary); the
  `.opencode/agents/code-writer.md` wording pinners — **rev-2 corrected list** (verified by reading each):
  `tests/test_cli_contract_prompt.py:241` (tokens `--listing-url`, `--fresh-discovery`, `--discover-only`,
  `--query`, `SCRAPER_LISTING_URL` — none touched, must stay green), `tests/test_job310_listing_priority.py:142`
  ("Zero-yield discovery must self-heal", `DEFAULT_LISTING_URL`), `tests/test_wave14_seed_contract.py:435`
  ("verification-scope", "same-host"), `tests/test_wave19_discovery_url_hygiene.py:234`
  ("protocol-relative", the `https://products/x` trap), `tests/test_wave25_root_causes.py:88`
  ("Prices are NUMBERS", `_norm_price`). **Also a reader, unaffected (round-2 finding C7):**
  `tests/test_wave20_writer_gotchas.py` (`:43`, `:126-133` — pins `input_urls.json`,
  `--fresh-discovery`, `json.loads`, `application/ld+json`; none touched by a line-40-only edit).
  **`tests/test_wave29_skills_loop.py:134` is NOT in the blast
  set** (rev-1 wrongly listed it — it greps the *site-analyzer* prompt about `load_skill`, not
  code-writer.md's template line). All five real pinners must pass unmodified — E6 edits line 40 only.
- Rollback: text-only; revert strings.

---

## 5. Explicitly deferred (named, not forgotten)

| Item | Why deferred | Revisit when |
|------|-------------|--------------|
| Bounded embed for **pristine** first-cycle templates (E2b) | The writer adapting an 86K template it cannot fully see is a real quality risk with no prod evidence yet; E2a captures ~all of the win (587 was pure retry-cycle) | After E0 rows show bounded-mode read counts ≤3/invocation across ≥3 retry-cycle drives |
| MAP as a **tool** (`draft_outline`) instead of an embed block | Costs a round per cycle (the thing D1 just made more expensive) and the embed version is free; only wins if maps grow past 6K | If real maps truncate at `CODE_WRITER_EMBED_MAP_MAX_CHARS` in E0 telemetry |
| Range-patch tool (`edit_lines(start,end,new)`) to kill the exact-unique-`old_string` constraint | New tool = new pydantic schema + guard + prompt surgery + F821-gate wiring; the staleness clause in E6 attacks the symptom cheaply | If E0 shows `old_string not found` replies still driving full rewrites |
| Template slimming (split http_navigation's 86K into a shared transport module) | Touches every template + the `src.discovery` import contract + codegen-contract audit; orthogonal to the embed | Own wave, with the codegen contract doc |
| Provider cache instrumentation at the HTTP layer (usage/billable-token capture) | Needs `llm.py` callback work and a place to put per-turn token counts; E0's char-level proxy answers the design question at 1/10 the cost | If E0 shows char counts don't predict turn latency |
| `code_tester` embed parity | Tester is on `create_agent` (no `pre_model_hook`, `subagents.py:789-791`) and never receives a template embed | Not a defect; re-check if the tester ever gets one |
| SummarizationMiddleware for the writer | Rejected again: it reintroduces the pre-wave-23 LLM call inside the cancellation path (see `_trunc_settings` docstring, `subagents.py:809-811`) | Never, unless the deterministic path is proven insufficient |

---

## 6. Open questions (measurement or prod access; none block the green light)

1. **Does the provider prompt-cache the ~142K system prompt?** Inv #2 of 587 ran ~28s/round vs 569's
   145-161s/turn — consistent with caching, also consistent with low-latency turns. If caching hits, E2's
   win is *rounds and budget honesty*, not latency; if it misses, latency drops too. Settled by E0: correlate
   `[WRITER-EMBED]` sizes with per-turn wall-clock in the same job.
2. Does `glm-5-turbo`'s gateway bill/429 on cached vs uncached prefix tokens differently? Affects whether
   E2 also cuts cost/rate-limit pressure, not just latency.
3. What is the real draft-size distribution on prod (not just 587's 107-124KB)? E0 rows accumulate this
   for free; it is the input to any future threshold re-tuning.
4. Does any template family carry its site-specific work *only* in the middle segment (lines that head+tail
   misses)? One-time read of the 2 bounded-mode families; if yes, the map's docstring lines must be chosen
   to surface it.
5. Whether wave-32 D1 lands with `_WRITER_ROUNDS = 50` as planned — E5's pre-seed sizing assumes rounds stay
   scarce; if D1 lands at 80+, E2b's risk shrinks and can ship earlier.

---

## 7. Sequencing

1. **W25-E0 lands first, inside 25e-a** — measurement only. Amended for tonight (rev-2): **this wave is
   LOCAL-ONLY** (no deploy while the wave-31 PR is pending), so E0's rows accrue from the local e2e
   drives and the next prod deploy, not from prod immediately.
2. **Wave-32 groups 32-a/32-b land first** — specifically **D5 (reshaped: the `draft_context.py` module
   + `embed_mode=` wiring + kill-switch defaulting `"full"`)**, D1 rounds + D2 tool-deadline, D3 freeze,
   A4 `line=` telemetry. W25-e depends on D1/D2 (a writer that must read a targeted window needs the
   rounds and the deadline to do it), **builds directly on D5's module and kwarg** (E1 extends it,
   E2a flips its default — no re-creation), benefits from A4 (map line numbers become visible in the
   trail), and is *complementary* to D3/D4 (D3 protects the bytes; W25-e shrinks the prompt that edits
   them). **Shared files (full list, round-2 finding B7):** `webapp/agents/graph.py` (`:5736-5801`
   D5 wiring → E1/E2a/E4; `:5859-5876` is D3's parse gate, untouched by this plan; `:5533` E0's
   job_id read); `webapp/agents/subagents.py` (D5: `:785-786/:975-1002/:1029`; E1 carve deletion;
   E3 `:921-972/:1078`; E0 `_build_agent` logging); `webapp/config/settings.py` (D5 and E1/E2a/E5 —
   knob names verified non-colliding); and `webapp/agents/draft_context.py` itself (created by D5,
   extended by E1 — by design, sequenced, not conflicting).
3. **Commit group 25e-a** `[SAFE]`: E0, E1, E3, E6 — plus deletion of any carve logic D5 left inline in
   `subagents.py` (module becomes the single renderer).
4. **Commit group 25e-b**: E2a `[RISKY]` (the settings-default flip), E4, E5 — after 25e-a is green and
   one local e2e (an `http_navigation` retry-cycle drive, e.g. a marimekko/587 replay, run with
   `CODE_WRITER_EMBED_MODE=bounded` exported) shows: embed row reports `mode=bounded`,
   read_file count ≤3 per writer invocation, draft parses, tester verdict honest, **no** step-budget death.
5. **E2b** only after §5's condition holds.
6. Per item: RED → GREEN → named blast set → `ruff check . ../src/` from `/app/webapp` (E4/E7/E9/F/I/UP)
   → full suites (root + `webapp/`) with the known pre-existing-failure list diffed (truncation non-seed-cap,
   admin visibility, oversized-offset).
7. Standing constraints unchanged: no langgraph topology change (`pre_model_hook` stays, only its closure
   factory changes), `LLM_ASYNC_EXECUTION` stays off, code_writer keeps `streaming=True`, no target-site
   fetches, TDD throughout. EB sync per standing rules after local green; deploy order django+celery →
   browser_service (unchanged by this plan, which touches no browser_service code).
