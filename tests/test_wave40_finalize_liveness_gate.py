"""[wave-40 T11] finalize may not delete a workspace a live/queued sibling
job (same slug) still needs — and a job's own registration must not block
its own cleanup. Prod 765+807: workspace vanished mid-run; the URL-scoped
guard (tasks.py:1810-1815) is slug-blind.

Three legs, one registry (``agents/invocation_registry.py``, process-local,
stdlib-only):

- the REGISTRY: ``register``/``unregister`` bracket a task generation,
  ``register_abandoned`` records a wall-clock abandon that no token can ever
  clear (the daemon walk keeps going), ``alive_for_slug`` counts every entry
  and never special-cases job ids — the 765 zombie shares the resumed job's id;
- the GUARD: ``delete_blocked_reason`` = `_`-namespace refusal + liveness +
  the slug-scoped DB sibling check the old URL filter should have been;
- the DISPOSAL: ``tasks._maybe_delete_workspace`` = clear own token → guard →
  tombstone (same-filesystem rename into ``workspace/_trash/``) or delete,
  with ``FINALIZE_WORKSPACE_DELETE=0`` as the always-tombstone kill switch.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import time

import pytest

PAPIER = "https://www.papier.com/x"   # _generate_slug -> "papier-com"


def _reg():
    # ADAPT of the brief's ``reg._entries().clear()``: the store is two
    # process-local dicts behind a lock, so the module carries a dedicated
    # test-support reset. The autouse fixture below resets on BOTH sides —
    # abandoned entries are designed to outlive a task, and must not outlive
    # a test into a neighbouring file.
    import agents.invocation_registry as reg

    reg._reset_for_tests()
    return reg


@pytest.fixture(autouse=True)
def _clean_registry():
    _reg()
    yield
    _reg()


def _workspace(tmp_path, slug="papier-com", files=("scraper_draft.py",)):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True)
    for name in files:
        (ws / name).write_text("# draft")
    return ws


# ─── the registry ────────────────────────────────────────────────────────────


def test_registry_roundtrip_and_abandoned_outlives_unregister():
    reg = _reg()
    assert reg.alive_for_slug("nike-in") is False
    tok = reg.register("nike-in", 765)
    assert reg.alive_for_slug("nike-in") is True
    reg.unregister("nike-in", 765, tok)
    assert reg.alive_for_slug("nike-in") is False
    # an ABANDONED walk shares the job_id and outlives the task token:
    reg.register_abandoned("nike-in", 765)
    reg.unregister("nike-in", 765, "stale-token")   # task's own finally
    assert reg.alive_for_slug("nike-in") is True    # zombie still counted


def test_registry_is_one_module_object_under_both_import_paths():
    """Identity pin: in-container ``agents.*`` and ``webapp.agents.*`` are
    distinct module objects for the same file, and a split store would make
    graph.py's abandoned-walk registrations invisible to the finalize guard
    (``scraper.tasks`` imports the ``agents`` identity). Both names must
    resolve to ONE object — the module aliases its twin at import, first
    loader wins — and graph.py must reach it via the absolute ``agents``
    identity, never a relative one."""
    import agents.graph  # noqa: F401
    import agents.invocation_registry as via_agents

    import webapp.agents.invocation_registry as via_webapp

    assert sys.modules["agents.invocation_registry"] is \
        sys.modules["webapp.agents.invocation_registry"]
    assert via_agents is via_webapp
    assert via_agents._live is via_webapp._live
    source = inspect.getsource(agents.graph)
    assert "from agents import invocation_registry" in source, (
        "graph.py must import the registry by the absolute agents identity"
    )


@pytest.mark.django_db
def test_abandoned_generation_under_the_same_job_id_still_blocks(
        db, tmp_path, monkeypatch):
    """765: the zombie IS this job's own earlier generation — the resumed
    task's registration must not white-wash it. clear_own is token-scoped and
    alive_for_slug never special-cases job ids."""
    from scraper import tasks as t
    from scraper.models import ScrapeJob

    reg = _reg()
    monkeypatch.delenv("FINALIZE_WORKSPACE_DELETE", raising=False)
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    reg.register_abandoned("papier-com", mine.id)   # the timed-out walk
    tok = reg.register("papier-com", mine.id)       # the resumed generation
    ws = _workspace(tmp_path)
    out = t._maybe_delete_workspace(mine, ws)
    assert out == "tombstoned"
    assert not ws.exists()                          # moved, not destroyed
    trash = tmp_path / "workspace" / "_trash"
    assert trash.exists() and any(trash.iterdir())
    reg.unregister("papier-com", mine.id, tok)


# ─── the guard ───────────────────────────────────────────────────────────────


def test_trash_namespace_is_refused():
    reg = _reg()
    assert reg.delete_blocked_reason("_trash", exclude_job_id=None) != ""


@pytest.mark.django_db
def test_db_leg_is_slug_scoped_not_url_scoped(db):
    """783/790 accessorize: two concurrent jobs, same slug, DIFFERENT urls —
    exactly the case the old ``filter(url=job.url)`` guard could not see."""
    from scraper.models import ScrapeJob

    reg = _reg()
    mine = ScrapeJob.objects.create(
        url="https://www.accessorize.com/uk/sale/a",
        status=ScrapeJob.STATUS_RUNNING)
    ScrapeJob.objects.create(
        url="https://www.accessorize.com/uk/sale/b",
        status=ScrapeJob.STATUS_RUNNING)
    reason = reg.delete_blocked_reason("accessorize-com", exclude_job_id=mine.id)
    assert reason, "a same-slug RUNNING sibling must block the delete"
    assert str(mine.id) not in reason, "the job's own row must be excluded"


# ─── the disposal ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_finalize_tombstones_when_sibling_same_slug_is_running(
        db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import tasks as t
    from scraper.models import ScrapeJob

    reg = _reg()
    monkeypatch.delenv("FINALIZE_WORKSPACE_DELETE", raising=False)
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    other = ScrapeJob.objects.create(url="https://www.papier.com/y",
                                     status=ScrapeJob.STATUS_RUNNING)
    reg.register_abandoned("papier-com", other.id)
    ws = _workspace(tmp_path)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    out = t._maybe_delete_workspace(mine, ws)
    assert out == "tombstoned"
    assert not ws.exists()
    trash = tmp_path / "workspace" / "_trash"
    assert trash.exists() and any(trash.iterdir())


@pytest.mark.django_db
def test_own_registration_does_not_block_own_delete(db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import tasks as t
    from scraper.models import ScrapeJob

    reg = _reg()
    monkeypatch.delenv("FINALIZE_WORKSPACE_DELETE", raising=False)
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    tok = reg.register("papier-com", mine.id)
    ws = _workspace(tmp_path)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    out = t._maybe_delete_workspace(mine, ws)
    assert out == "deleted"            # read-and-clear cleared our own entry
    assert not ws.exists()
    reg.unregister("papier-com", mine.id, tok)


@pytest.mark.django_db
def test_kill_switch_always_tombstones(db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import tasks as t
    from scraper.models import ScrapeJob

    _reg()
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    mine = ScrapeJob.objects.create(url=PAPIER, status=ScrapeJob.STATUS_RUNNING)
    ws = _workspace(tmp_path)
    monkeypatch.setenv("FINALIZE_WORKSPACE_DELETE", "0")
    assert t._maybe_delete_workspace(mine, ws) == "tombstoned"
    assert not ws.exists()
    assert (tmp_path / "workspace" / "_trash").exists()


@pytest.mark.django_db
def test_finalize_job_wiring_tombstones_not_rmtree(db, tmp_path, monkeypatch):
    """The wiring pin: `_finalize_job` itself must route the tail through the
    guard — a same-slug RUNNING sibling with a DIFFERENT url (invisible to the
    old URL-scoped filter) keeps its workspace. Drives the real `_finalize_job`
    with the checkpoint read frozen (T9 harness shape)."""
    from types import SimpleNamespace

    from django.conf import settings
    from django.utils import timezone
    from scraper import tasks
    from scraper.models import ScrapeJob

    reg = _reg()
    monkeypatch.delenv("FINALIZE_WORKSPACE_DELETE", raising=False)
    mine = ScrapeJob.objects.create(
        url="https://www.papier.com/gifts",
        status=ScrapeJob.STATUS_RUNNING,
        started_at=timezone.now() - timezone.timedelta(hours=1))
    sibling = ScrapeJob.objects.create(
        url="https://www.papier.com/cards",   # same slug, different url
        status=ScrapeJob.STATUS_RUNNING)
    reg.register_abandoned("papier-com", sibling.id)

    class _FakeGraph:
        def get_state(self, config):
            return SimpleNamespace(values={"site_slug": "papier-com"})

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def build_graph(self):
            return _FakeGraph()

        @staticmethod
        def get_config(thread_id):
            return {"configurable": {"thread_id": thread_id}}

    monkeypatch.setattr(tasks, "LangGraphService", _FakeService)
    _fm(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    ws = _workspace(tmp_path)

    tasks._finalize_job(mine)

    assert ws.exists() or any((tmp_path / "workspace" / "_trash").iterdir()), (
        "finalize must not destroy a workspace a same-slug sibling still needs"
    )
    assert not list(ws.glob("*")), "at worst the dir is tombstoned away"


# ─── the other wipe sites + the namespace consumers ─────────────────────────


def test_other_wipe_sites_consult_the_guard():
    # ADAPT binding (sys.modules idiom): ``agents.nodes.X`` and
    # ``webapp.agents.X`` are distinct module objects in-container — bind the
    # one the celery worker imports.
    import agents.nodes.check_tracker  # noqa: F401
    import agents.nodes.setup_workspace  # noqa: F401

    ct = sys.modules["agents.nodes.check_tracker"]
    sw = sys.modules["agents.nodes.setup_workspace"]
    assert "delete_blocked_reason" in inspect.getsource(ct)
    assert "delete_blocked_reason" in inspect.getsource(sw)


@pytest.mark.django_db
def test_check_tracker_tombstones_instead_of_wiping_for_a_sibling(
        db, tmp_path, monkeypatch):
    """The check_tracker wipe site honours the same guard: blocked → tombstone
    (nothing lost), never a silent skip."""
    import agents.nodes.check_tracker  # noqa: F401
    from django.test.utils import override_settings

    # sys.modules idiom: agents.nodes.__init__ re-exports the check_tracker
    # FUNCTION over the submodule, so attribute binding gives a function, not
    # the module.
    ct = sys.modules["agents.nodes.check_tracker"]

    reg = _reg()
    reg.register_abandoned("papier-com", 4242)
    ws = _workspace(tmp_path)
    (tmp_path / "scrapers" / "papier-com" / "analysis").mkdir(parents=True)
    (tmp_path / "scrapers" / "papier-com" / "analysis" / "site_analysis.json").write_text("{}")
    with override_settings(PROJECT_ROOT=str(tmp_path)):
        ct._clean_workspace(str(tmp_path), "papier-com", exclude_job_id=807)
    assert not ws.exists(), "blocked wipe must tombstone, not delete in place"
    trash = tmp_path / "workspace" / "_trash"
    assert trash.exists() and any(trash.iterdir())
    kept = list((tmp_path / "scrapers" / "papier-com" / "analysis").glob("*.json"))
    assert kept, "the scrapers sweep is skipped while a sibling is alive"


@pytest.mark.django_db
def test_setup_workspace_tombstones_instead_of_wiping_for_a_sibling(
        db, tmp_path, monkeypatch):
    import agents.nodes.setup_workspace  # noqa: F401
    from django.test.utils import override_settings

    # sys.modules idiom (agents.nodes re-exports the function over the module).
    sw = sys.modules["agents.nodes.setup_workspace"]

    reg = _reg()
    reg.register_abandoned("papier-com", 4242)
    ws = _workspace(tmp_path)
    (ws / "stale_debris.txt").write_text("stale")
    with override_settings(PROJECT_ROOT=str(tmp_path)):
        sw.setup_workspace({
            "site_slug": "papier-com", "job_id": 807, "url": PAPIER,
            "input_mode": "url_list", "input_urls": ["https://www.papier.com/p/1"],
        })
    trash = tmp_path / "workspace" / "_trash"
    assert trash.exists() and any(trash.iterdir()), "blocked wipe tombstones"
    assert (tmp_path / "workspace" / "papier-com").is_dir(), (
        "this run still gets a fresh workspace (makedirs recreates it)"
    )
    assert not (tmp_path / "workspace" / "papier-com" / "stale_debris.txt").exists()


def test_health_agent_workspace_scan_ignores_trash(tmp_path):
    """``job_health_agent._workspaces`` must not report ``workspace/_trash`` as
    a site workspace (the `_`-namespace guard)."""
    spec = importlib.util.spec_from_file_location(
        "job_health_agent_under_test",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "job_health_agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "workspace" / "papier-com").mkdir(parents=True)
    (tmp_path / "workspace" / "_trash" / "papier-com-807-1").mkdir(parents=True)
    found = [os.path.basename(p) for p in module._workspaces(str(tmp_path))]
    assert "papier-com" in found
    assert "_trash" not in found


# ─── retention: the `_trash` sweep rides the existing windows ────────────────


@pytest.mark.django_db
def test_retention_sweeps_trash_on_the_existing_windows(db, tmp_path, monkeypatch):
    from django.conf import settings
    from scraper import retention
    from scraper.models import ScrapeJob

    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    completed = ScrapeJob.objects.create(
        url="https://a.example/x", status=ScrapeJob.STATUS_COMPLETED)
    running = ScrapeJob.objects.create(
        url="https://b.example/x", status=ScrapeJob.STATUS_RUNNING)
    trash = tmp_path / "workspace" / "_trash"
    trash.mkdir(parents=True)
    now = int(time.time())
    # trash_name stamps time.time_ns() — the sweep parses that back.
    entries = {
        "completed-100d": trash / f"a-example-{completed.id}-{(now - 100 * 86400) * 10 ** 9}",
        "completed-40d": trash / f"a-example-{completed.id}-{(now - 40 * 86400) * 10 ** 9}",
        "running-100d": trash / f"b-example-{running.id}-{(now - 100 * 86400) * 10 ** 9}",
        "orphan-30d": trash / f"c-example-999999-{(now - 30 * 86400) * 10 ** 9}",
        "fresh": trash / f"d-example-999998-{now * 10 ** 9}",
    }
    for entry in entries.values():
        entry.mkdir()
        (entry / "scraper_draft.py").write_text("# d")

    report = retention.purge_retention(days_failed=7, days_completed=90)

    assert report["trash_entries_purged"] == 2
    assert not entries["completed-100d"].exists(), "past the completed window"
    assert entries["completed-40d"].exists(), "inside the completed window"
    assert entries["running-100d"].exists(), "a resumable sibling is never swept"
    assert not entries["orphan-30d"].exists(), "past the failed window"
    assert entries["fresh"].exists()


@pytest.mark.django_db
def test_retention_leaves_unparseable_trash_names_alone(db, tmp_path, monkeypatch):
    """A directory whose name does not carry the tombstone stamp is never
    auto-deleted — the sweep may only remove what the tombstone wrote."""
    from django.conf import settings
    from scraper import retention

    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    trash = tmp_path / "workspace" / "_trash"
    mystery = trash / "something-else"
    mystery.mkdir(parents=True)
    (mystery / "keep.txt").write_text("not ours")

    report = retention.purge_retention(days_failed=0, days_completed=0)

    assert report["trash_entries_purged"] == 0
    assert mystery.exists()


# ─── helpers ─────────────────────────────────────────────────────────────────


def _fm(monkeypatch):
    """Stub the File Master client for the finalize-wiring test.

    Re-load the real module and patch IT: in the full-suite process
    tests/test_f8_f16_output_selection.py installs a shadow module at
    ``sys.modules["src.artifacts"]`` at collection time, so a literal
    ``monkeypatch.setattr("src.artifacts.write", ...)`` would land nowhere.
    """
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    monkeypatch.setattr(real, "list_keys", lambda prefix="": [])
    monkeypatch.setattr(real, "exists", lambda key: False)
    monkeypatch.setattr(real, "read_text", lambda key: "")
    monkeypatch.setattr(real, "read_json", lambda key: {})
    monkeypatch.setattr(real, "read", lambda key: b"")
    monkeypatch.setattr(real, "write", lambda key, data: len(data or b""))
    monkeypatch.setattr(real, "write_json", lambda key, data: len(json.dumps(data)))
    monkeypatch.setattr(real, "delete", lambda key: True)
