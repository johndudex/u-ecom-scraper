"""Shared seed-URL hygiene — one filter rule for every surface that writes,
redirects, or promotes an ``input_urls.json``.

[wave-14 job-133] athleta.gap.com's seed file arrived polluted with gap.com
family links (``www.gap.com/...``, ``bananarepublic.gap.com/...``) and the
intake filter passed them: it compared *registrable domains* (``gap.com`` ==
``gap.com``). Every downstream phase then burned cycles extracting fields
from another brand's product pages, and the run died honest-but-poisoned.

The rule everywhere is now **full-host equality** (case-folded, one leading
``www.`` stripped): the seed may only contain URLs on the exact host the job
was submitted against. Sibling subdomains and registrable-family links are
dropped — a wrong-brand item URL is never a legitimate seed, and a scraper
that needs an CDN/API host discovers that host at runtime, not from the
user's seed list.

Surfaces that call this:

- ``setup_workspace._filter_seed_urls`` — intake (url_list) filtering
- ``scraper.models._sync_input_urls_file`` — Site.input_urls → FM file
- ``scraper.views`` re-run staging — FM re-run writes + extra_files staging
- ``agents.tools.shell_tools.run_scraper`` — seed-file hygiene before dispatch
  (belt: catches ANY writer this module's callers missed)
- ``agents.graph`` code_writer seed write — navigation-derived sample URLs

Stdlib only (``urllib.parse``) — importable from src/, webapp/, templates.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = [
    "filter_seed_payload",
    "filter_seed_urls",
    "normalize_host",
    "seed_report",
]


def normalize_host(host: str | None) -> str:
    """Case-folded host with one leading ``www.`` stripped.

    ``www.shop-com-au.com`` and ``shop-com-au.com`` are the same shop front —
    the intake form and the discovered links disagree on the prefix all the
    time, and refusing to canonicalize it would drop legitimate seeds.
    ``athleta.gap.com`` and ``gap.com`` stay DIFFERENT — that is the point.
    """
    return str(host or "").strip().lower().removeprefix("www.")


def seed_report(urls: list, job_url: str) -> tuple[list[str], dict[str, int]]:
    """Filter ``urls`` down to same-host, item-plausible seeds.

    Returns ``(kept, dropped_by_reason)`` so callers can log what was thrown
    away. Rules, in order:

    - blank / non-string → ``blank``
    - unparseable → ``unparseable``
    - non-http(s) scheme or no hostname → ``not-http``
    - pathless AND queryless (``https://site.com``) → ``no-path`` — a bare
      homepage is not an item URL
    - host (normalized) != job host (normalized) → ``off-host``. A blank
      ``job_url`` disables this rule (the seed list is then only
      sanity-filtered, never host-filtered).
    - exact duplicate of an earlier entry → ``duplicate`` (first wins; a
      ``?x=1`` variant is a distinct URL)
    """
    try:
        job_host = normalize_host(urlparse(job_url or "").hostname)
    except Exception:
        job_host = ""

    kept: list[str] = []
    dropped: dict[str, int] = {}
    seen: set[str] = set()

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for raw in urls or []:
        if not isinstance(raw, str):
            u = str(raw or "").strip()
        else:
            u = raw.strip()
        if not u:
            _drop("blank")
            continue
        try:
            p = urlparse(u)
        except Exception:
            _drop("unparseable")
            continue
        if p.scheme not in ("http", "https") or not p.hostname:
            _drop("not-http")
            continue
        if not p.path.strip("/") and not p.query:
            _drop("no-path")
            continue
        if job_host and normalize_host(p.hostname) != job_host:
            _drop("off-host")
            continue
        if u in seen:
            _drop("duplicate")
            continue
        seen.add(u)
        kept.append(u)
    return kept, dropped


def dropped_summary(dropped: dict[str, int]) -> str:
    """Compact ``{k: v}`` → ``"off-host=3, duplicate=1"`` for log lines."""
    return ", ".join(f"{k}={v}" for k, v in sorted(dropped.items()))


def filter_seed_urls(urls: list, job_url: str) -> list[str]:
    """Just the kept list (the common caller shape)."""
    return seed_report(urls, job_url)[0]


def filter_seed_payload(
    payload: dict | list, job_url: str
) -> tuple[dict | list, dict[str, int]]:
    """Filter a full ``input_urls.json`` payload, preserving its shape.

    Accepts ``{"urls": [...]}`` (the standard shape) or a bare list
    (job-88 legacy drafts wrote bare arrays). Returns ``(filtered_payload,
    dropped)`` — ``filtered_payload`` is the SAME object when nothing was
    dropped (no mtime churn for callers that rewrite files), and a new
    payload of the same shape otherwise.
    """
    if isinstance(payload, dict):
        urls = payload.get("urls") or []
    else:
        urls = payload or []
    kept, dropped = seed_report(urls, job_url)
    if not dropped:
        return payload, dropped
    if isinstance(payload, dict):
        return {**payload, "urls": kept}, dropped
    return kept, dropped
