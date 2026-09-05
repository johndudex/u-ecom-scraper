"""[wave-20 T3] Writer retry context must carry the two prod gotchas.

Prod RCA (jobs 359 michaelhill / 360 marimekko, 2026-09-05): across every
retry cycle the writer regenerated drafts carrying the SAME two defects the
cycle started with, because nothing in its retry context named them:

- 360: the draft invented a mode gate ("Phase 1: SKIPPED (url_list mode with
  1 seed URLs)") that let the existence of ``input_urls.json`` — a file the
  TESTER writes — override the explicit ``--fresh-discovery``/`--discover-only``
  flags. Phase 1 never ran, on every cycle.
- 359: the parser was inert — analyzer-captured JavaScript expressions were
  pasted verbatim (JS is not Python) and parse exceptions were swallowed
  (``except: return []``). Pages fetched healthy; zero items extracted, on
  every cycle.

Contract (deterministic, no LLM trust):

- report with ``discovery_coverage.stop_reason == "phase1_skipped"`` → the
  builder's message carries the DISCOVERY-FLAGS-OVERRIDE gotcha;
- report with ``remediation.target == "scraper"`` + zero extractions + NO
  crash → the builder's message carries the PORT-BROWSER-JS gotcha (healthy
  fetch + zero items = inert parser);
- any other failed report carries NEITHER (negative controls);
- the standing agent prompt (``.opencode/agents/code-writer.md``) names both
  gotchas — file-content contract so the guidance cannot silently vanish.
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

from agents import subagents as sub  # noqa: E402

WRITER_MD = os.path.join(ROOT, ".opencode", "agents", "code-writer.md")

BASE = {
    "site_slug": "marimekko-com",
    "url": "https://www.marimekko.com/us_en/store",
    "sample_url": "https://www.marimekko.com/us_en/harhautus-unikko-cardigan",
    "strategy": "http_navigation",
}


def _msg(state: dict) -> str:
    return str(sub.build_code_writer_message(state)[0].content)


class TestPhase1SkippedGotcha:
    def test_skipped_report_carries_flags_override_gotcha(self):
        state = dict(BASE, test_retry_count=1, test_report={
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "discovery_coverage": {
                "ran_phase1": True,  # forced True by the zero-yield guard
                "stop_reason": "phase1_skipped",
                "discovered_urls": 0,
            },
        })
        msg = _msg(state)
        assert "DISCOVERY FLAGS OVERRIDE" in msg
        assert "input_urls.json" in msg
        assert "--fresh-discovery" in msg or "--discover-only" in msg
        assert "phase1_skipped" in msg

    def test_ordinary_failure_does_not_carry_it(self):
        state = dict(BASE, test_retry_count=1, test_report={
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": "empty_first_page",
                "discovered_urls": 0,
            },
        })
        assert "DISCOVERY FLAGS OVERRIDE" not in _msg(state)


class TestInertParserGotcha:
    def test_scraper_remediation_zero_items_no_crash_carries_it(self):
        # The michaelhill shape: healthy fetches, zero items, tester named
        # the code. No crash_error anywhere in the report.
        state = dict(BASE, test_retry_count=1, test_report={
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "crash_error": "",
            "discovery_coverage": {"ran_phase1": True, "discovered_urls": 0},
            "remediation": {"target": "scraper", "field": "extraction"},
        })
        msg = _msg(state)
        assert "PORT BROWSER-JS" in msg
        assert "json.loads" in msg
        assert "swallow" in msg.lower()

    def test_crash_report_does_not_carry_it(self):
        # A real traceback is NOT the inert-parser shape (Fix A already
        # surfaces the raw error); the gotcha must not fire on it.
        state = dict(BASE, test_retry_count=1, test_report={
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "crash_error": "NameError: name 'session' is not defined",
            "remediation": {"target": "scraper", "field": "extraction"},
        })
        assert "PORT BROWSER-JS" not in _msg(state)

    def test_some_items_extracted_does_not_carry_it(self):
        # Parser demonstrably works when items>0 — no parsing sermon needed.
        state = dict(BASE, test_retry_count=1, test_report={
            "overall_assessment": "NEEDS_FIXES",
            "results": {"successful_extractions": 3},
            "remediation": {"target": "scraper", "field": "extraction"},
        })
        assert "PORT BROWSER-JS" not in _msg(state)


class TestStandingPromptContract:
    def test_writer_prompt_names_both_gotchas(self):
        with open(WRITER_MD, encoding="utf-8") as fh:
            prompt = fh.read()
        # Mode precedence: seed file never beats the discovery flags.
        assert "input_urls.json" in prompt
        assert "--fresh-discovery" in prompt or "--discover-only" in prompt
        # JSON-LD porting: pure-Python parse, exceptions surface.
        assert "json.loads" in prompt
        assert "application/ld+json" in prompt


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
