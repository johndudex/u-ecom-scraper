# Code Writer Summarization + Template-in-System-Prompt — Implementation Plan

> **Status:** PROPOSED for review. No code changes yet.
> Created 2026-07-21.
> Root cause analysis: code_writer is slow because the react agent re-processes
> the ENTIRE growing message history (template read + write + test + edits =
> 100-150K chars) on every LLM call. With 10+ iterations that's 10× redundant
> token processing = ~880s. The fix: keep the template in the system prompt
> (never summarized) + use LLM-driven summarization for the conversation history.

## Problem (the math)

```
Current code_writer per-call token cost (grows with each iteration):

Call 1: [system 800T + message 5K + read_file template 8K] = 14K tokens → ~30s
Call 2: [system 800T + message 5K + template 8K + write_file 8K] = 22K → ~45s
Call 3: [system 800T + message 5K + template 8K + write 8K + run_scraper 2K] = 24K → ~50s
Call 4: [system 800T + ... + edit_file 8K] = 32K → ~60s
...
Call 7: [system 800T + everything accumulated] = 40K+ → ~70s

Total: 14+22+24+32+40+... = ~250K tokens processed across 7 calls → ~880s
```

## Solution (template in system prompt + summarization)

```
With template in system prompt + SummarizationMiddleware:

System prompt (FIXED, never summarized): ~9K tokens (95L instructions + 1077L template)
Conversation history (summarized when >20K tokens): grows then compresses

Call 1: [system 9K + message 5K] = 14K → writes scraper directly (no read_file)
Call 2: [system 9K + message 5K + write 8K] = 22K → run_scraper
Call 3: [system 9K + message 5K + write 8K + test 2K] = 24K → summarization fires
Call 4: [system 9K + SUMMARY 3K + recent 10K] = 22K → edit fix
Call 5: [system 9K + SUMMARY 4K + recent 10K] = 23K → run_scraper again
Call 6: [system 9K + SUMMARY 5K + recent 8K] = 22K → done

Total: 14+22+24+22+23+22 = 127K tokens across 6 calls → ~280s
```

**~3× faster.** And the template code is NEVER at risk (system prompt is
never summarized).

## Implementation steps (sequenced, each independently verifiable)

### Step 1: Determine the template path per job

**File:** `webapp/agents/graph.py:_invoke_code_writer` (~line 2236)

Currently the template selection lives inside `build_code_writer_message`
(subagents.py). The template is chosen based on `strategy` + `data_source`.
Extract this selection into a reusable function so `_invoke_code_writer` can
also call it (to read the template file for the system prompt).

```python
def _select_template_file(state: ScrapeState) -> str:
    """Return the template filename (e.g. 'http_navigation_scraper.py') for this job.

    Mirrors the logic in build_code_writer_message's nav_template_hint / template_hint
    sections. Extracted so _invoke_code_writer can read the template contents for
    the system prompt without duplicating the selection logic.
    """
    nav_analysis = state.get("navigation_analysis") or {}
    scraper_analysis = state.get("scraper_analysis") or {}
    data_source = nav_analysis.get("data_source", "")
    strategy = (scraper_analysis.get("strategy") or "").lower()

    # Precedence: api > ssr_div_list > embedded_json > two-phase
    api_endpoint = nav_analysis.get("api_endpoint") or {}
    if isinstance(api_endpoint, dict) and (api_endpoint.get("url") or api_endpoint.get("api_url")):
        return "api_scraper.py"
    if data_source == "ssr_div_list":
        return "ssr_div_list_scraper.py"

    # Check embedded_json
    emb = nav_analysis.get("embedded_json") or {}
    if isinstance(emb, dict) and emb.get("best", {}).get("record_count"):
        if strategy in ("http_requests", "requests", "internal_api", "api"):
            return "requests_scraper.py"
        return "http_navigation_scraper.py"

    # Strategy-based selection
    if strategy in ("http_requests", "requests"):
        return "requests_scraper.py"
    if strategy in ("http_navigation", "playwright"):
        return "http_navigation_scraper.py"
    if strategy in ("internal_api", "api"):
        return "api_scraper.py"
    # Default for url_list
    return "requests_scraper.py"
```

**Verification:** unit test that `_select_template_file` returns the right
template for each (strategy, data_source, api_endpoint) combination. Cross-check
against the existing template selection in `build_code_writer_message`.

### Step 2: Read template contents + pass to create_code_writer

**File:** `webapp/agents/graph.py:_invoke_code_writer`

After Step 1, read the template file and pass its contents to the agent factory:

```python
# In _invoke_code_writer, before creating the agent:
_template_file = _select_template_file(state)
_template_path = os.path.join(_get_project_root(), "templates", _template_file)
_template_code = ""
try:
    with open(_template_path) as f:
        _template_code = f.read()
    logger.info("_invoke_code_writer: template %s (%d lines) for system prompt",
                _template_file, _template_code.count("\n"))
except Exception as exc:
    logger.warning("_invoke_code_writer: could not read template %s: %s", _template_file, exc)

# Pass to agent factory:
agent = create_code_writer(site_slug=slug, template_code=_template_code)
```

**File:** `webapp/agents/subagents.py:create_code_writer`

Add `template_code` parameter:

```python
def create_code_writer(site_slug: str = "", template_code: str = "") -> object:
    return _build_agent("code_writer", site_slug=site_slug, template_code=template_code)
```

**Verification:** log confirms the template is read (line count) + the agent is
created with it.

### Step 3: Inject template into the system prompt

**File:** `webapp/agents/subagents.py:_build_agent`

When `template_code` is provided, append it to the system prompt:

```python
def _build_agent(agent_name, site_slug="", use_create_agent=False, template_code=""):
    ...
    system_prompt = _load_system_prompt(agent_name)  # reads .opencode/agents/{name}.md

    # Inject template into system prompt (never summarized — always present)
    if template_code:
        system_prompt += (
            "\n\n## Template Reference (adapt this — do NOT read_file it)\n"
            "The full template code is below. It is a WORKING scraper. Your job is to\n"
            "substitute the field extraction functions per the Field Map in the message.\n"
            "Keep the template's structure, pagination, rate limiting, error handling,\n"
            "and output format intact. Only change the extraction logic.\n\n"
            "```python\n"
            f"{template_code}\n"
            "```\n"
        )

    # Use create_agent (v1) with SummarizationMiddleware for code_writer
    if agent_name == "code_writer":
        use_create_agent = True

    if use_create_agent:
        from langchain.agents import create_agent
        from langchain.agents.middleware import SummarizationMiddleware
        from agents.llm import get_small_llm

        _ summarizer = SummarizationMiddleware(
            model=get_small_llm(temperature=0.0),
            trigger=("tokens", 20000),   # summarize when context > ~20K tokens
            keep=("messages", 6),        # keep last 6 messages + summarize older
        )
        agent = create_agent(
            model=llm,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[summarizer],
        )
    else:
        agent = create_react_agent(llm, tools=tools, prompt=system_prompt,
                                    pre_model_hook=_truncate_messages)
```

**Why `trigger=("tokens", 20000)`:** the system prompt is ~9K tokens. The
initial message is ~5K. So the first summarization fires when the conversation
(tool results) reaches ~6K tokens (20K total - 9K system - 5K message). That's
after ~2 tool calls (write_file + run_scraper). The summarizer compresses old
tool results, keeping the system prompt + recent messages intact.

**Why `keep=("messages", 6)`:** code_writer's critical recent context is the
latest tool result (test output or edit feedback). 6 messages = 3 LLM+tool
rounds = enough to see the current state + last fix.

**Verification:**
1. Log confirms the system prompt length (should be ~template_lines + 95).
2. Log confirms SummarizationMiddleware is active.
3. Verify the LLM does NOT call `read_file` on the template (it's in the system
   prompt — the instructions say "do NOT read_file it").

### Step 4: Update build_code_writer_message (remove template_hint)

**File:** `webapp/agents/subagents.py:build_code_writer_message`

Since the template is now in the system prompt, the message builder no longer
needs `template_hint` or `nav_template_hint`. Remove those sections. The message
should contain ONLY:

1. Strategy Contract (strategy, data model, stealth, proxy, listing URL).
2. Field Map (from `_summarize_product_analysis`).
3. Retry/Fix context (if applicable).
4. Output Contract (save path, output key, argparse).

This is the builder refactor (Slice 10's Step 2) — collapsing the conditional
sections into a lean Strategy Contract.

**Verification:** log the final message length (should be ~5-8K chars, down from
~30-50K).

### Step 5: Update the system prompt (remove read_file template instruction)

**File:** `.opencode/agents/code-writer.md`

Update the Workflow section:

```
## Workflow (strict, in order)

1. The template is in your system prompt above — read it. Do NOT `read_file` it.
2. `write_file workspace/{slug}/scraper_draft.py` — adapt the template's extraction
   functions per the Field Map (in the message). Keep the template's structure,
   waits, pagination, discovery, and output code intact.
3. `run_scraper --sample`. If output is empty or a traceback, `edit_file` a targeted
   fix and re-run. Max 3 `run_scraper` calls.
4. Stop.
```

**Verification:** the system prompt says "do NOT read_file the template" + the
template code is present in the system prompt.

## What stays unchanged

- The Self-Test Loop (run_scraper) — still in the workflow.
- The Field Map (_summarize_product_analysis) — in the message.
- The Strategy Contract — in the message (from the builder).
- The 3-call run_scraper cap (subagents.py:_apply_guards) — still applied.
- The budget management (_run_budgeted_agent) — still wraps the agent.
- The wall-clock timeout (_invoke_agent_with_timeout) — still applied.
- All other agents (site_analyzer, product_analyzer, code_tester) — unchanged.

## What this does NOT do (deferred)

- Does NOT switch other agents to SummarizationMiddleware (only code_writer).
- Does NOT remove _truncate_messages (other agents still use it).
- Does NOT change the recursion limit (120 → stays; summarization keeps context
  bounded so the limit matters less).
- Does NOT change templates (they stay as-is; the system prompt just includes
  whichever one is selected per job).

## A/B test plan (before switching permanently)

1. Run with the OLD setup (no template in system prompt, no SummarizationMiddleware)
   on locumtenens → record: wall-clock, retry count, output count.
2. Run with the NEW setup on locumtenens → compare.
3. Repeat for aya, adameve, dystaffing.
4. Switch only if: faster + same-or-better output + no regression.

## Risk assessment

| Risk | Mitigation |
|------|------------|
| Template in system prompt is too big (9K tokens per call) | It's FIXED (never grows). The current pattern has 40K+ growing per call. 9K fixed is better. |
| SummarizationMiddleware loses critical context (e.g., test output) | `keep=("messages", 6)` retains the last 3 tool rounds. Test outputs are in recent messages. |
| LLM ignores "do NOT read_file the template" + reads it anyway (double context) | The system prompt explicitly says don't. If it does, it wastes one call but doesn't break. |
| Template selection mismatch (wrong template in system prompt) | Step 1's _select_template_file mirrors the existing selection logic exactly. Cross-check with unit test. |
| Regression on passing sites | A/B test (above) before switching. Keep old prompt as code-writer-v1.md for revert. |
| SummarizationMiddleware itself is slow (adds an LLM call for summarization) | Uses get_small_llm (cheap model). The summarization call is ~5s. Saves ~30s per subsequent call. Net positive. |

## Expected impact

| Metric | Current | Expected |
|--------|---------|----------|
| Per-call tokens | 14K→40K+ (growing) | ~15K (fixed + summarized) |
| Number of calls | 10-15 | 6-8 (no read_file template) |
| Total tokens processed | ~250K+ | ~100K |
| Wall-clock | ~880s (caps at 15min) | ~250-350s (under 6min) |
| Template safety | In conversation history (at risk from truncation) | In system prompt (never summarized) |

## File references

- `webapp/agents/graph.py:_invoke_code_writer` (~2236) — Step 1+2 (template selection + reading)
- `webapp/agents/graph.py:_run_budgeted_agent` (~986) — unchanged (still wraps the agent)
- `webapp/agents/subagents.py:_build_agent` (~569) — Step 3 (system prompt injection + SummarizationMiddleware)
- `webapp/agents/subagents.py:create_code_writer` (~462) — Step 2 (template_code parameter)
- `webapp/agents/subagents.py:build_code_writer_message` (~1933) — Step 4 (remove template_hint)
- `.opencode/agents/code-writer.md` — Step 5 (update workflow)
- `webapp/agents/tools/__init__.py:38-46` — code_writer tool set (unchanged)
- `langchain.agents.middleware.SummarizationMiddleware` — the middleware (verified available)
