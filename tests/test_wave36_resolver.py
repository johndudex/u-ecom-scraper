"""[wave-36 Fix 1b] The chip→canonical resolver (src/field_mapping.py).

Chain: alias → one-shot LLM (injected; unresolved chips only) → abstaining
fuzzy (0.78, abstain near-ties) → verbatim passthrough. NEVER raises.
Deterministic validation afterwards: registry membership (else CUSTOM),
duplicate-target demotion, charset discipline. Verbatim entries are exempt —
they ARE "no mapping" (byte-compat with today).
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from src.field_mapping import (  # noqa: E402
    _fuzzy_lookup,
    content_hash_for,
    mapping_enabled,
    registry_field_names,
    rekey_field_notes,
    resolve_mapping,
)


class TestKillSwitchAndHash:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("FIELD_MAPPING_ENABLED", raising=False)
        assert mapping_enabled() is True

    def test_disabled_by_env(self, monkeypatch):
        for val in ("0", "false", "no", "off"):
            monkeypatch.setenv("FIELD_MAPPING_ENABLED", val)
            assert mapping_enabled() is False, val

    def test_hash_stable_and_sensitive(self):
        a1 = content_hash_for(["product name", "rrp"], "product")
        a2 = content_hash_for(["rrp", "product name"], "product")  # order-free
        assert a1 == a2
        assert a1 != content_hash_for(["product name"], "product")
        assert a1 != content_hash_for(["product name", "rrp"], "article")

    def test_registry_names_product(self):
        names = registry_field_names("product")
        assert {"title", "price", "availability", "sku"} <= names
        assert "not_a_field" not in names


class TestAliasChain:
    def test_658_chips_resolve_without_llm(self):
        mapping, warnings = resolve_mapping(
            ["product name", "avaliability", "rrp"], "product"
        )
        assert mapping["product name"]["target"] == "title"
        assert mapping["avaliability"]["target"] == "availability"
        assert mapping["rrp"]["target"] == "original_price"
        assert mapping["product name"]["source"] == "alias"
        assert warnings == []

    def test_unknown_chip_stays_verbatim(self):
        mapping, _ = resolve_mapping(["glarb factor"], "product")
        assert mapping["glarb factor"]["target"] == "glarb factor"
        assert mapping["glarb factor"]["source"] == "verbatim"


class TestLLMLeg:
    def test_unresolved_chips_go_to_llm(self):
        seen = {}

        def fake_llm(chips, page_type, registry_block="", site_context=""):
            seen["chips"] = list(chips)
            seen["registry_block"] = registry_block
            return {chips[0]: {"target": "brand", "confidence": 0.9,
                               "rationale": "domain term"}}

        mapping, _ = resolve_mapping(
            ["product name", "maker label"], "product", llm_fn=fake_llm
        )
        # Only the UNRESOLVED chip went to the LLM (F6: alias hits never do).
        assert seen["chips"] == ["maker label"]
        assert "title" in seen["registry_block"]
        assert mapping["maker label"]["target"] == "brand"
        assert mapping["maker label"]["source"] == "llm"

    def test_llm_failure_falls_through_fuzzy_or_verbatim(self):
        def boom(*a, **k):
            raise RuntimeError("llm down")

        mapping, _ = resolve_mapping(
            ["titel"], "product", llm_fn=boom
        )
        # fuzzy rescue OR verbatim — never a raise
        assert mapping["titel"]["target"] in ("title", "titel")


class TestFuzzy:
    def test_fuzzy_rescue(self):
        assert _fuzzy_lookup(
            "titel", {"title": "title", "brand": "brand"}
        ) == "title"

    def test_near_tie_abstains(self):
        # "prise" scores 0.80 against BOTH candidates → ambiguous → None
        assert _fuzzy_lookup(
            "prise", {"price": "price", "prize": "prize"}
        ) is None

    def test_below_threshold_abstains(self):
        assert _fuzzy_lookup("zzz", {"title": "title"}) is None

    def test_resolve_mapping_fuzzy_layer(self):
        mapping, _ = resolve_mapping(["titel"], "product")  # no llm_fn
        assert mapping["titel"]["target"] in ("title", "titel")


class TestValidation:
    def test_llm_invented_target_demoted_to_custom(self):
        def fake_llm(chips, page_type, registry_block="", site_context=""):
            return {chips[0]: {"target": "not_a_field", "confidence": 0.9,
                               "rationale": "hallucination"}}

        mapping, warnings = resolve_mapping(
            ["maker label"], "product", llm_fn=fake_llm
        )
        assert mapping["maker label"]["target"] == "CUSTOM"
        assert mapping["maker label"]["target_key"] == "maker_label"
        assert warnings

    def test_duplicate_canonical_demotion(self):
        def fake_llm(chips, page_type, registry_block="", site_context=""):
            # Both chips → title; different confidences.
            out = {}
            for i, c in enumerate(chips):
                out[c] = {"target": "title", "confidence": 0.6 + 0.1 * i,
                          "rationale": "x"}
            return out

        mapping, warnings = resolve_mapping(
            ["maker label", "item heading"], "product", llm_fn=fake_llm
        )
        targets = {c: e["target"] for c, e in mapping.items()}
        # Highest confidence (0.7) keeps title; the other demoted.
        kept = [c for c, t in targets.items() if t == "title"]
        assert len(kept) == 1
        assert any(e.get("demoted") for e in mapping.values())
        assert warnings

    def test_verbatim_exempt_from_registry_discipline(self):
        mapping, warnings = resolve_mapping(["glarb factor"], "product")
        assert mapping["glarb factor"]["target"] == "glarb factor"
        assert mapping["glarb factor"].get("demoted") is None
        assert warnings == []


class TestFieldNotesRekey:
    def test_notes_follow_resolved_key(self):
        mapping = {
            "product name": {"target": "title", "source": "alias"},
            "custom metric": {"target": "CUSTOM",
                              "target_key": "custom_metric"},
        }
        out = rekey_field_notes(
            {"product name": "include RRP", "custom metric": "int only",
             "unmapped chip": "keep"},
            mapping,
        )
        assert out == {"title": "include RRP", "custom_metric": "int only",
                       "unmapped chip": "keep"}

    def test_empty_notes_noop(self):
        assert rekey_field_notes({}, {"a": {"target": "b"}}) == {}
        assert rekey_field_notes(None, None) == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
