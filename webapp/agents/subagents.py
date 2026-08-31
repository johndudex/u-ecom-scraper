"""Agent subgraph factories for the LangGraph scraping workflow.

Each agent in the scraping pipeline is instantiated as a ``create_react_agent``
from ``langgraph.prebuilt``.  These factory functions centralize the
configuration (system prompt, LLM temperature, tool set) for every agent so
that the main graph assembly in ``graph.py`` stays declarative.

Temperature values are drawn from the original OpenCode agent definitions in
``opencode.json`` / ``.opencode/agents/*.md`` frontmatter.

Usage (from graph.py)::

    from webapp.agents.subagents import create_site_analyzer
    agent_subgraph = create_site_analyzer()
    workflow.add_node("site_analyzer", agent_subgraph)
"""

from __future__ import annotations

import logging
import os
import re

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

from .constants import DEAD_STATUS_CODES, FINAL_RETRY_SENTINEL
from .llm import get_main_llm, get_llm
from .prompts import load_agent_prompt

logger = logging.getLogger(__name__)

# ── Temperature mapping from .opencode/agents/*.md frontmatter ──────────────

# Per-agent sampling temperatures (Phase 5: configurable for A/B determinism).
# When LLM_CODEGEN_DETERMINISTIC is True, code-writer + product-analyzer are
# forced to temperature 0 to narrow the codegen distribution (NOTE: z.ai does
# not reliably honor seed, so this narrows but does not guarantee determinism;
# the behavioral floor lives in route_after_testing's ground-truth + coverage
# gates + the _probe_phase1_discovery probe). Default False — temp=0 can reduce
# code_writer's exploration on hard sites, so keep current temps unless A/B
# testing shows temp=0 helps.
def _agent_temperatures() -> dict[str, float]:
    base = {
        "site-analyzer": 0.2,
        "product-analyzer": 0.2,
        "nav-skill-review": 0.2,
        "code-writer": 0.4,
        "code-tester": 0.1,
        "cleanup": 0.1,
        "skill-learner": 0.3,
        "dagster-converter": 0.1,
    }
    try:
        from django.conf import settings

        # Per-agent env overrides: CODE_WRITER_TEMP etc.
        for stem in list(base):
            env_val = getattr(settings, f"AGENT_TEMP_{stem.upper().replace('-', '_')}", None)
            if env_val is not None:
                base[stem] = float(env_val)
        if getattr(settings, "LLM_CODEGEN_DETERMINISTIC", False):
            base["code-writer"] = 0.0
            base["product-analyzer"] = 0.0
    except Exception:
        pass
    return base


AGENT_TEMPERATURES: dict[str, float] = _agent_temperatures()

# Per-agent model overrides (keyed by prompt_stem, like AGENT_TEMPERATURES).
# Value is the NAME of a settings attr holding the model; resolved lazily in
# _build_agent (settings isn't imported at module level). Agents not listed use
# the main model (get_main_llm / ZAI_MAIN_MODEL). code_writer is pure codegen —
# analysis/nav/field-mapping is done upstream and handed to it as structured
# summaries — so it runs on the faster flash model.
# Set CODE_WRITER_MODEL=glm-5-turbo (or =ZAI_MAIN_MODEL) to revert.
AGENT_MODEL_SETTINGS: dict[str, str] = {
    "code-writer": "CODE_WRITER_MODEL",
}


def _agent_llm_timeouts() -> dict[str, int]:
    """Per-agent per-call LLM timeout overrides (keyed by prompt stem, like
    AGENT_MODEL_SETTINGS). Read lazily so tests can override settings without
    import-order issues. code_writer gets a longer budget than the 300s global
    default — reasoning models generating ~500-line drafts can exceed 300s on a
    single call, and classified retry would multiply that inside the 900s wall."""
    try:
        from django.conf import settings as _s

        return {"code-writer": int(getattr(_s, "CODE_WRITER_LLM_TIMEOUT", 600))}
    except Exception:
        return {}


# Keyed by prompt stem (e.g. "code-writer" — NOT the node name "code_writer").
_AGENT_LLM_TIMEOUTS: dict[str, int] = _agent_llm_timeouts()

# ── Internal name mapping: agent node name → prompt file stem ─────────────

AGENT_PROMPT_MAP: dict[str, str] = {
    "site_analyzer": "site-analyzer",
    "product_analyzer": "product-analyzer",
    # ═══ ARCHIVED (replaced by browser_traverse) ═══
    # "navigation_agent": "navigation-agent",
    # "navigation_explore": "navigation-agent",
    # ══════════════════════════════════════════════
    "nav_skill_review": "nav-skill-review",
    "code_writer": "code-writer",
    "code_tester": "code-tester",
    "cleanup": "cleanup",
    "skill_learner": "skill-learner",
    "dagster_converter": "dagster-converter",
}


# T1.7: the dead AGENT_MAX_ITERATIONS dict ({site_analyzer: 30, code_writer:
# 20, ...}) was DELETED — zero importers (the graph caps react loops via
# AGENT_RECURSION_MAP in graph.py + _AGENT_INVOKE_TIMEOUT; the playground has
# its own AGENT_MAX_ITERATIONS_LOOKUP in scraper/tasks.py). Do NOT re-unify
# them: they count DIFFERENT UNITS (recursion_limit = graph steps; the lookup =
# playground LLM turns, ~2.5× apart) and the playground's smaller budget is
# its only bound — inheriting graph values would make it unbounded, and
# capping the graph at lookup values would GraphRecursionError at scale.

BROWSER_UNAVAILABLE_WARNING = (
    "\n\n"
    "NOTE: Playwright MCP browser tools are unavailable. "
    "Use probe_page to access the page — it handles proxy escalation "
    "automatically. If probe_page also fails, write analysis based on "
    "URL structure and any existing workspace artifacts."
)


def _build_content_type_context(state: dict) -> str:
    """Build a concise content-type context block for agent messages."""
    content_type_config = state.get("content_type_config", {})
    if not content_type_config:
        return ""
    ct_name = content_type_config.get("content_type", "")
    output_key = content_type_config.get("output_key", "products")
    fields = content_type_config.get("fields", [])
    if not ct_name and not fields:
        return ""
    lines = ["### Content Type Context\n"]
    if ct_name:
        lines.append(f"- Scraping content type: **{ct_name}**")
    if output_key:
        lines.append(f"- Output key in JSON: `{output_key}`")
    if fields:
        core = [f for f in fields if f.get("required")]
        if core:
            field_names = ", ".join(f["name"] for f in core)
            lines.append(f"- Core fields to expect: {field_names}")
    return "\n".join(lines) + "\n\n"


# ── Field-map summarizer constants + helpers (T1.1 / T1.5) ─────────────────
# The seed message is truncation-exempt (headroom only compresses tool output),
# so every cap below is LOAD-BEARING — it is the only thing bounding these
# blocks.

# method values that mean "the value comes from a JSON API response body".
# A field is also API-shaped when it carries an explicit api_path /
# api_fallback_path regardless of the method label.
_API_FIELD_METHODS = ("api", "internal_api")

# Char caps.
_FIELD_NOTE_CAP = 300
_API_EXTRACTION_CAP = 600

# T1.2 relays (code-tester artifacts). Bounded — the writer's seed message is
# truncation-exempt, so an unbounded relay re-inflates the context the summary
# exists to shrink.
_ISSUE_FIX_CAP = 300
_WRITER_FEEDBACK_CAP = 600
# T2.6: the EXACT-FAILURE block (failing run's argv + error tail) gets its own
# cap so a chatty crash can't eat the feedback/caps above — the writer must
# always see WHAT TO PRESERVE even when the failure text is huge.
_EXACT_FAILURE_CAP = 1200

# T1.5 (I7): exact strategy tokens a mechanism_reassessment verdict may carry.
# Value-match, NOT a key-alias set, and NEVER ``scraping_mechanism`` (a key
# ``_derive_strategy`` itself writes — a restated old verdict there would be
# self-confirming).
_MR_VERDICT_TOKENS = ("http_requests", "http_navigation", "playwright")

# Marker ``_derive_strategy`` writes into strategy_justification when the
# mechanism verdict was APPLIED (tests/test_job12_strategy_gate.py pins the
# substring contract).
_MR_APPLIED_MARKER = "mechanism_reassessment"

# ── run_scraper cap buckets (T1.4 / I4 — code_writer ONLY) ─────────────────
# A single flat cap let probe-family scratch runs (probe*.py / test_*.py /
# debug*.py) consume the whole budget, so the writer handed off a draft it had
# never executed. Buckets are counted INDEPENDENTLY: probe-family 2 + draft 2.
# "other" keeps the pre-split flat cap of 3 so an unclassified target is no
# looser than before the split. Scope guard (regression critic): this binds
# ONLY code_writer — code_tester legitimately re-runs the draft across fix
# cycles and stays UNCAPPED; do not move the guard into shell_tools.
RUN_SCRAPER_PROBE_PREFIXES = ("probe", "test_", "debug")
RUN_SCRAPER_DRAFT_PREFIX = "scraper_draft"
RUN_SCRAPER_CAPS = {"probe_family": 2, "scraper_draft": 2, "other": 3}
_RUN_SCRAPER_BUCKET_LABELS = {
    "probe_family": "probe-family (probe*/test_*/debug* targets)",
    "scraper_draft": "scraper_draft*",
    "other": "other targets",
}


def _run_scraper_bucket(target: object) -> str:
    """Classify a run_scraper target (path or bare stem) into its cap bucket."""
    base = os.path.basename(str(target or "").replace("\\", "/").strip()).lower()
    stem = base.removesuffix(".py")
    if stem.startswith(RUN_SCRAPER_PROBE_PREFIXES):
        return "probe_family"
    if stem.startswith(RUN_SCRAPER_DRAFT_PREFIX):
        return "scraper_draft"
    return "other"


def _run_scraper_target(args: tuple, kwargs: dict) -> str:
    """Pull the run_scraper target out of a guard invocation."""
    target = kwargs.get("scraper_path") or kwargs.get("scraper")
    if not target:
        for arg in args:
            if isinstance(arg, str) and (
                arg.endswith(".py") or "/" in arg or "\\" in arg
            ):
                target = arg
                break
    return str(target or "")


def _is_api_field(info: object) -> bool:
    """True when a field-map entry is fed by a JSON API response body."""
    if not isinstance(info, dict):
        return False
    if str(info.get("method", "")).lower() in _API_FIELD_METHODS:
        return True
    return bool(info.get("api_path") or info.get("api_fallback_path"))


def _mr_has_verdict(mr: object) -> bool:
    """True when a mechanism_reassessment block carries a strategy verdict.

    Recursive VALUE-match confined to the block itself (``recommended`` is the
    canonical key but recorded artifacts also used ``reassessed_mechanism``) —
    any string exactly equal to one of the enum tokens counts. It is NOT a
    key-alias set: the ``scraping_mechanism`` key ``_derive_strategy`` itself
    writes lives on scraper_analysis, OUTSIDE this block, and is never scanned
    here — a restated old verdict there must not read as self-confirmation.
    Non-string / non-exact-token values never match (poison descriptors report
    opinions under invented tokens).
    """
    if not isinstance(mr, dict):
        return False
    stack: list = [mr]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and cur.strip() in _MR_VERDICT_TOKENS:
            return True
    return False


def _suppress_mechanism_reassessment(
    mr: object, scraper_analysis: object
) -> bool:
    """T1.5 (I7): decide whether the mechanism_reassessment block is injected.

    DEFAULT is render — resumed jobs and any run where scraper_analysis is
    absent/unreadable must keep the verdict evidence. SUPPRESS only when the
    strategy gate ARMED and REJECTED the verdict: scraper_analysis exists, its
    strategy_justification does not cite the applied-verdict marker, and the
    block does carry a verdict. In that state the block argues against the
    message's own strategy instruction.
    """
    if not isinstance(mr, dict) or not mr:
        return False
    sa = scraper_analysis if isinstance(scraper_analysis, dict) else {}
    if not sa:
        return False
    justification = str(sa.get("strategy_justification") or "")
    if _MR_APPLIED_MARKER in justification:
        return False
    return _mr_has_verdict(mr)


def _render_api_extraction(ax: object, cap: int = _API_EXTRACTION_CAP) -> str:
    """Bounded ``api_extraction`` render (T1.1) — the endpoint URL, the
    code-extraction pattern, the response sample keys and ONE example entry.

    priceline's raw block is 2.3K (headers, code_from_url, every
    key_response_fields entry). The seed is truncation-exempt, so verbatim
    injection would move all of it into untrimmable context; this keeps what
    codegen needs and hard-caps the rest.
    """
    if not isinstance(ax, dict) or not ax:
        return ""
    import json as _json

    parts: list[str] = []
    url = ax.get("endpoint") or ax.get("url") or ax.get("api_url") or ""
    if url:
        parts.append(f"endpoint=`{ax.get('method') or 'GET'} {url}`")
    cfu = ax.get("code_from_url")
    if isinstance(cfu, dict) and cfu.get("regex"):
        parts.append(f"code_from_url=`{cfu['regex']}`")
    keys = ax.get("sample_keys") or ax.get("keys") or ax.get("response_keys") or []
    if isinstance(keys, dict):
        keys = list(keys)
    if keys:
        parts.append(f"sample_keys={list(keys)[:12]}")
    out = "; ".join(parts)
    example = _one_api_example(ax)
    if example is not None:
        prefix = ("; " if out else "") + "example="
        room = cap - len(out) - len(prefix) - 1
        if room >= 80:
            blob = _json.dumps(example, ensure_ascii=False)
            if len(blob) > room:
                blob = blob[: room - 1].rstrip() + "…"
            out += prefix + blob
    if len(out) > cap:
        out = out[: cap - 1] + "…"
    return f"\n**API Extraction:** {out}" if out else ""


def _one_api_example(ax: dict) -> object | None:
    """ONE worked response-field example from an api_extraction block."""
    krf = ax.get("key_response_fields")
    if isinstance(krf, dict) and krf:
        name = next(iter(krf))
        return {name: krf[name]}
    for key in ("sample_record", "example", "sample_response"):
        val = ax.get(key)
        if isinstance(val, dict) and val:
            return val
        if isinstance(val, list) and val:
            return val[0]
    return None


def _summarize_product_analysis(
    pa: dict,
    allowed: set[str] | None = None,
    scraper_analysis: dict | None = None,
) -> str:
    """Complete-but-lean summary of product_analysis for code_writer.

    Replaces ``read_file`` of the full product_analysis.json (20K+). Includes
    EVERYTHING code_writer needs to write the scraper:

    - the per-field extraction map (method + selector/API path + fallback +
      JS snippet + API notes) — compacted; ``examples``/``expectations``/
      ``tested`` are dropped (validation is code_tester's concern, not
      extraction) EXCEPT as the [T1.8] UNVERIFIED evidence probe: a field
      carrying neither an example value nor a verified/tested flag renders
      with an explicit ``[UNVERIFIED — analyzer guess…]`` marker so
      code_writer knows to wire a fallback instead of trusting the selector.
      API-method fields are PINNED into the rendered set: the
      core-first ``_MAX_FIELDS`` cap would otherwise drop the non-core API
      field the map was extended to surface.
    - ``page_structure``, ``extraction_methods``, ``jsonld_extraction``,
      ``site_analysis_review``, ``variants`` — included verbatim (each is
      small; these carry the page layout, the primary extraction approach,
      JSON-LD details + field guidance like "title truncated -> use CSS h1").
    - ``mechanism_reassessment`` — CONDITIONAL (see
      ``_suppress_mechanism_reassessment``).
    - ``api_extraction`` — bounded render (endpoint + sample keys + one
      example entry), never the raw block.
    - ``content_type`` / ``output_key`` scalars.

    Only genuinely redundant/non-codegen keys are dropped: ``connectivity``
    (site_analysis / scraper_analysis_section already cover it),
    ``confidence_score``, ``site_slug``, ``analyzed_products`` (a count).
    ~6-7K vs 24K, with zero loss of codegen-relevant information.
    """
    import json as _json

    pa = pa or {}
    fields = pa.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        return ""
    # Schema enforcement: when a requested schema exists, narrow the Field
    # Extraction Map to it (code_writer can't emit what it can't see). If the
    # schema has no overlap with the mapped fields, leave the map unchanged
    # (the gap is surfaced by validate_coverage).
    if allowed:
        _filtered = {k: v for k, v in fields.items() if k in allowed}
        if _filtered:
            fields = _filtered
    lines = ["\n### Product Analysis (COMPLETE summary — do NOT read the file)\n"]
    # Per-field extraction map (compact). Cap at core + top-12 non-core to keep
    # the generated scraper concise (35+ fields → 972-line scraper blows up context).
    _CORE_FIELDS = {"title", "price", "url", "src_url", "company", "location",
                    "description", "posted_date", "salary", "job_id", "availability",
                    "currency", "employment_type", "specialty", "profession"}
    _sorted_fields = sorted(
        fields.items(),
        key=lambda kv: (0 if kv[0] in _CORE_FIELDS else 1, kv[0])
    )
    _shown = 0
    _MAX_FIELDS = 15
    lines.append("**Field Extraction Map:**")
    for name, info in _sorted_fields:
        # T1.1 guard: API-method fields are PINNED — they are exactly the
        # non-core entries (e.g. ratings on an API-fed site) the core-first
        # cap would silently drop, and the api_path/notes this loop renders
        # are the only place code_writer sees how to read them.
        if _shown >= _MAX_FIELDS and name not in _CORE_FIELDS and not _is_api_field(info):
            continue
        _shown += 1
        if not isinstance(info, dict):
            lines.append(f"- **{name}**: {info}")
            continue
        method = info.get("method", "?")
        sel = (
            info.get("selector")
            or info.get("path")
            or info.get("api_path")
            or info.get("api_fallback_path")
            or ""
        )
        fb = (
            info.get("jsonld_fallback")
            or info.get("css_fallback")
            or info.get("fallback")
            or ""
        )
        js = info.get("js_extraction") or ""
        line = f"- **{name}** [{method}]"
        if sel:
            line += f" sel=`{sel}`"
        if fb:
            line += f" fallback=`{fb}`"
        if js:
            line += f" js=`{js}`"
        # API-fed fields carry their read recipe in `notes` (which path wins,
        # when the fallback applies, what to strip). Without it the rendered
        # api_path is an unlabelled key into an undocumented response.
        if _is_api_field(info):
            notes = re.sub(r"\s+", " ", str(info.get("notes") or "")).strip()
            if notes:
                if len(notes) > _FIELD_NOTE_CAP:
                    notes = notes[: _FIELD_NOTE_CAP - 1] + "…"
                line += f' notes="{notes}"'
        # [T1.8/wave-13] Honesty marker: a map entry with NO live-page
        # evidence (no example value, not flagged verified/tested) is the
        # analyzer's best GUESS. The writer must treat it as a hypothesis —
        # wire a fallback source — not as ground truth. Job-118 class: a
        # price mapped from an unverified selector sat empty on all 20
        # extracted rows and nothing downstream said the map was a guess.
        # [T2.7/wave-13] `tested` carries either the analyzer's boolean or a
        # field_verification string verdict ("verified"/"empty"/"skipped") —
        # bool("empty") is truthy, so a string-verdict-aware read is required
        # or a PROVEN-DEAD source would suppress the honesty marker.
        try:
            _has_example = bool(
                info.get("example")
                or info.get("examples")
                or info.get("sample_value")
            )
            _tested_raw = info.get("tested")
            if isinstance(_tested_raw, str):
                _tested_ok = _tested_raw.strip().lower() == "verified"
            else:
                _tested_ok = bool(_tested_raw)
            _verified = bool(info.get("verified")) or _tested_ok
            if str(_tested_raw or "").strip().lower() == "empty":
                line += (
                    " [VERIFIED EMPTY — the live-render check produced NO value "
                    "for this source; do NOT ship this mapping as-is — re-anchor "
                    "to a populated source (JSON-LD / embedded JSON / CSS) first]"
                )
            elif _tested_ok and info.get("resolved_value"):
                line += f' value="{str(info["resolved_value"])[:60]}"'
            elif isinstance(info, dict) and not _has_example and not _verified:
                line += (
                    " [UNVERIFIED — analyzer guess, no live-page evidence; "
                    "wire a fallback source and verify on the live page]"
                )
        except AttributeError:
            pass
        lines.append(line)
    # [T2.7/wave-13] Verification summary (written by normalize_fields' live
    # render): counts + the named dead sources, so the writer sees at a glance
    # which map entries are proven vs guessed.
    _fv = pa.get("field_verification")
    if isinstance(_fv, dict) and _fv:
        _fv_line = (
            f"**Field Verification** (live render via {_fv.get('method', '?')}): "
            f"verified={_fv.get('verified', 0)}, empty={_fv.get('empty', 0)}, "
            f"skipped={_fv.get('skipped', 0)} — `verified` sources are PROVEN on "
            "the live page (trust them); `empty` sources produced NOTHING and "
            "must be re-anchored before shipping."
        )
        _ef = _fv.get("empty_fields")
        if _ef:
            _fv_line += f" Dead sources: {', '.join(str(x) for x in _ef[:10])}."
        lines.append(_fv_line)
    # T1.5 (I7): mechanism_reassessment is CONDITIONAL — render by default,
    # suppress only when the strategy gate armed and rejected the verdict
    # (injecting it then argues against the message's own strategy).
    if not _suppress_mechanism_reassessment(
        pa.get("mechanism_reassessment"), scraper_analysis
    ):
        _mr = pa.get("mechanism_reassessment")
        if _mr:
            lines.append(
                f"**Mechanism Reassessment:** {_json.dumps(_mr, ensure_ascii=False)}"
            )
    # The other extraction-relevant sections — verbatim (small + complete).
    for key, label in (
        ("page_structure", "Page Structure"),
        ("extraction_methods", "Extraction Methods"),
        ("jsonld_extraction", "JSON-LD Extraction"),
        ("site_analysis_review", "Site Analysis Review"),
        ("variants", "Variants"),
    ):
        val = pa.get(key)
        if val:
            _rendered = _json.dumps(val, ensure_ascii=False)
            # [T1.9/wave-13] A raw analyzer JSON-LD dump can be the single
            # largest block in the writer seed; cap it. The actionable
            # structure still reaches the writer through the field map and
            # _fetch_rendered_jsonld's projection.
            if key == "jsonld_extraction" and len(_rendered) > 2500:
                _rendered = _rendered[:2500] + " …(truncated)"
            lines.append(f"**{label}:** {_rendered}")
    # T1.1: api_extraction bounded to url + code pattern + sample keys + ONE
    # example entry (never the raw block — the seed is truncation-exempt).
    _api_section = _render_api_extraction(pa.get("api_extraction"))
    if _api_section:
        lines.append(_api_section)
    # Tiny scalars code_writer references.
    if pa.get("content_type"):
        lines.append(f"**content_type:** {pa['content_type']}")
    if pa.get("output_key"):
        lines.append(f"**output_key:** {pa['output_key']}")
    return "\n".join(lines) + "\n"


def _summarize_navigation_extras(na: dict) -> str:
    """Navigation mechanics the structured nav_section doesn't already inject:
    filters (e.g. a POST-form discovery strategy), search/category notes. Keeps
    code_writer from needing to read_file navigation_analysis.json.
    """
    na = na or {}
    lines = []
    fl = na.get("filters") or {}
    if isinstance(fl, dict) and (fl.get("has_filters") or fl.get("description")):
        lines.append(
            f"**Filters:** method={fl.get('method', '?')} — {fl.get('description', '')}"
        )
    s = na.get("search") or {}
    if isinstance(s, dict) and s.get("description"):
        lines.append(f"**Search notes:** {s['description']}")
    cats = na.get("categories") or {}
    if isinstance(cats, dict) and cats.get("description"):
        lines.append(f"**Categories:** {cats['description']}")
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def _embedded_json_listing_urls(navigation_analysis: dict, limit: int = 15) -> list[str]:
    """Listing/category URLs to fetch for the embedded-JSON model (deduped)."""
    na = navigation_analysis or {}
    urls: list[str] = []
    search = na.get("search") or {}
    if isinstance(search, dict):
        for k in ("working_url", "listing_url_used"):
            v = search.get(k)
            if isinstance(v, str) and v and not v.startswith(("javascript", "#")):
                urls.append(v)
    cats = na.get("categories") or {}
    if isinstance(cats, dict):
        for c in cats.get("category_links") or []:
            if isinstance(c, str) and c:
                urls.append(c)
    return list(dict.fromkeys(urls))[:limit]


def _embedded_json_code_writer_section(
    navigation_analysis: dict, scraper_analysis: dict, state: dict, slug: str
) -> str:
    """code_writer guidance for the EMBEDDED-JSON listing data model.

    The site embeds its items as a JSON array inside ``<script>`` tags in the
    listing/category page HTML (server-rendered hydration data — a
    ``window.X=[...]`` assignment, Next.js ``__NEXT_DATA__``, Nuxt ``__NUXT__``,
    or a JSON-LD ``ItemList``). The listing page ITSELF contains every record,
    so there is **no per-detail-page Phase 2** and **no backend search API** —
    this supersedes the two-phase detail-page text. Content-agnostic (works for
    products, jobs, articles…). Returns "" when the signal is absent.
    """
    na = navigation_analysis or {}
    sa = scraper_analysis or {}
    emb = na.get("embedded_json") or sa.get("embedded_json") or {}
    best = (emb.get("best") or {}) if isinstance(emb, dict) else {}
    if not isinstance(best, dict) or not best.get("record_count"):
        return ""

    strategy = (sa.get("strategy") or "").lower()
    is_http = strategy in ("http_requests", "requests", "internal_api", "api")
    page_type = (state.get("page_type") or "").lower()
    is_job = page_type.startswith("job")

    locator = best.get("locator") or ""
    kind = best.get("kind") or "inline_script"
    sample_keys = list(best.get("sample_keys") or [])
    record_count = best.get("record_count")
    array_path = best.get("array_path") or ""
    sample_record = best.get("sample_record") or {}
    urls = _embedded_json_listing_urls(na)

    lines = [
        "\n### CRITICAL — EMBEDDED-JSON LISTING data model (PREFERRED — do NOT scrape detail pages)\n",
        "This site embeds its items as a JSON array inside ``<script>`` tags in the "
        "LISTING/category page HTML (server-rendered hydration data — e.g. a "
        "``window.X=[...]`` assignment, Next.js ``__NEXT_DATA__``, Nuxt "
        "``__NUXT__``, or a JSON-LD ``ItemList``). The listing page ITSELF "
        "contains every record, so there is **NO per-detail-page Phase 2** and "
        "**NO backend search API**. Do NOT follow the two-phase detail-page "
        "discovery described below — it does not apply to this data model.\n",
        "**Detected signal** (from the rendered listing page):",
        f"- source kind: `{kind}`",
        f"- locator (re-find this at runtime): `{locator or '(scan all inline <script>)'}`",
    ]
    if array_path:
        lines.append(f"- array path inside the blob: `{array_path}`")
    lines.append(f"- record count on the sampled page: **{record_count}**")
    if sample_keys:
        lines.append(f"- sample record keys: `{sample_keys}`")
    if isinstance(sample_record, dict) and sample_record:
        import json as _json

        try:
            lines.append(
                "- sample record (first item, truncated):\n```json\n"
                + _json.dumps(sample_record, ensure_ascii=False)[:800]
                + "\n```"
            )
        except Exception:
            pass

    lines += [
        "\n**Extraction recipe (single phase — listing pages → records):**",
        f"1. For each listing/category URL, fetch the page HTML. Use "
        f"**{'HTTP `requests`/`httpx` (the data is server-rendered)' if is_http else 'a browser via `_navigate`/Playwright (the data needs JS rendering)'}** "
        f"— the strategy is `{strategy or 'http_navigation'}`.",
        "2. Locate the JSON array. If a `locator` is given, find the `<script>` "
        "whose text contains it (BeautifulSoup "
        "`soup.find('script', string=re.compile(re.escape(locator)))` or a regex "
        "`NAME\\s*=\\s*\\[`). For `__NEXT_DATA__`/`__NUXT__` parse "
        "`soup.find(id='__NEXT_DATA__')`; for JSON-LD parse the "
        "`application/ld+json` blocks. Extract the array with a BALANCED-BRACKET "
        "scan (the blob has nested brackets/quotes — a greedy `\\[.*?\\]` regex "
        "WILL truncate it), then `json.loads`.",
        "3. If the array is nested (array_path given), navigate to it; collect "
        "every record.",
        "4. **Paginate + dedup**: iterate ALL category URLs (below) AND each "
        "category's pagination (`?page=N` / next-button / load-more) until a page "
        "returns no new records; **dedup by the item's id field** across "
        "pages+categories. Do not cap the count.",
    ]
    if urls:
        lines.append(
            f"5. **Listing/category URLs to fetch** (seed discovery here; also "
            f"saved in `workspace/{slug}/input_urls.json`):"
        )
        for u in urls:
            lines.append(f"   - `{u}`")

    if is_job:
        lines += [
            "\n**Field mapping (jobs — generic, NO site-specific keys):** "
            "`from src.job_fields import map_jobs` then "
            "`jobs = map_jobs(sample_items=first_batch, raw_items=all_records)`. "
            "`map_jobs` infers each standard field (title, company, location, "
            "salary, job_type, posted_date, apply_url, requirements) by coverage "
            "over the sample. If records have a job id but no url, construct "
            "`url` per item from the id + the job-link pattern. Verify per-field "
            "coverage in code_tester's `results.field_coverage`; add missing "
            "aliases to `JOB_ALIASES` in `src/job_fields.py` (NOT the scraper).",
        ]
    else:
        lines += [
            "\n**Field mapping (generic):** map each record's fields to the "
            "output schema using the `product_analysis` field-extraction map and "
            "the generic JSON-LD/CSS resolver the template already provides "
            "(`_populate_from_jsonld` / `transform_api_product`). Read field "
            "names from the sample record above — do NOT hardcode site-specific keys.",
        ]
    lines.append(
        "\n**Output:** write the full list to `output_{datetime}.json` under the "
        "content type's output key. The default (no-args) run must do the FULL "
        "extraction across all categories.\n"
    )
    return "\n".join(lines)


def _get_skill_descriptions() -> str:
    """Bullet list of skill names + descriptions for the system prompt.

    This is the *progressive disclosure* layer: agents see lightweight
    descriptions and call ``load_skill`` for full content on demand.

    Skills resolve via ``src.skills_store`` (File Master first, image seed
    fallback; the 2 image-only UI-authoring skills are excluded from the
    scan — they were 2.7MB of per-build full-file reads and are irrelevant
    to scraping agents). The snapshot is cached in-process and invalidated
    on any skills_store write; descriptions only change on
    create_new_skill (learn_skill appends below the frontmatter).
    """
    try:
        from src.skills_store import descriptions_snapshot

        snap = descriptions_snapshot()
    except Exception:
        return ""

    lines = [f"- **{name}**: {desc}" for name, desc in sorted(snap.items())]
    if not lines:
        return ""

    return (
        "\n\n## Available Skills\n\n"
        "You have access to specialized scraping skills. Use `load_skill` "
        "to load full instructions when relevant.\n\n"
        + "\n".join(lines)
        + "\n\n**IMPORTANT:** When you detect a platform matching a skill "
        "(e.g. Shopify, SFCC, Algolia, Amazon, Kibo, Localised), load it "
        "with `load_skill` for proven detection and extraction methods.\n"
    )


def _append_skill_descriptions(system_prompt: str) -> str:
    """Append skill discovery section to the agent system prompt."""
    skill_section = _get_skill_descriptions()
    if not skill_section:
        return system_prompt
    return system_prompt + skill_section


# ── Factory functions ────────────────────────────────────────────────────


def create_site_analyzer(site_slug: str = "") -> object:
    return _build_agent("site_analyzer", site_slug=site_slug)


def create_product_analyzer(site_slug: str = "") -> object:
    return _build_agent("product_analyzer", site_slug=site_slug)


# ═══ ARCHIVED (replaced by browser_traverse) ═══
# def create_navigation_agent(site_slug: str = "") -> object:
#     return _build_agent("navigation_agent", site_slug=site_slug)
# ══════════════════════════════════════════════


def create_nav_skill_review(site_slug: str = "") -> object:
    return _build_agent("nav_skill_review", site_slug=site_slug)


def create_code_writer(site_slug: str = "", template_code: str = "") -> object:
    return _build_agent("code_writer", site_slug=site_slug, template_code=template_code)


def create_code_tester(site_slug: str = "") -> object:
    # First agent migrated to create_agent (v1). Short agent, no truncation needed.
    return _build_agent("code_tester", site_slug=site_slug, use_create_agent=True)


def create_cleanup_agent(site_slug: str = "") -> object:
    return _build_agent("cleanup", site_slug=site_slug)


def create_skill_learner(site_slug: str = "") -> object:
    return _build_agent("skill_learner", site_slug=site_slug)


def create_dagster_converter(site_slug: str = "") -> object:
    return _build_agent("dagster_converter", site_slug=site_slug)


# ── Shared builder ────────────────────────────────────────────────────────


def _trunc_settings():
    """Truncation config (lazy settings read). LLM_TRUNCATION_MODE:
    'deterministic' (default, no LLM call) | 'off' (no-op, emergency rollback)."""
    try:
        from django.conf import settings

        return (
            str(getattr(settings, "LLM_TRUNCATION_MODE", "deterministic")).lower(),
            int(getattr(settings, "LLM_TRUNCATION_MAX_CHARS", 180_000)),
            int(getattr(settings, "LLM_TRUNCATION_PER_MSG_CAP", 8000)),
        )
    except Exception:
        return "deterministic", 180_000, 8000


def _truncate_messages(input_dict: dict) -> dict:
    """Pre-model hook: deterministic context truncation that the LLM ACTUALLY sees.

    Returns ``{"llm_input_messages": kept}`` (NOT ``{"messages": kept}``). Per the
    langgraph ``pre_model_hook`` contract, returning ``messages`` is merged into
    state via ``add_messages`` (append/update-by-ID) and never replaces — so the
    model kept seeing the full ballooned history and this hook was a no-op for
    shrinking. Returning ``llm_input_messages`` instead is read FIRST by
    ``_get_model_input_state`` and used directly as the model input, WITHOUT
    mutating ``state["messages"]`` (so the react loop's accumulation is untouched).

    Behavior:
    1. **Trim oversized NON-seed messages** to a head+tail preview (per-msg cap,
       default 8000). The seed (first HumanMessage: task/strategy/field-map) is
       NEVER trimmed — capping a 25–35k seed to 8k destroys the task spec.
    2. **If still over budget**, keep system + seed + the most recent N messages,
       dropping oldest. ``_clen`` counts ``tool_calls`` too, so write_file/edit_file
       args (the ballooning driver) are measured honestly.
    3. **Pair-safe drop**: a ``ToolMessage`` whose ``AIMessage`` was dropped is
       removed, else the provider returns HTTP 400 ("assistant message with
       tool_calls must be found before a tool message").

    No network, no LLM, no variance. Kill-switch ``LLM_TRUNCATION_MODE='off'`` →
    no-op (returns the input verbatim = exact rollback to pre-fix behavior).
    """
    mode, max_chars, per_msg_cap = _trunc_settings()
    if mode == "off":
        return input_dict

    messages = input_dict.get("messages", [])
    if not messages:
        return input_dict

    _MIN_KEEP_RECENT = 6  # always retain the most recent N non-system messages
    seed = next((m for m in messages if getattr(m, "type", "") == "human"), None)

    def _clen(m) -> int:
        # Count tool_calls too: an AIMessage whose .content is "" but which carries
        # a large write_file/edit_file arg is otherwise invisible to the budget.
        # READ-ONLY — we never mutate tool_calls.
        n = len(str(m.content)) if hasattr(m, "content") else 0
        tc = getattr(m, "tool_calls", None)
        if tc:
            n += len(str(tc))
        return n

    def _trim(m):
        if m is seed:
            return m  # NEVER cap the seed (task/strategy/field-map)
        content = str(m.content) if hasattr(m, "content") else ""
        if len(content) <= per_msg_cap:
            return m
        half = per_msg_cap // 2
        new_content = (
            content[:half]
            + f"\n\n…[deterministic-truncated {len(content)}→{per_msg_cap} chars]"
            + content[-half:]
        )
        try:
            # ToolMessage needs tool_call_id; other message types take content only.
            if hasattr(m, "tool_call_id"):
                return type(m)(content=new_content, tool_call_id=m.tool_call_id)
            return type(m)(content=new_content)
        except Exception:
            return m  # fall back to the original if reconstruction fails

    before_total = sum(_clen(m) for m in messages)

    # Step 1: deterministically trim oversized non-seed messages in place.
    trimmed = [_trim(m) for m in messages]
    total = sum(_clen(m) for m in trimmed)
    if total <= max_chars:
        if total < before_total:
            logger.info(
                "truncate: deterministically trimmed oversized messages (%d → %d chars)",
                before_total, total,
            )
        return {"llm_input_messages": trimmed}

    # Step 2: still over budget — keep system + seed + recent N.
    system_msgs = [m for m in trimmed if hasattr(m, "type") and m.type == "system"]
    other = [m for m in trimmed if not (hasattr(m, "type") and m.type == "system")]
    kept_recent = other[-_MIN_KEEP_RECENT:] if len(other) > _MIN_KEEP_RECENT else list(other)

    budget = max_chars - sum(_clen(m) for m in system_msgs)
    if seed is not None:
        budget -= _clen(seed)  # reserve room for the seed we'll prepend

    selected = []
    acc = 0
    for m in reversed(kept_recent):
        if seed is not None and m is seed:
            continue  # seed is prepended separately; don't double-count
        ml = _clen(m)
        if acc + ml > budget:
            break
        selected.append(m)
        acc += ml
    selected.reverse()  # back to chronological order
    kept = system_msgs + ([seed] if seed is not None else []) + selected

    # Pair-safe: drop any ToolMessage whose tool_call_id isn't backed by a kept
    # AIMessage (otherwise the provider rejects the history with HTTP 400).
    opened = set()
    pair_safe = []
    for m in kept:
        tcid = getattr(m, "tool_call_id", None)
        if tcid:
            if tcid in opened:
                pair_safe.append(m)
            # else: orphaned ToolMessage — drop
        else:
            for tc in (getattr(m, "tool_calls", None) or []):
                if isinstance(tc, dict) and tc.get("id"):
                    opened.add(tc["id"])
            pair_safe.append(m)
    kept = pair_safe

    logger.info(
        "Truncated messages: %d → %d (was %d chars, budget %d, seed_retained=%s)",
        len(messages), len(kept), total, budget, seed is not None,
    )
    return {"llm_input_messages": kept}


def _build_agent(agent_name: str, site_slug: str = "", use_create_agent: bool = False, template_code: str = "") -> object:
    prompt_stem = AGENT_PROMPT_MAP[agent_name]
    temperature = AGENT_TEMPERATURES[prompt_stem]

    try:
        system_prompt = load_agent_prompt(prompt_stem)
    except FileNotFoundError:
        logger.warning("Agent prompt not found for '%s', using fallback", prompt_stem)
        system_prompt = (
            f"You are the {prompt_stem} agent for the Universal Ecommerce Scraper."
        )

    system_prompt = _append_skill_descriptions(system_prompt)

    # Bug 3a fix: actually inject template_code into the system prompt. This
    # parameter was declared but NEVER used — code_writer had to read_file the
    # template (extra round-trip) and could paraphrase/drop pieces. Now the full
    # template is in the system prompt (never summarized), so code_writer SEES
    # the `from src.discovery import ...` line and the `discover_item_urls(...)`
    # call — and copies them instead of reimplementing.
    if template_code:
        system_prompt += (
            "\n\n### Template (use VERBATIM — do not rewrite discovery/pagination)\n"
            "The template below is the scraper skeleton. Fill in the site-specific "
            "parts (EXTRACT_PRODUCT_URLS_JS selectors, field extraction). KEEP the "
            "`from src.discovery import ...` line, the `discover_item_urls(...)` call, "
            "the argparse, and the env-var gate UNCHANGED. Do NOT define "
            "`_click_load_more`, `_get_next_page_url`, or any pagination loop inline.\n\n"
            "```python\n"
            + template_code
            + "\n```\n"
        )

    tools = _get_tools_sync(agent_name, workspace_scope=site_slug)

    if not _has_playwright_tools(tools):
        from .tools import AGENT_TOOL_MAP as _atm

        if "playwright" in _atm.get(agent_name, []):
            logger.warning(
                "Playwright MCP unavailable for '%s'. probe_page will handle page access.",
                agent_name,
            )
            system_prompt += BROWSER_UNAVAILABLE_WARNING

    tools = _strip_v_prefix_from_tools(tools)
    from django.conf import settings as _settings

    _model_setting = AGENT_MODEL_SETTINGS.get(prompt_stem)
    _model_override = (
        getattr(_settings, _model_setting, None) if _model_setting else None
    )
    if _model_override:
        llm = get_llm(
            model=_model_override,
            temperature=temperature,
            timeout=_AGENT_LLM_TIMEOUTS.get(prompt_stem),
        )
    else:
        llm = get_main_llm(temperature)

    logger.info(
        "Creating agent '%s' (model=%s, temp=%.1f, prompt_stem=%s, tools=%d)",
        agent_name,
        _model_override or getattr(_settings, "ZAI_MAIN_MODEL", "glm-5-turbo"),
        temperature,
        prompt_stem,
        len(tools),
    )

    if use_create_agent:
        # v1 path: langchain.agents.create_agent (create_react_agent is deprecated).
        # code_tester is a short agent that doesn't need truncation.
        from langchain.agents import create_agent

        agent = create_agent(
            model=llm, tools=tools, system_prompt=system_prompt,
        )
        logger.info("Created agent '%s' via create_agent (v1 path)", agent_name)
    else:
        agent = create_react_agent(
            llm, tools=tools, prompt=system_prompt, pre_model_hook=_truncate_messages
        )
    return agent


def _strip_v_prefix_from_tools(tools: list) -> list:
    """Monkey-patch ``BaseTool._parse_input`` to strip ``v__`` prefixes.

    The GLM model emits tool-call arguments with a ``v__`` prefix (e.g.
    ``v__command`` instead of ``command``).  LangChain's ``_parse_input``
    validates via Pydantic but then checks the **original** raw input dict
    to decide which fields to pass to the tool function — so a Pydantic
    ``model_validator`` alone is insufficient.  We override
    ``BaseTool._parse_input`` globally to strip prefixes from the raw
    input before any validation occurs.

    Idempotent — the patch is applied once on first call.
    """
    from langchain_core.tools import BaseTool

    if getattr(BaseTool, "_v_prefix_patch_applied", False):
        return tools

    _original_parse_input = BaseTool._parse_input

    def _patched_parse_input(self, tool_input, tool_call_id):
        if isinstance(tool_input, dict):
            tool_input = {
                (k[3:] if k.startswith("v__") else k): v for k, v in tool_input.items()
            }
        return _original_parse_input(self, tool_input, tool_call_id)

    BaseTool._parse_input = _patched_parse_input
    BaseTool._v_prefix_patch_applied = True

    return tools


def _has_playwright_tools(tools: list) -> bool:
    """Check if the tool list contains any Playwright browser tools."""
    return any(t.name.startswith("playwright_") for t in tools)


def _get_tools_sync(agent_name: str, workspace_scope: str = "") -> list:
    from .tools import AGENT_TOOL_MAP, ALLOWED_PLAYWRIGHT_TOOLS
    from .tools.playwright_tools import get_playwright_status

    requested = AGENT_TOOL_MAP.get(agent_name, [])
    tools: list = []

    needs_playwright = "playwright" in requested
    needs_web = "web" in requested
    needs_probe = "probe" in requested
    fs_tool_names = {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "search_content",
    }
    needs_fs = bool(fs_tool_names & set(requested))
    needs_bash = "run_bash" in requested
    needs_scraper = "run_scraper" in requested
    needs_skill = "load_skill" in requested or "list_skills" in requested

    if needs_playwright:
        try:
            from .tools.playwright_tools import create_playwright_tools_sync

            all_pw_tools = create_playwright_tools_sync()
            allowed = set(ALLOWED_PLAYWRIGHT_TOOLS.get(agent_name, []))
            if allowed:
                filtered = [t for t in all_pw_tools if t.name in allowed]
                if not filtered and all_pw_tools:
                    logger.warning(
                        "No allowed Playwright tools matched for '%s' "
                        "(allowed=%s, got=%s). Using all tools.",
                        agent_name,
                        allowed,
                        [t.name for t in all_pw_tools],
                    )
                    tools.extend(all_pw_tools)
                else:
                    tools.extend(filtered)
            else:
                tools.extend(all_pw_tools)

            if not all_pw_tools:
                status = get_playwright_status()
                logger.warning(
                    "Playwright MCP unavailable for '%s' (error=%s). "
                    "probe_page can still access pages.",
                    agent_name,
                    status.get("error", "unknown"),
                )
        except Exception as exc:
            logger.error("Failed to load Playwright tools: %s", exc)

    if needs_probe:
        try:
            from .tools.probe_tools import get_probe_tools as _gpt

            tools.extend(_gpt())
        except Exception as exc:
            logger.error("Failed to load probe tools for '%s': %s", agent_name, exc)

    if needs_web:
        try:
            from .tools.web_tools import get_web_tools as _gwt

            tools.extend(_gwt())
        except Exception as exc:
            logger.error("Failed to load web tools for '%s': %s", agent_name, exc)

    if needs_fs:
        try:
            from .tools.filesystem_tools import get_filesystem_tools as _gft

            tools.extend(_gft(workspace_scope=workspace_scope or None))
        except Exception as exc:
            logger.error(
                "Failed to load filesystem tools for '%s': %s", agent_name, exc
            )

    if needs_bash or needs_scraper:
        try:
            from .tools.shell_tools import get_shell_tools as _gst

            all_shell = _gst()
            if needs_scraper and not needs_bash:
                all_shell = [t for t in all_shell if t.name == "run_scraper"]
            tools.extend(all_shell)
        except Exception as exc:
            logger.error("Failed to load shell tools for '%s': %s", agent_name, exc)

    if needs_skill:
        try:
            from .tools.skill_tools import get_skill_tools as _gsk

            tools.extend(_gsk())
        except Exception as exc:
            logger.error("Failed to load skill tools for '%s': %s", agent_name, exc)

    logger.info(
        "Tools for agent '%s': %s",
        agent_name,
        [t.name for t in tools],
    )

    tools = _apply_guards(tools, agent_name)

    # Generic tool-error handling: v1's ToolNode re-raises tool errors (v0.6
    # swallowed them into retry messages). Wrap every tool to catch exceptions
    # → return an error message, so an agent recovers instead of crashing.
    # Applied AFTER guards so it's the outer wrapper (catches guard errors too).
    from .tools.guards import apply_tool_error_catcher

    tools = [apply_tool_error_catcher(t) for t in tools]

    return tools


def _apply_guards(tools: list, agent_name: str) -> list:
    from .tools.guards import (
        apply_guard,
        require_non_akamai_tool,
        require_non_blocked_domain,
        require_same_domain,
        require_target_url,
    )

    guarded_agents = {
        "site_analyzer",
        "product_analyzer",
        "scraper_analyzer",
        # ═══ ARCHIVED (replaced by browser_traverse) ═══
        # "navigation_agent",
        # ══════════════════════════════════════════════
    }
    url_locked_agents = {
        "site_analyzer",
        "product_analyzer",
        # ═══ ARCHIVED (replaced by browser_traverse) ═══
        # "navigation_agent",
        # ══════════════════════════════════════════════
    }
    domain_locked_agents = {
        "site_analyzer",
        "product_analyzer",
        "scraper_analyzer",
    }

    # code_writer self-test cap (Slice 4): run_scraper lets code_writer close the
    # generate→run→fix loop inside the agent (raising first-attempt quality, cuttin
    # retry churn), but cap it so the agent finalizes instead of run→fix→run→fix
    # looping within its recursion budget. The recursion limit is the hard
    # backstop; this is the soft one that keeps it productive.
    if agent_name == "code_writer":
        from .tools.guards import _make_guard

        # T1.4 (I4): the cap is PER BUCKET, not global — see the constants block.
        _rs_budget: dict[str, int] = {k: 0 for k in RUN_SCRAPER_CAPS}

        def _cap_run_scraper(func, args, kwargs):
            bucket = _run_scraper_bucket(_run_scraper_target(args, kwargs))
            _rs_budget[bucket] += 1
            used = _rs_budget[bucket]
            if used <= RUN_SCRAPER_CAPS[bucket]:
                return None
            remaining = ", ".join(
                f"{_RUN_SCRAPER_BUCKET_LABELS[b]} "
                f"{max(RUN_SCRAPER_CAPS[b] - _rs_budget[b], 0)}/{RUN_SCRAPER_CAPS[b]}"
                for b in RUN_SCRAPER_CAPS
            )
            return (
                f"run_scraper cap reached for the {_RUN_SCRAPER_BUCKET_LABELS[bucket]} "
                f"bucket ({RUN_SCRAPER_CAPS[bucket]} allowed, {used} used). "
                f"Remaining run_scraper budget: {remaining}. You have tested the "
                "scraper enough — finalize it with write_file/edit_file and stop; "
                "code_tester runs the full validation."
            )

        for i, t in enumerate(tools):
            if getattr(t, "name", "") == "run_scraper":
                tools[i] = apply_guard(t, _make_guard(_cap_run_scraper))
        return tools

    if agent_name not in guarded_agents:
        return tools

    for i, t in enumerate(tools):
        name = getattr(t, "name", "")

        if name.startswith("playwright_browser_"):
            if agent_name in guarded_agents:
                t = apply_guard(t, require_non_akamai_tool)
            if agent_name in url_locked_agents and "navigate" in name:
                t = apply_guard(t, require_target_url)
            if agent_name in domain_locked_agents:
                t = apply_guard(t, require_same_domain)
            tools[i] = t

        elif name == "web_fetch":
            if agent_name in guarded_agents:
                t = apply_guard(t, require_non_akamai_tool)
                t = apply_guard(t, require_non_blocked_domain)
                if agent_name in domain_locked_agents:
                    t = apply_guard(t, require_same_domain)
            elif agent_name == "code_tester":
                t = apply_guard(t, require_non_akamai_tool)
            tools[i] = t

        elif name == "probe_page":
            if agent_name in domain_locked_agents:
                t = apply_guard(t, require_same_domain)
            tools[i] = t

    logger.info(
        "Guards applied for '%s': non_akamai=%s, target_url=%s, same_domain=%s",
        agent_name,
        agent_name in guarded_agents,
        agent_name in url_locked_agents,
        agent_name in domain_locked_agents,
    )
    return tools


# ── Message builders ──────────────────────────────────────────────────────


def build_site_analyzer_message(state: dict) -> list:
    """Build the initial HumanMessage for the site-analyzer agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or state.get("sample_url") or "auto-discover"
    currency = state.get("currency") or "auto-detect"

    content_type_context = _build_content_type_context(state)
    probe_result = state.get("probe_result")
    has_verified_probe = False
    cached_probe = ""
    if probe_result and probe_result.get("connectivity"):
        conn = probe_result["connectivity"]
        verified = probe_result.get("captcha_verified", False)
        has_verified_probe = True
        _probed_url_line = (
            f"probed_url: {probe_result.get('probed_url', '')}\n"
            if probe_result.get("probed_url")
            else ""
        )
        # T3.7: state the anchor explicitly when the probe ran somewhere other
        # than the job URL — otherwise the analyzer's evidence is a false anchor.
        _probed_anchor_note = (
            "The evidence above was collected on `probed_url` — if it differs from "
            "the job URL, your analysis anchors to the probed URL, not the job URL. "
            if probe_result.get("probed_url")
            and probe_result.get("probed_url") != state.get("url")
            else ""
        )
        cached_probe = (
            f"\n### Pre-verified Probe Result (from accessibility check)\n"
            f"The page has ALREADY been probed{' and verified as captcha-free' if verified else ''}. "
            f"Do NOT call probe_page again — use this data directly:\n"
            f"```\n"
            f"{_probed_url_line}"
            f"method_that_worked: {conn.get('method_that_worked', 'unknown')}\n"
            f"http_method: {conn.get('http_method', 'none')}\n"
            f"browser_method: {conn.get('browser_method', 'none')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"```\n"
            f"**IMPORTANT**: The connectivity methods above bypass captcha/anti-bot. "
            f"Use `method_that_worked` in your site_analysis.json connectivity section. "
            f"{_probed_anchor_note}"
            f"If `http_method` is available, HTTP requests may also work. "
            f"If `browser_method` is available but different from `method_that_worked`, "
            f"prefer `method_that_worked` for scraping.\n"
        )

    if has_verified_probe:
        access_strategy = (
            "### Page Access Strategy\n\n"
            "The page has already been probed (see Pre-verified Probe Result above). "
            "**Do NOT call probe_page** — it would waste a tool call and return the same cached data.\n\n"
            "Use `playwright_browser_*` tools directly if you need deeper analysis "
            "(network requests, cookies, DOM inspection). Otherwise, proceed directly to "
            "writing site_analysis.json with the connectivity data from the pre-verified probe.\n\n"
        )
        call_allocation = (
            "### Call Allocation (target: 3-5 calls)\n"
            "1. Optional: playwright_browser_* for deeper analysis (0-2 calls)\n"
            "2. write_file to save analysis (1 call)\n\n"
        )
    else:
        access_strategy = (
            f"### Page Access Strategy\n\n"
            f"Use `probe_page` as your FIRST tool call. It automatically tries "
            f"direct HTTP → browser (no proxy) → browser (datacenter proxy) → "
            f"browser (residential proxy) and returns what worked.\n\n"
            f"```\n"
            f'probe_page(url="{product_url}")\n'
            f"```\n\n"
            f"From the probe result, extract:\n"
            f"- Platform clues (from JSON-LD, HTML structure, meta tags)\n"
            f"- Anti-bot status (probe reports if blocked)\n"
            f"- JSON-LD structured data availability\n"
            f"- Which connection method worked (direct HTTP vs browser vs proxy)\n\n"
            f"If probe_page succeeded, you have all the page data you need. "
            f"Optionally use `playwright_browser_*` tools for deeper analysis "
            f"(network requests, cookies) if the probe result is inconclusive.\n\n"
        )
        call_allocation = (
            "### Call Allocation (target: 5-8 calls)\n"
            "1. probe_page on product URL (1 call)\n"
            "2. Optional: playwright_browser_* for deeper analysis (1-3 calls)\n"
            "3. write_file to save analysis (1 call)\n\n"
        )

    url_is_homepage = product_url == url or product_url.rstrip("/") == url.rstrip("/")
    page_label = (
        "No specific product URL was provided — analyze the homepage "
        "using the pre-verified probe data above."
        if url_is_homepage
        else f"Analyze this product page: {product_url}"
    )

    content = (
        f"## OBJECTIVE\n"
        f"Building a scraper for {url}. The scraper reads URLs "
        f"from `input_urls.json` and extracts data from each page.\n\n"
        f"{content_type_context}"
        f"## Your Task: Site Analysis\n\n"
        f"{page_label}\n\n"
        f"**Site URL (for reference):** {url}\n"
        f"**Currency:** {currency}\n"
        f"**Site slug:** {slug}\n"
        f"**Save artifact to:** workspace/{slug}/site_analysis.json\n\n"
        f"{cached_probe}"
        f"{access_strategy}"
        f"{call_allocation}"
        f"### BUDGET: 10 tool calls maximum (target 3-5).\n\n"
        f"### WRITE EARLY — CRITICAL\n"
        f"Write site_analysis.json as soon as you have platform + mechanism + anti-bot.\n"
        f"You can overwrite the file later if you learn more. Do NOT wait until the end.\n"
        f"If you are running low on budget and haven't written the file yet, STOP exploring\n"
        f"and write what you have immediately. A partial analysis is better than none.\n\n"
        f"### Connectivity Info in Output\n"
        f"Include a `connectivity` section in your site_analysis.json:\n"
        f"```json\n"
        f'"connectivity": {{\n'
        f'  "method_that_worked": "direct_http|browser_none|uc_chrome_none|...",\n'
        f'  "proxy_tier": "none",\n'
        f'  "js_rendering_needed": true,\n'
        f'  "anti_bot_detected": false\n'
        f"}}\n"
        f"```\n"
        f"Downstream agents (product_analyzer, scraper_analyzer) read this.\n\n"
        f"### CRITICAL — URL Prohibition\n"
        f"- **Do NOT probe any URL other than the one provided above.**\n"
        f"- Do NOT guess product URLs, probe sitemap.xml, category pages, or variant URLs.\n"
        f"- Do NOT use Wayback Machine, archive.org, cached snapshots, or any archived version\n"
        f"- Do NOT enumerate all Algolia indices or test facet partitioning\n"
        f"- Do NOT read `input_urls.json` — that file is for the code-writer\n"
        f"- Do NOT load skill files — detect from page content only\n"
        f"- Do NOT spend more than 2-3 calls on any single sub-task\n\n"
        f"**CRITICAL: You MUST call write_file to save the analysis as JSON to "
        f"workspace/{slug}/site_analysis.json. Do NOT just print the analysis as text. "
        f"Write the file as soon as you have the core findings.**"
    )
    return [HumanMessage(content=content)]


def _fetch_api_sample(api_url: str, limit: int = 3) -> str:
    """Fetch a compact sample of ONE record from a backend data API.

    Used by build_product_analyzer_message when data_source == "api" so the agent
    maps fields from real response data WITHOUT browsing the page — the page is a
    CSR SPA whose accessibility snapshot stalls the @playwright/mcp browser. Returns
    a pretty JSON string of the first record (values truncated) or "" on failure.
    """
    import json

    import httpx

    try:
        r = httpx.get(
            api_url,
            params={"limit": limit, "offset": 0},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=30,
            follow_redirects=True,
        )
        if r.status_code >= 400:
            return ""
        data = r.json()
    except Exception:
        return ""
    rec = None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        rec = data[0]
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                rec = v[0]
                break
    if not isinstance(rec, dict):
        return ""
    compact = {}
    for k, v in rec.items():
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        compact[k] = s[:120]
    return json.dumps(compact, indent=2, ensure_ascii=False)[:4000]


_JSONLD_MAX_KEYS = 40
_JSONLD_SAMPLE = 120


def _project_jsonld(node, _depth: int = 0) -> str:
    """[T1.9/wave-13] Structure-preserving projection of a JSON-LD node.

    Blind character-slicing cuts mid-object and drops exactly the deep keys
    field maps are built from (offers.price, aggregateRating.ratingValue).
    This renders @type plus every scalar key with a SHORT value sample, and
    recurses ONE level into dict/list values (offers, brand) so the nested
    keys survive. Bounded: 40 keys per node, 120-char samples, 2 levels.
    """
    import json

    if isinstance(node, list):
        return "\n".join(_project_jsonld(n, _depth) for n in node[:6])
    if not isinstance(node, dict):
        return str(node)[:_JSONLD_SAMPLE]
    lines = []
    t = node.get("@type")
    if t:
        lines.append(f"@type: {t}")
    for k, v in node.items():
        if k == "@type" or len(lines) >= _JSONLD_MAX_KEYS:
            break
        if isinstance(v, dict) and _depth < 2:
            lines.append(f"{k}: {{{_project_jsonld(v, _depth + 1)}}}")
        elif isinstance(v, list) and v and isinstance(v[0], dict) and _depth < 2:
            lines.append(
                f"{k}: [{_project_jsonld(v[0], _depth + 1)}] (len={len(v)})"
            )
        else:
            s = v if isinstance(v, str) else json.dumps(v, default=str)
            lines.append(f"{k}: {str(s)[:_JSONLD_SAMPLE]}")
    return "\n  ".join(lines)


def _fetch_rendered_jsonld(url: str) -> str:
    """Fetch the JS-rendered HTML of a sample page (via browser_service ``/render``,
    which applies the anti-bot cloak) and extract its JSON-LD blocks.

    Lets ``product_analyzer`` map fields from rendered structured data WITHOUT
    repeatedly browsing heavy SPA pages — the react agent's snapshot iteration
    blows the 15-min budget on myntra/calvklein. One /render here replaces many
    browser calls. Returns the JSON-LD blocks as text (truncated) or ``''``.

    [T1.9] Parseable blocks are rendered through ``_project_jsonld`` so nested
    pricing/offer structure survives; unparseable blocks keep the legacy raw
    (truncated) text.
    """
    import json
    import re

    import httpx

    try:
        from django.conf import settings

        bs = getattr(settings, "BROWSER_SERVICE_URL", "http://browser_service:8001")
    except Exception:
        bs = "http://browser_service:8001"
    try:
        r = httpx.post(f"{bs}/render", json={"url": url, "timeout": 60}, timeout=75)
        data = r.json()
    except Exception:
        return ""
    if not data.get("success"):
        return ""
    html = data.get("html", "") or ""
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    out = []
    for b in blocks[:6]:
        b = b.strip()
        if not b:
            continue
        try:
            parsed = json.loads(b)
        except Exception:
            parsed = None
        if parsed is not None:
            out.append(_project_jsonld(parsed)[:1500])
        else:
            out.append(b[:1500])
    jsonld = ("\n---\n".join(out))[:4000] if out else ""

    # Compact DOM summary for sites with sparse/missing JSON-LD (adameve): title,
    # h1, meta/og, and a visible-text snippet. Combined with JSON-LD this gives the
    # agent enough to map fields WITHOUT browsing (which blows the 15-min budget).
    def _first(pat):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        return (m.group(1).strip()[:200] if m else "")

    title = _first(r"<title[^>]*>(.*?)</title>")
    h1 = _first(r"<h1[^>]*>(.*?)</h1>")
    desc = _first(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']') or _first(
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']'
    )
    og_title = _first(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']')
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    dom = (
        f"TITLE: {title}\nH1: {h1}\nOG_TITLE: {og_title}\nDESC: {desc}\n"
        f"VISIBLE_TEXT: {text[:1500]}"
    )
    parts = []
    if jsonld:
        parts.append(f"[JSON-LD]\n{jsonld}")
    parts.append(f"[DOM SUMMARY]\n{dom}")
    return ("\n\n".join(parts))[:6000]


def _user_requirements_section(state: dict) -> str:
    """Surface intake-UI field chips + notes to the agent.

    When the user specified fields (target_fields), the schema is ENFORCED:
    extract ONLY those (+ standard bookkeeping); extra fields are pruned from
    the output. With only notes (no fields) it stays advisory. Returns "" for
    jobs with neither (legacy home view).
    """
    target_fields = state.get("target_fields") or []
    notes = (state.get("user_notes") or "").strip()
    parts: list[str] = []
    if target_fields:
        parts.append("Fields requested by the user: " + ", ".join(map(str, target_fields)))
    if notes:
        parts.append("User notes: " + notes)
    if not parts:
        return ""
    if target_fields:
        return (
            "### User Requirements (schema — ENFORCE)\n"
            + "\n".join(parts)
            + "\n\n**The user requires ONLY these fields.** The standard field "
            "table in your system prompt (title, price, availability, currency, "
            "brand, category, images, sku, rating, etc.) **DOES NOT APPLY** to "
            "this job — ignore it entirely. Map and extract ONLY the fields "
            "listed above. The standard bookkeeping fields (url, src_url, "
            "scraped_at, status_code) are always kept automatically. If a "
            "requested field is absent on the page, omit it — do NOT substitute "
            "a field from the system prompt's table.\n\n"
        )
    return (
        "### User Requirements (advisory)\n"
        + "\n".join(parts)
        + "\n\nThese are hints — prioritize them, but still map every field the "
        "page actually exposes.\n\n"
    )


def _render_nested_node(name: str, node: dict, indent: str) -> list[str]:
    """Render one nested-schema node as a shape line (+ recursive children)."""
    t = node.get("type", "text")
    children = node.get("children") or {}
    if not children:
        return [f"{indent}- {name} ({t})"]
    kind = "array of objects" if t == "list" else "object"
    lines = [f"{indent}- {name} ({kind}):"]
    for ck, cn in children.items():
        lines.extend(_render_nested_node(ck, cn, indent + "  "))
    return lines


def _nested_schema_section(state: dict) -> str:
    """Surface the nested JSON-Schema SHAPE to code_writer so it emits nested
    objects/arrays (variants:[{size,color}], address:{city,zip}) instead of
    flattening. Only for nested-schema jobs; "" otherwise. Types are shape hints
    only — NOT enforced."""
    tree = state.get("nested_schema")
    if not tree:
        return ""
    lines = ["### Nested Schema — PRESERVE STRUCTURE"]
    lines.append("The user's schema contains nested fields. Emit each in the EXACT shape below:")
    for name, node in tree.items():
        lines.extend(_render_nested_node(name, node, ""))
    lines.append(
        "\n**Do NOT flatten nested children into top-level keys.** For an object field, "
        "emit a dict with its sub-fields; for an array-of-objects field, emit a list of "
        "dicts each with its sub-fields. If the page exposes the data nested (JSON-LD, "
        "embedded JSON), preserve that structure. The output is pruned to this shape, so "
        "flattened fields (e.g. `variants_size` instead of `variants:[{size}]`) WILL BE "
        "DROPPED. Types are shape hints only — not validated.\n"
    )
    return "\n".join(lines) + "\n"


def _remap_sample_urls(state: dict, slug: str, limit: int = 5) -> list[str]:
    """[T3.13d] Real item URLs from the failed run, for the remap message.

    Sources (first hit wins): ``test_report.discovery_coverage.discovered_urls``
    when the template emitted the list form; else the newest workspace output
    files' item ``url``/``src_url`` — the pages the failed extraction actually
    touched. Best-effort throughout: [] on any absence, and the remap message
    then falls back to its original single-sample behavior.
    """
    urls: list[str] = []
    report = state.get("test_report") or {}
    cov = report.get("discovery_coverage") if isinstance(report, dict) else None
    if isinstance(cov, dict):
        cand = cov.get("discovered_urls")
        if isinstance(cand, list):
            urls = [str(u) for u in cand if isinstance(u, str) and u.startswith("http")]
    if not urls:
        try:
            import json as _json

            from django.conf import settings as _settings

            root = str(
                getattr(_settings, "PROJECT_ROOT", os.environ.get("PROJECT_ROOT", "."))
            )
            ws = os.path.join(root, "workspace", slug)
            outs = sorted(
                [
                    f for f in (os.listdir(ws) if os.path.isdir(ws) else [])
                    if f.startswith("output_") and f.endswith(".json")
                ],
                key=lambda f: os.path.getmtime(os.path.join(ws, f)),
                reverse=True,
            )[:3]
            for name in outs:
                try:
                    with open(os.path.join(ws, name), errors="ignore") as fh:
                        data = _json.load(fh)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        urls = [
                            str(r.get("url") or r.get("src_url") or "")
                            for r in v
                            if isinstance(r, dict)
                        ]
                        urls = [u for u in urls if u.startswith("http")]
                        break
                if urls:
                    break
        except Exception:
            return []
    return list(dict.fromkeys(urls))[:limit]


def build_product_analyzer_message(state: dict) -> list:
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or state.get("sample_url") or ""

    # When no specific item URL was provided, gather the URLs navigation discovered
    # and let the product_analyzer LLM pick a real item/detail page from them
    # (content-type-generic — works for products / jobs / articles / …). We do NOT
    # deterministically pick one here: the LLM is better at recognizing a real item
    # page vs an about-us / category / listing page across arbitrary sites, and a
    # Python heuristic (the old ">=2 path segments" rule) let content pages through.
    pick_candidates: list[str] = []
    if not product_url:
        nav_findings = state.get("navigation_findings") or {}
        nav_analysis = state.get("navigation_analysis") or {}
        candidates: list[str] = []
        # Prefer the CURATED item URLs from navigation_synthesize (deduped/filtered
        # real item pages). listing_page.product_links is the RAW extract and can
        # include content pages (about-us/resources) that navigate_explore's JS
        # looksLikeProduct filter leaks through — using it first shadowed the real
        # item URLs (locumtenens job 219 got about-us candidates, not job URLs).
        item_links = nav_analysis.get("item_links", {})
        for u in item_links.get("url_examples") or []:
            if u and u not in candidates:
                candidates.append(u)
        # Supplement with raw listing_page links only if synthesize produced none.
        if not candidates:
            listing = nav_findings.get("listing_page", {})
            for pl in listing.get("product_links") or []:
                href = pl.get("href", "") if isinstance(pl, dict) else str(pl)
                if href and href not in candidates:
                    candidates.append(href)
        pick_candidates = [c for c in candidates if c]
    if not product_url and not pick_candidates:
        product_url = "auto-discover"

    content_type_context = _build_content_type_context(state)

    cached_probe = ""
    probe_result = state.get("probe_result")
    has_verified_probe = False
    if probe_result and probe_result.get("connectivity"):
        conn = probe_result["connectivity"]
        verified = " (captcha-verified)" if probe_result.get("captcha_verified") else ""
        has_verified_probe = True
        cached_probe = (
            f"\n### Cached Probe Result (from site_analyzer){verified}\n"
            f"The site_analyzer already probed this page{verified}. Use this data instead of calling probe_page again:\n"
            f"```\n"
            f"method_that_worked: {conn.get('method_that_worked', 'unknown')}\n"
            f"http_method: {conn.get('http_method', 'none')}\n"
            f"browser_method: {conn.get('browser_method', 'none')}\n"
            f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
            f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
            f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
            f"platform: {probe_result.get('platform', 'unknown')}\n"
            f"```\n\n"
        )

    if has_verified_probe:
        # Anti-bot sites: cloak re-renders are very slow (each playwright_browser_*
        # call clears an Akamai/CF challenge), and the react agent's iterations
        # pushed product_analysis past the 15-min cap (calvklein). The cached probe
        # already rendered the page — forbid further browser calls and map fields
        # from the cached JSON-LD/selectors only.
        try:
            from agents.tools.context import is_anti_bot_detected

            _pa_anti_bot = is_anti_bot_detected()
        except Exception:
            _pa_anti_bot = False
        if _pa_anti_bot:
            access_strategy = (
                "### Page Access Strategy (ANTI-BOT SITE)\n\n"
                "The page has already been probed (see Cached Probe Result above) and the "
                "site uses anti-bot protection. **Do NOT call probe_page OR playwright_browser_*** "
                "— each browser re-render clears a slow bot challenge and will blow the time "
                "budget. Map every field from the cached probe result (JSON-LD, meta tags, "
                "selectors) and site_analysis.json ALONE.\n\n"
            )
            workflow = (
                "### Workflow\n"
                "1. Read site_analysis.json (1 call)\n"
                "2. Map all fields from the cached probe result — JSON-LD, selectors, meta tags\n"
                "3. write_file to save field mapping (1 call)\n\n"
            )
        else:
            access_strategy = (
                "### Page Access Strategy\n\n"
                "The page has already been probed (see Cached Probe Result above). "
                "**Do NOT call probe_page** — it would waste a tool call and return the same data.\n\n"
                "Use `playwright_browser_*` tools directly for deeper analysis (DOM inspection, "
                "additional selectors, network requests) if needed.\n\n"
            )
            workflow = (
                "### Workflow\n"
                "1. Read site_analysis.json (1 call)\n"
                "2. Map all fields from cached probe result — JSON-LD, selectors, meta tags\n"
                "3. Optionally use playwright_browser_* for additional selector testing (2-5 calls)\n"
                "4. write_file to save field mapping (1 call)\n\n"
            )
    else:
        access_strategy = (
            f"### Page Access Strategy\n\n"
            f"Use `probe_page` as your FIRST tool call after reading site_analysis.json. "
            f"It automatically tries direct HTTP → browser (no proxy) → browser "
            f"(datacenter proxy) → browser (residential proxy) and returns what worked.\n\n"
            f"```\n"
            f'probe_page(url="{product_url}", render_js=True)\n'
            f"```\n\n"
            f"The probe result includes:\n"
            f"- JSON-LD blocks (with field-level detail)\n"
            f"- Open Graph meta tags\n"
            f"- Common selector test results (h1, price, availability, etc.)\n"
            f"- Which connection method and proxy tier worked\n\n"
        )
        workflow = (
            "### Workflow\n"
            "1. Read site_analysis.json (1 call)\n"
            "2. Call probe_page on the product URL (1 call)\n"
            "3. Map all fields from probe result — JSON-LD, selectors, meta tags\n"
            "4. Optionally use playwright_browser_evaluate for additional selector testing (2-5 calls)\n"
            "5. write_file to save field mapping (1 call)\n\n"
        )

    # Re-map mode: code_tester flagged a MAPPING failure → focus on the failed
    # fields instead of redoing the whole analysis. The pipeline routes here from
    # route_after_testing when test_report.remediiation.target == "mapping".
    remap_context = ""
    test_report = state.get("test_report") or {}
    remediation = test_report.get("remediation") if isinstance(test_report, dict) else None
    failed_fields: list[str] = []
    if isinstance(remediation, dict) and remediation.get("target") == "mapping":
        failed_fields = [f for f in (remediation.get("fields") or []) if isinstance(f, str)]
        fields_str = ", ".join(failed_fields) or "(unspecified)"
        # [T3.13d] Hand the remap the URLs the failed run actually touched —
        # ``results.sample_products`` is never populated (not in code_tester's
        # report schema), so every remap cycle re-verified against the SAME
        # single sample page and could pass there while failing on real pages.
        _remap_urls = _remap_sample_urls(state, slug)
        _remap_urls_block = ""
        if _remap_urls:
            _remap_urls_block = (
                "URLs from the failed run — verify each re-mapped field against 2-3 of "
                "these REAL URLs (probe or ONE httpx fetch), not just the original "
                "sample page:\n"
                + "\n".join(f"  - {u}" for u in _remap_urls)
                + "\n"
            )
        remap_context = (
            f"\n### CRITICAL — RE-MAP FAILED FIELDS (mapping-failure recovery)\n"
            f"code_tester ran the generated scraper and these required fields FAILED because their "
            f"MAPPING in product_analysis.json is missing/wrong/unverified:\n"
            f"  **{fields_str}**\n"
            f"{_remap_urls_block}"
            f"Steps:\n"
            f"1. Read `workspace/{slug}/test_report.json` (why each field failed).\n"
            f"2. Read `workspace/{slug}/product_analysis.json` (the current mapping).\n"
            f"3. Re-probe the product page and re-map ONLY the failed fields with corrected "
            f"selectors/methods — try JSON-LD, the backend API (see hint below), or the rendered "
            f"DOM. Keep the `expectations` blocks; set `tested: true` with a real example.\n"
            f"4. Write product_analysis.json back (full file).\n"
            f"Do NOT redo fields that already work. Spend your budget on the failed fields.\n\n"
        )

    # Backend API. When data_source == "api" the API is the PRIMARY data source —
    # fetch a sample record and instruct the agent to map from it WITHOUT browsing
    # (the page is a heavy CSR SPA whose accessibility snapshot stalls the MCP
    # browser). Otherwise it's just a hint.
    api_hint = ""
    nav_analysis = state.get("navigation_analysis") or {}
    api_endpoint = nav_analysis.get("api_endpoint") if isinstance(nav_analysis, dict) else None
    api_endpoint = api_endpoint if isinstance(api_endpoint, dict) else {}
    api_url = api_endpoint.get("url")
    if api_url and nav_analysis.get("data_source") == "api":
        sample = _fetch_api_sample(api_url)
        sample_block = (
            f"\nSample record from the API (map fields from THIS):\n```json\n{sample}\n```\n"
            if sample else ""
        )
        api_hint = (
            f"\n### ★ DATA SOURCE = BACKEND JSON API (primary) ★\n"
            f"browser_traverse determined this site's data comes from a backend JSON API. "
            f"Map EVERY field from the API response — **do NOT browse the page** with "
            f"playwright_browser_* or probe_page. The page is a heavy client-rendered SPA; "
            f"browsing it stalls the browser and is unnecessary — the structured data is in the API.\n"
            f"- API URL: `{api_url}`\n"
            f"- method: GET; paginate with `limit` + `offset` query params.\n"
            f"{sample_block}"
            f"Workflow: read the sample above (already fetched — no tool call needed), map each "
            f"core field to an API response key, optionally make ONE httpx GET to confirm "
            f"pagination/total count, then write_file product_analysis.json.\n\n"
        )
    elif api_url:
        api_hint = (
            f"\n### Backend API Hint (may contain the fields you need)\n"
            f"navigate_explore captured this backend API on the site:\n"
            f"- URL: `{api_url}`\n"
            f"- method: {api_endpoint.get('method', 'GET')}, pagination: "
            f"{api_endpoint.get('pagination_param', '?')}\n"
            f"Test whether this API returns the data for the fields you're mapping. If it does, "
            f"prefer mapping from the API (structured JSON) over DOM selectors.\n\n"
        )

    # Content-type-generic page instruction. If a specific URL was provided, analyze
    # it directly; otherwise hand the discovered candidates to the LLM and let it pick
    # a real item/detail page — no deterministic heuristic (the old ">=2 path segments"
    # rule let about-us pages through). Works across content types (products/jobs/…).
    content_type_name = (
        (state.get("content_type_config") or {}).get("content_type")
        or state.get("page_type")
        or "item"
    )
    if pick_candidates:
        cand_list = "\n".join(f"- {c}" for c in pick_candidates[:12])
        page_instruction = (
            f"**No specific {content_type_name} URL was provided.** The navigation step "
            f"discovered these candidate URLs:\n{cand_list}\n\n"
            f"Pick the one that is a **real {content_type_name} detail/item page** — a "
            f"*single* {content_type_name} (one job posting / one product / one article — "
            f"match this site's content type), **not** a listing, category, or about-us / "
            f"contact / blog / search page. `probe_page` it, then map every extractable "
            f"field from it. If none look like item pages, probe 2-3 of them to find a "
            f"real one.\n"
        )
    else:
        page_instruction = f"**Page URL (analyze this page):** {product_url}\n"

    # Heavy-SPA speedup: pre-render the sample page's JSON-LD ONCE (cloak via
    # browser_service /render) so product_analyzer maps fields without repeated
    # slow browsing — the react agent's snapshot iteration blows the 15-min budget
    # on myntra/calvklein. Skipped for API-source sites (they use _fetch_api_sample).
    rendered_jsonld_section = ""
    _rj_url = ""
    if product_url and str(product_url).startswith("http"):
        _rj_url = product_url
    elif pick_candidates:
        _rj_url = pick_candidates[0]
    if _rj_url and not (api_url and nav_analysis.get("data_source") == "api"):
        _jl = _fetch_rendered_jsonld(_rj_url)
        if _jl:
            rendered_jsonld_section = (
                f"\n### ★ Rendered page content (JSON-LD + DOM summary — map from THIS, do NOT browse)\n"
                f"The sample page `{_rj_url}` was already rendered (cloak applied). Its JSON-LD "
                f"and a compact DOM summary (title/h1/meta/visible-text) are below — map every "
                f"field from them. **Do NOT call playwright_browser_* or probe_page** unless a "
                f"CORE field is missing AND absent below; if so, make at most ONE targeted call. "
                f"Repeated browsing blows the time budget.\n"
                f"```\n{_jl}\n```\n"
            )

    content = (
        f"## OBJECTIVE\n"
        f"Building a scraper for {url}. The scraper reads URLs "
        f"from `input_urls.json` and extracts data from each page.\n\n"
        f"{content_type_context}"
        f"## Your Task: Content Field Mapping\n\n"
        f"Critically review the site analysis, then analyze the **page** "
        f"below to map every extractable field with exact selectors.\n\n"
        f"{page_instruction}"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Save artifact to:** workspace/{slug}/product_analysis.json\n"
        f"{remap_context}"
        f"{api_hint}"
        f"{rendered_jsonld_section}"
        f"{cached_probe}"
        f"{access_strategy}"
        f"{workflow}"
        f"### BUDGET: 50 tool calls maximum.\n\n"
        f"### WRITE EARLY — CRITICAL\n"
        f"Write product_analysis.json as soon as you have mapped the core fields.\n"
        f"You can overwrite the file later if you discover more selectors. Do NOT wait\n"
        f"until the end. If you are running low on budget and haven't written the file\n"
        f"yet, STOP exploring and write what you have immediately.\n"
        f"A partial field mapping is better than none.\n\n"
        f"### Connectivity Info in Output\n"
        f"Include a `connectivity` section in your product_analysis.json:\n"
        f"```json\n"
        f'"connectivity": {{\n'
        f'  "method_that_worked": "direct_http|browser_none|uc_chrome_none|...",\n'
        f'  "proxy_tier": "none",\n'
        f'  "js_rendering_needed": true\n'
        f"}}\n"
        f"```\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT use Wayback Machine, archive.org, cached snapshots, or any archived version\n"
        f"- Do NOT explore related products, similar items, or recommendations\n"
        f"- Do NOT click size/color selectors beyond initial verification\n"
        f"- Do NOT test Algolia API or any structured API (site-analyzer did that)\n"
        f"- Do NOT check dataLayer, anti-bot, or load platform skills\n"
        f"- Do NOT examine newsletters, store locators, or site navigation\n"
        f"- Do NOT read `input_urls.json` — that file is for the code-writer\n"
        f"- Do NOT revisit sections you've already analyzed\n"
        f"- Do NOT guess or probe random URLs — only analyze the product URL provided above\n"
        f"- Do NOT probe category pages or site sections unrelated to the product URL\n\n"
        f"**CRITICAL: You MUST call write_file to save the field mapping as JSON to "
        f"workspace/{slug}/product_analysis.json as your LAST action. "
        f"Do NOT just print the analysis as text.**"
    )
    _user_req = _user_requirements_section(state)
    if _user_req:
        content = _user_req + content
    return [HumanMessage(content=content)]


# ═══ ARCHIVED (replaced by browser_traverse) ═══
# def build_navigation_agent_message(state: dict) -> list:
#     slug = state.get("site_slug", "unknown")
#     url = state.get("url", "")
#     input_mode = state.get("input_mode", "navigation")
#     search_criteria = state.get("search_criteria", "")
#
#     content_type_context = _build_content_type_context(state)
#
#     probe_result = state.get("probe_result")
#     connectivity_section = ""
#     if probe_result and probe_result.get("connectivity"):
#         conn = probe_result["connectivity"]
#         connectivity_section = (
#             f"\n### Pre-verified Connectivity (from site_analyzer)\n"
#             f"```\n"
#             f"method_that_worked: {conn.get('method_that_worked', 'unknown')}\n"
#             f"http_method: {conn.get('http_method', 'none')}\n"
#             f"browser_method: {conn.get('browser_method', 'none')}\n"
#             f"proxy_tier: {conn.get('proxy_tier', 'none')}\n"
#             f"js_rendering_needed: {conn.get('js_rendering_needed', True)}\n"
#             f"anti_bot_detected: {conn.get('anti_bot_detected', False)}\n"
#             f"```\n"
#             f"Use this connectivity method for all page access. Do NOT call probe_page.\n\n"
#         )
#
#     mode_section = ""
#     if input_mode == "list_page":
#         sample_url = state.get("sample_url") or state.get("product_url") or ""
#         mode_section = (
#             f"\n### Input Mode: List Page Analysis\n"
#             f"The user has provided a listing page URL. Analyze THIS page:\n"
#             f"**Listing page URL:** {sample_url}\n\n"
#             f"Focus on:\n"
#             f"- Item link pattern (how to extract content page URLs from this listing)\n"
#             f"- Pagination (how to get more pages)\n"
#             f"- Skip search and category analysis — the user already has the listing page\n\n"
#         )
#     else:
#         mode_section = (
#             f"\n### Input Mode: Navigation Analysis\n"
#             f'**Search criteria:** "{search_criteria}"\n\n'
#             f"Analyze:\n"
#             f"- Search functionality (search box, URL-based search, or both)\n"
#             f"- Category navigation (menus, dropdowns, category links)\n"
#             f"- Pagination (next button, page params, infinite scroll)\n"
#             f"- Item link patterns from search/category results\n\n"
#         )
#
#     # Skills reuse: always tell the agent to load navigation-patterns first — it
#     # carries reusable patterns + `## Learned:` sections captured from prior sites
#     # by nav_skill_review (e.g. ASP.NET POST-to-session-id job boards). This closes
#     # the learn→reuse loop.
#     skills_section = (
#         "\n### Reuse captured patterns (Skills)\n"
#         "BEFORE exploring from scratch, call `load_skill(\"navigation-patterns\")`. "
#         "Its `## Learned:` sections record reusable patterns from prior sites "
#         "(e.g. ASP.NET MVC job boards that POST to a `/SearchResults?sId=...` page). "
#         "If a Learned pattern matches this site, apply it directly. Also `list_skills` "
#         "and load any platform-specific skill that fits.\n\n"
#     )
#
#     # Handoff context: when the deterministic navigate_explore couldn't drive a
#     # form, it stashes its partial findings + a handoff_reason. Tell the agent what
#     # was already tried so it doesn't repeat the dead-end.
#     # LLM URL-selector prior: navigate_explore pre-judged the candidate nav links
#     # (correct listing pages vs wrong marketing/info pages) given content type + query.
#     # Surface it so the heavy navigation_agent starts discovery from the right URLs.
#     url_selector_section = ""
#     _nf = state.get("navigation_findings") or {}
#     _sel = (
#         (_nf.get("homepage_nav") or {}).get("llm_url_selection")
#         if isinstance(_nf, dict) else None
#     )
#     if isinstance(_sel, dict) and _sel.get("ranking"):
#         _correct = [r.get("url") for r in _sel["ranking"] if r.get("verdict") == "correct"][:6]
#         _wrong = [r.get("url") for r in _sel["ranking"] if r.get("verdict") == "wrong"][:6]
#         _correct = [u for u in _correct if u]
#         _wrong = [u for u in _wrong if u]
#         if _correct or _wrong:
#             url_selector_section = (
#                 "\n### URL selector prior (LLM pre-judged the candidate nav links)\n"
#                 "An LLM already classified the site's nav/category links as listing pages vs "
#                 "non-listing pages, given the content type + query. Treat this as a strong prior:\n"
#                 f"- **Likely listing/data pages — start discovery here:** {_correct or 'none'}\n"
#                 f"- **Likely NON-listing pages (marketing/info — do NOT treat as a listing even "
#                 f"if they contain links):** {_wrong or 'none'}\n"
#                 "Verify by visiting, but prefer the 'likely listing' URLs and skip the 'wrong' ones.\n\n"
#             )
#
#     handoff_section = ""
#     if state.get("handoff_reason") or state.get("navigation_findings"):
#         _nf_h = state.get("navigation_findings") or {}
#         _lp_h = (_nf_h.get("listing_page") or {}) if isinstance(_nf_h, dict) else {}
#         _ex_url = (_lp_h.get("url") or "").strip()
#         _ex_ds = _lp_h.get("data_source")
#         _ex_links = len(_lp_h.get("product_links") or [])
#         _ex_bits = []
#         if _ex_url:
#             _ex_bits.append(f"working listing URL=`{_ex_url}`")
#         if _ex_ds:
#             _ex_bits.append(f"data_source=`{_ex_ds}`")
#         if _ex_links:
#             _ex_bits.append(f"{_ex_links} extracted links")
#         _ex_summary = ", ".join(_ex_bits) if _ex_bits else "no working listing URL / data"
#         handoff_section = (
#             "\n### Handed off from the deterministic explorer — BUILD ON IT, don't redo it\n"
#             f"The fast deterministic explorer ran first and reported: {_ex_summary}. READ "
#             f"`workspace/{slug}/navigation_findings.json`.\n"
#             "**Start from the explorer's findings; do not redo its homepage/category crawl.** "
#             "The explorer is now reliable for finding the listing page (LLM URL selector + "
#             "embedded-JSON detector). Visit its working URL and VERIFY the links/data are real "
#             "item pages. Your job is what the explorer CAN'T do — driving JS/validation-gated "
#             "forms, detecting filters, confirming pagination mechanics. **Only OVERRIDE** the "
#             "explorer's `working_url` / `data_source` / `item_links` with a VERIFIED replacement "
#             "you concretely observed; never discard them by default (a downstream merge fills "
#             "any gaps you leave, but prefer to confirm/extend).\n"
#             f"**Handoff reason:** {state.get('handoff_reason', 'always_rediscover')}\n\n"
#             "### Form-driving mechanics discovery (SCOPE-LIMITED)\n"
#             "You are discovering HOW the form works so code_writer can iterate it at scale "
#             "— you are NOT doing bulk extraction.\n"
#             "- The deterministic explorer usually fails because: (a) it clicked a decorative "
#             "submit button OUTSIDE the form instead of the form's real `<input type=submit>` "
#             "INSIDE it, or (b) the form has a required field (often Specialty) enforced by "
#             "client-side validation whose rule lives in a JS bundle, not the HTML.\n"
#             "- Drive ONE successful form submission: fill EVERY `<select>`/input (use "
#             "`playwright_browser_evaluate` to set values + dispatch change events so the "
#             "validation library sees them), click the form's OWN submit input (inside "
#             "`<form>`), and if submit is blocked, read the validation message and satisfy it.\n"
#             "- CAPTURE the result: the results page URL (often a redirect to "
#             "`/SearchResults?sId=...`) AND any AJAX endpoint that carries the items "
#             "(`playwright_browser_network_requests`). The downstream scraper will replay this.\n"
#             "- STOP once you have a working results URL / AJAX endpoint + a sample item link. "
#             "Do NOT iterate all dropdown combinations — that is code_writer's job.\n\n"
#         )
#
#     content = (
#         f"## OBJECTIVE\n"
#         f"Analyze the navigation patterns of {url} to enable a self-navigating scraper.\n\n"
#         f"{content_type_context}"
#         f"## Your Task: Navigation Pattern Analysis\n\n"
#         f"**Site URL:** {url}\n"
#         f"**Site slug:** {slug}\n"
#         f"**Input mode:** {input_mode}\n"
#         f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
#         f"**Save artifact to:** workspace/{slug}/navigation_analysis.json\n"
#         f"{connectivity_section}"
#         f"{mode_section}"
#         f"{skills_section}"
#         f"{handoff_section}"
#         f"{url_selector_section}"
#         f"### Workflow\n"
#         f"1. `load_skill(\"navigation-patterns\")` — apply any matching Learned pattern (1 call)\n"
#         f"2. Read site_analysis.json (1 call); if handed off, also read navigation_findings.json\n"
#         f"3. Navigate to the site homepage or listing page (1 call)\n"
#         f"4. Explore navigation patterns: search, categories, pagination, item links (5-15 calls)\n"
#         f"5. Write navigation_analysis.json (1 call)\n\n"
#         f"### BUDGET: {'40' if input_mode == 'navigation' else '20'} tool calls maximum.\n\n"
#         f"### WRITE EARLY — CRITICAL\n"
#         f"Write navigation_analysis.json as soon as you have the key patterns "
#         f"(search + item_links minimum). Overwrite later if you find more.\n\n"
#         f"### STRICT JSON — CRITICAL\n"
#         f"The file MUST be strict, parseable JSON. No `...`/ellipsis placeholders, "
#         f"no `// comments`, no trailing commas, no unquoted keys. If a list is long, "
#         f"write the first 10 REAL entries and stop — never write `...` as a stand-in. "
#         f"Validate it parses before finishing.\n\n"
#         f"### What NOT to Do\n"
#         f"- Do NOT collect individual content URLs — analyze patterns only\n"
#         f"- Do NOT scrape content from individual pages\n"
#         f"- Do NOT call probe_page — use connectivity info from site_analysis\n"
#         f"- Do NOT crawl more than 2-3 pages\n"
#         f"- Do NOT explore related search terms beyond the given criteria\n"
#         f"- Do NOT write scraper code\n\n"
#         f"**CRITICAL: You MUST call write_file to save the analysis as JSON to "
#         f"workspace/{slug}/navigation_analysis.json. Do NOT just print the analysis as text.**"
#     )
#     return [HumanMessage(content=content)]
# ══════════════════════════════════════════════


# ═══ ARCHIVED (replaced by browser_traverse) ═══
# def build_navigation_synthesize_message(state: dict) -> list:
#     """Build the prompt for the navigation synthesis agent.
#
#     This agent reads raw findings (from navigate_explore) and produces
#     the structured navigation_analysis.json. It has NO browser/web tools —
#     it can only read files and write files.
#     """
#     slug = state.get("site_slug", "unknown")
#     url = state.get("url", "")
#     search_criteria = state.get("search_criteria", "")
#     input_mode = state.get("input_mode", "navigation")
#
#     content = (
#         f"## OBJECTIVE\n"
#         f"Convert raw navigation exploration data into structured navigation_analysis.json.\n\n"
#         f"## Context\n"
#         f"**Site URL:** {url}\n"
#         f"**Site slug:** {slug}\n"
#         f"**Input mode:** {input_mode}\n"
#         f"**Search criteria:** {search_criteria}\n\n"
#         f"## Your Task\n\n"
#         f"You have TWO files to read:\n"
#         f"1. `workspace/{slug}/navigation_findings.json` — raw data extracted by the "
#         f"deterministic explorer (category links, search form, pagination, item links)\n"
#         f"2. `workspace/{slug}/site_analysis.json` — site platform info, connectivity, "
#         f"product URL patterns\n\n"
#         f"Read both files, then **write** `workspace/{slug}/navigation_analysis.json` "
#         f"with this exact structure:\n\n"
#         f"```json\n"
#         f"{{\n"
#         f'  "discovery_method": "search | category | url_pattern",\n'
#         f'  "search": {{\n'
#         f'    "has_search": true/false,\n'
#         f'    "input_selector": "CSS selector for search input",\n'
#         f'    "submit_selector": "CSS selector for submit button",\n'
#         f'    "url_pattern": "URL pattern with {{query}} placeholder",\n'
#         f'    "has_url_search": true/false,\n'
#         f'    "search_url_pattern": "/search?q={{query}}",\n'
#         f'    "working_url": "ACTUAL URL where products were found (from listing_page.url in findings)"\n'
#         f"  }},\n"
#         f'  "categories": {{\n'
#         f'    "menu_selector": "CSS selector for category menu",\n'
#         f'    "category_links": ["url1", "url2"],\n'
#         f'    "url_patterns": ["/category/{{slug}}"]\n'
#         f"  }},\n"
#         f'  "pagination": {{\n'
#         f'    "type": "next_button | page_param | infinite_scroll | load_more",\n'
#         f'    "next_button_selector": "CSS selector",\n'
#         f'    "page_param_name": "page | pnum | p",\n'
#         f'    "max_pages": null,\n'
#         f'    "total_count_selector": "CSS selector for item count text"\n'
#         f"  }},\n"
#         f'  "item_links": {{\n'
#         f'    "container_selector": "CSS selector for item grid container",\n'
#         f'    "link_selector": "CSS selector for item links",\n'
#         f'    "url_pattern": "URL pattern for items",\n'
#         f'    "url_examples": ["url1", "url2"]\n'
#         f"  }}\n"
#         f"}}\n"
#         f"```\n\n"
#         f"## Rules\n\n"
#         f"- READ the findings file FIRST (1 call)\n"
#         f"- READ site_analysis.json if you need platform/URL info (1 call)\n"
#         f"- WRITE navigation_analysis.json (1 call)\n"
#         f"- That's 2-3 calls total. Do NOT do anything else.\n"
#         f"- If the findings have 0 category links AND 0 product links, the "
#         f"exploration FAILED. Write discovery_method: 'failed' and leave all "
#         f"selectors and url_examples as empty strings. Do NOT fabricate URLs, "
#         f"selectors, or platform-specific details. Empty is better than wrong.\n"
#         f"- Only include url_examples that appear verbatim in the findings JSON. "
#         f"Never invent URLs or selectors that are not grounded in the data.\n"
#         f"- Choose `discovery_method` based on what's available: prefer 'search' if "
#         f"the site has a working search and criteria was provided; use 'category' if "
#         f"categories were found; use 'failed' if both are empty.\n"
#         f"- Check the top-level `search_attempted` field in navigation_findings.json. "
#         f"If it is `true`, the explorer tried searching even if `homepage_nav.search_form` "
#         f"is null or absent. In that case, set `search.has_search: true` and "
#         f"`search.has_url_search: true`.\n"
#         f"- **CRITICAL: working_url.** Read `listing_page.url` from navigation_findings.json. "
#         f"If it exists AND `listing_page.product_links` is non-empty, set `search.working_url` "
#         f"to that exact URL. This is the actual browser URL where products were found — it is "
#         f"more reliable than the `url_pattern` or `search_url_pattern` (which are guessed from "
#         f"the homepage form's action attribute). Downstream agents use `working_url` as the "
#         f"authoritative search URL.\n"
#         f"- For selectors, use the most specific CSS selector you can derive from the "
#         f"raw data (parent classes, element types, attributes). If no data, leave empty.\n\n"
#         f"**You MUST call write_file to save the output. Do NOT just print the JSON as text.**\n"
#     )
#     return [HumanMessage(content=content)]
# ══════════════════════════════════════════════


def build_nav_skill_review_message(state: dict) -> list:
    """Build the initial HumanMessage for the nav-skill-review agent.

    This agent reads raw navigation findings, compares them against existing
    skills, and auto-applies reusable learnings by appending ``## Learned:``
    sections to skill files. It runs after navigation_synthesize and is
    non-blocking (failures don't halt the pipeline).
    """
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    platform = state.get("platform", "custom")

    content = (
        f"## OBJECTIVE\n"
        f"Review navigation findings for {url} against existing skills and "
        f"auto-apply any new reusable patterns.\n\n"
        f"## Your Task: Navigation Skill Review\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Detected platform:** {platform}\n\n"
        f"## Files to Read\n"
        f"1. `workspace/{slug}/navigation_findings.json` — raw explorer data "
        f"(category links, search form, pagination, item links, platform signals)\n"
        f"2. `workspace/{slug}/site_analysis.json` — platform info, connectivity\n"
        f"3. `workspace/{slug}/navigation_analysis.json` — structured analysis "
        f"from synthesize (optional, for reference)\n\n"
        f"## Workflow\n"
        f"1. READ navigation_findings.json (1 call)\n"
        f"2. READ site_analysis.json (1 call)\n"
        f"3. LIST existing skills (1 call)\n"
        f"4. LOAD 'navigation-patterns' skill — this is the PRIMARY skill to "
        f"compare against (1 call)\n"
        f"5. Optionally LOAD platform-specific skills if the site matches "
        f"(shopify-detection, sfcc-detection, etc.) — 0-2 calls\n"
        f"6. COMPARE findings against skills — identify 0-3 genuinely new patterns\n"
        f"7. APPLY learnings: for each new pattern, call learn_skill (ONE call "
        f"per pattern) — it appends the '## Learned:' section to the File "
        f"Master for you (format enforced, duplicate-safe). Use "
        f"create_new_skill ONLY for a genuinely new skill (rare). Direct "
        f"write_file/edit_file on skill files are DISABLED.\n"
        f"8. WRITE your report to workspace/{slug}/nav_learning_report.json "
        f"(1 call — your LAST action)\n\n"
        f"## BUDGET: 15 tool calls maximum.\n\n"
        f"## ⚠️ Safe Auto-Apply Rules\n"
        f"- You MAY append '## Learned: {{title}}' sections to existing skills\n"
        f"- You MUST NOT remove or modify existing skill content\n"
        f"- You MUST NOT overwrite entire skill files\n"
        f"- You MUST NOT modify YAML frontmatter (the --- block at top)\n"
        f"- When in doubt, append rather than create a new skill\n"
        f"- Quality over quantity — ZERO learnings is better than wrong ones\n\n"
        f"## When NOT to Apply\n"
        f"- Pattern already documented in any skill (check carefully!)\n"
        f"- Pattern is site-specific (e.g., a unique CSS class for one site)\n"
        f"- Pattern is trivial (e.g., standard <nav> links)\n"
        f"- Findings are incomplete (don't guess patterns from partial data)\n\n"
        f"## Output Format for nav_learning_report.json\n"
        f"```json\n"
        f"{{\n"
        f'  "site_slug": "{slug}",\n'
        f'  "site_url": "{url}",\n'
        f'  "platform": "{platform}",\n'
        f'  "review_timestamp": "ISO-8601",\n'
        f'  "patterns_reviewed": 0,\n'
        f'  "new_patterns_found": 0,\n'
        f'  "skills_updated": [],\n'
        f'  "skills_created": [],\n'
        f'  "patterns_skipped": [],\n'
        f'  "status": "applied|no_new_patterns"\n'
        f"}}\n"
        f"```\n\n"
        f"**CRITICAL: You MUST call write_file to save your report to "
        f"workspace/{slug}/nav_learning_report.json as your LAST action. "
        f"Even if you found no new patterns, write a report saying so.**"
    )
    return [HumanMessage(content=content)]


def _url_discovery_rules(site_input_urls: list[str]) -> str:
    if site_input_urls:
        return (
            f"- Do NOT discover, crawl, or add any product URLs to input_urls.json\n"
            f"- Do NOT overwrite the existing input_urls.json — read it and use it as-is\n"
            f"- input_urls.json MUST contain ONLY the {len(site_input_urls)} URLs "
            f"provided by the user, NOT discovered links\n"
        )
    return (
        "- Do NOT read an existing input_urls.json from a previous run — write a fresh one\n"
        "- Do NOT add discovered/related product URLs to input_urls.json — "
        "only include the URL(s) provided by the user\n"
        "- input_urls.json MUST contain ONLY the Product URL(s) listed above, "
        "NOT discovered links\n"
    )


def _summarize_test_report(state: dict) -> str:
    report = state.get("test_report")
    if not report:
        return ""
    assessment = report.get("overall_assessment", "UNKNOWN")
    try:
        confidence = float(report.get("confidence_score", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0
    issues = report.get("issues", [])
    retry_count = state.get("test_retry_count", 0)
    if retry_count == FINAL_RETRY_SENTINEL:
        retry_label = "FINAL ATTEMPT"
    else:
        retry_label = f"Retry Cycle {retry_count + 1}"

    lines = [
        f"### Previous Test Results ({retry_label})",
        f"- **Assessment:** {assessment}",
        f"- **Confidence:** {confidence:.0%}",
    ]

    def _issue_fix(i: dict) -> str:
        """T1.2 (I8-narrow): relay the tester's mechanical fix instruction.

        ``suggested_fix`` had ZERO readers in .py — the writer only ever saw
        the complaint, so the retry applied a coin-flip rewrite instead of the
        fix the tester already knew. Bounded (the seed is truncation-exempt).
        """
        fix = str(i.get("suggested_fix") or "").strip()
        if not fix:
            return ""
        fix = re.sub(r"\s+", " ", fix)
        if len(fix) > _ISSUE_FIX_CAP:
            fix = fix[: _ISSUE_FIX_CAP - 1] + "…"
        return f"    Fix: {fix}"

    if issues:
        # Issue-shape normalization (P1): three vocabularies are live in the
        # repo — graph.py inserts write `message`, code-tester.md documents
        # `problem`, this summarizer reads `description`/`field` (which made the
        # probe-crash bounce render as "`?`: <empty>" until now). Read all
        # three keys; deterministic inserts carry message+description both.
        def _issue_text(i: dict) -> str:
            return (
                i.get("description")
                or i.get("message")
                or i.get("problem")
                or ""
            )

        high = [i for i in issues if i.get("severity") == "high"]
        medium = [i for i in issues if i.get("severity") == "medium"]
        if high:
            lines.append(f"\n**HIGH severity ({len(high)}):**")
            for i in high[:5]:
                field = i.get("field") or (
                    _issue_text(i)[:60] if _issue_text(i) else "?"
                )
                desc = _issue_text(i)
                expected = i.get("expected", "")
                actual = i.get("actual", "")
                lines.append(f"  - `{field}`: {desc}")
                if expected or actual:
                    lines.append(f"    Expected: {expected!r} | Actual: {actual!r}")
                _fix = _issue_fix(i)
                if _fix:
                    lines.append(_fix)
        if medium:
            lines.append(f"\n**MEDIUM severity ({len(medium)}):**")
            for i in medium[:3]:
                field = i.get("field") or (
                    _issue_text(i)[:60] if _issue_text(i) else "?"
                )
                desc = _issue_text(i)
                lines.append(f"  - `{field}`: {desc}")
                _fix = _issue_fix(i)
                if _fix:
                    lines.append(_fix)
        # Relay CLI-contract issues verbatim (Edit 7): the marker-prefixed
        # bounce must reach the next code_writer message intact so the fix
        # instruction's vocabulary matches the guard's.
        _marked = [
            i
            for i in issues
            if "CLI CONTRACT VIOLATION" in _issue_text(i)
        ]
        if _marked:
            lines.append(
                "\n**⚠️ CLI CONTRACT VIOLATION — targeted argparse fix, do NOT "
                "regenerate the scraper:**\n" + _issue_text(_marked[0])
            )
    # T1.2: report-level remediation instructions (code-tester.md:146 — also
    # written deterministically on discovery-probe crashes and CLI contract
    # violations). Unread until now; the issue relay above carried only the
    # marker text, not the fix instruction. Outside the issues guard so a
    # report with feedback and an empty issue list is not silently dropped.
    _feedback = str(report.get("feedback_for_writer") or "").strip()
    if _feedback:
        if len(_feedback) > _WRITER_FEEDBACK_CAP:
            _feedback = _feedback[: _WRITER_FEEDBACK_CAP - 1] + "…"
        lines.append(
            "\n**⚠️ REMEDIATION INSTRUCTION (apply this fix — do NOT rewrite "
            "from scratch):**\n" + _feedback
        )
    # T2.6: retry writers kept breaking WORKING phases because the summary never
    # said what worked — a "price MISSING" report reads as "rewrite everything",
    # and the rewrite discarded the proven Phase-1 discovery (jobs 71/76/81).
    # Both sources here are attached deterministically (graph.py attaches
    # phases_tested + discovery_coverage), no LLM trust involved.
    _phases = report.get("phases_tested")
    _cov_t26 = report.get("discovery_coverage")
    _preserve: list[str] = []
    if isinstance(_cov_t26, dict):
        try:
            _disc_n = int(_cov_t26.get("discovered_urls") or 0)
        except (TypeError, ValueError):
            _disc_n = 0
        if _disc_n > 0:
            _preserve.append(
                f"Phase-1 discovery WORKED: {_disc_n} item URLs discovered "
                f"(stop_reason={_cov_t26.get('stop_reason') or 'n/a'}). DO NOT "
                "rewrite, 'simplify', or replace the discovery/pagination code — "
                "the rewrite discards a proven Phase 1 and re-discovers from zero."
            )
    if isinstance(_phases, dict):
        _ran = [k for k, v in _phases.items() if v]
        if _ran:
            _preserve.append(
                "Phases tested and completed: " + ", ".join(sorted(_ran)) + "."
            )
    _res_t26 = report.get("results")
    _succ = 0
    if isinstance(_res_t26, dict):
        try:
            _succ = int(_res_t26.get("successful_extractions") or 0)
        except (TypeError, ValueError):
            _succ = 0
    if _succ > 0:
        _preserve.append(
            f"{_succ} sample item(s) extracted successfully — the run mechanics "
            "(fetch → parse → output) work. Fix ONLY the listed issues; keep the "
            "fetching/parsing structure intact."
        )
    if _preserve:
        lines.append("\n**✅ WHAT TO PRESERVE (proven working in the failed run):**")
        lines.extend(f"  - {p}" for p in _preserve)

    # T2.6: the exact invocation the failing run used, so a targeted fix lands
    # in the code path that actually ran (best-effort — the tester logs every
    # run_scraper launch; an unavailable DB must never break the builder).
    try:
        from scraper.models import SessionLog

        _row = (
            SessionLog.objects.filter(
                job_id=state.get("job_id") or 0,
                content__startswith="[RUN_SCRAPER]",
            )
            .order_by("-id")
            .values_list("content", flat=True)
            .first()
        )
        if _row:
            lines.append("\n**EXACT FAILURE CONTEXT** (the run being judged):")
            lines.append(f"  `{str(_row).strip()}`")
    except Exception:
        pass

    # Fix A: surface the RAW error so code_writer makes a targeted fix instead of
    # regenerating from scratch (which reintroduces variance).
    strategy_error = report.get("strategy_error") if isinstance(report, dict) else None
    crash_error = report.get("crash_error") if isinstance(report, dict) else None
    # code_tester nests crash info at script_checks.crash_error (not top-level).
    if not crash_error and isinstance(report, dict):
        _sc = report.get("script_checks")
        if isinstance(_sc, dict):
            crash_error = _sc.get("crash_error") or _sc.get("error_message")
    if strategy_error:
        lines.append(
            f"\n**⚠️ STRATEGY MISMATCH — rewrite using the correct strategy:**\n"
            f"{strategy_error}\n"
            f"Rewrite using the correct strategy; do NOT keep the wrong approach."
        )
    elif crash_error:
        # T2.6: a traceback's verdict lives in its LAST lines — tail 40 lines
        # under the dedicated cap instead of head-slicing 1500 chars (which
        # kept the banner and cut the actual exception).
        _crash_text = "\n".join(str(crash_error).splitlines()[-40:])
        if len(_crash_text) > _EXACT_FAILURE_CAP:
            _crash_text = _crash_text[: _EXACT_FAILURE_CAP - 1] + "…"
        lines.append(
            "\n**⚠️ THE SCRAPER CRASHED — make a MINIMAL, targeted fix for THIS error "
            "(do NOT rewrite from scratch — that reintroduces variance):**\n"
            f"```\n{_crash_text}\n```"
        )
    if retry_count > 0 and retry_count != FINAL_RETRY_SENTINEL:
        lines.append(f"\n*{retry_count} previous attempt(s) failed.*")
    elif retry_count == FINAL_RETRY_SENTINEL:
        lines.append("\n*This is the FINAL retry attempt based on user feedback. "
                      "If this does not pass, the job will end.*")
    return "\n".join(lines)


def _render_verified_selectors(scraper_analysis: dict) -> str:
    """Render `verified_selectors` as a section (reused by code_writer AND
    code_reviewer so both agents see the analyzer-confirmed selectors).

    Returns "" when absent (callers no-op). Handles both string and dict
    field_info shapes (matching the inline block this replaces).
    """
    verified = (scraper_analysis or {}).get("verified_selectors") or {}
    if not isinstance(verified, dict) or not verified:
        return ""
    lines = []
    for field_name, field_info in verified.items():
        if isinstance(field_info, str):
            lines.append(f"  - {field_name}: {field_info}")
            continue
        if not isinstance(field_info, dict):
            lines.append(f"  - {field_name}: {field_info}")
            continue
        method = field_info.get("method", "unknown")
        verified_flag = field_info.get("verified", False)
        note = field_info.get("note", "")
        if method == "jsonld":
            path = field_info.get("path", "")
            lines.append(
                f"  - {field_name}: JSON-LD path `{path}` (verified={verified_flag})"
            )
        elif method == "css":
            selector = field_info.get("selector", "")
            lines.append(
                f"  - {field_name}: CSS `{selector}` (verified={verified_flag})"
            )
        elif method == "static":
            value = field_info.get("value", "")
            lines.append(f"  - {field_name}: static value `{value}`")
        if note:
            lines.append(f"    Note: {note}")
    return (
        "\n### Verified Selectors (from scraper_analyzer)\n"
        + "\n".join(lines)
        + "\n"
    )


def _render_critical_fix(scraper_analysis: dict) -> str:
    """Render `critical_fix` + `retry_adjustments` as a CRITICAL block
    (reused by code_writer AND code_reviewer).

    Returns "" when absent. Matches the inline block this replaces — a
    documented defect + its mandatory fix, so the agent does not regenerate
    the same defect on retry.
    """
    sa = scraper_analysis or {}
    critical_fix = sa.get("critical_fix") or {}
    retry_adj = sa.get("retry_adjustments") or {}
    section = ""
    if isinstance(critical_fix, dict) and critical_fix:
        cf_lines = []
        if critical_fix.get("issue"):
            cf_lines.append(f"- **Issue:** {critical_fix['issue']}")
        if critical_fix.get("root_cause"):
            cf_lines.append(f"- **Root cause:** {critical_fix['root_cause']}")
        if critical_fix.get("fix"):
            cf_lines.append(f"- **FIX (MANDATORY):** {critical_fix['fix']}")
        if critical_fix.get("alternative_discovery"):
            cf_lines.append(
                f"- **Alternative discovery:** {critical_fix['alternative_discovery']}"
            )
        section = (
            "\n### ⚠️ CRITICAL FIX — DO NOT IGNORE (from scraper_analyzer)\n"
            "A previous scraper attempt crashed with a KNOWN, DOCUMENTED defect. "
            "You MUST apply the fix below. Reproducing the same defect will fail. "
            "Pay special attention to any selector marked as non-existent — it "
            "MUST NOT appear in your code.\n"
            + "\n".join(cf_lines)
            + "\n"
        )
    if isinstance(retry_adj, dict) and retry_adj:
        ra_changes = retry_adj.get("changes_made") or []
        ra_lines = []
        if retry_adj.get("failure_reason"):
            ra_lines.append(f"- **Prior failure:** {retry_adj['failure_reason']}")
        if isinstance(ra_changes, list):
            for c in ra_changes:
                if isinstance(c, str):
                    ra_lines.append(f"- {c}")
        if ra_lines:
            section += (
                "\n### Retry Adjustments (prior attempt failed — apply these)\n"
                + "\n".join(ra_lines)
                + "\n"
            )
    return section


def _checkpoint_discovery_section(state: dict) -> str:
    """T2.4: relay the PREVIOUS cycle's discovery checkpoint into the writer seed.

    ``discovered_urls_checkpoint.json`` persists across cycles within a run
    (the workspace wipe is per-JOB, which is correct — cross-run reuse is the
    H3 contamination class). Job-88's shape: cycle-2 discovered 8 URLs and
    extracted 5/5, then cycle-3 regenerated the draft, ran a FRESH Phase 1
    that found 0, and the checkpoint sat unused in the workspace. Surfacing
    those URLs tells the writer (a) discovery mechanics were PROVEN and (b)
    exactly which URLs a rewritten Phase 1 must still find.
    """
    slug = str(state.get("site_slug") or "").strip()
    if not slug:
        return ""
    try:
        from django.conf import settings

        root = str(getattr(settings, "PROJECT_ROOT", "") or "")
    except Exception:
        root = os.environ.get("PROJECT_ROOT", "")
    if not root:
        return ""
    path = os.path.join(root, "workspace", slug, "discovered_urls_checkpoint.json")
    try:
        import json as _json

        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = _json.load(fh)
    except Exception:
        return ""
    urls = data.get("urls") if isinstance(data, dict) else None
    if not isinstance(urls, list) or not urls:
        return ""
    sample = [str(u) for u in urls[:25] if str(u).strip()]
    if not sample:
        return ""
    lines = [
        f"\n### PROVEN DISCOVERY (previous cycle's checkpoint: {len(urls)} URLs)",
        "Phase-1 discovery ALREADY WORKED in a previous attempt on this exact run "
        "(workspace/" + slug + "/discovered_urls_checkpoint.json). These item URLs are "
        "GROUND TRUTH — if you touch Phase 1, your rewritten discovery MUST still "
        "find them. Do NOT regenerate discovery from scratch and hope.",
    ]
    lines.extend(f"  {i + 1}. {u}" for i, u in enumerate(sample))
    if len(urls) > len(sample):
        lines.append(f"  … and {len(urls) - len(sample)} more.")
    return "\n".join(lines) + "\n"


def _platform_distillation(state: dict) -> str:
    """T3.11: ≤2KB deterministic platform distillation for code_writer.

    Digests the site mechanics the writer keeps re-deriving — platform, item
    URL shape, discovery/pagination, field headings — from the analysis
    artifacts already in state. Replaces the disabled full-skill injection
    (``skills_section = ""``): 25KB/skill looped the writer; this is ~1KB and
    never guesses (every line comes from an artifact, "" when absent).
    """
    site_analysis = state.get("site_analysis") if isinstance(
        state.get("site_analysis"), dict
    ) else {}
    nav = state.get("navigation_analysis") if isinstance(
        state.get("navigation_analysis"), dict
    ) else {}
    scraper_analysis = state.get("scraper_analysis") if isinstance(
        state.get("scraper_analysis"), dict
    ) else {}

    lines: list[str] = ["\n### SITE MECHANICS (deterministic digest — from analysis artifacts)"]

    def _add(label: str, value) -> None:
        text = str(value or "").strip()
        if text and len(lines) < 40:
            lines.append(f"- {label}: {text[:300]}")

    _add("Platform", site_analysis.get("platform"))
    _add(
        "Strategy",
        scraper_analysis.get("strategy") or site_analysis.get("scraping_mechanism"),
    )
    search = nav.get("search") if isinstance(nav.get("search"), dict) else {}
    _add("Search URL pattern", search.get("url_pattern"))
    _add("Search input selector", search.get("input_selector"))
    discovery = nav.get("discovery") if isinstance(nav.get("discovery"), dict) else {}
    _add("Promoted listing URL", discovery.get("listing_url"))
    pagination = nav.get("pagination") if isinstance(nav.get("pagination"), dict) else {}
    _add("Pagination type", pagination.get("type"))
    _add("Pagination page param", pagination.get("page_param_name"))
    _add("Next-button selector", pagination.get("next_button_selector"))
    item_links = nav.get("item_links") if isinstance(nav.get("item_links"), dict) else {}
    _add("Item URL pattern", item_links.get("url_pattern"))
    _add("Item container selector", item_links.get("container_selector"))
    _add("Item link selector", item_links.get("link_selector"))
    api = nav.get("api_endpoint") if isinstance(nav.get("api_endpoint"), dict) else {}
    _add("Backend JSON API", api.get("url") or api.get("api_url"))
    try:
        _fields = [
            str(f) for f in (state.get("target_fields") or [])
            if str(f).strip()
        ]
        if _fields:
            lines.append(f"- Fields to extract: {', '.join(_fields[:20])}")
    except Exception:
        pass
    if len(lines) <= 1:
        return ""  # no artifact data — stay silent, don't emit an empty header
    block = "\n".join(lines) + "\n"
    if len(block) > 2048:
        block = block[:2047] + "…\n"
    return block


def build_code_writer_message(state: dict) -> list:
    """Build the initial HumanMessage for the code-writer agent.

    On retry cycles, includes the test report so the agent can apply fixes.
    """
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    product_url = state.get("product_url") or state.get("sample_url") or ""
    site_analysis = state.get("site_analysis") or {}
    scraper_analysis = state.get("scraper_analysis") or {}

    # Prior-art count (job-10 lesson, ~40 tokens — NOT the scraper file): a
    # previous run on this site already established a discovery baseline. The
    # writer gets the number as context; code_tester's count-regression check
    # (below) enforces it as a band, scope-matched.
    _prior_count_line = ""
    try:
        from scraper.models import ScrapeJob as _SJ

        _prior = (
            _SJ.objects.filter(
                site_folder__contains=slug, status=_SJ.STATUS_COMPLETED,
                product_count__gt=0,
            )
            .exclude(pk=state.get("job_id") or 0)
            .order_by("-product_count")
            .values_list("product_count", flat=True)
            .first()
        )
        if _prior:
            _prior_count_line = (
                f"\n### Prior run on this site\nA previous completed run extracted "
                f"**{_prior} items**. Your scraper should discover and extract a "
                f"catalog of that order of magnitude (unless this run's scope "
                f"deliberately narrows it — --limit/--sample). If your design "
                f"naturally yields far fewer, you have missed pagination or "
                f"categories.\n"
            )
    except Exception:
        pass
    mechanism = scraper_analysis.get("strategy") or site_analysis.get(
        "scraping_mechanism", ""
    )
    # When anti-bot is detected, stealth is applied at runtime via CloakBrowser
    # (a Playwright backend, injected when STEALTH_BROWSER=cloak).  Normalize a
    # UC-style mechanism to "playwright" so code_writer gets ONE consistent
    # instruction set (the cloak note: use p.chromium.launch()) instead of
    # CONFLICTING guidance (seleniumbase_section saying "MUST use SeleniumBase"
    # vs cloak note saying "do NOT use UC/Selenium").  Non-anti-bot sites that
    # genuinely need SeleniumBase are unaffected.  Generic. [code_writer clarity]
    try:
        from agents.tools.context import is_anti_bot_detected

        if is_anti_bot_detected() and mechanism in (
            "seleniumbase_uc", "undetected_chromedriver", "stealth_browser", "uc_chrome",
        ):
            mechanism = "playwright"
    except Exception:
        pass
    algolia = site_analysis.get("algolia", {})
    site_input_urls = state.get("input_urls") or []

    content_type_context = _build_content_type_context(state)
    output_schema = state.get("output_schema", {})
    _template_family = ""
    if output_schema and "template_family" in output_schema:
        _template_family = output_schema["template_family"]

    provided_urls_section = ""
    if site_input_urls:
        count = len(site_input_urls)
        provided_urls_section = (
            f"\n### PROVIDED URLs (FROM SITE MODEL)\n"
            f"The user has provided {count} URLs via the Site configuration. "
            f"These URLs are already saved to `workspace/{slug}/input_urls.json`.\n\n"
            f"**You MUST use these URLs exactly as provided.** Do NOT discover new URLs, "
            f"do NOT crawl categories or sitemaps, do NOT modify the URL list. "
            f"Read the existing `input_urls.json` and use it as-is in the scraper.\n"
            f"The `--sample` flag should scrape the first 5 URLs from this list.\n"
        )

    retry_section = ""
    test_report_path = f"workspace/{slug}/test_report.json"
    if state.get("test_report"):
        retry_section = (
            f"\n\n{_summarize_test_report(state)}\n"
            f"Read the full test report at: {test_report_path}\n"
            f"Fix the scraper at: workspace/{slug}/scraper_draft.py\n"
            f"Focus on the HIGH severity issues above. Do NOT change parts that work.\n"
        )

    human_feedback_section = ""
    human_feedback = state.get("human_feedback", "")
    if human_feedback:
        human_feedback_section = (
            f"\n\n### User Feedback (from approval)\n"
            f"The user provided this feedback after reviewing the failed test:\n"
            f"> {human_feedback}\n\n"
            f"Address this feedback in your fix. The user's insight may identify "
            f"the root cause that automated testing missed.\n"
        )

    algolia_section = ""
    if algolia and algolia.get("detected"):
        algolia_section = (
            f"\n### Algolia API\n"
            f"If the site uses Algolia for product data:\n"
            f"- Use Algolia as a **per-product lookup**: extract the product ID "
            f"from the URL and query Algolia by objectID\n"
            f"- Endpoint: {algolia.get('endpoint', ' Algolia Search API')}\n"
            f"- App ID: {algolia.get('application_id', '')}, "
            f"Key: {algolia.get('api_key', '')}\n"
            f"- Index: {algolia.get('index_name', '')}\n"
            f"- Do NOT implement discover mode, facet partitioning, or bulk extraction\n"
            f"- Products come from `input_urls.json`, not Algolia discovery\n"
        )

    template_hint = ""
    input_mode = (state.get("input_mode") or "").lower()
    template_file = ""
    # Navigation-style jobs (discovery required) use the two-phase navigation
    # template — but ONLY for BROWSER access strategies. API/HTTP strategies
    # (e.g. AMN's backend-API discovery, method=http_requests) keep their own
    # single-phase template. Stealth for browser jobs is applied at runtime via
    # STEALTH_BROWSER (cloak), NOT by switching to the UC template.
    _browser_strategies = (
        "playwright", "stealth_browser", "undetected_chromedriver",
        "seleniumbase_uc", "browser", "uc_chrome", "http_navigation", "",
    )
    if input_mode in ("navigation", "list_page", "search_term") and mechanism in _browser_strategies:
        if mechanism == "http_navigation":
            template_file = "http_navigation_scraper.py"
        elif mechanism == "playwright":
            template_file = "playwright_scraper.py"
        else:
            template_file = "navigation_scraper.py"
    elif mechanism:
        template_file = f"{mechanism}_scraper.py"
        if mechanism in (
            "stealth_browser",
            "undetected_chromedriver",
            "seleniumbase_uc",
        ):
            template_file = "undetected_chromedriver_scraper.py"
    if template_file:
        _cloak_note = ""
        try:
            from agents.tools.context import is_anti_bot_detected

            if is_anti_bot_detected():
                if mechanism == "http_navigation":
                    # http_navigation: the scraper calls browser_service's /navigate
                    # endpoint per page. Cloak is applied SERVER-SIDE — the scraper
                    # just passes `stealth: "cloak"` in the /navigate payload.
                    _cloak_note = (
                        "\n**STEALTH:** This site uses anti-bot/Akamai protection. Pass "
                        "`stealth: \"cloak\"` in the /navigate payload for every call — "
                        "set `STEALTH = \"cloak\"` at the top of the scraper so it is the "
                        "default. The browser_service /navigate endpoint applies "
                        "CloakBrowser's stealth Chromium (C++ fingerprint patches) "
                        "server-side per call. Do NOT import playwright or selenium, do "
                        "NOT set the STEALTH_BROWSER env var, do NOT call "
                        "cloakbrowser.launch() directly — stealth is entirely server-side "
                        "via the /navigate `stealth` field. Use the `http_navigation` "
                        "strategy.\n"
                        "**ANTI-BOT PLAYBOOK (both phases via /navigate + cloak):**\n"
                        "- **Discovery (Phase 1):** call `_navigate(SEARCH_URL, "
                        "actions=[fill, click, wait, sleep], stealth=\"cloak\")` to drive "
                        "the search form; extract product links from the returned HTML "
                        "(`_extract_item_links(r[\"html\"])`) — keep SAME-DOMAIN links, "
                        "drop nav/category/account/help. Verify `len(product_urls) > 0` "
                        "before Phase 2. Do NOT use a backend HTTP API for discovery on "
                        "an anti-bot site (likely protected: HTTP 400/403).\n"
                        "- **Extraction (Phase 2):** for each product URL, call "
                        "`_navigate(url, stealth=\"cloak\")` + read JSON-LD from the "
                        "returned HTML (`<script type=\"application/ld+json\">` → Product "
                        "schema: Offers.price / priceCurrency / availability). Cloak "
                        "renders JSON-LD reliably.\n"
                    )
                else:
                    _cloak_note = (
                        "\n**STEALTH:** This site uses anti-bot/Akamai protection. Stealth "
                        "is handled AUTOMATICALLY at runtime — browser_service's cloak_stealth "
                        "patch wraps Playwright's launch() to drive CloakBrowser's stealth "
                        "Chromium (C++ fingerprint patches + build_args + ignore_default_args) "
                        "when STEALTH_BROWSER=cloak is set. **Just use a normal "
                        "`p.chromium.launch()`** inside `with sync_playwright() as p:` — do NOT "
                        "call `cloakbrowser.launch()` directly (it starts a second Playwright and "
                        "crashes), do NOT swap to UC/Selenium, do NOT add anti-bot workarounds. "
                        "Use the `playwright` strategy.\n"
                        "**ANTI-BOT PLAYBOOK (both phases via the cloak browser):**\n"
                        "- **Discovery (Phase 1):** render the search/category URL "
                        "(`navigation_analysis.search.working_url`) via `page.goto(url, "
                        "wait_until='domcontentloaded')` + a short wait; then extract product "
                        "links from the RENDERED DOM with `page.eval_on_selector_all('a[href]', "
                        "'els => els.map(e => e.href)')`. Keep every SAME-DOMAIN link, then DROP "
                        "obvious non-product links (nav/category/account/help). Verify "
                        "`len(product_urls) > 0` before Phase 2. Do NOT use a backend HTTP API "
                        "for discovery on an anti-bot site (likely protected: HTTP 400/403).\n"
                        "- **Extraction (Phase 2):** for each product URL, render via cloak + "
                        "read JSON-LD (`<script type=\"application/ld+json\">` → Product schema: "
                        "Offers.price / priceCurrency / availability). Cloak renders JSON-LD "
                        "reliably.\n"
                    )
        except Exception:
            pass
        # T2.5: when edit-over-write is active (graph verified a parseable draft
        # from THIS template exists), "use the template as your base" is a
        # rewrite invitation — it discards the prior draft's accumulated fixes.
        # Point the writer at the draft instead.
        _eow_base = str(state.get("last_writer_template") or "")
        if _eow_base and _eow_base == template_file:
            template_hint = (
                f"\n### EDIT MODE — DO NOT REGENERATE\nA working draft already exists at "
                f"workspace/{slug}/scraper_draft.py (built from templates/{template_file}). "
                "Treat THAT draft as your base: apply ONLY the targeted edits the test "
                "report asks for. Do NOT restart from the template — that discards prior "
                "fixes and reintroduces past failures.\n"
            )
        else:
            template_hint = (
                f"\n### Template\nRead the template at: templates/{template_file} "
                f"and use it as your base. The scraper will run on a dedicated worker "
                f"container that has Chrome, SeleniumBase, Playwright, and CloakBrowser "
                f"installed.{_cloak_note}"
            )

    scraper_analysis_section = ""
    if scraper_analysis:
        proxy_tier = scraper_analysis.get("proxy_tier", "none")
        no_proxy = scraper_analysis.get("no_proxy_flag", proxy_tier == "none")

        proxy_instructions = ""
        if no_proxy:
            # T3.5: "direct works" is only TRUE when the probe's working method
            # was itself a direct-HTTP tier. no_proxy_flag can also mean "the
            # analyzer chose no proxy for a BROWSER strategy" — telling the
            # writer "direct connection works" then shipped bare
            # requests.get() paths against sites the probe only reached through
            # a browser (the job-62 birkenstock 200-challenge soft block).
            _conn_t35 = site_analysis.get("connectivity") if isinstance(
                site_analysis.get("connectivity"), dict
            ) else {}
            _probe_t35 = state.get("probe_result") if isinstance(
                state.get("probe_result"), dict
            ) else {}
            _conn_t35 = _conn_t35 or (_probe_t35.get("connectivity") if isinstance(
                _probe_t35.get("connectivity"), dict
            ) else {})
            _method_t35 = str(
                _conn_t35.get("method_that_worked")
                or _probe_t35.get("method")
                or ""
            )
            if _method_t35.startswith("direct_http"):
                proxy_instructions = (
                    "\n**PROXY: Do NOT use any proxy.** The probe VERIFIED that direct "
                    "HTTP (no proxy) reaches this site. The scraper MUST accept "
                    "`--no-proxy` flag and should default to NO proxy for this site. "
                    "Do NOT import or use the proxy module.\n"
                )
            else:
                _reached = _method_t35 or "an unknown probe method"
                proxy_instructions = (
                    "\n**PROXY: Do NOT use any proxy.** The strategy for this site uses "
                    "no proxy tier. **CAUTION: the probe did NOT verify direct HTTP — "
                    f"it reached the site via `{_reached}`. Direct HTTP may be BLOCKED "
                    "(soft challenge / 403). Do NOT ship a bare `requests.get()` fetch "
                    "path unless the strategy is genuinely HTTP-based; for browser "
                    "strategies fetch every page through the browser.** The scraper MUST "
                    "accept `--no-proxy` flag. Do NOT import or use the proxy module.\n"
                )
        elif proxy_tier == "datacenter":
            proxy_instructions = (
                "\n**PROXY: Use datacenter proxy.** The scraper should use the datacenter "
                "proxy from `config/proxy.json` via `src/proxy.py`.\n"
            )
        elif proxy_tier == "residential":
            proxy_instructions = (
                "\n**PROXY: Use residential proxy (expensive).** Only use as last resort. "
                "The scraper should use residential proxy from `config/proxy.json`.\n"
            )

        verified_section = _render_verified_selectors(scraper_analysis)

        extraction_approach = scraper_analysis.get("extraction_approach", "")
        approach_section = ""
        if extraction_approach:
            approach_section = f"\n### Extraction Approach: {extraction_approach}\n"

        warmup = scraper_analysis.get("warmup_required", False)
        cookie = scraper_analysis.get("cookie_consent_required", False)
        extras = []
        if warmup:
            extras.append(
                "- **Warmup required:** Visit homepage first, wait for anti-bot sensors"
            )
        if cookie:
            extras.append(
                "- **Cookie consent required:** Accept cookies before scraping"
            )
        extras_section = ""
        if extras:
            extras_section = (
                "\n### Additional Requirements\n" + "\n".join(extras) + "\n"
            )

        seleniumbase_section = ""
        if mechanism in (
            "stealth_browser",
            "undetected_chromedriver",
            "seleniumbase_uc",
        ):
            seleniumbase_section = (
                "\n### SeleniumBase UC Mode — MANDATORY API Constraints\n"
                "The scraper MUST use SeleniumBase with UC Mode. Follow these rules EXACTLY:\n\n"
                "**SB() constructor — ONLY valid kwargs (SeleniumBase 4.44+):**\n"
            "```python\n"
            "with SB(uc=True, xvfb=args.xvfb, locale_code='en-gb') as sb:\n"
            "    driver = sb.driver\n"
            "```\n"
            "Valid kwargs: `uc`, `xvfb`, `locale_code`, `proxy`, `chromium_arg`, "
            "`page_load_strategy`, `driver_type`, `extension_zip`, `extension_dir`, "
            "`use_auto_ext`\n"
            "The run_scraper tool auto-injects `--xvfb` CLI flag. "
            "Your argparse MUST accept `--xvfb` (action='store_true') and use "
            "`args.xvfb` in SESSION_KWARGS — otherwise argparse rejects the flag and the scraper crashes.\n\n"
            "**INVALID kwargs (NEVER use):** `browser_args` (wrong → use `chromium_arg`), "
            "`chrome_args` (wrong → use `chromium_arg`), "
            "`headless=True` with `uc=True` (unreliable → use `xvfb=True`), "
            "`driver_kwargs` (doesn't exist)\n\n"
            "**Proxy with auth:** Use `extension_zip=/path/to/auth.zip` kwarg "
            "(NOT `use_auto_ext` — that enables Chrome's built-in automation extension). "
            "The template already has `_make_proxy_auth_extension()` — call it and pass "
            "the result as `extension_zip`. Use `chromium_arg` for `--proxy-server` flag.\n\n"
                "**Page navigation — ALWAYS use driver.uc_open_with_reconnect():**\n"
                "```python\n"
                "driver.uc_open_with_reconnect(url, reconnect_time=4)\n"
                "time.sleep(3)\n"
                "```\n"
                "Do NOT use `sb.open()` — it triggers EMPTY_PAGE_BLOCK detection that kills the session.\n\n"
                "**JS execution — ALWAYS use driver.execute_script() directly:**\n"
                "```python\n"
                "data = driver.execute_script('return document.title')\n"
                "```\n"
                "Do NOT use `sb.execute_script()` (CDP Mode limitations) or "
                "`sb.driver.execute_script()` (can crash CDP). "
                "Just `driver.execute_script()` — it's the raw WebDriver API.\n\n"
                "**Pattern summary:**\n"
            "```python\n"
            "with SB(uc=True, xvfb=args.xvfb) as sb:\n"
                "    driver = sb.driver\n"
                "    driver.uc_open_with_reconnect(url, reconnect_time=4)\n"
                "    time.sleep(3)\n"
                "    data = driver.execute_script('return document.title')\n"
                "```\n"
            )

        # CRITICAL FIX block: scraper_analyzer writes `critical_fix` (and
        # `retry_adjustments`) when a prior scrape crashed on a known defect —
        # e.g. a CSS selector that DOES NOT EXIST on the page. Without this
        # block code_writer never sees the documented fix and regenerates the
        # same defect on every retry (root cause of the locumtenens loop).
        # Surfaced FIRST, above everything else, with imperative framing.
        # Rendered via the shared _render_critical_fix helper (also used by
        # code_reviewer) so both agents see identical critical-fix context.
        critical_fix_section = _render_critical_fix(scraper_analysis)

        scraper_analysis_section = (
            f"\n### Scraper Analysis (VERIFIED — follow these instructions)\n"
            f"{critical_fix_section}"
            f"**Strategy:** {mechanism}\n"
            f"**Proxy tier:** {proxy_tier}\n"
            f"**Strategy justification:** {scraper_analysis.get('strategy_justification', '')}\n"
            f"{proxy_instructions}{approach_section}{verified_section}{extras_section}"
            f"{seleniumbase_section}"
        )

    navigation_section = ""
    navigation_analysis = state.get("navigation_analysis") or {}
    # SOURCE FIX: navigation_synthesize (LLM) sometimes drops the product URLs
    # that navigation_explore discovered. Merge them from navigation_findings.json
    # HERE — at the point code_writer consumes the analysis. This ensures
    # code_writer has the actual product URLs to build the scraper around.
    if navigation_analysis:
        try:
            import json as _json_nf, os as _os_nf
            _slug = state.get("site_slug", "")
            _nf_path = _os_nf.join(settings.PROJECT_ROOT, "workspace", _slug, "navigation_findings.json")
            if _os_nf.isfile(_nf_path):
                _nf = _json_nf.load(open(_nf_path))
                _lp = _nf.get("listing_page") or {}
                _purls_raw = _lp.get("product_links") or []
                _purls = []
                for u in _purls_raw:
                    if isinstance(u, str):
                        _purls.append(u)
                    elif isinstance(u, dict) and u.get("href"):
                        _purls.append(u["href"])
                if _purls:
                    _il = navigation_analysis.get("item_links")
                    if not isinstance(_il, dict):
                        _il = {}
                    _existing = [u for u in (_il.get("urls") or []) if isinstance(u, str)]
                    if len(_existing) < len(_purls):
                        _il["urls"] = list(dict.fromkeys(_existing + _purls))
                        navigation_analysis["item_links"] = _il
                        logger.info("build_code_writer_message: merged %d product URLs from findings → nav_analysis", len(_purls))
        except Exception:
            pass
        input_mode = state.get("input_mode", "url_list")
        discovery = navigation_analysis.get("discovery_method", "unknown")
        search_info = navigation_analysis.get("search", {})
        pagination_info = navigation_analysis.get("pagination") or {}
        item_links_info = navigation_analysis.get("item_links", {})
        search_criteria = state.get("search_criteria", "")

        nav_lines = [
            "\n### Navigation Analysis (TWO-PHASE SCRAPER REQUIRED)\n",
            f"**Discovery method:** {discovery}",
            f"**Input mode:** {input_mode}",
        ]

        if search_info.get("has_search") or search_info.get("has_url_search"):
            nav_lines.append("**Search:** supported")
            working_search_url = search_info.get("working_url") or search_info.get("listing_url_used")
            if working_search_url:
                nav_lines.append(f"  - **Working search URL (USE THIS):** `{working_search_url}`")
            if search_info.get("url_pattern") and search_info.get("url_pattern") != working_search_url:
                nav_lines.append(f"  - URL pattern (from form action — may be WRONG): `{search_info['url_pattern']}`")
            if search_info.get("search_url_pattern") and search_info.get("search_url_pattern") != working_search_url:
                nav_lines.append(f"  - Search URL pattern (may be WRONG): `{search_info['search_url_pattern']}`")
            if search_info.get("input_selector"):
                nav_lines.append(f"  - Search input: `{search_info['input_selector']}`")
            if search_criteria:
                nav_lines.append(f'  - Search criteria: "{search_criteria}"')

        if pagination_info.get("type"):
            nav_lines.append(f"**Pagination:** {pagination_info['type']}")
        elif pagination_info and any(pagination_info.values()):
            nav_lines.append("**Pagination:** detected (type not specified)")
            if pagination_info.get("next_button_selector"):
                nav_lines.append(
                    f"  - Next button: `{pagination_info['next_button_selector']}`"
                )
            if pagination_info.get("next_text"):
                nav_lines.append(
                    f"  - Next button text: \"{pagination_info['next_text']}\""
                )
            if pagination_info.get("next_href"):
                nav_lines.append(
                    f"  - Next href: `{pagination_info['next_href']}`"
                )
            if pagination_info.get("page_param_name"):
                nav_lines.append(
                    f"  - Page param: `{pagination_info['page_param_name']}`"
                )
            if pagination_info.get("max_pages"):
                nav_lines.append(f"  - Max pages: {pagination_info['max_pages']}")
            if pagination_info.get("note"):
                nav_lines.append(f"  - Note: {pagination_info['note']}")
            if pagination_info.get("page_indicator_text"):
                nav_lines.append(
                    f"  - Page indicator: \"{pagination_info['page_indicator_text']}\""
                )

        # Strong directive: paginate the FULL catalog (don't stop at page 1).
        _pg_param = pagination_info.get("page_param_name") if pagination_info else None
        nav_lines.append(
            "\n**DISCOVERY — paginate EVERY page (CRITICAL for full extraction):** "
            "Search/category pages usually show only 24-48 products each. You MUST "
            "follow pagination to the LAST page to discover the full catalog (often "
            "65-200+ products across multiple pages). Set `MAX_PAGES` high (e.g. 20) "
            "or null (unlimited), and keep paginating until a page returns NO new "
            "product URLs. Stopping at page 1 misses most products — a discovery "
            "failure.\n"
            "**Pagination method (HARD RULE):** use the template's "
            "`_get_next_page_url` UNMODIFIED — do NOT write a custom pagination loop. "
            f"When `page_param_name` is known (`{_pg_param or 'the detected param'}`), "
            f"the template CONSTRUCTS `?{_pg_param or 'param'}=N` directly "
            "(deterministic — immune to the first-match pitfall). NEVER use a "
            "`a[href*='param']` click selector — it matches EVERY numbered page link "
            "and the first is always page 1 → the scraper re-fetches page 1 forever. "
            "Reserve click-based pagination for `next_button`/`infinite_scroll`/`load_more`.\n"
        )

        if item_links_info.get("container_selector"):
            nav_lines.append(
                f"**Item links:** container `{item_links_info['container_selector']}` "
                f"→ link `{item_links_info.get('link_selector', 'a')}`"
            )
        if item_links_info.get("url_pattern"):
            nav_lines.append(f"  - URL pattern: `{item_links_info['url_pattern']}`")

        # Discovery mechanics the structured nav_lines above don't cover
        # (filters/POST-form strategy, search/category notes). Injected so
        # code_writer doesn't need to read_file navigation_analysis.json.
        nav_lines.append(_summarize_navigation_extras(navigation_analysis))
        nav_lines.append(
            "\n### Two-Phase Architecture (REQUIRED for this scraper)\n"
            "This scraper must implement TWO phases:\n\n"
            "**Phase 1: Discover item URLs (use navigation_analysis — do NOT re-discover)\n"
            "- Phase 1 MUST start from the listing URL in navigation_analysis.search\n"
        )

        working_first_url = search_info.get("working_url") or search_info.get("listing_url_used")

        if working_first_url:
            nav_lines.append(
                f"- First URL: `{working_first_url}` "
                f"(this is where {len(item_links_info.get('url_examples', []))} products were found)\n"
            )
        elif search_info.get("search_url_pattern"):
            nav_lines.append(
                f"- First URL: `{search_info['search_url_pattern']}` (replace `{{criteria}}` with `{search_criteria}`)\n"
            )
        elif search_info.get("url_pattern"):
            nav_lines.append(
                f"- First URL: `{search_info['url_pattern']}`\n"
            )

        if item_links_info.get("link_selector") and item_links_info.get("link_selector") != "a[href]":
            nav_lines.append(
                f"- Extract product links using selector: `{item_links_info['link_selector']}` "
                f"within container: `{item_links_info['container_selector']}`\n"
            )
        elif item_links_info.get("link_selector"):
            nav_lines.append(
                f"- Extract product links within container: `{item_links_info.get('container_selector', 'product grid')}`\n"
            )

        nav_lines.append(
            "- Paginate through all result pages (click 'next page' links, "
            "load more buttons, or scroll for infinite scroll)\n"
            "- Collect item page URLs (NOT content — just URLs)\n"
            "- Filter: only keep URLs matching the pattern from navigation_analysis\n"
            "- Store discovered URLs in a list\n\n"
            "**Phase 2: Scrape each item page**\n"
            "- For each discovered URL, extract field data\n"
            "- Map raw data to output fields\n"
            "- Write results to output file\n\n"
            "**CRITICAL:** Do NOT write your own discovery logic from scratch. "
            "The navigation_analysis has the exact URL and selectors that found "
            f"{len(item_links_info.get('url_examples', []))} products. Use them.\n"
        )

        _template_family = "navigation"
        # T3.2 single authority: this hint now derives from the SAME selector
        # graph.py uses for the system-prompt template injection
        # (agents/template_selector.select_template_file). The old inline
        # mechanism-first derivation never returned api/ssr_div_list/requests
        # templates, so for those strategies the writer read one template's
        # code (system prompt) while being pointed at another (this hint).
        try:
            from .template_selector import select_template_file

            _nav_template_file = select_template_file(state)
        except Exception:
            _nav_template_file = (
                "http_navigation_scraper.py" if mechanism == "http_navigation"
                else "playwright_scraper.py"
            )
        # T2.5 (nav half): same edit-over-write suppression as the url_list hint —
        # when the surviving draft came from this template, the draft IS the base.
        _eow_nav = str(state.get("last_writer_template") or "")
        if _eow_nav and _eow_nav == _nav_template_file:
            nav_template_hint = (
                f"\n### EDIT MODE — DO NOT REGENERATE\nA working two-phase draft already "
                f"exists at workspace/{slug}/scraper_draft.py (built from "
                f"templates/{_nav_template_file}). Treat THAT draft as your base: apply "
                "ONLY the targeted edits the test report asks for, keeping the proven "
                "Phase 1 (navigation/discovery) and Phase 2 (extraction) structure. Do NOT "
                "restart from the template — that discards prior fixes and re-discovers "
                "from zero.\n"
            )
        else:
            nav_template_hint = (
                f"\n### Template\nRead the template at: templates/{_nav_template_file} "
                "and use it as your base for the two-phase architecture. "
                "Adapt the Phase 1 (navigation) and Phase 2 (extraction) logic "
                "to match this site's patterns.\n"
            )

        navigation_section = "\n".join(nav_lines)

        # Set True when one of the precedence data models (api > ssr_div_list >
        # embedded_json) REPLACES navigation_section below. Those modes are
        # browser-free / non-two-phase by design (their de-bloat comments), so
        # the two-phase-framed appends further down (the "Phase 1 discovery"
        # filter header and the classic-search browser block) must not leak
        # onto them — they would give the LLM two OPPOSING instructions again.
        _dm_replaced = False

        # If navigate_explore captured a backend JSON search API (React/Vue SPA,
        # e.g. AMN Healthcare's /JobSearch), PREFER a clean HTTP api_scraper over
        # driving the browser.  The API returns fully-populated items, so the
        # browser two-phase discovery below is superseded.
        api_endpoint = navigation_analysis.get("api_endpoint") or {}
        api_section = ""

        if api_endpoint.get("url") or api_endpoint.get("api_url"):
            api_base = api_endpoint.get("base") or str(api_endpoint.get("url", "")).split("?")[0]
            api_params = api_endpoint.get("query_params") or []
            page_param = api_endpoint.get("pagination_param") or (
                "PageNumber" if "PageNumber" in api_params
                else ("page" if "page" in api_params else "page")
            )
            page_size_param = api_endpoint.get("page_size_param") or "PageSize"
            api_section = (
                "\n### CRITICAL — Backend JSON search API discovered (PREFERRED — do NOT drive a browser)\n"
                "The site renders listings client-side by calling a JSON search API, which "
                "navigate_explore captured from the browser's network calls. **Use this API "
                "directly with HTTP `requests` — do NOT use Playwright/Selenium, do NOT parse "
                "the DOM, and do NOT follow the two-phase browser discovery above.**\n"
                f"- **Endpoint:** `{api_endpoint.get('method', 'GET')} {api_base}`\n"
                f"- **Discovered query params:** {api_params}\n"
                "- The complete working URL (with every param + value) is in "
                "`navigation_analysis.api_endpoint.url` — READ it. But you do NOT need every "
                "captured param: many are **facet selectors** (e.g. repeated `FilterTypes=`) that "
                "only control which filter facets are RETURNED in the response, NOT which items "
                "match. **Use a MINIMAL URL** — just the search/location param + "
                f"`{page_param}` + `{page_size_param}` (+ orderby if present). Omit facet "
                "params entirely. A truncated/guessed facet value (e.g. `PayRateTyp` instead of "
                "`PayRateType`) causes HTTP 400, so dropping facets is the safe choice.\n"
                f"- **Pagination:** increment `{page_param}` starting at 1; use a large "
                f"`{page_size_param}` (e.g. 100) to minimize calls. Stop when "
                "`len(items) >= response total` — the total is in a key like `jobCount`, "
                "`totalCount`, `count`, or `total` (inspect the first response).\n"
                "- **Headers:** set a real browser `User-Agent`, `Accept: application/json`, "
                "and `Origin` + `Referer` matching the site.\n"
                "- **No proxy needed — this is a public API.** Send DIRECT requests "
                "(`proxies=None`); a datacenter/residential proxy is unnecessary and may be "
                "rejected. If a proxy helper is in the template, force the no-proxy/direct path.\n"
                "- **No auth/cookies/subscription key required.** If a request fails, re-check "
                "the param NAMES — do not add login or key logic.\n"
            )
            # Anti-bot caveat: the captured API may itself be protected (e.g. calvklein's
            # PVH API returns 400 directly). When anti-bot is detected, tell code_writer to
            # VERIFY the API + fall back to browser+cloak+JSON-LD if it fails. Generic.
            try:
                from agents.tools.context import is_anti_bot_detected

                if is_anti_bot_detected():
                    api_section += (
                        "\n**⚠️ ANTI-BOT SITE — VERIFY THE API, ELSE FALL BACK TO BROWSER.** "
                        "This site uses anti-bot protection, so the captured API may itself be "
                        "protected (returns 400/403/empty when called directly, without the "
                        "browser-warmed headers/cookies). **On the FIRST run, make a single test "
                        "request and check you actually get items back.** If the API returns 0 "
                        "items or an error, do NOT keep retrying it — SWITCH to rendering each "
                        "page with the cloak browser (`p.chromium.launch()`; stealth is applied "
                        "automatically via STEALTH_BROWSER=cloak) and extract fields from JSON-LD "
                        "(`<script type=\"application/ld+json\">` — Product schema → Offers.price / "
                        "priceCurrency / availability), which the cloak browser renders reliably. "
                        "The browser path is the reliable fallback for anti-bot sites.\n"
                    )
            except Exception:
                pass
            if (state.get("page_type") or "").lower().startswith("job"):
                api_section += (
                    "\n**This API returns FULLY-populated items — Phase 2 (per-detail scrape) "
                    "is NOT needed.** Map fields with the GENERIC resolver; do NOT hardcode any "
                    "site-specific field names (no `divisionCompany.companyName`, no `or`-chains):\n"
                    "- **Use `src/job_fields.py`.** `from src.job_fields import map_jobs` then "
                    "`jobs = map_jobs(sample_items=first_page, raw_items=all_items)`. It "
                    "auto-detects the source path for each standard job field (title, company, "
                    "location, description, salary, job_type, posted_date, apply_url, "
                    "requirements) by coverage over the sample — it handles nested objects, "
                    "composite location (city.name + state.abbrev -> 'City, ST'), salary ranges, "
                    "list-valued employmentType, and normalizes posted_date to ISO-8601. The "
                    "resolver picks whatever source is actually populated for THIS site, so the "
                    "same code works across job platforms without per-site edits.\n"
                    "- **Verify each field.** code_tester reports per-field coverage in "
                    "`results.field_coverage`. If a field shows `MISSING`/0% the resolver found "
                    "no populated candidate — if a real source exists that the alias table "
                    "misses, ADD it to `JOB_ALIASES` in `src/job_fields.py` (do not patch the "
                    "generated scraper). Never ship a core field at 0%.\n"
                    "- **Construct `url` when the API has none.** Many job APIs expose a job "
                    "ID but no direct URL (the posting is a SPA route). If `map_jobs` leaves "
                    "`url` empty, build it per item from the job ID using the job-link pattern "
                    "navigate_explore discovered (see `navigation_findings.json` product_links / "
                    "`navigation_analysis.item_links.url_pattern`, e.g. "
                    "`https://site/job-details/{jobID}/{slug}/`). Set this constructed URL on "
                    "each mapped job so the `url` core field is populated.\n"
                    "- **Date filter (last 7 days):** there is usually NO server-side "
                    "posted-date param. Fetch ALL pages, then KEEP ONLY items whose `posted_date` "
                    "(normalized to ISO-8601 `YYYY-MM-DD` by the resolver) is within "
                    "`datetime.now(timezone.utc) - 7 days`.\n"
                    "- Add a `--query` arg defaulting to the target location (e.g. 'Alabama') "
                    "and feed it into the API's location query param. The captured URL may have "
                    "been taken from a category/browse page and OMIT the location param — the "
                    "site's location search box is in "
                    "`navigation_analysis.filters.location_filter` / findings `filter_ui.location_selectors`. "
                    "Add the location param and VERIFY it works: the response total should DROP "
                    "to only jobs in that location. Common param names to try (in order): "
                    "`LocationSearch`, `location`, `state`, `State`, `city`, `q`. Pair with a "
                    "radius/distance param if one appears in the captured URL (e.g. "
                    "`LocationDistance`).\n"
                )
            api_section += (
                "\n**CATALOG COVERAGE (HARD RULE — applies to API loops too):**\n"
                "A single endpoint's first page is NOT the catalog. Enumerate EVERYTHING:\n"
                "- If the API paginates (page/offset/cursor params in the captured URL or "
                "response): LOOP until exhausted — keep fetching while the response returns "
                "new items; stop only on an empty page or a repeated set. Record how many "
                "pages you fetched and how many unique items total in the output metadata "
                "(`metadata.pages_fetched`, `metadata.total_discovered`).\n"
                "- If the API has category/search variants: enumerate the categories FIRST "
                "(one request each) and aggregate — a single-category pull is an incomplete "
                "run. Use the category taxonomy from navigation_analysis when present.\n"
                "- If genuinely only ONE call exists (a config-like response with a fixed "
                "array), say so in a code comment — but VERIFY by trying ?page=2 and a "
                "category param before concluding that.\n"
                "- **src_url discipline:** every record's `src_url` must be the LISTING or "
                "SEARCH URL the item was discovered from (or the API query URL that returned "
                "it) — NOT the record's own detail URL and NOT the API endpoint host root. "
                "Partners use src_url to reconstruct discovery provenance.\n"
                "\n**Output:** write the filtered list to `output_{datetime}.json`.\n"
            )
            # The API loop replaces the browser navigation template.
            nav_template_hint = (
                "\n### Template\nRead templates/api_scraper.py as your base (HTTP + JSON). "
                "The entire scraper is ONE paginated API loop — no Playwright/browser.\n"
            )
            # De-bloat (#1 audit finding — code_writer clarity): emit ONLY the API
            # data-model section. Do NOT concatenate with the two-phase browser
            # text below — that gives the LLM two OPPOSING instructions
            # ("use HTTP API, no browser" vs "drive a browser, two-phase detail
            # pages"), which was the top contradiction flagged in the audit.
            # Precedence: api_endpoint > embedded_json > two-phase (below).
            navigation_section = api_section
            _dm_replaced = True

        # SSR div-listing data model: items are server-rendered divs/li with
        # data-*-id on a single listing page (no per-item detail pages).
        # Precedence: api > ssr_div_list > embedded_json > two-phase.
        _goal_url = (navigation_analysis.get("search") or {}).get("working_url", "")
        if not api_section and navigation_analysis.get("data_source") == "ssr_div_list":
            nav_template_hint = (
                "\n### Template\nRead templates/ssr_div_list_scraper.py as your base. "
                "The items are divs/li with data-*-id attributes on a SINGLE listing "
                "page — extract records directly from the listing DOM. NO per-item "
                "detail pages, NO Phase 1/Phase 2 split. Paginate via ?page=N.\n"
            )
            navigation_section = (
                f"\n### ★ DATA MODEL = SSR DIV-LIST (extract from listing DOM)\n"
                f"The listing page `{_goal_url}` has items as server-rendered div/li "
                f"elements with data-*-id attributes. Extract records directly from "
                f"the listing page HTML — do NOT construct per-item detail URLs (they "
                f"will 404). Adapt `_find_items` (ITEM_SELECTOR) + `_extract_record` "
                f"(field selectors) in the template per the Field Map.\n"
            )
            _dm_replaced = True

        # Embedded-JSON listing data model: items live in a <script> JSON blob in
        # the listing/category page (NOT detail pages, NOT an API). Activates only
        # when no backend API took precedence (api > embedded_json > two-phase).
        if not api_section:
            _emb_section = _embedded_json_code_writer_section(
                navigation_analysis, scraper_analysis, state, slug
            )
            if _emb_section:
                _emb_strategy = (scraper_analysis.get("strategy") or "").lower()
                if _emb_strategy in ("http_requests", "requests", "internal_api", "api"):
                    nav_template_hint = (
                        "\n### Template\nRead templates/requests_scraper.py as your base (HTTP "
                        "fetch). The scraper fetches each listing/category page over HTTP, extracts "
                        "the embedded JSON array from the <script> blob, maps records, and paginates "
                        "categories — NO browser, NO per-detail Phase 2.\n"
                    )
                else:
                    nav_template_hint = (
                        "\n### Template\nRead templates/http_navigation_scraper.py as your base. "
                        "Adapt Phase 1 to fetch each listing/category page via `_navigate`, then "
                        "extract the embedded JSON array from the returned HTML (in place of the "
                        "link extraction) and map records. There is NO per-detail Phase 2.\n"
                    )
                # De-bloat: emit ONLY the embedded-JSON section. Do NOT concatenate
                # with the two-phase text — the embedded-JSON guidance explicitly
                # says "NO per-detail Phase 2", contradicting the two-phase block.
                navigation_section = _emb_section
                _dm_replaced = True

        # Filter requirements (date/location/category — job portals & search sites)
        filters_info = navigation_analysis.get("filters", {}) or {}
        # fmethod is set inside the has_filters block below, but referenced by
        # the form/classic-search check further down — initialise it so a
        # navigation job with NO detected filters doesn't NameError there.
        fmethod = filters_info.get("method") if filters_info.get("has_filters") else None
        if filters_info.get("has_filters"):
            fmethod = filters_info.get("method", "url")
            # When a precedence data model replaced the two-phase text, keep
            # the filter VALUES (category/location/date params still steer
            # which listing/category URLs to fetch) but drop the two-phase
            # framing — there is no "Phase 1" in api/ssr/embedded mode.
            _filter_hdr = (
                "\n### Filter Requirements (apply while building the listing/"
                "category URLs to fetch)\n"
                if _dm_replaced
                else "\n### Filter Requirements (apply during Phase 1 discovery)\n"
            )
            filter_lines = [
                _filter_hdr,
                f"This site supports result filtering via: **{fmethod}**\n",
            ]
            for label, key in [
                ("Date", "date_filter"),
                ("Location", "location_filter"),
                ("Category", "category_filter"),
            ]:
                fcfg = filters_info.get(key) or {}
                if not fcfg:
                    continue
                _strategy = fcfg.get("strategy", "")
                _sval = fcfg.get("strategy_value")
                _dval = fcfg.get("detected_value", "")
                _values = fcfg.get("values") or []
                detail = []
                if fcfg.get("param_name"):
                    detail.append(f"URL param `{fcfg['param_name']}`")
                if fcfg.get("url_pattern"):
                    detail.append(f"pattern `{fcfg['url_pattern']}`")
                if fcfg.get("selector"):
                    detail.append(f"form element `{fcfg['selector']}`")
                if fcfg.get("form_action") or fcfg.get("submit_button"):
                    detail.append(
                        f"submit form `{fcfg.get('form_id') or fcfg.get('form_action')}`"
                        f" (button `{fcfg.get('submit_button')}`)"
                    )
                # Strategy-driven filter guidance (replaces hardcoded "Alabama" etc.).
                # pin → use the specific value from the query; iterate → loop options + dedup.
                if _strategy == "pin" and _sval:
                    filter_lines.append(
                        f"- **{label}**: PIN to `{_sval}` ({fcfg.get('reason','')}). "
                        f"{', '.join(detail)}.\n"
                    )
                elif _strategy == "iterate" or (not _strategy and _dval in ("all", "any", "", None) and _values):
                    _opts = [
                        (v.get("v", "") if isinstance(v, dict) else v)
                        for v in _values
                    ]
                    _opts = [o for o in _opts if o and o not in ("all", "any")]
                    filter_lines.append(
                        f"- **{label}**: ITERATE over {len(_opts)} options "
                        f"({_opts[:12]}{'...' if len(_opts) > 12 else ''}). For each, "
                        f"build the URL via the pattern above, collect item links, and "
                        f"**dedup by job/item ID** across iterations. (detected_value="
                        f"'{_dval}'; query didn't specify this dimension → enumerate "
                        f"for full catalog.)\n"
                    )
                elif _strategy == "ignore":
                    continue  # don't mention irrelevant filters
                else:
                    # Fallback (older analysis without strategy): use detected_value,
                    # NOT a hardcoded default. Surface options for code_writer to decide.
                    filter_lines.append(
                        f"- **{label}**: use detected_value `{_dval}`. {', '.join(detail)}. "
                        f"Options: {_values[:8]}{'...' if len(_values) > 8 else ''}\n"
                    )
            filter_lines.append(
                "\nApply pin filters to their values; iterate iterate-filters "
                "(loop options, dedup by ID). For URL-based filters, append params; "
                "for form-based, interact with the form. Sample job URLs "
                "(item_links.url_examples) are for field/selector mapping ONLY — "
                "do NOT infer filter values from their content.\n"
            )
            navigation_section += "\n".join(filter_lines)

        # Form-based / "classic" search discovery CANNOT be done via raw HTTP
        # (anti-forgery tokens + server-side session → HTTP 500). Force a
        # browser-driven Phase 1 and tell the code-writer exactly how.
        # SKIPPED when a precedence data model replaced the two-phase text:
        # its "Phase 1 MUST use Playwright" text contradicts the api/ssr/
        # embedded sections ("no browser / no per-detail Phase 2"). If a
        # browser fetch is genuinely needed there, the embedded section's
        # http_navigation template hint already says to adapt Phase 1 to
        # `_navigate` — that covers it without re-introducing the conflict.
        classic = (navigation_analysis.get("homepage_nav", {}) or {}).get("classic_search")
        if not _dm_replaced and (fmethod == "form" or classic):
            navigation_section += (
                "\n**CRITICAL — browser-driven Phase 1 (form/classic search):** "
                "This site's search is a POST form protected by anti-forgery tokens "
                "and a server-side session, so raw `requests.post(...)` returns HTTP "
                "500 and discovers nothing. Phase 1 MUST use **Playwright in a real "
                f"browser**. Read `workspace/{slug}/navigation_findings.json` "
                "(`homepage_nav.classic_search`) for the search-page URL and its "
                "`<select>` fields, then: open the search page, fill the dropdowns "
                "(Location → Alabama/AL; leave category at 'Any'), **click the submit "
                "button** to obtain the session results URL, then apply the result "
                "filters (set the date `<select>` to the 'Last 7 Days' option and "
                "click the result-form submit button), and paginate. NOTE: this "
                "site's form REQUIRES a **Specialty** selection to submit — a "
                "Discipline or Location alone will NOT submit (validation blocks "
                "it). So for **ALL categories** you MUST iterate the **Specialties** "
                "`<select>` (NOT Disciplines), submitting once per specialty and "
                "deduping the discovered links. Phase 2 (field extraction from the "
                "detail pages) may use HTTP since pages are server-rendered.\n"
                "**Form-interaction tips (important — sites vary):**\n"
                "- Submit buttons may be `input[type='submit']` OR `button[type='submit']` "
                "(LocumTenens' search form uses `input[type='submit']`; its results filter "
                "form uses `button[type='submit']`). Click with a fallback chain, e.g. try "
                "`input[type='submit']`, then `button[type='submit']`, then "
                "`form button`, then `form.requestSubmit()` — never assume `button` only.\n"
                "- `<select>` changes on jQuery sites need `page.select_option(sel, value)` "
                "(Playwright) which fires the right events; if a `<select>` is a "
                "bootstrap-multiselect, also dispatch a jQuery `change` after setting.\n"
            )

    if navigation_section:
        # Navigation jobs use the two-phase template selected above — either
        # http_navigation_scraper.py (httpx + /navigate, new default) or
        # navigation_scraper.py (legacy Playwright). Do NOT fall back to the
        # SeleniumBase UC template — it bypasses the cloak safety net that lets
        # the browser strategy defeat Akamai on anti-bot sites.
        template_hint = _template_family and nav_template_hint or ""

        # Form-search iteration hint: if navigation submitted a form with a
        # <select> filter (e.g. locumtenens Specialty), tell code_writer to
        # fill in FORM_ACTION + FORM_SELECT_NAME so the template iterates
        # through ALL options (not just one).
        _nav_form_data = (navigation_analysis.get("search") or {}).get("form_data") or {}
        _nav_form_action = (navigation_analysis.get("search") or {}).get("form_action") or ""
        if _nav_form_action and _nav_form_data:
            # Find the select field name (the key in form_data that's a filter)
            _select_name = ""
            for k in _nav_form_data:
                if any(t in k.lower() for t in ("special", "discipline", "category", "profession", "role")):
                    _select_name = k
                    break
            if not _select_name and _nav_form_data:
                _select_name = next(iter(_nav_form_data))
            if _select_name:
                template_hint += (
                    f"\n**FORM-SEARCH ITERATION:** The navigation submitted a form at "
                    f"`{_nav_form_action}` with a `<select>` filter `{_select_name}`. "
                    f"This form REQUIRES a selection (returns 500 without one). To get "
                    f"ALL items (not just one filter value), set these template constants:\n"
                    f'  FORM_ACTION = "{_nav_form_action}"\n'
                    f'  FORM_METHOD = "{(navigation_analysis.get("search") or {}).get("form_method", "POST")}"\n'
                    f'  FORM_SELECT_NAME = "{_select_name}"\n'
                    f"  FORM_BASE_URL = the form page URL (where the <select> lives)\n"
                    f"The template's Phase 1 will iterate through ALL options in that "
                    f"<select>, submit once per option, paginate, and deduplicate.\n"
                )
        _sa = state.get("site_analysis") or {}
        _ab = _sa.get("anti_bot")
        _conn = _sa.get("connectivity")
        _method = _conn.get("method_that_worked", "") if isinstance(_conn, dict) else ""
        _nav_anti_bot = (isinstance(_ab, dict) and bool(_ab.get("detected"))) or _method.startswith("uc_chrome")
        if _nav_anti_bot:
            if mechanism == "http_navigation":
                template_hint += (
                    "\n**STEALTH:** Anti-bot/Akamai detected. Pass "
                    "`stealth: \"cloak\"` in every /navigate payload (set "
                    "`STEALTH = \"cloak\"` at the top of the scraper). The "
                    "/navigate server applies CloakBrowser server-side — do NOT "
                    "import playwright or set the STEALTH_BROWSER env var. Use "
                    "the `http_navigation` strategy.\n"
                )
            else:
                template_hint += (
                    "\n**STEALTH:** Anti-bot/Akamai detected. Stealth is AUTOMATIC at "
                    "runtime — browser_service wraps Playwright launch() to drive "
                    "CloakBrowser's stealth Chromium when STEALTH_BROWSER=cloak is set. "
                    "**Use a normal `p.chromium.launch()`** — do NOT use UC/Selenium, do "
                    "NOT call cloakbrowser.launch() directly (it conflicts with "
                    "sync_playwright). Use the `playwright` strategy.\n"
                )

    # T3.11: code_writer-only ≤2KB platform distillation. The full-skill
    # injection was disabled (each skill ~25KB blew the writer's truncation
    # budget and looped it); this deterministic digest carries the four things
    # the writer keeps needing — platform, item-URL shape, discovery/pagination
    # mechanics, field headings — straight from the analysis artifacts.
    skills_section = _platform_distillation(state)

    # CLI contract tail — rendered from the SAME constants the deterministic
    # guard consumes (agents.constants.required_cli_flags), so the prompt and
    # the guard can never drift apart. Strategy-aware (api family has no
    # listing page; ssr_div_list declares a reduced set).
    from .constants import required_cli_flags

    _cw_input_mode = (state.get("input_mode") or "").strip().lower()
    _cw_strategy = ""
    _cw_sa = state.get("scraper_analysis")
    if isinstance(_cw_sa, dict):
        _cw_strategy = (_cw_sa.get("strategy") or "").strip().lower()
    _cw_flags = required_cli_flags(_cw_input_mode, _cw_strategy)
    _discovery_flags = [
        f for f in _cw_flags
        if f not in ("--input", "--urls", "--sample", "--limit")
    ]
    _cli_contract_tail = ""
    if _discovery_flags:
        _flag_list = ", ".join(f"`{f}`" for f in _discovery_flags)
        _cli_contract_tail = (
            f"- **Discovery flags for this {_cw_input_mode} job (HARD "
            f"CONTRACT):** {_flag_list} — copy the add_argument lines verbatim "
            "from the template's main()\n"
            "- `main()` MUST contain "
            '`_env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()` '
            "BEFORE the seed-file/checkpoint gate, wired into the discovery "
            "branch exactly as the template does — execution sets this env var, "
            "not a CLI flag\n"
        )

    content = (
        "## OBJECTIVE\n"
        f"Build a **scraper** for {url}. "
        + (
            "The scraper discovers content via site navigation and extracts data from each page."
            if navigation_section
            else "The scraper reads URLs from `input_urls.json` (in its own directory) and extracts data from each page."
        )
        + "\n\n"
        f"{content_type_context}"
        f"## Your Task: Write the Scraper\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Sample URL:** {product_url}\n"
        f"**Scraping mechanism:** {mechanism or 'auto-detect'}\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Scraper analysis:** workspace/{slug}/scraper_analysis.json\n"
        f"**Save scraper to:** workspace/{slug}/scraper_draft.py\n"
        f"**Save input URLs to:** workspace/{slug}/input_urls.json"
        f"{provided_urls_section}"
        f"{retry_section}{human_feedback_section}{skills_section}{algolia_section}{navigation_section}{template_hint}{scraper_analysis_section}\n\n"
        f"### Full Extraction (MANDATORY — no item caps)\n"
        f"The scraper MUST extract EVERY item the source exposes — never cap the count.\n"
        f"- Do NOT set an arbitrary `MAX_PAGES` / `MAX_ITEMS` limit. Paginate until exhaustion "
        f"(a page returns fewer items than the page size) OR until the API's reported total "
        f"(`totalCount`/`count`/`total`) is reached.\n"
        f"- Set the page size to the source's MAX (often 100-500; e.g. Aya=500, AMN=100). "
        f"Larger pages = fewer requests = faster full extraction.\n"
        f"- **Concurrency:** if Phase 2 (per-detail-page extraction) is HTTP-based "
        f"(`requests`/`httpx`, not browser), extract items concurrently with a "
        f"`ThreadPoolExecutor(max_workers=8)` and a thread-local `requests.Session` "
        f"(the Session is NOT thread-safe to share). This turns thousands of sequential "
        f"2s fetches into minutes (e.g. 1300 jobs in ~7min vs ~54min). Preserve order by "
        f"indexing results to the discovery position.\n"
        f"- The default (no-args) run must do the FULL extraction. `--sample`/`--limit` are "
        f"only for quick tests; never make them the default behavior.\n\n"
        f"### Architecture\n"
    )
    if navigation_section:
        content += (
            "The scraper has TWO phases:\n"
            "**Phase 1: Navigate and discover item URLs.** "
            "Use the search/category/pagination patterns from navigation_analysis.json. "
            "Collect all item page URLs into a list.\n"
            "**Phase 2: Scrape each discovered URL.** "
            "Extract field data from each item page and map to output fields.\n"
            "Write results to `output_{datetime}.json`.\n\n"
            "### DO NOT (Navigation Scraper)\n"
            "- Hardcode URLs — discover them dynamically using the navigation patterns\n"
            "- Skip pagination — scrape ALL pages up to max_pages\n"
            "- Use input_urls.json — the scraper discovers its own URLs\n"
            "- Deviate from the navigation_analysis.json patterns\n"
            "- Add a fallback to read input_urls.json in the default (no-args) branch — "
            "the scraper MUST default to Phase 1 search discovery (using `--query` or the "
            "DEFAULT_QUERY constant), NEVER fall back to input_urls.json\n\n"
        )
    else:
        content += (
            f"The scraper reads URLs from `input_urls.json` in SCRIPT_DIR and "
            f"extracts data from EACH page. For each URL:\n"
            f"1. Extract ID/codes from the URL\n"
            f"2. Fetch data (via API lookup, HTTP request, or page scrape)\n"
            f"3. Map raw data to output fields\n"
            f"4. Write results to `output_{{datetime}}.json`\n\n"
            f"### DO NOT\n"
            f"- Add 'discover mode', catalog crawling, or site-wide discovery\n"
            f"- Add pagination logic (items come from input_urls.json)\n"
            f"- Add bulk extraction via APIs (Algolia partitioning, etc.)\n"
            f"- Add multiple modes of operation\n"
            f"- Use `Accept-Encoding: gzip, deflate, br` — use only `gzip, deflate` "
            f"(requests library may not support Brotli)\n"
            f"{_url_discovery_rules(site_input_urls)}\n"
            f"- Deviate from the template's input/output structure\n\n"
        )

    content += (
        "### Field Formatting Rules (CRITICAL)\n"
        '- **price**: Must include the currency symbol, e.g. `"$1,795.00"` not `"1,795.00"`\n'
        "- **src_url**: Set to the URL where the item was discovered. "
        "If input comes from input_urls.json, src_url equals the item URL. "
        "For navigation scrapers, src_url is the listing/search page URL.\n"
        '- **original_price**: Empty string `""` if not on sale, otherwise include '
        'currency symbol like `"$2,000.00"`\n'
        '- **availability**: Normalize to the exact tokens `"in_stock"` or '
        '`"out_of_stock"` (lowercase snake_case — pass schema.org '
        '`http://schema.org/InStock` URIs through the same normalizer)\n'
        '- **currency**: ISO 4217 code e.g. `"USD"`, `"EUR"`\n\n'
        "### Soft 404 Detection (CRITICAL)\n"
        "Many e-commerce sites return HTTP 200 for deleted/expired products but show "
        "'Product Not Found', 'No Longer Available', or redirect to a search page.\n\n"
        "Your scraper MUST detect these cases and set the `remarks` field:\n"
        "- Check if JSON-LD contains a Product type — if not, likely not a product page\n"
        "- Check if the page title or H1 contains 'not found', 'unavailable', "
        "'discontinued', 'no longer available'\n"
        "- Check if the final URL after redirects differs from the requested product URL\n"
        "- When detected, set `remarks` to a description like "
        "'Soft 404: product not found' and leave title/price empty — "
        "do NOT extract data from a non-product page.\n\n"
        "### Image Extraction Rules (CRITICAL)\n"
        "Product images must be scoped to the PRODUCT GALLERY only. Never capture:\n"
        "- Navigation banners, header images, or hero images\n"
        "- Recommended/related product thumbnails\n"
        "- Emoji, icon, flag, or badge images\n"
        "- Logo or brand images\n\n"
        "To achieve this:\n"
        "- Scope image selectors to the product gallery container "
        "(e.g. [data-auto-id='product-image'], .product-gallery, "
        "#pdp-gallery, [data-testid*='gallery'])\n"
        "- Filter collected images by product SKU/code in the src URL\n"
        "- Skip images with URLs containing /brand.assets/, /emoji/, "
        "/flags/, /icon/, or /navigation/\n"
        "- Skip images where the src URL path has no product identifier\n"
        "- A typical product page should have 3-15 images, NOT 100+\n\n"
        "### Required CLI Arguments (HARD CONTRACT — machine-checked)\n"
        "The scraper's argparse MUST declare every flag below with these EXACT "
        "spellings. Execution passes them verbatim; a flag your argparse does not "
        "declare is STRIPPED at launch and discovery silently falls back to "
        "input_urls.json (the seed file) — output collapses to the seed count.\n"
        "- `--input FILE` — Path to input URLs JSON file\n"
        "  **CRITICAL: `--input` MUST take precedence over any checkpoint file.** "
        "Check `args.input` BEFORE `_load_checkpoint()`; if `--input` is set, skip "
        "the checkpoint load entirely.\n"
        "- `--urls URL [URL ...]` — Product URLs as CLI arguments\n"
        "- `--sample` — Scrape only 5 products (action='store_true')\n"
        "- `--limit N` — Max products to scrape (type=int)\n"
        f"{_cli_contract_tail}"
        "- Do NOT rename, merge, or delete any flag the template's main() declares "
        "(`--query`→`--search`, `--listing-url`→`--start-url` both break execution). "
        "Keep the template's add_argument block; ADD to it, never replace it.\n\n"
        f"**CRITICAL: You MUST call write_file to save the scraper to "
        f"workspace/{slug}/scraper_draft.py"
        f"{' AND call write_file to save input URLs to workspace/' + slug + '/input_urls.json' if not site_input_urls and not navigation_section else ''}. "
        f"Do NOT just print code.**"
    )

    # Inject COMPLETE summaries of the analysis JSONs so code_writer does NOT
    # read_file them. The full files (product_analysis 20K+, navigation_analysis
    # 8K, scraper_analysis 4K) bloat the conversation past the context budget and
    # thrash truncation — these summaries carry every extraction method/selector
    # and discovery mechanic losslessly at ~10x smaller. [code_writer bloat]
    # Schema enforcement: narrow the field map to the requested schema so the
    # generated scraper only extracts what the user asked for.
    try:
        from src.content_types import resolve_allowed_fields

        _cw_allowed = resolve_allowed_fields(
            state.get("target_fields") or [], state.get("output_schema") or {}
        )
    except Exception:
        _cw_allowed = None
    _pa_raw = state.get("product_analysis") or {}
    if _prior_count_line:
        content = content + _prior_count_line
    pa_summary = _summarize_product_analysis(
        _pa_raw, allowed=_cw_allowed, scraper_analysis=scraper_analysis
    )
    if pa_summary:
        pa_summary += (
            "\n### DO NOT read_file the analysis JSONs\n"
            "The field-extraction map and navigation mechanics above are COMPLETE. "
            "Do NOT call read_file on product_analysis.json, navigation_analysis.json, "
            "or scraper_analysis.json — re-reading them bloats context and slows you "
                "down. The template (templates/*.py) is the only file you need to read.\n"
            )
        content = content + pa_summary
    elif _pa_raw:
        # Non-empty analysis the summarizer couldn't render (unknown shape or
        # partially-salvaged after corruption — job 10 lesson): say so LOUDLY.
        # Silent "" removed both the field map AND this verify instruction.
        content += (
            "\n### PRODUCT ANALYSIS PRESENT BUT UNREADABLE — VERIFY, DON'T GUESS\n"
            "The product_analysis for this site could not be summarized (unknown or "
            "damaged shape). You have NO reliable field-extraction map. Before writing "
            "extraction code, run the page's real network/API behavior yourself "
            "(run_scraper on a --sample, or inspect the page via the browser tool), "
            "confirm each requested field's actual source, and prefer whatever the "
            "site's live data shows over any single page's embedded markup. If the "
            "site has a backend products API, FIND IT (check XHR/fetch in the browser) "
            "— do not conclude 'there is no API' from one page's HTML alone.\n"
        )
    _ckpt_section = _checkpoint_discovery_section(state)
    if _ckpt_section:
        content = content + _ckpt_section
    _user_req = _user_requirements_section(state)
    if _user_req:
        content = _user_req + content
    _nested = _nested_schema_section(state)
    if _nested:
        content = _nested + content
    # Reinforce template-fidelity for discovery/pagination (prevents execution-time
    # crashes that --sample testing can't see — e.g. session.url phantom attributes).
    content = (
        "\n### Template fidelity — discovery & pagination\n"
        "Do NOT re-signature or redefine the template's discovery/pagination helpers "
        "(`_get_next_page_url`, `_discover_urls_via_*`, `_fetch_html`, checkpoint "
        "load/save). Call them exactly as the template does. The template's main() "
        "argparse block and its "
        '`_env_listing = os.environ.get("SCRAPER_LISTING_URL", "")` read are '
        "CONTRACT, not boilerplate — copy both verbatim; a renamed or deleted flag "
        "passes `--sample` testing and silently zeroes the real run. NEVER reference attributes "
        "that don't exist on an object — `requests.Session`/`httpx.Client` have NO `.url`; "
        "capture the current URL from the response (`final_url = str(resp.url)`) and pass "
        "the string. A scratch run exercises Phase 1 discovery end-to-end; a wrong call "
        "there crashes the job at execution even though --sample passed.\n"
    ) + content
    return [HumanMessage(content=content)]


def build_code_tester_message(state: dict) -> list:
    """Build the initial HumanMessage for the code-tester agent."""
    slug = state.get("site_slug", "unknown")

    retry_context = ""
    retry_count = state.get("test_retry_count", 0)
    if retry_count == FINAL_RETRY_SENTINEL:
        retry_context = (
            f"\n### FINAL RETEST MODE (User-Initiated Final Retry)\n"
            f"This is the FINAL test attempt based on user feedback. "
            f"If the scraper does not pass this test, the job will end.\n"
            f"Read the previous test report at: workspace/{slug}/test_report.json\n\n"
        )
        human_feedback = state.get("human_feedback", "")
        if human_feedback:
            retry_context += (
                f"### User-Flagged Issue (CRITICAL — verify this is fixed)\n"
                f"The user specifically reported:\n"
                f"> {human_feedback}\n\n"
                f"Your validation MUST specifically check this issue. "
                f"If it is NOT resolved, this is a FAIL regardless of other "
                f"field quality. Do NOT give the scraper credit for fixing "
                f"this issue unless the output JSON demonstrably shows the "
                f"correct value.\n\n"
            )
    elif retry_count > 0:
        retry_context = (
            f"\n### RETEST MODE (Cycle {retry_count + 1})\n"
            f"The scraper was modified after previous test failures. "
            f"Focus your validation on the fields that previously failed. "
            f"Read the previous test report at: workspace/{slug}/test_report.json\n\n"
        )

    input_mode = state.get("input_mode", "url_list")
    search_criteria = state.get("search_criteria", "")
    # Phase-1 test invocation, PRE-COMPUTED from the draft's own declared flags
    # (critique v1 vector 5): run_scraper does NOT strip undeclared flags, so a
    # hardcoded arg list would manufacture argparse exit-2 failures execution
    # never sees (execution filters first). Only flags the draft declares are
    # passed; --limit stays (http_navigation's --discover-only runs Phase 1 to
    # exhaustion vs a 300s deadline — the cap keeps the probe bounded).
    _tester_phase1_instruction = " `run_scraper(args=['--discover-only','--limit','50'])`."
    try:
        import os as _os

        _draft_p = _os.path.join(
            _os.environ.get("PROJECT_ROOT", "/app"),
            "workspace", slug, "scraper_draft.py",
        )
        if _os.path.isfile(_draft_p):
            from .nodes.run_execution import _accepted_cli_flags

            _accepted = _accepted_cli_flags(_draft_p) or set()
            _p1_flags = []
            if input_mode == "search_term" and "query" in _accepted and search_criteria:
                _p1_flags = ["--query", search_criteria]
            elif "listing-url" in _accepted:
                _listing = (
                    ((state.get("navigation_analysis") or {}).get("discovery") or {})
                    .get("listing_url")
                    or ""
                )
                _p1_flags = ["--listing-url", _listing] if _listing else []
            if "fresh-discovery" in _accepted:
                _p1_flags.append("--fresh-discovery")
            if "discover-only" in _accepted:
                _p1_flags.append("--discover-only")
            _has_discovery_flag = bool(
                {"fresh-discovery", "discover-only", "listing-url", "query"}
                & _accepted
            )
            if "limit" in _accepted:
                _p1_flags += ["--limit", "50"]
            if _has_discovery_flag and _p1_flags:
                _args_rendered = ",".join(
                    repr(f) for f in _p1_flags
                )
                _tester_phase1_instruction = (
                    f" `run_scraper(args=[{_args_rendered}])`."
                )
            else:
                _tester_phase1_instruction = (
                    " the draft declares NO discovery flags (no --listing-url/"
                    "--query/--fresh-discovery/--discover-only) — report that "
                    "as a HIGH issue prefixed `CLI CONTRACT VIOLATION:` "
                    "(discovery cannot run at execution either)."
                )
    except Exception:
        pass  # fall back to the generic instruction above
    # [job-88 selfridges] The volume assertion below must carry the SAME scope
    # waiver as the deterministic gate (_volume_gap bails for firstn/filter or
    # any scope_value). This job was firstn/10: its requests draft discovered 8
    # real URLs and extracted 5/5 correct products — ~80% of the user's actual
    # ask — but the LLM applied the unbounded "> 1 page worth" bar, flagged it
    # NEEDS_FIXES, and the writer responded by switching to a browser template
    # that discovered 0. The cascade destroyed a working scraper.
    _scope_l = str(state.get("scope") or "").strip().lower()
    if _scope_l in ("firstn", "filter") or str(state.get("scope_value") or "").strip():
        _tester_volume_rule = (
            " BOUNDED SCOPE: this job runs under a firstn/filter scope, so a "
            "small genuine yield that covers the scope is CORRECT — do NOT "
            "flag sub-page-size discovery as a volume defect (the "
            "deterministic gate waives it for bounded scopes too)."
        )
    else:
        _tester_volume_rule = ""
    nav_validation = ""
    if input_mode in ("navigation", "list_page", "search_term"):
        nav_analysis = state.get("navigation_analysis") or {}
        discovery = nav_analysis.get("discovery_method", "")
        nav_validation = (
            f"\n### Navigation Job Validation (input_mode={input_mode})\n"
            f"This is a navigation job — the scraper discovers products via search/category.\n"
        )
        if discovery == "search":
            nav_validation += (
                "- **Phase 1 MUST start from the search/listing URL in navigation_analysis.json** — "
                "not from a guessed category page\n"
                "- Validate that discovered URLs are PRODUCT pages, not category pages\n"
            )
        nav_validation += (
            f"- `--sample --query \"{search_criteria}\"` — use these exact args so Phase 1 discovery runs.\n"
            f"- Do NOT run with only `--sample` — the scraper will fall back to input_urls.json "
            f"instead of discovering products via search\n"
            f"- A FAIL is expected if Phase 1 discovers category/landing page URLs instead of product URLs\n"
            f"- This is a navigation scraper — input_urls.json is NOT used. "
            f"Products come from the scraper's own discovery.\n"
        )

        # Coverage-target context (Tier 2/3 inputs). The orchestrator stamps
        # `source` deterministically — see src/discovery_coverage.py and
        # navigate_synthesize._build_coverage_target. Treat a None/[] target as
        # "Tier 1 only" — no ratio or dimension demand applies.
        cov_target = nav_analysis.get("coverage_target") or {}
        cov_total = cov_target.get("total_items")
        cov_source = cov_target.get("source", "unknown")
        cov_dims = cov_target.get("dimensions") or []
        cov_raw = cov_target.get("raw_count_string")
        if cov_total is not None and cov_source == "site_reported":
            dims_repr = ", ".join(f"{d['name']}={d['count']}" for d in cov_dims) or "none"
            nav_validation += (
                "\n### Coverage Target (Tier 3 active — site-reported total)\n"
                f"The site reports a trusted item total of **{cov_total}** "
                f"(raw: `{cov_raw!r}`, source: `{cov_source}`, dimensions: {dims_repr}).\n"
                "- This activates the Tier 3 ratio gate. If Phase 1 discovers "
                f"far fewer than {cov_total} URLs (e.g. tens vs thousands), "
                "treat it as a HIGH severity coverage gap and set "
                "`target: \"strategy\"` with reason `discovery incomplete: "
                "ratio <threshold>` — the scraper is giving up, not exhausting.\n"
                "- The Phase 1 probe in this test run is capped; you may not "
                "see the full ratio manifest here. If you see `stop_reason: "
                "navigate_error` OR clearly partial discovery (e.g. "
                f"<10% of {cov_total}), flag it.\n"
            )
        elif cov_dims:
            dims_repr = ", ".join(f"{d['name']}={d['count']}" for d in cov_dims)
            nav_validation += (
                "\n### Coverage Target (Tier 2 active — deterministic dimensions)\n"
                f"Deterministic dimensions captured: {dims_repr} "
                f"(source: `{cov_source}`). No site-reported total — Tier 3 "
                "is a no-op.\n"
                "- If the scraper iterates only ONE value of a dimension that "
                f"has {cov_dims[0]['count']}+ options (e.g. 1 specialty of "
                "207), that is a HIGH severity coverage gap → set "
                "`target: \"strategy\"` with reason `dimensions 1/"
                f"{cov_dims[0]['count']}`.\n"
            )
        else:
            nav_validation += (
                "\n### Coverage Target (Tier 1 only)\n"
                "No site-reported total and no deterministic dimensions "
                f"(source: `{cov_source}`, total_items: None, dimensions: []).\n"
                "- Only Tier 1 (stop_reason) applies. A clean short page on a "
                "small catalog is a PASS. A `navigate_error` / blocked stop "
                "is a FAIL regardless of how many items came back.\n"
            )

    # Job-portal filter validation: verify date/location filtering was applied
    page_type = state.get("page_type", "")
    if page_type in ("job_navigation", "job_posting") or state.get("site_type") == "jobs":
        nav_analysis = state.get("navigation_analysis") or {}
        filters_info = nav_analysis.get("filters", {}) or {}
        if filters_info.get("has_filters"):
            nav_validation += (
                "\n### Job Filter Validation (REQUIRED)\n"
                "This job portal supports filtering. Verify the scraper applied it:\n"
                "- Every output item's `location` must match Alabama (or be empty if "
                "unparseable — never an out-of-state location)\n"
                "- Every output item's `posted_date` must be within the last 7 days "
                "(parse the date; reject items older than 7 days)\n"
                "- If items violate the date/location filter, this is a HIGH severity "
                "issue (filters not applied)\n"
                "- Confirm the `posted_date` field is populated for most items\n"
            )

    # Strategy constraint (Fix C): code_tester must not recommend switching strategies.
    strategy = (state.get("scraper_analysis") or {}).get("strategy", "") or (
        state.get("site_analysis") or {}
    ).get("scraping_mechanism", "")
    strategy_constraint = ""
    if strategy:
        strategy_constraint = (
            f"\n### Strategy Constraint (CRITICAL for remediation)\n"
            f"The scraping strategy was chosen upstream by scraper_analyzer as **{strategy}**. "
            f"Default: work WITHIN this strategy — if the scraper used the wrong selectors or "
            f"missed a field, say so with `target: \"mapping\"` or `target: \"scraper\"`.\n\n"
            f"EXCEPTION — access/strategy failure: if the scraper extracted ~0 items BECAUSE "
            f"the strategy itself can't reach the content (e.g. `http_requests`/`api` returned "
            f"empty/403/blocked, or `playwright` TIMED OUT trying to drive a heavy form), set "
            f"`target: \"strategy\"` and name the cause in `reason` (timeout / blocked / api-400 "
            f"/ http-empty). The pipeline will switch to a different strategy. Use `strategy` "
            f"ONLY for these access-class failures — never for a field-mapping or selector bug.\n"
        )
    # Crash capture (Fix A): record the raw traceback so code_writer can make a targeted fix.
    crash_capture = (
        "\n### If the Scraper CRASHED\n"
        "If `run_scraper` shows the scraper exited non-zero, raised an exception, or hit a "
        "syntax/argparse/import error (it never produced valid output), set "
        "`overall_assessment: \"CRASH\"` and put the EXACT stderr/traceback verbatim in a "
        "top-level `crash_error` field. A crash is a code bug — the pipeline routes it to a "
        "targeted bug fix, not a full rewrite, so the verbatim error is essential.\n"
    )

    content = (
        f"## OBJECTIVE\n"
        f"Validate the generated scraper for {slug}.\n\n"
        f"{retry_context}"
        f"## Your Task: Test the Scraper\n\n"
        f"**Scraper path:** workspace/{slug}/scraper_draft.py\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Save test report to:** workspace/{slug}/test_report.json\n\n"
        f"{nav_validation}"
        f"### Workflow\n"
        f"1. Run the scraper to validate BOTH phases:\n"
        f"   - **Phase 2 (field extraction):** `run_scraper(args=['--sample','--input','input_urls.json'])` "
        f"— fast field check against known URLs.\n"
        f"   - **Phase 1 (discovery) — for navigation/list_page/search_term jobs ONLY:** "
        f"run discovery EXACTLY as execution does, so a dropped CLI flag fails HERE instead "
        f"of in production:{_tester_phase1_instruction} "
        f"Assert `metadata.discovered_urls` > 1 page worth (e.g. > items_per_page) — if it "
        f"returns only ~1 page, pagination/discovery is broken (HIGH severity)."
        f"{_tester_volume_rule} If argparse "
        f"rejects a flag (exit 2, `unrecognized arguments: ...`), that is a HIGH severity "
        f"issue whose problem text MUST start with `CLI CONTRACT VIOLATION:` and name the "
        f"missing flags — set `target: \"scraper\"`. For url_list jobs, skip this (no Phase 1).\n"
        f"2. Read `workspace/{slug}/product_analysis.json` for field expectations (1 call)\n"
        f"3. Read the output JSON file(s) that run_scraper produced (1 call)\n"
        f"4. Write test_report.json — include `phases_tested: {{phase1_discovery: <bool>, "
        f"phase2_extraction: <bool>}}` so routing knows whether discovery was validated. "
        f"Set each phase TRUE only if you actually ran it; a false phase1 with real "
        f"findings is better than a defaulted true.\n\n"
        f"**IMPORTANT: Do NOT read scraper_draft.py.** Assess the scraper ONLY by its output. "
        f"The code contains post-generation patches (robust overrides, output filters) that are correct by design. "
        f"Judge solely by whether the output JSON has correctly-populated product fields.\n\n"
        f"### Validation Method\n"
        f"Compare scraper output against `product_analysis.json > fields > {{field}} > expectations`. "
        f"Each field has a validation contract (type, required, min_length, should_not_match, "
        f"sample_values, known_bad_values, format_hint). Do NOT re-fetch live pages.\n\n"
        f"### IMPORTANT: Non-Product URLs Are Expected\n"
        f"The scraper uses BROAD link discovery (captures all same-domain links). Some discovered "
        f"URLs will be category/nav/non-item pages — the scraper's output filter removes items "
        f"without substantive data. **This is correct behavior, not a failure.** Judge the scraper "
        f"ONLY by items with real, substantive data (NOT just 'title' or 'price' — this site may "
        f"be a people directory, job board, or article archive whose fields are Name/email/phone, "
        f"company/location, or author/content). If the real items have correctly populated core "
        f"fields for THIS content type, it's a PASS. A sample that yields 3+ items with good "
        f"substantive data is a PASS.\n\n"
        f"### BUDGET: 10 tool calls maximum.\n\n"
        f"### How Scraper Execution Works\n"
        f"The `run_scraper` tool automatically detects browser-based scrapers (Playwright, "
        f"SeleniumBase, etc.) and dispatches them to a remote `browser_service` container "
        f"that has Chrome + Xvfb + all browser libraries pre-installed. "
        f"HTTP-based scrapers run locally. You NEVER need to install packages.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify or fix the scraper — only report issues\n"
        f"- Do NOT re-fetch live product pages — validate against product_analysis expectations\n"
        f"- Do NOT run the scraper more than 2 times\n"
        f"- Do NOT install packages, run bash commands, or load skills\n"
        f"- Do NOT read input_urls.json — that file is not your concern\n"
        f"- **NEVER use `--urls <single_url>` — ALWAYS use `--input input_urls.json`** "
        f"for Phase 2 testing. Using --urls with a single URL tests only 1 item, which "
        f"will ALWAYS fail the >= 3-item ground-truth threshold and cause a false cascade.\n\n"
        f"### Dead URLs\n"
        f"Products with status_code in {sorted(DEAD_STATUS_CODES)} are dead URLs "
        f"— exclude from quality assessment. If ALL are dead, set PASS with confidence 1.0.\n\n"
        f"### Optional Fields\n"
        f"Fields `original_price` and `location` are optional — missing = severity low, never high.\n\n"
        f"### Remediation Recommendation (REQUIRED in test_report)\n"
        f"After assessing the scraper, add a top-level `remediation` object telling the pipeline "
        f"WHERE the fix should happen:\n"
        f"```json\n"
        f"\"remediation\": {{\n"
        f"  \"target\": \"mapping\" | \"scraper\" | \"strategy\",\n"
        f"  \"fields\": [\"price\"],\n"
        f"  \"reason\": \"...\"\n"
        f"}}\n"
        f"```\n"
        f"Decision rule — for each FAILED **required** field, compare the scraper output against "
        f"`product_analysis.json`'s mapping for that field:\n"
        f"- If the field's mapping is **missing / `tested: false` / `tested: \"empty\"` (the "
        f"live-render check found the source dead) / selector looks wrong or "
        f"unverified** → the root cause is the MAPPING → set `target: \"mapping\"` and list those "
        f"fields. The pipeline re-runs product_analyzer to fix the mapping (not just regenerate the "
        f"scraper with the same bad input).\n"
        f"- If the mapping looks correct (right selector/method, `tested: true`) but the scraper "
        f"didn't implement it → `target: \"scraper\"`.\n"
        f"- If the scraper extracted ~0 items because the STRATEGY can't access the content "
        f"(http/api empty or 403/blocked; playwright timed out) → `target: \"strategy\"` with the "
        f"cause in `reason` (timeout / blocked / api-400 / http-empty). The pipeline switches "
        f"strategy. Do NOT use `strategy` for selector or field-mapping bugs.\n"
        f"- If everything passes → `target: \"scraper\"` (no real remediation needed).\n"
        f"When unsure, default to `\"scraper\"`. Only list fields that are required AND failed.\n\n"
        f"{strategy_constraint}{crash_capture}"
        f"**CRITICAL: You MUST call write_file to save your test report to "
        f"workspace/{slug}/test_report.json as your LAST action.**"
    )
    return [HumanMessage(content=content)]


def build_cleanup_message(state: dict) -> list:
    """Build the initial HumanMessage for the cleanup agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")

    content = (
        f"## OBJECTIVE\n"
        f"Finalize the product scraper for {url}.\n\n"
        f"## Your Task: Cleanup\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Scraper draft:** workspace/{slug}/scraper_draft.py\n"
        f"**Site analysis:** workspace/{slug}/site_analysis.json\n"
        f"**Product analysis:** workspace/{slug}/product_analysis.json\n"
        f"**Target folder:** scrapers/{slug}/\n"
        f"**Save cleanup report to:** workspace/{slug}/cleanup_report.json\n\n"
        f"### Workflow\n"
        f"1. Use `run_bash` to copy files (NOT read_file — you don't need to read their contents):\n"
        f"   - `cp workspace/{slug}/input_urls.json scrapers/{slug}/input_urls.json` (if it exists)\n"
        f"   - `cp workspace/{slug}/output_*.json scrapers/{slug}/` (if any exist)\n"
        f"   - `cp -r workspace/{slug}/analysis scrapers/{slug}/analysis` (if it exists)\n"
        f"   - Do NOT copy scraper_draft.py → scraper.py yourself; the pipeline promotes the scraper deterministically (and only on success).\n"
        f"2. Use `search_files` to list what's in the workspace (NOT read_file).\n"
        f"3. write_file to save cleanup report (1 call).\n\n"
        f"### BUDGET: 10 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT use read_file on output_*.json files — they can be very large and will\n"
        f"  exceed the LLM's prompt limit. Use `search_files` to check they exist, then\n"
        f"  `run_bash` (`cp`) to copy them.\n"
        f"- Do NOT modify the scraper code\n"
        f"- Do NOT delete workspace analysis files (site_analysis.json, product_analysis.json, test_report.json)\n"
        f"- Do NOT run the scraper\n\n"
        f"**CRITICAL: You MUST call write_file to save your cleanup report to "
        f"workspace/{slug}/cleanup_report.json as your LAST action.**"
    )
    return [HumanMessage(content=content)]


def build_skill_learner_message(state: dict) -> list:
    """Build the initial HumanMessage for the skill-learner agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    platform = state.get("platform", "custom")

    nav_review_note = ""
    nav_report = state.get("nav_learning_report")
    if nav_report:
        updated = nav_report.get("skills_updated", [])
        nav_review_note = (
            f"\n### nav-skill-review already applied (do NOT duplicate)\n"
            f"The nav-skill-review agent ran during this pipeline and auto-applied "
            f"{len(updated)} navigation learnings. Read "
            f"`workspace/{slug}/nav_learning_report.json` for details. "
            f"Focus your analysis on NON-navigation learnings (product extraction, "
            f"code patterns, anti-bot) and skip anything already covered.\n"
        )

    content = (
        f"## OBJECTIVE\n"
        f"Capture reusable knowledge from the completed scrape of {url}.\n\n"
        f"## Your Task: Skill Learning\n\n"
        f"**Site URL:** {url}\n"
        f"**Site slug:** {slug}\n"
        f"**Detected platform:** {platform}\n"
        f"**Scraper:** scrapers/{slug}/scraper.py\n"
        f"**Save learning report to:** workspace/{slug}/learning_report.json\n"
        f"{nav_review_note}\n"
        f"### Workflow\n"
        f"1. Read the scraper and analysis files (2-4 calls):\n"
        f"   - scrapers/{slug}/scraper.py\n"
        f"   - workspace/{slug}/site_analysis.json\n"
        f"   - workspace/{slug}/product_analysis.json\n"
        f"   - workspace/{slug}/test_report.json (if present)\n"
        f"   - workspace/{slug}/navigation_findings.json (if present — raw nav data)\n"
        f"   - workspace/{slug}/nav_learning_report.json (if present — already-applied nav learnings)\n"
        f"2. Check existing skills in .opencode/skills/ (1-2 calls)\n"
        f"3. write_file to save learning report (1 call)\n\n"
        f"### BUDGET: 15 tool calls maximum.\n\n"
        f"### What NOT to Do\n"
        f"- Do NOT modify any skill files — only report proposals\n"
        f"- Do NOT modify the scraper\n"
        f"- Do NOT run anything\n"
        f"- Do NOT propose navigation learnings already applied by nav-skill-review\n\n"
        f"**CRITICAL: You MUST call write_file to save your learning report to "
        f"workspace/{slug}/learning_report.json as your LAST action.**"
    )
    return [HumanMessage(content=content)]


def build_dagster_converter_message(state: dict) -> list:
    """Build the message for the dagster_converter agent."""
    slug = state.get("site_slug", "unknown")
    url = state.get("url", "")
    site_name = state.get("site_name", slug)

    # Find the existing scraper path (production first, workspace fallback)
    scraper_path = f"scrapers/{slug}/scraper.py"
    workspace_scraper = f"workspace/{slug}/scraper_draft.py"

    content = (
        f"## OBJECTIVE\n"
        f"Convert the existing scraper for {url} into the client's `BaseTlsScraper` format.\n\n"
        f"## Files to Read\n"
        f"1. **Existing scraper**: `{scraper_path}` (or `{workspace_scraper}` if the production "
        f"version doesn't exist yet). Read the FULL scraper — understand Phase 1 (discovery) + "
        f"Phase 2 (extraction).\n"
        f"2. **Client template**: `templates/dagster_template.py` — the `BaseTlsScraper` class. "
        f"Understand `self._fetch()`, `scrape_one(url)`, `discover_urls()`.\n"
        f"3. **Field mappings**: `workspace/{slug}/product_analysis.json` — which selectors/methods "
        f"extract each field.\n"
        f"4. **Navigation**: `workspace/{slug}/navigation_analysis.json` — filter dimensions, URL "
        f"patterns, option lists (for `discover_urls`).\n"
        f"5. **Site info**: `workspace/{slug}/site_analysis.json` — platform, scraping method.\n\n"
        f"## What to Generate\n"
        f"Write `workspace/{slug}/{slug}_dagster.py` — a `BaseTlsScraper` subclass with:\n"
        f"- `discover_urls(self) -> list[str]`: Phase 1 — iterate filter combinations from "
        f"navigation_analysis, build listing URLs, fetch each, extract + dedup item URLs.\n"
        f"- `scrape_one(self, url: str) -> dict`: Phase 2 — fetch one item page + parse fields "
        f"(using the SAME selectors/logic as the original scraper).\n"
        f"- Helper methods as needed (copied/adapted from the original).\n"
        f"- All required imports (tls_client, requests, BeautifulSoup, re, json, etc.).\n\n"
        f"## Strategy Adaptation\n"
        f"- `http_requests`: use `self._fetch()` for both phases + BeautifulSoup/regex parse.\n"
        f"- `playwright`: use browser_service `/render` endpoint or Playwright internally "
        f"(keep the `BaseTlsScraper` interface; change the implementation).\n"
        f"- `internal_api`: convert API calls to `self._fetch()` or `requests.get()`.\n\n"
        f"## Rules\n"
        f"- PRESERVE the parsing logic faithfully (same selectors, same field names).\n"
        f"- Keep the class extending `BaseTlsScraper` + `scrape_one(self, url) -> dict`.\n"
        f"- Include `discover_urls()` for navigation jobs; return `[]` for url_list jobs.\n"
        f"- Dedup by job/item ID in `discover_urls()`.\n"
        f"- Syntax-check before writing (valid Python).\n"
        f"- Write to `workspace/{slug}/{slug}_dagster.py`.\n"
    )
    return [HumanMessage(content=content)]


__all__ = [
    "create_site_analyzer",
    "create_product_analyzer",
    "create_code_writer",
    "create_code_tester",
    "create_cleanup_agent",
    "create_skill_learner",
    "create_dagster_converter",
    "create_nav_skill_review",
    "build_site_analyzer_message",
    "build_product_analyzer_message",
    "build_code_writer_message",
    "build_code_tester_message",
    "build_cleanup_message",
    "build_skill_learner_message",
    "build_nav_skill_review_message",
    "build_dagster_converter_message",
]
