"""O2: one-off File-Master seeding of input_urls.json from local scrapers/ dirs.

WHY: the Jul-28 File-Master migration made _build_initial_state read
input_urls.json from the FM ONLY (tasks.py:486-503), but the FM was never
seeded — 366 local files on prod, 1 in the FM. Every pre-migration site's
url_list fallback is dead, and operator hand-fixes written to the local dir
are silently ignored (prod priceline/pillowtalk).

RAILS (from the critique loop — do not weaken):
  1. Site-backed slugs only (a Site row must exist for the slug).
  2. First-URL shape check: http(s) + path depth >= 2 (catches homepage/
     listing-page seeds like kirkland.com/ or dystaffing.com/job-search).
  3. >=2 URLs unless --allow-single is passed for that slug.
  4. Denylist test slugs (skipverify-example, books-toscrape-com,
     desidime-com, and anything whose first-URL host != the slug's
     Site.url host registrable).
  5. DRY-RUN by default: prints slug -> N urls -> first url -> Site.status;
     --apply performs the writes via src.artifacts (FM-authoritative).
  6. Never shrinks: skips a slug whose FM key already exists (the
     shrink-guard makes a bad seed sticky — only seed, never overwrite).

Run ON THE SERVER (where the 366 files live) inside any container with
FILE_MASTER_URL + django settings, e.g.:
    docker compose exec -T django python /app/scripts/seed_fm_input_urls.py
    docker compose exec -T django python /app/scripts/seed_fm_input_urls.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DENYLIST = {
    "skipverify-example",
    "books-toscrape-com",
    "desidime-com",
}


def _registrable(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        two_part = (
            ".co.uk", ".org.uk", ".com.au", ".co.nz", ".co.za", ".com.br",
            ".co.jp", ".com.sg", ".com.mx",
        )
        for tld in two_part:
            if host.endswith(tld):
                pre = host[: -len(tld)].rstrip(".")
                return f"{pre.split('.')[-1]}{tld}" if pre else host
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""


def _path_depth(url: str) -> int:
    from urllib.parse import urlparse

    try:
        return len([p for p in urlparse(url).path.split("/") if p])
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    ap.add_argument("--root", default=None, help="project root (default: django PROJECT_ROOT)")
    args = ap.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from django.conf import settings
    from scraper.models import Site
    import src.artifacts as artifacts

    root = args.root or str(settings.PROJECT_ROOT)
    scrapers_dir = os.path.join(root, "scrapers")
    if not os.path.isdir(scrapers_dir):
        print(f"NO scrapers dir at {scrapers_dir}")
        return 1

    sites = {s.slug: s for s in Site.objects.all() if s.slug}
    plan: list[tuple[str, int, str, str]] = []  # slug, n, first, site_status
    skipped: list[tuple[str, str]] = []

    for entry in sorted(os.listdir(scrapers_dir)):
        slug = entry
        fpath = os.path.join(scrapers_dir, entry, "input_urls.json")
        if not os.path.isfile(fpath):
            continue
        if slug in DENYLIST:
            skipped.append((slug, "denylist (test slug)"))
            continue
        site = sites.get(slug)
        if not site:
            skipped.append((slug, "no Site row"))
            continue
        try:
            data = json.load(open(fpath))
            urls = [u for u in (data.get("urls") or []) if isinstance(u, str) and u.strip()]
        except Exception as exc:
            skipped.append((slug, f"unparseable: {exc}"))
            continue
        if not urls:
            skipped.append((slug, "empty url list"))
            continue
        first = urls[0].strip()
        if not first.startswith(("http://", "https://")):
            skipped.append((slug, f"first URL not http: {first[:50]}"))
            continue
        if _path_depth(first) < 2:
            skipped.append((slug, f"first URL too shallow (homepage/listing?): {first[:60]}"))
            continue
        site_reg = _registrable(site.url or "")
        first_reg = _registrable(first)
        if site_reg and first_reg and site_reg != first_reg:
            skipped.append((slug, f"host mismatch: seed {first_reg} vs site {site_reg}"))
            continue
        if len(urls) < 2:
            skipped.append((slug, f"single URL (pass --allow-single to force)"))
            continue
        key = artifacts.scrapers_key(slug, "input_urls.json")
        if artifacts.exists(key):
            skipped.append((slug, "FM key already exists (never shrink/overwrite)"))
            continue
        plan.append((slug, len(urls), first, site.status))

    print(f"== FM input_urls seeding ({'APPLY' if args.apply else 'DRY RUN'}) ==")
    print(f"candidates: {len(plan)}  skipped: {len(skipped)}\n")
    for slug, n, first, status in plan:
        print(f"  {slug:36s} {n:4d} urls  site={status:12s} first={first[:60]}")
    if skipped:
        print("\nskipped:")
        for slug, why in skipped:
            print(f"  {slug:36s} {why}")

    if not args.apply:
        print("\n(dry-run: re-run with --apply to write)")
        return 0
    import src.artifacts as art
    written = 0
    for slug, n, first, _ in plan:
        src = os.path.join(scrapers_dir, slug, "input_urls.json")
        art.write_json(art.scrapers_key(slug, "input_urls.json"), json.load(open(src)))
        written += 1
        print(f"wrote scrapers/{slug}/input_urls.json ({n} urls)")
    print(f"\ndone: {written} written, {len(skipped)} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
