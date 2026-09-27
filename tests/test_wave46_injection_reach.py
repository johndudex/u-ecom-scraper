"""[wave-46] Injection reach — the structural ceiling found by the
harvest-2 audit (docs/plans/wave46-harvest2-plan.md §6, §4).

Three changes, all inside ``_platform_distillation``:

1. ``variant-availability`` joins ``output-schema-integrity`` as a
   cross-cutting reader — per-variant availability/price is the single
   biggest behavior-changing lesson in the whole 408-job harvest (~50
   jobs, platform-independent), so it must reach the writer on EVERY
   job, not sit behind a load_skill call nobody makes.
2. ``navigation-patterns`` becomes MODE-GATED: navigation/list_page/
   search_term jobs only. Its lessons (listing walks, pagination,
   soft-404s) are noise for url_list PDP jobs.
3. The four homeless platform families join the injection map
   (sap-hybris / bigcommerce / thg-ingenuity / fanatics-commerce), and
   the wave-45 URL-hint ladder learns the bigcommerce/thcdn-thehut/
   fanatics host tells — verdicts alone starve these keys (the LLM
   labels them "custom").
"""

# One recognizable lesson per skill — the fake read_skill makes any wrong
# selection visible in the rendered block.
SECTIONS = {
    "shopify-detection": "shopify: use the /products.json endpoint",
    "kibo-detection": "kibo: preload script carries the API",
    "sap-hybris-detection": "hybris: never emit priceType FROM placeholders",
    "bigcommerce-detection": "bigcommerce: OG plus BCData when JSON-LD is absent",
    "thg-ingenuity-detection": "thg: hasVariant offers live past stale empty flags",
    "fanatics-commerce-detection": "fanatics: same-rung retry beats ladder climb",
    "output-schema-integrity": "schema: every requested field present or honestly empty",
    "variant-availability": "variants: availability is per-variant or it is fiction",
    "navigation-patterns": "navigation: walk the listing, scroll the container",
}


def _fake_read_skill(name):
    lesson = SECTIONS.get(name)
    if lesson is None:
        return None
    return (
        f"---\nname: {name}\ndescription: d\n---\n\nbaseline\n"
        f"\n## Learned: {name} distilled\n{lesson}\n"
    )


def _distill(
    monkeypatch,
    platform,
    input_mode=None,
    sample_url=None,
    read_skill=_fake_read_skill,
):
    from agents.subagents import _platform_distillation

    import src.skills_store as store

    monkeypatch.setattr(store, "read_skill", read_skill)
    site_analysis = platform if isinstance(platform, dict) else {"platform": platform}
    state = {
        "site_analysis": site_analysis,
        "scraper_analysis": {"strategy": "http_requests"},
    }
    if input_mode is not None:
        state["input_mode"] = input_mode
    if sample_url:
        state["sample_url"] = sample_url
    return _platform_distillation(state)


class TestVariantAvailabilityCrosscutting:
    """Second cross-cutting reader — injects on every job regardless of
    platform, after output-schema-integrity."""

    def test_injects_on_unmatched_platform(self, monkeypatch):
        out = _distill(monkeypatch, "custom-cms", input_mode="url_list")
        assert "variant-availability" in out
        assert "per-variant or it is fiction" in out

    def test_appends_after_output_schema_integrity(self, monkeypatch):
        out = _distill(monkeypatch, "shopify", input_mode="url_list")
        assert out.index("output-schema-integrity") < out.index(
            "variant-availability"
        )

    def test_silent_when_variant_skill_absent_from_fm(self, monkeypatch):
        """FM without the skill must degrade silently, not crash the block."""

        def _no_variants(name):
            if name == "variant-availability":
                return None
            return _fake_read_skill(name)

        out = _distill(
            monkeypatch, "custom-cms", input_mode="url_list", read_skill=_no_variants
        )
        assert "output-schema-integrity" in out
        assert "variant-availability" not in out


class TestNavigationPatternsModeGate:
    """navigation-patterns injects ONLY for discovery-shaped jobs."""

    def test_injects_for_navigation(self, monkeypatch):
        out = _distill(monkeypatch, "custom-cms", input_mode="navigation")
        assert "walk the listing" in out

    def test_injects_for_list_page(self, monkeypatch):
        out = _distill(monkeypatch, "custom-cms", input_mode="list_page")
        assert "walk the listing" in out

    def test_injects_for_search_term(self, monkeypatch):
        out = _distill(monkeypatch, "custom-cms", input_mode="search_term")
        assert "walk the listing" in out

    def test_gated_off_for_url_list(self, monkeypatch):
        out = _distill(monkeypatch, "shopify", input_mode="url_list")
        assert "walk the listing" not in out

    def test_gated_off_when_input_mode_missing(self, monkeypatch):
        """The graph default for a missing input_mode is url_list — the
        gate must read it the same way."""
        out = _distill(monkeypatch, "custom-cms")
        assert "walk the listing" not in out


class TestHomelessPlatformKeys:
    """The four harvest-2 families join the injection map."""

    def test_hybris_verdict_injects_sap_hybris(self, monkeypatch):
        out = _distill(monkeypatch, "SAP Hybris 6.7 (B2C Accelerator)")
        assert "sap-hybris-detection" in out
        assert "priceType FROM placeholders" in out

    def test_spartacus_verdict_injects_sap_hybris(self, monkeypatch):
        out = _distill(monkeypatch, "Spartacus headless storefront")
        assert "sap-hybris-detection" in out

    def test_bigcommerce_verdict_injects_bigcommerce(self, monkeypatch):
        out = _distill(monkeypatch, "BigCommerce Stencil (Cornerstone)")
        assert "bigcommerce-detection" in out

    def test_thg_verdict_injects_thg(self, monkeypatch):
        out = _distill(monkeypatch, "custom (THG Ingenuity platform)")
        assert "thg-ingenuity-detection" in out

    def test_fanatics_verdict_injects_fanatics(self, monkeypatch):
        out = _distill(monkeypatch, "custom (Kibo-family offers shape)")
        assert "fanatics-commerce-detection" not in out  # kibo still wins
        out = _distill(monkeypatch, "custom (Fanatics Commerce)")
        assert "fanatics-commerce-detection" in out

    def test_negation_guard_covers_new_keys(self, monkeypatch):
        out = _distill(
            monkeypatch,
            "custom (server-rendered, no hybris/bigcommerce markers)",
        )
        assert "sap-hybris-detection" not in out
        assert "bigcommerce-detection" not in out
        assert "honestly empty" in out  # cross-cutting unaffected


class TestHostHintsForNewFamilies:
    """wave-45's URL-evidence ladder extended: verdicts alone say 'custom'
    for these platforms — the asset hosts are the real tell."""

    def test_cdn11_bigcommerce_host_hint(self, monkeypatch):
        out = _distill(
            monkeypatch,
            "custom",
            sample_url="https://cdn11.bigcommerce.com/s-abc/images/stencil/1280x1280/products/x.png",
        )
        assert "bigcommerce-detection" in out

    def test_thcdn_host_hint_injects_thg(self, monkeypatch):
        out = _distill(
            monkeypatch,
            "custom",
            sample_url="https://www.dermstore.com/dp/1234",
        )
        assert "thg-ingenuity-detection" not in out  # no tell in the PDP URL
        out = _distill(
            monkeypatch,
            "custom",
            sample_url="https://static.thcdn.com/images/x.jpg",
        )
        assert "thg-ingenuity-detection" in out

    def test_thehut_host_hint_injects_thg(self, monkeypatch):
        out = _distill(
            monkeypatch, "custom", sample_url="https://csp.thehut.net/assets/a.js"
        )
        assert "thg-ingenuity-detection" in out

    def test_fanatics_host_hint(self, monkeypatch):
        out = _distill(
            monkeypatch, "custom", sample_url="https://www.fanatics.com/jersey/x"
        )
        assert "fanatics-commerce-detection" in out

    def test_lookalike_hosts_refused(self, monkeypatch):
        out = _distill(
            monkeypatch,
            "custom",
            sample_url="https://notbigcommerce.com/p https://bigcommerce-watch.org/p",
        )
        assert "bigcommerce-detection" not in out

    def test_hints_never_override_kibo_slug(self, monkeypatch):
        """Hint words ADD to the verdict — an existing family match that
        sits earlier in the map keeps the pick (job 465: fanatics runs
        Kibo; the kibo slug is the tighter tell)."""
        out = _distill(
            monkeypatch,
            "custom (Kibo-family offers shape)",
            sample_url="https://www.fanatics.com/o-1+t-2+p-3",
        )
        assert "preload script carries the API" in out
        assert "same-rung retry beats ladder climb" not in out
