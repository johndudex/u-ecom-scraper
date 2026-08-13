"""Single source of truth for pagination selectors + regexes.

Imported by BOTH layers that must agree on what "load more" / "next page"
looks like:

  * Layer A — ``experimental/nav_traversal/traversal.py`` (the ``_PAGE_STATE_JS``
    probe that classifies pagination type via ``has_load_more`` /
    ``has_rel_next_page_param``).
  * Layer C — ``src/discovery.py`` (``DEFAULT_LOAD_MORE_SELECTORS`` /
    ``DEFAULT_NEXT_BUTTON_SELECTORS`` / ``_OFFSET_PARAMS`` — the clicker that
    actually drives discovery at run time).

Before this module existed the two layers drifted: Layer A's JS probe checked
3 load-more selectors while Layer C's clicker checked 9, so Layer A could
report ``has_load_more=false`` on a page where Layer C's clicker would have
succeeded — producing a ``discovery_config.json`` with the wrong
``pagination.type`` and a strategy that can't paginate.

Pure-python (no Django, no Playwright). Mirrors the ``src/job_fields.py`` /
``src/page_analysis.py`` convention.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class PaginationPatterns:
    # ── load-more CLICK targets (Layer C clicker — the liberal superset) ──
    # Order matters: ``click_load_more`` clicks the FIRST match it can. The
    # originals appear first so a page that matched before still matches on the
    # same element. Includes the broad aria-label/magicbox selectors as click
    # FALLBACKS — a wrong click in a scroll/load_more fallback is low-stakes.
    load_more_selectors: tuple[str, ...] = (
        "[class*='load-more' i]", "[class*='loadMore' i]",
        "[class*='show-more' i]", "[class*='showMore' i]",
        "[class*='pager-next' i]", "[class*='pagerNext' i]",
        "button[aria-label*='more' i]", "a[aria-label*='next' i]",
        ".coveo-magicbox-load-more",
    )

    # ── load-more PRESENCE selectors (Layer A classifier — conservative) ──
    # Subset of load_more_selectors: ONLY the unambiguous class-substring
    # selectors. The broad ``button[aria-label*='more' i]`` /
    # ``a[aria-label*='next' i]`` / ``.coveo-magicbox-load-more`` are EXCLUDED
    # from CLASSIFICATION because they false-positive on non-pagination buttons
    # whose label/class merely CONTAINS "more"/"next" — e.g. Coveo facet
    # checkboxes for "Morehead State University" / "Skidmore College" (visible +
    # enabled, so the visibility gate can't filter them). A false
    # has_load_more=true MISROUTES the whole job to load_more; a wrong click in
    # a click fallback only no-ops, so classification stays conservative while
    # clicking stays liberal. Verified on lw.com (Coveo): pager-next still
    # detected (→ correct load_more) while the 6 school-name false positives no
    # longer flip classification.
    load_more_presence_selectors: tuple[str, ...] = (
        "[class*='load-more' i]", "[class*='loadMore' i]",
        "[class*='show-more' i]", "[class*='showMore' i]",
        "[class*='pager-next' i]", "[class*='pagerNext' i]",
    )

    # ── next-button click targets (Layer C only — semantic fallbacks) ──
    # Layer A does NOT inject these into the JS probe: its rel-next check is a
    # URL-evidence probe (?page=N in the href), not a click-target search.
    next_button_selectors: tuple[str, ...] = (
        'a[rel="next"]', "a.next", "li.next a",
        '[aria-label*="next" i]',
        'a:has-text("Next")', 'button:has-text("Next")',
    )

    # ── Layer A — the one URL-evidence selector + its param regex ──
    rel_next_selector: str = 'a[rel="next"]'
    page_param_regex: str = r"[?&](page|p|pg|pn|pagenum|start|offset)=(\d+)"

    # ── Layer C — offset-style query params (value = (page-1)*ipp, not page no.) ──
    offset_params: frozenset[str] = frozenset({"offset", "start", "skip", "begin", "from"})

    @property
    def load_more_css_list(self) -> str:
        """Comma-joined Layer-C click list as a JS string literal (see below)."""
        return self._css_list(self.load_more_selectors)

    @property
    def load_more_presence_css_list(self) -> str:
        """Comma-joined Layer-A presence list as a JS string literal.

        This is what ``_PAGE_STATE_JS`` injects — the conservative subset (see
        ``load_more_presence_selectors``) so classification doesn't false-flip
        on broad aria-label matches.
        """
        return self._css_list(self.load_more_presence_selectors)

    @staticmethod
    def _css_list(selectors: tuple[str, ...]) -> str:
        """``json.dumps`` the comma-joined selectors into a valid JS string arg.

        Wraps the list in double quotes and preserves the single quotes inside
        each CSS attribute selector untouched — e.g.
        ``"[class*='load-more' i],[class*='loadMore' i],..."`` — a valid
        argument for ``document.querySelectorAll(...)``.
        """
        return json.dumps(",".join(selectors))


PATTERNS: Final[PaginationPatterns] = PaginationPatterns()

# ── Backward-compatible aliases (existing callers in src/discovery.py unchanged) ──
DEFAULT_LOAD_MORE_SELECTORS: tuple[str, ...] = PATTERNS.load_more_selectors
DEFAULT_NEXT_BUTTON_SELECTORS: tuple[str, ...] = PATTERNS.next_button_selectors
_OFFSET_PARAMS: frozenset[str] = PATTERNS.offset_params
PAGE_PARAM_REGEX: str = PATTERNS.page_param_regex
REL_NEXT_SELECTOR: str = PATTERNS.rel_next_selector
