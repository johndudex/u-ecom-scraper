"""W31: site-level duplicate detection shared by the intake view and the
partner-API create endpoint (docs/plans/wave31-dedupe-plan.md).

Identity is the normalized HOST (src.seed_urls.normalize_host) — never the
raw URL (ScrapeJob.url stores one sample item page; every teammate pasting a
different product from the same site is a "new" URL today) and never
Site.slug (collision-prone, and the Site row may not exist until mid-job).

The gate fires on prior COMPLETED jobs only (failed/parked attempts are
surfaced as context but never block — product decision 2026-09-14), and an
archived Site is exempt (archive = a deliberate allow-re-scrape signal).

Query shape: SQL prefilter ``url__icontains=host`` capped at the 500 newest
rows, then an exact host refine in Python (icontains alone would match
"jo.com" inside "jo.com.au"). ScrapeJob.url is unindexed, so this is a
bounded table scan — tolerable at current volume (precedent:
intake_check_site views.py:2697); if it ever shows up, the fix is a
site_host column written at create time, not a gate change.
"""
from __future__ import annotations

from urllib.parse import urlparse

from django.urls import reverse

from . import models


def site_host(url: str) -> str:
    """Normalized host of a URL ("" when the URL has no parseable host)."""
    from src.seed_urls import normalize_host

    return normalize_host(urlparse(url or "").hostname)


def site_processing_history(url: str, limit: int = 5) -> dict:
    """Prior-job history for the host of ``url`` (team-wide by design)."""
    host = site_host(url)
    out = {
        "host": host, "site_slug": "", "processed": False,
        "has_scraper": False, "site_archived": False,
        "prior_jobs": [], "attempt_count": 0,
    }
    if not host:
        return out

    cands = (
        models.ScrapeJob.objects.exclude(url="")
        .filter(url__icontains=host)
        .order_by("-id")
        .values("id", "url", "status", "product_count", "created_at",
                "title", "user__username")[:500]
    )
    prior = [c for c in cands if site_host(c["url"]) == host]
    out["attempt_count"] = len(prior)
    out["prior_jobs"] = [
        {
            "job_id": c["id"],
            "title": c["title"] or "",
            "status": c["status"],
            "item_count": c["product_count"],
            "created_at": c["created_at"].isoformat() if c["created_at"] else "",
            "owner_username": c["user__username"] or "",
            "job_url": reverse("intake") + f"?job={c['id']}",
        }
        for c in prior[:limit]
    ]
    out["processed"] = any(
        c["status"] == models.ScrapeJob.STATUS_COMPLETED for c in prior
    )

    try:
        # Lazy like views.py — tasks pulls the whole celery/graph world.
        from .tasks import _generate_slug

        slug = _generate_slug(url)
        out["site_slug"] = slug
        site = (
            models.Site.objects.filter(slug=slug)
            .only("has_scraper", "archived_at")
            .first()
        )
        if site:
            out["has_scraper"] = bool(site.has_scraper)
            out["site_archived"] = site.archived_at is not None
    except Exception:
        pass

    return out
