"""[wave-17 S24 revert] The blocked-PDP settle-and-refetch arm must stay dead.

History: the "S24" hunk (committed inside 39cbf29) added three things to
``templates/http_navigation_scraper.py`` — a per-page settle floor
(``NAVIGATE_SETTLE_MS``), a Phase-2 concurrency drop (``PHASE2_WORKERS`` 4 →
2), and a 90s settle + ONE refetch for a challenged PDP
(``BLOCKED_RETRY_SETTLE_S``). The session-audit (2026-09-05) overturned the
RCA the refetch arm was built on: job 329 did not die of a per-IP rate band —
it died of a SyntaxError written by an abandoned wall-clock writer thread
(see tests/test_draft_corruption_gates.py). The band-trip failure the refetch
arm targets NEVER OCCURRED, and on a real band the arm is harmful: a 90s
window rarely outlives a per-IP band, so the refetch just doubles hits on an
edge that has already refused the identity twice.

Decision (user, 2026-09-04): keep the two evidence-backed pieces, remove the
refetch arm — a challenged PDP fails HONESTLY on first refusal, consistent
with the S23 challenge-tripwire doctrine (an interstitial is never a product,
and a wall is never worth a second knock).

Static contract per the template-test convention (templates are not
importable — see test_template_output_collision / test_discovery_ladder).
"""
from __future__ import annotations

import os

TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
TEMPLATE = os.path.join(TEMPLATES_DIR, "http_navigation_scraper.py")


def _src() -> str:
    with open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def _extract_item_body(src: str) -> str:
    fn_start = src.index("def _extract_item(")
    fn_end = src.index("def ", fn_start + 10)
    return src[fn_start:fn_end]


class TestRefetchArmRemoved:
    def test_blocked_retry_settle_constant_is_gone(self):
        assert "BLOCKED_RETRY_SETTLE_S" not in _src(), (
            "the 90s blocked-PDP refetch constant targets a band-trip failure "
            "that never occurred (329 was the SyntaxError) — remove the arm"
        )

    def test_challenged_pdp_is_not_refetched(self):
        body = _extract_item_body(_src())
        assert body.count("_navigate(item_url)") == 1, (
            "a challenged PDP must fail honestly on first refusal — no second "
            "_navigate(item_url) behind the blocked check"
        )
        assert "refetching once" not in body

    def test_s24_docstring_rationale_is_gone(self):
        assert "[wave-17 S24]" not in _src()


class TestEvidenceBackedPiecesRetained:
    """Guard the KEEP side of the split against an over-zealous revert."""

    def test_settle_floor_retained(self):
        src = _src()
        assert "NAVIGATE_SETTLE_MS" in src

    def test_phase2_workers_still_two(self):
        src = _src()
        assert "PHASE2_WORKERS = 2" in src

    def test_extract_item_still_paces(self):
        body = _extract_item_body(_src())
        assert "if DELAY_BETWEEN_REQUESTS:" in body, (
            "Phase 2 pacing (DELAY_BETWEEN_REQUESTS before the PDP fetch) was "
            "kept by the split decision — the concurrent executor hides bursts"
        )
