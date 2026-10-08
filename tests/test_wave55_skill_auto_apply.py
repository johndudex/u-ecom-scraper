"""[wave-55 H6] skill_learner's proposals reach the skill DB — the write arm.

Audit (2026-10-05, H6 #21): skill learning was structurally dead.
``skill_learner`` has no write tools (wave-29 A1) — it only *reports*
proposals into ``learning_report.json`` — and it defers to
``nav_skill_review``, which runs BEFORE it in the graph (right after
navigation_synthesize) and early-returns on re-scrapes and non-navigation
jobs. Net effect: every proposal died in an ephemeral workspace file, the
File Master's ``skills/_audit.jsonl`` went untouched after 2026-09-27, and
``Step navigation_skill_review`` measured a 17 ms median (an early return).

The fix is the smaller of the audit's two options: replay the report's
high-confidence, ``status: "applied"`` entries through the SAME write path
the admin UI uses (``src.skills_store.append_learned`` / ``create_skill`` —
flock-guarded, append-only, audit-trailed), actor-tagged
``skill_learner:auto`` so the trail distinguishes machine applies from
``ui:<username>`` ones. Capped at 2 applies per job.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest ../tests/test_wave55_skill_auto_apply.py -q"
"""
from __future__ import annotations

import json
import os
import sys
import types
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402

SLUG = "amx-com"
ACTOR = "skill_learner:auto"


def _fm_stub(state: dict):
    """In-memory ``src.artifacts`` (same shape as tests/test_skills_store.py)."""
    art = types.ModuleType("src.artifacts")

    def _read(k):
        if k in state:
            return state[k]
        raise FileNotFoundError(k)

    art.read_text = mock.Mock(side_effect=_read)
    art.write_text = mock.Mock(side_effect=lambda k, v: state.__setitem__(k, v))
    art.exists = mock.Mock(side_effect=lambda k: k in state)
    art.list_keys = mock.Mock(side_effect=lambda p: [k for k in state if k.startswith(p)])
    return art


class _FMPatch:
    """Patch BOTH sys.modules and the package attribute — ``from X import Y``
    inside skills_store would otherwise bypass a sys.modules-only patch."""

    def __init__(self, art):
        self.art = art

    def __enter__(self):
        import src as _src_pkg

        self._real = getattr(_src_pkg, "artifacts", None)
        _src_pkg.artifacts = self.art
        self._dict = mock.patch.dict(sys.modules, {"src.artifacts": self.art})
        self._dict.__enter__()
        return self

    def __exit__(self, *a):
        import src as _src_pkg

        if self._real is not None:
            _src_pkg.artifacts = self._real
        else:
            delattr(_src_pkg, "artifacts")
        self._dict.__exit__(*a)
        return False


def _write_report(root, slug, entries, raw=None):
    d = os.path.join(str(root), "workspace", slug)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "learning_report.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw if raw is not None else json.dumps({
            "site_slug": slug, "potential_learnings": entries,
        }))
    return path


APPLIED_APPEND = {
    "title": "Turnstile bypass via cookie warmup",
    "category": "anti-bot",
    "existing_skill": "anti-bot-handling",
    "action": "update",
    "reusability": "high",
    "description": "Warm the cf_clearance cookie on the homepage before the PDP fetch.",
    "evidence": ["site_analysis.json showed Turnstile", "test_report shows bypass worked"],
    "status": "applied",
    "reason": "generic across Turnstile sites",
}

APPLIED_NEW = {
    "title": "Swell storefront detection",
    "category": "platform",
    "existing_skill": "",
    "action": "create",
    "reusability": "high",
    "description": "Swell serves storefront.swell.store API roots.",
    "status": "applied",
    "proposed_new_skill": {
        "name": "swell-detection",
        "description": "Detect Swell storefronts",
        "body": "Look for storefront.swell.store in the HTML.",
    },
}


def _run(monkeypatch, tmp_path, fm_state, entries=None, raw=None, slug=SLUG):
    from agents.nodes import skill_learner as mod

    if entries is not None:
        _write_report(tmp_path, slug, entries)
    elif raw is not None:
        _write_report(tmp_path, slug, [], raw=raw)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    with _FMPatch(_fm_stub(fm_state)):
        return mod.apply_learning_proposals(
            {"job_id": 7, "site_slug": slug, "url": f"https://{SLUG}/p/1"}
        )


def _seeded_skill(state, name="anti-bot-handling"):
    state[f"skills/{name}/SKILL.md"] = (
        "---\nname: anti-bot-handling\ndescription: Anti-bot techniques\n---\n\n"
        "# Anti-bot\n\nBase body.\n"
    )


class TestProposalSelection:
    @pytest.mark.django_db
    def test_a_high_confidence_applied_entry_is_written_to_the_skill(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)

        result = _run(monkeypatch, tmp_path, state, [APPLIED_APPEND])

        body = state["skills/anti-bot-handling/SKILL.md"]
        assert "## Learned: Turnstile bypass via cookie warmup" in body
        assert "cf_clearance" in body
        assert result["applied"] and not result["skipped"]

    @pytest.mark.django_db
    def test_the_audit_row_carries_the_skill_learner_auto_actor(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)

        _run(monkeypatch, tmp_path, state, [APPLIED_APPEND])

        audit = state["skills/_audit.jsonl"]
        assert '"actor": "skill_learner:auto"' in audit
        # ui:<username> is the only other actor shape the store writes.
        assert '"actor": "ui:' not in audit

    @pytest.mark.django_db
    def test_a_below_threshold_entry_is_never_applied(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)
        low = dict(APPLIED_APPEND, reusability="medium")

        result = _run(monkeypatch, tmp_path, state, [low])

        assert "## Learned:" not in state["skills/anti-bot-handling/SKILL.md"]
        assert result["applied"] == [] and len(result["skipped"]) == 1

    @pytest.mark.django_db
    def test_a_skipped_entry_is_never_applied(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)
        skipped = dict(APPLIED_APPEND, status="skipped_duplicate")

        result = _run(monkeypatch, tmp_path, state, [skipped])

        assert "## Learned:" not in state["skills/anti-bot-handling/SKILL.md"]
        assert result["applied"] == []

    @pytest.mark.django_db
    def test_proposed_new_skill_creates_a_skill_via_the_store(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)

        result = _run(monkeypatch, tmp_path, state, [APPLIED_NEW])

        body = state["skills/swell-detection/SKILL.md"]
        assert "name: swell-detection" in body
        assert "storefront.swell.store" in body
        assert result["applied"][0]["skill"] == "swell-detection"

    @pytest.mark.django_db
    def test_an_entry_with_no_actionable_payload_is_skipped(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)
        vague = {
            "title": "Something about footers",
            "existing_skill": "",
            "reusability": "high",
            "status": "applied",
            "description": "The page has a footer.",
        }

        result = _run(monkeypatch, tmp_path, state, [vague])

        assert result["applied"] == []
        assert result["skipped"][0]["reason"]

    @pytest.mark.django_db
    def test_at_most_two_proposals_are_applied_per_job(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)
        entries = [dict(APPLIED_APPEND, title=f"Learning number {i}") for i in range(4)]

        result = _run(monkeypatch, tmp_path, state, entries)

        assert len(result["applied"]) == 2
        assert len(result["skipped"]) == 2
        body = state["skills/anti-bot-handling/SKILL.md"]
        assert "## Learned: Learning number 0" in body
        assert "## Learned: Learning number 1" in body
        assert "## Learned: Learning number 2" not in body

    @pytest.mark.django_db
    def test_the_threshold_and_cap_are_module_constants(self):
        from agents.nodes import skill_learner as mod

        assert mod.MAX_AUTO_APPLY_PER_JOB == 2
        assert mod.MIN_PROPOSAL_CONFIDENCE == "high"
        assert mod.ACTOR == "skill_learner:auto"


class TestRobustness:
    @pytest.mark.django_db
    def test_a_missing_report_is_a_noop(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)

        result = _run(monkeypatch, tmp_path, state, entries=None)

        assert result == {}
        assert "skills/_audit.jsonl" not in state

    @pytest.mark.django_db
    def test_a_corrupt_report_is_a_noop(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state)

        result = _run(monkeypatch, tmp_path, state, raw="{not json")

        assert result == {}
        assert "skills/_audit.jsonl" not in state

    @pytest.mark.django_db
    def test_an_empty_slug_is_a_noop(self, monkeypatch, tmp_path):
        result = _run(monkeypatch, tmp_path, {}, [APPLIED_APPEND], slug="")

        assert result == {}

    @pytest.mark.django_db
    def test_an_fm_failure_never_raises(self, monkeypatch, tmp_path):
        from agents.nodes import skill_learner as mod

        _write_report(tmp_path, SLUG, [APPLIED_APPEND])
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
        art = _fm_stub({})
        art.read_text = mock.Mock(side_effect=RuntimeError("FM down"))

        with _FMPatch(art):
            result = mod.apply_learning_proposals(
                {"job_id": 7, "site_slug": SLUG, "url": f"https://{SLUG}/p/1"}
            )

        assert result["applied"] == []
        assert len(result["skipped"]) == 1

    @pytest.mark.django_db
    def test_a_duplicate_title_surfaces_as_a_skip_not_a_crash(self, monkeypatch, tmp_path):
        state: dict = {}
        _seeded_skill(state, name="anti-bot-handling")
        _seeded_skill(state, name="navigation-patterns")
        state["skills/anti-bot-handling/SKILL.md"] += (
            "\n## Learned: Turnstile bypass via cookie warmup\n"
            "**Source:** elsewhere\n\nOld body.\n"
        )

        result = _run(monkeypatch, tmp_path, state, [APPLIED_APPEND])

        assert result["applied"] == []
        assert result["skipped"][0]["reason"]
        assert state["skills/anti-bot-handling/SKILL.md"].count("## Learned: Turnstile") == 1


class TestNodeWiring:
    """``_invoke_skill_learner`` must actually call the applier — the audit's
    defect is a report nobody reads."""

    @pytest.mark.django_db
    def test_the_skill_learner_node_calls_the_auto_apply(self, monkeypatch, tmp_path):
        import agents.graph as graph
        from agents.nodes import skill_learner as mod

        _write_report(tmp_path, SLUG, [APPLIED_APPEND])
        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))

        calls: list = []
        monkeypatch.setattr(
            mod, "apply_learning_proposals", lambda state: calls.append(state) or {}
        )
        # Stub every collaborator the node touches; the node is a tail phase
        # and must never block on them.
        monkeypatch.setattr(graph, "build_skill_learner_message", lambda state: [])
        monkeypatch.setattr(
            graph, "create_skill_learner", lambda site_slug="": mock.Mock()
        )
        monkeypatch.setattr(
            graph, "_invoke_agent_with_timeout",
            lambda *a, **k: {"output_text": "done"},
        )
        monkeypatch.setattr(graph, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_start_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_stop_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_record_writer_memory", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(graph, "set_tool_context", lambda *a, **k: None)
        monkeypatch.setattr(graph, "clear_tool_context", lambda *a, **k: None)

        out = graph._invoke_skill_learner(
            {"job_id": 7, "site_slug": SLUG, "url": f"https://{SLUG}/p/1",
             "execution_status": "SUCCESS"},
            {},
        )

        assert out == {"messages": []}
        assert calls and calls[0]["site_slug"] == SLUG

    @pytest.mark.django_db
    def test_a_failing_auto_apply_never_fails_the_job(self, monkeypatch, tmp_path):
        import agents.graph as graph

        monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))

        def _boom(state):
            raise RuntimeError("skills down")

        monkeypatch.setattr("agents.nodes.skill_learner.apply_learning_proposals", _boom)
        monkeypatch.setattr(graph, "build_skill_learner_message", lambda state: [])
        monkeypatch.setattr(
            graph, "create_skill_learner", lambda site_slug="": mock.Mock()
        )
        monkeypatch.setattr(
            graph, "_invoke_agent_with_timeout",
            lambda *a, **k: {"output_text": "done"},
        )
        monkeypatch.setattr(graph, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_start_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_stop_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_record_writer_memory", lambda *a, **k: None)
        monkeypatch.setattr(graph, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(graph, "set_tool_context", lambda *a, **k: None)
        monkeypatch.setattr(graph, "clear_tool_context", lambda *a, **k: None)

        out = graph._invoke_skill_learner(
            {"job_id": 7, "site_slug": SLUG, "execution_status": "SUCCESS"}, {}
        )

        assert out == {"messages": []}
