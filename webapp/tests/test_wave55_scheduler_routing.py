"""[wave-55 L1] ``schedule_next_site`` rides the events pool like its peers.

Audit (2026-10-05, L1): every beat entry but one was routed off the 3-slot
scrape pool — ``schedule_next_site`` was the holdout, so its no-op ticks
(idle/skipped on the vast majority of ticks) queued behind real scrape jobs on
the ``celery`` queue. The task touches only the DB and the File Master (an
HTTP service, ``src.artifacts``), so nothing about it needs the worker
container.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_scheduler_routing.py -q"
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

TASK_NAME = "scraper.tasks.schedule_next_site"
ENTRY_NAME = "schedule-next-site"
# celery-events consumes ``-Q events`` (docker-compose.yml); the one deliberate
# exception is the H13 tombstone sweep, which needs the worker's workspace.
DELIBERATE_WORKER_ROUTE = {"scraper.retention.sweep_workspace_trash"}


def _compose_service(src: str, name: str) -> str:
    """The compose block for one top-level service (header to the next)."""
    chunks = re.split(r"^  (?=[\w-]+:)", src, flags=re.MULTILINE)
    for chunk in chunks:
        if chunk.startswith(f"{name}:"):
            return chunk
    return ""


def _compose_env(block: str, key: str) -> str:
    """One environment value out of a compose service block ('' if unset)."""
    found = re.search(rf"^\s+{key}: (.+)$", block, re.MULTILINE)
    return found.group(1).strip() if found else ""


class TestScheduleNextSiteRouting:
    def test_beat_schedules_the_tick(self):
        entry = settings.CELERY_BEAT_SCHEDULE.get(ENTRY_NAME)
        assert entry is not None, f"{ENTRY_NAME} vanished from the beat schedule"
        assert entry["task"] == TASK_NAME

    def test_the_tick_is_routed_off_the_scrape_pool(self):
        assert settings.CELERY_TASK_ROUTES.get(TASK_NAME) == "events", (
            f"{TASK_NAME} is unrouted — its no-op ticks queue behind real "
            "scrape jobs on the 3-slot pool"
        )

    def test_no_beat_entry_is_left_unrouted(self):
        """The fleet-wide invariant L1 closed: an entry with no route follows
        the default queue, which only celery-worker consumes. The single
        deliberate exception is pinned by name in DELIBERATE_WORKER_ROUTE."""
        unrouted = sorted(
            f"{name} -> {entry['task']}"
            for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
            if not settings.CELERY_TASK_ROUTES.get(entry["task"])
        )
        assert unrouted == [], (
            f"beat entries without a CELERY_TASK_ROUTES entry: {unrouted}"
        )

    def test_every_route_names_a_consumed_queue(self):
        """A route to a queue nobody consumes is a silent black hole (the
        wave-40 T11 shape H13 fixed). Every route must be ``events`` or the
        worker's default ``celery``, and celery-events must actually consume
        ``events`` in compose."""
        routes = set(settings.CELERY_TASK_ROUTES.values())
        assert routes <= {"events", "celery"}, (
            f"route names a queue with no consumer: {routes}"
        )
        compose = Path(ROOT) / "docker-compose.yml"
        if compose.exists():
            events = _compose_service(compose.read_text(encoding="utf-8"), "celery-events")
            assert events and "-Q events" in events
            # The routed tick writes input_urls.json through src.artifacts, whose
            # _base() raises on an unset FILE_MASTER_URL — and the site query's
            # own .exclude(input_urls=[]) means that write precedes every queued
            # tick, so a container without the URL errors exactly when it has
            # work to queue. [round-30 GAP 5] The VALUE is the pin: an empty
            # value dies in the same _base() raise and a wrong host:port dies
            # on the first httpx call, so key-present was never a pin. The
            # value must also match the worker's own — the two containers
            # resolving different File Masters would split the artifact tree.
            events_url = _compose_env(events, "FILE_MASTER_URL")
            worker_url = _compose_env(
                _compose_service(compose.read_text(encoding="utf-8"), "celery-worker"),
                "FILE_MASTER_URL",
            )
            assert events_url == "http://file-master:8002", (
                "a task routed to events touches the File Master (src.artifacts) — "
                f"the container needs the worker's URL, got {events_url!r}"
            )
            assert events_url == worker_url, (
                "the events and worker containers must resolve the same File "
                f"Master: events={events_url!r} worker={worker_url!r}"
            )

    def test_only_the_deliberate_sweep_rides_the_worker(self):
        worker_routed = sorted(
            task for task, queue in settings.CELERY_TASK_ROUTES.items()
            if queue == "celery"
        )
        assert worker_routed == sorted(DELIBERATE_WORKER_ROUTE), (
            "an unexpected task is pinned to the scrape pool's default queue: "
            f"{worker_routed}"
        )


# [wave-55 L2 / round-30 GAP 4] The scrape pool is 3 slots (the audit settled
# the 2-vs-3 dispute with the prod boot banner: worker `concurrency: 3
# (prefork)`). The drift produced a real concurrency dispute in the audit
# threads, so every remaining '2-slot' mention in webapp/ + src/ is
# enumerated here. [wave-55 R12] L2 is CLOSED: the three HEAD-era settings.py
# rationales below were the census L2 PENDING row and now read 3-slot (pinned
# by test_the_settings_rationales_name_the_real_pool_size), so the allow-list
# is empty — any new mention re-reddens the walker above.
# (This pin's own file is skipped: its docstrings spell the strings it
# forbids.)
KNOWN_PENDING_2_SLOT: tuple = ()


class TestScrapePoolSlotCommentDrift:
    def test_every_remaining_2_slot_mention_is_a_known_pending_row(self):
        stale = []
        for base in (Path(ROOT) / "src", Path(ROOT) / "webapp"):
            for path in sorted(base.rglob("*.py")):
                if path == Path(__file__):
                    continue
                rel = path.relative_to(ROOT).as_posix()
                for line in path.read_text(encoding="utf-8").splitlines():
                    if re.search(r"2-slot|2 slots", line):
                        stale.append((rel, line.strip()))

        unmatched = [
            (rel, line)
            for rel, line in stale
            if not any(
                rel == want_rel and want in line
                for want_rel, want in KNOWN_PENDING_2_SLOT
            )
        ]
        assert unmatched == [], (
            "a '2-slot' mention outside the census L2 PENDING rows — the "
            f"scrape pool is 3 slots: {unmatched}"
        )
        # The allow-list is a checkable list, not a memory: a row that no
        # longer appears means L2 closed and the list must shrink with it.
        for want_rel, want in KNOWN_PENDING_2_SLOT:
            assert any(
                rel == want_rel and want in line for rel, line in stale
            ), (
                f"census L2 PENDING row resolved ({want_rel}: {want!r}) — "
                "shrink the allow-list and move the census row to FIXED"
            )

    def test_the_settings_rationales_name_the_real_pool_size(self):
        """[wave-55 R12 L2] The three HEAD-era settings.py rationales (the
        whole census L2 PENDING backlog) now read 3-slot — the audit settled
        the count with the prod boot banner (worker ``concurrency: 3
        (prefork)``). A '2-slot' anywhere in the settings file is the drift
        that started the audit-thread dispute."""
        settings_src = (
            Path(ROOT) / "webapp" / "config" / "settings.py"
        ).read_text(encoding="utf-8")
        assert not re.search(r"2-slot|2 slots", settings_src), (
            "settings.py still describes the scrape pool as 2 slots — it is 3"
        )
        assert settings_src.count("3-slot") >= 3, (
            "the wave-16 B3 / Fold B5 / wave-15 1.2 rationales must each name "
            "the pool they are sizing against"
        )
