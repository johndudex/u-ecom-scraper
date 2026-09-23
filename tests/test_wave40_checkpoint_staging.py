"""[wave-40 T15] reuse is armed on the run_execution side: env for the
subprocess, checkpoint in the staging tuple — and the probe path stays
checkpoint-free.

Ruling deviations from the brief's verbatim draft (each flagged inline with
``# RULING Rn:``):
- R1: ``--fresh-discovery`` stays appended UNCONDITIONALLY — it is the api
  family's execution trigger, and armed runs take the fresh-then-rescue lane
  (Phase 1 runs fresh; on a rescuable zero the T14 branch reads the staged
  checkpoint through the validated loader). The test pins that the append
  site EXISTS and carries the lane-decision comment.
- R2: ``_checkpoint_reuse_env`` returns an EXPLICIT ``"0"`` when not armed
  (defeats cross-container env skew), so the three ``== {}`` assertions
  became ``== {"SCRAPER_CHECKPOINT_REUSE": "0"}``.
- R4: ``_probe_phase1_discovery_once`` lives in ``webapp/agents/graph.py``,
  not run_execution — the source pin follows it there (file read, no import:
  graph.py drags the whole django/celery graph into a source-only assertion).
"""

import importlib
import inspect
import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _re_mod():
    # module-shadowing idiom — see Global Constraints
    return importlib.import_module("webapp.agents.nodes.run_execution")


def _fn_src(src, name):
    m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, f"{name} not found"
    return m.group(0)


def _graph_src():
    with open(os.path.join(ROOT, "webapp", "agents", "graph.py"),
              encoding="utf-8") as fh:
        return fh.read()


def _ckpt(tmp_path):
    p = tmp_path / "discovered_urls_checkpoint.json"
    p.write_text('{"urls": ["https://x/p/1"], "count": 1, "ts": 0}')
    return p


def test_arm_sets_env_for_phase1_mode_with_checkpoint(tmp_path, monkeypatch):
    re_mod = _re_mod()
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    _ckpt(tmp_path)
    env = re_mod._checkpoint_reuse_env({"input_mode": "navigation"},
                                       str(tmp_path))
    assert env == {"SCRAPER_CHECKPOINT_REUSE": "1"}


# RULING R2: not-armed returns the explicit OFF value, never {} — an explicit
# "0" defeats cross-container env skew (browser_service's ambient env may
# carry the var while celery's does not).
def test_no_checkpoint_no_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation"}, str(tmp_path)
    ) == {"SCRAPER_CHECKPOINT_REUSE": "0"}


def test_url_list_mode_never_arms(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    _ckpt(tmp_path)
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "url_list"}, str(tmp_path)
    ) == {"SCRAPER_CHECKPOINT_REUSE": "0"}


def test_kill_switch_blocks_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "0")
    _ckpt(tmp_path)
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation"}, str(tmp_path)
    ) == {"SCRAPER_CHECKPOINT_REUSE": "0"}


def test_unset_env_means_not_armed(tmp_path, monkeypatch):
    # RULING R7(e): default-OFF at the arm — an UNSET env is reuse disabled,
    # and the arm says so explicitly rather than inheriting ambiguity.
    monkeypatch.delenv("SCRAPER_CHECKPOINT_REUSE", raising=False)
    _ckpt(tmp_path)
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation"}, str(tmp_path)
    ) == {"SCRAPER_CHECKPOINT_REUSE": "0"}


def test_force_full_run_never_arms(tmp_path, monkeypatch):
    # RULING R3: the discover-only/force-full exemption mirrors the flag
    # run_execution's orbit already owns (state.force_full — the user-declared
    # full re-run that wipes the workspace and must regenerate every phase).
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    _ckpt(tmp_path)
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation", "force_full": True}, str(tmp_path)
    ) == {"SCRAPER_CHECKPOINT_REUSE": "0"}


def test_blank_workspace_folder_fails_closed(monkeypatch):
    # No folder → no knowable checkpoint location → never arm.
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "1")
    assert _re_mod()._checkpoint_reuse_env(
        {"input_mode": "navigation"}, ""
    ) == {"SCRAPER_CHECKPOINT_REUSE": "0"}


def test_staging_tuple_includes_the_checkpoint():
    src = inspect.getsource(_re_mod())
    assert "discovered_urls_checkpoint.json" in src


def test_probe_phase1_path_stays_checkpoint_free():
    # RULING R4 (adapted location): the probe lives in webapp/agents/graph.py.
    src = _graph_src()
    probe = _fn_src(src, "_probe_phase1_discovery_once")
    assert "checkpoint" not in probe.lower()


def test_probe_lane_staging_stays_checkpoint_free():
    # RULING R4 (staging side): the probe's own extra_files staging loop must
    # never stage a checkpoint — that is what kept 763 nastygal honest.
    probe = _fn_src(_graph_src(), "_probe_phase1_discovery_once")
    assert "discovered_urls_checkpoint" not in probe


def test_execution_lane_merges_the_arm_into_both_env_lanes():
    # Source pins for R5/R6 (behavioural proof is T16's dead-seed gate): every
    # /scrape dispatch and the in-process env receive _checkpoint_reuse_env,
    # and the execution staging tuple stages the checkpoint VERBATIM (no seed
    # filtering — the T13 reader filters).
    re_mod = _re_mod()
    src = inspect.getsource(re_mod)
    bs = _fn_src(src, "_run_via_browser_service")
    assert "_checkpoint_reuse_env" in bs
    # The multi-source category lane POSTs its own /scrape payload — it must
    # carry the explicit value too, never inherit the other container's ambient
    # env (review minor 2).
    assert "_checkpoint_reuse_env" in _fn_src(src, "_run_category_sources")
    staging = re.search(r'for _sf in \(([^)]*)\)', bs)
    assert staging, "execution staging tuple moved — re-locate"
    # Either the literal or the T13 constant (same value, imported at the top
    # of run_execution) — the constant is the preferred spelling.
    assert ("discovered_urls_checkpoint.json" in staging.group(1)
            or "CHECKPOINT_FILENAME" in staging.group(1))
    # The in-process legs must receive the armed value too: BOTH _run_in_process
    # call sites (primary + the RC1 listing redispatch) merge it into the
    # env_overrides dict that _run_in_process folds into its Popen env.
    ip_sites = re.findall(r"env_overrides=\{\*\*_stealth_env\(state\), \*\*_ckpt_reuse_env\}", src)
    assert len(ip_sites) == 2, (
        f"expected both _run_in_process call sites to merge the arm, found {len(ip_sites)}"
    )
    # def + the run_execution-body arm call (:1029) + the browser-service lane.
    # The two in-process call sites are pinned separately just above, and the
    # _run_category_sources multi-source lane adds a fourth.
    assert src.count("_checkpoint_reuse_env(") >= 3


# RULING R1: the append site must EXIST and carry the lane-decision comment —
# it stays UNCONDITIONAL (it is the api family's execution trigger); nobody
# should "fix" it into the B-core resume lane later.
def test_fresh_discovery_append_is_unconditional_with_lane_note():
    src = inspect.getsource(_re_mod())
    m = re.search(r'"--fresh-discovery"', src)
    assert m, "append site moved — re-locate"
    region = src[max(0, m.start() - 1500): m.end() + 200]
    assert "checkpoint" in region.lower(), (
        "append site must document the fresh-then-validated-rescue lane"
    )
    # The B-core lane (checkpoint_urls = [] if args.fresh_discovery else
    # _load_checkpoint()) must stay OUT of run_execution.
    assert "_load_checkpoint" not in src


def test_docs_contract_documents_checkpoint_reused():
    with open(os.path.join(ROOT, "docs", "discovery-coverage-gate-contract.md"),
              encoding="utf-8") as fh:
        doc = fh.read()
    assert "checkpoint_reused" in doc                       # (a) stop reason
    assert "fresh_stop_reason" in doc                       # (a) honesty
    assert "listing_yield_failure" in doc                   # (a) unaffected
    assert "_probe_phase1_discovery_once" in doc            # (b) probe exclusion
    assert "discovered_urls_checkpoint.json" in doc         # (c) both lanes
    assert '"skipped"' in doc                               # (c) B-core lane
    assert '"0"' in doc or "explicit" in doc                # (d) disabled overload
    assert "below_floor" in doc                             # (d) floor vs yield gates
    assert "default" in doc.lower() and "off" in doc.lower()  # (e) default-OFF
