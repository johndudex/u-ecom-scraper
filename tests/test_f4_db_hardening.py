"""F4/M5: DB-death hardening for the finalize path.

Prod 284/332/333: postgres was OOM-killed; the celery task hit
OperationalError('the connection is closed') inside _finalize_job, the
except path re-saved on the same dead connection and re-raised → job
stranded RUNNING for days (beat dead too). Worse, a get_state failure fell
through with final_state={} → the status ladder landed on COMPLETED
(silent success on an unreadable checkpoint).

Pure-python static verification.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tasks_src() -> str:
    return open(os.path.join(ROOT, "webapp/scraper/tasks.py")).read()


class TestGetStateRetry:
    def test_retry_with_close_old_connections(self):
        src = _tasks_src()
        i_first = src.index("snapshot = graph.get_state(config)")
        i_close = src.index("close_old_connections()", i_first)
        i_retry = src.index("snapshot = graph.get_state(config)", i_close)
        assert i_first < i_close < i_retry

    def test_second_failure_marks_failed_and_returns(self):
        src = _tasks_src()
        i_fn = src.index("def _finalize_job")
        i_first = src.index("snapshot = graph.get_state(config)", i_fn)
        i_retry = src.index("snapshot = graph.get_state(config)", i_first + 10)
        tail = src[i_retry:i_retry + 2500]
        assert "STATUS_FAILED" in tail
        assert "finalizer could not read graph state" in tail
        assert "\n            return" in tail  # must NOT fall through to the COMPLETED ladder


class TestExceptPathHardening:
    def test_close_old_connections_before_save(self):
        src = _tasks_src()
        i_except = src.index("except Exception as exc:\n        logger.exception(\"Scrape job %d failed")
        block = src[i_except:i_except + 3000]
        i_close = block.index("close_old_connections()")
        i_save = block.index('job.save(update_fields=["status", "error_message", "completed_at"])')
        assert i_close < i_save

    def test_save_failure_has_fresh_instance_retry(self):
        src = _tasks_src()
        assert "job = ScrapeJob.objects.get(pk=job_id)  # fresh instance+connection" in src
        assert "could not persist FAILED status (job stranded)" in src

    def test_publish_and_site_updates_guarded(self):
        src = _tasks_src()
        i_except = src.index("except Exception as exc:\n        logger.exception(\"Scrape job %d failed")
        block = src[i_except:i_except + 4200]
        assert "try:\n            _publish_job_status" in block


class TestConnectionSettings:
    def test_base_dict_has_max_age_and_health_checks(self):
        src = open(os.path.join(ROOT, "webapp/config/settings.py")).read()
        assert '"CONN_MAX_AGE": 60,' in src
        assert '"CONN_HEALTH_CHECKS": True,' in src
        # the PgBouncer override block must still run AFTER the base dict
        i_base = src.index('"CONN_MAX_AGE": 60,')
        i_pgb = src.index('if config("DB_USE_PGBOUNCER"')
        assert i_base < i_pgb
