"""Tests for the deterministic dagster renderer (plan v2 T3.1 / critique I12).

The dagster_converter node (``graph.py:_invoke_dagster_converter``) currently
spends an LLM invocation producing a file that is only ever ``ast.parse``'d and
import-binding-checked.  ``webapp/agents/dagster_renderer.py`` replaces that
LLM with a mechanical AST transform, keeping the LLM path as the fallback for
unrecognised draft shapes.  These tests pin:

1. the output parses (the caller's first gate),
2. the output passes a reimplementation of the caller's import-binding gate
   (``graph.py`` ~4198-4232),
3. recognised draft -> source, unrecognised -> ``None`` (never a raise),
4. byte determinism for stable input,
5. semantic fidelity against real draft/dagster pairs on disk, when present,
6. the output actually imports in a stubbed client environment.

Run:
  docker compose exec -e PYTHONPATH=/app:/app/webapp celery-worker \
      bash -c "cd /app && python -m pytest tests/test_dagster_renderer.py -v"
  (also runs standalone: the renderer is stdlib-only and imports no Django.)
"""

from __future__ import annotations

import ast
import os
import re
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "webapp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import dagster_renderer as dr  # noqa: E402

FIXTURE_DRAFT = os.path.join(ROOT, "tests", "fixtures", "dagster_renderer_draft.py")
FIXTURE_TEMPLATE = os.path.join(ROOT, "templates", "dagster_template.py")

# Real artefacts live in the File Master; the suite must not depend on them.
SCRAPER_ROOTS = (
    os.path.join(ROOT, "scrapers"),
    os.path.join(ROOT, "shared-data", "scrapers"),
)

# Instance attributes the stubbed ``BaseScraper.__init__`` below sets; a copied
# method is allowed to read these without them existing as class methods.
BASE_INSTANCE_ATTRS = frozenset({"proxy", "brightdata_proxy", "bypass", "log"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(draft, context=None, template=None):
    """Render with the real client template, returning ``(text, report)``."""
    tpl = template
    if tpl is None and os.path.isfile(FIXTURE_TEMPLATE):
        with open(FIXTURE_TEMPLATE, "r", encoding="utf-8") as fh:
            tpl = fh.read()
    report: dict = {}
    text = dr.render_dagster_module(draft, tpl or "", context or {}, report)
    return text, report


def _import_binding_gate_violations(source: str) -> list[str]:
    """Reimplementation of graph.py:_invoke_dagster_converter's acceptance gate.

    Byte-for-byte the same walk: module-scope names bound by imports / classdefs
    / assignments, then every ``ClassDef`` base must be one of them.
    """
    tree = ast.parse(source)
    bound = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)

    unresolved = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id not in bound:
                    unresolved.append(
                        f"class {node.name}: base '{base.id}' not imported"
                    )
    return unresolved


def _source(maybe_path: str) -> str:
    """Test helpers accept either source text or a path to it."""
    if os.path.isfile(maybe_path):
        with open(maybe_path, "r", encoding="utf-8") as fh:
            return fh.read()
    return maybe_path


def _class_of(source: str) -> ast.ClassDef:
    tree = ast.parse(_source(source))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            return node
    raise AssertionError("no class in rendered output")


def _method_names(cls: ast.ClassDef) -> set[str]:
    return {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _selectors(source: str) -> set[str]:
    """String literals passed to ``soup.select`` / ``select_one``."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(_source(source))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"select", "select_one"} and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value)
    return out


def _string_literals(source: str) -> set[str]:
    return {
        n.value
        for n in ast.walk(ast.parse(_source(source)))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _dict_keys(source: str) -> set[str]:
    """String keys of every dict literal (the output field names)."""
    return {
        k.value
        for node in ast.walk(ast.parse(_source(source)))
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _record_keys(source: str, func_name: str) -> set[str]:
    """Dict keys inside one function — the item record, not pipeline plumbing."""
    tree = ast.parse(_source(source))
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name
    )
    return {
        k.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _iter_real_drafts() -> list[str]:
    found: list[str] = []
    for root in SCRAPER_ROOTS:
        if not os.path.isdir(root):
            continue
        for site in sorted(os.listdir(root)):
            jobs = os.path.join(root, site, "jobs")
            if not os.path.isdir(jobs):
                continue
            for name in sorted(os.listdir(jobs)):
                if re.fullmatch(r"scraper-\d+\.py", name):
                    found.append(os.path.join(jobs, name))
    return found


@pytest.fixture(scope="module")
def rendered_fixture():
    text, report = _render(FIXTURE_DRAFT, {"site_slug": "fixtures-example-com"})
    assert text is not None, report
    return text


# ---------------------------------------------------------------------------
# 1. Recognised structure -> source; unrecognised -> None
# ---------------------------------------------------------------------------


def test_recognized_draft_returns_source(rendered_fixture):
    assert isinstance(rendered_fixture, str)
    assert "class FixtureWidgetsScraper(BaseTlsScraper):" in rendered_fixture


def test_output_parses(rendered_fixture):
    ast.parse(rendered_fixture)  # raises on failure


def test_output_passes_import_binding_gate(rendered_fixture):
    """The caller's gate must pass unchanged — this is the whole contract."""
    assert _import_binding_gate_violations(rendered_fixture) == []


def test_base_class_import_is_active_not_commented(rendered_fixture):
    """P0-5 regression: a commented-out base import 'syntax OK's but NameErrors."""
    lines = [ln.strip() for ln in rendered_fixture.splitlines() if ln.strip()]
    assert "from dagster_scraper_base import BaseTlsScraper" in lines
    assert not any(
        ln.startswith("#") and "dagster_scraper_base" in ln for ln in lines
    )


def test_template_text_drives_the_base_class():
    custom = "class ClientBase(SomeOtherBase):\n    pass\n"
    text, report = _render(FIXTURE_DRAFT, template=custom)
    assert text is not None, report
    assert "class FixtureWidgetsScraper(ClientBase):" in text
    assert _import_binding_gate_violations(text) == []


def test_url_list_context_emits_empty_discover_urls(rendered_fixture):
    text, report = _render(
        FIXTURE_DRAFT, {"site_slug": "fixtures-example-com", "input_mode": "url_list"}
    )
    assert text is not None, report
    cls = _class_of(text)
    disc = next(n for n in cls.body if getattr(n, "name", "") == "discover_urls")
    assert "return []" in ast.unparse(disc)
    # The navigation-mode render DOES wire the draft's discovery helper up.
    nav = _method_names(_class_of(rendered_fixture))
    assert "discover_product_urls" in nav


def test_entry_points_present_with_the_contracted_signatures(rendered_fixture):
    cls = _class_of(rendered_fixture)
    sigs = {
        n.name: ast.unparse(n.args) if not isinstance(n.args, ast.arguments) else ""
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name in {"discover_urls", "scrape_one"}
    }
    assert set(sigs) == {"discover_urls", "scrape_one"}
    scrape_one = next(
        n for n in _class_of(rendered_fixture).body if getattr(n, "name", "") == "scrape_one"
    )
    assert [a.arg for a in scrape_one.args.args] == ["self", "url"]


# ---------------------------------------------------------------------------
# 2. Unrecognised structure -> None, never a raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   \n\n  ",
        "not python at all {{{",
        "class OnlyAClass:\n    pass\n",
        "x = 1\ny = 2\n",
        "\x00\x01binary\xff",
    ],
)
def test_unrecognised_drafts_return_none_without_raising(bad):
    text, report = _render(bad)
    assert text is None
    assert report["ok"] is False
    assert report["reason"]


def test_garbage_path_returns_none():
    assert dr.render_dagster_module("/nonexistent/nope.py") is None


def test_draft_calling_repo_helper_is_rejected():
    """``src.discovery`` has no equivalent in the client's environment."""
    draft = (
        "import requests\n"
        "from src.discovery import discover_item_urls\n"
        "def fetch_page(url):\n"
        "    r = requests.get(url)\n"
        "    return r.text, r.status_code\n"
        "def discover_product_urls():\n"
        "    return discover_item_urls(None, 'http://x', None, None)\n"
        "def extract_product_from_page(soup, url, status_code, src_url):\n"
        "    return {'title': '', 'url': url, 'src_url': src_url}\n"
    )
    text, report = _render(draft)
    assert text is None
    assert "discover_item_urls" in report["reason"]


def test_transport_returning_raw_response_is_rejected():
    """A shim cannot fake a requests.Response — the LLM path owns that shape."""
    draft = (
        "import requests\n"
        "def _fetch(url):\n"
        "    return requests.get(url)\n"
        "def extract_item_from_product_page(url, src_url):\n"
        "    resp = _fetch(url)\n"
        "    return {'title': resp.text[:10], 'url': url, 'src_url': src_url}\n"
    )
    text, report = _render(draft)
    assert text is None
    assert "_fetch" in report["reason"]


def test_extractor_with_unfillable_argument_is_rejected():
    draft = (
        "import requests\n"
        "def fetch_api(url, params=None):\n"
        "    return requests.get(url, params=params).json()\n"
        "def transform_api_product(record, index, src_url):\n"
        "    return {'title': record.get('n',''), 'src_url': src_url}\n"
    )
    text, report = _render(draft)
    assert text is None
    assert "record" in report["reason"]


def test_report_dict_is_populated_on_success_and_failure():
    ok_report: dict = {}
    ok = dr.render_dagster_module(FIXTURE_DRAFT, "", {"site_slug": "s"}, ok_report)
    assert ok is not None and ok_report["ok"] is True
    assert ok_report["class_name"] == "FixtureWidgetsScraper"
    bad_report: dict = {}
    assert dr.render_dagster_module("}}}", "", {}, bad_report) is None
    assert bad_report["ok"] is False and bad_report["reason"]


def test_describe_rejection_round_trip():
    assert "rendered" in dr.describe_rejection({"ok": True, "shape": "soup_extractor"})
    assert dr.describe_rejection({"ok": False, "reason": "because"}) == "because"
    assert dr.describe_rejection(None) == "no report"


# ---------------------------------------------------------------------------
# 3. Determinism
# ---------------------------------------------------------------------------


def test_two_calls_are_byte_identical():
    a = dr.render_dagster_module(FIXTURE_DRAFT, "", {"site_slug": "s", "job_id": 7})
    b = dr.render_dagster_module(FIXTURE_DRAFT, "", {"site_slug": "s", "job_id": 7})
    assert a is not None and b is not None
    assert a == b


def test_output_carries_no_clock():
    """A timestamp in the output would break byte-determinism and reproducibility."""
    text = dr.render_dagster_module(FIXTURE_DRAFT, "", {"site_slug": "s"})
    assert text is not None
    assert not re.search(r"\d{4}-\d{2}-\d{2}[T_ ]\d{2}:\d{2}", text)
    assert "datetime.now()" not in text  # only inside copied item bodies, if at all


def test_path_input_and_text_input_agree():
    with open(FIXTURE_DRAFT, "r", encoding="utf-8") as fh:
        source = fh.read()
    by_path = dr.render_dagster_module(FIXTURE_DRAFT, "", {"site_slug": "s"})
    by_text = dr.render_dagster_module(
        source, "", {"site_slug": "s", "source_name": "dagster_renderer_draft.py"}
    )
    assert by_path == by_text


def test_renderer_is_llm_free_and_network_free():
    """I12's whole point: no model call on the happy path."""
    with open(dr.__file__, "r", encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("litellm", "openai", "anthropic", "get_small_llm", "ChatOpenAI"):
        assert banned not in src, banned


# ---------------------------------------------------------------------------
# 4. The output imports in a (stubbed) client environment
# ---------------------------------------------------------------------------


def _load_from_source(source: str, name: str):
    module = types.ModuleType(name)
    module.__dict__["__name__"] = name
    module.__dict__["__file__"] = f"/client/{name}.py"
    exec(compile(source, f"/client/{name}.py", "exec"), module.__dict__)
    return module


@pytest.fixture(scope="module")
def stub_client_env():
    """Minimal stand-ins for the client's ``dagster_scraper_base`` + deps."""
    saved = {k: sys.modules.get(k) for k in ("dagster_scraper_base", "bs4", "tls_client")}
    base = types.ModuleType("dagster_scraper_base")

    class BaseScraper:
        def __init__(self, proxy=None, brightdata_proxy=None, bypass=None, log=None):
            self.proxy = proxy
            self.brightdata_proxy = brightdata_proxy
            self.log = log

    class BaseTlsScraper(BaseScraper):
        TLS_CLIENTS = ["chrome_120"]

        def _fetch(self, url, proxy=None):
            raise AssertionError("network stub must not be called by import-time tests")

    base.BaseScraper = BaseScraper
    base.BaseTlsScraper = BaseTlsScraper
    sys.modules["dagster_scraper_base"] = base

    if "bs4" not in sys.modules:
        bs4 = types.ModuleType("bs4")
        bs4.BeautifulSoup = object
        sys.modules["bs4"] = bs4
    if "tls_client" not in sys.modules:
        sys.modules["tls_client"] = types.ModuleType("tls_client")
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_rendered_module_imports_and_exposes_the_class(rendered_fixture, stub_client_env):
    module = _load_from_source(rendered_fixture, "fixtures_example_com_dagster")
    cls = getattr(module, "FixtureWidgetsScraper")
    assert hasattr(cls, "discover_urls") and hasattr(cls, "scrape_one")
    instance = cls(brightdata_proxy="proxy-obj")
    assert callable(instance.discover_urls) and callable(instance.scrape_one)


def test_every_self_call_target_exists_on_the_class(rendered_fixture, stub_client_env):
    """A rewritten ``self.x()`` whose method was dropped would NameError client-side.

    ``self.<attr>`` reads of *data* (``self.brightdata_proxy``) are legal too, so
    they are checked against the base class's instance attributes rather than its
    methods.
    """
    module = _load_from_source(rendered_fixture, "fixtures_example_com_dagster")
    cls = getattr(module, "FixtureWidgetsScraper")
    calls = set(re.findall(r"self\.([A-Za-z_]\w*)\s*\(", rendered_fixture))
    refs = set(re.findall(r"self\.([A-Za-z_]\w*)\b(?!\s*[=(])", rendered_fixture))
    uncalled = {n for n in calls if not callable(getattr(cls, n, None))}
    unread = {n for n in refs if getattr(cls, n, None) is None}
    assert uncalled == set(), uncalled
    assert unread <= set(BASE_INSTANCE_ATTRS), unread - set(BASE_INSTANCE_ATTRS)


def test_staticmethod_helpers_have_no_orphan_self(rendered_fixture, stub_client_env):
    """``@staticmethod`` + a leading ``self`` param would shift every argument."""
    module = _load_from_source(rendered_fixture, "fixtures_example_com_dagster")
    cls = getattr(module, "FixtureWidgetsScraper")
    tree = ast.parse(rendered_fixture)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("__"):
            continue
        deco = [ast.unparse(d) for d in node.decorator_list]
        has_self = bool(node.args.args) and node.args.args[0].arg == "self"
        assert not (deco == ["staticmethod"] and has_self), node.name
    # And the two contract methods really are instance methods.
    for contract in ("discover_urls", "scrape_one"):
        fn = next(
            n
            for n in _class_of(rendered_fixture).body
            if getattr(n, "name", "") == contract
        )
        assert not any(
            "staticmethod" in ast.unparse(d) for d in fn.decorator_list
        ), contract
    assert cls is not None


def test_url_list_render_still_imports(rendered_fixture, stub_client_env):
    """The url_list shape is a second output that must be importable too."""
    text, report = _render(
        FIXTURE_DRAFT, {"site_slug": "s", "input_mode": "url_list"}, ""
    )
    assert text is not None, report
    module = _load_from_source(text, "fixtures_example_com_dagster_url_list")
    assert callable(getattr(module.FixtureWidgetsScraper, "scrape_one"))


# ---------------------------------------------------------------------------
# 5. Semantic fidelity — what the transform must preserve
# ---------------------------------------------------------------------------


def test_selectors_are_copied_not_reauthored(rendered_fixture):
    draft_selectors = _selectors(FIXTURE_DRAFT)
    assert draft_selectors, "fixture lost its selectors"
    rendered = _selectors(rendered_fixture)
    missing = draft_selectors - rendered
    assert missing == set(), f"selectors dropped by the transform: {missing}"


def test_constants_are_carried_across(rendered_fixture):
    rendered_names = {
        t.id
        for node in ast.parse(rendered_fixture).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    for expected in ("SITE_URL", "PRODUCT_LISTING_URL", "PAGE_PARAM_NAME", "MAX_PAGES"):
        assert expected in rendered_names, expected


def test_run_environment_constants_are_not_shipped(rendered_fixture):
    """Our paths / clocks / output naming mean nothing in the client's process."""
    rendered_names = {
        t.id
        for node in ast.parse(rendered_fixture).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    for banned in ("SCRIPT_DIR", "TIMESTAMP", "OUTPUT_FILE", "INPUT_FILE", "LOG_FILE"):
        assert banned not in rendered_names, banned
    assert "src.discovery" not in rendered_fixture


def test_soft_404_and_field_names_are_preserved(rendered_fixture):
    """The item record's field names come out exactly as the draft wrote them."""
    draft_keys = _record_keys(FIXTURE_DRAFT, "extract_product_from_page")
    assert draft_keys, "fixture extractor lost its record keys"
    assert draft_keys <= _dict_keys(rendered_fixture)
    assert "Soft 404: content not found" in _string_literals(rendered_fixture)


def test_parsing_helpers_survive_as_methods(rendered_fixture):
    draft_fns = set(_module_functions(ast.parse(_source(FIXTURE_DRAFT))))
    rendered_methods = _method_names(_class_of(rendered_fixture))
    for fn in ("clean_html", "make_absolute_url", "extract_jsonld",
               "extract_product_from_page", "discover_product_urls"):
        assert fn in draft_fns, fn
        assert fn in rendered_methods, f"{fn} not carried into the dagster class"


def test_pipeline_only_functions_are_not_shipped(rendered_fixture):
    rendered_methods = _method_names(_class_of(rendered_fixture))
    for banned in ("main", "load_urls_from_file", "save_urls_to_file"):
        assert banned not in rendered_methods, banned


# ---------------------------------------------------------------------------
# 6. Real draft/dagster pairs on disk (skipped when the File Master is absent)
# ---------------------------------------------------------------------------


def _real_pairs():
    pairs = []
    for draft in _iter_real_drafts():
        jobs = os.path.dirname(draft)
        job_id = re.fullmatch(r"scraper-(\d+)\.py", os.path.basename(draft)).group(1)
        dagster = os.path.join(jobs, f"dagster-{job_id}.py")
        if os.path.isfile(dagster):
            pairs.append((draft, dagster, job_id))
    return pairs


@pytest.mark.skipif(not _iter_real_drafts(), reason="no File Master artefacts on disk")
def test_real_corpus_recognition_rate_is_reported():
    """Recognition is honest, not aspirational — record what actually renders.

    Everything unrecognised must carry a *reason*, because that reason is what
    the coordinator logs before falling back to the LLM path.
    """
    rendered, rejected = [], []
    for draft in _iter_real_drafts():
        text, report = _render(draft, {"site_slug": "x", "input_mode": "list_page"})
        (rendered if text is not None else rejected).append(
            (draft, report.get("reason", ""))
        )
    assert rendered, "renderer recognises nothing on the real corpus"
    for _, reason in rejected:
        assert reason, f"unexplained rejection for {draft}"


@pytest.mark.skipif(
    not any(
        os.path.isfile(os.path.join(os.path.dirname(d), f"dagster-{j}.py"))
        for d, j in (
            (d, re.fullmatch(r"scraper-(\d+)\.py", os.path.basename(d)).group(1))
            for d in _iter_real_drafts()
        )
    ),
    reason="no real draft/dagster pairs on disk",
)
def test_rendered_output_matches_recorded_semantics():
    """For every recognisable real pair, the renderer preserves the essentials.

    Compared against the *recorded* LLM output: same class-level entry points,
    same selectors, same soft-404 markers.  Byte equality is explicitly NOT
    asserted — the critique records that three recorded quotes-toscrape outputs
    differ by hundreds of cosmetic lines while extracting the same two fields.
    """
    checked = 0
    for draft, dagster_path, job_id in _real_pairs():
        text, report = _render(draft, {"site_slug": "x", "input_mode": "list_page"})
        if text is None:
            continue
        with open(dagster_path, "r", encoding="utf-8") as fh:
            recorded = fh.read()

        assert _import_binding_gate_violations(text) == []
        rendered_methods = _method_names(_class_of(text))
        assert {"discover_urls", "scrape_one"} <= rendered_methods

        # Every selector the draft used survives the transform...
        draft_selectors = _selectors(draft)
        assert draft_selectors - _selectors(text) == set()
        # ...and the recorded output agreed on the same core selectors.
        core = draft_selectors & _selectors(recorded)
        assert core <= _selectors(text)

        # Soft-404 markers are copied, not re-worded.
        for lit in _string_literals(draft):
            if isinstance(lit, str) and lit.lower().startswith("soft 404"):
                assert lit in _string_literals(text), lit

        checked += 1
    assert checked > 0, "no real pair was recognisable — renderer is dead code"


def test_transport_is_bridged_to_base_fetch(rendered_fixture):
    """The one deliberate semantic change: HTTP transport becomes BaseTlsScraper's."""
    cls_source = ast.get_source_segment(rendered_fixture, _class_of(rendered_fixture))
    assert "self._fetch(" in cls_source
    assert "requests.get(" not in cls_source
