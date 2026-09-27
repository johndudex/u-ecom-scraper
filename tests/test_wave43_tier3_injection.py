"""[wave-43→45b] Tier-3 skill injection into the writer's platform
distillation — now UNCONDITIONAL.

docs/plans/wave43-skill-seeding-plan.md §4.1: magento/netsuite in the
injection map (with a negation guard — "magento" appears inside non-Magento
verdicts like "custom (... no shopify/magento/sfcc markers)"), plus
output-schema-integrity as a cross-cutting (non-platform) reader, and the
nested ``site.platform`` read the LLM site_analyzer actually emits.

Originally gated behind WAVE43_TIER3_SKILL_INJECTION for local-only
development. [wave-45b] Owner decision: env-var switches are not a proper
switch — the gate is REMOVED and the merge/deploy is the switch. These
tests pin the unconditional behavior, including explicitly asserting the
env var is dead (setting or unsetting it must change nothing).
"""

# One recognizable lesson per skill — the fake read_skill makes any wrong
# selection visible in the rendered block.
SECTIONS = {
    "shopify-detection": "shopify: use the /products.json endpoint",
    "sfcc-detection": "sfcc: check for __PRELOADED_STATE__",
    "algolia-detection": "algolia: read the facet payload",
    "amazon-detection": "amazon: pin the transport per surface",
    "kibo-detection": "kibo: preload script carries the API",
    "magento-detection": "magento: data-ui-id tiles, PWA listing needs rendering",
    "netsuite-detection": "netsuite: unauthenticated /api/items is the lane",
    "output-schema-integrity": "schema: every requested field present or honestly empty",
}


def _fake_read_skill(name):
    lesson = SECTIONS.get(name)
    if lesson is None:
        return None
    return (
        f"---\nname: {name}\ndescription: d\n---\n\nbaseline\n"
        f"\n## Learned: {name} distilled\n{lesson}\n"
    )


def _distill(monkeypatch, platform, read_skill=_fake_read_skill):
    from agents.subagents import _platform_distillation

    import src.skills_store as store

    monkeypatch.setattr(store, "read_skill", read_skill)
    site_analysis = platform if isinstance(platform, dict) else {"platform": platform}
    state = {
        "site_analysis": site_analysis,
        "scraper_analysis": {"strategy": "http_requests"},
    }
    return _platform_distillation(state)


class TestInjectionBehavior:
    """The unconditional contract: platform keys, negation guard,
    cross-cutting schema skill, nested artifact shape."""

    def test_magento_platform_injects_magento_skill(self, monkeypatch):
        out = _distill(monkeypatch, "Magento 2.4.6 (Luma theme + PWA Studio)")
        assert "magento-detection" in out
        assert "data-ui-id" in out

    def test_suitecommerce_verdict_injects_netsuite_skill(self, monkeypatch):
        out = _distill(monkeypatch, "NetSuite SuiteCommerce Advanced (SCA) 2018.2")
        assert "netsuite-detection" in out
        assert "/api/items" in out

    def test_plain_netsuite_verdict_injects_netsuite_skill(self, monkeypatch):
        out = _distill(monkeypatch, "NetSuite (unknown theme)")
        assert "netsuite-detection" in out

    def test_negated_markers_platform_gets_no_platform_skill(self, monkeypatch):
        """The harvest's real false-positive: this verdict contains
        'shopify', 'magento' AND 'sfcc' — all three in a 'no ...' list. No
        PLATFORM skill may inject; the cross-cutting schema skill still does."""
        out = _distill(
            monkeypatch,
            "custom (server-rendered ecommerce, no shopify/magento/sfcc markers)",
        )
        assert "/products.json" not in out
        assert "data-ui-id" not in out
        assert "__PRELOADED_STATE__" not in out
        assert "shopify-detection" not in out
        assert "magento-detection" not in out
        assert "sfcc-detection" not in out
        assert "honestly empty" in out  # cross-cutting, platform-independent

    def test_positive_shopify_still_injects_under_guard(self, monkeypatch):
        out = _distill(monkeypatch, "Shopify Online Store")
        assert "/products.json endpoint" in out

    def test_positive_sfcc_with_negation_elsewhere_still_injects(self, monkeypatch):
        """Guard is 'no <one-word> keyword', not 'no anywhere' — an unrelated
        negation must not suppress a positive verdict."""
        out = _distill(
            monkeypatch, "Salesforce Commerce Cloud SFCC (no longer SiteGenesis)"
        )
        assert "__PRELOADED_STATE__" in out

    def test_schema_skill_crosscutting_on_unmatched_platform(self, monkeypatch):
        out = _distill(monkeypatch, "custom-cms")
        assert "output-schema-integrity" in out
        assert "honestly empty" in out

    def test_schema_skill_appends_after_platform_skill(self, monkeypatch):
        out = _distill(monkeypatch, "shopify")
        assert "/products.json endpoint" in out
        assert "honestly empty" in out
        assert out.index("shopify-detection") < out.index("output-schema-integrity")

    def test_silent_when_all_renders_come_back_empty(self, monkeypatch):
        def _no_learnings(name):
            return f"---\nname: {name}\ndescription: d\n---\n\nbaseline only\n"

        out = _distill(monkeypatch, "shopify", read_skill=_no_learnings)
        assert "LEARNED SKILL NOTES" not in out

    def test_nested_platform_artifact_shape_injects(self, monkeypatch):
        """The real site_analysis.json nests platform under 'site' — job 453
        proved the flat read misses it and the platform block never fires."""
        out = _distill(monkeypatch, {"site": {"platform": "shopify"}})
        assert "shopify-detection" in out
        assert "/products.json endpoint" in out


class TestEnvVarIsDead:
    """[wave-45b] The old WAVE43_TIER3_SKILL_INJECTION gate is removed —
    setting it (to anything, including "0"/"1") or unsetting it must change
    nothing. These tests would have passed the OPPOSITE way under the old
    flag-OFF contract."""

    def test_unset_env_still_injects_magento(self, monkeypatch):
        monkeypatch.delenv("WAVE43_TIER3_SKILL_INJECTION", raising=False)
        out = _distill(monkeypatch, "Magento 2.4.6 (Luma theme + PWA Studio)")
        assert "magento-detection" in out

    def test_env_zero_still_injects_schema_skill(self, monkeypatch):
        monkeypatch.setenv("WAVE43_TIER3_SKILL_INJECTION", "0")
        out = _distill(monkeypatch, "custom-cms")
        assert "output-schema-integrity" in out
        assert "honestly empty" in out

    def test_env_one_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("WAVE43_TIER3_SKILL_INJECTION", "1")
        out = _distill(monkeypatch, "custom-cms")
        assert "output-schema-integrity" in out

    def test_nested_shape_injects_with_env_zero(self, monkeypatch):
        monkeypatch.setenv("WAVE43_TIER3_SKILL_INJECTION", "0")
        out = _distill(monkeypatch, {"site": {"platform": "shopify"}})
        assert "shopify-detection" in out

    def test_gate_symbols_are_gone(self):
        import agents.subagents as mod

        assert not hasattr(mod, "TIER3_INJECTION_ENV")
        assert not hasattr(mod, "_tier3_injection_enabled")
        assert not hasattr(mod, "_PLATFORM_SKILLS_TIER3")
