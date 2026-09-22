"""[wave-40 T8] Real-items evidence must be JOB-scoped and must scan
workspace, local scrapers/, AND the FM. Prod 770 (19 products in scrapers/,
FAILED n=0) and 762 (8 workspace outputs pre-dating the crashed attempt's
draft floor) both escaped the attempt-scoped workspace-only guard."""

import json

from django.conf import settings
from django.utils import timezone

from scraper import tasks
from scraper.models import ScrapeJob

OUT_NAME = "output_2026-09-20_235455_000001_99.json"  # %H%M%S_%f_%pid


def _job(started_at, **kw):
    # unsaved instance — the helper reads only attributes.
    # NOTE: ScrapeJob has NO site_slug field; the slug is a separate arg.
    return ScrapeJob(url="https://x.example/", started_at=started_at, **kw)


def _root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))


def _rows(n):
    return [{"title": f"p{i}", "price": "$1", "url": f"https://x/p/{i}"}
            for i in range(n)]


def _fm(monkeypatch, keys, payloads):
    """Stub the File Master client to `(keys, payloads)`.

    ADAPT (implement-time, invocation only — the brief's stub semantics are
    unchanged): the literal string-path `monkeypatch.setattr(
    "src.artifacts.list_keys", ...)` raises AttributeError in the FULL-suite
    process, because tests/test_f8_f16_output_selection.py installs a
    2-attribute stub at COLLECTION time (`sys.modules.setdefault(
    "src.artifacts", <ModuleType with .latest_output_key/.read>)`) that
    shadows the real module; test_api_gaps.py:157 documents the same trap.
    So: re-load the real module, pin it in sys.modules AND on the package
    (the two routes `import src.artifacts as artifacts` can resolve through —
    the same pair the skills_store/writer_memory helpers juggle), and patch
    the two functions on that object. monkeypatch restores both after each
    test, so nothing leaks into sibling files.
    """
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "src.artifacts",
        Path(__file__).resolve().parents[1] / "src" / "artifacts.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setitem(sys.modules, "src.artifacts", real)
    import src as _src_pkg

    monkeypatch.setattr(_src_pkg, "artifacts", real, raising=False)
    monkeypatch.setattr(real, "list_keys", lambda prefix="": keys)
    monkeypatch.setattr(real, "read_text", lambda key: payloads.get(key, ""))


def test_770_shape_fm_output_rescued_workspace_empty(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=2)
    key = f"scrapers/sephora-cz/{OUT_NAME}"
    _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(19)})})
    (tmp_path / "workspace" / "sephora-cz").mkdir(parents=True)  # no outputs
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "sephora-cz", _job(started),
        final_state={"last_tested_draft_fp": "abc"})
    assert n == 19 and loc.endswith(OUT_NAME)


def test_762_shape_workspace_outputs_from_earlier_attempt_admitted(
        tmp_path, monkeypatch):
    import os
    started = timezone.now() - timezone.timedelta(hours=3)
    ws = tmp_path / "workspace" / "marimekko"
    ws.mkdir(parents=True)
    for i in range(4):   # four files, ALL older than any draft, newer than start
        f = ws / f"output_2026-09-20_0{i}0000_000001_1.json"
        f.write_text(json.dumps({"products": _rows(1)}))
    (ws / "scraper_draft.py").write_text("# current attempt draft")
    old = (timezone.now() - timezone.timedelta(hours=2)).timestamp()
    for f in ws.glob("output_*.json"):
        os.utime(f, (old, old))
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "marimekko", _job(started),
        final_state={"last_tested_draft_fp": "fp"})
    assert n == 4 and "marimekko" in loc


def test_prior_job_output_never_rescues(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "sally"
    ws.mkdir(parents=True)
    f = ws / "output_2026-09-20_010000_000001_1.json"   # hours BEFORE started
    f.write_text(json.dumps({"products": _rows(20)}))
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "sally", _job(started), final_state={"tested_draft_sha256": "a"})
    assert n == 0


def test_discovery_stub_file_is_skipped(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "stub"
    ws.mkdir(parents=True)
    (ws / "output_2026-09-20_020000_000001_1.json").write_text(json.dumps(
        {"products": _rows(5), "metadata": {"phase": "discovery"}}))
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "stub", _job(started), final_state={"tested_draft_sha256": "a"})
    assert n == 0


def test_no_tested_draft_provenance_blocks(monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    key = "scrapers/z/output_2026-09-20_020000_000001_1.json"
    _fm(monkeypatch, [key], {key: json.dumps({"products": _rows(9)})})
    n, _ = tasks._real_items_evidence("z", _job(started), final_state=None)
    assert n == 0   # trace guard: no draft provenance -> no rescue


def test_corrupt_json_degrades_to_zero(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "bad"
    ws.mkdir(parents=True)
    (ws / "output_2026-09-20_030000_000001_1.json").write_text("{not json")
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "bad", _job(started), final_state={"tested_draft_sha256": "a"})
    assert n == 0 and loc == ""


def test_kill_switch_disables(monkeypatch, tmp_path):
    monkeypatch.setenv("REAL_ITEMS_RESCUE_ENABLED", "0")
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "k", _job(timezone.now()), final_state={"tested_draft_sha256": "a"})
    assert n == 0


def test_min_count_is_1_for_url_list_and_3_otherwise():
    assert tasks._rescue_min_count("url_list") == 1
    assert tasks._rescue_min_count("list_page") == 1
    assert tasks._rescue_min_count("navigation") == 3


def test_output_name_epoch_parses_both_formats():
    assert tasks._output_name_epoch(
        "output_2026-09-20_235455_000001_99.json") is not None
    assert tasks._output_name_epoch(
        "output_2026-09-20_235455_99.json") is not None
    assert tasks._output_name_epoch("input_urls.json") is None


# ── [wave-40 T8] additive implementer coverage ──────────────────────────────
# The brief's workspace fixtures above are named before the job window, so the
# window gate rejects them BEFORE the discovery tag / parse / dead-row
# predicates run. These pin those predicates on files the window admits.

def _window_name(minutes_ago, pid=1):
    """An output name the window admits: stamped now-`minutes_ago`, pid-suffixed."""
    base = (timezone.now() - timezone.timedelta(minutes=minutes_ago)).strftime(
        "output_%Y-%m-%d_%H%M%S_%f_")
    return f"{base}{pid}.json"


def test_discovery_stub_skipped_even_when_draft_vouches(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "stub2"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft")
    (ws / _window_name(5)).write_text(json.dumps(
        {"products": _rows(5), "metadata": {"phase": "discovery"}}))
    _fm(monkeypatch, [], {})
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "stub2", _job(started), final_state={"last_tested_draft_fp": "fp"})
    assert n == 0 and loc == ""


def test_corrupt_file_degrades_but_its_sibling_survives(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "halfbad"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft")
    (ws / _window_name(4, pid=1)).write_text("{not json")
    good = _window_name(4, pid=2)
    (ws / good).write_text(json.dumps({"products": _rows(3)}))
    _fm(monkeypatch, [], {})
    _root(monkeypatch, tmp_path)
    n, loc = tasks._real_items_evidence(
        "halfbad", _job(started), final_state={"last_tested_draft_fp": "fp"})
    assert n == 3 and loc.endswith(good)


def test_dead_rows_do_not_count(tmp_path, monkeypatch):
    started = timezone.now() - timezone.timedelta(hours=1)
    ws = tmp_path / "workspace" / "dead"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("# draft")
    (ws / _window_name(4)).write_text(json.dumps({"products": [
        {"title": "alive", "price": "$1", "url": "https://x/p/1"},
        {"title": "gone", "price": "$1", "url": "https://x/p/2",
         "status_code": 404},
        {"title": "soft", "price": "$1", "url": "https://x/p/3",
         "remarks": "product not found"},
    ]}))
    _fm(monkeypatch, [], {})
    _root(monkeypatch, tmp_path)
    n, _ = tasks._real_items_evidence(
        "dead", _job(started), final_state={"last_tested_draft_fp": "fp"})
    assert n == 1


def test_good_rows_key_list_bare_array_and_schema_fields():
    jobs = [{"title": "j", "company": "c"}]
    assert tasks._good_rows({"jobs": jobs}, [], "products") == jobs
    assert tasks._good_rows({"threads": jobs}, [], "products") == jobs
    assert tasks._good_rows(jobs, [], "products") == jobs          # bare array
    assert tasks._good_rows({"products": [{"title": "t"}]}, ["price"],
                            "products") == []                      # needs price
    assert tasks._good_rows({"metadata": {"phase": "discovery"},
                             "products": jobs}, [], "products") == []
    assert tasks._good_rows("nope", [], "products") == []
    assert tasks._good_rows(None, [], "products") == []


def test_fm_draft_key_alone_satisfies_the_trace_guard(monkeypatch):
    """Trace-guard arm 3: this job's per-job FM draft key, with NO state
    provenance (T9's promotion test leans on exactly this arm)."""
    started = timezone.now() - timezone.timedelta(hours=1)
    out_key = "scrapers/p/output_2026-09-20_040000_000001_1.json"
    draft_key = "scrapers/p/jobs/scraper-7.py"
    _fm(monkeypatch, [out_key, draft_key],
        {out_key: json.dumps({"products": _rows(2)})})
    n, loc = tasks._real_items_evidence(
        "p", _job(started, id=7), final_state=None)
    assert n == 2 and loc == out_key


def test_output_name_epoch_is_second_granular_utc():
    from datetime import datetime as dt
    from datetime import timezone as dttz

    stamped = tasks._output_name_epoch(
        "output_2026-09-20_235455_000001_99.json")
    plain = tasks._output_name_epoch("output_2026-09-20_235455_99.json")
    assert stamped == plain                       # %f is a stamp, not precision
    assert dt.fromtimestamp(stamped, tz=dttz.utc).strftime(
        "%Y-%m-%d %H:%M:%S") == "2026-09-20 23:54:55"
    assert tasks._output_name_epoch("output_2026-13-01_000000_1.json") is None
    assert tasks._output_name_epoch("output_2026-09-20_235455.json") is None
    assert tasks._output_name_epoch("") is None
    assert tasks._output_name_epoch(None) is None
