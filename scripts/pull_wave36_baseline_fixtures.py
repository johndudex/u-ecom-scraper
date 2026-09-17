"""[wave-36 gate] Pull baseline fixtures for the 14-site LOCAL gate (read-only).

Per plan §5.1: for each baseline prod job capture
  - from ``/jobs/<id>/api/``: chips, input_mode, search_criteria, scope,
    scope_value, url, site_folder, output_file, product_count;
  - from the output download: ``metadata.discovered_urls`` COUNT (the list
    itself is not recoverable — checkpoint never preserved);
  - the prod draft via its PER-JOB FM key (``scrapers/{slug}/jobs/
    scraper-draft-{id}.py`` → ``-good`` freeze → plain ``scraper-{id}.py``),
    NOT the ``/jobs/<id>/scraper-code/`` view whose fallback serves the
    shared later-overwritten ``scrapers/{slug}/scraper.py``.

Stored under ``tests/fixtures/wave36_baseline/<job_id>/``:
  ``fixture.json`` (metadata + discovery count) and ``draft.py``.

Usage:
  python3 scripts/pull_wave36_baseline_fixtures.py            # all 14
  python3 scripts/pull_wave36_baseline_fixtures.py 658 657    # subset
"""
from __future__ import annotations

import json
import os
import sys

BASE = "https://django-production-3b34.up.railway.app"
COOKIES = "/tmp/rj_cookies.txt"
OUT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "wave36_baseline",
)

BASELINE = [658, 657, 423, 387, 477, 499, 375, 473, 653, 403, 481, 469, 479, 498]


def _get(path: str) -> bytes | None:
    import subprocess

    r = subprocess.run(
        ["curl", "-s", "-m", "60", "-b", COOKIES, f"{BASE}{path}"],
        capture_output=True,
    )
    return r.stdout or None


def pull(job_id: int) -> bool:
    raw = _get(f"/jobs/{job_id}/api/")
    if not raw:
        print(f"[{job_id}] API fetch FAILED")
        return False
    api = json.loads(raw)
    slug = (api.get("site_folder") or "").rsplit("/", 1)[-1]
    out_file = (api.get("output_file") or "").rsplit("/", 1)[-1]
    rec = {
        "job_id": job_id,
        "url": api.get("url"),
        "site_slug": slug,
        "site_name": api.get("site_name"),
        "platform": api.get("platform"),
        "target_fields": api.get("target_fields"),
        "input_mode": api.get("input_mode"),
        "search_criteria": api.get("search_criteria"),
        "scope": api.get("scope"),
        "scope_value": api.get("scope_value"),
        "product_count": api.get("product_count"),
        "output_file": out_file,
        "completed_at": api.get("completed_at"),
        "discovered_urls_count": None,
        "output_key": None,
        "draft_key": None,
    }

    # Output download → discovery count (second-signal corroboration material).
    if out_file:
        blob = _get(f"/jobs/{job_id}/output/{out_file}/download/")
        if blob:
            try:
                data = json.loads(blob)
                meta = data.get("metadata") or {}
                rec["output_key"] = data.get("products") is not None and "products" or (
                    next(iter(k for k, v in data.items() if isinstance(v, list)), None)
                )
                disc = meta.get("discovered_urls")
                if isinstance(disc, list):
                    rec["discovered_urls_count"] = len(disc)
                elif isinstance(disc, (int, float)):
                    rec["discovered_urls_count"] = int(disc)
            except Exception as exc:
                print(f"[{job_id}] output parse failed: {exc}")
        else:
            print(f"[{job_id}] output download FAILED")

    # Per-job draft archive (fallback chain per plan §5.1).

    for key in (
        f"scrapers/{slug}/jobs/scraper-draft-{job_id}.py",
        f"scrapers/{slug}/jobs/scraper-draft-{job_id}-good.py",
        f"scrapers/{slug}/jobs/scraper-{job_id}.py",
    ):
        blob = _get(f"/fm/artifact/{key}/")
        if blob and blob.lstrip()[:1] not in (b"<", b"{"):  # HTML/JSON = miss
            rec["draft_key"] = key
            break

    ddir = os.path.join(OUT_ROOT, str(job_id))
    os.makedirs(ddir, exist_ok=True)
    with open(os.path.join(ddir, "fixture.json"), "w") as f:
        json.dump(rec, f, indent=2)
    if rec["draft_key"] and blob:
        with open(os.path.join(ddir, "draft.py"), "wb") as f:
            f.write(blob)

    print(
        f"[{job_id}] {slug:28s} chips={len(rec['target_fields'] or [])} "
        f"mode={rec['input_mode']:12s} scope={rec['scope']}/{rec['scope_value']} "
        f"shipped={rec['product_count']} discovered={rec['discovered_urls_count']} "
        f"draft={'YES' if rec['draft_key'] else 'MISSING'}"
    )
    return bool(rec["draft_key"])


def main() -> int:
    jobs = [int(a) for a in sys.argv[1:]] or BASELINE
    ok = 0
    for jid in jobs:
        try:
            ok += 1 if pull(jid) else 0
        except Exception as exc:
            print(f"[{jid}] ERROR: {exc}")
    print(f"\n{ok}/{len(jobs)} fixtures complete (draft present)")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
