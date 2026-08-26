#!/usr/bin/env python3
"""Autonomous job-health agent — invoked every 30 min by the session scheduler.

Wakes up, inspects recent + stuck jobs in the local stack, classifies issues,
and writes a prioritized findings file for the main agent to spin deep agents
on (the user's directive: check → deep-analyze → critique-fix → repeat until
clean). Deterministic inspection only — no LLM calls from here.

Output: /tmp/job_health/findings-<ts>.md (empty file when nothing found).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, "/app/webapp")
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

OUT_DIR = "/tmp/job_health"


def classify_jobs() -> list[dict]:
    from scraper.models import EventOutbox, ScrapeJob, Step

    now = dt.datetime.now(dt.timezone.utc)
    findings: list[dict] = []

    # 1. Stuck running jobs (>45 min without a step transition)
    running = ScrapeJob.objects.filter(status=ScrapeJob.STATUS_RUNNING)
    for j in running:
        latest = Step.objects.filter(job=j).order_by("-id").first()
        if latest and latest.started_at:
            age_min = (now - latest.started_at).total_seconds() / 60
            if age_min > 45:
                findings.append({
                    "sev": "high",
                    "kind": "stuck_running",
                    "job": j.id,
                    "detail": f"running, last step {latest.phase} started {int(age_min)}m ago",
                })
        elif j.started_at:
            age_min = (now - j.started_at).total_seconds() / 60
            if age_min > 60:
                findings.append({
                    "sev": "high", "kind": "stuck_running", "job": j.id,
                    "detail": f"running {int(age_min)}m with no step rows",
                })

    # 2. FAILED jobs in the last window — capture the error for triage
    recent_failed = ScrapeJob.objects.filter(
        status__in=[ScrapeJob.STATUS_FAILED, ScrapeJob.STATUS_CAPTCHA_BLOCKED,
                    ScrapeJob.STATUS_AKAMAI_BLOCKED],
        completed_at__gte=now - dt.timedelta(hours=1),
    )
    for j in recent_failed:
        findings.append({
            "sev": "medium", "kind": "failed", "job": j.id,
            "detail": (j.error_message or "")[:180],
        })

    # 3. Suspicious completions: 0 items, or empty-error completions with no output
    recent_done = ScrapeJob.objects.filter(
        status=ScrapeJob.STATUS_COMPLETED, completed_at__gte=now - dt.timedelta(hours=1),
    )
    for j in recent_done:
        if j.product_count == 0:
            findings.append({
                "sev": "medium", "kind": "empty_completion", "job": j.id,
                "detail": f"completed with 0 items, output={j.output_file or 'none'}",
            })

    # 4. Outbox health (events layer): stale pending rows with no delivery
    stale_outbox = EventOutbox.objects.filter(
        state=EventOutbox.STATE_PENDING,
        next_attempt_at__lt=now - dt.timedelta(minutes=10),
    ).count()
    if stale_outbox > 0:
        findings.append({
            "sev": "high", "kind": "outbox_stalled",
            "detail": f"{stale_outbox} pending event rows >10m past due (dispatcher stalled?)",
        })
    perm_fail = EventOutbox.objects.filter(
        state=EventOutbox.STATE_PERMANENTLY_FAILED,
        created_at__gte=now - dt.timedelta(hours=1),
    ).count()
    if perm_fail:
        findings.append({
            "sev": "low", "kind": "outbox_permfail",
            "detail": f"{perm_fail} permanently-failed deliveries in the last hour",
        })

    # 5. Corrupt artifacts anywhere in workspaces (the job-10 class)
    root = os.environ.get("PROJECT_ROOT", "/app")
    for slug_dir in _workspaces(root):
        for fn in os.listdir(slug_dir):
            if fn.endswith(".json") and not fn.endswith(".corrupt"):
                p = os.path.join(slug_dir, fn)
                try:
                    json.load(open(p))
                except Exception as exc:
                    findings.append({
                        "sev": "high", "kind": "corrupt_artifact",
                        "detail": f"{p}: {str(exc)[:80]}",
                    })
    return findings


def _workspaces(root: str):
    ws = os.path.join(root, "workspace")
    if not os.path.isdir(ws):
        return []
    return [os.path.join(ws, d) for d in os.listdir(ws)
            if os.path.isdir(os.path.join(ws, d))]


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        findings = classify_jobs()
    except Exception as exc:  # DB down etc — report, don't crash
        findings = [{"sev": "high", "kind": "agent_error", "detail": repr(exc)[:200]}]
    out = os.path.join(OUT_DIR, f"findings-{ts}.md")
    body = ""
    if findings:
        lines = [f"# Job health findings {ts}", ""]
        order = {"high": 0, "medium": 1, "low": 2}
        for f in sorted(findings, key=lambda x: order.get(x["sev"], 3)):
            job = f" (job {f['job']})" if "job" in f else ""
            lines.append(f"- [{f['sev'].upper()}] {f['kind']}{job}: {f['detail']}")
        body = "\n".join(lines) + "\n"
    with open(out, "w") as fh:
        fh.write(body)
    print(body if body else f"{ts}: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
