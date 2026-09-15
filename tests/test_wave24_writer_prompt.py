"""[wave-24 W24-6] The writer burns its window reading templates that only
exist in its prompt.

Prod 393 cycle-1: four failed ``read_file`` attempts on
``templates/http_requests_scraper.py`` ("File not found") — the template
ships EMBEDDED in the code_writer system prompt (injected by
``_build_agent``), not on disk in the writer's container. Writers overall
spend 55–75% of each window on tool-call research before the first write;
this note removes one guaranteed-dead research loop.
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

import agents.subagents as sub  # noqa: E402

ROOT_DIR = ROOT  # repo root, for the wiring contract below


class TestEmbeddedTemplateNote:
    def test_embedded_template_carries_do_not_read_note(self):
        prompt = sub._embed_template("BASE PROMPT", "SCRAPER TEMPLATE CODE")
        assert "BASE PROMPT" in prompt
        assert "SCRAPER TEMPLATE CODE" in prompt, "template itself still embedded"
        assert "embedded in this prompt" in prompt
        assert "do NOT" in prompt
        assert "read_file templates/" in prompt
        assert "does not exist" in prompt

    def test_no_template_means_no_note(self):
        assert sub._embed_template("BASE PROMPT", "") == "BASE PROMPT"

    def test_builder_wires_the_helper(self):
        """Source contract: _build_agent routes template_code through the
        embed dispatch (the note must ride along with every embedded
        template). [wave-32 D5] The dispatch is now
        draft_context.render_writer_embed — full mode still delegates to
        _embed_template (pinned behaviorally in test_wave32_eow_diet), so
        the note rides along with every embedded template either way."""
        path = os.path.join(ROOT_DIR, "webapp", "agents", "subagents.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _build_agent("):src.index("def _install_invocation_cancellation(")]
        assert "render_writer_embed(" in body
        assert "_resolve_embed_mode(embed_mode)" in body
        assert 'system_prompt += (\n            "\\n\\n### Template' not in body, (
            "the inline embed block must be replaced by the dispatch call"
        )
