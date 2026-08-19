# Plan — code_writer via LiteLLM proxy (standardcompute) (v2, post-critique)

> Status: **v2 — critique round 1 folded in. Every load-bearing critique claim was
> re-verified in source or by live probe before acceptance.**
> Proxy: `https://llm.johnjf.xyz/v1`, model `standardcompute` (reasoning model, the
> only exposed model), key in hand (rotate — it was shared in chat).
> v1 verdict was **NO-GO as written**: one dead-code discovery (breaker), one
> backwards claim (context "safe direction"), one wrong "free" claim (glm-5.2), one
> underpowered A/B. All fixed below.

## Live probe results (all verified against the real proxy)

| Check | Result |
|---|---|
| `/v1/models` with key | ✅ exactly one model: `standardcompute` |
| Plain completion | ✅ works; is a **reasoning model** (`reasoning_content` field, needs generous max_tokens) |
| `temperature: 0.4` | ✅ accepted (no 400 — unlike claude-5/o1 families) |
| Raw tool calling (`tools=[...]`) | ✅ 1 valid call, correct args, `finish_reason: tool_calls` |
| langchain `ChatOpenAI.bind_tools` | ✅ works (`.tool_calls` populated) |
| **Full `create_react_agent` loop** | ✅ **works end-to-end** through the proxy |
| System prompts | ✅ accepted |
| **Context window (v2 probe)** | ✅ **180K chars = 36,177 prompt tokens → OK, 17s**; 360K chars = 72,177 tokens → **also OK, 18s**. Window ≥72K tokens — our 180K-char cap fits with ~2x margin |

**Conclusion: `standardcompute` is a drop-in for our react-agent stack, and the
context window is no longer a risk** — measured, not assumed. Latency note: prefill
of a 36K-token prompt took 17s with `max_tokens=32`; real generation calls will be
slower (reasoning model), which motivates the per-agent timeout fix (§D).

## What the critique found (verified before folding in)

1. **The circuit breaker is dead code on langchain-core 1.5.1.** Confirmed in the
   installed source: `CallbackManagerForLLMRun.on_llm_end`/`on_llm_error` forward
   only `run_id`/`parent_run_id`/`tags` — never `serialized`. So
   `_extract_model(None)` → `None` → `record_failure/record_success` return
   immediately (llm_breaker.py:55,75). **Zero breaker state has ever been
   recorded; `effective_model()` has always returned the primary.** v1's entire
   §B analyzed a hazard that cannot fire. Fix in §B below — and it dissolves the
   prefix-coherence question for free.
2. **Timeout plumbing doesn't reach the call site.** `subagents.py:679` calls
   `get_llm(model=..., temperature=...)` — no `timeout=`. Every react-loop call
   inherits the global `LLM_REQUEST_TIMEOUT=300s`. A reasoning model generating
   ~500 lines can exceed 300s **per call**, classified-retried up to 2× (3×300s
   inside the 900s wall), and the retry-cycle chain (graph.py:2910-2918 +
   `_fix_scraper_syntax` re-invocations) can stack toward the 7200s Celery
   `soft_time_limit`. Fix in §D.
3. **Step 0 "glm-5.2 is free" was wrong** — 3x quota multiplier peak / 2x
   off-peak on the shared GLM Coding Plan 5-hour window; code_writer is the most
   token-hungry agent and would tax the same quota every other agent draws on.
   Also: **Railway is already on glm-5.2** (runbook Phase 7 paste block,
   railway-migration.md:226) — so Railway baseline data is glm-5.2 data, and Step
   0 is a no-op there.
4. **v1's A/B (3 sites × 2 arms) was a smoke test masquerading as a go/no-go** —
   6 runs on a Bernoulli outcome with site-level variance can't support the
   syntax-error-rate claim, and the headline metric had no persisted data source
   (`ast.parse` outcome is transient; model attribution is only a
   `logger.info`). Split into smoke vs quality gate in §F.

## Integration design (v2)

**The plumbing already exists** — `AGENT_MODEL_SETTINGS["code-writer"] →
CODE_WRITER_MODEL → get_llm()`, and `get_llm()` is the single choke point for
`openai_api_base`/`openai_api_key`.

### A. Provider resolver in `get_llm` (webapp/agents/llm.py)

```python
def _provider_for(model: str) -> tuple[str, str, str]:
    """(base_url, api_key, model_name_to_send)."""
    prefixes = getattr(settings, "LITELLM_MODEL_PREFIXES", ("litellm/",))
    if getattr(settings, "LITELLM_ENABLED", True) and getattr(settings, "LITELLM_API_KEY", ""):
        for p in prefixes:
            if model.startswith(p):
                return (settings.LITELLM_BASE_URL, settings.LITELLM_API_KEY, model[len(p):])
    return (settings.ZAI_BASE_URL, settings.ZAI_API_KEY, model)

# in get_llm — ORDER IS LOAD-BEARING:
effective = effective_model(requested)            # breaker swap (per-provider, §B)
base_url, api_key, model_name = _provider_for(effective)   # AFTER the swap
```

- **Prefix scheme (Q1: settled)**: `litellm/` — must never collide with a real
  model name on either backend. `openai/` risks exactly that later.
- **Strip client-side (Q2: settled)**: in `_provider_for`, as shown. Proxy-side
  aliasing adds a second config surface for zero benefit — and it does NOT fix
  breaker-key divergence anyway (`llm_output['model_name']` echoes what the
  *server* says, so the callback would still record the post-strip name).

### B. Breaker: relocate recording out of the (dead) callback

The callback never worked on langchain-core 1.5.1 and cannot be repaired there
(`serialized` is simply not forwarded). `ClassifiedRetryChatOpenAI._generate` /
`_agenerate` (llm.py:155-173) already intercept **every** failure and already
know `self.model_name` — record there:

```python
def _generate(self, messages, stop=None, run_manager=None, **kwargs):
    cfg = _retry_settings()
    _super_generate = super()._generate
    try:
        result = _retry_classified_sync(lambda: _super_generate(...), cfg)
    except Exception as e:
        if not _is_caller_bug(e):          # auth/bad-request don't trip
            record_failure(self.model_name)
        raise
    record_success(self.model_name)
    return result
```

- **Key coherence by construction**: `self.model_name` is exactly the string
  `get_llm` configured → the breaker records and looks up the same key. The
  v1 prefix-divergence hazard is dissolved, not patched.
- **`effective_model` gets a fallback param with explicit None-semantics**:

```python
def effective_model(primary: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    # fallback=None → use the configured ZAI default (llm_breaker._config()[3]).
    # Do NOT write `fallback = fallback or _config()[3]` — that `or` silently
    # turns "no litellm fallback" into the ZAI default (cross-provider bug).
```

  `get_llm` passes `fallback=_litellm_fallback(requested)`: `""` for litellm
  models (→ no swap, stays on the proxy) and `None` for ZAI models (→
  `ZAI_FALLBACK_MODEL`).
- **`LITELLM_FALLBACK_MODEL` is not a knob — leave empty permanently.** The proxy
  exposes exactly one model; any non-empty litellm-side fallback 404s today.
- Keep `CircuitBreakerCallback` attached (harmless) or drop it — implementer's
  choice; recording no longer depends on it.
- Note: even fixed, the breaker consults at agent-build time, not per call — a
  tripped model reroutes on the *next* node invocation, never mid-react-loop.
  That's acceptable; per-call protection is the classified-retry layer's job.

### C. Settings + env

```python
# settings.py
LITELLM_BASE_URL = config("LITELLM_BASE_URL", default="https://llm.johnjf.xyz/v1")
LITELLM_API_KEY  = config("LITELLM_API_KEY", default="")
LITELLM_ENABLED  = config("LITELLM_ENABLED", default=True, cast=bool)
LITELLM_MODEL_PREFIXES = config("LITELLM_MODEL_PREFIXES", default="litellm/").split(",")
LITELLM_FALLBACK_MODEL = config("LITELLM_FALLBACK_MODEL", default="")  # leave empty — proxy has 1 model
```

Plus: docker-compose worker block, `.env.example`, Railway runbook Phase 7 paste
block (key → ⋮ Seal). **Rotate the key on the proxy FIRST, then seal the rotated
one** — rotating after sealing means two sealed-var updates and a window where
local and Railway use different keys.

### D. Timeouts (Q3: settled — raise preemptively, per-agent)

Thread `timeout=` through `_build_agent` (subagents.py:679 currently can't pass
one):

```python
# subagents.py
AGENT_LLM_TIMEOUTS = {"code_writer": int(getattr(settings, "CODE_WRITER_LLM_TIMEOUT", 600))}
llm = get_llm(model=_model_override, temperature=temperature,
              timeout=AGENT_LLM_TIMEOUTS.get(agent_name))
```

- Per-call 300s fires before the 900s wall does; waiting for data means
  collecting it from failed A/B runs. 600s for code_writer only; all other
  agents keep the global 300s default.
- **Instrument the A/B to record per-call latency** (see §F) so the 900s-wall
  question is answered with data.
- Wall-clock: leave 900s for the smoke; revisit only if smoke shows
  median-call > 180s.
- **Async abandonment (concurrency=2)**: with `LLM_ASYNC_EXECUTION=False`
  (default), a 900s timeout abandons the thread rather than cancelling it — the
  abandoned reasoning call holds its ~36K-token context while the retry cycle
  spawns a second code_writer LLM in the same process (documented celery-OOM
  history). **Set `LLM_ASYNC_EXECUTION=True` on the litellm A/B arm** (real
  cancellation via `asyncio.wait_for`); if that proves unstable, state the
  accepted leak explicitly and cap concurrency instead.

### E. What needs NO changes

- `_strip_v_prefix_from_tools` — global no-op for non-GLM models
- `headroom.compress(model="glm-5-turbo")` — model only used for token estimation
- Async path (`_invoke_agent_async`) — provider-agnostic
- **Truncation caps: KEEP — now with measurement.** 180K chars ≈ 36K tokens
  (4.6 chars/token measured); window proven ≥72K tokens. Real-world worst case
  (36K + system-with-full-template + seed ≈ 45-55K) fits with margin. The
  un-trimmable floor (system prompt + seed) is inside the measured envelope.
- **Streaming: nothing streams** — no `.stream(`/`astream` anywhere in
  `webapp/agents/`; Django SSE is job logs, not LLM tokens.
- Token-usage readers: **none exist** (grepped `token_usage|usage_metadata|
  total_tokens|prompt_tokens|completion_tokens` across webapp/ + src/ — zero
  hits); a None/zero `usage` from the proxy breaks nothing. `max_tokens` is
  never set by us — server default governs.
- All other agents stay on Z.AI via the same `get_llm` default path.

## Rollout plan (revised)

0. **Sequencing gate (Q4: settled — AFTER skills-FM).** The branch carries ~25
   unpushed commits including skills-FM, which changes what `load_skill` returns
   to code_writer — i.e., changes its prompt content and context size, the exact
   variables the A/B measures. **Push + deploy skills-FM first → then A/B → then
   flip.** (Railway job 2 baseline predates skills-FM — usable as a smoke
   reference only, not a quality baseline.)
1. **Step 0 reframed (no longer "free"):** Railway already runs glm-5.2 —
   nothing to do there. Local compose: optionally set `CODE_WRITER_MODEL=glm-5.2`
   for parity, knowing it draws 3x/2x quota from the shared 5-hour window every
   other agent also uses. Check the quota window before/after any glm-5.2 runs.
2. **Implement** §A resolver + §B breaker relocation + §C settings + §D timeout
   threading (~60-80 lines incl. tests, not v1's ~25). Default `LITELLM_ENABLED`
   behavior: no litellm model names configured → nothing routes there, so the
   merge is inert until the env var flip.
3. **Unit tests**: resolver routing (prefix → litellm + stripped name; bare →
   ZAI), breaker records via retry layer (mock `_generate` failure → state
   updated under configured key), `fallback=None` semantics (no cross-provider
   `or`-trap), timeout threading reaches `get_llm`.
4. **A/B — split into two gates (v1 conflated them):**
   - **Smoke (3 sites × 2 arms — books.toscrape, zquiet, one SFCC):** gates
     *plumbing only* — calls complete, per-call latency distribution, no
     per-call timeouts, no 400s, template invariants intact (imports,
     `MAX_DISCOVER_PAGES`).
   - **Quality gate (≥5 runs/site/arm, site as blocking factor, ≥15/arm):**
     decides syntax-error rate. **Only if we intend to claim quality.** If we
     flip on "smoke clean + no regression" instead, say that explicitly.
   - **Prerequisite:** persist the metric — write `ast.parse` outcome +
     model name into the job record (today it's transient inside
     `_fix_scraper_syntax` and model attribution is only `logger.info`). Small
     change: one field on SessionLog/Job at draft-write time.
5. **Railway flip:** add env vars (key sealed, rotated first per §C) +
   `CODE_WRITER_MODEL=litellm/standardcompute` on the worker. **Kill switch:**
   unset the prefix → back to glm-5.2, no redeploy. **Stance (deliberate):** if
   the proxy is fully down, code_writer hard-fails — with
   `LITELLM_FALLBACK_MODEL` permanently empty and (pre-fix) no live breaker
   there is no degradation path. Acceptable for a managed proxy; revisit after
   the breaker is alive (§B), at which point a GLM-name fallback becomes
   possible if ever desired.

## Security note

The LiteLLM key was shared in chat — **rotate on the proxy first, then seal on
Railway** (ordering matters, §C). Railway token + superuser password likewise
advised rotated.

## Critique-round log

- **Round 1 (2026-08-19):** NO-GO as written. 7 attack vectors; 3 FIX, 1 BLOCKED
  (breaker dead code), all claims independently re-verified before folding in
  (breaker forwarding inspected in installed langchain-core 1.5.1; timeout
  plumbing grep at subagents.py:679; Railway glm-5.2 at railway-migration.md:226;
  context window re-probed live — 180K chars OK in 17s, resolving the "biggest
  hole" in the safe direction). v2 answers all four open questions (Q1 `litellm/`
  prefix, Q2 client-side strip, Q3 per-agent 600s preemptive, Q4 after
  skills-FM). Estimate corrected ~25 → ~60-80 lines.
