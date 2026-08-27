"""Deterministic AST-based renderer for the ``dagster_converter`` phase (T3.1).

Replaces the LLM in ``graph.py:_invoke_dagster_converter`` on the happy path.
The LLM path is retained as the fallback for draft shapes this module does not
recognise (``render_dagster_module`` returns ``None`` — it never raises).

Why this is safe to do without an LLM
-------------------------------------
The converter's output is only ``ast.parse``'d + import-binding-checked
(``graph.py`` ~4198-4232).  It is never executed by the pipeline and never
re-verified against a live site, so the LLM buys no validated correctness —
only prose.  What the output *must* preserve from the draft is mechanical:

* the base-class contract (``class X(BaseTlsScraper)`` + ``discover_urls`` /
  ``scrape_one``),
* the module constants (site/listing/API URLs, selectors, regexes),
* the Phase-2 parsing logic verbatim (selectors, soft-404 tuples, field names),
* the Phase-1 discovery logic verbatim (listing URL, pagination param, dedup).

This module copies all of the above out of the draft's AST and re-emits them,
which preserves *strictly more* of the draft than an LLM rewrite does.

Recognition contract (what makes a draft "recognised")
------------------------------------------------------
A draft is rendered only when ALL of these hold; otherwise ``None``:

1. It has module-level ``def``s (not a class-only draft).
2. Nothing from our repo (``src.*``) is actually *called* — those modules do
   not exist in the client's environment.
3. Exactly one *item extractor* can be identified, and every one of its
   parameters is fillable from ``url`` / the fetched page / a module constant
   (``_fill_extractor_args``).
4. Its HTTP transport uses a *bridging* return contract — soup-pair, text-pair
   or JSON (``_classify_transport``).  Drafts whose transport returns a raw
   ``requests.Response``, or which drive a Playwright ``page``/``browser``
   object, are rejected: adapting those is a strategy decision, not a
   mechanical one, and the LLM fallback owns it.
5. No copied helper shadows a ``BaseTlsScraper`` method name.

Determinism: no clock, no randomness, no I/O beyond reading the draft.
Stable input -> byte-identical output (pinned by test).
"""

from __future__ import annotations

import ast
import builtins
import os
import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_BASE_CLASS",
    "DEFAULT_BASE_MODULE",
    "RENDERER_NAME",
    "describe_rejection",
    "render_dagster_module",
]

RENDERER_NAME = "dagster_renderer.py"

DEFAULT_BASE_MODULE = "dagster_scraper_base"
DEFAULT_BASE_CLASS = "BaseTlsScraper"

_FETCH_CALL = "self._fetch(url, self.brightdata_proxy)"

# ---------------------------------------------------------------------------
# Rejection plumbing
# ---------------------------------------------------------------------------


class _Reject(Exception):
    """Internal control flow — never escapes :func:`render_dagster_module`."""


def describe_rejection(report: Mapping[str, Any] | None) -> str:
    """Human-readable one-liner for a ``report`` dict handed to the renderer."""
    if not report:
        return "no report"
    if report.get("ok"):
        return f"rendered ({report.get('shape')})"
    return str(report.get("reason") or "unrecognised draft structure")


# ---------------------------------------------------------------------------
# Name tables
# ---------------------------------------------------------------------------

# Functions that exist only to drive OUR pipeline's CLI/concurrency/checkpoint
# model.  None of them are part of the dagster contract, so none are copied.
_NON_COPIED = frozenset(
    {
        "main",
        "load_urls_from_file",
        "save_urls_to_file",
        "_write_checkpoint",
        "_load_checkpoint",
        "_scrape_codes",
        "_scrape_one",
        "scrape_product_concurrent",
        "scrape_from_listing",
        "_extract_all_concurrent",
        "_extract_item_concurrent",
        "fetch_all_products_via_api",
        "_extract_item_safe",
        "_get_session",
        "_thread_local_session",
        "_get_thread_session",
        "_load_input_urls",
        "new_context",
        "walk_source",
    }
)

# Ordered most-specific first.  The first present name wins.
_EXTRACTOR_NAME_PRIORITY: Sequence[str] = (
    "extract_product_from_page",
    "extract_article_from_page",
    "extract_item_from_product_page",
    "extract_item_from_page",
    "_extract_item_data",
    "_extract_item_http",
    "_extract_item",
    "_extract_record",
    "extract_product",
    "extract_item",
    "scrape_product",
    "scrape_one",
    "scrape_item",
    "transform_api_product",
    "transform_jsonld_product",
)

_DISCOVERY_NAME_PRIORITY: Sequence[str] = (
    "discover_product_urls",
    "discover_item_urls",
    "discover_urls",
    "discover_product_urls_from_html",
    "_discover_urls",
    "_discover_codes",
    "_discover_urls_via_category",
    "_discover_urls_via_search",
    "_discover_urls_via_form_search",
    "extract_product_pages",
)

# Candidate HTTP helpers.  A draft usually has at most one; when it has more
# (e.g. GET + POST) we only proceed when exactly one of them is bridgeable.
_TRANSPORT_NAMES = frozenset(
    {
        "fetch_page",
        "fetch_html",
        "fetch_url",
        "fetch_api",
        "http_get",
        "_http_get",
        "_fetch",
        "_get_page",
        "_fetch_api",
    }
)

# ``BaseTlsScraper`` (templates/dagster_template.py) defines these.  A draft
# helper with the same name would silently shadow the base implementation.
_RESERVED_METHOD_NAMES = frozenset(
    {
        "__init__",
        "_fetch",
        "make_request",
        "get_thread_session",
        "discover_urls",
        "scrape_one",
    }
)

# Module constants that describe OUR run environment (paths, clocks, output
# naming, our own infra).  They are meaningless in the client's process.
_ENV_CONSTANTS = frozenset(
    {
        "SCRIPT_DIR",
        "TIMESTAMP",
        "OUTPUT_FILE",
        "INPUT_FILE",
        "LOG_FILE",
        "OUTPUT_KEY",
        "PROXY_TIER",
        "STEALTH",
        "_env_stealth",
        "BROWSER_SERVICE_URL",
        "NAVIGATE_TIMEOUT",
        "_thread_local",
        "_tls",
        "proxy_config",
    }
)

# Slug tokens that are a TLD / host fragment, not part of the site name.
_SLUG_NOISE = frozenset(
    {"www", "com", "au", "org", "net", "co", "uk", "us", "io", "gov", "edu"}
)

# Our own packages.  Anything imported from these cannot ship to the client.
_REPO_PACKAGE_PREFIXES = ("src", "webapp", "browser_service", "templates")

# Names an emitted method may legally load without being imported.
_RESOLVABLE = set(dir(builtins)) | {
    "self",
    "cls",
    "__name__",
    "__file__",
    "__doc__",
    "__class__",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_dagster_module(
    draft_path_or_text: str,
    template_text: str = "",
    context: Mapping[str, Any] | None = None,
    report: dict[str, Any] | None = None,
) -> str | None:
    """Render a dagster ``BaseTlsScraper`` module from a generated scraper draft.

    Args:
        draft_path_or_text: absolute path to the draft, **or** the draft source
            itself (resolved via ``os.path.isfile``).
        template_text: the client template (``templates/dagster_template.py``).
            Used only to discover the base class name so it is not hardcoded;
            ignored when empty or unparseable.
        context: optional ``{site_slug, site_url, site_name, input_mode,
            source_name, job_id}``.  ``input_mode == "url_list"`` forces
            ``discover_urls() -> []`` (the converter prompt's url_list rule).
        report: optional dict filled with ``{ok, reason, shape, class_name}``
            for logging.  Passed in, never stored module-level (thread-safe).

    Returns:
        The rendered module source, or ``None`` when the draft's structure is
        not recognised (the caller falls back to the LLM path).  Never raises.
    """
    ctx = dict(context or {})
    rep: dict[str, Any] = {"ok": False, "reason": "", "shape": "", "class_name": ""}
    try:
        source = _load_source(draft_path_or_text)
        # Only derive source_name from a PATH-like input. For raw source text,
        # basename() returns everything after the last "/" in the code — a
        # multi-line code fragment that would be spliced (uncommented!) into
        # the "# Source draft:" header line and break ast.parse.
        if (
            not ctx.get("source_name")
            and isinstance(draft_path_or_text, str)
            and "\n" not in draft_path_or_text
        ):
            name = os.path.basename(draft_path_or_text)
            if name:
                ctx["source_name"] = name
        tree = _parse(source)
        base_module, base_class = _base_names(template_text)
        plan = _build_plan(tree, ctx)
        text = _emit(plan, base_module, base_class, ctx)
        # Belt and braces: the emitted text must round-trip ast.parse before we
        # hand it back.  (The caller's acceptance gate re-checks it anyway.)
        ast.parse(text)
    except _Reject as exc:
        rep["reason"] = str(exc)
        _hand_over(report, rep)
        return None
    except Exception as exc:  # never propagate — the caller has an LLM fallback
        rep["reason"] = f"renderer error: {type(exc).__name__}: {exc}"
        _hand_over(report, rep)
        return None

    rep.update(ok=True, shape=plan.shape, class_name=plan.class_name, reason="")
    _hand_over(report, rep)
    return text


def _hand_over(report: dict[str, Any] | None, rep: Mapping[str, Any]) -> None:
    if report is None:
        return
    report.clear()
    report.update(rep)


# ---------------------------------------------------------------------------
# Loading / parsing
# ---------------------------------------------------------------------------


def _load_source(draft_path_or_text: str) -> str:
    if not isinstance(draft_path_or_text, str) or not draft_path_or_text.strip():
        raise _Reject("empty draft input")
    if "\n" not in draft_path_or_text and os.path.isfile(draft_path_or_text):
        try:
            with open(draft_path_or_text, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError as exc:
            raise _Reject(f"unreadable draft: {exc}") from exc
    return draft_path_or_text


def _parse(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise _Reject(f"draft does not parse: {exc}") from exc


def _base_names(template_text: str) -> tuple[str, str]:
    """``(module, class)`` of the client base, from the template if available.

    The class the converter is asked to subclass is the template's *own* class
    (``BaseTlsScraper``), not its parent (``BaseScraper``).
    """
    if not template_text.strip():
        return DEFAULT_BASE_MODULE, DEFAULT_BASE_CLASS
    try:
        tree = ast.parse(template_text)
    except SyntaxError:
        return DEFAULT_BASE_MODULE, DEFAULT_BASE_CLASS
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.bases:
            return DEFAULT_BASE_MODULE, node.name
    return DEFAULT_BASE_MODULE, DEFAULT_BASE_CLASS


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _called_names(fn: ast.FunctionDef) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def _call_graph(funcs: Mapping[str, ast.FunctionDef]) -> dict[str, set[str]]:
    return {name: _called_names(fn) & set(funcs) for name, fn in funcs.items()}


def _reachable(start: str, graph: Mapping[str, set[str]], limit: int = 6) -> set[str]:
    seen: set[str] = set()
    frontier = [start]
    depth = 0
    while frontier and depth < limit:
        nxt: list[str] = []
        for node in frontier:
            for callee in graph.get(node, ()):
                if callee in seen or callee == start:
                    continue
                seen.add(callee)
                nxt.append(callee)
        frontier = nxt
        depth += 1
    return seen


def _assigned_names(node: ast.stmt) -> set[str]:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    return {t.id for t in targets if isinstance(t, ast.Name)}


def _used_names(nodes: Iterable[ast.AST]) -> set[str]:
    """Name ids referenced anywhere, including the root of ``a.b.c`` chains."""
    out: set[str] = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                root = sub
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    out.add(root.id)
    return out


def _annotation_names(nodes: Iterable[ast.AST]) -> set[str]:
    """Names appearing only in annotations (they still need importing)."""
    out: set[str] = set()
    for node in nodes:
        if not isinstance(node, ast.FunctionDef):
            continue
        args = list(node.args.args) + list(node.args.kwonlyargs)
        for arg in args:
            if arg.annotation is not None:
                out |= _used_names([arg.annotation])
        if node.returns is not None:
            out |= _used_names([node.returns])
    return out


def _dict_literal_keys(fn: ast.FunctionDef) -> list[str]:
    keys: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict) and node.keys:
            if all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
                keys.extend(k.value for k in node.keys)
    return keys


def _constant_name_map(tree: ast.Module) -> dict[str, str]:
    """``UPPER`` -> source name for module-level constants."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for name in _assigned_names(node):
                if name.isupper():
                    out[name] = name
    return out


def _regex_constants(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "compile"
        ):
            out.extend(_assigned_names(node))
    return out


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


class _Plan:
    __slots__ = (
        "shape",
        "class_name",
        "constants",
        "helpers",
        "bridges",
        "discovery",
        "extractor",
        "extractor_args",
        "needs_bs4",
        "needs_json",
        "needs_datetime",
        "site_url",
        "source_name",
        "import_lines",
        "import_bound",
        "discovery_args",
    )

    def __init__(self) -> None:
        self.shape = ""
        self.class_name = ""
        self.constants: list[ast.stmt] = []
        self.helpers: list[ast.FunctionDef] = []
        self.bridges: list[ast.FunctionDef] = []
        self.discovery: tuple[str, str] = ("url_list", "")
        self.discovery_args: list[str] = []
        self.extractor: ast.FunctionDef | None = None
        self.extractor_args: list[str] = []
        self.needs_bs4 = False
        self.needs_json = False
        self.needs_datetime = False
        self.site_url = ""
        self.source_name = ""
        self.import_lines: list[str] = []
        self.import_bound: set[str] = set()


def _build_plan(tree: ast.Module, ctx: Mapping[str, Any]) -> _Plan:
    funcs = _module_functions(tree)
    if not funcs:
        raise _Reject("draft has no module-level functions")
    _assert_no_repo_call(funcs)

    graph = _call_graph(funcs)

    transport = _pick_transport(funcs)
    contract = _classify_transport(transport) if transport else ""

    extractor = _pick_extractor(funcs)
    shape = _classify_extractor(extractor, transport, graph)

    plan = _Plan()
    plan.source_name = str(ctx.get("source_name") or "")
    plan.site_url = _site_url(tree, ctx)
    plan.class_name = _derive_class_name(tree, ctx)
    plan.shape = shape
    plan.extractor = extractor
    plan.needs_bs4 = "soup" in shape or contract == "soup_pair"

    regex_consts = _regex_constants(tree)
    plan.extractor_args = _fill_extractor_args(extractor, tree, regex_consts)

    helpers, bridges = _methodify(funcs, transport, contract)
    plan.helpers = helpers
    plan.bridges = bridges
    plan.needs_json = contract == "json"

    forced_regex = _forced_regex_constant(plan.extractor_args, regex_consts)
    plan.constants = _pick_constants(tree, plan, forced_regex)

    used = _used_names([extractor, *helpers, *plan.constants])
    plan.needs_datetime = bool({"datetime", "timezone"} & used) or "scraped_at" in _dict_literal_keys(
        extractor
    )

    plan.discovery = _pick_discovery(funcs, ctx)
    if plan.discovery[0] == "function":
        plan.discovery_args = _fill_discovery_args(funcs[plan.discovery[1]], tree)

    plan.import_lines, plan.import_bound = _plan_imports(tree, plan)
    _assert_resolvable(plan)
    return plan


def _assert_no_repo_call(funcs: Mapping[str, ast.FunctionDef]) -> None:
    """``src.discovery`` helpers do not exist in the client's environment."""
    banned = {"discover_item_urls", "config_for_load_more"}
    for fn in funcs.values():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in banned:
                    raise _Reject(
                        f"{fn.name}() calls our repo helper {node.func.id}(), "
                        f"which has no equivalent in the client environment"
                    )


# -- transport ---------------------------------------------------------------


def _pick_transport(funcs: Mapping[str, ast.FunctionDef]) -> ast.FunctionDef | None:
    candidates = [funcs[n] for n in sorted(_TRANSPORT_NAMES) if n in funcs]
    bridgeable = [c for c in candidates if _classify_transport(c) != "unknown"]
    if len(bridgeable) == 1:
        return bridgeable[0]
    if len(candidates) == 1:
        return candidates[0]
    # Zero candidates, or several that are each un-bridgeable: leave it to the
    # LLM (a second fetch mechanism would be silently dropped).
    return candidates[0] if len(candidates) > 1 else None


def _classify_transport(fn: ast.FunctionDef | None) -> str:
    """``soup_pair`` | ``text_pair3`` | ``json`` | ``unknown``.

    Only contracts that can be re-pointed at ``self._fetch(url, proxy)`` with an
    argument-compatible shim count.  A transport returning a raw
    ``requests.Response`` (books-toscrape job 218) or a Playwright ``page``
    cannot be shimmed without inventing an object, so it is ``unknown``.
    """
    if fn is None:
        return "unknown"
    kinds: set[str] = set()
    for ret in ast.walk(fn):
        if not isinstance(ret, ast.Return):
            continue
        val = ret.value
        if val is None or (isinstance(val, ast.Constant) and val.value is None):
            continue
        if isinstance(val, ast.Tuple):
            if len(val.elts) == 2:
                first = val.elts[0]
                if isinstance(first, ast.Call) and _is_bs4_call(first):
                    kinds.add("soup_pair")
                elif isinstance(first, ast.Call):
                    kinds.add("unknown")
                else:
                    kinds.add("soup_pair")
            elif len(val.elts) == 3:
                kinds.add("text_pair3")
            else:
                kinds.add("unknown")
        elif isinstance(val, ast.Call):
            kinds.add("json" if _is_json_call(val) else "unknown")
        else:
            # `return sess.get(url)`, `return resp.text`, `return page` ...
            kinds.add("unknown")
    if len(kinds) != 1:
        return "unknown"
    return kinds.pop()


def _is_bs4_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == "BeautifulSoup"


def _is_json_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "json"
        and isinstance(call.func.value, ast.Name)
    )


# -- extractor ---------------------------------------------------------------


def _pick_extractor(funcs: Mapping[str, ast.FunctionDef]) -> ast.FunctionDef:
    for name in _EXTRACTOR_NAME_PRIORITY:
        fn = funcs.get(name)
        if fn is not None and name not in _NON_COPIED:
            return fn
    for name, fn in funcs.items():
        if name in _NON_COPIED:
            continue
        if _dict_literal_keys(fn):
            return fn
    raise _Reject("no item-extractor function found")


def _classify_extractor(
    fn: ast.FunctionDef,
    transport: ast.FunctionDef | None,
    graph: Mapping[str, set[str]],
) -> str:
    p0 = fn.args.args[0].arg if fn.args.args else ""
    if p0.lower().startswith("soup") or "beautifulsoup" in _annotation_text(fn, 0).lower():
        return "soup_extractor"
    if transport is not None:
        reaches = transport.name in _reachable(fn.name, graph) or _called_names(fn) >= {
            transport.name
        }
        if reaches or transport.name in _called_names(fn):
            return "self_fetching_extractor"
    if p0.lower() in {"record", "product", "data", "api_product", "jsonld", "item"}:
        return "record_transform"
    # No recognised transport and the extractor fetches with its own session /
    # client object.  Legitimate to copy verbatim (the draft's own HTTP code
    # ships unchanged) — hence its own label, not a failure.
    return "direct_http_extractor"


def _annotation_text(fn: ast.FunctionDef, index: int) -> str:
    try:
        ann = fn.args.args[index].annotation
    except IndexError:
        return ""
    return ast.unparse(ann) if ann is not None else ""


def _fill_extractor_args(
    extractor: ast.FunctionDef, tree: ast.Module, regex_consts: Sequence[str]
) -> list[str]:
    """Build the argument list ``scrape_one(url)`` passes to the extractor.

    Fillable from the dagster contract:
      * ``url`` / ``item_url`` / ``product_url`` / ``src_url`` -> ``url``
      * ``soup*`` / annotated ``BeautifulSoup``               -> parsed page
      * ``status_code`` / ``status``                          -> fetch status
      * ``index`` / ``idx`` / ``i`` / ``n`` / ``seq``         -> ``0``
      * name matching a module constant                       -> that constant
      * exactly one unfillable scalar AND exactly one module
        ``re.compile`` constant -> ``CODE_RE.search(url).group(1)``
        (priceline's ``_extract_record(code, listing_url)`` + ``CODE_RE``).

    Anything else is a rejection — guessing would silently change behaviour.
    """
    consts = _constant_name_map(tree)
    args: list[str] = []
    unfillable: list[str] = []

    for arg in extractor.args.args:
        name = arg.arg
        low = name.lower()
        ann = ast.unparse(arg.annotation).lower() if arg.annotation is not None else ""
        if low in {"url", "item_url", "product_url", "src_url", "page_url"}:
            args.append("url")
        elif low.startswith("soup") or "beautifulsoup" in ann:
            args.append("soup")
        elif low in {"status_code", "status", "http_status"}:
            args.append("status_code")
        elif low in {"index", "idx", "i", "n", "seq"}:
            args.append("0")
        elif name.upper() in consts:
            args.append(consts[name.upper()])
        else:
            unfillable.append(name)
            args.append(f"\x00{name}\x00")

    if not unfillable:
        return args

    if len(unfillable) == 1 and len(regex_consts) == 1:
        return [
            f"{regex_consts[0]}.search(url).group(1) if {regex_consts[0]}.search(url) else ''"
            if a.startswith("\x00")
            else a
            for a in args
        ]

    raise _Reject(
        f"extractor {extractor.name}({', '.join(a.arg for a in extractor.args.args)}) "
        f"has argument(s) {unfillable} that cannot be derived from scrape_one(url)"
    )


def _forced_regex_constant(
    args: Sequence[str], regex_consts: Sequence[str]
) -> str:
    if len(regex_consts) == 1 and any(".search(url)" in a for a in args):
        return regex_consts[0]
    return ""


def _fill_discovery_args(
    fn: ast.FunctionDef, tree: ast.Module
) -> list[str]:
    """Argument list ``discover_urls()`` passes to the copied discovery helper.

    Only module constants and already-defaulted parameters are acceptable —
    there is no ``url`` to hand down, and inventing one would silently change
    which listing page is walked.
    """
    consts = _constant_name_map(tree)
    args: list[str] = []
    unfillable: list[str] = []
    for arg in fn.args.args:
        name = arg.arg
        low = name.lower()
        if name.upper() in consts:
            args.append(consts[name.upper()])
        elif low in {"limit", "max_items", "max_pages", "sample", "n"}:
            args.append("0")
        elif name.lower() in {"state", "discovery_state"}:
            args.append("{}")
        else:
            unfillable.append(name)
            args.append(f"\x00{name}\x00")
    if unfillable:
        raise _Reject(
            f"discovery helper {fn.name}({', '.join(a.arg for a in fn.args.args)}) "
            f"has argument(s) {unfillable} that cannot be derived from module "
            f"constants"
        )
    return args


# -- methodification ---------------------------------------------------------


def _methodify(
    funcs: Mapping[str, ast.FunctionDef],
    transport: ast.FunctionDef | None,
    contract: str,
) -> tuple[list[ast.FunctionDef], list[ast.FunctionDef]]:
    """Turn module functions into methods; swap the transport for a bridge."""
    if transport is not None and contract == "unknown":
        raise _Reject(
            f"transport {transport.name}() returns a value that cannot be "
            f"re-pointed at {DEFAULT_BASE_CLASS}._fetch() "
            f"(raw Response/page object) — needs the LLM path"
        )

    copied = {
        n for n in funcs if n not in _NON_COPIED and n not in _RESERVED_METHOD_NAMES
    }
    if transport is not None:
        copied.discard(transport.name)

    # Call sites must be re-pointed at *methods*, so the bridge's name is part
    # of the rewrite set even though its body is replaced.
    rewrite = set(copied) | ({transport.name} if transport is not None else set())

    helpers: list[ast.FunctionDef] = []
    for name in sorted(copied):
        fn = funcs[name]
        _assert_no_dangling_sibling(fn, rewrite)
        helpers.append(_as_method(fn, rewrite))

    bridges = [_make_bridge(transport, contract)] if transport is not None else []
    return helpers, bridges


def _assert_no_dangling_sibling(fn: ast.FunctionDef, siblings: set[str]) -> None:
    """A sibling referenced *not* as a direct call would dangle after methodify."""
    call_targets = {
        id(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in siblings:
            if id(node) not in call_targets:
                raise _Reject(
                    f"{fn.name}() uses helper {node.id} as a value rather than "
                    f"calling it — cannot be methodified"
                )
        if isinstance(node, (ast.Lambda, ast.FunctionDef)) and node is not fn:
            closed = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if closed & siblings:
                raise _Reject(
                    f"{fn.name}() nests a lambda/function closing over a sibling "
                    f"helper ({sorted(closed & siblings)})"
                )


def _as_method(fn: ast.FunctionDef, siblings: set[str]) -> ast.FunctionDef:
    """Module function -> method. ``@staticmethod`` when it touches no sibling.

    Sibling detection runs on the *pre-rewrite* body: at this point sibling
    calls are still plain ``Name`` calls, which is exactly what we are testing
    for (``_calls_any_sibling`` only sees already-rewritten ``self.x()``).
    """
    new = _clone(fn)
    if _calls_sibling_by_name(new, siblings):
        new.args.args.insert(0, ast.arg(arg="self"))
        _rewrite_sibling_calls(new, siblings)
    else:
        new.decorator_list = [ast.Name(id="staticmethod", ctx=ast.Load())]
    ast.fix_missing_locations(new)
    return new


def _calls_sibling_by_name(node: ast.AST, siblings: set[str]) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in siblings
        for n in ast.walk(node)
    )


def _clone(fn: ast.FunctionDef) -> ast.FunctionDef:
    new = ast.parse(ast.unparse(fn)).body[0]
    assert isinstance(new, ast.FunctionDef)
    return new


def _rewrite_sibling_calls(node: ast.AST, siblings: set[str]) -> None:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id in siblings:
                sub.func = ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr=sub.func.id,
                    ctx=ast.Load(),
                )


_BRIDGE_BODIES: Mapping[str, str] = {
    "soup_pair": (
        "html, status_code = {fetch}\n"
        "if html is None:\n"
        "    return None\n"
        'return BeautifulSoup(html, "html.parser"), status_code'
    ),
    "text_pair3": (
        "html, status_code = {fetch}\n"
        'return (html or "", status_code, {url_var})'
    ),
    "json": (
        "html, status_code = {fetch}\n"
        "if not html:\n"
        "    return None\n"
        "try:\n"
        "    return json.loads(html)\n"
        "except (ValueError, TypeError):\n"
        '    logger.warning("Non-JSON response (status %s)", status_code)\n'
        "    return None"
    ),
}


def _make_bridge(fn: ast.FunctionDef, contract: str) -> ast.FunctionDef:
    """Re-point the draft's transport helper at ``BaseTlsScraper._fetch()``.

    Same name and same parameter list as the draft's helper, so every call site
    in the copied code keeps working unchanged.  Only the body changes: the
    draft's own retry/backoff/session-rotation loop is replaced by the base
    class's, which is exactly what ``BaseTlsScraper`` exists to own.
    """
    new = _clone(fn)
    new.args.args.insert(0, ast.arg(arg="self"))
    _default_all_args(new)
    new.decorator_list = []
    new.returns = None

    url_var = _first_url_arg(new)
    body_src = _BRIDGE_BODIES[contract].format(
        fetch=f"self._fetch({url_var}, self.brightdata_proxy)", url_var=url_var
    )

    # Single-line docstring: ast.unparse does not re-indent embedded newlines,
    # so a multi-line one would render at the wrong indent inside the class.
    doc = (ast.get_docstring(new) or f"Bridges the draft's {new.name}() helper.")
    doc = " ".join(doc.split())
    new.body = ast.parse(
        f'"""{doc} [re-pointed at {DEFAULT_BASE_CLASS}._fetch() by {RENDERER_NAME}.]"""\n'
        + body_src
    ).body
    ast.fix_missing_locations(new)
    return new


def _first_url_arg(fn: ast.FunctionDef) -> str:
    for arg in fn.args.args:
        if "url" in arg.arg.lower():
            return arg.arg
    return fn.args.args[1].arg if len(fn.args.args) > 1 else "url"


def _default_all_args(fn: ast.FunctionDef) -> None:
    """Every positional param gets a default, so any call arity stays legal."""
    args = fn.args
    n_pos = max(len(args.args) - 1, 0)
    defaults = [d for d in args.defaults if d is not None]
    while len(defaults) < n_pos:
        defaults.insert(0, ast.Constant(value=None))
    args.defaults = defaults[-n_pos:] if n_pos else []
    if args.kwonlyargs:
        args.kw_defaults = [
            d if d is not None else ast.Constant(value=None) for d in args.kw_defaults
        ] or [ast.Constant(value=None) for _ in args.kwonlyargs]
    ast.fix_missing_locations(fn)


# -- constants ---------------------------------------------------------------


def _pick_constants(
    tree: ast.Module, plan: _Plan, forced: str = ""
) -> list[ast.stmt]:
    """Module constants still referenced by the emitted code (2-pass fixpoint)."""
    emitted = [n for n in (plan.extractor, *plan.helpers, *plan.bridges) if n]
    candidates: dict[str, ast.stmt] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        names = _assigned_names(node)
        if not names or (names & _ENV_CONSTANTS):
            continue
        if _is_nondeterministic_value(node):
            continue
        for name in names:
            candidates[name] = node

    used = _used_names(emitted) | _extractor_arg_names(plan.extractor_args)
    selected: dict[str, ast.stmt] = {}
    for _ in range(4):
        selected = {n: node for n, node in candidates.items() if n in used}
        if forced and forced in candidates:
            selected[forced] = candidates[forced]
        used = _used_names([*emitted, *selected.values()])

    chosen = {id(node) for node in selected.values()}
    return [node for node in tree.body if id(node) in chosen]


def _locally_bound(fn: ast.FunctionDef) -> set[str]:
    bound: set[str] = set()
    args = fn.args
    for arg in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
        bound.add(arg.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def _extractor_arg_names(args: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for arg in args:
        out |= set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", arg))
    return out


def _is_nondeterministic_value(node: ast.stmt) -> bool:
    value = node.value
    for sub in ast.walk(value):
        if not isinstance(sub, ast.Call):
            continue
        root: ast.AST = sub.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in {
            "os",
            "sys",
            "datetime",
            "time",
            "logging",
            "random",
            "open",
            "input",
            "id",
            "Path",
        }:
            return True
        if isinstance(sub.func, ast.Name) and sub.func.id in {"open", "input", "id"}:
            return True
    return False


# -- discovery ---------------------------------------------------------------


def _pick_discovery(
    funcs: Mapping[str, ast.FunctionDef], ctx: Mapping[str, Any]
) -> tuple[str, str]:
    """``("url_list", "")`` (emit ``return []``) or ``("function", name)``."""
    if str(ctx.get("input_mode", "")).strip().lower() == "url_list":
        return ("url_list", "")
    for name in _DISCOVERY_NAME_PRIORITY:
        fn = funcs.get(name)
        if fn is None or name in _NON_COPIED:
            continue
        if _returns_tuple(fn) or _returns_list(fn):
            return ("function", name)
    return ("url_list", "")


def _returns_tuple(fn: ast.FunctionDef) -> bool:
    return any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple) for n in ast.walk(fn)
    )


def _returns_list(fn: ast.FunctionDef) -> bool:
    return any(
        isinstance(n, ast.Return)
        and isinstance(n.value, (ast.List, ast.Name, ast.Call))
        for n in ast.walk(fn)
    )


# -- naming ------------------------------------------------------------------


def _site_url(tree: ast.Module, ctx: Mapping[str, Any]) -> str:
    for key in ("site_url", "url"):
        val = ctx.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for node in tree.body:
        if isinstance(node, ast.Assign) and (_assigned_names(node) & {"SITE_URL", "BASE_URL", "DOMAIN"}):
            try:
                val = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(val, str):
                return val
    return ""


def _derive_class_name(tree: ast.Module, ctx: Mapping[str, Any]) -> str:
    site_name = ctx.get("site_name")
    if isinstance(site_name, str) and site_name.strip():
        stem = _camel(site_name)
        if stem:
            return f"{stem}Scraper"
    for node in tree.body:
        if isinstance(node, ast.Assign) and "SITE_NAME" in _assigned_names(node):
            try:
                raw = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                raw = ""
            if isinstance(raw, str):
                stem = _camel(raw)
                if stem:
                    return f"{stem}Scraper"
            break
    slug = str(ctx.get("site_slug") or "") or _slug_from_constants(tree) or "site"
    return f"{_camel_from_slug(slug)}Scraper"


def _slug_from_constants(tree: ast.Module) -> str:
    for node in tree.body:
        if isinstance(node, ast.Assign) and "SITE_SLUG" in _assigned_names(node):
            try:
                val = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return ""
            return val if isinstance(val, str) else ""
    return ""


def _camel(text: str) -> str:
    tokens = [t for t in re.split(r"[^0-9A-Za-z]+", str(text)) if t]
    tokens = [t for t in tokens if t.lower() not in {"scraper", "scrapers"}]
    return "".join(t[:1].upper() + t[1:] for t in tokens)


def _camel_from_slug(slug: str) -> str:
    tokens = [t for t in re.split(r"[-_.]+", slug.lower()) if t]
    while len(tokens) > 1 and tokens[-1] in _SLUG_NOISE:
        tokens.pop()
    return "".join(t[:1].upper() + t[1:] for t in tokens) or "Site"


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _emit(plan: _Plan, base_module: str, base_class: str, ctx: Mapping[str, Any]) -> str:
    out: list[str] = []
    out.append(f"# Rendered deterministically by webapp/agents/{RENDERER_NAME} (T3.1).")
    out.append(f"# Source draft: {plan.source_name or 'scraper draft'}; LLM path not used.")
    out.append("")
    # The caller's acceptance gate requires every ClassDef base to be bound at
    # module scope, so the base import goes first and is never commented out.
    out.append(f"from {base_module} import {base_class}")
    out.extend(plan.import_lines)
    out.append("")
    out.append("import logging")
    out.append("")
    out.append("logger = logging.getLogger(__name__)")

    if plan.constants:
        out.append("")
        out.append("# " + "-" * 70)
        out.append("# Constants copied verbatim from the source draft")
        out.append("# " + "-" * 70)
        for node in plan.constants:
            out.append(ast.unparse(node))
            out.append("")

    out.append("")
    out.append(_render_class(plan, base_class, ctx).rstrip("\n"))
    return "\n".join(out) + "\n"


def _plan_imports(tree: ast.Module, plan: _Plan) -> tuple[list[str], set[str]]:
    """Imports the emitted code actually needs.

    Carries the draft's own import lines forward (minus repo-internal ones and
    minus anything nothing in the emitted code references), then adds whatever
    the renderer itself introduces.  Returns ``(lines, bound_names)``.
    """
    emitted: list[ast.AST] = [
        n for n in (plan.extractor, *plan.helpers, *plan.bridges, *plan.constants) if n
    ]
    used = _used_names(emitted) | _extractor_arg_names(plan.extractor_args)
    used |= _annotation_names(emitted)
    if plan.shape == "soup_extractor" or plan.needs_bs4:
        used.add("BeautifulSoup")
    if plan.needs_json:
        used.add("json")

    kept: list[str] = []
    bound: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.Import):
            module = None
            bindings = [(a.asname or a.name.split(".")[0]) for a in node.names]
            line = "import " + ", ".join(
                a.name if not a.asname else f"{a.name} as {a.asname}" for a in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] in _REPO_PACKAGE_PREFIXES:
                continue
            bindings = [(a.asname or a.name) for a in node.names]
            line = f"from {'.' * node.level}{module} import " + ", ".join(
                a.name if not a.asname else f"{a.name} as {a.asname}" for a in node.names
            )
        else:
            continue
        needed = [b for b in bindings if b in used]
        if not needed:
            continue
        kept.append(line)
        bound.update(needed)

    # What the renderer itself introduces.
    extras: list[tuple[str, str]] = []  # (line, bound name)

    def _ensure(name: str, line: str) -> None:
        if name in used and name not in bound:
            extras.append((line, name))
            bound.add(name)

    _ensure("BeautifulSoup", "from bs4 import BeautifulSoup")
    _ensure("json", "import json")
    _ensure("re", "import re")
    if (plan.needs_datetime or {"datetime", "timezone"} & used) and not (
        {"datetime", "timezone"} <= bound
    ):
        extras.append(("from datetime import datetime, timezone", "datetime"))
        bound.update({"datetime", "timezone"})

    all_lines = [*kept, *(line for line, _ in extras)]
    plain = sorted(l for l in all_lines if l.startswith("import "))
    dotted = sorted(l for l in all_lines if not l.startswith("import "))
    return [*plain, *dotted], bound


def _assert_resolvable(plan: _Plan) -> None:
    """Every name the emitted code loads must actually resolve in the output.

    A strict superset of the caller's import-binding gate: it catches a helper
    or constant dropped by the copy rules that is still referenced — which
    today would only surface as a NameError inside the *client's* process.
    """
    module_names = {fn.name for fn in (*plan.bridges, *plan.helpers)}
    for node in plan.constants:
        module_names |= _assigned_names(node)
    allowed = module_names | plan.import_bound | {"logger"} | _RESOLVABLE

    for fn in (*plan.bridges, *plan.helpers, plan.extractor):
        if fn is None:
            continue
        loaded = {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        unresolved = loaded - _locally_bound(fn) - allowed
        if unresolved:
            raise _Reject(
                f"{fn.name}() references {sorted(unresolved)} which is not part "
                f"of the emitted module (dropped by the copy rules) — needs the "
                f"LLM path"
            )


def _render_class(plan: _Plan, base_class: str, ctx: Mapping[str, Any]) -> str:
    ind = "    "
    out: list[str] = [f"class {plan.class_name}({base_class}):"]
    out.extend(_docstring_block(_class_docstring(plan), ind))
    out.append("")

    # -- discover_urls -----------------------------------------------------
    kind, dname = plan.discovery
    out.append(f"{ind}def discover_urls(self) -> list[str]:")
    if kind == "url_list" or not dname:
        out.extend(
            _docstring_block(
                ["Phase 1: url_list job - the Dagster pipeline supplies the URLs."], ind + "    "
            )
        )
        out.append(f"{ind}    return []")
    else:
        out.extend(
            _docstring_block(
                ["Phase 1: discover item URLs (logic copied from the draft)."],
                ind + "    ",
            )
        )
        dfunc = _plan_function(plan, dname)
        call = f"self.{dname}({', '.join(plan.discovery_args)})"
        if dfunc is not None and _returns_tuple(dfunc):
            out.append(f"{ind}    urls, _discovery_meta = {call}")
            out.append(f"{ind}    return urls")
        else:
            out.append(f"{ind}    return {call}")
    out.append("")

    # -- scrape_one --------------------------------------------------------
    out.append(f"{ind}def scrape_one(self, url: str) -> dict:")
    out.extend(
        _docstring_block(
            ["Phase 2: fetch one item page and extract its fields."], ind + "    "
        )
    )
    if plan.shape == "soup_extractor":
        out.append(f"{ind}    html, status_code = {_FETCH_CALL}")
        out.append(f"{ind}    if html is None:")
        out.append(f"{ind}        return {_empty_dict_src(plan)}")
        out.append(f'{ind}    soup = BeautifulSoup(html, "html.parser")')
    out.append(
        f"{ind}    return self.{plan.extractor.name}({', '.join(plan.extractor_args)})"
    )
    out.append("")

    for fn in plan.bridges:
        out.extend(_indent_source(ast.unparse(fn), 1))
        out.append("")
    for fn in plan.helpers:
        out.extend(_indent_source(ast.unparse(fn), 1))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _docstring_block(lines: Sequence[str], indent: str) -> list[str]:
    """Emit a properly terminated triple-quoted docstring at ``indent``."""
    if not lines:
        return []
    if len(lines) == 1:
        return [f'{indent}"""{lines[0]}"""']
    body = [f'{indent}"""{lines[0]}']
    for line in lines[1:]:
        body.append(f"{indent}{line}" if line else "")
    body.append(f'{indent}"""')
    return body


def _plan_function(plan: _Plan, name: str) -> ast.FunctionDef | None:
    for fn in (*plan.bridges, *plan.helpers):
        if fn.name == name:
            return fn
    return None


def _empty_dict_src(plan: _Plan) -> str:
    """The ``html is None`` bail-out dict, keyed from the extractor's own keys."""
    ordered: list[str] = []
    for key in _dict_literal_keys(plan.extractor):
        if key not in ordered:
            ordered.append(key)
    entries: list[str] = []
    for key in ordered:
        if key == "url" or key == "src_url":
            entries.append(f'"{key}": url')
        elif key in {"status_code", "status"}:
            entries.append(f'"{key}": 0')
        elif key in {"id", "item_id", "index"}:
            entries.append(f'"{key}": 0')
        elif key == "scraped_at":
            entries.append('"scraped_at": datetime.now(timezone.utc).isoformat()')
        elif key == "remarks":
            entries.append('"remarks": "fetch failed"')
        else:
            entries.append(f'"{key}": ""')
    if not entries:
        entries = ['"url": url', '"src_url": url', '"status_code": 0', '"remarks": "fetch failed"']
    return "{" + ", ".join(entries) + "}"


def _class_docstring(plan: _Plan) -> list[str]:
    url = plan.site_url or "the target site"
    source = plan.source_name or "the scraper draft"
    return [
        f"Dagster-format scraper for {url}.",
        "",
        f"Converted deterministically from {source} by {RENDERER_NAME} (T3.1).",
        "",
        "Phase 1 (discover_urls) and Phase 2 (scrape_one) reuse the draft's own",
        "parsing logic — selectors, constants and soft-404 rules are copied,",
        "not re-authored. The HTTP transport is re-pointed at",
        f"{DEFAULT_BASE_CLASS}._fetch() with an argument-compatible shim.",
    ]


def _indent_source(src: str, levels: int) -> list[str]:
    pad = "    " * levels
    return [pad + line if line.strip() else "" for line in src.splitlines()]
