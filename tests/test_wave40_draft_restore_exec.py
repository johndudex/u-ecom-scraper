"""[wave-40 T10] run_execution must attempt draft restore before refusing
(prod 807: papier, PASS 0.94 + 25 products, draft vanished mid-run, job
FAILED 'scraper_draft.py not found'), and finalize must publish the draft.

Two halves, one job shape:
- execution: a missing ``workspace/{slug}/scraper_draft.py`` is no longer an
  unconditional refusal — ``draft_safety.restore_job_draft`` re-hydrates THIS
  job's per-job FM archive first; the honest refusal is the LAST resort.
- finalize: ``_publish_analysis_artifacts`` publishes the draft to that same
  per-job key, so even a later workspace loss leaves the FM holding the
  executable. Promotion to ``scrapers/{slug}/scraper.py`` stays T9's
  compile-gated job — this is the archive key only.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.django_db


def _re_mod():
    # agents.nodes.__init__ re-exports the run_execution FUNCTION over the
    # submodule — attribute access gives a function, not the module. Import it
    # under the ``agents`` identity so the node's function-local
    # ``from ..draft_safety import ...`` resolves to the SAME module object the
    # tests below patch (``webapp.agents.draft_safety`` is a *second*, distinct
    # module object in sys.modules — patching it would silently land nowhere).
    import agents.nodes.run_execution  # noqa: F401  (registers the submodule)

    return sys.modules["agents.nodes.run_execution"]


def test_missing_draft_is_restored_from_fm_archive(tmp_path, monkeypatch):
    re_mod = _re_mod()
    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    calls = {}

    def fake_restore(root, slug, job_id):
        calls["args"] = (root, slug, job_id)
        (ws / "scraper_draft.py").write_text("# restored\n", encoding="utf-8")
        return str(ws / "scraper_draft.py")

    monkeypatch.setattr("agents.draft_safety.restore_job_draft", fake_restore)
    out = re_mod._ensure_draft_or_restore(str(tmp_path), "papier", 807, ws)
    assert calls["args"] == (str(tmp_path), "papier", 807)
    assert (ws / "scraper_draft.py").exists()
    assert out is None  # no refusal — execution proceeds


def test_refusal_only_after_restore_fails(tmp_path, monkeypatch):
    """Restore produced nothing → the honest refusal REMAINS (no silent pass),
    with the pre-T10 message byte-identical."""
    re_mod = _re_mod()
    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    monkeypatch.setattr("agents.draft_safety.restore_job_draft",
                        lambda *a, **kw: None)
    out = re_mod._ensure_draft_or_restore(str(tmp_path), "papier", 807, ws)
    assert isinstance(out, dict)      # the honest refusal REMAINS (no silent pass)
    assert not (ws / "scraper_draft.py").exists()
    assert out["execution_status"] == "FAILED"
    assert out["error_message"] == (
        "scraper_draft.py not found at "
        + os.path.join(str(tmp_path), "workspace", "papier", "scraper_draft.py")
    )


def test_finalize_publishes_the_draft(tmp_path, monkeypatch):
    """_publish_analysis_artifacts must also publish
    workspace/{slug}/scraper_draft.py -> FM, so a later workspace loss can
    still be restored (the T10 half of the 807 fix)."""
    from django.conf import settings

    import scraper.tasks as t
    import src.artifacts as artifacts

    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft\n", encoding="utf-8")
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    published = {}
    monkeypatch.setattr(artifacts, "write",
                        lambda key, data: published.setdefault(key, data))
    t._publish_analysis_artifacts(807, "papier", ws)
    draft_keys = [k for k in published if "scraper-draft-807" in k]
    assert draft_keys, "the draft must reach its per-job FM archive key"
    assert published[draft_keys[0]] == b"# draft\n"
    # archive key ONLY — promotion to scrapers/{slug}/scraper.py stays T9's
    assert all("/jobs/" in k or "/analysis/" in k for k in published)


def test_finalize_without_a_draft_publishes_no_draft_key(tmp_path, monkeypatch):
    """The publish is guarded by the file existing: a draft that is already
    gone adds no key (and no error) to the finalize copy loop."""
    from django.conf import settings

    import scraper.tasks as t
    import src.artifacts as artifacts

    ws = tmp_path / "workspace" / "papier"
    ws.mkdir(parents=True)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    published = {}
    monkeypatch.setattr(artifacts, "write",
                        lambda key, data: published.setdefault(key, data))
    t._publish_analysis_artifacts(807, "papier", ws)
    assert not [k for k in published if "scraper-draft-" in k]
