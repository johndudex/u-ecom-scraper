"""Wave-23 W23-4: the template-fidelity guard must not fight a discovery
remediation.

RC-4 of docs/plans/wave23-writer-convergence-plan.md (prod 374): the injected
"[CONTEXT] Template fidelity" block orders "Do NOT re-signature or redefine
the template's discovery/pagination helpers ... CONTRACT", while the tester's
remediation for the actual defect ordered "rework DISCOVERY ONLY — switch the
discovery source". The writer was caught between contradictory instructions
for 33 minutes and produced zero writes in 99 calls.

Fix: when the retry context carries a REMEDIATION INSTRUCTION that targets
discovery, the fidelity block gains an explicit override clause — replacing
the template's DISCOVERY-source functions is then allowed; the CLI/argparse
and signature contracts stay binding.

Run from repo root:  python3 -m pytest tests/test_discovery_remediation_override.py -v
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

from agents.subagents import build_code_writer_message  # noqa: E402


def _state(feedback: str | None):
    state = {
        "url": "https://x.com",
        "site_slug": "x-com",
        "sample_url": "https://x.com/p/1",
        "product_url": "https://x.com/p/1",
        "input_mode": "list_page",
        "search_criteria": "",
        "content_type": "product",
        "page_type": "product",
    }
    if feedback is not None:
        state["test_report"] = {
            "verdict": "FAIL",
            "issues": [],
            "feedback_for_writer": feedback,
        }
    return state


class TestDiscoveryRemediationOverride:
    def test_discovery_remediation_gets_override_clause(self):
        msgs = build_code_writer_message(
            _state("Rework DISCOVERY ONLY: switch the discovery source to "
                   "sitemap.xml. Do NOT touch the PDP extraction.")
        )
        content = msgs[0].content
        assert "### Template fidelity" in content, "fidelity guard still present"
        assert "OVERRIDE" in content, (
            "a discovery-targeted remediation must lift the discovery-helper "
            "fidelity freeze — otherwise the writer gets contradictory orders"
        )

    def test_non_discovery_remediation_stays_frozen(self):
        msgs = build_code_writer_message(
            _state("Fix the price selector — prices are MISSING on every item.")
        )
        content = msgs[0].content
        assert "### Template fidelity" in content
        assert "OVERRIDE" not in content, (
            "non-discovery fixes have no reason to touch discovery helpers"
        )

    def test_first_invocation_no_remediation_no_override(self):
        msgs = build_code_writer_message(_state(None))
        content = msgs[0].content
        assert "### Template fidelity" in content
        assert "OVERRIDE" not in content


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
