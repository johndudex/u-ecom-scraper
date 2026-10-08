"""[wave-55 R12 H9] Server-side stamps the LLM must not author.

Two audit defects where a wall-clock/truth value was trusted to a model or a
racy counter:

- **H9a** — ``test_report.json``'s ``test_timestamp`` was written by the
  code_tester MODEL from its prompt's schema (``"test_timestamp": "ISO-8601"``),
  so the field carried whatever the model believed the time was. The truth is
  the file's own mtime — the moment the verdict was actually written — so
  ``_load_test_report`` stamps it server-side and the prompt stops asking for
  it.
- **H9b** — SessionLog rows were sequenced with ``SessionLog.objects.count()``
  in a batch writer, so any interleaved writer (or a deleted row) made two
  rows share a seq. The SSE/poll cursors read ``filter(seq__gt=since_seq)``,
  so the row landing AFTER the cursor advances is never delivered. Sequence
  numbers must be allocated per job from ``max(seq) + 1`` under the job-row
  lock, never from a count of surviving rows.

Run: docker compose exec -T django bash -lc "cd /app && pytest ../tests/test_wave55_server_side_stamps.py -q"
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from agents.graph import _load_test_report  # noqa: E402

# 2019-01-01 — what a model that "writes the time" tends to invent, and
# deliberately nothing like the real mtime the stamp must carry.
_LLMS_LIE = "2019-01-01T00:00:00Z"
_MTIME_EPOCH = 1_700_000_000 + 0.625  # sub-second, so a coarse stamp can't pass


def _write_report(tmp_path, slug: str, report: dict) -> str:
    folder = tmp_path / "workspace" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "test_report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    os.utime(path, (_MTIME_EPOCH, _MTIME_EPOCH))
    return str(path)


def _expected_stamp() -> str:
    return datetime.fromtimestamp(_MTIME_EPOCH, timezone.utc).isoformat()


class TestTestTimestampIsTheFilesMtime:
    """H9a: the stamp is a property of the report FILE, not the author."""

    def test_the_stamp_is_overwritten_with_the_file_mtime(
        self, tmp_path, monkeypatch
    ):
        slug = "mtime-stamp-example-com"
        _write_report(
            tmp_path, slug, {"site_slug": slug, "test_timestamp": _LLMS_LIE}
        )
        monkeypatch.chdir(tmp_path)

        report = _load_test_report(slug)

        assert report is not None
        assert report["test_timestamp"] == _expected_stamp()

    def test_a_report_without_a_stamp_gets_one(self, tmp_path, monkeypatch):
        """The field is now server-owned: its presence never depended on the
        model remembering to write it."""
        slug = "no-stamp-example-com"
        _write_report(tmp_path, slug, {"site_slug": slug, "passed": True})
        monkeypatch.chdir(tmp_path)

        assert _load_test_report(slug)["test_timestamp"] == _expected_stamp()

    def test_the_stamp_survives_the_stale_mtime_gate(
        self, tmp_path, monkeypatch
    ):
        """min_mtime ACCEPTS a fresh report — and the accepted report must
        still carry the mtime stamp (the two reads must not disagree)."""
        slug = "fresh-gate-example-com"
        _write_report(
            tmp_path, slug, {"site_slug": slug, "test_timestamp": _LLMS_LIE}
        )
        monkeypatch.chdir(tmp_path)

        report = _load_test_report(slug, min_mtime=_MTIME_EPOCH - 60)

        assert report is not None, "fresh report must pass the gate"
        assert report["test_timestamp"] == _expected_stamp()

    def test_a_stale_report_is_still_rejected(self, tmp_path, monkeypatch):
        """The [job-81] staleness gate is untouched by the new stamp."""
        slug = "stale-gate-example-com"
        _write_report(
            tmp_path, slug, {"site_slug": slug, "test_timestamp": _LLMS_LIE}
        )
        monkeypatch.chdir(tmp_path)

        assert _load_test_report(slug, min_mtime=_MTIME_EPOCH + 60) is None

    def test_no_report_is_still_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _load_test_report("ghost-example-com") is None

    def test_the_prompt_stops_asking_the_model_for_a_timestamp(self):
        """The schema block that made the model author the field is gone —
        otherwise the next prompt edit re-introduces the drift."""
        prompt_path = os.path.join(
            ROOT, ".opencode", "agents", "code-tester.md"
        )
        with open(prompt_path, encoding="utf-8") as fh:
            prompt = fh.read()
        assert '"test_timestamp"' not in prompt, (
            "code-tester.md still asks the model to write test_timestamp; "
            "the server now stamps it from the file mtime (H9a)"
        )


# ─── H9b: SessionLog seq is a per-job high-water mark, not a row count ──────
#
# The allocator read ``SessionLog.objects.filter(job_id=...).count()`` — a
# count of SURVIVING rows, not the highest number handed out. Two writers
# that read it together (the heartbeat Timer thread vs the agent loop's batch
# flush in _persist_agent_logs) both wrote the same seq, and the job log
# pages on ``seq__gt=since_seq`` (views.py:1434/:1455/:1526), so the row that
# lands behind the cursor is never delivered.

import threading  # noqa: E402

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from agents.graph import _log_event_row, _persist_agent_logs  # noqa: E402
from scraper.models import ScrapeJob, SessionLog  # noqa: E402


@pytest.mark.django_db
class TestSessionSeqIsAHighWaterMark:
    def _job(self, host: str) -> ScrapeJob:
        return ScrapeJob.objects.create(url=f"https://{host}/p/1")

    def test_every_writer_in_the_lane_allocates_from_the_helper(self):
        """The count() allocator was inline at eight sites — a refactor that
        converted one leaves the others colliding exactly as before, so the
        pin counts the call sites instead of trusting the diff."""
        graph_path = os.path.join(ROOT, "webapp", "agents", "graph.py")
        tasks_path = os.path.join(ROOT, "webapp", "scraper", "tasks.py")
        with open(graph_path, encoding="utf-8") as fh:
            graph_src = fh.read()
        with open(tasks_path, encoding="utf-8") as fh:
            tasks_src = fh.read()
        # The helper's own degraded read (no DB / a stand-in manager) is the
        # only count() allowed to survive in the lane — every writer's own
        # allocation must come from the allocator.
        start = graph_src.index("def _session_seq_alloc(")
        end = graph_src.index("\ndef ", start)
        helper_src, lane_src = graph_src[start:end], graph_src[:start] + graph_src[end:]
        assert "SessionLog.objects.filter(job_id=self.job_id).count()" not in lane_src
        assert "SessionLog.objects.filter(job_id=job_id).count()" not in lane_src
        assert helper_src.count(
            "SessionLog.objects.filter(job_id=job_id).count()"
        ) == 1
        # the helper docstring's usage example is the 9th match: 8 sites + 1
        assert graph_src.count("with _session_seq_alloc(") == 9, (
            "a graph.py SessionLog writer stopped allocating under the "
            "job-row lock (wave-55 H9b)"
        )
        # and every SessionLog INSERT in the lane fills its seq from the
        # allocator, not from a number computed outside the lock
        lines = lane_src.splitlines()
        naked = [
            (i + 1, lines[i].strip())
            for i in range(len(lines))
            if "SessionLog.objects.create(" in lines[i]
            and not any("seq=alloc()" in l for l in lines[i:i + 12])
        ]
        assert naked == [], (
            f"a graph.py SessionLog writer bypasses _session_seq_alloc: {naked}"
        )
        assert "SessionLog.objects.filter(job_id=job.id).count()" not in tasks_src
        assert tasks_src.count("with _session_seq_alloc(") == 1

    def test_every_writer_in_the_lane_allocates_from_the_helper_fleet_wide(self):
        """The count() allocator was not only inline in graph.py — seven more
        SessionLog writers outside it read the same surviving-row count
        CONCURRENTLY with the allocator's own users: the zombie-refusal log
        (subagents), the two field_confirmation rows, the navigation summary,
        both probe ladders' step logs and the tester's run_scraper heartbeat
        (the exact shape of the audit's job-1200 duplicate seq). Walk the
        whole webapp lane and fail on any count() seq source outside the
        helper's own degraded read."""
        pattern = re.compile(
            r"SessionLog\.objects\.filter\(job_id=[^)]*\)\s*\.count\(\)"
        )
        graph_path = os.path.join(ROOT, "webapp", "agents", "graph.py")
        with open(graph_path, encoding="utf-8") as fh:
            graph_src = fh.read()
        # The allowed span is the allocator's own documentation block plus
        # its body — the comment above the def quotes the old spelling, and
        # the span ends at the next module-level def AFTER the helper.
        helper_start = graph_src.index("[wave-55 H9b] SessionLog seq allocation")
        helper_end = graph_src.index(
            "\ndef ", graph_src.index("def _session_seq_alloc(")
        )

        offenders: list[str] = []
        for base in ("agents", "scraper"):
            root = os.path.join(ROOT, "webapp", base)
            for dirpath, _dirs, files in os.walk(root):
                for name in sorted(files):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8") as fh:
                        src = fh.read()
                    for match in pattern.finditer(src):
                        if path == graph_path and (
                            helper_start <= match.start() < helper_end
                        ):
                            continue  # the helper's own degraded fallback
                        line = src.count("\n", 0, match.start()) + 1
                        offenders.append(f"{os.path.relpath(path, ROOT)}:{line}")
        assert offenders == [], (
            "a SessionLog writer still derives seq from a count of "
            f"surviving rows outside _session_seq_alloc: {offenders}"
        )

    def test_a_deleted_row_does_not_rewind_the_sequence(self):
        """count() re-derives its number from the rows that SURVIVE, so a
        single deletion rewinds every later allocation onto a live seq."""
        job = self._job("seq-gap-example-com")
        for i in range(3):
            _log_event_row(job.id, "probe", f"row {i}")
        SessionLog.objects.filter(job_id=job.id, seq=1).delete()

        _log_event_row(job.id, "probe", "written after the delete")

        seqs = [r.seq for r in SessionLog.objects.filter(job_id=job.id)]
        assert len(seqs) == len(set(seqs)), f"duplicate seq: {sorted(seqs)}"
        newest = SessionLog.objects.filter(
            job_id=job.id, content="written after the delete"
        ).get()
        assert newest.seq > max(s for s in seqs if s != newest.seq)

    def test_the_batch_flush_writes_above_the_existing_high_water_mark(self):
        """_persist_agent_logs allocates from count(), so a job whose rows
        already carry a higher seq (the tool-call logger runs concurrently)
        gets its messages filed BEFORE older rows — and a cursor past the
        count never sees them at all."""
        job = self._job("batch-flush-example-com")
        SessionLog.objects.create(
            job_id=job.id,
            role=SessionLog.ROLE_SYSTEM,
            agent="heartbeat",
            content="Agent code_writer still running...",
            seq=7,
        )

        _persist_agent_logs(
            {"job_id": job.id},
            {
                "messages": [
                    HumanMessage(content="find the catalogue"),
                    AIMessage(content="searching the listing page"),
                ]
            },
            "code_writer",
            {},
        )

        rows = list(SessionLog.objects.filter(job_id=job.id).order_by("seq"))
        assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
        assert rows[0].seq == 7, "the pre-existing row keeps its place"
        assert rows[1].seq > 7 and rows[2].seq > rows[1].seq, (
            f"batch rows filed below the high-water mark: "
            f"{[r.seq for r in rows]}"
        )


@pytest.mark.django_db(transaction=True)
class TestSessionSeqUnderRacingWriters:
    """Real threads, real commits — the shape the heartbeat Timer thread and
    the agent loop's batch flush actually race in."""

    def test_racing_writers_never_share_a_seq(self):
        job = ScrapeJob.objects.create(url="https://seq-race-example-com/p/1")
        n_threads, n_writes = 3, 8
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def _worker(tid: int) -> None:
            from django.db import connection

            try:
                barrier.wait(timeout=30)
                for i in range(n_writes):
                    _log_event_row(job.id, "site_analyzer", f"t{tid}-w{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert not errors, errors[:3]
        seqs = [r.seq for r in SessionLog.objects.filter(job_id=job.id)]
        assert len(seqs) == n_threads * n_writes, (
            f"{n_threads * n_writes - len(seqs)} writes were swallowed"
        )
        assert len(set(seqs)) == len(seqs), (
            f"two writers shared a seq: {sorted(seqs)}"
        )

