"""[wave-20 T0] Soft-block challenge markers must be split STRONG/WEAK.

Prod RCA (jobs 359 michaelhill / 360 marimekko, 2026-09-05): setting
``SCRAPER_SOFT_BLOCK_MIN_BYTES`` armed the ``CHALLENGE_MARKERS`` substring
scan for the first time, and that list contains bare tokens — ``captcha``,
``_abck``, ``akamai`` — that appear inside every real SFCC/Akamai-fronted
page. Result: 1 MB real bodies classified as challenge pages, the proxy
ladder escalated through all 4 tiers, and a no-anti-bot site earned itself a
real 403 residential ban.

Contract going forward:

- STRONG markers (structural challenge phrases, e.g. "just a moment",
  ``cf-chl``) classify a body as a challenge regardless of size;
- WEAK tokens (``captcha``, ``_abck``, ``akamai``) only classify when the
  body is ALSO under the min-bytes floor (the wave-19 tiny-wall defense);
- floor <= 0 keeps the whole detector OFF (pre-existing contract).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import pytest  # noqa: E402

from src.http_fetch import (  # noqa: E402
    CHALLENGE_MARKERS_STRONG,
    CHALLENGE_MARKERS_WEAK,
    SoftBlock,
    detect_soft_block,
)

FLOOR_ENV = "SCRAPER_SOFT_BLOCK_MIN_BYTES"


@pytest.fixture
def floor_20k(monkeypatch):
    monkeypatch.setenv(FLOOR_ENV, "20000")


def _big_body(*needles: str) -> str:
    """~1 MB realistic body with the given needles planted in it."""
    body = (
        "<!DOCTYPE html><html><head><title>Shop</title></head><body>"
        + "<div class='product-tile'>item</div>" * 12000
        + "</body></html>"
    )
    assert len(body) > 20000
    for n in needles:
        body = body[:500] + n + body[500:]
    return body


class TestWeakTokensOnRealBodies:
    def test_large_real_body_with_weak_tokens_is_not_soft_block(self, floor_20k):
        # The michaelhill shape: real SFCC page, ~1 MB, embeds Akamai
        # telemetry + captcha help links in every page.
        body = _big_body(
            "window._abck = 'telemetry';",
            "<a href='/help'>captcha help</a>",
            "akamai_image_url",
        )
        assert detect_soft_block(body) is None

    def test_weak_token_names_preserved(self):
        # The split must not lose the tiny-wall vocabulary.
        assert "captcha" in CHALLENGE_MARKERS_WEAK
        assert "_abck" in CHALLENGE_MARKERS_WEAK
        assert "akamai" in CHALLENGE_MARKERS_WEAK


class TestTinyWallDefenseIntact:
    def test_tiny_body_with_weak_token_is_soft_block(self, floor_20k):
        body = "._abck challenge stub" * 100  # ~2 KB
        block = detect_soft_block(body)
        assert isinstance(block, SoftBlock)

    def test_tiny_body_alone_is_soft_block(self, floor_20k):
        block = detect_soft_block("tiny stub page")
        assert isinstance(block, SoftBlock)
        assert block.reason == "under_min_bytes"


class TestStrongMarkers:
    def test_strong_marker_fires_regardless_of_size(self, floor_20k):
        body = _big_body("Just a moment...")
        block = detect_soft_block(body)
        assert isinstance(block, SoftBlock)
        assert block.reason == "challenge_marker"

    def test_strong_marker_list_has_structural_phrases(self):
        for phrase in (
            "just a moment",
            "verify you are human",
            "checking your browser",
            "cf-chl",
        ):
            assert phrase in CHALLENGE_MARKERS_STRONG
        # The moved tokens must NOT be strong: that's the whole point.
        assert "captcha" not in CHALLENGE_MARKERS_STRONG
        assert "_abck" not in CHALLENGE_MARKERS_STRONG
        assert "akamai" not in CHALLENGE_MARKERS_STRONG


class TestFloorDisabled:
    def test_floor_zero_disables_everything(self, monkeypatch):
        # Pre-existing contract: floor 0 = detector fully off, even for
        # strong markers and tiny bodies (a mis-set floor must never be able
        # to reject real pages, and prod defaults to 0).
        monkeypatch.delenv(FLOOR_ENV, raising=False)
        monkeypatch.setenv(FLOOR_ENV, "0")
        assert detect_soft_block("just a moment") is None
        assert detect_soft_block("") is None


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
