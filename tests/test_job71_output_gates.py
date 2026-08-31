"""[job-71 popsockets / job-76 myhouse] Output-file gates must read the right
files and use the right bar.

Three proven defects, one gate:

1. (76) ``--discover-only`` artifacts — 40 ``{url, src_url}`` stubs — were
   scanned by ``_scraper_has_real_items`` like extraction results. They never
   changed the verdict here (0 good rows either way) but they define what
   "the output says" everywhere downstream; the UI preview showed "40
   products" on a FAILED job. Tagged ``metadata.phase == "discovery"`` files
   are now skipped.
2. (71) The tester's parallel Phase-1/Phase-2 runs shared one second-
   resolution output filename and the discovery write destroyed the passing
   extraction result — fixed at the template layer (see
   test_template_output_collision); here we pin that a *tagged discovery
   file never counts as extraction truth* even when it is the newest file.
3. (71) The GROUND-TRUTH override hardcoded ``min_count=3`` while the
   pre-check above it used 1 for url_list/list_page — a list_page job whose
   every URL extracted (1 rich item) could never clear the override its own
   pre-check used.

Pure-python per the test_f15_ground_truth pattern (source extraction with
stubbed imports — the node module pulls langgraph types).
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py")


def _load():
    src = open(NODE).read()

    def grab(name):
        m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.M | re.S)
        assert m, f"{name} not found"
        return m.group(0)

    fn_src = (
        grab("_freshness_floor")
        + grab("_is_dead_product")
        + grab("_is_discovery_output")
        + grab("_scraper_has_real_items")
    )

    import logging
    from src.content_types import output_filter_fields, has_substantive_field

    ns = {
        "logging": logging,
        "logger": logging.getLogger("t.j71"),
        "output_filter_fields": output_filter_fields,
        "has_substantive_field": has_substantive_field,
        "ScrapeState": dict,
        "DEAD_STATUS_CODES": {"out of stock", "sold out"},
        "SOFT_404_MARKERS": (
            "soft 404",
            "product not found",
            "no longer available",
            "discontinued",
            "not a product page",
        ),
        "__name__": "t_j71",
    }
    exec(fn_src, ns)
    return ns["_scraper_has_real_items"], src


def _ws(tmp_path, files: dict):
    """Workspace with the draft written FIRST — the freshness floor only
    counts outputs newer than the draft (in a real run the tester executes
    the draft, then the draft writes outputs)."""
    ws = tmp_path / "workspace" / "acme-com"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft\n")
    for name, data in files.items():
        (ws / name).write_text(json.dumps(data))
    return ws


def _state(slug="acme-com", mode="list_page"):
    return {"site_slug": slug, "input_mode": mode, "content_type_config": {"content_type": "product", "output_key": "products"}}


DISCOVERY_FILE = {
    "site": {"name": "Acme"},
    "products": [{"url": f"https://acme.com/products/{i}", "src_url": "https://acme.com/sale"} for i in range(40)],
    "metadata": {"phase": "discovery", "discovered_urls": 40},
}

EXTRACTION_DEAD = {
    "site": {"name": "Acme"},
    "products": [
        {"title": "", "price": "", "availability": "", "remarks": "Soft 404: no ProductGroup JSON-LD found"}
    ] * 5,
    "metadata": {"phase": "extraction"},
}

EXTRACTION_ONE_GOOD = {
    "site": {"name": "Acme"},
    "products": [{"title": "PopWallet", "price": "$14.99", "availability": "InStock"}],
    "metadata": {"phase": "extraction"},
}

LEGACY_UNTAGGED = {
    "site": {"name": "Acme"},
    "products": [{"title": "Old Draft Item", "price": "$9.99"}],
}


class TestDiscoveryFilesAreNotExtractionTruth:
    def test_tagged_discovery_file_is_skipped(self, tmp_path, monkeypatch):
        fn, _ = _load()
        _ws(tmp_path, {"output_2026-08-31_070536_744_401.json": DISCOVERY_FILE})
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        # 40 stub rows exist but carry no phase=extraction truth
        assert fn(_state(), min_count=1) is False

    def test_discovery_plus_dead_extraction_is_still_no_items(self, tmp_path, monkeypatch):
        """The exact job-76 workspace shape: stubs + soft-404 rows → no."""
        fn, _ = _load()
        _ws(tmp_path, {
            "output_2026-08-31_070536_744_401.json": DISCOVERY_FILE,
            "output_2026-08-31_070114_222_402.json": EXTRACTION_DEAD,
        })
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        assert fn(_state(), min_count=1) is False

    def test_extraction_file_still_counts(self, tmp_path, monkeypatch):
        fn, _ = _load()
        _ws(tmp_path, {"output_2026-08-31_051402_249_403.json": EXTRACTION_ONE_GOOD})
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        assert fn(_state(), min_count=1) is True

    def test_legacy_untagged_file_counts_as_extraction(self, tmp_path, monkeypatch):
        """Backward compat: pre-tagging outputs keep their old meaning."""
        fn, _ = _load()
        _ws(tmp_path, {"output_2026-08-30_101010.json": LEGACY_UNTAGGED})
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        assert fn(_state(), min_count=1) is True


class TestGroundTruthOverrideBar:
    def test_override_bar_is_input_mode_aware(self):
        """The GROUND-TRUTH override must not hardcode min_count=3 — it must
        match _scraper_produced_valid_output's url_list/list_page bar of 1."""
        _, src = _load()
        assert "_override_min" in src
        # no remaining hardcoded 3-item bar in the override condition
        assert not re.search(r"and _scraper_has_real_items\(state, min_count=3\)", src)
