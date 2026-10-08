"""[wave-55 C2] url_list checkpoint cross-contamination.

Prod: job 1227 (made-in-china.com) submitted ONE PDP and shipped TEN
unrelated laptops; job 1229 (fr.sandro-paris.com) submitted one PDP and
shipped two unsubmitted strays. Both COMPLETED, both stamped
``metadata.discovery_coverage.skipped_reason = "checkpoint_loaded"``.

The chain, all inside ``webapp/agents/nodes/run_execution.py``:

  1. code_tester's ``--sample`` run executes Phase-1 discovery and the
     template banks ``discovered_urls_checkpoint.json``;
  2. run_execution stages that checkpoint VERBATIM into the /scrape
     ``extra_files`` tuple for every input mode, including url_list;
  3. ``--fresh-discovery`` is withheld for url_list (the api family turns it
     into "ignore the user's URLs, crawl the catalog" — job 570), so the
     template's ``_load_checkpoint()`` reads the staged file and skips
     Phase 1;
  4. both url_list junk gates are explicitly exempt, so nothing flags the
     strays.

Three fixes, one gate each:

  S1  the checkpoint is only ever staged where checkpoint reuse can be
      armed (Phase-1 modes); the url_list lane never sees one, and the
      in-process lane deletes a stale one before launching;
  S2  url_list rows go through the same real-item bar as every other mode;
  S3  post-run assertion: a url_list job's output URL set must be a subset
      of the submitted seed set, else the run FAILS.

Policy note (audit open question 13a): this is the strict reading the brief
specifies — ANY stray row fails. Whether a url_list row that is a real
sibling of the seed should ever be tolerated is still an owner decision.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

re_ = importlib.import_module("agents.nodes.run_execution")  # noqa: E402

CHECKPOINT = "discovered_urls_checkpoint.json"

SEED_PDP = "https://fr.sandro-paris.com/fr/p/baskets-en-cuir-suede-a-vague/SFACH01521_30.html"
STRAY_1 = "https://fr.sandro-paris.com/fr/p/sweatshirt-effet-noue-en-coton/SFPSW00809_27.html"
STRAY_2 = "https://fr.sandro-paris.com/fr/p/short-a-rayures-plisse-en-laine/SFPSH00661_44.html"
LISTING_PAGE = "https://books.example/personalized-books/kids/age/0-3"


def _src() -> str:
    return (ROOT / "webapp" / "agents" / "nodes" / "run_execution.py").read_text(
        encoding="utf-8"
    )


def _write(path: Path, name: str, content: str) -> str:
    p = path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def _write_output(tmp_path: Path, rows, key="products") -> str:
    return _write(tmp_path, "output_test.json", json.dumps({key: rows}))


def _row(url, title="Baskets en cuir", price=225.0):
    return {"title": title, "price": price, "currency": "EUR", "url": url}


# ═══════════════════════════════════ S1 ═══════════════════════════════════════
class TestCheckpointStaging:
    def _staged(self, tmp_path, input_mode):
        d = tmp_path / "ws"
        d.mkdir(exist_ok=True)
        _write(d, "input_urls.json", json.dumps({"urls": [SEED_PDP]}))
        _write(d, CHECKPOINT, json.dumps({"urls": [STRAY_1, STRAY_2]}))
        return re_._stage_extra_files(str(d / "scraper_draft.py"), input_mode)

    def test_url_list_never_stages_the_checkpoint(self, tmp_path):
        extra = self._staged(tmp_path, "url_list")
        assert CHECKPOINT not in extra, (
            "the tester's checkpoint rode into a url_list execution — the "
            "template's _load_checkpoint() then ships URLs the user never "
            "submitted (job 1227)"
        )

    def test_url_list_still_gets_the_seed_file(self, tmp_path):
        """input_urls.json IS the url_list seed — staging must keep it."""
        extra = self._staged(tmp_path, "url_list")
        assert "input_urls.json" in extra
        assert SEED_PDP in extra["input_urls.json"]

    def test_url_list_still_gets_discovery_config(self, tmp_path):
        d = tmp_path / "ws2"
        d.mkdir()
        _write(d, "discovery_config.json", json.dumps({"max_pages": 3}))
        extra = re_._stage_extra_files(str(d / "scraper_draft.py"), "url_list")
        assert "discovery_config.json" in extra

    @pytest.mark.parametrize("mode", ["navigation", "list_page", "search_term"])
    def test_phase1_modes_keep_the_checkpoint(self, tmp_path, mode):
        """The T14 checkpoint-rescue lane is Phase-1-only and still needs the
        file staged (prod 769)."""
        extra = self._staged(tmp_path, mode)
        assert CHECKPOINT in extra

    def test_missing_files_stage_nothing(self, tmp_path):
        d = tmp_path / "ws3"
        d.mkdir()
        assert re_._stage_extra_files(str(d / "scraper_draft.py"), "url_list") == {}

    def test_staging_keyed_on_the_same_mode_set_as_the_flag(self):
        """One frozenset owns both decisions — a second literal set is how the
        two drift apart (the wave-40 T15 rule)."""
        assert "_wants_fresh_discovery(input_mode)" in _src()
        assert "input_urls.json" in _src() and CHECKPOINT in _src()


class TestInProcessLaneCheckpoint:
    def test_url_list_run_clears_a_stale_checkpoint_before_launch(self):
        """The in-process subprocess shares this filesystem, so "don't stage"
        is not enough there: the tester's checkpoint sits in the workspace the
        scraper reads. It must be gone before the launch, and only for the
        modes that never had a Phase-1 leg."""
        src = _src()
        i_mode = src.index('input_mode = state.get("input_mode", "")')
        i_launch = src.index("result = _run_in_process(")
        window = src[i_mode:i_launch]
        assert "_delete_discovery_checkpoint(" in window, (
            "an url_list in-process run reads the tester's checkpoint straight "
            "off the workspace — it must be deleted before the launch"
        )
        i_del = window.index("_delete_discovery_checkpoint(")
        guard = window[max(0, i_del - 600) : i_del]
        assert "_wants_fresh_discovery" in guard, (
            "the pre-run delete must be gated to non-Phase-1 modes; deleting "
            "for navigation/list_page/search_term would break the T14 rescue "
            "(prod 769)"
        )


# ═══════════════════════════════════ S2 ═══════════════════════════════════════
class TestJunkGatesCoverUrlList:
    def test_nonitem_row_gate_fires_for_url_list(self, tmp_path):
        """Job 1227's cousin: a url_list run that ships shell URLs must not
        read as COMPLETED just because the seeds were user-provided."""
        out = _write_output(tmp_path, [_row("https://www.briscoes.co.nz/#main")])
        msg = re_._nonitem_row_gate(out, "url_list")
        assert msg, "url_list is exempt from the non-item row bar"
        assert "Non-item row bar" in msg

    def test_nonitem_row_gate_still_passes_real_url_list_seeds(self, tmp_path):
        out = _write_output(tmp_path, [_row(SEED_PDP)])
        assert re_._nonitem_row_gate(out, "url_list") == ""

    def test_nonitem_row_gate_minority_junk_still_passes_for_url_list(self, tmp_path):
        rows = [_row(SEED_PDP), _row(STRAY_1)]
        rows.append(_row("https://fr.sandro-paris.com/#main"))
        out = _write_output(tmp_path, rows)
        assert re_._nonitem_row_gate(out, "url_list") == ""

    def test_shallow_section_gate_applies_to_url_list(self, tmp_path):
        """A url_list job whose scraper walked the nav tree instead of the
        seed ships section links; the depth comparison against the submitted
        PDP is grounded (the seed IS the walked page's section)."""
        shallow = [
            "https://fr.sandro-paris.com/fr/new-in",
            "https://fr.sandro-paris.com/fr/women",
            "https://fr.sandro-paris.com/fr/accessories",
        ]
        out = _write_output(tmp_path, [_row(u, f"T{i}", 100 + i)
                                       for i, u in enumerate(shallow)])
        msg = re_._shallow_section_gate(out, SEED_PDP, "url_list")
        assert msg, "url_list is exempt from the nav-tree junk bar"
        assert "Nav-tree junk bar" in msg

    def test_shallow_section_gate_still_passes_deep_url_list_rows(self, tmp_path):
        """Sandro 1229's real shape: PDP-depth strays are NOT shallow section
        links — that class belongs to the S3 scope gate, not this one."""
        out = _write_output(tmp_path, [_row(SEED_PDP), _row(STRAY_1), _row(STRAY_2)])
        assert re_._shallow_section_gate(out, SEED_PDP, "url_list") == ""

    def test_url_list_verdict_uses_the_passed_seed_not_the_file(self, tmp_path):
        """[round 16] input_urls.json IS the laundering vector the scope-gate
        docstring names: templates/playwright_scraper.py's save_urls_to_file
        rewrites it with Phase-1's own discoveries, so grading a url_list run
        against that file grades it by the rows this bar exists to police.
        The verdict must come from the DISPATCHED seed list."""
        shallow = [
            "https://fr.sandro-paris.com/fr/new-in",
            "https://fr.sandro-paris.com/fr/women",
            "https://fr.sandro-paris.com/fr/accessories",
            "https://fr.sandro-paris.com/fr/gifts",
        ]
        out = _write_output(tmp_path, [_row(u, f"T{i}", 100 + i)
                                       for i, u in enumerate(shallow)])
        # what the template left in the workspace: PDP strays, not the seeds.
        _write(tmp_path, "input_urls.json",
               json.dumps({"urls": [STRAY_1, STRAY_2]}))
        msg = re_._shallow_section_gate(
            out, SEED_PDP, "url_list", seed_urls=[SEED_PDP],
        )
        assert msg, "the stray file diluted the nav-tree verdict below the bar"
        assert "Nav-tree junk bar" in msg

    def test_list_page_still_reads_the_walked_listing_file(self, tmp_path):
        """list_page's anchor legitimately IS the walked listing, so its seed
        merge keeps coming off input_urls.json (pinned wave-50 behaviour)."""
        shallow = [
            "https://books.example/personalized-books/new-in",
            "https://books.example/personalized-books/bestsellers",
            "https://books.example/personalized-books/gifts",
            "https://books.example/personalized-books/baby",
        ]
        out = _write_output(tmp_path, [_row(u, f"T{i}", 5 + i)
                                       for i, u in enumerate(shallow)])
        _write(tmp_path, "input_urls.json", json.dumps({"urls": [LISTING_PAGE]}))
        assert re_._shallow_section_gate(
            out, LISTING_PAGE, "list_page").startswith("Nav-tree junk bar")


# ═══════════════════════════════════ S3 ═══════════════════════════════════════
class TestUrlListScopeGate:
    def test_seed_plus_strays_fires(self, tmp_path):
        """The exact job 1229 shape: the seed IS present, two rows are not."""
        out = _write_output(tmp_path, [_row(STRAY_1), _row(STRAY_2), _row(SEED_PDP)])
        msg = re_._url_list_scope_gate(out, [SEED_PDP], "url_list")
        assert msg.startswith("url_list scope violation: 2/3 output rows are "
                              "not submitted URLs")
        assert STRAY_1 in msg and STRAY_2 in msg

    def test_seed_absent_fires(self, tmp_path):
        """The exact job 1227 shape: ten rows, none of them the seed."""
        rows = [_row(f"https://www.made-in-china.com/p/{i}/", f"Laptop {i}")
                for i in range(10)]
        out = _write_output(tmp_path, rows)
        msg = re_._url_list_scope_gate(out, [SEED_PDP], "url_list")
        assert msg.startswith("url_list scope violation: 10/10 output rows are "
                              "not submitted URLs")

    def test_offender_list_is_capped_at_three(self, tmp_path):
        rows = [_row(f"https://shop.example/p/{i}/", f"T{i}") for i in range(7)]
        out = _write_output(tmp_path, rows)
        msg = re_._url_list_scope_gate(out, [SEED_PDP], "url_list")
        assert msg.count("https://shop.example/p/") == 3

    def test_output_subset_of_seed_passes(self, tmp_path):
        out = _write_output(tmp_path, [_row(SEED_PDP)])
        assert re_._url_list_scope_gate(out, [SEED_PDP, STRAY_1], "url_list") == ""

    def test_multi_seed_run_passes_on_its_own_seeds(self, tmp_path):
        out = _write_output(tmp_path, [_row(SEED_PDP), _row(STRAY_1)])
        assert re_._url_list_scope_gate(
            out, [SEED_PDP, STRAY_1], "url_list") == ""

    def test_cosmetic_drift_is_not_a_violation(self, tmp_path):
        """Scheme, www. prefix, host case and a trailing slash are the same
        item — a gate that fired on them would fail honest runs."""
        out = _write_output(
            tmp_path,
            [
                _row("HTTPS://WWW.Fr.Sandro-Paris.com" + SEED_PDP.split(".com")[1]
                     + "/"),
                _row(STRAY_1 + "?utm_source=feed"),
            ],
        )
        assert re_._url_list_scope_gate(
            out, [SEED_PDP, STRAY_1], "url_list") == ""

    def test_no_seed_is_a_noop(self, tmp_path):
        out = _write_output(tmp_path, [_row(STRAY_1)])
        assert re_._url_list_scope_gate(out, [], "url_list") == ""
        assert re_._url_list_scope_gate(out, None, "url_list") == ""

    def test_unreadable_output_noops(self, tmp_path):
        assert re_._url_list_scope_gate(
            str(tmp_path / "gone.json"), [SEED_PDP], "url_list") == ""

    def test_other_output_keys_are_harvested(self, tmp_path):
        out = _write_output(tmp_path, [{"title": "Job", "company": "X",
                                        "url": STRAY_1}], key="jobs")
        msg = re_._url_list_scope_gate(out, [SEED_PDP], "url_list")
        assert msg.startswith("url_list scope violation: 1/1")

    def test_non_url_list_modes_are_a_noop(self, tmp_path):
        """[round 16] The scope contract is url_list's alone. Site.input_urls
        rides into graph state for EVERY mode (tasks.py copies it
        unconditionally), and navigation/list_page/search_term deliberately
        merge category-page rows (_run_category_sources) — judging those
        against the seed set hard-FAILs honest multi-source runs (a
        priceline-com-au job carries 30 PDP input_urls on the Site row)."""
        rows = [_row(f"https://fr.sandro-paris.com/fr/p/other-{i}/", f"T{i}")
                for i in range(5)]
        out = _write_output(tmp_path, rows)
        for mode in ("navigation", "list_page", "search_term"):
            assert re_._url_list_scope_gate(out, [SEED_PDP], mode) == "", (
                f"{mode} rows graded against the seed set"
            )
        # positive control: the same output under url_list still fires.
        assert re_._url_list_scope_gate(out, [SEED_PDP], "url_list").startswith(
            "url_list scope violation: 5/5"
        )

    def test_mode_matching_tolerates_case_and_padding(self, tmp_path):
        """The prescribed ``.strip().lower()`` spelling — a sloppily-cased
        mode still gets the STRICT reading (over-failing beats under-shipping
        junk); only an unambiguous nav mode no-ops."""
        out = _write_output(tmp_path, [_row(STRAY_1)])
        assert re_._url_list_scope_gate(out, [SEED_PDP], " URL_List ").startswith(
            "url_list scope violation"
        )


class TestScopeGateWiring:
    def test_wired_in_the_browser_service_lane(self):
        src = _src()
        assert src.count("_url_list_scope_gate(") >= 3, (
            "the assertion must run in BOTH lanes (an HTTP url_list scraper "
            "runs in-process, a Playwright one on browser_service)"
        )
        # After the non-item row bar — the last url_list net before SUCCESS.
        # rindex: the browser_service lane is the LATER of the two (the
        # in-process lane mirrors it since round 16's S2).
        i_ni = src.rindex("_ni = _nonitem_row_gate(")
        i_call = src.index("_us = _url_list_scope_gate(", i_ni)
        assert i_ni < i_call
        assert src.index("_notify_phase(_exec_job_id", i_call) > i_call

    def test_wired_in_the_in_process_lane(self):
        src = _src()
        i_q = src.index("_q = _extraction_quality_gate(")
        i_success = src.index('# H3: in-process path owns its own checkpoint')
        window = src[i_q:i_success]
        assert "_url_list_scope_gate(" in window, (
            "the in-process lane returns SUCCESS without the scope assertion"
        )

    def test_seed_comes_from_the_dispatched_input_urls(self):
        """NOT the on-disk input_urls.json — the tester rewrites that file
        during its own sample run, which is exactly the laundering vector."""
        src = _src()
        # browser_service lane: the seed rides the gate call itself.
        i_ni = src.rindex("_ni = _nonitem_row_gate(")
        i_call = src.index("_us = _url_list_scope_gate(", i_ni)
        i_end = src.index("\n\n", i_call)
        assert 'state.get("input_urls")' in src[i_call:i_end].replace(
            "(state or {})", "state"
        )
        # in-process lane: the seed is threaded in as seed_urls= at the
        # _run_in_process call sites (the lane has no state of its own).
        assert src.count("seed_urls=state.get(\"input_urls\")") >= 2, (
            "both _run_in_process call sites must thread the dispatched seed"
        )

    def test_both_call_sites_pass_input_mode(self):
        """[round 16] The gate is url_list-only, so a caller that drops the
        mode turns it into a silent no-op everywhere. Grep each call — through
        its closing paren — for ``input_mode`` so neither site can regress."""
        src = _src()

        def _call_text(marker: str, start: int = 0) -> str:
            i = src.index(marker, start)
            depth = 0
            for j in range(i, len(src)):
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        return src[i:j + 1]
            return src[i:]

        calls = [
            _call_text("_us = _url_list_scope_gate("),
            _call_text(
                "_us = _url_list_scope_gate(",
                src.index("_us = _url_list_scope_gate(") + 8,
            ),
        ]
        assert len(calls) == 2
        for call in calls:
            assert "input_mode" in call, (
                f"scope-gate call dropped the mode guard: {call}"
            )


# ═════════════════ S2 lane coverage ══════════════════════════════════════════
class TestInProcessLaneJunkGates:
    """[round 16] _run_in_process is the DEFAULT lane for the HTTP family
    (requests / api / http_navigation — _needs_browser False). Wiring the junk
    bars into the browser_service lane only left half the executions unpoliced.
    """

    def _window(self) -> str:
        src = _src()
        i_q = src.index("_q = _extraction_quality_gate(")
        i_h3 = src.index("# H3: in-process path owns its own checkpoint")
        return src[i_q:i_h3]

    def test_success_path_runs_both_junk_bars(self):
        window = self._window()
        assert "_shallow_section_gate(" in window, (
            "the in-process lane returns SUCCESS without the nav-tree junk bar"
        )
        assert "_nonitem_row_gate(" in window, (
            "the in-process lane returns SUCCESS without the non-item row bar"
        )

    def test_browser_lane_ordering_is_mirrored(self):
        """F9 -> shallow -> nonitem -> scope, exactly as the browser_service
        lane runs them — the two lanes must not disagree about which verdict
        names the failure."""
        window = self._window()
        assert window.index("_shallow_section_gate(") < window.index(
            "_nonitem_row_gate("
        ) < window.index("_url_list_scope_gate("), (
            "the in-process lane's gate ordering has diverged from the "
            "browser_service lane"
        )

    def test_each_bar_returns_the_same_failed_shape_as_f9(self):
        window = self._window()
        for marker in ("_sh = _shallow_section_gate(", "_ni = _nonitem_row_gate("):
            tail = window[window.index(marker):window.index(marker) + 1400]
            assert '"execution_status": "FAILED"' in tail, marker
            assert '"output_file": output_file' in tail, marker
            assert '"product_count": product_count' in tail, marker
            assert '"discovery_coverage": discovery_coverage' in tail, marker
            assert '"error_message"' in tail, marker

    def test_url_list_shell_rows_fail_through_the_in_process_path(self, tmp_path):
        """End-to-end through the real lane: an HTTP-family url_list run that
        ships /#main shell rows must come back FAILED, not COMPLETED-with-junk
        (F9 exempts url_list, so this bar is the only net)."""
        scraper = tmp_path / "scraper_draft.py"
        scraper.write_text(
            "import json, os, sys\n"
            "here = os.path.dirname(os.path.abspath(__file__))\n"
            "rows = [{'title': f'Shell {i}', 'price': 10.0 + i,\n"
            "         'url': 'https://www.briscoes.co.nz/#main'}\n"
            "        for i in range(3)]\n"
            "with open(os.path.join(here, 'output_test.json'), 'w') as fh:\n"
            "    json.dump({'products': rows}, fh)\n"
            "sys.stderr.write('wrote output_test.json\\n')\n",
            encoding="utf-8",
        )
        res = re_._run_in_process(
            str(scraper), [], str(tmp_path), str(tmp_path),
            workspace_folder=str(tmp_path), job_id=0,
            input_mode="url_list", seed_urls=[SEED_PDP],
        )
        assert res["execution_status"] == "FAILED", res
        assert "Non-item row bar" in res["error_message"]
        assert res["product_count"] == 3
        assert res["output_file"].endswith(".json")
        assert "discovery_coverage" in res


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
