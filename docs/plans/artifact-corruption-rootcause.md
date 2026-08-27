# Artifact Corruption Root Cause — write-path forensics

**Date:** 2026-08-27 · **Scope:** every path by which LLM-produced content becomes a `.json` file on disk, plus byte-level forensics on the three known corrupt artifacts.

**TL;DR** — `write_file` writes the tool-call argument **verbatim with no JSON validation** (`filesystem_tools.py:143-163`), and the streaming tool-argument parser **silently repairs** (rather than rejects) literal control characters and truncated arguments. Every LLM-authored artifact therefore reaches disk exactly as the model typed it. Three distinct defect shapes were emitted in one day: unquoted prose scalars, literal newlines inside a string, and unescaped quotes inside an embedded JS regex. Only `product_analysis.json` has a repair-on-write guard; `test_report.json`, `site_analysis.json`, `learning_report.json`, `cleanup_report.json`, and `nav_learning_report.json` have **none**, and three unvalidated byte-copy paths propagate corrupt bytes to the File Master and back into later jobs.

---

## 1. Per-sample byte-level forensics

### Sample 1 — `workspace/sidley-com/product_analysis.json.corrupt` (33,849 bytes, 2026-07-18)

Recovered from `u-ecom-scraper-django-1:/app/workspace/sidley-com/product_analysis.json.corrupt`.

```
json.JSONDecodeError: Expecting ',' delimiter: line 716 column 25 (char 30983)
```

Context at the failure point:

```
"search_criteria": {
  "offices": 25 offices with counts,
  "services": 100+ service/industry categories with counts,
  "titles": 20+ title levels with counts (Partner, Associate, etc.)
}
```

Facts established:

- **Not truncation** — the file is structurally complete: balanced braces, ends `..., "confidence_score": 0.92}`.
- **Not encoding** — clean UTF-8, first byte `0x7b` (`{`, no BOM), valid throughout.
- **Not control characters** — a character-walk found **zero** control chars (0x00–0x1F) inside strings, unlike sample 2.
- **The defect is exactly three unquoted string scalars** at lines 716–718. Proof: quoting only those three values makes the entire 33,849-byte file parse (12 top-level keys) — verified in-container.
- Adjacent evidence of escaping-depth confusion in the same region: line 721 stores `"{\\"@context\\":\\"https://schema.org\\"} — no useful structured data..."` — *double*-escaped quotes inside a valid string (legal JSON, but the parsed value contains literal `\"` artifacts).
- The file carries the `.corrupt` suffix, meaning `_fix_json_artifact` ran all three passes and correctly quarantined it (pass 1's bad-escape regex was tried and made things worse — `Invalid \escape` — before being discarded; nothing mangled was written).

**Corruption shape: unquoted bare scalars in a prose-summary sub-object.** The model switched to YAML-style values while summarizing the Coveo search facet taxonomy. Mechanism class: LLM JSON-authoring error, delivered verbatim by `write_file`.

### Sample 2 — `scrapers/priceline-com-au/analysis/test_report.json` (File Master, 4,661 bytes)

Fetched via `src.artifacts.read('scrapers/priceline-com-au/analysis/test_report.json')`. The local workspace copy is gone (`/app/workspace/priceline-com-au/` no longer exists — `_finalize_job` rmtree'd it).

```
json.JSONDecodeError: Invalid control character at: line 87 column 299 (char 3426)
```

Hexdump at the failure point (`0a 0a` between sentences):

```
...66 69 6c 65 20 70 61 74 68 20 62 75 67 20 69 73 20 61 6c 73 6f 20 72 65 73 6f 6c 76 65 64 2e | 0a 0a | 48 6f 77 65 76 65 72 ...
   f  i  l  e     p  a  t  h     b  u  g     i  s     a  l  s  o     r  e  s  o  l  v  e  d  .  LF LF  H  o  w  e  v  e  r ...
```

Facts established:

- **The file parses perfectly with `json.loads(..., strict=False)`.** The *only* defect in 4,661 bytes is control characters inside strings.
- Exactly **8 literal LF (`0x0A`) bytes**, all inside one field: `feedback_for_writer`, a 1,042-character multi-paragraph prose blob ("Phase 2 (extraction) is now FIXED… \n\n However, Phase 1 (discovery) is STILL BROKEN…").
- No CR, no tab, no other control bytes. Not truncated, not BOM'd, not double-encoded.

**Corruption shape: literal newlines inside a JSON string.** `code_tester` wrote a multi-paragraph essay into one string field and emitted real newline bytes instead of the two-character `\n` escape. §3.2 proves how those bytes survived the tool-call layer.

### Sample 3 — Railway job 10 `product_analysis` (class C, evidence from `docs/plans/codegen-regression-analysis.md` §3)

```
Expecting ',' delimiter: line 36 column 202 (char 2858)
```

Unescaped **double quotes** inside a `js_extraction` JS snippet string: `t.match(/\{"cx-state"[\s\S]*$/` embedded in prose. The raw `"` terminates the JSON string early → delimiter error. Same escaping-depth failure as sample 1's line 721, but one level the other way (unescaped instead of double-escaped). The doc records the downstream consequence: `_read_json_artifact` returned `{}`, `_summarize_product_analysis` returned `""`, and `code_writer` received **no field map at all** — then implemented the corrupt file's prose claims verbatim (`extract_cx_state()`, the `entities.{code}.variants.value` walk) and shipped 30 blank records.

Note: the doc's criticism that `_fix_json_artifact` "writes the mangled content first, then validates" (L137/L138) describes an **older** revision — current code validates before writing (`json.loads(fixed)` at `graph.py:147` precedes `f.write(fixed)` at `graph.py:148`). The repair-on-write ordering bug is already fixed; the coverage gap (unescaped quotes are unrepairable by design) is not.

---

## 2. The write-path map

### 2.1 The `write_file` tool — no validation, verbatim bytes

`webapp/agents/tools/filesystem_tools.py:143-163`:

```python
@tool
def write_file(path: str, content: str) -> str:
    ...
    safe = _enforce_not_skills(path, root)      # only sandbox check
    os.makedirs(os.path.dirname(safe) or ".", exist_ok=True)
    with open(safe, "w", encoding="utf-8") as f:
        f.write(content)                        # ← verbatim, no parse, no .json branch
```

There is **no special-case for `.json` paths anywhere in the tool**. `content` — whatever string the tool-call argument carried — is flushed to disk as-is. `edit_file` (`filesystem_tools.py:165-214`) is equally unvalidated: an LLM that "fixes" a JSON file with `edit_file` can introduce the same defects. `run_bash` (`shell_tools.py:113-160`) is a third, unguarded write channel (any `cat > file`, heredoc, or `python -c` the model chooses). Guards (`_apply_guards`, `subagents.py:887`) constrain **URLs and Akamai tools only** — nothing touches file content.

### 2.2 Writer inventory — `json.dump` (SAFE) vs. raw LLM text (EXPOSED)

**SAFE — deterministic serializer writers** (validity guaranteed by `json.dump`):

| Artifact | Writer | Location |
|---|---|---|
| `navigation_findings.json` | `navigate_explore` node | `webapp/agents/nodes/navigate_explore.py:3609-3610` |
| `navigation_analysis.json` | `browser_traverse` node | `webapp/agents/graph.py:2202-2203` |
| `scraper_analysis.json` | `_decide_strategy` (deterministic strategy picker) | `webapp/agents/graph.py:2714-2716` |
| `input_urls.json` | `_invoke_code_writer` / `setup_workspace` | `graph.py:3256-3257`, `setup_workspace.py:172-174` |
| `discovery_config.json` | `_invoke_code_writer` | `graph.py:3271-3273` |
| `output_*.json` (scraper outputs) | the generated scraper's own `json.dump` (`templates/*.py`), returned as `output_content` and persisted verbatim by `run_scraper` (`shell_tools.py:334-341`) and `views.py:1636` | separate class — serializer-generated, **not** the corruptor here |

**EXPOSED — raw LLM text through `write_file`** (validity depends entirely on model escaping discipline):

| Artifact | Author | Prompt instructing the write | Guard? |
|---|---|---|---|
| `site_analysis.json` | `site_analyzer` (LLM) | `subagents.py:1075`, `1104-1105` | **none** |
| `product_analysis.json` | `product_analyzer` (LLM) | `subagents.py:1541`, `1576` | `_fix_json_artifact` repair-on-write (`graph.py:1921` → `graph.py:122-194`); also silently re-serialized by `normalize_fields` **iff it parses** (`normalize_fields.py:88-92`) |
| `test_report.json` | `code_tester` (LLM) | `subagents.py:3481`, `3563`; `.opencode/agents/code-tester.md:60-61`, `98` | **none** |
| `cleanup_report.json` | `cleanup` (LLM) | `subagents.py:3583`, `3601` | **none** (mitigated: nothing reads it back — `state.cleanup_report` is never loaded from disk) |
| `learning_report.json` | `skill_learner` (LLM) | `subagents.py:3633`, `3652` | **none** |
| `nav_learning_report.json` | `nav_skill_review` (LLM) | `subagents.py:1898`, `1929` | read is tolerant (`navigate_skill_review.py:89-105` catches `JSONDecodeError` and continues) |

### 2.3 Who writes `test_report.json`

**The `code_tester` LLM agent, via `write_file`** — it is *not* a deterministic dumper. `.opencode/agents/code-tester.md:60-61`: "Step 5: Write test_report.json (1 call) … **This MUST be your last action.** Use `write_file` to save the report." The graph then reads it back with `_load_test_report` (`graph.py:548-571`, returns `None` on `JSONDecodeError` with only a warning). There is **no `artifact_fix_fn`** for `code_tester` — it does not go through `_run_budgeted_agent` at all (invoked directly at `graph.py:3505-3509`), so the one repair guard in the codebase does not apply to it.

### 2.4 The propagation paths (how corrupt bytes travel)

Three byte-copy paths move workspace files **without ever parsing them**:

1. **`_finalize_job`** — `webapp/scraper/tasks.py:834-846`: copies all five analysis artifacts (`site_analysis`, `navigation_analysis`, `product_analysis`, `scraper_analysis`, `test_report`) to the File Master via `src.read_bytes()` → `artifacts.write()`. No validation. **This is how the corrupt priceline `test_report.json` reached the File Master** — `_preserve_test_report` would have skipped it (it only runs when `_load_test_report` succeeds, `graph.py:3507-3517`), but the finalize loop copies blindly.
2. **`_invoke_skill_learner`** — `graph.py:3747-3764`: `learning_report.json` + `nav_learning_report.json` raw bytes → FM `analysis/`, no parse check.
3. **`setup_workspace._restore_from_archive`** — `setup_workspace.py:77-97`, called at `149-155`: re-hydrates FM `analysis/` bytes back into the workspace on `skip_*` re-runs. **Corruption is therefore durable across jobs** — a corrupt FM copy is faithfully restored into the next job's workspace and re-consumed by `code_writer`.

`src/artifacts.py:42-55` itself is a dumb byte pipe by design (`write` = `httpx.put`, `write_json` = `json.dumps` then upload); the File Master stores verbatim and cannot corrupt anything — the bytes were already bad before upload.

---

## 3. Confirmed mechanisms, ranked by evidence

### M1 — `write_file` performs no JSON validation (the enabler for all three samples)

The tool has no `.json` branch, no `json.loads` trial, no schema check (`filesystem_tools.py:143-163`). Whatever string the model emitted is the file. This is *the* single mechanism common to all three corrupt artifacts; every other finding is about what the model emitted and why the surrounding layers didn't stop it.

### M2 — The streaming tool-argument parser silently *repairs* invalid tool-call JSON (proven in-container)

This is the non-obvious mechanism that lets sample 2's literal newlines reach `write_file` at all. Two parsers exist in the tool-call path:

- **Non-streaming** — `langchain_core/messages/tool.py:349` `default_tool_parser` uses strict `json.loads`. A tool argument containing a literal LF **fails** with `Invalid control character` and the tool call is dropped (verified).
- **Streaming** — `langchain_core/messages/ai.py:557` uses `parse_partial_json` (`langchain_core/utils/json.py:96-140`). Its repair loop rewrites a literal `\n` inside a string into the two-character escape, closes unterminated strings/brackets, then parses with `strict=False`.

Verified experimentally against the installed `langchain_core`:

```
outer tool-args JSON with literal LF inside the content string:
  A. strict json.loads  -> FAILS: Invalid control character   (call would be dropped)
  B. parse_partial_json -> SUCCEEDS; content arg contains a REAL LF
                           content-as-file is INVALID JSON (would land on disk corrupt)
  C. truncated args     -> {'path': 'a.json', 'content': '{"k": "some long valu'}
                           (silently COMPLETES broken JSON -> truncated write)
```

Consequences:

- Because LiteLLM-prefixed models run with `streaming=True` (`webapp/agents/llm.py:344-345`, required to dodge the proxy's ~60s non-streaming 504), **every agent's tool calls go through the lenient parser**. Literal control characters in a `write_file` argument are normalized into the value instead of rejecting the call.
- The same leniency is the **truncation** channel: a generation cut mid-argument parses to a *shortened but valid* `content` string, and `write_file` happily writes the truncated JSON. (Truncation is *not* the cause of samples 1 and 2 — both are structurally complete — but it is a live, demonstrated path for future corruption.)

### M3 — The LLM's JSON-authoring failures (the actual defect shapes)

| Shape | Sample | Trigger |
|---|---|---|
| Unquoted bare scalars | sidley L716-718 (`"offices": 25 offices with counts,`) | model drifts to YAML-style values in a prose-summary sub-object |
| Literal control chars in a string | priceline `feedback_for_writer` (8× LF) | multi-paragraph prose in one string field; model emits real newlines |
| Unescaped quotes in embedded code | job 10 (`t.match(/\{"cx-state"...`) | JS regex quoted in prose; model loses track of escape depth |

Note the pattern: **every failure sits where prose meets JSON** — prose summaries, prose feedback, prose containing code. Structured leaves (urls, counts, booleans) never corrupted.

### M4 — Unvalidated byte-copy propagation (blast-radius amplifier)

`tasks.py:834-846`, `graph.py:3747-3764` copy raw bytes to the File Master without parsing, and `setup_workspace.py:149-155` restores them on re-runs. A corrupt artifact therefore outlives its job and re-enters later generations — job 10's author consumed a poisoned artifact this way.

### Ruled out

- **Truncation mid-write** — both obtainable samples are structurally complete (balanced braces, proper terminal values). Live path via M2-C, but not the cause here.
- **Encoding mismatch / BOM** — both files start `0x7b`, decode cleanly as UTF-8 throughout.
- **Double-encoding** — sidley L721 contains double-escaped quotes *inside a valid string* (legal JSON, ugly value), but that is not what breaks parsing; the parse breaks on L716. Job 10 is the inverse (unescaped), not a double-encode.

---

## 4. Blast-radius table

| Artifact | Written by | Writer location | Class | Net exposure |
|---|---|---|---|---|
| `product_analysis.json` | product_analyzer LLM → `write_file` | `filesystem_tools.py:143-163`; prompt `subagents.py:1541/1576` | **EXPOSED** | Partially guarded: `_fix_json_artifact` repairs escapes/truncation & quarantines the rest (`graph.py:1921→122-194`); `normalize_fields.py:88-92` re-serializes **iff parseable**. Samples 1 & 3 both hit this file. |
| `site_analysis.json` | site_analyzer LLM → `write_file` | prompt `subagents.py:1075/1104` | **EXPOSED** | No guard. Corrupt → `_read_json_artifact` `{}` → downstream runs on empty platform/anti-bot data. Byte-copied to FM by `tasks.py:834`. |
| `test_report.json` | code_tester LLM → `write_file` | prompt `subagents.py:3481/3563`, `code-tester.md:60-98` | **EXPOSED** | **No guard** (code_tester bypasses `_run_budgeted_agent`). Sample 2. Corrupt → `_load_test_report` returns `None` → route_after_testing treats as "no report" → retry loop burns budget on a phantom failure. Byte-copied to FM by both `_preserve_test_report` (valid case) and `tasks.py:834-846` (blind). |
| `navigation_findings.json` | `navigate_explore` deterministic | `navigate_explore.py:3609-3610` `json.dump` | SAFE | Serializer guarantees validity. |
| `navigation_analysis.json` | `browser_traverse` deterministic | `graph.py:2202-2203` `json.dump` | SAFE | Same. |
| `scraper_analysis.json` | `_decide_strategy` deterministic | `graph.py:2714-2716` `json.dump` | SAFE | Same. |
| `cleanup_report.json` | cleanup LLM → `write_file` | prompt `subagents.py:3583/3601` | EXPOSED (low) | No guard, but nothing reads it back (`state.cleanup_report` never loaded from disk). Cosmetic exposure only. |
| `learning_report.json` | skill_learner LLM → `write_file` | prompt `subagents.py:3633/3652` | **EXPOSED** | No guard **and** byte-copied to FM unvalidated (`graph.py:3747-3764`). |
| `nav_learning_report.json` | nav_skill_review LLM → `write_file` | prompt `subagents.py:1898/1929` | EXPOSED (low) | Reader is tolerant (`navigate_skill_review.py:89-105` catches the parse error and continues), but still byte-copied to FM unvalidated. |
| `output_*.json` | generated scraper's own `json.dump` | `templates/*.py`; persisted via `shell_tools.py:334-341`, `views.py:1636`, `tasks.py:663` | SAFE (separate class) | Serializer-generated; not the corruptor. |

**One-line summary of exposure:** three artifacts are structurally safe (deterministic `json.dump`), six are raw LLM text — of which `product_analysis.json` is the only one with a repair guard, and `test_report.json` is the only one that is both unguarded *and* consumed by control-flow routing (`route_after_testing`).

---

## 5. Where a fix would attach (observations, not prescriptions)

1. **Gate at the tool** — a `.json`-path branch in `write_file` (`filesystem_tools.py:143`) that tries `json.loads(content)` and returns the parser error as the tool result (instead of writing) closes M1 and M3 for *every* artifact at once, and gives the model an immediate retry signal. `strict=False` would additionally catch exactly sample 2's shape (literal LF) while tolerating nothing else.
2. **Truncation fence** — M2-C means a cut generation can still produce a valid-looking short write; a gate that also warns when `content` ends mid-string (unbalanced quotes) would surface it.
3. **Validate before copy** — parse-gate the three byte-copy paths (`tasks.py:834-846`, `graph.py:3747-3764`, `setup_workspace.py:77-97`) so corruption can't reach the File Master or be re-hydrated into later jobs.
4. **Extend `artifact_fix_fn` to code_tester** — it is the one LLM-artifact author outside `_run_budgeted_agent`, and the one whose output steers the retry loop.

## Appendix — evidence commands

```
# sample 1 (in-container)
docker exec u-ecom-scraper-django-1 python3 - <<'EOF'
import json
s=open('/app/workspace/sidley-com/product_analysis.json.corrupt',encoding='utf-8').read()
json.loads(s)   # Expecting ',' delimiter: line 716 column 25 (char 30983)
EOF

# sample 2 (fetch from FM, then parse)
docker exec -e PYTHONPATH=/app:/app/webapp -e DJANGO_SETTINGS_MODULE=config.settings \
  u-ecom-scraper-django-1 python -c "import django; django.setup(); import src.artifacts as a; \
  open('/tmp/tr.json','wb').write(a.read('scrapers/priceline-com-au/analysis/test_report.json'))"
# json.loads -> Invalid control character at line 87 col 299 (char 3426)
# json.loads(s, strict=False) -> PARSES; 8x 0x0A, all in feedback_for_writer

# M2 proof (streaming parser leniency) — see §3; strict fails, parse_partial_json succeeds
# and the content arg carries a real LF; truncated args parse to truncated content.
```
