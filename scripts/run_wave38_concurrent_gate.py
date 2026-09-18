"""[wave-38 gate] Concurrent two-site traversal gate (the 411/412 shape).

Two REAL full-pipeline drives (force_full: workspace + analysis archive wiped
→ probe, analyzer, browser_traverse, writer, tester, execution all run) on two
DISTINCT registrable domains, dispatched so their traversals OVERLAP. The
traversal lock forces a sequential A→B handoff — the exact shape that let 412
read westelm state on renttherunway's first walk read.

Wave-36's sentinel-twin driver is the WRONG shape here: its self-derived skip
flags route AROUND the traversal. This driver forces the walk to run.

PASS (plan T7 Step 4):
  1. both drives COMPLETED with product_count > 0;
  2. (grepped outside: celery log has ZERO wrong-site abort / CROSS-DOMAIN /
     dropping-off-domain markers);
  3. neither drive's output JSON mentions the OTHER site's registrable;
  4. (conditional) any `scrape rejected (busy` 429 shows as W8 park-and-retry,
     never a failed run;
  5. (grepped outside: `[TRAVERSAL-LOCK] acquired/released` pairs for BOTH
     jobs; ZERO `page never settled` INFO lines).

Usage (inside the django container):
    python3 /app/scripts/run_wave38_concurrent_gate.py drive 658 498 --delay 300
    python3 /app/scripts/run_wave38_concurrent_gate.py seed 658 498 --delay 300
    python3 /app/scripts/run_wave38_concurrent_gate.py watch 658 498
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

FIXTURE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "wave36_baseline",
)
WAVE38_NOTE = "[wave-38 concurrent gate]"


def _setup_django() -> None:
    sys.path.insert(  # settings module lives in <root>/webapp/config
        0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"
        )
    )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def _load_fixture(job_id: int) -> dict:
    with open(os.path.join(FIXTURE_ROOT, str(job_id), "fixture.json")) as f:
        return json.load(f)


def seed(job_id: int) -> int:
    """Create ONE full-pipeline drive from a wave-36 fixture and dispatch it."""
    from scraper.models import ScrapeJob
    from scraper.tasks import dispatch_scrape_job

    fx = _load_fixture(job_id)
    common = dict(
        url=fx["url"],
        site_name=fx.get("site_name") or fx["site_slug"],
        page_type="product",
        input_mode=fx["input_mode"],
        search_criteria=fx.get("search_criteria") or "",
        target_fields=fx.get("target_fields") or [],
        scope=fx.get("scope") or "",
        scope_value=str(fx.get("scope_value") or ""),
        skip_approvals=True,
    )
    drive = ScrapeJob.objects.create(
        status="pending", notes=f"{WAVE38_NOTE} fixture={job_id}", **common
    )
    # rescrape + force_full: wipe workspace AND the analysis archive so no
    # prior artifact (draft, navigation_analysis.json) can re-hydrate —
    # every phase regenerates, the walk DEFINITELY runs.
    dispatch_scrape_job(drive.id, rescrape=True, force_full=True)
    print(f"[{job_id}] drive={drive.id} dispatched force_full ({fx['site_slug']})",
          flush=True)
    return drive.id


def _drive_job(job_id: int):
    from scraper.models import ScrapeJob

    # contains, NOT endswith: the graph APPENDS phase rows (e.g. [FIELD-MAP])
    # to notes as the drive progresses, so the fixture tag drifts off the end.
    return (
        ScrapeJob.objects.filter(notes__startswith=WAVE38_NOTE,
                                 notes__contains=f"fixture={job_id}")
        .order_by("-id")
        .first()
    )


def watch(job_id: int, timeout_s: int = 5400) -> tuple[bool, object]:

    job = _drive_job(job_id)
    if job is None:
        print(f"[{job_id}] NO drive job", flush=True)
        return False, None
    fx = _load_fixture(job_id)
    last = None
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        job.refresh_from_db()
        if job.status != last:
            print(f"[{job_id}] {fx['site_slug']}: {last} → {job.status} "
                  f"({time.strftime('%H:%M:%S')})", flush=True)
            last = job.status
        if job.status in ("completed", "failed", "cancelled"):
            return True, job
        time.sleep(20)
    print(f"[{job_id}] TIMEOUT after {timeout_s}s (status={job.status})")
    return False, job


def _output_registrables(job) -> tuple[list[str], str]:
    """The output JSON's raw text (for cross-domain substring scans)."""
    import src.artifacts as artifacts

    if not job or not job.output_file:
        return [], ""
    try:
        raw = artifacts.read_text(job.output_file)
    except Exception:
        try:
            with open(job.output_file) as f:
                raw = f.read()
        except Exception:
            return [], ""
    return [raw], raw


def evidence(job_id: int, other_registrable: str) -> bool:
    fx = _load_fixture(job_id)
    job = _drive_job(job_id)
    if job is None:
        print(f"[{job_id}] NO drive job")
        return False
    print(f"\n== [{job_id}] {fx['site_slug']} drive={job.id} ==")
    print(f"  status={job.status}  product_count={job.product_count}")
    verdict = []
    if job.status != "completed" or (job.product_count or 0) == 0:
        verdict.append(f"FAIL(not-completed/0: status={job.status} "
                       f"count={job.product_count})")
    # PASS criterion 3: no trace of the OTHER sentinel's domain in the output.
    _, raw = _output_registrables(job)
    if raw and other_registrable and other_registrable in raw:
        verdict.append(f"FAIL(output contains other site's registrable "
                       f"{other_registrable})")
    print(f"  VERDICT: {'PASS' if not verdict else '; '.join(verdict)}")
    return not verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("seed", "watch", "drive"))
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--delay", type=int, default=300,
                    help="seconds between dispatches (overlap control)")
    ap.add_argument("--timeout", type=int, default=5400)
    args = ap.parse_args()
    _setup_django()

    if args.cmd == "seed":
        for i, jid in enumerate(args.ids):
            if i:
                time.sleep(args.delay)
            seed(jid)
        return 0
    if args.cmd == "watch":
        oks = []
        for jid in args.ids:
            ok, _ = watch(jid, args.timeout)
            oks.append(evidence(jid, ""))
        return 0 if all(oks) else 1

    # drive: seed with overlap, watch BOTH concurrently, then cross-evidence
    threads = []

    def _seed_later(idx: int, jid: int) -> None:
        if idx:
            time.sleep(args.delay)
        seed(jid)

    for i, jid in enumerate(args.ids):
        t = threading.Thread(target=_seed_later, args=(i, jid), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    print("both drives dispatched — watching", flush=True)

    results: dict[int, tuple[bool, object]] = {}

    def _watch(jid: int) -> None:
        results[jid] = watch(jid, args.timeout)

    watchers = [threading.Thread(target=_watch, args=(j,), daemon=True)
                for j in args.ids]
    for w in watchers:
        w.start()
    for w in watchers:
        w.join()

    # cross-domain evidence: each job's output scanned against the OTHER's
    # registrable (the slug's site domain, from the fixture URL host).
    from urllib.parse import urlparse

    regs = {}
    for jid in args.ids:
        fx = _load_fixture(jid)
        host = urlparse(fx["url"]).netloc.removeprefix("www.")
        regs[jid] = host
    oks = []
    for jid in args.ids:
        other = next((regs[o] for o in args.ids if o != jid), "")
        oks.append(evidence(jid, other))

    print("\n== GATE SUMMARY ==")
    for jid in args.ids:
        ok = oks[args.ids.index(jid)]
        print(f"  {jid}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
