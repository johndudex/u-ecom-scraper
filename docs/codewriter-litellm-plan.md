# Plan — code_writer via LiteLLM proxy (standardcompute) (v1, pre-critique)

> Status: **v1 — live-probed + 2 deep agents (routing map + model research). Critique rounds pending.**
> Proxy: `https://llm.johnjf.xyz/v1`, model `standardcompute` (reasoning model, single exposed model), key in hand.

## Live probe results (all verified against the real proxy just now)

| Check | Result |
|---|---|
| `/v1/models` with key | ✅ exactly one model: `standardcompute` |
| Plain completion | ✅ works; is a **reasoning model** (`reasoning_content` field, needs generous max_tokens — a 10-token cap died in thinking) |
| `temperature: 0.4` | ✅ **accepted** (no 400 — unlike claude-5/o1 families) |
| Raw tool calling (`tools=[...]`) | ✅ 1 valid call, correct args, `finish_reason: tool_calls` |
| langchain `ChatOpenAI.bind_tools` | ✅ works (`.tool_calls` populated; my first probe read the wrong attr) |
| **Full `create_react_agent` loop** | ✅ **works end-to-end** (add-tool agent computed 21+21=42 through the proxy) |
| System prompts | ✅ accepted |
| Codegen smoke | ✅ valid Python (wrapped in ``` fences — normal; our pipeline already strips fences) |

**Conclusion: `standardcompute` is a drop-in for our entire react-agent stack — no parameter surgery needed.** The model-selection agent's claude-5 temperature caveats don't apply to this backend.

## Step 0 (free, do today, no infra): promote GLM-5.2

Per the model research: `CODE_WRITER_MODEL` is currently **`glm-5-turbo` — the budget tier** — while **GLM-5.2 (flagship coder, 1M context, 62.1% SWE-bench Pro) is already included free in the GLM Coding Plan**. One env var on the worker (local compose + Railway):

```
CODE_WRITER_MODEL=glm-5.2
```

This is also the honest A/B baseline: if glm-5.2 fixes the syntax errors, the LiteLLM switch may be unnecessary for now.

## Integration design (from the routing-map agent — verified minimal)

**The plumbing already exists.** `AGENT_MODEL_SETTINGS["code-writer"] → CODE_WRITER_MODEL` → `get_llm(model=...)`, and `get_llm()` is the single choke point for `openai_api_base`/`openai_api_key`. The change is **~25 lines in llm.py + 5 settings + env plumbing**:

### A. Provider resolver in `get_llm` (webapp/agents/llm.py)

```python
def _is_litellm(model: str) -> bool:
    if not getattr(settings, "LITELLM_ENABLED", True): return False
    if not getattr(settings, "LITELLM_API_KEY", ""): return False
    return model.startswith(getattr(settings, "LITELLM_MODEL_PREFIXES", ("litellm/",)))

def _provider_for(model: str) -> tuple[str, str]:
    if _is_litellm(model):
        return (settings.LITELLM_BASE_URL, settings.LITELLM_API_KEY)
    return (settings.ZAI_BASE_URL, settings.ZAI_API_KEY)

# in get_llm — ORDER IS LOAD-BEARING:
effective = effective_model(requested, fallback=_litellm_fallback(requested))
base_url, api_key = _provider_for(effective)   # AFTER the breaker swap
```

- **Prefix scheme**: `CODE_WRITER_MODEL=litellm/standardcompute` — the prefix IS the kill switch (unset the prefix → back to Z.AI). Add `"litellm/"` to `LITELLM_MODEL_PREFIXES` (NOT `openai/` — the proxy exposes exactly one aliased model, and a distinctive prefix prevents accidental matches).
- Model name sent to the proxy: `standardcompute` (strip the `litellm/` prefix before sending, or let the proxy alias it — decide in implementation; simplest is strip in `_provider_for`).

### B. The ONE real hazard: cross-provider circuit-breaker fallback

`llm_breaker.effective_model` returns `ZAI_FALLBACK_MODEL` (a GLM name) for ANY tripped model → a tripped `litellm/standardcompute` would silently route a GLM model name **to the LiteLLM proxy** (404) or GLM-on-ZAI (silent quality switch). Fix (3 lines, per the routing agent):

```python
# llm_breaker.py
def effective_model(primary, fallback=None): ...   # fallback param
# llm.py: LiteLLM models get LITELLM_FALLBACK_MODEL (default: none → no swap;
# the classified-retry layer already bounds transient failures)
```

The retry classifier itself is provider-agnostic (same OpenAI SDK exceptions from any /v1) — no changes.

### C. Settings + env

```python
# settings.py
LITELLM_BASE_URL = config("LITELLM_BASE_URL", default="https://llm.johnjf.xyz/v1")
LITELLM_API_KEY  = config("LITELLM_API_KEY", default="")
LITELLM_ENABLED  = config("LITELLM_ENABLED", default=True, cast=bool)
LITELLM_MODEL_PREFIXES = config("LITELLM_MODEL_PREFIXES", default="litellm/")
LITELLM_FALLBACK_MODEL = config("LITELLM_FALLBACK_MODEL", default="")   # empty = no breaker swap
```
Plus: docker-compose worker block, `.env.example`, Railway runbook Phase 7 paste block (key → ⋮ Seal).

### D. Timeouts (the one tuning risk)

`standardcompute` is a reasoning model — **slower per call than glm-5-turbo**. Current caps: per-call `LLM_REQUEST_TIMEOUT=300s`, wall-clock 900s. The react smoke was fast, but a 30-round code_writer run with deep thinking could press the 900s cap. Plan: keep defaults, add `LLM_REQUEST_TIMEOUT=600` for the code_writer call site if probe runs show timeouts (its `get_llm` already accepts `timeout=`).

### E. What needs NO changes (verified by the routing agent)

- `_strip_v_prefix_from_tools` — global monkey-patch, no-op for non-GLM models
- `headroom.compress(model="glm-5-turbo")` hardcodes — model is only used for token estimation
- Async path (`_invoke_agent_async`) — provider-agnostic
- Truncation caps — keep for cutover (bigger window = wasted headroom, the safe direction); per-model budgets noted as follow-up
- All other agents — stay on Z.AI via the same `get_llm` default path

## Rollout plan

1. **Step 0** (today): `CODE_WRITER_MODEL=glm-5.2` everywhere (free). Run 2-3 known sites; measure.
2. **Implement** the resolver + breaker fix + settings (behind `LITELLM_ENABLED`, default off until step 4).
3. **Unit tests**: resolver routing (prefix → litellm, bare → ZAI), breaker fallback stays provider-local, prefix stripping.
4. **E2E A/B** (local first, then Railway): 3 sites known to stress code_writer (books.toscrape = syntax errors historically, zquiet = the e2e baseline, one SFCC site) × both models. Metrics from the model agent: **first-attempt syntax-error rate** (ast.parse the draft), template-invariant retention (imports, MAX_DISCOVER_PAGES), tool-call schema failures, context growth.
5. **Railway**: add the 3 env vars + flip `CODE_WRITER_MODEL=litellm/standardcompute` on the worker. Kill switch = flip back to `glm-5.2` (one env var, no redeploy of code).

## Security note
The LiteLLM key was shared in chat — rotate it on the proxy when convenient. It'll also be a sealed var on Railway.

## Open questions for critique
1. Prefix `litellm/` vs reusing `openai/` (proxy exposes one model — which is less confusing later when more models are added)?
2. Strip the prefix client-side vs alias `litellm/standardcompute` in the proxy config (`model_name: litellm/standardcompute`)?
3. `LLM_REQUEST_TIMEOUT` for code_writer on a reasoning model — raise preemptively to 600 or wait for data?
4. Should the A/B run before or after the skills-FM deploy (both change code_writer's environment)?
