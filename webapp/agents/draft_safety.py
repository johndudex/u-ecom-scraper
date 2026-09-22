"""Draft-safety helpers shared by the code_writer / code_tester boundaries.

[wave-13 B1] Three facts about ``scraper_draft.py`` motivated this module:

1. The draft can go missing between the writer's write and the tester's read
   (watchdog re-drive, ephemeral-volume recycle, a wipe racing a cycle) — the
   tester then burns its cascade crashing on an absent file (58% of the
   campaign's test verdicts were missing-draft cycles).
2. A writer invocation that died mid-reply often LEFT the scraper in its last
   assistant message as a fenced code block, with no ``write_file`` call —
   the bytes were delivered, only the write never happened.
3. Every draft boundary check (isfile + ast.parse) and every per-job FM
   restore was previously re-implemented inline in 2-3 places.
"""

import ast
import logging
import os
import re

logger = logging.getLogger(__name__)

# Upper bound on a fence-extracted draft (pathological-payload guard).
MAX_EXTRACTED_DRAFT_BYTES = 400_000

DRAFT_FILENAME = "scraper_draft.py"

# [wave-19 T1.1] Strategies whose drafts fetch with a real browser — the
# HTTP ladder does not apply to them, so the gate exempts them outright.
# (http_navigation is deliberately NOT exempt: its template carries the
# shared ladder for SSR/form-search fallbacks.)
LADDER_EXEMPT_STRATEGIES = frozenset({
    "playwright", "stealth_browser", "seleniumbase_uc", "undetected_chromedriver",
})

# [wave-19 T1.1] Calls that prove a draft carries the proxy-aware fetch
# machinery. ``DIRECT_ONLY_TIERS`` is deliberately absent — job 324's draft
# defined it and never consumed it (a dead marker, not wiring).
_LADDER_CALLEES = frozenset({
    "create_fetch_page",
    "create_fetch_text",
    "create_fetch_json",
    "create_fetch_html",
    "get_escalation_tier",
    "get_proxy_dict",
})


def draft_path_for(root: str, slug: str) -> str:
    return os.path.join(root, "workspace", slug, DRAFT_FILENAME)


# [wave-32 D3] The per-job FM archive gains a KNOWN-GOOD twin:
# ``scraper-draft-{job_id}-good.py`` freezes the last PARSEABLE draft, so a
# later broken snapshot (587's SyntaxError draft) can never destroy the last
# restorable copy. Both restore paths prefer it when the latest does not parse.
GOOD_SUFFIX = "-good.py"


def draft_good_key(slug: str, job_id) -> str:
    """FM key of the job's known-good draft freeze."""
    import src.artifacts as artifacts

    return artifacts.scrapers_key(slug, "jobs", f"scraper-draft-{job_id}{GOOD_SUFFIX}")


def payload_parses(payload: bytes) -> bool:
    """Does this archived draft content parse as Python?"""
    try:
        ast.parse(bytes(payload).decode("utf-8", errors="replace"))
        return True
    except (SyntaxError, ValueError):
        return False


def freeze_good_draft(root: str, slug: str, job_id) -> bool:
    """Snapshot the CURRENT workspace draft to the known-good FM key.

    A freeze is by definition a parseable draft — an unparseable one is
    refused (return False) and never overwrites an earlier good freeze.
    """
    if not slug or not job_id:
        return False
    target = draft_path_for(root, slug)
    if not draft_parses(target):
        return False
    try:
        import src.artifacts as artifacts

        artifacts.write(draft_good_key(slug, job_id), open(target, "rb").read())
        return True
    except Exception as exc:
        logger.warning(
            "draft_safety: known-good freeze failed (job %s): %s", job_id, exc
        )
        return False


def draft_parses(path: str) -> bool:
    """Is ``path`` an existing, parseable Python file? (the draft floor)"""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            ast.parse(fh.read(), filename=path)
        return True
    except Exception:
        return False


def extract_fenced_python(text: str) -> str | None:
    """Return the LARGEST ```python fenced block from ``text`` that parses.

    Deterministic (no LLM): a dead writer invocation frequently delivered the
    complete scraper as a prose fence without ever calling write_file — the
    fix is to read the bytes back out of the message it already sent.

    Only ``python``-tagged fences are candidates (a bare ``` fence is prose
    more often than code). Each candidate must ast.parse — a truncated fence
    (the other failure mode) is rejected rather than half-shipped. Returns
    None when nothing usable is found.
    """
    if not text:
        return None
    blocks: list[str] = []
    for marker in ("```python", "```Python", "```py"):
        start = 0
        while True:
            i = text.find(marker, start)
            if i < 0:
                break
            j = text.find("```", i + len(marker))
            if j < 0:
                # Unterminated fence — the invocation died mid-fence. Take
                # what's there; the ast.parse gate below rejects truncation.
                blocks.append(text[i + len(marker):])
                break
            blocks.append(text[i + len(marker):j])
            start = j + 3
    best: str | None = None
    for block in blocks:
        code = block.strip("\n")
        if not code or len(code) > MAX_EXTRACTED_DRAFT_BYTES:
            continue
        try:
            ast.parse(code)
        except Exception:
            continue
        if best is None or len(code) > len(best):
            best = code
    return best


def ladder_preservation_violation(
    scraper_path: str, input_mode: str, strategy: str = ""
) -> str | None:
    """AST check: a nav-mode HTTP-family draft must keep a proxy-aware fetch
    path. [wave-19 T1.1 — the wall against the 324 draft class]

    Job 324 (myhouse): the writer shipped an api-family draft whose custom
    ``_http_get`` checked ``proxy_config.is_banned()`` but had NO ``proxies=``
    kwarg and NO shared-ladder import — structurally unproxied. It tested
    green (the probe URL answered direct that hour) and execution zeroed the
    moment the site throttled the direct egress IP.

    Satisfied by ANY of:
      L1  ``import src.http_fetch`` / ``from src.http_fetch import ...``
      L2  any call carrying a ``proxies=`` keyword
      L3  a shared-ladder call (``create_fetch_*`` / ``get_escalation_tier``
          / ``get_proxy_dict``)

    Absent all three → a violation description for the writer fix directive
    and the run_execution refusal. Never blocks an unparseable draft (the
    syntax fixer owns those) or a missing file, and browser-only strategies
    are exempt (their fetching rides a real browser).
    """
    from .constants import NAV_INPUT_MODES

    im = (input_mode or "").strip().lower()
    if im not in NAV_INPUT_MODES:
        return None
    if not scraper_path or not os.path.isfile(scraper_path):
        return None
    if (strategy or "").strip().lower() in LADDER_EXEMPT_STRATEGIES:
        return None
    try:
        with open(scraper_path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=scraper_path)
    except Exception:
        return None  # unparseable → the syntax fixer owns it

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "src.http_fetch":
            return None  # L1
        if isinstance(node, ast.Import):
            if any(alias.name == "src.http_fetch" for alias in node.names):
                return None  # L1
        if isinstance(node, ast.Call):
            if any(kw.arg == "proxies" for kw in node.keywords):
                return None  # L2
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in _LADDER_CALLEES:
                return None  # L3

    return (
        "LADDER PRESERVATION VIOLATION "
        f"(input_mode={im}, strategy={strategy or 'unknown'}): the draft has "
        "no proxy-aware fetch path — no src.http_fetch import, no proxies= "
        "keyword on any HTTP call, and no shared-ladder call "
        "(create_fetch_*/get_escalation_tier/get_proxy_dict). Need: fetch "
        "through the shared ladder (from src.http_fetch import "
        "create_fetch_json / create_fetch_text) exactly as your template "
        "does, or pass proxies=proxy_config.get_proxy_dict(tier) on your "
        "requests calls. A structurally unproxied draft tests green until "
        "the site throttles the direct egress IP, then execution discovers "
        "zero items (job 324 myhouse). DIRECT_ONLY_TIERS-style tier lists "
        "are not wiring."
    )


# [wave-37 W37-NEW-D] The repair rewrites bare ``requests.<m>(...)`` call
# sites — the only shape that can be rewritten BOTH safely (a dict ``.get``
# must never gain a proxies kwarg) and honestly (the ACTUAL fetches become
# proxied, not a dead marker that merely satisfies the gate).
_LADDER_REWRITE_METHODS = frozenset({
    "get", "post", "head", "put", "delete", "options", "request",
})
# get_proxy_dict's own default tier — the base of every escalation ladder.
_LADDER_REPAIR_PROXIES_EXPR = "_W37_PROXY_CONFIG.get_proxy_dict('datacenter')"
_LADDER_REPAIR_PREAMBLE = (
    "from src.proxy import ProxyConfig  # [wave-37 W37-NEW-D] re-injected\n"
    "_W37_PROXY_CONFIG = ProxyConfig.get_instance()\n\n"
)


def _ladder_gate_on_text(text: str, strategy: str) -> str | None:
    """Run the REAL ladder gate over source text (temp-file round-trip).

    ``list_page`` is the nav-mode applicability key — the gate's checks are
    mode-independent beyond that. Reusing the gate (vs re-implementing L1-L3)
    means repair and veto can never drift apart.
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return ladder_preservation_violation(path, "list_page", strategy)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def repair_ladder_violation(text: str, strategy: str) -> str | None:
    """[wave-37 W37-NEW-D] Deterministic ladder re-injection; repaired source
    or None when the draft is unrepairable.

    Rewrites every bare ``requests.<method>(...)`` call to carry a resolved
    ``proxies=`` kwarg (``ProxyConfig.get_proxy_dict('datacenter')``) and
    prepends the ProxyConfig wiring. Honesty guards, each returning None so
    the caller keeps today's honest-fail:
      - draft unparseable (the syntax fixer owns those);
      - the gate is ALREADY satisfied (exempt strategy / non-nav mode /
        wired draft — nothing to repair);
      - zero rewrite targets (fetches ride Session variables or unknown
        clients — rewriting those is unsafe, and a dead factory injection
        would pass the gate while the runtime stays unproxied);
      - the rewritten source still violates the gate.

    Formatting is NOT preserved (ast.unparse round-trip) — comments and
    layout are lost, semantics and parseability are not.
    """
    if not text or not text.strip():
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    if _ladder_gate_on_text(text, strategy) is None:
        return None  # exempt / non-nav / already wired
    rewrites = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LADDER_REWRITE_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
            and not any(kw.arg == "proxies" for kw in node.keywords)
        ):
            node.keywords.append(ast.keyword(
                arg="proxies",
                value=ast.parse(
                    _LADDER_REPAIR_PROXIES_EXPR, mode="eval"
                ).body,
            ))
            rewrites += 1
    if not rewrites:
        return None
    src = ast.unparse(ast.fix_missing_locations(tree))
    if "from src.proxy import" not in src:
        src = _LADDER_REPAIR_PREAMBLE + src
    try:
        ast.parse(src)
    except SyntaxError:
        return None
    if _ladder_gate_on_text(src, strategy) is not None:
        return None
    return src


def restore_job_draft(root: str, slug: str, job_id) -> str | None:
    """Restore THIS job's own draft archive into the workspace.

    code_writer snapshots every completed draft to
    ``scrapers/{slug}/jobs/scraper-draft-{job_id}.py`` in the File Master.
    The key is per-job, so a fresh user re-run (new job id) can never inherit
    a stale draft from it. Returns the restore path on success, else None.
    """
    if not slug or not job_id:
        return None
    target = draft_path_for(root, slug)
    if draft_parses(target):
        return None  # nothing to restore over
    try:
        import src.artifacts as artifacts

        per_job_key = artifacts.scrapers_key(slug, "jobs", f"scraper-draft-{job_id}.py")
        if not artifacts.exists(per_job_key):
            return None
        payload = artifacts.read(per_job_key)
        # [wave-32 D3] The latest archive may itself be a broken snapshot
        # (587 snapshotted a SyntaxError draft). Prefer the known-good
        # freeze when the latest content does not parse.
        if not payload_parses(payload):
            good_key = draft_good_key(slug, job_id)
            if artifacts.exists(good_key):
                good_payload = artifacts.read(good_key)
                if payload_parses(good_payload):
                    payload = good_payload
                    logger.info(
                        "draft_safety: latest archive unparseable — using the "
                        "known-good freeze (job %s)", job_id,
                    )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(payload)
        logger.info(
            "draft_safety: restored scraper_draft.py from THIS job's FM draft "
            "archive (job %s)", job_id,
        )
        return target
    except Exception as exc:
        logger.warning("draft_safety: per-job draft restore failed (job %s): %s", job_id, exc)
        return None


# ── [wave-40 T6] AST-only helper CALL-signature gate ─────────────────────────
#
# Three prod jobs crashed AT EXECUTION on a helper call shape that both the
# compile gate and the F821 gate (filesystem_tools) accept, because neither
# binds arguments:
#   job 760  fetch_json() got an unexpected keyword argument 'post'
#            (the create_fetch_json closure takes url, params, min_tier)
#   job 791  _discover_listing_urls_with_retry() got multiple values for
#            argument 'fetch_page' (positional slot 1 AND keyword)
#   job 762  re.error: nothing to repeat (a bad regex literal)
#
# Everything here is AST-only: no runtime import/exec of the draft, no network,
# no LLM. Registry signatures are read from SOURCE (importing the registry
# modules would silently fall open whenever an import fails), so a draft can
# never be executed or imported to be judged.

DRAFT_CALL_VIOLATION_MARKER: str = "HELPER CALL SIGNATURE VIOLATION"

# Findings shown to the writer per draft — enough to fix the class, not enough
# to drown the fix directive.
_DRAFT_CALL_FINDING_CAP = 5

# Repo-relative module paths whose callables a generated draft may legally
# call bare. src/ modules first (their signatures win name collisions);
# the active template set follows. The retired per-domain templates
# (article/forum/generic/akamai_stealth) are deliberately absent — CLAUDE.md
# dead-templates list.
_REGISTRY_MODULES: list[str] = [
    "src/http_fetch.py",
    "src/listing_discovery.py",
    "src/seed_urls.py",
    "src/content_types.py",
    "src/intake_coerce.py",
    "src/intake_url_list.py",
    "templates/http_navigation_scraper.py",
    "templates/navigation_scraper.py",
    "templates/undetected_chromedriver_scraper.py",
    "templates/api_scraper.py",
    "templates/playwright_scraper.py",
    "templates/requests_scraper.py",
    "templates/shopify_scraper.py",
]

# ``re.<method>(pattern, ...)`` calls whose FIRST positional argument, when a
# string literal, must compile (job-762 class).
_REGEX_PATTERN_METHODS = frozenset({
    "compile", "match", "fullmatch", "search", "sub", "subn", "split",
    "findall", "finditer",
})

# Memoization. Two views are built in one pass:
#   _REGISTRY_CACHE — name -> signature profile (the brief's registry dict)
#   _FACTORY_SIGS   — factory name -> the factory's OWN signature
# The registry deliberately maps a factory name to its RETURNED CLOSURE's
# signature (both names), because drafts call the closures bare after
# ``fetch_json = create_fetch_json()``. But a factory is also INVOKED with its
# own kwargs — templates/requests_scraper.py:105 writes
# ``create_fetch_page(delay_s=..., headers=...)`` and drafts copy that line
# verbatim — so judging the invocation against the closure signature would
# false-flag every template-derived draft. The factory's own signature is kept
# aside for exactly that call shape.
_REGISTRY_CACHE: dict[str, dict] = {}
_FACTORY_SIGS: dict[str, dict] = {}
_REGISTRY_BUILT = False


def _registry_root() -> str:
    """Repo root for the registry's repo-relative module paths.

    This module lives at ``<repo>/webapp/agents/``, so the root is two parents
    up; a couple of extra levels are probed for exotic layouts. A wrong root
    just empties the registry, which makes the gate fall open.
    """
    agents_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(agents_dir))
    probe = _REGISTRY_MODULES[0]
    for candidate in (root, os.path.dirname(root),
                      os.path.dirname(os.path.dirname(root))):
        if os.path.isfile(os.path.join(candidate, probe)):
            return candidate
    return root


def _signature_profile(node) -> dict:
    """Plain signature dict for a FunctionDef/AsyncFunctionDef."""
    args = node.args
    positional = [a.arg for a in getattr(args, "posonlyargs", []) + args.args]
    return {
        "positional": positional,
        "kwonly": [a.arg for a in args.kwonlyargs],
        "vararg": args.vararg is not None,
        "kwarg": args.kwarg is not None,
        "required_positional": max(0, len(positional) - len(args.defaults)),
        "required_kwonly": [
            a.arg for a, default in zip(args.kwonlyargs, args.kw_defaults)
            if default is None
        ],
    }


def _signature_hint(name: str, profile: dict) -> str:
    parts = list(profile["positional"])
    if profile["vararg"]:
        parts.append("*args")
    elif profile["kwonly"]:
        parts.append("*")
    parts.extend(profile["kwonly"])
    if profile["kwarg"]:
        parts.append("**kwargs")
    return f"{name}({', '.join(parts)})"


def _returned_closure_name(top) -> str | None:
    """Name returned by a module-level factory's tail ``return <name>``, else None."""
    body = top.body
    if not body:
        return None
    tail = body[-1]
    if isinstance(tail, ast.Return) and isinstance(tail.value, ast.Name):
        return tail.value.id
    return None


def _register_module(tree, factories: dict[str, dict]) -> dict[str, dict]:
    """Signatures for one module: every def at ANY depth, plus factory→closure
    aliasing (a module-level function whose body ends in ``return <nested>``
    registers the NESTED signature under both names, because that closure is
    what a draft actually calls)."""
    mod: dict[str, dict] = {}
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        mod[top.name] = _signature_profile(top)
        tail = _returned_closure_name(top)
        if not tail:
            continue
        for sub in ast.walk(top):
            if (sub is not top
                    and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == tail):
                closure = _signature_profile(sub)
                mod[top.name] = closure
                mod.setdefault(tail, closure)
                factories.setdefault(top.name, _signature_profile(top))
                break
        for sub in ast.walk(top):
            if (sub is not top
                    and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))):
                mod.setdefault(sub.name, _signature_profile(sub))
    return mod


def _helper_registry() -> dict[str, dict]:
    """Memoized AST-only registry: helper name -> signature profile."""
    global _REGISTRY_BUILT
    if _REGISTRY_BUILT:
        return _REGISTRY_CACHE
    registry: dict[str, dict] = {}
    factories: dict[str, dict] = {}
    root = _registry_root()
    parsed_any = False
    for rel in _REGISTRY_MODULES:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except Exception as exc:
            # A missing/unreadable module contributes nothing — the OTHER
            # modules still carry the load-bearing http_fetch signatures.
            logger.warning("draft_safety: registry module skipped %s (%s)", rel, exc)
            continue
        parsed_any = True
        try:
            module_sigs = _register_module(tree, factories)
        except Exception as exc:
            logger.warning("draft_safety: registry module half-parsed %s (%s)", rel, exc)
            continue
        for name, profile in module_sigs.items():
            registry.setdefault(name, profile)
    if not registry:
        # A half-built gate must never block drafts — fall open, loudly.
        logger.error(
            "draft_safety: helper call registry is EMPTY (root=%s, parsed_any=%s) "
            "— draft_call_violation will fall open", root, parsed_any,
        )
    _REGISTRY_CACHE.clear()
    _REGISTRY_CACHE.update(registry)
    _FACTORY_SIGS.clear()
    _FACTORY_SIGS.update(factories)
    _REGISTRY_BUILT = True
    return _REGISTRY_CACHE


def _factory_signatures() -> dict[str, dict]:
    """Factory name -> the factory's OWN (pre-closure) signature."""
    if not _REGISTRY_BUILT:
        _helper_registry()
    return _FACTORY_SIGS


def _signature_violations(name: str, node: ast.Call, profile: dict) -> list[str]:
    """bind-partial call check: only shapes that CRASH at runtime are
    violations. Missing-required and starred calls stay LEGAL — the writer may
    know runtime defaults and shapes the AST cannot see."""
    out: list[str] = []
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return out  # *args: the runtime arity is unknowable — LEGAL
    positional = profile["positional"]
    if len(node.args) > len(positional) and not profile["vararg"]:
        out.append(
            f"line {node.lineno}: {name}() takes at most {len(positional)} "
            f"positional argument(s) but {len(node.args)} were given "
            f"(known signature {_signature_hint(name, profile)})"
        )
    for kw in node.keywords:
        if kw.arg is None:
            continue  # **spread — LEGAL
        if kw.arg in positional:
            slot = positional.index(kw.arg)
            if slot < len(node.args):
                out.append(
                    f"line {node.lineno}: {name}() got multiple values for "
                    f"argument '{kw.arg}' — it is positional slot {slot + 1} "
                    "and is also passed by keyword"
                )
            continue
        if kw.arg in profile["kwonly"] or profile["kwarg"]:
            continue
        out.append(
            f"line {node.lineno}: {name}() got an unexpected keyword argument "
            f"'{kw.arg}' (known signature {_signature_hint(name, profile)})"
        )
    return out


def _regex_literal_violation(node: ast.Call, func) -> list[str]:
    """A string-literal pattern handed to ``re.<method>`` must compile."""
    if not (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
            and func.attr in _REGEX_PATTERN_METHODS
            and node.args):
        return []
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return []
    try:
        re.compile(first.value)
    except re.error as exc:
        return [
            f"line {node.lineno}: re.{func.attr}({first.value!r}) raises "
            f"re.error: {exc} — this pattern literal never compiles "
            "(job-762 class)"
        ]
    except Exception:
        return []  # not a clean re.error → fall open
    return []


def _call_violations(node: ast.Call, local: dict, registry: dict,
                     factory_sigs: dict) -> list[str]:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else ""
    profile: dict | None = None
    if name:
        if name in local:
            profile = local[name]  # draft-local def shadows the registry
        elif name in factory_sigs:
            profile = factory_sigs[name]  # factory INVOCATION → own signature
        else:
            profile = registry.get(name)
    out = _signature_violations(name, node, profile) if profile else []
    out.extend(_regex_literal_violation(node, func))
    return out


def draft_call_violation(source: str) -> str:
    """AST pre-gate: helper CALL signatures in a generated draft.

    Returns "" on PASS (or whenever the gate cannot decide — fall OPEN, a gate
    bug must never block a good draft) and otherwise a capped, newline-joined
    findings string prefixed with ``DRAFT_CALL_VIOLATION_MARKER``.

    Checked, per ``ast.Call``:
      - unexpected-kwarg / multiple-values-for-argument / too-many-positionals
        against the AST-derived registry of src/ + template helpers;
      - ``re.<method>('literal', ...)`` patterns that raise ``re.error``.
    Legal by design: missing-required args and starred calls (the writer may
    know runtime defaults), ``**spread`` keywords, and any call on a name the
    draft defines itself or that the registry does not know.

    No runtime import/exec of the draft, no network, no LLM.
    """
    if not source or not source.strip():
        return ""
    try:
        tree = ast.parse(source)
    except Exception:
        return ""  # unparseable → the syntax fixer owns those, not this gate
    try:
        findings = _draft_call_findings(tree)
    except Exception as exc:
        logger.warning(
            "draft_safety: call-signature gate fell open on an internal error: %s",
            exc,
        )
        return ""
    if not findings:
        return ""
    findings = findings[:_DRAFT_CALL_FINDING_CAP]
    return (
        f"{DRAFT_CALL_VIOLATION_MARKER} — {len(findings)} draft call(s) would "
        f"crash at execution:\n" + "\n".join(findings)
    )


def _draft_call_findings(tree) -> list[str]:
    registry = _helper_registry()
    if not registry:
        return []  # empty registry → fall open (logged at build time)
    factory_sigs = _factory_signatures()
    # Draft-local view, built FIRST so it shadows the registry:
    #   - every def/async def name in the draft;
    #   - ``name = factory_call()`` aliases name to the factory's returned
    #     closure signature — how generated drafts actually reach the
    #     http_fetch closures (``fetch_json = create_fetch_json()`` then a
    #     bare ``fetch_json(url, ...)``).
    local: dict[str, dict] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local[node.name] = _signature_profile(node)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)):
            continue
        factory_name = node.value.func.id
        closure = registry.get(factory_name) if factory_name in factory_sigs else None
        if closure:
            local[node.targets[0].id] = closure
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        findings.extend(_call_violations(node, local, registry, factory_sigs))
        if len(findings) >= _DRAFT_CALL_FINDING_CAP:
            break
    return findings
