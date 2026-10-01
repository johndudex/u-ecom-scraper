"""[wave-50 T3] Core-value real-item bar: a row counts as real only through
``_field_has_value`` on a filter field, and ``currency`` — which every
template default-fills — joins the always-present fields.

Job 1116: 5 junk rows (``{"title": …, "currency": "USD", "price": ""}``)
passed every deterministic real-item bar because ``currency:"USD"`` alone
satisfies bare-truthiness ``any(p.get(f))``. Job 468's mirror image: the same
bare truthiness still mis-reads tatcha's genuine float ``0.0`` at these sites
(F1 fixed ``has_substantive_field`` only).
"""

from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])


def _load_real_items_fn():
    """The test_f15_ground_truth extraction harness (kept in sync with it)."""
    import logging
    import re

    rat_src = (Path(ROOT) / "webapp" / "agents" / "nodes"
               / "route_after_testing.py").read_text(encoding="utf-8")

    def grab(name):
        m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", rat_src,
                      re.M | re.S)
        assert m, f"{name} not found"
        return m.group(0)

    fn_src = (
        grab("_freshness_floor")
        + grab("_is_dead_product")
        + grab("_is_discovery_output")
        + grab("_row_url_has_path")
        + grab("_scraper_has_real_items")
    )
    from src.content_types import (
        _field_has_value,
        has_substantive_field,
        output_filter_fields,
    )

    ns = {
        "logging": logging,
        "logger": logging.getLogger("t.w50"),
        "output_filter_fields": output_filter_fields,
        "has_substantive_field": has_substantive_field,
        "_field_has_value": _field_has_value,
        "ScrapeState": dict,
        "DEAD_STATUS_CODES": {"out of stock", "sold out", "unavailable",
                              "discontinued"},
        "SOFT_404_MARKERS": ("soft 404", "product not found",
                             "no longer available", "discontinued",
                             "not a product page"),
        "__name__": "t_w50",
    }
    exec(fn_src, ns)  # noqa: S102
    return ns["_scraper_has_real_items"]


# The two row shapes this wave is about.
def _junk_rows(n=5):
    return [{"title": f"Personalized {i}", "price": "", "currency": "USD",
             "availability": ""} for i in range(n)]


def _zero_price_rows(n=5):
    return [{"title": f"Sample {i}", "price": 0.0, "currency": "USD",
             "status_code": 200} for i in range(n)]


class TestRegistry:
    def test_currency_is_always_present_so_not_a_filter_field(self):
        from src.content_types import output_filter_fields

        assert output_filter_fields("product") == ["price", "availability"]

    def test_junk_row_is_not_substantive(self):
        from src.content_types import has_substantive_field

        assert has_substantive_field(
            {"title": "x", "currency": "USD", "price": ""}) is False

    def test_typed_zero_price_still_substantive(self):
        from src.content_types import has_substantive_field

        assert has_substantive_field({"title": "x", "price": 0.0}) is True

    def test_other_content_types_keep_their_filter_fields(self):
        from src.content_types import output_filter_fields

        assert output_filter_fields("article") == [
            "author", "publish_date", "content"]


class TestScraperHasRealItems:
    def setup_method(self):
        self.fn = _load_real_items_fn()

    def _state(self, items):
        return {
            "content_type_config": {"content_type": "product"},
            "page_type": "product",
            "site_slug": "",
            "test_report": {"sample_products": items},
        }

    def test_currency_defaulted_junk_rows_do_not_count(self):
        """THE 1116 shape: currency alone must not make a row real."""
        assert self.fn(self._state(_junk_rows()), min_count=3) is False

    def test_typed_zero_price_rows_count(self):
        """THE 468 shape: float 0.0 is data — must keep counting."""
        assert self.fn(self._state(_zero_price_rows()), min_count=3) is True

    def test_empty_price_and_no_availability_does_not_count(self):
        rows = [{"title": f"x{i}", "price": ""} for i in range(5)]
        assert self.fn(self._state(rows), min_count=3) is False


class TestInjectedOutputFilter:
    def _patch_and_exec(self, tmp_path, slug, rows):
        import agents.graph as graph_mod

        slug_dir = tmp_path / "workspace" / slug
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(
            "import json\n"
            f"output = {{'products': {rows!r}}}\n"
            "json.dump(output, open('out.json', 'w'))\n"
        )
        orig = graph_mod._get_project_root
        graph_mod._get_project_root = lambda: str(tmp_path)
        try:
            graph_mod._patch_scraper_output_filter(slug, "product")
        finally:
            graph_mod._get_project_root = orig
        ns: dict = {}
        exec(compile(draft.read_text(), str(draft), "exec"), ns)  # noqa: S102
        return ns["output"]["products"]

    def test_junk_dropped_zero_price_kept(self, tmp_path):
        rows = (_junk_rows(2) + _zero_price_rows(2)
                + [{"title": "real", "price": "29.99", "currency": "USD"}])
        kept = self._patch_and_exec(tmp_path, "w50a", rows)
        titles = [r["title"] for r in kept]
        assert "real" in titles
        assert sum(1 for t in titles if t.startswith("Sample")) == 2
        assert not any(t.startswith("Personalized") for t in titles)

    def test_target_fields_branch_also_value_checks(self, tmp_path):
        """Custom-schema jobs: empty-string target fields don't count either."""
        import agents.graph as graph_mod

        slug_dir = tmp_path / "workspace" / "w50b"
        slug_dir.mkdir(parents=True)
        draft = slug_dir / "scraper_draft.py"
        draft.write_text(
            "import json\n"
            "output = {'products': [{'name': 'a', 'sku': ''},"
            " {'name': '', 'sku': 'S-1'}]}\n"
            "json.dump(output, open('out.json', 'w'))\n"
        )
        orig = graph_mod._get_project_root
        graph_mod._get_project_root = lambda: str(tmp_path)
        try:
            graph_mod._patch_scraper_output_filter(
                "w50b", "product", target_fields=["name", "sku"])
        finally:
            graph_mod._get_project_root = orig
        ns: dict = {}
        exec(compile(draft.read_text(), str(draft), "exec"), ns)  # noqa: S102
        assert len(ns["output"]["products"]) == 2


class TestRunExecutionHelpers:
    def _write(self, tmp_path, rows):
        import json

        p = tmp_path / "out.json"
        p.write_text(json.dumps({"products": rows}))
        return str(p)

    def test_substantive_item_count_junk_vs_zero(self, tmp_path):
        from agents.nodes.run_execution import _substantive_item_count

        assert _substantive_item_count(self._write(tmp_path, _junk_rows())) == 0
        assert _substantive_item_count(
            self._write(tmp_path, _zero_price_rows())) == 5

    def test_item_has_core_field_junk_vs_zero(self):
        from agents.nodes.run_execution import _item_has_core_field

        assert _item_has_core_field(
            {"title": "x", "currency": "USD", "price": ""}) is False
        assert _item_has_core_field({"title": "x", "price": 0.0}) is True

    def test_prune_empty_records_drops_junk_keeps_zero(self, tmp_path):
        from agents.nodes.run_execution import prune_empty_records

        rows = _junk_rows(3) + _zero_price_rows(2)
        path = self._write(tmp_path, rows)
        removed = prune_empty_records(path)
        assert removed == 3
        import json

        kept = json.loads(Path(path).read_text())["products"]
        assert len(kept) == 2
        assert all(r["price"] == 0.0 for r in kept)
