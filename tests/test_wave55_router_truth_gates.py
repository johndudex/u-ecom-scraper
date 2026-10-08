"""[wave-55 R6a / M1+M2] The router must gate on the artifact, not the LLM's
story about it.

Audit 2026-10-05, two Medium defects in ``route_after_testing``:

M1 (#13) — ``_core_field_zero_coverage`` read the tester LLM's own
``results.field_coverage`` block. Job 1219 (aleensabbagh): 10/10 output rows
with ``price=None`` while ``test_report.json`` recorded price
``{count: 19, coverage: "100%", status: "CORRECT", quality: "excellent"}``.
The honest site wall (a couture "book an appointment" collection with no
price anywhere) shipped as a PASS because the gate trusted the claim.

M2 (#15) — dead-store evidence did not force the job off the execution path.
Job 1205 (johnnybigg.com.au): product_analyzer proved the storefront dead via
3 access paths; the tester wrote ``site_status: "DEAD_STORE_SUNSET"`` +
``products []`` — and then ``overall_assessment: PASS, confidence 0.8,
ready_for_execution: true``. The cascade read the zero-item PASS as a code
bug, logged ``[CASCADE] code-fix`` on a dead domain and burned 81 minutes
(the writer grepped ``all_tiers_blocked`` ~18x) before an identical-draft
guard killed it.

Fixes (both in ``webapp/agents/nodes/route_after_testing.py``):

1. M1 — core-field coverage is computed FROM THE OUTPUT ARTIFACT rows
   (null-aware: a core field with zero non-empty values across the live rows
   is a coverage failure no matter what the LLM claimed). The LLM's
   ``field_coverage`` block survives only as a fallback for runs whose rows
   are unreadable — it can only ever be as wrong as before, never more, and
   it can no longer paper over a null column.
2. M2 — a tester dead-site verdict with no real items on disk short-circuits
   to the honest-fail terminal (``cleanup`` under skip_approvals,
   ``human_approval`` otherwise) instead of entering the fix cascade.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest ../tests/test_wave55_router_truth_gates.py -q"
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

# `agents.nodes.__init__` re-exports the route FUNCTION under the module's own
# name, so the submodule has to come out of sys.modules explicitly.
rat = importlib.import_module("agents.nodes.route_after_testing")

# The finalize-side GAP-1/GAP-3 stamps live in the same round; the module is
# imported lazily so a collection-time import failure names the right file.
MAX_TEST_RETRIES = rat.MAX_TEST_RETRIES
tasks_module = importlib.import_module("scraper.tasks")

# A row set the item-shape floor accepts (price carries data) with the
# availability/currency columns dead on every row — the shape that reaches the
# exhausted-partial arm with `missing_core` naming a field.
_PARTIAL_NULL_ROWS = [
    {
        "title": f"Look {i}",
        "price": f"{19 + i}.00",
        "availability": None,
        "currency": "",
        "url": f"https://aleensabbagh.com/CollectionProductDetails/Look-{i:02d}",
        "src_url": "https://aleensabbagh.com/collections/book-an-appointment",
    }
    for i in range(1, 4)
]


# ── helpers ─────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def mock_project_root(tmp_path):
    old = os.environ.get("PROJECT_ROOT")
    os.environ["PROJECT_ROOT"] = str(tmp_path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PROJECT_ROOT", None)
        else:
            os.environ["PROJECT_ROOT"] = old


def _stage_output(tmp_path, slug, rows, *, phase=None, name="output_sample.json"):
    """Write one output artifact into the staged workspace, route_after_testing
    style (``{"products": [...]}``; ``metadata.phase`` tags a --discover-only
    file)."""
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True, exist_ok=True)
    payload: dict = {"metadata": {}, "products": rows}
    if phase:
        payload["metadata"]["phase"] = phase
    (ws / name).write_text(json.dumps(payload), encoding="utf-8")
    return ws / name


def _fm(monkeypatch, keys, payloads):
    """Stub the File Master client to `(keys, payloads)`.

    Same reload-and-pin dance tests/test_wave55_rescue_provenance.py documents:
    a string-path monkeypatch on `src.artifacts` loses to the collection-time
    stub the full suite installs, so the real module is re-loaded and patched
    in place (sys.modules AND the package attribute).
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    monkeypatch.setattr(real, "list_keys", lambda prefix="": keys)
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))
    monkeypatch.setattr(real, "exists", lambda key: key in payloads)
    # [wave-55 R6b] the finalize row gates rewrite a changed artifact through
    # `artifacts.write_json` — without this the suite PUTs test rows into the
    # real File Master (a leaked `scrapers/x-example/output_…json` key).
    monkeypatch.setattr(real, "write", lambda key, data: len(data))
    monkeypatch.setattr(real, "read_json",
                        lambda key: json.loads(payloads[key]) if key in
                        payloads else (_ for _ in ()).throw(ValueError(key)))
    return payloads


def _freeze_state(monkeypatch, state):
    """Freeze `_finalize_job`'s checkpoint read to `state` (wave-40 chain)."""
    from types import SimpleNamespace

    class _FakeGraph:
        def get_state(self, config):
            return SimpleNamespace(values=state)

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def build_graph(self):
            return _FakeGraph()

        @staticmethod
        def get_config(thread_id):
            return {"configurable": {"thread_id": thread_id}}

    monkeypatch.setattr(tasks_module, "LangGraphService", _FakeService)


# The 1219 shape: price/availability/currency null on every row, title real.
_NULL_ROWS = [
    {
        "title": f"Look {i}",
        "price": None,
        "availability": None,
        "currency": "",
        "url": f"https://aleensabbagh.com/CollectionProductDetails/Look-{i:02d}",
    }
    for i in range(1, 4)
]

# The tester LLM's story about the same run (job 1219's field_coverage block).
_LLM_ALL_CORRECT = {
    "results": {
        "successful_extractions": 19,
        "field_coverage": {
            "title": {"count": 19, "coverage": "100%", "status": "CORRECT"},
            "price": {"count": 19, "coverage": "100%", "status": "CORRECT",
                      "quality": "excellent"},
            "availability": {"count": 19, "coverage": "100%", "status": "CORRECT"},
            "currency": {"count": 19, "coverage": "100%", "status": "CORRECT"},
        },
    }
}


# ── M1: null-aware core-field coverage ──────────────────────────────────────


class TestNullAwareCoreCoverage:
    def _state(self, report, slug="aleensabbagh-com"):
        return {
            "site_slug": slug,
            "page_type": "product",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "test_report": report,
            "input_mode": "list_page",
        }

    def test_null_column_fails_even_when_llm_claims_100_percent(self, tmp_path):
        """The 1219 defect: every row's price is null and the LLM says 100%.
        The artifact wins."""
        _stage_output(tmp_path, "aleensabbagh-com", _NULL_ROWS)
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(_LLM_ALL_CORRECT, self._state({})) \
                == "price"

    def test_no_llm_coverage_block_at_all_still_fails_on_null_rows(self, tmp_path):
        """Null-aware means the gate no longer needs the LLM to have noticed."""
        _stage_output(tmp_path, "aleensabbagh-com", _NULL_ROWS)
        report = {"results": {"successful_extractions": 3}}
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(report, self._state({})) == "price"

    def test_populated_column_silences_an_llm_missing_claim(self, tmp_path):
        """Inverse direction: the artifact is truth, so a hysterical LLM
        MISSING claim cannot force a remap on a column that is populated."""
        rows = [
            {"title": f"P{i}", "price": f"{19 + i}.00", "currency": "USD",
             "availability": "in stock",
             "url": f"https://shop.example.com/products/p{i}",
             "src_url": "https://shop.example.com/collections/all"}
            for i in range(1, 4)
        ]
        _stage_output(tmp_path, "populated-com", rows)
        report = {
            "results": {
                "successful_extractions": 3,
                "field_coverage": {
                    "price": {"count": 0, "coverage": "0%", "status": "MISSING"},
                },
            }
        }
        state = self._state(report, slug="populated-com")
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(report, state) is None

    def test_dead_rows_are_not_judged(self, tmp_path):
        """A sample whose every row is a dead URL proves nothing about the
        mapping (the tester's own schema excludes dead URLs from quality)."""
        rows = [
            {"title": f"P{i}", "price": None, "status_code": 404,
             "url": f"https://shop.example.com/products/p{i}"}
            for i in range(1, 4)
        ]
        _stage_output(tmp_path, "deadrows-com", rows)
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage({}, self._state({}, "deadrows-com")) \
                is None

    def test_discovery_stub_output_is_not_extraction_truth(self, tmp_path):
        """A --discover-only artifact holds URL stubs — it must not read as a
        0%-price extraction run."""
        _stage_output(
            tmp_path, "stubbed-com",
            [{"url": "https://shop.example.com/p/1", "src_url": "listing"}],
            phase="discovery",
        )
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage({}, self._state({}, "stubbed-com")) \
                is None

    def test_unreadable_rows_fall_back_to_the_llm_claims(self, tmp_path):
        """No artifact → the legacy LLM-claim check still runs (it can only be
        as wrong as before; the gate must never go blind on a read miss)."""
        _stage_output(tmp_path, "noread-com", [], name="output_empty.json")
        report = {
            "results": {
                "successful_extractions": 4,
                "field_coverage": {
                    "price": {"count": 0, "coverage": "0%", "status": "MISSING"},
                },
            }
        }
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(report, self._state({}, "noread-com")) \
                == "price"

    def test_core_field_order_follows_the_registry_not_alphabet(self, tmp_path):
        """The reported field is the first dead core field in the content
        type's declared order (price before availability/currency) — the log
        line must name the field the user asked about."""
        _stage_output(tmp_path, "order-com", _NULL_ROWS)
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage({}, self._state({}, "order-com")) \
                == "price"

    def test_an_honest_partial_column_set_still_ships(self, tmp_path):
        """GAP-1b: the gate judges the fields the JOB asked for, not the whole
        registry. A title+price+url job run against a site that publishes no
        availability/currency produces rows with no such keys at all — that is
        an honest extraction, not a 0%-coverage mapping bug, and burning the
        writer slot on it (then failing exhausted) is the 1219 class in
        reverse."""
        rows = [
            {"title": f"Look {i}", "price": f"{19 + i}.00",
             "url": f"https://aleensabbagh.com/CollectionProductDetails/Look-{i:02d}"}
            for i in range(1, 6)
        ]
        _stage_output(tmp_path, "aleensabbagh-com", rows)
        report = {
            "overall_assessment": "PASS",
            "confidence_score": 0.93,
            "ready_for_execution": True,
            "issues": [],
            "results": {"successful_extractions": 5},
            "phases_tested": {"phase1_discovery": True,
                              "phase2_extraction": True},
        }
        state = self._state(report)
        state["target_fields"] = ["title", "price", "url"]
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(report, state) is None
            assert rat.route_after_testing(state) == "field_confirmation"

    def test_a_requested_optional_field_stays_on_the_medium_remap_path(
            self, tmp_path):
        """Round-7 GAP-1b: the judged set is the job's CONTRACT ∩ the registry
        core — not the union wholesale. A requested OPTIONAL field the site
        never publishes (`description`) must ride the T2.8 medium remap issue;
        under the union-judged gate it escalated to high severity, vetoed the
        PASS and failed the job (a fleet-facing regression against the file's
        own contract)."""
        rows = [
            {"title": f"P{i}", "price": f"{19 + i}.00", "currency": "USD",
             "availability": "in stock",
             "url": f"https://shop.example.com/products/p{i}",
             "src_url": "https://shop.example.com/collections/all"}
            for i in range(1, 4)
        ]
        _stage_output(tmp_path, "shop-example-com", rows)
        report = {
            "overall_assessment": "PASS",
            "confidence_score": 0.93,
            "ready_for_execution": True,
            "issues": [],
            "results": {
                "successful_extractions": 3,
                "field_coverage": {
                    "description": {"count": 0, "coverage": "0%",
                                    "status": "MISSING"},
                },
            },
            "phases_tested": {"phase1_discovery": True,
                              "phase2_extraction": True},
        }
        state = self._state(report, slug="shop-example-com")
        state["target_fields"] = ["title", "price", "description", "url"]
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(report, state) is None, (
                "a requested optional field was judged as strict core"
            )
            assert rat._requested_field_zero_coverage(report, state) == [
                "description"]
            assert rat.route_after_testing(state) == "field_confirmation"

    def test_a_declared_core_set_cannot_drop_a_requested_registry_core_field(
            self, tmp_path):
        """Round-8 GAP-2, the router half: cfg_core must INTERSECT the
        requested list, never REPLACE it. A config declaring core
        ``['title','price']`` on a title+price+url job whose rows leave
        ``url`` empty must still fail the gate on ``url`` — under the
        replace-shape the strict set silently shrinks and the dead identity
        column ships."""
        rows = [
            {"title": f"P{i}", "price": f"{19 + i}.00", "url": None,
             "availability": "in stock", "currency": "USD",
             "src_url": "https://cfg-core-com/collections/all"}
            for i in range(1, 4)
        ]
        _stage_output(tmp_path, "cfg-core-com", rows)
        report = {"results": {"successful_extractions": 3}}
        state = self._state(report, slug="cfg-core-com")
        state["target_fields"] = ["title", "price", "url"]
        state["content_type_config"] = {
            "content_type": "product", "output_key": "products",
            "core_field_names": ["title", "price"],
        }
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(report, state) == "url"

    def test_router_rejects_a_pass_the_rows_refute(self, tmp_path):
        """End-to-end 1219: PASS / 0.9 / ready-true resting entirely on
        field_coverage claims the artifact contradicts must not reach
        execution — the null column escalates to high severity, which both
        rejects the PASS gate and vetoes the ground-truth override."""
        _stage_output(tmp_path, "aleensabbagh-com", _NULL_ROWS)
        report = dict(_LLM_ALL_CORRECT)
        report.update({
            "overall_assessment": "PASS",
            "confidence_score": 0.9,
            "ready_for_execution": True,
            "issues": [],
            "phases_tested": {"phase1_discovery": True, "phase2_extraction": True},
        })
        state = {
            "site_slug": "aleensabbagh-com",
            "page_type": "product",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "input_mode": "list_page",
            "test_retry_count": 0,
            "skip_approvals": True,
            "probe_result": {},
            "scraper_analysis": {"strategy": "http_navigation"},
            "test_report": report,
        }
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(state)
        assert dest != "field_confirmation"
        assert dest in ("code_writer", "product_analyzer", "scraper_analyzer")


# ── M2: dead-store short-circuit ────────────────────────────────────────────


def _dead_report(**over):
    """Job 1205's tester report shape: a proven-dead domain, 0 extractions,
    and a PASS/0.8/ready-true verdict riding on top."""
    report = {
        "site_slug": "johnnybigg-com-au",
        "site_status": "DEAD_STORE_SUNSET",
        "dead_site_assessment": {
            "site_status": "DEAD_STORE_SUNSET",
            "summary": (
                "www.johnnybigg.com.au no longer serves any product pages: "
                "every request redirects to a 'cease trading' landing page."
            ),
        },
        "results": {
            "successful_extractions": 0,
            "failed_extractions": 0,
            "skipped_dead_urls": 1,
        },
        "issues": [],
        "overall_assessment": "PASS",
        "confidence_score": 0.8,
        "ready_for_execution": True,
    }
    report.update(over)
    return report


def _dead_state(report=None, **over):
    state = {
        "site_slug": "johnnybigg-com-au",
        "input_mode": "list_page",
        "skip_approvals": True,
        "test_retry_count": 0,
        "probe_result": {},
        "scraper_analysis": {"strategy": "http_navigation"},
        "test_report": report if report is not None else _dead_report(),
    }
    state.update(over)
    return state


class TestDeadStoreShortCircuit:
    def test_dead_store_pass_routes_to_cleanup_not_a_fix_cycle(self, tmp_path):
        """The 1205 defect: PASS/0.8/ready-true on a dead store with an empty
        output must terminate honestly, not enter the cascade."""
        _stage_output(tmp_path, "johnnybigg-com-au", [])
        with mock_project_root(tmp_path):
            assert rat.route_after_testing(_dead_state()) == "cleanup"

    def test_dead_store_with_fix_budget_left_owes_no_writer_cycle(self, tmp_path):
        """retry 0/2 with no remediation: the burn was a code-fix arm on a
        domain that has nothing left to fix."""
        _stage_output(tmp_path, "johnnybigg-com-au", [])
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(_dead_state())
        assert dest not in ("code_writer", "scraper_analyzer", "code_tester")
        assert dest == "cleanup"

    def test_dead_store_only_in_dead_site_assessment_also_vetoes(self, tmp_path):
        """Some testers stamp the verdict only inside dead_site_assessment."""
        report = _dead_report()
        report.pop("site_status")
        _stage_output(tmp_path, "johnnybigg-com-au", [])
        with mock_project_root(tmp_path):
            assert rat.route_after_testing(_dead_state(report)) == "cleanup"

    def test_dead_store_with_a_human_in_the_loop_asks_the_human(self, tmp_path):
        _stage_output(tmp_path, "johnnybigg-com-au", [])
        state = _dead_state(skip_approvals=False)
        with mock_project_root(tmp_path):
            assert rat.route_after_testing(state) == "human_approval"

    def test_real_items_on_disk_outrank_the_dead_store_claim(self, tmp_path):
        """Ground truth beats the tester's story in both directions: an
        extraction that DID pull real products is not killed by a dead-site
        stamp."""
        rows = [
            {"title": f"Jean {i}", "price": f"{89 + i}.00", "currency": "AUD",
             "availability": "in stock",
             "url": f"https://www.johnnybigg.com/au/p/jean-{i}",
             "src_url": "https://www.johnnybigg.com/au/jeans"}
            for i in range(1, 4)
        ]
        _stage_output(tmp_path, "johnnybigg-com-au", rows)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(_dead_state())
        assert dest != "cleanup"
        assert dest == "field_confirmation"

    def test_zero_items_without_a_dead_signature_keeps_the_cascade(self, tmp_path):
        """The veto is keyed on the dead-site evidence, not on emptiness — an
        ordinary 0-item run with fix budget left must still be fixable."""
        report = _dead_report()
        report.pop("site_status")
        report.pop("dead_site_assessment")
        _stage_output(tmp_path, "johnnybigg-com-au", [])
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(_dead_state(report))
        assert dest not in ("cleanup", "human_approval")

    def test_dead_store_evidence_helper_reads_both_stamps(self):
        assert rat._dead_store_evidence(_dead_report()) == "dead_store_sunset"
        assert rat._dead_store_evidence({"site_status": "live"}) == ""
        assert rat._dead_store_evidence({}) == ""
        assert rat._dead_store_evidence(None) == ""


# ── GAP-1a: the shared null-column row predicate ────────────────────────────


class TestNullCoreColumnsHelper:
    """``null_core_columns`` is the ONE row predicate; both the router gate and
    the finalize/partner stamps consume it instead of re-deriving the loop."""

    def test_lists_every_core_column_empty_on_all_rows_in_registry_order(self):
        # _NULL_ROWS carry no src_url key either — the full core set is judged,
        # bookkeeping keys included (the M1 tightening).
        assert rat.null_core_columns(_NULL_ROWS, "product") == [
            "price", "availability", "currency", "src_url",
        ]

    def test_core_field_names_override_wins_over_the_registry(self):
        rows = [{"title": "Eng", "company": "Acme", "location": "Leeds"}]
        # job_posting's registry set has no `price` to mis-name.
        assert rat.null_core_columns(rows, "job_posting") == [
            "description", "url",
        ]
        assert rat.null_core_columns(
            rows, "job_posting",
            core_field_names=["company", "location", "description"],
        ) == ["description"]

    # ── round-8 GAP-2: the ceiling semantics, pinned at the helper itself ───
    #
    # `null_core_columns`' `core_field_names` parameter is only the delivery
    # channel; the SEMANTICS live in `core_judged_set`, which no test reached
    # directly. A regression to replace-shape (cfg_core replacing the registry
    # instead of widening it) would silently drop `url` from the strict set.

    def test_core_judged_set_never_shrinks_a_requested_registry_core_field(self):
        from src.field_mapping import core_judged_set

        # `url` is product registry core AND requested; a narrow declared core
        # set may not drop it from the strict set.
        assert core_judged_set(
            ["title", "price", "url"], "product", cfg_core=["title", "price"],
        ) == ["title", "price", "url"]

    def test_core_judged_set_does_not_promote_a_requested_optional_field(self):
        from src.field_mapping import core_judged_set

        # `description` is not product core and not in the declared set — it
        # stays a T2.8 medium remap issue, never promoted to strict core.
        assert core_judged_set(
            ["title", "description"], "product", cfg_core=["title", "price"],
        ) == ["title"]

    def test_core_judged_set_with_nothing_requested_judges_the_cfg_set(self):
        from src.field_mapping import core_judged_set

        # A job that asked nothing is judged against the core its config
        # declares, not the whole registry.
        assert core_judged_set([], "product", cfg_core=["title", "price"]) == [
            "title", "price"]

    def test_a_column_with_one_populated_row_is_not_null(self):
        rows = list(_NULL_ROWS) + [
            {"title": "Priced", "price": "245.00", "availability": "in stock",
             "currency": "AED", "url": "https://aleensabbagh.com/p/1",
             "src_url": "https://aleensabbagh.com/collections/all"},
        ]
        assert rat.null_core_columns(rows, "product") == []

    def test_no_live_rows_prove_nothing(self):
        # dead rows are excluded from judgement (the tester's own schema does
        # the same), and an empty row set can never name a null column.
        dead = [dict(r, status_code=404) for r in _NULL_ROWS]
        assert rat.null_core_columns(dead, "product") == []
        assert rat.null_core_columns([], "product") == []

    def test_a_digit_free_price_string_is_a_null_column_for_the_router(self):
        # [GAP-C] 'Book an appointment' is a producer's spelling of 'no price',
        # so the coverage gate must read it as the same empty column a null
        # price is — the gate and the finalize stamp share one predicate.
        assert rat.null_core_columns(
            [{"title": "t", "price": "Book an appointment",
              "url": "https://x.example/p/1"}],
            "product", ["title", "price", "url"],
        ) == ["price"]

    def test_a_currency_word_is_never_a_dead_column(self):
        # [GAP-C] the priceish rule is price-scoped on purpose: currency is a
        # digit-free string that is REAL data, so it must not start reading as
        # an empty column just because it carries no digit.
        assert rat.null_core_columns(
            [{"title": "t", "currency": "USD", "url": "https://x/p/1"}],
            "product", ["title", "currency", "url"],
        ) == []

    def test_artifact_coverage_reports_the_shared_predicate_s_first_dead_field(
            self, tmp_path):
        """`_artifact_core_coverage` delegates — it must agree with the helper
        about WHICH column died first, not re-derive its own order."""
        rows = [
            {"title": f"P{i}", "price": f"{19 + i}.00", "availability": None,
             "currency": "", "url": f"https://populated-com/p/{i}",
             "src_url": "https://populated-com/collections/all"}
            for i in range(1, 4)
        ]
        _stage_output(tmp_path, "populated-com", rows)
        with mock_project_root(tmp_path):
            readable, dead = rat._artifact_core_coverage(
                {"site_slug": "populated-com", "page_type": "product"}, [
                    "title", "price", "availability", "currency", "url",
                    "src_url",
                ])
        assert readable is True
        assert dead == rat.null_core_columns(rows, "product")[0] == "availability"


# ── GAP-1b: the exhausted-partial arm must not ship a null column ───────────


class TestExhaustedPartialArmHonoursMissingCore:
    def _state(self, report, **over):
        state = {
            "site_slug": "aleensabbagh-com",
            "page_type": "product",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "input_mode": "list_page",
            "test_retry_count": MAX_TEST_RETRIES,
            "skip_approvals": False,
            "probe_result": {},
            "scraper_analysis": {"strategy": "http_navigation"},
            "test_report": report,
        }
        state.update(over)
        return state

    def _report(self):
        # A partial-credit verdict: the tester saw the rows and scored them
        # 0.6 — below the PASS bar, above MIN_CONFIDENCE_PARTIAL.
        return {
            "overall_assessment": "NEEDS_FIXES",
            "confidence_score": 0.6,
            "ready_for_execution": False,
            "issues": [],
            "results": {"successful_extractions": 3},
        }

    def test_exhausted_partial_arm_does_not_ship_a_null_core_column(
            self, tmp_path):
        """Audit clause 2 of fix #13: `confidence >= MIN_CONFIDENCE_PARTIAL and
        _scraper_produced_valid_output(state)` returned field_confirmation
        WITHOUT consulting missing_core — the same null column the arms below
        veto on (`_exhausted_cleanup_redirect`) sailed through the one arm that
        scores a run as a valid partial output."""
        _stage_output(tmp_path, "aleensabbagh-com", _PARTIAL_NULL_ROWS)
        with mock_project_root(tmp_path):
            assert rat._core_field_zero_coverage(self._report(),
                                                 self._state(self._report()))
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(self._report()))
        assert dest != "field_confirmation", (
            "the exhausted-partial arm shipped a null core column as a valid "
            "partial output"
        )
        assert dest == "human_approval"

    def test_intake_rescue_arm_does_not_ship_a_null_core_column(
            self, tmp_path):
        """The intake lane (GAP-1a): the veto above only stops the FIRST
        exhausted-partial return. Under skip_approvals the very next arm —
        `cascade-exhausted-rescue`, guarded on `_scraper_has_real_items`
        alone — routes the same null-column run to field_confirmation, which
        auto-approves to run_execution. Rows carry title+price+url, so the
        item-shape floor is satisfied and the column still ships."""
        _stage_output(tmp_path, "aleensabbagh-com", _PARTIAL_NULL_ROWS)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(
                self._report(), skip_approvals=True, input_mode="url_list"))
        assert dest != "field_confirmation", (
            "the intake rescue arm shipped a null core column straight to "
            "execution (the 1219/1227 blast radius)"
        )

    def test_no_report_final_attempt_does_not_ship_a_null_core_column(
            self, tmp_path):
        """Round-7 GAP-1a (arm 1 of 4): the no-report FINAL rescue is guarded on
        `_scraper_has_real_items` alone and sits ABOVE the `missing_core`
        computation — a null core column with no verdict at all still shipped
        to field_confirmation (→ run_execution under skip_approvals)."""
        _stage_output(tmp_path, "aleensabbagh-com", _PARTIAL_NULL_ROWS)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(
                None, test_retry_count=rat.FINAL_RETRY_SENTINEL))
        assert dest != "field_confirmation", (
            "the no-report FINAL rescue shipped a null core column"
        )

    def test_no_report_exhausted_does_not_ship_a_null_core_column(
            self, tmp_path):
        """Round-7 GAP-1a (arm 2 of 4): the no-report retries-exhausted rescue —
        literally a 'retries exhausted' arm — same single guard, same ship."""
        _stage_output(tmp_path, "aleensabbagh-com", _PARTIAL_NULL_ROWS)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(
                None, test_retry_count=rat.MAX_TEST_RETRIES))
        assert dest != "field_confirmation", (
            "the no-report exhausted rescue shipped a null core column"
        )

    def test_access_wall_rescue_does_not_ship_a_null_core_column(
            self, tmp_path):
        """Round-7 GAP-1a (arm 3 of 4): the access-wall ×2 ground-truth rescue
        is guarded on `_scraper_has_real_items` alone even though `missing_core`
        is already in scope there — the wall terminal is the honest arm."""
        _stage_output(tmp_path, "aleensabbagh-com", _PARTIAL_NULL_ROWS)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(
                self._report(), access_wall_cycles=2))
        assert dest != "field_confirmation", (
            "the access-wall rescue shipped a null core column"
        )

    def test_final_attempt_rescue_does_not_ship_a_null_core_column(
            self, tmp_path):
        """Round-7 GAP-1a (arm 4 of 4): the `final-attempt-rescue` arm guards on
        `_scraper_has_real_items` alone under skip_approvals — the same
        one-line hole the intake rescue arm had, for class closure."""
        _stage_output(tmp_path, "aleensabbagh-com", _PARTIAL_NULL_ROWS)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(
                self._report(), test_retry_count=rat.FINAL_RETRY_SENTINEL,
                skip_approvals=True, input_mode="url_list"))
        assert dest != "field_confirmation", (
            "the final-attempt rescue shipped a null core column"
        )

    def test_a_populated_column_still_takes_the_partial_arm(self, tmp_path):
        """The veto is keyed on the null column, not on exhaustion — a run
        whose core columns carry data keeps the partial-credit route."""
        rows = [
            dict(r, availability="in stock", currency="AED")
            for r in _PARTIAL_NULL_ROWS
        ]
        _stage_output(tmp_path, "aleensabbagh-com", rows)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(self._state(self._report()))
        assert dest == "field_confirmation"


# ── GAP-2a: a negated dead-site value is not dead-site evidence ─────────────


class TestDeadStoreNegationGuard:
    def test_negated_values_are_not_dead_site_evidence(self):
        assert rat._dead_store_evidence(
            {"dead_site_assessment": {"status": "not_dead"}}) == ""
        assert rat._dead_store_evidence({"site_status": "not dead"}) == ""
        assert rat._dead_store_evidence(
            {"site_status": "dead_store_ruled_out"}) == ""
        assert rat._dead_store_evidence({"site_status": "no_dead_store"}) == ""
        assert rat._dead_store_evidence({"site_status": "alive"}) == ""
        assert rat._dead_store_evidence({"site_status": "active"}) == ""
        assert rat._dead_store_evidence({"site_status": "live_site"}) == ""
        assert rat._dead_store_evidence({"site_status": "operational"}) == ""

    def test_a_real_dead_verdict_still_fires(self):
        assert rat._dead_store_evidence(
            {"site_status": "DEAD_STORE_SUNSET"}) == "dead_store_sunset"
        assert rat._dead_store_evidence(
            {"dead_site_assessment": {"site_status": "DOMAIN_PARKED"}}) == (
            "domain_parked")

    def test_a_negated_dead_site_does_not_kill_a_live_run(self, tmp_path):
        """The end-to-end harm: a tester that writes `not_dead` must not route
        a live site's run to the honest-fail terminal."""
        _stage_output(tmp_path, "johnnybigg-com-au", [])
        report = {
            "site_status": "not_dead",
            "dead_site_assessment": {"status": "not_dead",
                                     "summary": "store reachable"},
            "results": {"successful_extractions": 0},
            "issues": [],
            "overall_assessment": "NEEDS_FIXES",
            "confidence_score": 0.4,
            "ready_for_execution": False,
        }
        state = _dead_state(report)
        with mock_project_root(tmp_path):
            dest = rat.route_after_testing(state)
        assert dest != "cleanup"
        assert dest != "human_approval"


# ── GAP-2b: the tester prompt must define the fields the router reads ───────


class TestTesterPromptNamesTheDeadSiteContract:
    """Prod 1205 emitted DEAD_STORE_SUNSET only because the LLM improvised it —
    the prompt's output schema defined neither field, so the router's veto
    fired only when the model happened to pick a matching substring."""

    def test_output_schema_defines_both_dead_site_fields(self):
        text = _tester_prompt_text()
        assert '"site_status"' in text
        assert '"dead_site_assessment"' in text
        assert '"summary"' in text

    def test_the_dead_vocabulary_is_enumerated(self):
        text = _tester_prompt_text()
        for token in ("DEAD_STORE_SUNSET", "DEAD_STORE_CLOSED", "DOMAIN_PARKED",
                      "OUT_OF_BUSINESS"):
            assert token in text, f"{token} is not in the tester's vocabulary"

    def test_the_dead_site_rule_forces_a_fail(self):
        text = _tester_prompt_text()
        assert ">= 2 independent access methods" in text
        assert "`overall_assessment` to `FAIL`" in text
        assert "`ready_for_execution` to `false`" in text
        assert "Never report a dead domain as `PASS`" in text


def _tester_prompt_text() -> str:
    path = os.path.join(ROOT, ".opencode", "agents", "code-tester.md")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── GAP-1c / GAP-3: the finalize-side stamps ────────────────────────────────


def _null_price_rows(n):
    """`job.output_file`'s 1219 shape: every row real-shaped, every price null."""
    return [
        {"title": f"Look {i}", "price": None, "availability": None,
         "currency": "", "url": f"https://x.example/Look-{i:02d}",
         "src_url": "https://x.example/collections/book-an-appointment"}
        for i in range(1, n + 1)
    ]


class TestFinalizeMissingCoreStamp:
    """A COMPLETED row whose core column is null on every record must say so —
    today it lands COMPLETED with zero record of why (audit fix #13 clause 2)."""

    @pytest.mark.django_db
    def test_completed_stamp_records_the_null_core_column(
            self, monkeypatch, tmp_path):
        from django.conf import settings
        from django.utils import timezone

        from scraper import tasks
        from scraper.models import ScrapeJob

        key = "scrapers/x-example/output_2026-09-20_050000_000001_3.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"products": _null_price_rows(10)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
            page_type="product", input_mode="url_list",
            started_at=timezone.now() - timezone.timedelta(hours=1))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "url_list",
            "execution_status": "SUCCESS", "product_count": 10,
            "output_file": key, "test_report": {}})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "MISSING-CORE price" in (job.error_message or "")
        assert "10/10 rows empty" in (job.error_message or "")

    @pytest.mark.django_db
    def test_soft_time_limit_stamp_records_it_too(self, monkeypatch, tmp_path):
        """`finalize_from_artifacts` is the SECOND COMPLETED stamp (soft-time-
        limit kills) — the same null column must reach the row there."""
        from django.conf import settings
        from django.utils import timezone

        from scraper import tasks
        from scraper.models import ScrapeJob, Step

        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_7.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"products": _null_price_rows(4)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
            page_type="product", input_mode="url_list", output_file=key,
            started_at=now - timezone.timedelta(hours=1))
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "MISSING-CORE price" in (job.error_message or "")

    @pytest.mark.django_db
    def test_a_populated_output_is_never_stamped(self, monkeypatch, tmp_path):
        from django.conf import settings
        from django.utils import timezone

        from scraper import tasks
        from scraper.models import ScrapeJob

        key = "scrapers/x-example/output_2026-09-20_050000_000001_3.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"products": _null_price_rows(3)})})
        # the same file with real prices
        _fm(monkeypatch, [key], {key: json.dumps({"products": [
            dict(r, price=f"{19 + i}.00", availability="in stock",
                 currency="AED")
            for i, r in enumerate(_null_price_rows(3), start=1)]})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
            page_type="product", input_mode="url_list",
            started_at=timezone.now() - timezone.timedelta(hours=1))
        _freeze_state(monkeypatch, {
            "site_slug": "x-example", "input_mode": "url_list",
            "execution_status": "SUCCESS", "product_count": 3,
            "output_file": key, "test_report": {}})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "MISSING-CORE" not in (job.error_message or "")


    @pytest.mark.django_db
    def test_a_job_that_never_asked_for_price_is_not_stamped(
            self, monkeypatch, tmp_path):
        """GAP-1c: `finalize_from_artifacts` is the soft-time-limit COMPLETED
        stamp and it must read the job's own schema the way `_finalize_job`
        does — a job whose target_fields omit price is not missing price, and
        the two stamps must not disagree about that."""
        from django.conf import settings
        from django.utils import timezone

        from scraper import tasks
        from scraper.models import ScrapeJob, Step

        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_9.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"products": _null_price_rows(4)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
            page_type="product", input_mode="url_list", output_file=key,
            target_fields=["title", "url"],
            started_at=now - timezone.timedelta(hours=1))
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_COMPLETED
        assert "MISSING-CORE" not in (job.error_message or ""), (
            "a job that never asked for price was MISSING-CORE-stamped by the "
            "soft-time-limit stamp"
        )

    @pytest.mark.django_db
    def test_a_discovery_payload_is_not_stamped(self, monkeypatch, tmp_path):
        """GAP-1c: `_output_file_rows` must refuse a --discover-only artifact —
        the router's own reader does (`skip_discovery=True`) — or the URL stubs
        read as a null-core extraction run."""
        from django.conf import settings

        from scraper import tasks
        from scraper.models import ScrapeJob

        key = "scrapers/x-example/output_2026-09-20_050000_000001_11.json"
        _fm(monkeypatch, [key], {key: json.dumps({
            "metadata": {"phase": "discovery"},
            "products": [{"url": f"https://x.example/p/{i}",
                          "src_url": "https://x.example/collections/all"}
                         for i in range(1, 5)],
        })})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_COMPLETED,
            page_type="product", input_mode="url_list", output_file=key)
        assert tasks._missing_core_stamp(job) == ""


    @pytest.mark.django_db
    def test_a_requested_optional_field_is_never_stamped(
            self, monkeypatch, tmp_path):
        """Round-7 GAP-1b finalize twin: the stamp must judge the SAME set the
        router does (contract ∩ registry core), so the row and the gate cannot
        disagree about which column died. A job that asked for
        title+price+description+url over honest rows — every registry-core
        column populated, no `description` key at all — is not MISSING-CORE
        anything."""
        from django.conf import settings

        from scraper import tasks
        from scraper.models import ScrapeJob

        rows = [
            {"title": f"P{i}", "price": f"{19 + i}.00", "currency": "USD",
             "availability": "in stock",
             "url": f"https://x.example/products/p{i}",
             "src_url": "https://x.example/collections/all"}
            for i in range(1, 4)
        ]
        key = "scrapers/x-example/output_2026-09-20_050000_000001_15.json"
        _fm(monkeypatch, [key], {key: json.dumps({"products": rows})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_COMPLETED,
            page_type="product", input_mode="url_list", output_file=key,
            target_fields=["title", "price", "description", "url"])
        assert tasks._missing_core_stamp(
            job, ["title", "price", "description", "url"]) == ""

    @pytest.mark.django_db
    def test_intake_drawer_does_not_label_a_completed_caveat_an_error(self):
        """Round-7 GAP-1d: the intake details drawer is JS-rendered, so a
        template pin is the executable assertion. A COMPLETED row carrying the
        `[MISSING-CORE …]` caveat must not be labelled a crash on the primary
        ops board — the same COMPLETED+error_message shape wave-19 T1.5 scrubs
        on job_detail.html."""
        path = os.path.join(ROOT, "webapp", "scraper", "templates", "scraper",
                            "intake.html")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert "det.push(['Error', j.error_message])" not in src, (
            "the intake drawer still labels a COMPLETED caveat 'Error'"
        )
        assert "'Data caveat'" in src, (
            "the completed branch never relabels the caveat"
        )

    @pytest.mark.django_db
    def test_a_completed_rows_stamp_is_not_a_red_error_panel(
            self, monkeypatch, tmp_path):
        """GAP-1d: the stamp rides `error_message` on a COMPLETED row, and
        job_detail.html rendered any non-empty `error_message` as a red danger
        panel headed "Error:" — exactly the COMPLETED+error shape wave-19 T1.5
        exists to scrub. The row's own status says how to read the text."""
        from django.conf import settings
        from django.test import Client
        from django.urls import reverse
        from django.utils import timezone

        from scraper.models import ScrapeJob, Step

        now = timezone.now()
        key = "scrapers/x-example/output_2026-09-20_050000_000001_13.json"
        _fm(monkeypatch, [key], {key: json.dumps(
            {"products": _null_price_rows(4)})})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://x.example/", status=ScrapeJob.STATUS_RUNNING,
            page_type="product", input_mode="url_list", output_file=key,
            started_at=now - timezone.timedelta(hours=1))
        Step.objects.create(
            job=job, phase="execution", status=Step.STATUS_DONE,
            started_at=now - timezone.timedelta(minutes=5),
            completed_at=now - timezone.timedelta(minutes=5))
        assert tasks_module.finalize_from_artifacts(
            job.id, "SoftTimeLimitExceeded") == ScrapeJob.STATUS_COMPLETED
        job.refresh_from_db()
        assert "MISSING-CORE" in (job.error_message or "")

        html = Client().get(
            reverse("job_detail", kwargs={"job_id": job.id})).content.decode()
        assert "MISSING-CORE" in html, "the caveat must still be surfaced"
        assert "MISSING-CORE" not in _danger_panel(html), (
            "a COMPLETED row's data caveat rendered inside the red error panel"
        )


def _danger_panel(html: str) -> str:
    """The rendered `Error:` panel — the only `mb-5 panel border-danger/30`
    block in job_detail.html (the Cancel link's border-danger is not a panel)."""
    start = html.find('mb-5 panel border-danger/30')
    if start < 0:
        return ""
    end = html.find('<div class="mb-5 panel', start + 1)
    return html[start:end if end > 0 else len(html)]


class TestDeadStoreFinalizeStamp:
    """The dead-site verdict must reach the row the operator and the retry UI
    read — a log line is not an honest failure."""

    @pytest.mark.django_db
    def test_failed_finalize_carries_the_dead_site_verdict(
            self, monkeypatch, tmp_path):
        from django.conf import settings
        from django.utils import timezone

        from scraper import tasks
        from scraper.models import ScrapeJob

        _fm(monkeypatch, [], {})
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        job = ScrapeJob.objects.create(
            url="https://www.johnnybigg.com.au/",
            status=ScrapeJob.STATUS_RUNNING, page_type="product",
            input_mode="list_page",
            started_at=timezone.now() - timezone.timedelta(hours=1))
        _freeze_state(monkeypatch, {
            "site_slug": "johnnybigg-com-au", "input_mode": "list_page",
            "product_count": 0, "error_message": "",
            "test_report": _dead_report()})
        tasks._finalize_job(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert (job.error_message or "").startswith(
            "[DEAD-SITE dead_store_sunset]"), job.error_message
        assert "cease trading" in job.error_message
