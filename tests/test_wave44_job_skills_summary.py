"""[wave-44] Per-job skill provenance on the intake deep-link page.

User ask: "can each job in /intake/?job={id} show which skills it used and
which skills it wrote?"

Evidence base (all read-only, derived from existing rows — no new writes,
no migration):
- USED (injected): the writer's prompt carries deterministic distillation
  blocks whose header is baked by
  ``webapp.agents.subagents._learned_notes_block``:
  "### LEARNED SKILL NOTES (from `<skill>`, newest first ...". Verified on
  real jobs 455/460-463 (wave-43 e2e).
- USED (loaded): ToolCallLog rows with tool_name='load_skill' and
  args_summary "Load skill: <skill>" (wave-29 A2 telemetry).
- WRITTEN (actual): SessionLog "[TOOL] learn_skill: {...}" /
  "[TOOL] create_new_skill: {...}" rows — nav_skill_review is the only
  agent wired with write tools (wave-29 A1).
- WRITTEN (proposed): skill_learner's analysis summary contains
  "Skills modified: [<skill> (annotation), ...]" and "Skills created: [...]".
  These are PROPOSALS — skill_learner has no write tools (wave-29 A1 pins
  that), so the UI must never present them as actual writes.

Parsers are pure functions over row-like objects so tests don't need the DB;
``summarize_job_skills`` is the thin DB wrapper surfaced in job_api.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

from webapp.scraper.skills_summary import (  # noqa: E402
    parse_injected,
    parse_loaded,
    parse_written,
)


class _Row:
    """Duck-typed stand-in for SessionLog / ToolCallLog rows."""

    def __init__(self, agent="", content="", tool_name="", args_summary=""):
        self.agent = agent
        self.content = content
        self.tool_name = tool_name
        self.args_summary = args_summary


INJECTED_ROW = _Row(
    agent="code-writer",
    content=(
        "target_fields: price, url, availability, title\n\n"
        "### LEARNED SKILL NOTES (from `shopify-detection`, newest first — "
        "curated by post-run review; apply when relevant)\n"
        "## Learned: PDP JSON-LD\nuse @id #product\n\n"
        "### LEARNED SKILL NOTES (from `output-schema-integrity`, newest "
        "first — curated by post-run review; apply when relevant)\n"
        "## Learned: honest empty\nempty means empty\n"
    ),
)


class TestParseInjected:
    def test_extracts_every_injected_skill_from_writer_row(self):
        # sorted contract — deterministic regardless of block order
        assert parse_injected([INJECTED_ROW]) == [
            "output-schema-integrity",
            "shopify-detection",
        ]

    def test_dedupes_across_rows_and_sorts(self):
        other = _Row(
            agent="code-writer",
            content="### LEARNED SKILL NOTES (from `shopify-detection`, x)",
        )
        assert parse_injected([INJECTED_ROW, other]) == [
            "output-schema-integrity",
            "shopify-detection",
        ]

    def test_no_injection_rows_is_empty(self):
        plain = _Row(agent="code-writer", content="normal context, no notes")
        assert parse_injected([plain]) == []

    def test_platform_mention_without_block_header_is_ignored(self):
        """The word 'magento' in a verdict must NOT read as an injection —
        only the _learned_notes_block header counts (wave-43 negation guard
        means platform names appear in prompts that injected nothing)."""
        trap = _Row(
            agent="code-writer",
            content="platform: custom (no shopify/magento/sfcc markers)",
        )
        assert parse_injected([trap]) == []


class TestParseLoaded:
    def test_reads_toolcalllog_rows_with_agent(self):
        rows = [
            _Row(
                tool_name="load_skill",
                agent="product-analyzer",
                args_summary="Load skill: shopify-detection",
            ),
            _Row(
                tool_name="load_skill",
                agent="code-writer",
                args_summary="Load skill: netsuite-detection",
            ),
        ]
        assert parse_loaded(rows) == [
            {"skill": "shopify-detection", "agent": "product-analyzer"},
            {"skill": "netsuite-detection", "agent": "code-writer"},
        ]

    def test_ignores_other_tools_and_unparseable_summaries(self):
        rows = [
            _Row(
                tool_name="read_file",
                agent="code-writer",
                args_summary="Load skill: should-not-count",
            ),
            _Row(tool_name="load_skill", agent="code-writer", args_summary=""),
        ]
        assert parse_loaded(rows) == []

    def test_dedupes_same_skill_same_agent(self):
        rows = [
            _Row(
                tool_name="load_skill",
                agent="a1",
                args_summary="Load skill: x-detection",
            ),
            _Row(
                tool_name="load_skill",
                agent="a1",
                args_summary="Load skill: x-detection",
            ),
            _Row(
                tool_name="load_skill",
                agent="a2",
                args_summary="Load skill: x-detection",
            ),
        ]
        assert len(parse_loaded(rows)) == 2


WRITE_PROPOSAL_ROW = _Row(
    agent="skill-learner",
    content=(
        "✓ Learning analysis complete\n"
        "  Site: hellobubble-com\n"
        "  Potential learnings: 5\n"
        "  Applied: 2\n"
        "  Skills modified: [jsonld-extraction (proposed appends)]\n"
        "  Skills created: []\n"
    ),
)
WRITE_ACTUAL_ROW = _Row(
    agent="nav-skill-review",
    content="[TOOL] learn_skill: {'skill_name': 'magento-detection', "
    "'title': 'PWA listing lesson'}",
)
CREATE_ACTUAL_ROW = _Row(
    agent="nav-skill-review",
    content="[TOOL] create_new_skill: {'name': 'brand-new-skill', 'description': 'd'}",
)


class TestParseWritten:
    def test_proposals_are_reported_separately_from_writes(self):
        actual, proposed = parse_written([WRITE_PROPOSAL_ROW])
        assert actual == []
        assert proposed == ["jsonld-extraction"]

    def test_proposal_annotation_is_stripped(self):
        _, proposed = parse_written([WRITE_PROPOSAL_ROW])
        assert proposed == ["jsonld-extraction"]
        assert "proposed appends" not in proposed[0]

    def test_actual_learn_and_create_tool_rows_count(self):
        actual, proposed = parse_written([WRITE_ACTUAL_ROW, CREATE_ACTUAL_ROW])
        assert actual == ["magento-detection", "brand-new-skill"]
        assert proposed == []

    def test_empty_created_list_is_not_a_skill(self):
        actual, proposed = parse_written([WRITE_PROPOSAL_ROW])
        assert "Skills created" not in proposed
        assert actual == [] and proposed == ["jsonld-extraction"]

    def test_learner_summary_is_never_labelled_actual(self):
        """The wave-29 pin: skill_learner has NO write tools, so its
        'Skills modified' line must land in proposed even if the row's
        agent string drifts."""
        drifted = _Row(agent="skill_learner_v2", content=WRITE_PROPOSAL_ROW.content)
        actual, proposed = parse_written([drifted])
        assert actual == [] and proposed == ["jsonld-extraction"]


# ─── DB wrapper + job_api wiring ─────────────────────────────────────────────


@pytest.mark.django_db
class TestSummarizeJobSkillsDB:
    """summarize_job_skills against real rows, and the job_api 'skills' key."""

    @classmethod
    def _mk_job(cls):
        from django.contrib.auth.models import User
        from scraper.models import ScrapeJob

        user, _ = User.objects.get_or_create(username="w44owner")
        return ScrapeJob.objects.create(
            url="https://example.com/products/x", user=user, status="completed"
        )

    def test_wrapper_classifies_real_rows(self):
        from scraper.models import SessionLog, ToolCallLog
        from scraper.skills_summary import summarize_job_skills

        job = self._mk_job()
        SessionLog.objects.create(
            job=job,
            agent="code-writer",
            seq=1,
            content="### LEARNED SKILL NOTES (from `shopify-detection`, newest first) body",
        )
        SessionLog.objects.create(
            job=job,
            agent="skill-learner",
            seq=2,
            content="Applied: 1\n  Skills modified: [jsonld-extraction (proposed appends)]\n  Skills created: []",
        )
        ToolCallLog.objects.create(
            job=job,
            agent="product-analyzer",
            tool_name="load_skill",
            call_seq=1,
            args_summary="Load skill: shopify-detection",
        )
        s = summarize_job_skills(job.id)
        assert s["injected"] == ["shopify-detection"]
        assert s["loaded"] == [
            {"skill": "shopify-detection", "agent": "product-analyzer"}
        ]
        assert s["written"] == [] and s["proposed"] == ["jsonld-extraction"]

    def test_job_with_no_skill_rows_reports_empty(self):
        from scraper.skills_summary import summarize_job_skills

        job = self._mk_job()
        s = summarize_job_skills(job.id)
        assert s == {"injected": [], "loaded": [], "written": [], "proposed": []}

    def test_job_api_response_carries_skills_key(self):
        from django.test import Client
        from scraper.models import SessionLog

        job = self._mk_job()
        SessionLog.objects.create(
            job=job,
            agent="code-writer",
            seq=1,
            content="### LEARNED SKILL NOTES (from `sfcc-detection`, newest first) body",
        )
        c = Client()
        c.force_login(job.user)
        r = c.get(f"/jobs/{job.id}/api/")
        assert r.status_code == 200
        assert r.json()["skills"]["injected"] == ["sfcc-detection"]
