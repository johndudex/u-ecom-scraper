"""[wave-32 A5/B1] Render-gate semantics + override note.

``browser_service/server.py`` imports fastapi/pydantic at module level and
the package ``__init__`` imports server — so these tests load
``render_gate.py`` DIRECTLY from its file path (the test_f1_orphan_killer
dodge) and never import ``browser_service``.

A5 extracts the gate (pure data in, pure bool out) and adds the override
note-builder that names WHICH arm satisfied the gate + the probe counts —
587's antibot 429 was overridden by a challenge shell and the note never
said which signal claimed credit.

Run: docker compose exec -T django sh -c "cd /app && pytest tests/test_wave32_render_gate.py -q"
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

_RG_PATH = os.path.join(ROOT, "browser_service", "render_gate.py")


def _rg():
    spec = importlib.util.spec_from_file_location("render_gate_under_test", _RG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LISTING_PROBE = {
    "anchors": 892, "jsonld_items": 0, "body_len": 892_000, "has_price": False,
}


class TestA5OverrideNoteNamesArm:
    def test_override_note_names_arm(self):
        """The note must say WHICH signal satisfied the gate — 587's override
        printed the raw probe dict and let the RCA guess."""
        rg = _rg()
        arm = rg.render_gate_arm(LISTING_PROBE, wait_for_hit=False)
        assert arm == "anchors", arm
        note = rg.render_gate_note(
            "antibot_429", 429, arm, LISTING_PROBE,
        )
        assert "arm=anchors" in note, note
        assert "anchors=892" in note, "probe counts must ride the note"
        assert "antibot_429" in note and "429" in note, note

    def test_wait_for_hit_is_never_the_arm(self):
        """[wave-32 B1 amended] wait_for_hit is retired from the arm
        vocabulary: with content present the arm names the CONTENT signal;
        with a shell it is "none" even though a selector attached."""
        rg = _rg()
        assert rg.render_gate_arm(LISTING_PROBE, wait_for_hit=True) == "anchors"
        assert rg.render_gate_arm({}, wait_for_hit=True) == "none"
        note = rg.render_gate_note("captcha", 403, "anchors", LISTING_PROBE)
        assert "arm=anchors" in note, note

    def test_note_names_jsonld_arm(self):
        rg = _rg()
        probe = {"anchors": 3, "jsonld_items": 2, "body_len": 900, "has_price": False}
        assert rg.render_gate_arm(probe, wait_for_hit=False) == "jsonld"

    def test_note_names_body_len_and_price_arms(self):
        rg = _rg()
        body_probe = {"anchors": 0, "jsonld_items": 0, "body_len": 25_000, "has_price": False}
        assert rg.render_gate_arm(body_probe, wait_for_hit=False) == "body_len"
        price_probe = {"anchors": 0, "jsonld_items": 0, "body_len": 100, "has_price": True}
        assert rg.render_gate_arm(price_probe, wait_for_hit=False) == "has_price"

    def test_unsatisfied_probe_arm_is_none(self):
        rg = _rg()
        shell = {"anchors": 2, "jsonld_items": 0, "body_len": 900, "has_price": False}
        assert rg.render_gate_arm(shell, wait_for_hit=False) == "none"


class TestA5SatisfactionParity:
    """A5 pinned the pre-B1 semantics; B1 deliberately changed two rows
    (marked) — wait_for_hit no longer satisfies alone. Everything else is
    the same table the extraction was verified against."""

    @pytest.mark.parametrize("probe,wait_for,expected", [
        (LISTING_PROBE, False, True),
        (LISTING_PROBE, True, True),
        ({}, False, False),
        ({}, True, False),  # B1: was True — a selector hit is not content
        ("not-a-dict", False, False),
        ({"anchors": 24}, False, False),
        ({"anchors": 25}, False, True),
        ({"body_len": 19_999}, False, False),
        ({"body_len": 20_000}, False, True),
        ({"jsonld_items": 1}, False, True),
        ({"has_price": True}, False, True),
    ])
    def test_satisfied_table(self, probe, wait_for, expected):
        rg = _rg()
        assert rg.render_gate_satisfied(probe, wait_for) is expected


class TestB1RenderGateHonesty:
    """[wave-32 B1] Content evidence is the ONLY thing that satisfies the
    gate.

    Plan adjudication (recorded): the plan text said the gate becomes
    ``wait_for_hit AND (content arms)`` — but NO caller in the repo passes a
    ``wait_for`` selector to /navigate (the field defaults to None and
    templates/discovery/probe never send it), so a literal conjunction would
    fail every real rung and break the crocs-class 429-with-real-DOM save
    (wave-17 S15) that B1 explicitly protects. The 587 mechanism was the
    innerHTML body arm (challenge shells carry huge inline JS), with
    wait_for_hit retired entirely — content evidence alone decides, and a
    selector attach never earns the override.
    """

    def test_wait_for_hit_alone_does_not_satisfy(self):
        rg = _rg()
        assert rg.render_gate_satisfied({}, wait_for_hit=True) is False
        shell = {"anchors": 2, "jsonld_items": 0, "body_len": 900, "has_price": False}
        assert rg.render_gate_satisfied(shell, wait_for_hit=True) is False

    def test_script_shell_body_fails_text_length(self):
        """B1(b): the body arm must measure innerText (visible text), not
        innerHTML — an Akamai/Cloudflare challenge shell carries >20KB of
        inline JS markup but almost no visible text. Source-pin: server.py's
        probe JS reads as text (fastapi makes it unimportable)."""
        src = open(os.path.join(ROOT, "browser_service", "server.py"),
                   encoding="utf-8").read()
        m = re.search(r"const body_len = .*?;", src)
        assert m, "body_len expression not found in _RENDER_PROBE_JS"
        assert "innerText" in m.group(0), (
            f"body arm must measure innerText, got: {m.group(0)}"
        )
        assert "innerHTML" not in m.group(0), m.group(0)

    def test_image_grid_with_waitfor_and_no_content_arms_no_longer_overrides(self):
        """The residual regression class, pinned EXPLICITLY as accepted:
        a real-looking image-grid page with a waited selector attached but
        anchors<25, no jsonld, no price, and (post-B1) innerText<20k no
        longer overrides a block status. The block then escalates to a
        working proxy rung — a false-block is recoverable, a false-override
        poisons the whole job downstream (587)."""
        rg = _rg()
        image_grid = {
            "anchors": 10, "jsonld_items": 0, "body_len": 800, "has_price": False,
        }
        assert rg.render_gate_satisfied(image_grid, wait_for_hit=True) is False
        assert rg.render_gate_arm(image_grid, wait_for_hit=True) == "none"

    def test_crocs_class_429_with_real_dom_still_overrides(self):
        """The protected save: no wait_for, 429 status, but the DOM holds a
        real listing (anchors≥25 / jsonld) — the override MUST fire."""
        rg = _rg()
        listing = {"anchors": 892, "jsonld_items": 0, "body_len": 60_000,
                   "has_price": True}
        assert rg.render_gate_satisfied(listing, wait_for_hit=False) is True
        assert rg.render_gate_arm(listing, wait_for_hit=False) == "anchors"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
