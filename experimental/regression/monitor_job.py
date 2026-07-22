"""Per-job regression monitor (SCRATCH — not committed).

Run inside the django container:
    docker compose exec -T django sh -c \
      'cd /app/webapp && PYTHONPATH=/app/webapp python /app/experimental/regression/monitor_job.py <job_id>'

Does three things the goals doc requires:
  1. Enforces the 15-min per-agent timeout. Measures CONTINUOUS time in one phase:
     the clock resets on every phase transition, so a retry cycle
     (code_gen → testing → code_gen) times each leg independently. This is
     necessary because the graph reuses a single Step row across retries without
     resetting its started_at — naive (now - started_at) would false-fire.
  2. Auto-approves WAITING_APPROVAL interrupts immediately (debounced) so HITL
     gates (field_confirmation, validate_analysis, etc.) don't stall the run.
  3. Prints a step-duration summary on exit so we can see where time went.

NOT an iterating harness — caller decides which job to submit and reads the result.
"""
import os
import sys
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402
from scraper.models import ScrapeJob, Step  # noqa: E402
from scraper.tasks import resume_scrape_task  # noqa: E402

PER_PHASE_TIMEOUT = 900  # 15 min continuous in one phase — the goals-doc rule
CODE_GEN_TIMEOUT = 1200  # 20 min for code_generation — glm-5-turbo genuinely needs
                         # ~14-16 min for complex sites (write + syntax retry). The
                         # file exists at ~14 min; 900s kills it 1-2 min too early.
POLL = 20  # seconds between checks
APPROVE_DEBOUNCE = 60  # don't re-fire approve within this window

TERMINAL = {"completed", "failed", "cancelled", "captcha_blocked", "akamai_blocked"}
BLOCKED = {"captcha_blocked", "akamai_blocked"}


def fmt(now):
    return now.strftime("%H:%M:%S")


def current_phase(job_id: int):
    """Newest running Step for the job (highest id), or None.

    Picks newest so a stale left-over 'running' row from an earlier retry leg
    can't shadow the genuinely-current one.
    """
    return (
        Step.objects.filter(job_id=job_id, status=Step.STATUS_RUNNING)
        .order_by("-id")
        .first()
    )


def main(job_id: int) -> int:
    last_approve = 0.0
    prev_phase = None
    phase_since = None  # wall-clock (epoch) when current phase became running
    while True:
        job = ScrapeJob.objects.get(id=job_id)
        now = timezone.now()
        now_epoch = time.time()

        # 2. Auto-approve waiting interrupts (debounced) — checked first so a
        # paused job never trips the phase timer.
        if job.status == ScrapeJob.STATUS_WAITING_APPROVAL:
            prev_phase, phase_since = None, None  # reset; resumption is a fresh leg
            since = time.time() - last_approve
            if since > APPROVE_DEBOUNCE:
                print(
                    f"{fmt(now)} ⏳ WAITING_APPROVAL — auto-approving (job #{job_id})",
                    flush=True,
                )
                resume_scrape_task.delay(
                    job_id, {"decision": "approve", "label": "auto", "feedback": ""}
                )
                last_approve = time.time()
            time.sleep(POLL)
            continue

        # 3. Terminal?
        if job.status in TERMINAL:
            print(
                f"\n{'🟢' if job.status == 'completed' else '❌'} DONE: status={job.status}",
                flush=True,
            )
            if job.status in BLOCKED:
                print(f"   blocked reason: {job.error_message}", flush=True)
            return 0 if job.status == "completed" else 1

        cur = current_phase(job_id)

        # Track continuous time in the current phase (reset on transition).
        phase = cur.phase if cur else None
        if phase != prev_phase:
            phase_since = now_epoch  # new leg (or no-phase → phase) starts the clock
            prev_phase = phase
        continuous = (now_epoch - phase_since) if phase_since else 0.0

        # 1. Per-phase timeout (continuous). code_generation gets extra room.
        _limit = CODE_GEN_TIMEOUT if phase in ("code_generation", "code_generation_retry") else PER_PHASE_TIMEOUT
        if phase and continuous > _limit:
            print(
                f"\n❌ TIMEOUT: phase '{phase}' running {continuous:.0f}s continuous "
                f"> {PER_PHASE_TIMEOUT}s limit",
                flush=True,
            )
            # Revoke+terminate the celery task so it doesn't keep running as a
            # zombie (a DB-only cancel leaves the task clogging the worker).
            job.refresh_from_db()
            _tid = job.celery_task_id
            if _tid:
                try:
                    from celery import current_app

                    current_app.control.revoke(_tid, terminate=True)
                    print(f"   revoked celery task {_tid[:12]}", flush=True)
                except Exception as exc:
                    print(f"   WARN: revoke failed: {exc}", flush=True)
            job.status = ScrapeJob.STATUS_FAILED
            job.error_message = (
                f"regression monitor: phase '{phase}' exceeded 15min continuous "
                f"({continuous:.0f}s)"
            )
            if not job.completed_at:
                job.completed_at = now
            job.save(update_fields=["status", "error_message", "completed_at"])
            return 2

        # Progress line
        done = Step.objects.filter(job_id=job_id, status=Step.STATUS_DONE).count()
        if phase:
            print(
                f"{fmt(now)} ▶ {phase:26s} {continuous:5.0f}s  done={done}  status={job.status}",
                flush=True,
            )
        else:
            print(f"{fmt(now)} … status={job.status} done={done} (between phases)", flush=True)

        time.sleep(POLL)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: monitor_job.py <job_id>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(int(sys.argv[1])))
