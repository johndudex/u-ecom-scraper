"""Partner-API extractor resource (wave-27 W27-6).

An "extractor" on the partner surface is a Site — the reusable configuration
a job runs against. Tenancy is DERIVED, not stored: the list/detail/PATCH/
archive views only ever see Sites whose ``url`` matches one of the API key's
own ScrapeJobs (trailing-slash tolerant, iexact-equivalent). This is the same
scoping rule ``list_jobs`` applies to jobs — no ``Site.owner`` migration, and
other partners' sites 404 (never 403 — no oracle, mirroring ``_api_get_job``).

PATCH is a STRICT allowlist (unknown keys → 422) and archive/unarchive is the
removal path: there is NO partner DELETE in v1 (hard delete stays a
superuser UI action, per the wave-27 "delete is dangerous" agreement).
"""
from __future__ import annotations

import json
import logging

from django.core.paginator import Paginator
from django.http import JsonResponse
from django.utils import timezone

from .. import models
from . import errors
from .views import api_view

logger = logging.getLogger("scraper.api")

PATCH_ALLOWLIST = frozenset(
    {"name", "site_type", "sample_url", "currency", "input_urls", "field_notes"}
)
MAX_INPUT_URLS = 10_000        # parity with create_job's item_urls cap
MAX_NOTE_LEN = 300             # parity with src.schema_validation.MAX_DESCRIPTION_LEN
MAX_FIELD_NOTES = 100          # parity with src.schema_validation.MAX_FIELD_NOTES


# ── tenancy-scoped Site access ───────────────────────────────────────────────
def _scoped_sites(api_user):
    """Distinct Sites whose url matches any of this key's job urls.

    Both slash variants are matched because Site.save keeps a stored url's
    trailing slash while job urls may or may not carry one (the wave-27a
    ``_handle_new_site`` finding).
    """
    urls = set(
        models.ScrapeJob.objects.filter(user=api_user)
        .exclude(url="")
        .values_list("url", flat=True)
    )
    bases = {u.rstrip("/") for u in urls}
    variants = bases | {b + "/" for b in bases}
    return models.Site.objects.filter(url__in=variants)


def _get_scoped(slug: str, api_user) -> models.Site:
    site = _scoped_sites(api_user).filter(slug=slug).first()
    if site is None:
        raise errors.not_found(f"Extractor {slug}")
    return site


# ── projections ──────────────────────────────────────────────────────────────
def _iso(dt):
    return dt.isoformat() if dt else None


def _extractor_summary(site: models.Site) -> dict:
    return {
        "slug": site.slug,
        "name": site.name,
        "url": site.url,
        "site_type": site.site_type or None,
        "has_scraper": site.has_scraper,
        "product_count": site.product_count,
        "last_scraped_at": _iso(site.last_scraped_at),
        "archived_at": _iso(site.archived_at),
    }


def _extractor_detail(site: models.Site) -> dict:
    payload = _extractor_summary(site)
    payload.update(
        {
            "platform": site.platform or None,
            "scraping_method": site.scraping_method or None,
            "input_urls_count": len(site.input_urls or []),
            "fields": list((site.output_schema or {}).get("fields") or []),
        }
    )
    return payload


# ── endpoints ────────────────────────────────────────────────────────────────
@api_view(["GET"])
def list_extractors(request):
    try:
        page = max(1, int(request.GET.get("page", 1)))
        page_size = int(request.GET.get("page_size", 20))
        if not 1 <= page_size <= 100:
            raise ValueError
    except (TypeError, ValueError):
        raise errors.ApiError(
            422, "invalid_page_size", "page must be >= 1 and page_size in [1, 100]."
        )
    qs = _scoped_sites(request.api_user).order_by("-updated_at", "-id")
    paginator = Paginator(qs, page_size)
    if page > paginator.num_pages and paginator.num_pages > 0:
        raise errors.ApiError(422, "invalid_page", f"page must be <= {paginator.num_pages}.")
    rows = paginator.page(page).object_list
    return JsonResponse(
        {
            "extractors": [_extractor_summary(s) for s in rows],
            "page": page,
            "page_size": page_size,
            "total_items": paginator.count,
            "total_pages": paginator.num_pages,
        }
    )


@api_view(["GET"])
def extractor_detail(request, slug: str):
    return JsonResponse(_extractor_detail(_get_scoped(slug, request.api_user)))


@api_view(["PATCH"])
def patch_extractor(request, slug: str):
    site = _get_scoped(slug, request.api_user)
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise errors.ApiError(400, "validation_failed", "Request body is not valid JSON.")
    if not isinstance(body, dict):
        raise errors.ApiError(422, "validation_failed", "Body must be a JSON object.")

    unknown = set(body) - PATCH_ALLOWLIST
    if unknown:
        raise errors.ApiError(
            422, "validation_failed",
            f"Unknown field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(PATCH_ALLOWLIST))}.",
        )

    updates: dict = {}
    if "name" in body:
        name = str(body["name"] or "").strip()
        if not name or len(name) > 200:
            raise errors.ApiError(422, "validation_failed", "name must be 1-200 characters.")
        updates["name"] = name

    if "site_type" in body:
        st = str(body["site_type"] or "").strip()
        from src.content_types import SITE_TYPE_CHOICES

        if st not in {c for c, _ in SITE_TYPE_CHOICES}:
            raise errors.ApiError(
                422, "validation_failed",
                "site_type must be one of: " + ", ".join(c for c, _ in SITE_TYPE_CHOICES) + ".",
            )
        updates["site_type"] = st

    if "sample_url" in body:
        su = str(body["sample_url"] or "").strip()
        if su and not su.startswith(("http://", "https://")):
            raise errors.ApiError(
                422, "validation_failed", "sample_url must be an absolute http(s) URL."
            )
        updates["sample_url"] = su

    if "currency" in body:
        cur = str(body["currency"] or "").strip().upper()
        if cur and (not cur.isalpha() or len(cur) > 10):
            raise errors.ApiError(
                422, "validation_failed", "currency must be a short alphabetic code (e.g. USD)."
            )
        updates["currency"] = cur

    if "input_urls" in body:
        urls = body["input_urls"]
        if not isinstance(urls, list) or any(
            not isinstance(u, str) or not u.strip() for u in urls
        ):
            raise errors.ApiError(
                422, "validation_failed", "input_urls must be an array of URL strings."
            )
        urls = [u.strip() for u in urls if u.strip()]
        if len(urls) > MAX_INPUT_URLS:
            raise errors.ApiError(
                422, "validation_failed", f"input_urls: max {MAX_INPUT_URLS} items."
            )
        updates["input_urls"] = urls

    if "field_notes" in body:
        # Notes live in Site.output_schema fields[].description (the same home
        # the finalizer persists to), so PATCH merges by field name.
        notes = body["field_notes"]
        if not isinstance(notes, dict) or len(notes) > MAX_FIELD_NOTES:
            raise errors.ApiError(
                422, "validation_failed",
                f"field_notes must be an object with at most {MAX_FIELD_NOTES} entries.",
            )
        cleaned: dict = {}
        for k, v in notes.items():
            if not isinstance(k, str) or not isinstance(v, str) or not v.strip():
                raise errors.ApiError(
                    422, "validation_failed",
                    "field_notes keys must be field names and values non-empty strings.",
                )
            if len(v) > MAX_NOTE_LEN:
                raise errors.ApiError(
                    422, "validation_failed",
                    f"field_notes['{k}'] is {len(v)} chars; the limit is {MAX_NOTE_LEN}.",
                )
            cleaned[k] = v.strip()
        fields = [
            f for f in ((site.output_schema or {}).get("fields") or [])
            if isinstance(f, dict) and f.get("name")
        ]
        merged = [
            {**f, "description": cleaned[f["name"]]} if f["name"] in cleaned else f
            for f in fields
        ]
        have = {f.get("name") for f in merged}
        merged.extend(
            {"name": k, "description": v} for k, v in cleaned.items() if k not in have
        )
        updates["output_schema"] = {**(site.output_schema or {}), "fields": merged}

    if updates:
        for k, v in updates.items():
            setattr(site, k, v)
        site.save(update_fields=[*updates.keys(), "updated_at"])
        logger.info("extractor PATCH %s: %s by key %s", slug, sorted(updates),
                    request.api_key.prefix)
    return JsonResponse(_extractor_detail(site))


@api_view(["POST"])
def archive_extractor(request, slug: str):
    site = _get_scoped(slug, request.api_user)
    if site.archived_at is None:
        site.archived_at = timezone.now()
        site.save(update_fields=["archived_at", "updated_at"])
    return JsonResponse({"slug": site.slug, "archived_at": _iso(site.archived_at)})


@api_view(["POST"])
def unarchive_extractor(request, slug: str):
    site = _get_scoped(slug, request.api_user)
    if site.archived_at is not None:
        site.archived_at = None
        site.save(update_fields=["archived_at", "updated_at"])
    return JsonResponse({"slug": site.slug, "archived_at": None})


def extractor_dispatch(request, slug: str):
    """GET/PATCH dispatcher for /extractors/<slug>; DELETE is a deliberate 405."""
    if request.method == "GET":
        return extractor_detail(request, slug=slug)
    if request.method == "PATCH":
        return patch_extractor(request, slug=slug)
    return JsonResponse(
        {
            "code": "method_not_allowed",
            "message": (
                "extractors have no DELETE by design — POST .../archive is the "
                "removal path; hard delete stays a superuser UI action."
            ),
        },
        status=405,
    )
