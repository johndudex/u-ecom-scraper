"""[wave-36 Fix 2] Soft-block JSON carve-out — complete JSON is not a wall.

Job 658 (westelm): SCRAPER_SOFT_BLOCK_MIN_BYTES=20480 armed; Sitecore Content
Analytics (SCA) PDP JSON responses are COMPLETE payloads at 8-20KB — every one
was classified ``under_min_bytes``, the caller burned the whole proxy ladder,
and the run shipped 3-of-114 (docs/plans/wave36-field-mapping-plan.md §2).

Amended predicate (round-2 review):
1. STRONG challenge markers first — a JSON body carrying one IS a challenge.
2. First-char gate ('{'/'[') BEFORE json.loads — parsing 100KB bodies on the
   fetch hot path is the cost we are avoiding.
3. Array-shaped JSON accepted at ANY size (an array is data, full stop).
4. Bare single-object JSON accepted only above SCRAPER_SOFT_BLOCK_JSON_MIN_BYTES
   (default 1024); below → SoftBlock("json_no_items") + wall-rejection counter
   so the caller's escalation ladder stays meaningful.
5. Non-JSON bodies: legacy floor logic byte-identical (wave-13/15/20 pins).
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

from src.http_fetch import (  # noqa: E402
    SOFT_BLOCK_JSON_MIN_BYTES_ENV,
    detect_soft_block,
    json_wall_rejections,
    reset_json_wall_rejections,
)

FLOOR_ENV = "SCRAPER_SOFT_BLOCK_MIN_BYTES"


@pytest.fixture()
def floor_20k(monkeypatch):
    monkeypatch.setenv(FLOOR_ENV, "20480")


@pytest.fixture(autouse=True)
def _clean_counter():
    reset_json_wall_rejections()
    yield
    reset_json_wall_rejections()


class TestJsonCarveOut:
    def test_array_shaped_json_accepted_at_any_size(self, floor_20k):
        # 11 bytes — under BOTH floors. A JSON array is data, full stop.
        assert detect_soft_block('[{"a": 1}]') is None

    def test_bare_array_literal_accepted(self, floor_20k):
        assert detect_soft_block("[1, 2, 3]") is None

    def test_large_single_object_json_accepted(self, floor_20k):
        # Complete SCA-style payload: 2KB single object, under the 20KB floor.
        body = '{"product": {"title": "Chair", "desc": "' + "x" * 2048 + '"}}'
        assert detect_soft_block(body) is None

    def test_tiny_single_object_json_is_json_wall(self, floor_20k):
        block = detect_soft_block('{"ok": true}')
        assert block is not None and not block
        assert block.reason == "json_no_items"

    def test_json_wall_increments_counter(self, floor_20k):
        before = json_wall_rejections()
        detect_soft_block('{"ok": true}')
        detect_soft_block('{"a": 1}')
        assert json_wall_rejections() == before + 2

    def test_leading_whitespace_still_gated(self, floor_20k):
        assert detect_soft_block('   \n  {"ok": true}') is not None
        assert detect_soft_block('  \t [1, 2]') is None

    def test_json_floor_env_knob(self, monkeypatch, floor_20k):
        body = '{"pad": "' + "x" * 1024 + '"}'  # ~1KB: above default, below knob
        assert detect_soft_block(body) is None  # default 1024 → accept
        monkeypatch.setenv(SOFT_BLOCK_JSON_MIN_BYTES_ENV, "2000")
        block = detect_soft_block(body)
        assert block is not None  # knob 2000 → json_no_items
        assert block.reason == "json_no_items"

    def test_json_floor_zero_accepts_any_object(self, monkeypatch, floor_20k):
        monkeypatch.setenv(SOFT_BLOCK_JSON_MIN_BYTES_ENV, "0")
        assert detect_soft_block("{}") is None

    def test_strong_marker_inside_json_still_fires(self, floor_20k):
        body = '{"message": "Access Denied", "pad": "' + "x" * 2048 + '"}'
        block = detect_soft_block(body)
        assert block is not None
        assert block.reason == "challenge_marker"


class TestLegacyByteCompat:
    """wave-13/15/20 pins — non-JSON bodies take the legacy path unchanged."""

    def test_tiny_non_json_body_is_under_min_bytes(self, floor_20k):
        block = detect_soft_block("tiny stub page")
        assert block is not None
        assert block.reason == "under_min_bytes"

    def test_tiny_non_json_weak_token_corroborates(self, floor_20k):
        block = detect_soft_block("captcha help")
        assert block is not None
        assert block.reason == "challenge_marker"
        assert "_abck" not in str(block.markers) or block.markers

    def test_large_non_json_body_passes(self, floor_20k):
        body = "<html>" + "y" * 30_000 + "</html>"  # above the 20KB floor
        assert detect_soft_block(body) is None

    def test_floor_zero_disables_everything_including_json_wall(
        self, monkeypatch
    ):
        monkeypatch.setenv(FLOOR_ENV, "0")
        assert detect_soft_block('{"ok": true}') is None
        assert detect_soft_block("tiny") is None
        assert detect_soft_block("just a moment") is None

    def test_empty_body_passes(self, floor_20k):
        assert detect_soft_block("") is None


class TestLadderIntegration:
    """The SoftBlock stays falsy → fetch_text/fetch_json callers escalate."""

    def test_softblock_remains_falsy(self, floor_20k):
        block = detect_soft_block('{"ok": true}')
        assert not block

    def test_counter_accessor_roundtrip(self):
        reset_json_wall_rejections()
        assert json_wall_rejections() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
