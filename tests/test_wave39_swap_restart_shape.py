"""[wave-39] Restart-shape integration — the intake/status Retry row shape.

Prod retry batch 719-737: jobs shaped exactly like this (url = PDP,
input_mode = list_page, search_criteria = same-host listing, scope firstn)
demoted to 1-item url_list on restart. The swap must rewrite the seed to
the listing, keep list_page, materialize NO 1-item input_urls.json, and
persist the new target + [INTAKE-PDP-SWAP] marker on the ScrapeJob row.
"""

import pytest

from agents import graph
from agents.tools import probe_tools
from scraper import models as scraper_models

PDP_URL = "https://www.vinted.be/items/10014966848-levis-501"
LISTING_URL = "https://www.vinted.be/catalog/5-men"


def _probe_data(jsonld):
    return {
        "success": True,
        "method": "direct_http",
        "http_method": "direct_http",
        "browser_method": None,
        "proxy_tier": "none",
        "needs_browser": False,
        "blocked": False,
        "captcha_detected": False,
        "status_code": 200,
        "body_length": 200000,
        "methods_tried": ["direct_http"],
        "jsonld": jsonld,
        "meta": {},
        "selector_results": {},
    }


def _mock_probe(monkeypatch, jsonld):
    monkeypatch.setattr(
        probe_tools,
        "run_probe_with_captcha_check",
        lambda *a, **k: _probe_data(jsonld),
    )
    monkeypatch.setattr(
        probe_tools, "_verify_captcha_free", lambda data: {"captcha_detected": False}
    )
    monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)


@pytest.mark.django_db
def test_retry_shape_swaps_seed_to_listing(monkeypatch, settings, tmp_path):
    _mock_probe(monkeypatch, [{"@type": "Product", "offers": {"price": 44.99}}])
    # Point the flip's seed-file writer at tmp_path so the assertion "no
    # 1-item input_urls.json was materialized" is real, not vacuous.
    settings.PROJECT_ROOT = str(tmp_path)

    job = scraper_models.ScrapeJob.objects.create(
        url=PDP_URL,
        product_url=PDP_URL,
        input_mode="list_page",
        page_type="product",
        search_criteria=LISTING_URL,
        scope="firstn",
        scope_value="10",
    )
    state = {
        "job_id": job.id,
        "url": PDP_URL,
        "input_mode": "list_page",
        "site_slug": "vinted-be",
        "search_criteria": LISTING_URL,
        "scope": "firstn",
        "scope_value": "10",
    }
    cmd = graph.check_accessibility(state, None)

    # Swapped: discovery seed is the listing, mode untouched, no demote.
    assert cmd.goto == "browser_traverse"
    assert cmd.update["url"] == LISTING_URL
    assert cmd.update["product_url"] == LISTING_URL
    assert "input_mode" not in cmd.update
    assert "pdp_seed_flip" not in cmd.update
    assert "input_urls" not in cmd.update

    # DB propagation: target + marker on the row; no 1-item seed file.
    job.refresh_from_db()
    assert job.url == LISTING_URL
    assert "[INTAKE-PDP-SWAP]" in (job.notes or "")
    assert "[INTAKE-PDP]" not in (job.notes or "").replace("[INTAKE-PDP-SWAP]", "")
    assert not (tmp_path / "workspace" / "vinted-be" / "input_urls.json").exists()


@pytest.mark.django_db
def test_retry_shape_without_listing_still_demotes(monkeypatch, settings, tmp_path):
    # Control: the same shape minus search_criteria keeps the wave-34 demote.
    _mock_probe(monkeypatch, [{"@type": "Product", "offers": {"price": 44.99}}])
    settings.PROJECT_ROOT = str(tmp_path)

    job = scraper_models.ScrapeJob.objects.create(
        url=PDP_URL,
        product_url=PDP_URL,
        input_mode="list_page",
        page_type="product",
    )
    state = {
        "job_id": job.id,
        "url": PDP_URL,
        "input_mode": "list_page",
        "site_slug": "vinted-be",
    }
    cmd = graph.check_accessibility(state, None)

    assert cmd.goto == "site_analyzer"
    assert cmd.update["input_mode"] == "url_list"
    assert cmd.update["pdp_seed_flip"] is True
    assert (tmp_path / "workspace" / "vinted-be" / "input_urls.json").exists()


@pytest.mark.django_db
def test_swap_survives_db_failure(monkeypatch, settings, tmp_path):
    # Fail-open: if the ScrapeJob row update raises, the swap still applies
    # to graph state (the DB note is observability, not the contract).
    _mock_probe(monkeypatch, [{"@type": "Product", "offers": {"price": 44.99}}])
    settings.PROJECT_ROOT = str(tmp_path)

    # Real row: a DB log handler persists the node's warnings into
    # scraper_sessionlog, so job_id must satisfy its FK even while
    # ScrapeJob.objects itself is broken.
    real = scraper_models.ScrapeJob.objects.create(
        url=PDP_URL,
        product_url=PDP_URL,
        input_mode="list_page",
        page_type="product",
    )

    class _Boom:
        def filter(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(scraper_models, "ScrapeJob", _Boom)

    state = {
        "job_id": real.id,
        "url": PDP_URL,
        "input_mode": "list_page",
        "site_slug": "vinted-be",
        "search_criteria": LISTING_URL,
    }
    cmd = graph.check_accessibility(state, None)
    assert cmd.goto == "browser_traverse"
    assert cmd.update["url"] == LISTING_URL
