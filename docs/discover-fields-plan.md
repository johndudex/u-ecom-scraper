# Plan — "Discover Fields" Button on /intake (LLM-driven, v2)

> Status: **Planned; not implemented.** 3-agent design (LLM prompt + backend + frontend). Revised from v1 per user: use **LLM** for field discovery (not deterministic-only), for maximum completeness.

## TL;DR

A "Discover Fields" button on `/intake` that browses the actual website (anti-bot cloak browser, direct — **not** the 90s probe escalation), then uses an **LLM** to discover ALL extractable fields from the rendered page. Shows results in a **modal with a spinner**. User can add them as chips or insert as a JSON schema. Best-effort. Target latency: **~15-25 seconds**.

## The approach

### Step 1 — Browser: `/navigate` + cloak (~5-13s)

Call `POST {BROWSER_SERVICE_URL}/navigate` (`browser_service/server.py:1200`) with `stealth:"cloak"`, `return_what:"all"`, `wait_until:"domcontentloaded"`, `timeout:25`. Returns full HTML + classification + blocked status. No probe escalation, no PROBE_LOCK, no LLM captcha check. Bounded by `NAVIGATE_SEMAPHORE=3` (429 on overload).

### Step 2 — Content extraction (<0.1s)

From the returned HTML:
- **JSON-LD**: `src/page_analysis.extract_jsonld(html)` → list[dict]. Cap 8 blocks, 1500 chars each. Expand `@graph`.
- **Meta tags**: `extract_meta_tags(html)` → dict. Filter to og:/twitter:/article:/product: namespaces.
- **Content summary**: strip nav/footer/header/script/style, extract visible text, truncate to ~3-4k chars.

### Step 3 — LLM field discovery (~5-10s)

**Model**: `get_small_llm(temperature=0.0)` → glm-5-turbo, **one-shot** (`llm.invoke([HumanMessage])`, no agent loop/tools).

**Prompt** (forced-JSON, no markdown): "You are a field-discovery engine. Given this page's JSON-LD, meta tags, and content, list EVERY extractable field." Returns:
```json
{"content_type": "product|job_posting|article|...",
 "fields": [{"name":"title","type":"text","source":"jsonld","required":true,"description":"...","path":"name"}],
 "json_schema": {"type":"object","properties":{"title":{"type":"string"},"price":{"type":"number"}}, "required":["title","price"]},
 "model_notes": "..."}
```
Includes 2 worked examples (product + job posting with nested address). Nesting is explicitly preserved (objects/arrays, not flattened).

**Response parsing**: `_strip_fences` (reuse from `url_judge.py:51`) + `json.loads` + validate field names (snake_case regex) + rebuild json_schema if garbled.

**Fallback**: if LLM fails/garbage → deterministic `_infer_fields_from_probe` (JSON-LD @type → content-type core fields). Source tag: `"llm"` vs `"jsonld"`.

### Step 4 — Backend: `POST /intake/discover-fields/`

**File structure**:
- `src/field_discovery.py` (new, pure Python): `discover_fields_from_html(url, html, title)` → `{fields, json_schema, source, content_type}`. Lazy-imports `agents.llm` inside the function.
- `webapp/scraper/views.py`: `intake_discover_fields` view (~90 lines). Mirrors `intake_check_site` / `intake_validate_schema`.
- `webapp/scraper/urls.py`: +1 route.

**Response shape** (always 200 unless auth/method guard):
```json
{"fields": ["title","price",...], "json_schema": {...}, "source": "llm|jsonld",
 "content_type": "product", "error": null, "message": null}
```

**Error cases** (all 200 with `error`/`message` — codebase convention):
- Anti-bot blocked → `message: "This page is blocking automated access. Add fields manually."`
- Timeout → `message: "The page took too long. Add fields manually."`
- No fields → `message: "Couldn't detect structured fields. Add them manually."`
- BS unreachable → `error: "Browser service not reachable."`
- BS busy (429) → `message: "Browser is busy. Try again."`

### Step 5 — Frontend: button + modal + spinner

**Button**: "🔍 Discover fields from this page" — inside `#fields-block` (between `#fields-label` and `#chips`). Always visible after the URL check. Disabled when no valid URL. Low-emphasis `.btn-link` styling.

**Modal** (new overlay, reuses existing CSS classes):
- **Loading**: spinner + "Discovering fields from `<host>`… ~15-20 seconds." + Cancel button.
- **Success**: toggleable field chips (all pre-selected) + "Add selected as chips" (calls `addChip` for each) + "Insert as JSON schema" (fills `#schema-json-input` + `setSchemaMode('schema')` + `validateSchemaText()`).
- **Error/message**: red `.url-error` for `error`; yellow `.cfg-changed-note` for `message`.
- **Cancel**: AbortController (abort the fetch). Esc + backdrop click also close.

**JS**: `postJSON(INTAKE_CONFIG.discoverFieldsUrl, FormData({url}), {signal: controller.signal})`. 60s UX backstop timer.

**Integration**: "Add as chips" → existing `addChip()` (dedupes). "Insert as JSON schema" → `setSchemaMode('schema')` + fill textarea + `validateSchemaText()` (server-validates → populates chips). Both non-blocking, additive.

### Files to create/modify

| File | Change |
|------|--------|
| `src/field_discovery.py` | **New** — `discover_fields_from_html()`, `_content_summary()`, `_build_discovery_prompt()`, `_parse_llm_response()`, `_fallback_jsonld()`. ~150 lines. |
| `webapp/scraper/views.py` | **+`intake_discover_fields`** view (~90 lines). + `_discover_response()` helper. |
| `webapp/scraper/urls.py` | **+1 route**. |
| `webapp/scraper/templates/scraper/intake.html` | **+button + modal DOM + JS** (~80 lines). + `discoverFieldsUrl` in `INTAKE_CONFIG`. +~10 lines CSS (`.modal-overlay`, `.modal-panel`, `.discover-row`). |
| `webapp/agents/llm.py` | **+`timeout` kwarg** to `get_small_llm()` (one-line, so the discovery call can cap at 15s instead of the default 300s). |

### What we reuse (no reinvention)
- `postJSON` + AbortController (intake.html:866-873).
- `addChip` / `clearChips` / `setSchemaMode` / `validateSchemaText` (the existing chip + schema flow).
- `_strip_fences` (url_judge.py:51 — GLM fence-stripping).
- `get_small_llm` (llm.py:217 — the one-shot LLM pattern).
- `extract_jsonld` / `extract_meta_tags` / `extract_title` (page_analysis.py).
- `_infer_fields_from_probe` (views.py:2131 — deterministic JSON-LD fallback).
- `/navigate` endpoint (browser_service — cloak + full HTML, no probe).
- `.panel`, `.spinner-sm`, `.detect-status`, `.chip-select`, `.url-error`, `.hint`, `.btn-*` (existing CSS).

### Latency budget (target ~15-25s)
| Phase | Typical |
|-------|---------|
| CloakBrowser launch + goto + 1.5s settle | 5-13s |
| HTML parse + content extraction | <0.1s |
| LLM one-shot (glm-5-turbo, ~6k tokens in, ~1k out) | 5-10s |
| Response parse | <0.1s |
| **Total** | **~10-23s** |

### Risks
1. **Anti-bot defeats cloak** → surface error (don't silently fallback to 2-field default).
2. **LLM hallucination** → best-effort (user reviews chips before job creation). Tag `source:"llm"`.
3. **SPA shells render late** → if JSON-LD empty + HTML <5k, retry once with `wait_until:"load"`.
4. **LLM latency ceiling** → `timeout=15` on the LLM call; fallback to JSON-LD if exceeded.
5. **Concurrency** → `/navigate` NAVIGATE_SEMAPHORE=3; 429 → "browser busy, try again."
6. **"Insert as JSON schema" replaces chips** → `validateSchemaText` calls `clearChips()` (existing schema-mode behavior). Note in the UI.

### Verification (when implemented)
1. Unit tests: `src/field_discovery.py` with sample HTML (JSON-LD present/absent, LLM mock, fallback).
2. E2e: open `/intake/`, enter a product URL, click "Discover Fields", verify modal + spinner + fields. Test: Shopify site (rich JSON-LD), simple site (meta only), anti-bot site (blocked → message).
3. Integration: discovered fields → add as chips → submit job → pipeline runs. Discovered schema → insert → validate → chips → submit.
