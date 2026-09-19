"""[wave-39 gate] PDP-seed listing swap — swap shape + demote control.

Drive A (swap): a job shaped EXACTLY like the prod intake/status Retry rows
that collapsed to 1 item (prod 719 mimco: PDP seed, list_page, same-host
listing in search_criteria, firstn/10). Under wave-39 the seed must swap to
the listing and discovery must yield >1 item.

Drive B (control): the same shape minus search_criteria → the wave-34 demote
must still fire (the prod-419 shape: count=1, [INTAKE-PDP]).

Sites: mimco (shopify — JSON-LD Product on PDPs confirmed by prod 719's flip;
listing collections/jewellery; drivable locally since wave-34 e2e 375) and
briscoes PDP (Phase 2 5/5 in wave-38; its LISTING is Magento-PWA-dead so it
deliberately only plays the no-criteria demote control here).

PASS:
  A: COMPLETED, product_count >= 2, notes carry [INTAKE-PDP-SWAP], notes do
     NOT carry the demote marker "[INTAKE-PDP]" (exact token), and the output
     JSON never mentions the other drive's registrable domain.
  B: COMPLETED, product_count == 1, notes carry "[INTAKE-PDP]" (demote).
  (grepped outside: zero wrong-site abort / CROSS-DOMAIN markers in celery log)

Usage (inside the django container):
    python3 /app/scripts/run_wave39_swap_gate.py drive
    python3 /app/scripts/run_wave39_swap_gate.py watch
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

WAVE39_NOTE = "[wave-39 swap gate]"

SHAPES = {
    "swap": {
        "label": "mimco PDP + listing → swap",
        "url": "https://mimco.com.au/products/missing-link-necklace-60323927-908",
        "site_name": "Mimco",
        "site_slug": "mimco-com-au",
        "input_mode": "list_page",
        "search_criteria": "https://mimco.com.au/collections/jewellery",
        "registrable": "mimco.com.au",
        "target_fields": ["title", "price", "currency", "availability", "url"],
        "scope": "firstn",
        "scope_value": "10",
    },
    "demote": {
        "label": "briscoes PDP, no criteria → demote",
        "url": "https://www.briscoes.co.nz/product/1134726/kates-kitchen-butter-dome/",
        "site_name": "Briscoes NZ",
        "site_slug": "briscoes-co-nz",
        "input_mode": "list_page",
        "search_criteria": "",
        "registrable": "briscoes.co.nz",
        "target_fields": ["title", "price", "currency", "description", "availability"],
        "scope": "firstn",
        "scope_value": "10",
    },
}


def _setup_django() -> None:
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp"
        ),
    )
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def seed(shape: str) -> int:
    from scraper.models import ScrapeJob
    from scraper.tasks import dispatch_scrape_job

    s = SHAPES[shape]
    drive = ScrapeJob.objects.create(
        url=s["url"],
        site_name=s["site_name"],
        page_type="product",
        input_mode=s["input_mode"],
        search_criteria=s["search_criteria"],
        target_fields=s["target_fields"],
        scope=s["scope"],
        scope_value=str(s["scope_value"]),
        skip_approvals=True,
        status="pending",
        notes=f"{WAVE39_NOTE} shape={shape}",
    )
    # force_full: wipe workspace AND the analysis archive so the probe (and
    # therefore the swap/demote classification) definitely re-runs.
    dispatch_scrape_job(drive.id, rescrape=True, force_full=True)
    print(f"[{shape}] drive={drive.id} dispatched ({s['label']})", flush=True)
    return drive.id


def _drive_job(shape: str):
    from scraper.models import ScrapeJob

    return (
        ScrapeJob.objects.filter(
            notes__startswith=WAVE39_NOTE, notes__contains=f"shape={shape}"
        )
        .order_by("-id")
        .first()
    )


def watch(shape: str, timeout_s: int = 5400):
    job = _drive_job(shape)
    if job is None:
        print(f"[{shape}] NO drive job", flush=True)
        return None
    last = None
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        job.refresh_from_db()
        if job.status != last:
            print(
                f"[{shape}] {last} → {job.status} ({time.strftime('%H:%M:%S')})",
                flush=True,
            )
            last = job.status
        if job.status in ("completed", "failed", "cancelled"):
            return job
        time.sleep(20)
    print(f"[{shape}] TIMEOUT after {timeout_s}s (status={job.status})")
    return job


def _output_raw(job) -> str:
    import src.artifacts as artifacts

    if not job or not job.output_file:
        return ""
    try:
        return artifacts.read_text(job.output_file)
    except Exception:
        try:
            with open(job.output_file, encoding="utf-8") as fh:
                return fh.read()
        except Exception:
            return ""


def evidence(shape: str, other_registrable: str) -> bool:
    s = SHAPES[shape]
    job = _drive_job(shape)
    if job is None:
        print(f"== [{shape}] NO drive job ==")
        return False
    print(f"\n== [{shape}] {s['label']} drive={job.id} ==")
    print(f"  status={job.status}  product_count={job.product_count}")
    notes = job.notes or ""
    verdict = []
    if shape == "swap":
        if job.status != "completed":
            verdict.append(f"FAIL(status={job.status})")
        if (job.product_count or 0) < 2:
            verdict.append(
                f"FAIL(count={job.product_count} < 2 — swap did not broaden discovery)"
            )
        if "[INTAKE-PDP-SWAP]" not in notes:
            verdict.append("FAIL(notes missing [INTAKE-PDP-SWAP])")
        if "[INTAKE-PDP]" in notes:
            verdict.append("FAIL(demote marker [INTAKE-PDP] present)")
    else:  # demote control
        if job.status != "completed":
            verdict.append(f"FAIL(status={job.status})")
        if (job.product_count or 0) != 1:
            verdict.append(f"FAIL(count={job.product_count} != 1)")
        if "[INTAKE-PDP]" not in notes:
            verdict.append("FAIL(notes missing demote marker [INTAKE-PDP])")
    raw = _output_raw(job)
    if raw and other_registrable and other_registrable in raw:
        verdict.append(f"FAIL(output contains {other_registrable})")
    print(f"  VERDICT: {'PASS' if not verdict else '; '.join(verdict)}")
    return not verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("drive", "watch", "evidence"))
    ap.add_argument("--delay", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=5400)
    args = ap.parse_args()
    _setup_django()

    if args.cmd == "seed_only":
        for shape in SHAPES:
            seed(shape)
        return 0
    if args.cmd == "watch":
        for shape in SHAPES:
            watch(shape, args.timeout)
        return 0
    if args.cmd == "evidence":
        oks = {
            sh: evidence(
                sh, SHAPES["demote" if sh == "swap" else "swap"]["registrable"]
            )
            for sh in SHAPES
        }
        return 0 if all(oks.values()) else 1

    # drive: seed B after a short stagger, watch BOTH concurrently, evidence.
    results: dict[str, object] = {}

    def _seed_later(shape: str) -> None:
        if shape == "demote":
            time.sleep(args.delay)
        seed(shape)

    threads = [
        threading.Thread(target=_seed_later, args=(sh,), daemon=True) for sh in SHAPES
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("both drives dispatched — watching", flush=True)

    watchers = [
        threading.Thread(
            target=lambda sh=sh: results.__setitem__(sh, watch(sh, args.timeout)),
            daemon=True,
        )
        for sh in SHAPES
    ]
    for w in watchers:
        w.start()
    for w in watchers:
        w.join()

    oks = {
        sh: evidence(sh, SHAPES["demote" if sh == "swap" else "swap"]["registrable"])
        for sh in SHAPES
    }
    print("\n== GATE SUMMARY ==")
    for sh, ok in oks.items():
        print(f"  {sh}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(oks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
