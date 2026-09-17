"""[wave-36 gate] Sentinel-twin LOCAL gate driver (plan §5.2).

Rides check_tracker's selective-rescrape arm: pre-create the Site with
``status="complete"`` plus a completed twin ScrapeJob (same url string,
identical target_fields/input_mode/search_criteria/scope) → the skip flags
SELF-DERIVE (skip_site/skip_product/skip_code all True) → no probe, no
navigation, no analyzers, no writer. The tester DOES run (routing requires a
fresh test_report), then execution + cleanup. Draft is the prod per-job
archive planted into the workspace (skip_code_generation preserves it).

``skip_approvals=True`` is REQUIRED or the job parks at field_confirmation.

Usage (inside the django container):
    python3 /app/scripts/run_wave36_baseline_gate.py drive 658 657   # seed+watch
    python3 /app/scripts/run_wave36_baseline_gate.py seed 658        # dispatch only
    python3 /app/scripts/run_wave36_baseline_gate.py watch 658       # poll+evidence
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

FIXTURE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "wave36_baseline",
)


def _setup_django() -> None:
    sys.path.insert(  # settings module lives in <root>/webapp/config
        0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"
        )
    )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def _load_fixture(job_id: int) -> tuple[dict, bytes]:
    d = os.path.join(FIXTURE_ROOT, str(job_id))
    with open(os.path.join(d, "fixture.json")) as f:
        fx = json.load(f)
    with open(os.path.join(d, "draft.py"), "rb") as f:
        draft = f.read()
    return fx, draft


def seed(job_id: int) -> int:
    """Create Site+sentinel+drive, plant the draft, dispatch with rescrape."""
    from django.conf import settings
    from django.utils import timezone
    from scraper.models import ScrapeJob, Site
    from scraper.tasks import dispatch_scrape_job

    fx, draft = _load_fixture(job_id)
    slug = fx["site_slug"]

    site, _ = Site.objects.get_or_create(
        slug=slug,
        defaults={
            "url": fx["url"].rstrip("/"),  # _find_site matches rstrip("/") exactly
            "name": fx.get("site_name") or slug,
            "site_type": "shopping",
        },
    )
    if (site.url or "") != fx["url"].rstrip("/"):
        site.url = fx["url"].rstrip("/")
        site.save(update_fields=["url"])
    if site.status != "complete":
        site.status = "complete"
        site.save(update_fields=["status"])

    common = dict(
        url=fx["url"],
        site_name=fx.get("site_name") or slug,
        page_type="product",
        input_mode=fx["input_mode"],
        search_criteria=fx.get("search_criteria") or "",
        target_fields=fx.get("target_fields") or [],
        scope=fx.get("scope") or "",
        scope_value=str(fx.get("scope_value") or ""),
        skip_approvals=True,
    )

    ScrapeJob.objects.filter(
        url=fx["url"], status=ScrapeJob.STATUS_COMPLETED,
        notes__contains="[wave-36 gate sentinel]",
    ).delete()  # re-seedable: keep exactly ONE sentinel per url

    sentinel = ScrapeJob.objects.create(
        status=ScrapeJob.STATUS_COMPLETED,
        completed_at=timezone.now(),
        field_mapping=None,  # raw prior → raw-vs-raw chip compare → skip_code
        notes="[wave-36 gate sentinel]",
        **common,
    )

    drive = ScrapeJob.objects.create(status="pending", notes="[wave-36 gate drive]", **common)

    ws = os.path.join(str(settings.PROJECT_ROOT), "workspace", slug)
    if os.path.isdir(ws):
        shutil.rmtree(ws)  # minimal file set: the draft ONLY (no stale test_report)
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "scraper_draft.py"), "wb") as f:
        f.write(draft)

    dispatch_scrape_job(drive.id, rescrape=True)
    print(f"[{job_id}] sentinel={sentinel.id} drive={drive.id} dispatched ({slug})")
    return drive.id


def _read_output(job) -> dict | None:
    if not job.output_file:
        return None
    import src.artifacts as artifacts

    try:
        return json.loads(artifacts.read_text(job.output_file))
    except Exception:
        path = job.output_file
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None


def evidence(job_id: int) -> bool:
    """Print the per-job PASS evidence lines; return the gate verdict."""
    from scraper.models import ScrapeJob

    fx, _ = _load_fixture(job_id)
    job = (
        ScrapeJob.objects.filter(notes__startswith="[wave-36 gate drive]", url=fx["url"])
        .order_by("-id")
        .first()
    )
    if job is None:
        print(f"[{job_id}] NO drive job found")
        return False

    notes = job.notes or ""
    fm_row = next(
        (ln for ln in notes.splitlines() if ln.startswith("[FIELD-MAP]")), ""
    )
    out = _read_output(job)
    records, meta, out_key = [], {}, None
    if isinstance(out, dict):
        out_key = next((k for k, v in out.items() if isinstance(v, list) and v), None)
        records = out.get(out_key) or []
        meta = out.get("metadata") or {}
    rec_keys = sorted(records[0].keys()) if records else []

    print(f"\n== [{job_id}] {fx['site_slug']} drive={job.id} ==")
    print(f"  status={job.status}  product_count={job.product_count}  out_key={out_key}")
    print(f"  metadata: discovered={meta.get('discovered_urls') or len(meta.get('discovered_urls') or [])} "
          f"soft_block_escalations={meta.get('soft_block_escalations')} "
          f"stop_reason={meta.get('stop_reason')}")
    print(f"  record keys ({len(rec_keys)}): {rec_keys}")
    if fx.get("target_fields"):
        print(f"  [FIELD-MAP] {'FOUND' if fm_row else 'MISSING'}: {fm_row[:400]}")
    prod_disc = fx.get("discovered_urls_count") or 0
    this_disc = None
    d = meta.get("discovered_urls")
    if isinstance(d, list):
        this_disc = len(d)
    elif isinstance(d, (int, float)):
        this_disc = int(d)
    eff_disc = this_disc or prod_disc
    expected = min(10, eff_disc) if eff_disc else 10
    failed = int(meta.get("failed_products") or 0)
    escalations = meta.get("soft_block_escalations")
    verdict = []
    if job.status != "completed" or (job.product_count or 0) == 0:
        verdict.append("FAIL(not-completed/0)")
    # PASS arm 1: count == min(10, discovery). Arm 2: full extraction of the
    # effective discovery set — count + draft-attributed failures covers the
    # window (the draft's own emission filter drops site-side empty pages;
    # the wave's wall class would show as soft_block_escalations instead).
    if eff_disc and (job.product_count or 0) < expected:
        if failed and (job.product_count or 0) + failed >= expected and not escalations:
            print(f"  NOTE: full extraction of effective set "
                  f"({job.product_count} extracted + {failed} site-side empty/failed; "
                  f"soft_block_escalations={escalations})")
        else:
            verdict.append(
                f"FAIL(count {job.product_count} < expected {expected}; failed={failed} "
                f"escalations={escalations})"
            )
    if fx.get("target_fields") and not fm_row:
        verdict.append("FAIL(no FIELD-MAP row)")
    if fx.get("target_fields") and records:
        resolved = [k for k in rec_keys if k not in (fx.get("target_fields") or [])]
        if not resolved:
            verdict.append("FAIL(no resolved names in records)")
    if escalations:
        print(f"  NOTE: soft_block_escalations={escalations} "
              f"(non-JSON walls may persist; JSON-body discards must be 0)")
    if meta.get("soft_block_escalations") not in (None, 0):
        print(f"  NOTE: soft_block_escalations={meta.get('soft_block_escalations')} "
              f"(non-JSON walls may persist; JSON-body discards must be 0)")
    print(f"  VERDICT: {'PASS' if not verdict else '; '.join(verdict)}")
    return not verdict


def watch(job_id: int, timeout_s: int = 3600) -> bool:
    """Poll until terminal, then print evidence."""
    from scraper.models import ScrapeJob

    fx, _ = _load_fixture(job_id)
    job = (
        ScrapeJob.objects.filter(notes__startswith="[wave-36 gate drive]", url=fx["url"])
        .order_by("-id")
        .first()
    )
    if job is None:
        print(f"[{job_id}] NO drive job")
        return False
    last = None
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        job.refresh_from_db()
        if job.status != last:
            print(f"[{job_id}] {fx['site_slug']}: {last} → {job.status} "
                  f"({time.strftime('%H:%M:%S')})", flush=True)
            last = job.status
        if job.status in ("completed", "failed", "cancelled"):
            return evidence(job_id)
        time.sleep(20)
    print(f"[{job_id}] TIMEOUT after {timeout_s}s (status={job.status})")
    return False


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("seed", "watch", "drive"):
        print(__doc__)
        return 2
    _setup_django()
    cmd, ids = sys.argv[1], [int(a) for a in sys.argv[2:]]
    if cmd == "seed":
        for jid in ids:
            seed(jid)
        return 0
    if cmd == "watch":
        return 0 if all(watch(jid) for jid in ids) else 1
    results = {}
    for jid in ids:
        seed(jid)
        results[jid] = watch(jid)
    print("\n== GATE SUMMARY ==")
    for jid, ok in results.items():
        print(f"  {jid}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
