"""True-concurrency dispatch test (fold M3/M15, build-critic mandate).

The lease-CAS unit tests prove single-process semantics; this file proves
the DB-level guarantee under REAL parallelism: two threads, real
transactions (django_db(transaction=True) — TRUNCATE-style, no wrapper),
both sweeping simultaneously — every row delivered EXACTLY once.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import TransactionTestCase  # noqa: E402
from django.utils import timezone  # noqa: E402

from scraper import models  # noqa: E402
from scraper.events.dispatcher import claim_due_rows, mark_delivered  # noqa: E402


class DispatchConcurrencyTests(TransactionTestCase):
    """TransactionTestCase: NO wrapping transaction — setUp data really
    commits, threads on their own connections see it, and SKIP LOCKED
    genuinely contends. (Plain TestCase wraps every connection; threads
    that close_all() get fresh connections to a database without the
    fixtures — they see nothing and claim nothing.)"""

    def setUp(self):
        self.user = User.objects.create_user("_t_conc", password="x")
        self.job = models.ScrapeJob.objects.create(
            url="https://e.com/conc", user=self.user, created_via="api",
            status="running", input_mode="url_list", page_type="product",
        )
        models.JobCallback.objects.create(
            job=self.job, url="https://hooks.partner.example/cb", secret="s" * 40
        )
        from scraper.events import new_event_id

        for i in range(20):
            models.EventOutbox.objects.create(
                event_id=new_event_id(), job=self.job, user=self.user,
                event_type="job.phase.updated", dedupe_key="",
                payload={"i": i}, state=models.EventOutbox.STATE_PENDING,
                next_attempt_at=timezone.now() - timedelta(seconds=1),
            )

    def test_two_concurrent_sweeps_claim_disjoint_sets(self):
        """Two threads claim simultaneously: union = all due rows,
        intersection = empty (SKIP LOCKED + lease CAS under contention)."""
        results = {0: [], 1: []}
        errors = []

        def sweep(worker_id):
            from django.db import connections

            try:
                connections.close_all()
                results[worker_id] = list(claim_due_rows(limit=50))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=sweep, args=(i,)) for i in (0, 1)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        ids0 = {r.pk for r in results[0]}
        ids1 = {r.pk for r in results[1]}
        # THE invariants: no row claimed twice, no row left unclaimed by both.
        # (One sweeper finishing before the other starts is legitimate — its
        # claim leases everything and the other correctly finds nothing.)
        assert not (ids0 & ids1), f"double-claimed: {ids0 & ids1}"
        claimed_total = len(ids0) + len(ids1)
        n_rows = models.EventOutbox.objects.filter(job=self.job).count()
        assert claimed_total == n_rows, (
            f"claimed {claimed_total} of {n_rows} — rows missed by BOTH sweepers"
        )
        # and the DB agrees: every row is leased exactly once
        from scraper.models import EventOutbox

        leased = EventOutbox.objects.filter(
            job=self.job, state=EventOutbox.STATE_LEASED
        ).count()
        assert leased == n_rows

    def test_delivered_rows_not_reclaimed_by_second_sweep(self):
        rows = claim_due_rows(limit=50)
        assert len(rows) == 20
        for r in rows:
            mark_delivered(r)
        second = claim_due_rows(limit=50)
        assert second == []

    def test_parallel_deliver_no_double_count(self):
        """Deliveries racing mark_delivered: delivered_count increments per
        row, never twice (lease checked, states transition once)."""
        rows = claim_due_rows(limit=50)
        errors = []

        def deliver_all(batch):
            from django.db import connections

            try:
                connections.close_all()
                for r in batch:
                    fresh = models.EventOutbox.objects.filter(
                        pk=r.pk, state=models.EventOutbox.STATE_LEASED
                    ).first()
                    if fresh is not None:
                        mark_delivered(fresh)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connections.close_all()  # never leak thread connections

        t1 = threading.Thread(target=deliver_all, args=(rows[:10],))
        t2 = threading.Thread(target=deliver_all, args=(rows[10:],))
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)
        assert not errors, errors
        self.job.callback.refresh_from_db()
        assert self.job.callback.delivered_count == 20


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
