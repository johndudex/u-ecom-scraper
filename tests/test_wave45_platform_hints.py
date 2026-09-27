"""[wave-45] Evidence-side platform hints for the injection map — now
UNCONDITIONAL ([wave-45b] removed the env gate; the merge is the switch).

RCA (job 460, amazon.ie): the site_analyzer LLM labels generic platforms
"custom" — the verdict was literally ``platform: custom`` ("Amazon is a
well-known custom platform") so the existing ``"amazon": "amazon-detection"``
map key never matched and s03's learned notes were never injected. Same
shape for Kibo: the harvest's real verdicts are "custom (Kibo-family offers
shape)" (job 404, fanatics) or plain "custom" (ace hardware, job 493).

Fix: deterministic URL-evidence hints appended to the platform string —
amazon.<tld> host → "amazon", fanatics-style Kibo slug
``o-<n>+t-<n>+p-<n>`` → "kibo". Hints never override a verdict word — they
only add to it, so the negation guard still wins.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))


AMAZON_PDP = "https://www.amazon.ie/dp/B000V3Q0EQ"
KIBO_PDP = (
    "https://www.fanatics.com/la-liga/barcelona/barcelona-nike-2026/"
    "27-away-stadium-replica-jersey-purple/"
    "o-21202441+t-92296846+p-13886659419+z-9-3537643016"
)


def _site_platform(platform, extra_urls=()):
    from agents.subagents import _site_platform

    sa = platform if isinstance(platform, dict) else {"platform": platform}
    return _site_platform(sa, list(extra_urls))


class TestSitePlatformHints:
    def test_amazon_host_hinted(self):
        out = _site_platform("custom", [AMAZON_PDP])
        assert "custom" in out and "amazon" in out

    def test_kibo_slug_hinted(self):
        out = _site_platform("custom", [KIBO_PDP])
        assert "custom" in out and "kibo" in out

    def test_no_evidence_is_verdict_only(self):
        assert _site_platform("custom", ["https://example.com/p/1"]) == "custom"

    def test_lookalike_hosts_do_not_hint(self):
        # notamazon-shop.com / amazonwatch.org must NOT hint amazon
        out = _site_platform(
            "custom", ["https://notamazon-shop.com/p/1", "https://amazonwatch.org/x"]
        )
        assert out == "custom"

    def test_no_urls_is_verdict_only(self):
        assert _site_platform({"site": {"platform": "custom"}}) == "custom"


SECTIONS = {
    "amazon-detection": "amazon: pin the transport per surface",
    "kibo-detection": "kibo: preload script carries the API",
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


def _distill(monkeypatch, platform, url="", sample_url="", read_skill=_fake_read_skill):
    from agents.subagents import _platform_distillation

    import src.skills_store as store

    monkeypatch.setattr(store, "read_skill", read_skill)
    site_analysis = platform if isinstance(platform, dict) else {"platform": platform}
    state = {
        "site_analysis": site_analysis,
        "scraper_analysis": {"strategy": "http_requests"},
        "url": url,
        "sample_url": sample_url,
    }
    return _platform_distillation(state)


class TestDistillationWithHints:
    def test_amazon_verdict_custom_still_injects_s03(self, monkeypatch):
        block = _distill(
            monkeypatch,
            {"site": {"platform": "custom", "url": AMAZON_PDP}},
            url=AMAZON_PDP,
        )
        assert "LEARNED SKILL NOTES (from `amazon-detection`" in block
        assert "amazon: pin the transport per surface" in block

    def test_kibo_slug_injects_s14(self, monkeypatch):
        block = _distill(
            monkeypatch,
            {"site": {"platform": "custom (Kibo-family offers shape)"}},
            sample_url=KIBO_PDP,
        )
        assert "LEARNED SKILL NOTES (from `kibo-detection`" in block

    def test_negation_guard_beats_hint(self, monkeypatch):
        block = _distill(
            monkeypatch,
            {"site": {"platform": "custom (no amazon markers)"}},
            url=AMAZON_PDP,
        )
        assert "amazon-detection" not in block

    def test_crosscutting_still_appended(self, monkeypatch):
        block = _distill(monkeypatch, {"site": {"platform": "custom"}}, url=AMAZON_PDP)
        assert "LEARNED SKILL NOTES (from `output-schema-integrity`" in block

    def test_map_key_path_unaffected_by_hints(self, monkeypatch):
        # verdict itself says amazon, no URLs passed → plain key match
        block = _distill(monkeypatch, {"site": {"platform": "amazon"}})
        assert "LEARNED SKILL NOTES (from `amazon-detection`" in block


class TestEnvVarIsDead:
    """[wave-45b] No env gate: the old WAVE43_TIER3_SKILL_INJECTION variable
    must be inert for the hint path too."""

    def test_env_zero_hints_still_apply(self, monkeypatch):
        monkeypatch.setenv("WAVE43_TIER3_SKILL_INJECTION", "0")
        # one platform skill injects per block — verify each hint alone
        amazon_block = _distill(
            monkeypatch,
            {"site": {"platform": "custom"}},
            url=AMAZON_PDP,
        )
        assert "LEARNED SKILL NOTES (from `amazon-detection`" in amazon_block
        kibo_block = _distill(
            monkeypatch,
            {"site": {"platform": "custom"}},
            sample_url=KIBO_PDP,
        )
        assert "LEARNED SKILL NOTES (from `kibo-detection`" in kibo_block

    def test_unset_env_hints_still_apply(self, monkeypatch):
        monkeypatch.delenv("WAVE43_TIER3_SKILL_INJECTION", raising=False)
        out = _site_platform("custom", [AMAZON_PDP])
        assert "amazon" in out
