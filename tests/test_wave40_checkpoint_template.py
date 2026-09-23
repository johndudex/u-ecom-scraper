"""[wave-40 T14] both phase-1 templates carry a checkpoint rescue branch
between the zero-yield reclass and the DISCOVERY_ZERO exit; imports are lazy
(benign browser-service deploy skew); the honest exit is preserved.

Source-contract tests only (rev-2): the behavioural proof is T16's local
dead-seed gate, which drives real generated scrapers.

Deviations from the brief's verbatim draft (both flagged in the task report):
- ``_tpl`` resolves ``templates/`` from this file's repo root — pytest runs
  with cwd=/app/webapp inside the django container, so the brief's relative
  open would miss.
- ``test_honest_zero_exit_is_preserved`` uses one pattern per template: the
  brief guessed ``exit(3)|sys.exit(3)`` for both, but navigation_scraper.py has
  NO non-zero zero-discovery exit — its honest zero is the
  ``No item URLs discovered`` warning plus a 0-item output (found=0).
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The honest-zero marker each template must STILL carry, downstream of the
# rescue branch (the branch is inserted BEFORE it, never in place of it).
_HONEST_ZERO = {
    "http_navigation_scraper.py": re.compile(r"sys\.exit\(3\)"),
    "navigation_scraper.py": re.compile(r"No item URLs discovered"),
}


def _tpl(name):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as fh:
        src = fh.read()
    compile(src, name, "exec")          # sanity: still valid Python
    return src


def _rescue_region(src):
    m = re.search(r"zero_discovery_rescuable\(", src)
    assert m, "rescue branch absent"
    return src[m.start(): m.start() + 2500]


def _assert_env_gate(region):
    """[T14 ruling] the arming gate is the CALLER's obligation.

    ``load_discovery_checkpoint`` treats an UNSET ``SCRAPER_CHECKPOINT_REUSE``
    as enabled by design (it is a pure validator), so default-OFF has to live
    in this branch: without the ``== "1"`` read here, an un-armed run with a
    staged checkpoint would silently reuse it.
    """
    assert "SCRAPER_CHECKPOINT_REUSE" in region
    assert re.search(r"os\.getenv\(\s*[\"']SCRAPER_CHECKPOINT_REUSE[\"']\s*\)", region), \
        "branch must read the arming env itself (getenv), not just mention it"
    assert '== "1"' in region, "arming gate must compare against the ON value"


def test_http_navigation_template_has_rescue_branch():
    region = _rescue_region(_tpl("http_navigation_scraper.py"))
    assert "load_discovery_checkpoint" in region
    assert "checkpoint_coverage_patch" in region
    assert "[DISCOVERY-CHECKPOINT-REUSED]" in region
    assert "discover_only" in region                      # --discover-only exempt
    assert region.index("from src.listing_discovery import") >= 0  # lazy import
    _assert_env_gate(region)


def test_navigation_template_has_rescue_branch():
    region = _rescue_region(_tpl("navigation_scraper.py"))
    assert "load_discovery_checkpoint" in region
    assert "checkpoint_coverage_patch" in region          # merge-site name binding
    assert "[DISCOVERY-CHECKPOINT-REUSED]" in region
    assert "discover_only" in region                      # --discover-only exempt
    assert "from src.listing_discovery import" in region           # lazy import
    _assert_env_gate(region)


def test_honest_zero_exit_is_preserved():
    for name, pat in _HONEST_ZERO.items():
        src = _tpl(name)
        anchor = re.search(r"zero_discovery_rescuable\(", src)
        assert anchor, f"rescue branch absent in {name}"
        after = src[anchor.start():]
        assert pat.search(after), f"honest zero path lost from {name}"
