# Artifact-Corruption Fix Design — Five Candidates, Stress-Tested Against the Repo

**Scope:** the solution space only. The byte-level mechanism (why the LLM emits
bad bytes, where they enter the write path) is owned by
`docs/plans/artifact-corruption-rootcause.md` (parallel forensics agent) and is
deliberately not re-derived here.

**Corruption instances being defended against (given as context):**

| # | Site | Artifact | Class (see §0) |
|---|------|----------|----------------|
| 1 | sidley | `product_analysis.json` → unrepairable, renamed `.corrupt` | C2 — unescaped quote inside a string value |
| 2 | priceline (Railway) | `test_report.json` | C1 — literal control character inside a string |
| 3 | Railway job 10 | `product_analysis.json` | C2 — unescaped quotes inside an embedded JS snippet |

Local confirmation of #1: `workspace/sidley-com/product_analysis.json.corrupt`
exists on disk (33,849 bytes). `json.JSONDecodeError: Expecting ',' delimiter`
at char 30,983 — the LLM wrote `"offices": 25 offices with counts,` (unquoted
value, a C2/C4 hybrid). The file tail is intact
(`"confidence_score": 0.92\n}`) and **9 of 10 top-level keys with 36 field
mappings are recoverable by balanced-bracket truncation** — but the current
3-pass repair recovered nothing and renamed the whole file `.corrupt`
(`_fix_json_artifact`, `webapp/agents/graph.py:122`). That gap is one of the
inputs to §F4'/REPAIR-v2 below.

---

## 0. Corruption classes (vocabulary used throughout)

| Class | Example | `json.loads` error | Deterministically repairable? |
|-------|---------|--------------------|-------------------------------|
| **C1** control char | `"desc": "line1\nline2"` (literal 0x0A) | `Invalid control character` | **Yes** — `strict=False` parses it; re-dump escapes it. Lossless. |
| **C2** unescaped quote / unquoted scalar | `"a": he said "hi"` / `"offices": 25 offices with counts,` | `Expecting ',' delimiter` | **No** in general (string end is ambiguous). Recoverable only by **truncation salvage** (drop the damaged key, keep the parseable prefix). |
| **C3** truncation (token/stream limit, wall-clock kill) | `{"a": 1, "b":` | `Expecting value` | Partial — balanced re-close keeps the prefix. |
| **C4** bad escape | `"path": "C:\Users"` | `Invalid \escape` | **Yes** — regex pass 1 (already shipped). |
| **C5** fences/prose | ```` ```json{...}``` ```` or prose around JSON | `Expecting value` | **Yes** — strip fence, extract object. |
| **C6** wrong shape (valid JSON, bad schema) | `[1,2]`, `{"a": 1}` when a field map is required | parses clean | **Out of scope here** — handled by schema validation (`validate_coverage`, `validate_analysis`), not by the write path. |

**Efficiency constraint (user-stated):** no added LLM calls, no wall-clock
growth where avoidable. Every candidate is scored against this.

---

## Existing defenses (do not re-propose)

### D1 — `_fix_json_artifact` (post-hoc, shipped yesterday)
`webapp/agents/graph.py:122`. Called **only** from `product_analyzer`'s
`_run_budgeted_agent(... artifact_fix_fn=...)` at `graph.py:1921` — i.e. it
covers **one artifact from one agent**. Site-analyzer (`graph.py:1795`),
code_tester (`graph.py:3471`), cleanup, skill_learner have **no** repair hook.
Passes: (0) parse-if-valid; (1) bad-escape regex; (2) `raw_decode` salvage of
leading object; (3) coarse cut-and-reclose walking **down** from the end in
`len//200` steps trying only `"}"` / `"]}"`; then rename `.corrupt`.

**Two concrete defects found in D1 (relevant to the fix design):**
- Pass 3 only tries two hard-coded closers and steps by `len//200` — on the
  sidley file it found **no** cut point (verified by simulation) and fell to
  rename, despite 91% of the object being parseable with the correct bracket
  stack (`}}}}}`). A balanced-closer variant recovers 9/10 keys and 36 field
  mappings (verified). Pass 3 is weaker than intended.
- It runs **after** the agent has finished. When it renames `.corrupt`, the
  artifact is treated as missing and the phase goes to the budget/missing
  interrupts — a full agent re-run (up to 70 calls / 900 s wall clock) to
  recover data that a deterministic salvage could have kept.

### D2 — `build_code_writer_message` UNREADABLE branch (downstream loud-note)
`webapp/agents/subagents.py:3233`. Injects "PRODUCT ANALYSIS PRESENT BUT
UNREADABLE — VERIFY, DON'T GUESS" when `_summarize_product_analysis` yields `"`
but `state["product_analysis"]` is non-empty. This is a **prompt mitigation**,
not a fix; it only covers product_analysis → code_writer and only fires when
the state dict is non-empty-but-unsummarizable (a repaired-into-weird-shape
case), *not* when `_read_json_artifact` returned `{}` (corrupt file → state is
empty `dict`, which is falsy-adjacent — the branch checks `_pa_raw` truthiness).

### D3 — `tests/test_codegen_fixes.py::TestArtifactRepair`
Four cases: valid-untouched, bad-escape (C4), job-10 JS-snippet C2 salvage,
unrepairable → `.corrupt`. It tests D1 **as-is**, including the weak pass 3 —
so any repair change must extend, not contradict, this file.

### Existing silent-failure readers (why "downstream will notice" is FALSE)
`_read_json_artifact` (`graph.py:998`) returns `{}` on any parse error;
`_load_test_report` (`graph.py:548`) returns `None`;
`normalize_fields._load_analysis` and `validate_analysis._load_analysis`
return `None` → human interrupts; `retry_wrapper` logs and proceeds with
`{}`. Nothing panics; everything degrades quietly. This is why corruption
survives to bite later phases instead of failing fast at the write.

---

## F1 — VALIDATE-ON-WRITE (refuse + corrective error)

**Mechanism.** In `write_file` (`webapp/agents/tools/filesystem_tools.py:143`),
when `os.path.splitext(path)[1] == ".json"`, run `json.loads(content)` before
the write. On failure return a string error carrying `e.msg`, `e.lineno`,
`e.colno` and a short fix instruction ("escape control characters; quote
scalar values") instead of writing. The agent sees the ToolMessage and retries.

**Files touched.** `webapp/agents/tools/filesystem_tools.py` (write_file only;
edit_file optionally — see risk). Tests in `tests/test_codegen_fixes.py` or a
new `tests/test_write_validation.py`.

**Agent-visible behavior.** Tool returns an error where it previously returned
"Successfully wrote N characters". Agents already handle tool errors —
`apply_tool_error_catcher` (`subagents.py:882`, from `guards.py:301`) wraps
every tool so errors become messages, never crashes; and the v1 `ToolNode`
re-raises → catcher converts. So retry *is possible*.

**Cost.** One extra agent round-trip per caught corruption (one LLM call, ~2–5 s
per retry). No cost on clean writes (a `json.loads` of a ≤50 KB artifact is
sub-millisecond). **But the cost is unbounded in the failure mode that
matters:** a model that keeps emitting the same class of error burns
recursion budget until `GraphRecursionError`.

**Budget interaction (verified).** The effective cap is *not*
`AGENT_MAX_ITERATIONS` (`subagents.py:119` — that map is only read by the
playground, `tasks.py:1607`) nor the budgeted-agent `budget`/`budget_extended`
math (`graph.py:1567`, computed but never installed into `agent_cfg`). The real
cap is `AGENT_RECURSION_MAP` (`graph.py:667`: product_analyzer 200, code_tester
120, code_writer 120) applied by `_agent_config` (`graph.py:719`), plus the
900 s `_AGENT_INVOKE_TIMEOUT` wall clock (`graph.py:1382`). A refuse-loop on a
stubborn model therefore terminates via recursion error →
`GraphRecursionError` caught upstream → human_approval, or the 900 s timeout →
budget-exhausted path. Both are *survivable but expensive* (up to 15 min and a
human interrupt).

**Cap-then-fallback (mandatory if F1 ships alone).** Track refusals per agent
run (module-level counter keyed on the tool-context agent name,
`tools/context.py` already carries `agent_name`). After N=3 refusals for the
same path, **stop refusing**: write the bytes as-is (or as `.json` with a
logged warning) and let the post-hoc repair own it. This bounds the loop at
3 LLM calls and makes F1's worst case equal to today's behavior.

**False-positive / false-fix risk.** Refusal is safe — it writes nothing.
Two real false positives:
1. **`.json` files that aren't artifacts.** `input_urls.json` is
   written by *deterministic* code (`setup_workspace.py:160`,
   `graph.py:3255`) with `json.dump`, never by the LLM — unaffected.
   But the LLM *does* write `.json` scratch files occasionally; validation
   would correctly reject those too, which is fine/desirable.
2. **JSON5-ish output the model "knows" is readable** — trailing commas,
   comments. Refusing these is a feature, not a bug, as long as the error
   message names the character class.

**Coverage.** C1 ✅ (refused, agent escapes next round), C2 ✅ refused (agent
may fix, may loop), C3 ✅ refused, C4 ✅ refused, C5 ✅ refused. **F1 converts
every class into an agent-visible signal — but converts none of them into a
good file on its own.** Its unique value is the *error message*: it is the only
candidate that tells the model *what* it did wrong, which is what actually
reduces the future error rate.

**Verdict: strong, but only with the refusal cap, and only as a *signal*
layer.** Refuse-loop risk plus a bounded fallback is strictly better than
today only if the model retries correctly at least once — empirically plausible
for C1 (escape a char) and dicey for C2 (rewriting a big object).

---

## F2 — SANITIZE-ON-WRITE (parse-then-redump)

**Mechanism.** In `write_file`, for `.json` paths: (a) strip a
```` ```json … ```` fence if present (C5); (b) `json.loads(content,
strict=False)` — this accepts literal control chars in strings (verified
locally: `{"a": "line1\nline2"}` parses under `strict=False`); (c) on success,
write `json.dumps(parsed, indent=2, ensure_ascii=False)` — a **canonical,
guaranteed-strict-valid** serialization; (d) on failure, fall through to F1's
refuse path (or write raw, per the combination chosen).

**Files touched.** `webapp/agents/tools/filesystem_tools.py` only.

**Agent-visible behavior.** None. The write succeeds; the file on disk differs
byte-wise from what the model emitted (canonicalized), which is invisible to
the agent unless it `read_file`s its own artifact back — and when it does, the
file is *valid JSON*, so any re-read is strictly better.

**Cost.** Zero LLM calls, zero latency (one parse+dump of ≤50 KB). Best
efficiency profile of all candidates.

**Semantic-faithfulness check (the user's flagged risk).** Parse-then-redump is
a bijection on the **data model**: every value, key, ordering and nesting is
preserved; only the concrete escape spelling changes (`0x0A` → `\n`,
`\u003c`/`<` normalization, whitespace/indentation). Verified locally that
re-parse of the redump equals the parsed object. It does **not** invent,
drop, or reinterpret content. Two formatting caveats, both checked against the
repo:
- **Duplicate keys:** `json.loads` keeps the last; a model emitting a dup key
  silently loses the first. Already true of every downstream `json.load`, so
  no regression.
- **Non-string scalars preserved exactly**, including floats (repr round-trip
  is stable in `json.dumps`).

**Does anything downstream depend on formatting/key order/comments?** Checked
all readers: `_read_json_artifact`, `_load_test_report`,
`normalize_fields._load_analysis`, `validate_analysis._load_analysis`,
`field_confirmation.py` (3 reads), `navigate_skill_review.py:91`,
`retry_wrapper.py:111`, `_attach_discovery_coverage`, `_preserve_test_report`
(raw-byte copy — copies *canonical* bytes, fine), `setup_workspace`
`_restore_from_archive`, skill-learner preservation (`graph.py:3754`, raw
bytes). All go through `json.load`; **none** hash, diff, or column-index the
files. `src/skills_store` hashes only `SKILL.md` files, not artifacts. `.json`
artifacts are also copied raw to the File Master (`scrapers/{slug}/analysis/`)
— canonical form is an improvement there (stable diffs). **No dependency on
byte formatting exists.** Comments are not legal JSON anyway.

**False-positive / false-fix risk.**
- **Over-application to non-artifact `.json`.** Same note as F1; harmless
  (canonicalizing a model-written config file is fine, and deterministic
  writers don't go through this tool).
- **`strict=False` is *more* permissive than strict JSON in exactly one way**
  (control chars), and we then redump to strict — so the *output* is never
  looser than the input. No reader ever sees `strict=False` semantics.
- **The one real risk: C2.** Sanitization does *not* fix unescaped quotes — it
  can't (string boundaries are ambiguous) — so F2 alone leaves the sidley/job-10
  class untouched. F2 is a C1/C4/C5 killer and a C2 no-op.

**Coverage.** C1 ✅ (fully repaired, lossless), C4 ✅, C5 ✅, C3 ❌ (parse
fails), C2 ❌ (parse fails). **Zero agent round-trips for everything it
covers.**

**Verdict: ship it.** It is the only candidate with a perfect
cost-vs-coverage ratio on the classes it handles, and the priceline instance
(raw control character in `test_report.json`) is exactly C1.

---

## F3 — STRUCTURED-WRITE (tool-arg JSON, no free-text file content)

**Mechanism.** For pure-data artifacts, the LLM never writes file content as a
string. It calls a typed tool, e.g. `save_test_report(report: dict)`, and the
tool does `json.dump` — validity guaranteed by construction.

**The central claim, verified (not assumed).** "Tool-call args are parsed by
the provider's machinery, which already enforces valid JSON at the API
boundary" — **true but with a load-bearing exception.**
`langchain_openai.base._convert_dict_to_message` calls
`parse_tool_call` → `default_tool_parser`
(`langchain_core/messages/tool.py`), which does
`json.loads(raw_tool_call["function"]["arguments"])`; on `JSONDecodeError` the
call moves to **`invalid_tool_calls`**, not `tool_calls` (verified against
installed source: langchain-core 1.6.0 / langchain 1.3.16 / langgraph 1.2.11
in the running container). So yes — by the time args reach a tool they are a
parsed `dict`. **BUT** the guarantee is *parse-or-reject*, not parse-or-fix:
a malformed arg blob becomes an `InvalidToolCall`, which in this stack
(`ToolNode` + `apply_tool_error_catcher`) surfaces as an error the agent must
retry — the *same* refuse-loop dynamic as F1, one layer lower. And the args are
still LLM-emitted text: **the model can put an unescaped quote in a *string
value* inside the args exactly as it does today**; the difference is only that
failure is detected at the API boundary instead of on disk. GLM also has the
`v__` arg-prefix quirk (`_strip_v_prefix_from_tools`, `subagents.py:730`),
demonstrating that this boundary is already known-imperfect in this stack.
Structured-write therefore removes the *file* corruption but relocates the
*failure* to a retry loop; it does not remove it.

**Where it would apply.** `test_report.json` is written by `code_tester` via
`write_file` today (prompt `.opencode/agents/code-tester.md:62` mandates it),
and read by `_load_test_report` (`graph.py:548`) → state →
`route_after_testing`. Other pure-data candidates: `cleanup_report.json`,
`learning_report.json` / `nav_learning_report.json` (read by
`navigate_skill_review.py:91` and preserved raw to FM).

**What breaks.**
1. **New tool per artifact** (`save_test_report`, …) in
   `AGENT_TOOL_MAP`/`_get_tools_sync`, new prompts, new guards — a
   per-artifact maintenance tax forever.
2. **Mid-run iteration.** product-analyzer is explicitly instructed to
   "**Write product_analysis.json EARLY** … overwrite later if budget allows"
   (`.opencode/agents/product-analyzer.md:381`; same pattern in site-analyzer's
   WRITE EARLY). Structured-write can support overwrite, but the *iteration*
   contract (write partial → refine) must be re-specified per tool.
3. **Retry reads.** `build_code_tester_message` tells the agent to
   `read_file` the previous `test_report.json`
   (`subagents.py:3279`, `:3298`); keeping the file as the medium preserves
   that. A typed tool must still write the same file for these readers —
   fine, but now there are two write paths to keep consistent.
4. **Arg size.** A 20–30 KB `product_analysis` inside a tool-call argument is
   exactly the "tool_calls ballooning" driver the context-truncation work
   (`_truncate_messages`, `subagents.py:519`) fights. Structured-write makes
   big-arg writes *more* likely, not less.
5. **Provider variance.** Some providers chunk long args; partial-arg
   aggregation failures would produce a new, unrelated flakiness class.

**Cost.** Zero steady-state LLM calls, but the highest engineering cost and the
largest blast radius (prompts, tool map, guards, message builders, tests).

**Coverage.** C1/C2/C3/C4/C5 ✅ *for the artifacts migrated* (a corrupt arg
never reaches the file), at the price of retry loops on the same corruption;
C2 stays **unfixed at the source** (the model still writes the bad string; it
just fails earlier). Nothing for non-migrated artifacts.

**Verdict: not now.** It is the only candidate that addresses the *source*, but
its cost/risk profile (new tools × artifacts, arg-ballooning interaction,
prompt rewrites) is disproportionate when F2 already guarantees a valid file
for every class the parser can handle. Revisit only if forensics shows a class
that sanitization cannot parse and the refusal loop proves unable to teach the
model. **Exception worth carving out now:** none — `test_report.json`'s writers
are few but its readers (`_attach_discovery_coverage` mutating the dict, F19,
partner-sample hook) already operate on the parsed dict, so F2 gives it the
same guarantee for free.

---

## F4 — CANARY-THEN-READ (post-write verify + auto-repair in the tool)

**Mechanism.** `write_file` writes, then `json.loads`s what it wrote; on
failure it runs the existing 3-pass logic in place and logs; the agent never
sees a difference.

**Files touched.** `filesystem_tools.py` + import/refactor of the repair from
`graph.py` (currently module-private and hard-coded to
`workspace/{slug}/{filename}` — must be generalized to take an absolute path).

**Agent-visible behavior.** None.

**Cost.** Zero LLM calls. One parse per `.json` write (negligible).

**False-positive/false-fix risk — this is F4's problem.** Silent salvage means
the agent believes it wrote X while the file contains a *prefix* of X. For
`test_report.json`, a salvaged report that drops `overall_assessment` makes
`route_after_testing` see `FAIL` (`route_after_testing.py:433` defaults to
FAIL) → retry → eventually cleanup. Honest, but silent. For
`product_analysis.json`, a silently truncated field map flows into
`normalize_fields`/`validate_coverage` as a *smaller* map — the agent is never
told, and the gap may surface only as a coverage interrupt two phases later.
Silent truncation is precisely the "silently '{}'" failure mode the job-10
lesson says to avoid; F4 reintroduces it in a milder form. **If F4 ships, the
salvage must at minimum log loudly AND mutate the tool's return string**
(e.g. "Wrote N chars — NOTE: content was not valid JSON; salvaged M chars,
K bytes dropped"), so the agent gets the signal without a refusal loop. At
that point F4 converges toward F1+F2 compositionally.

**Coverage.** Everything D1 covers (C4, prefix-salvageable C2/C3), same blind
spots (C2 mid-file, and the sidley case where pass 3 fails).

**Verdict: as specified (fully silent) — reject.** With the return-string note,
it is a reasonable implementation *vehicle* for the recommended combination
(see F2+F4-fused below), not a standalone fix.

---

## F5 — PROMPT-LEVEL

**Mechanism.** Add to `.opencode/agents/product-analyzer.md`,
`site-analyzer.md`, `code-tester.md` (and `cleanup.md` /
`skill-learner.md` if desired) a short, concrete rule:
"When writing `.json` artifacts: emit strictly valid JSON — no literal control
characters inside strings (escape as `\n`, `\t`), quote every scalar value,
escape quotes inside string values."

**Files touched.** 3–5 prompt files. Zero code.

**Cost.** Zero runtime cost. A few hundred prompt tokens.

**Effectiveness.** Prompt rules of this shape have a real but partial effect
(the CLI-contract work — `aa74f02` — showed prompt reinforcement alone produced
zero violations on one e2e run, but that is one run and one behavior). They do
not change capability: a model that cannot reliably escape a 30 KB nested
object will still fail sometimes, now without a net. Prompts also rot silently
and are invisible in any audit of "why did this file parse".

**False-positive risk.** None. False-fix risk: none. Coverage: probabilistic
reduction across all classes, guaranteed coverage of none.

**Verdict: complement only, never a layer.** Worth adding at ~zero cost, but
must not be counted as a defense in the reliability argument.

---

## Additional candidate — F6: REPAIR-v2 (upgrade the existing 3-pass backstop)

Found during the survey; small, independent, and it directly rescues the
sidley-class loss. Two changes to `_fix_json_artifact`:
1. **Add `strict=False` to passes 0–2** — this makes pass 0 *succeed* on every
   C1 file, which today falls through to passes that may mangle it or rename
   it. (Pass 0 currently parses strict, so a control-char file skips straight
   past "valid as-is" into repair logic that was designed for other classes.)
2. **Add a balanced-closer pass** before the rename: walk back from the error
   position (`e.pos`), maintain the bracket/quote stack, and close with the
   exact required closer sequence. Verified on the sidley file: recovers 9/10
   keys and all 36 field mappings where current pass 3 recovers nothing.
3. **Honor `JSONDecodeError.pos`** — pass 3 today scans from the *end* with a
   fixed step and two closers; the error position is known and should bound
   the search.

Files: `webapp/agents/graph.py` (one function) + extend
`tests/test_codegen_fixes.py::TestArtifactRepair` with a C1 case
(`strict=False` pass 0) and a sidley-shaped case. Independently shippable
today, benefits every future corruption regardless of what else ships.

---

## Interaction with the existing 3-pass repair (asked explicitly)

**Keep it as the backstop — do not simplify it yet — but fix it (F6) and
de-scope it.**

- Write-time sanitization (F2) removes C1/C4/C5 *before* the repair ever sees
  them, so the repair's remaining workload is C2/C3 salvage + the `.corrupt`
  rename. That is a *narrower* job, not a deletable one: the write path can
  still fail in ways no validator catches (a model that writes a truncated
  file under the 900 s timeout — C3 — is caught only post-hoc; so are
  artifacts produced by *deterministic* writers regressing, and artifacts
  restored from the File Master by `setup_workspace` that were corrupt from an
  older run — `_restore_from_archive` rehydrates whatever bytes are stored).
- Conversely the repair currently runs **after** the agent exits, so on any
  corruption it can't fix it costs a full phase re-run (up to 70 calls / 900 s
  + a human interrupt). Moving the cheap half of its logic into the write path
  (F2) is what makes keeping the expensive half affordable.
- **What can be simplified later, and when:** once F2 has run in production
  for a period with zero C1/C4/C5 files reaching `_fix_json_artifact` (verify
  via its log lines), passes 1 (bad-escape regex) and the strict-mode pass 0
  become dead code and can be dropped, leaving salvage + rename. Until that
  evidence exists, defense-in-depth is justified because the two layers guard
  different entry points (tool path vs. restored-archived/FM path vs.
  non-`write_file` writers).
- **Coverage gap that nothing in F1–F5 closes:** artifacts rehydrated from the
  File Master by `setup_workspace` bypass `write_file` entirely; only a
  read-time/repair-time net (D1-style, or a load-time guard in
  `_read_json_artifact`) covers those. Keep D1 for exactly this reason.

---

## Recommended combination

**F2 (sanitize-on-write) + F1-lite (single corrective error, capped) + F6
(repair-v2) + F5 (one-line prompt reinforcement). Reject F3 and standalone F4.**

Concretely, the `write_file` `.json` branch becomes one deterministic
pipeline (all local, zero LLM calls on the happy path):

```
path ends .json?
 1. strip ```json fence if present            (C5)
 2. json.loads(content, strict=False)         (C1 accepted here)
 3. on success → write json.dumps(obj, indent=2, ensure_ascii=False)
      → canonical, strictly-valid file. Log "canonicalized (strict=False)" only
        when strict parse failed but non-strict succeeded, so C1 events are
        observable in logs without noise.
 4. on failure → if refusals_for_this_run < 3:
        return corrective error with e.msg/lineno/colno + "escape control
        characters; quote scalar values"        (F1-lite: the only LLM cost,
        bounded at 3 calls per run, and it is the piece that teaches the model)
    else:
        write raw bytes as-is + log ERROR       (fallback so the phase's
        post-hoc repair — not a missing file — owns it)
```

And in `graph.py`: F6 changes to `_fix_json_artifact` (`strict=False` in
passes 0–2, balanced-closer pass bounded by `e.pos`), plus **extend
`artifact_fix_fn` to site_analyzer and code_tester** — code_tester does not go
through `_run_budgeted_agent`, so it needs a one-line call after
`_load_test_report` returns `None` (repair-then-reload once) before the F19
no-report path fires; site-analyzer gets `artifact_fix_fn=lambda slug:
_fix_json_artifact(slug, "site_analysis.json")` in its existing
`_run_budgeted_agent` call.

### Why this combination

- **Zero added LLM calls and zero wall-clock growth for every corruption the
  parser can handle** (C1/C4/C5 — including the priceline instance). That is
  the user's efficiency constraint, satisfied exactly.
- **F1-lite is capped at 3 calls and only fires on the classes sanitization
  cannot fix** (C2/C3), where a corrective error is genuinely useful — and
  where the alternative (silent salvage) is the failure mode the job-10
  lesson explicitly forbids.
- **F6 rescues the sidley class at near-zero cost** and fixes a defect found
  in the shipped repair (pass 3 recovers nothing on that file; balanced-closer
  recovers 91%). It also keeps the backstop honest for the File-Master
  rehydration path, which no write-time fix can cover.
- **Defense in depth is preserved** with each layer at a different entry
  point: write tool (F2/F1-lite), phase exit (D1/F6), read time (D2 + the
  existing `{}`/`None` degradation), and the layers stop overlapping once F2's
  logs prove the classes no longer reach the repair.

### Honest gaps — what this combination does NOT cover

1. **C2 at the source.** Nothing here stops the model from emitting an
   unescaped quote; we only detect it fast (refusal), salvage from it
   (balanced-closer), and teach against it (prompt). The salvaged artifact
   *loses data after the corruption point* — the sidley salvage keeps 9/10
   keys; a corruption earlier in the object keeps less. Silent-partial-data is
   inherent to C2 salvage and is bounded, not solved, by this design.
2. **C3 truncation under the 900 s timeout.** A write that never happens
   (thread abandoned mid-generation) produces a *missing* file, which
   write-time validation cannot see; it is caught by the missing-artifact /
   budget machinery at full re-run cost. (Forensics may find the write itself
   is partial — if so, the raw-write fallback + F6 salvage is the net.)
3. **File-Master rehydration path.** `setup_workspace._restore_from_archive`
   bypasses `write_file`. Only D1/F6 (or a new load-time guard) covers corrupt
   bytes restored from a prior run. Recommend a follow-up: validate in
   `_read_json_artifact` and log (not repair) so drift is visible.
4. **C6 wrong-shape.** Valid JSON with a bad schema sails through every layer
   here by design; that belongs to `validate_coverage`/`validate_analysis`.
5. **Other writers.** `scraper_draft.py` is Python (out of scope); scrapers'
   `output_*.json` are written by deterministic code in the browser service
   (out of scope here); skill files have their own typed tools.
6. **`edit_file`.** An agent can still *edit* a valid JSON file into an
   invalid one. The same validate/canonicalize branch should apply to
   `edit_file` on `.json` paths (cheap, same helper); listed in build order.
   If skipped, it remains a hole.
7. **Non-JSON corruption of JSON-adjacent artifacts** (e.g. a `.py` scraper
   that fails to compile) is a different pipeline with its own gates
   (`_probe_phase1_discovery`, code_tester) — untouched.

---

## Build order (each step independently shippable, each with its own test)

1. **F2 core** — sanitize-on-write in `write_file` for `.json` (fence strip +
   `strict=False` parse + canonical redump). Tests: C1 control char → file
   contains `\n` escaped and re-parses strict; C4; C5 fence; valid file byte-
   identity NOT required but round-trip equality asserted; non-`.json` path
   untouched; `.opencode/skills` deny still first. *Ship alone: priceline
   class dead, zero behavior change otherwise.*
2. **F6 repair-v2** — `strict=False` in `_fix_json_artifact` passes, balanced
   closer bounded by `e.pos`. Tests: sidley-shaped file (add the real 200-byte
   snippet as a fixture) recovers ≥9 keys; existing 4 `TestArtifactRepair`
   cases still pass. *Ship alone: repairs today's live `.corrupt` losses.*
3. **F1-lite** — refusal-with-corrective-error in the same `write_file`
   branch, capped at 3 per agent run via the tool-context counter, then
   raw-write fallback + ERROR log. Tests: corrupt C2 content → error string
   contains `Expecting ',' delimiter` and the instruction; 4th attempt writes
   raw; counter resets per `set_tool_context`. *Ship after 1–2 so the fallback
   lands in a world where the repair is strong.*
4. **Extend `artifact_fix_fn` to site_analyzer (one line) and code_tester
   (repair-then-reload once around `_load_test_report`)**. Tests: corrupt
   `test_report.json` is repaired before F19 declares missing; corrupt
   `site_analysis.json` triggers repair not just missing-interrupt.
5. **`edit_file` gets the same `.json` validate+canonicalize branch** (same
   helper, one call site). Test: edit that breaks JSON → refused.
6. **F5 prompt line** in `product-analyzer.md`, `site-analyzer.md`,
   `code-tester.md` (one sentence each). *Zero-risk last.*
7. *(Deferred)* Load-time visibility: `_read_json_artifact` logs a warning on
   parse failure (it currently returns `{}` silently) — closes the
   FM-rehydration observability gap without behavior change.
8. *(Not scheduled)* F3 structured-write — revisit only if production logs
   show C2 surviving steps 1–4 at a rate that matters.

**Observability shipped with step 1:** log one line per canonicalization
(`strict failed, non-strict OK`) and one per refusal/fallback. After ~2 weeks,
grep those lines to (a) confirm C1/C4/C5 never reach `_fix_json_artifact`
again, and (b) decide whether repair passes 1 and strict pass 0 can be deleted.
