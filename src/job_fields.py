"""Generic job field-mapping resolver.

Maps raw job items from ANY source — a backend search-API JSON object, a
schema.org ``JobPosting`` JSON-LD block, or a DOM-derived dict — to the
standard job output fields (title, company, location, description,
posted_date, salary, job_type, requirements, apply_url) **without hardcoding
site-specific field names**.

How it stays generic: for each output field we keep a ranked list of candidate
source paths (the schema.org ``jsonld_key`` first, then common API names, plus
"composite" candidates like ``city.name + state.abbrev``). ``infer_field_map``
inspects a sample batch and picks, per field, the candidate with the highest
non-empty coverage — so a new platform works with zero per-site config if its
field names are in the alias table.

Candidates are plain strings (composites encoded as ``$compose:`` / ``$salrange:``
sentinels) so a field_map is JSON-serializable and drops into ``analysis["fields"]``.

This module has no Django dependency.  The date/location helpers were moved here
from ``templates/job_scraper.py`` so normalization is centralized.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from src.content_types import JOB_FIELDS, FieldDef

# Re-exported for templates/tests that import from here.
__all__ = [
    "resolve_path",
    "infer_field_map",
    "apply_field_map",
    "map_jobs",
    "parse_posted_date",
    "JOB_ALIASES",
    "MIN_SAMPLE_FOR_COVERAGE",
]

MIN_SAMPLE_FOR_COVERAGE = 3

# Output field names we always emit (so downstream never KeyErrors).
_JOB_OUTPUT_FIELDS: tuple[str, ...] = tuple(f.name for f in JOB_FIELDS)

# ─── date + location helpers (centralized, reused by templates) ───────────────

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

_DATE_FORMATS = [
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y",
    "%d %B %Y", "%d %b %Y",
]


def parse_posted_date(date_str: str) -> Optional[datetime]:
    """Parse a posting date from many formats. Returns datetime or None.

    Handles ISO strings (with optional timezone offset), common US formats,
    and relative phrases ("2 days ago", "today", "yesterday", "just posted").
    """
    if not date_str:
        return None


def assess_date_reliability(date_str: str, scraped_at_date) -> tuple:
    """Assess whether a posted_date is reliable for age filtering (P0-13).

    Sites that dynamically set JSON-LD datePosted to "today" on every page
    load produce fabricated freshness. This detects: equals_scrape_date,
    future_dated, missing. Returns (date_str_or_None, is_reliable: bool, reason: str).

    Generic — works for any site. The only fully-generic fallback for
    unreliable dates is the system-tracked first_seen_at (which the caller
    should prefer when is_reliable=False).
    """
    from datetime import date as _date

    dt = parse_posted_date(date_str if isinstance(date_str, str) else str(date_str))
    if not dt:
        return (None, False, "missing")
    if isinstance(scraped_at_date, str):
        try:
            scraped_at_date = _date.fromisoformat(scraped_at_date[:10])
        except (ValueError, TypeError):
            scraped_at_date = _date.today()
    posted = dt.date() if hasattr(dt, "date") else dt
    if posted == scraped_at_date:
        return (posted.isoformat(), False, "equals_scrape_date")
    if posted > scraped_at_date:
        return (posted.isoformat(), False, "future_dated")
    return (posted.isoformat(), True, "ok")
    s = str(date_str).strip()
    if not s:
        return None

    # ISO-8601 with timezone (e.g. 2026-06-25T12:27:50.12+00:00) — fromisoformat
    # handles most; fall back to chopping fractional seconds.
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    low = s.lower()
    now = datetime.now()
    if "today" in low or "just posted" in low or low == "just":
        return now
    if "yesterday" in low:
        return now - timedelta(days=1)

    m = re.search(r"(\d+)\s*(day|hour|week|month)s?\s*ago", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "hour":
            return now - timedelta(hours=n)
        if unit == "day":
            return now - timedelta(days=n)
        if unit == "week":
            return now - timedelta(weeks=n)
        if unit == "month":
            return now - timedelta(days=n * 30)
    return None


# ─── alias registry ───────────────────────────────────────────────────────────
# Plain strings.  Composite / range candidates use sentinels:
#   "$compose:p1|p2"           → join resolved p1, p2 with ", "
#   "$salrange:minPath|maxPath" → "$min - $max"
#
# The schema.org path (FieldDef.jsonld_key) is PREPENDED at module load
# (see _build_aliases), so it is candidate[0] and wins coverage ties.

_JOB_API_ALIASES: dict[str, list[str]] = {
    "title": [
        "title", "jobTitle", "positionTitle", "name", "headline", "job_title",
        "$compose:employmentTypeText|expertiseText|professionText",
    ],
    "company": [
        # schema.org hiringOrganization.name is prepended automatically.
        "divisionCompany.companyName", "organization.name", "company.name",
        "company", "companyName", "employer", "recruiter", "clientName",
        "facilityName",
    ],
    "location": [
        # schema.org jobLocation.address.addressLocality is prepended automatically.
        "location", "formattedLocation", "locationName", "jobLocationText",
        "locationText", "address",
        "$compose:jobLocation.address.addressLocality|jobLocation.address.addressRegion",
        "$compose:city.name|state.abbrev",
        "$compose:city.name|state.name",
        "$compose:city|state",
        "city.name", "city",
        "Location", "LocationText", "LocationName",
        "$compose:City|State",
        "$compose:city|stateAbbr",
        "$compose:FacilityCity|FacilityState",
        "$compose:jobCity|jobState",
        "$compose:city|stateAbbrev",
    ],
    "salary": [
        # schema.org baseSalary.value.value is prepended automatically.
        "salary", "salaryRange", "compensation", "pay",
        "customPayShift",
        "$salrange:payRate.minPayRate|payRate.maxPayRate",
        "$salrange:salary.min|salary.max",
        "$salrange:salaryMin|salaryMax",
        "$salrange:minSalary|maxSalary",
        "$salrange:regularPayLow|regularPayHigh",
        "$salrange:weeklyPayLow|weeklyPayHigh",
    ],
    "description": [
        "description", "descriptionLong", "jobDescription", "summary",
        "body", "about", "jobSummary",
        "details",
    ],
    "requirements": [
        "qualifications", "requirements", "skills", "requirementList",
    ],
    "job_type": [
        "employmentType", "jobType", "employment_type", "workType",
        "type", "job_type_name",
        "employmentTypeText",
    ],
    "apply_url": [
        "url", "applyUrl", "DetailsUrl", "jobUrl", "link",
        "detailUrl", "canonicalUrl", "externalUrl",
    ],
    "posted_date": [
        "datePosted", "postedDate", "posted_at", "datePublished",
        "createdAt", "listingDate", "searchDocumentModified",
        "postDate", "publishedDate", "PostedDate", "DatePosted",
        "PostedOn", "Posted", "CreatedDate", "DateCreated",
        "created_at", "modifiedDate", "updated_at", "lastModified",
        "publishDate", "listedDate", "jobPostedDate", "openDate",
        "posted", "enteredTime", "applicationDate",
    ],
}


def _build_aliases() -> dict[str, list[str]]:
    """Prepend each field's schema.org jsonld_key (ranked first), dedup."""
    out: dict[str, list[str]] = {}
    for fdef in JOB_FIELDS:
        seen: list[str] = []
        for c in (fdef.jsonld_key, *_JOB_API_ALIASES.get(fdef.name, [])):
            if c and c not in seen:
                seen.append(c)
        out[fdef.name] = seen
    return out


JOB_ALIASES: dict[str, list[str]] = _build_aliases()


# ─── path resolution ──────────────────────────────────────────────────────────
# Keys that identify the "scalar payload" of a dict leaf.  A dict with NONE of
# these (e.g. a GeoJSON ``{type:"Point", coordinates:[...]}``) is treated as
# NOT a scalar value — so a candidate resolving to such a dict counts as absent
# and loses coverage to a real place-name composite like city.name+state.abbrev.
_SCALAR_KEYS = ("name", "abbrev", "value", "text", "title", "label", "code")


def _recognized_scalar(d: dict) -> Any:
    """If dict has a recognized scalar payload key, return its value; else None."""
    for k in _SCALAR_KEYS:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def resolve_path(obj: Any, dotted: str) -> Any:
    """Walk a dotted path over dicts/lists, returning the RAW leaf value.

    - descends into a list by picking the first element that has the next key;
    - if a step lands on a string before the path is exhausted, the string IS
      the value (schema.org allows e.g. ``hiringOrganization: "Acme"``);
    - returns the raw leaf (dict / list / scalar / None) — coercion is done by
      the caller (``_present`` for coverage, ``_normalize_value`` for output).
    """
    if not dotted:
        return obj
    cur: Any = obj
    for key in dotted.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            nxt = None
            for e in cur:
                if isinstance(e, dict) and key in e:
                    nxt = e[key]
                    break
            cur = nxt
        elif isinstance(cur, str):
            # Leaf reached mid-path (e.g. org as a plain string) — it's the value.
            return cur
        else:
            return None
    return cur


def _present(raw: Any) -> bool:
    """Is this raw resolved value a usable, non-empty value for coverage?

    A dict counts only if it has a recognized scalar payload (so a GeoJSON
    Point is NOT counted as a populated location).  A list counts if any
    element is present.  ``0``/``False`` count as present (they are data).
    """
    if raw is None:
        return False
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, dict):
        return _recognized_scalar(raw) not in (None, "")
    if isinstance(raw, list):
        return any(_present(e) for e in raw)
    return True  # int / float / bool


def _resolve_candidate(item: dict, cand: str) -> Any:
    """Resolve one candidate string (plain path, $compose, or $salrange) to its
    RAW value (or a computed joined string for composites/ranges)."""
    if cand.startswith("$compose:"):
        parts = cand[len("$compose:"):].split("|")
        vals: list[str] = []
        for p in parts:
            v = resolve_path(item, p)
            if not _present(v):
                return None
            vals.append(str(_as_str(v)))
        return ", ".join(vals)
    if cand.startswith("$salrange:"):
        parts = cand[len("$salrange:"):].split("|")
        lo = resolve_path(item, parts[0]) if len(parts) > 0 else None
        hi = resolve_path(item, parts[1]) if len(parts) > 1 else None
        if not _present(lo) and not _present(hi):
            return None
        return _format_salary_range(lo, hi)
    return resolve_path(item, cand)


def _as_str(raw: Any) -> str:
    """Reduce a raw leaf to a display string (used after _present confirms it)."""
    if isinstance(raw, dict):
        s = _recognized_scalar(raw)
        return str(s) if s is not None else ""
    if isinstance(raw, list):
        # first present element as a scalar
        for e in raw:
            if _present(e):
                return _as_str(e)
        return ""
    if raw is None:
        return ""
    return str(raw)


def _format_salary_range(lo: Any, hi: Any) -> str:
    def to_num_or_str(raw: Any) -> Any:
        if isinstance(raw, dict):
            raw = _recognized_scalar(raw)
        if isinstance(raw, list):
            raw = _as_str(raw)
        return raw

    def fmt(x: Any) -> str:
        if x is None or x == "":
            return ""
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            try:
                return f"${int(x):,}" if float(x).is_integer() else f"${x:,.2f}"
            except Exception:
                return f"${x}"
        s = str(x).strip().replace(",", "")
        # Numeric-looking strings get comma-formatted for consistency.
        try:
            f = float(s)
            return f"${int(f):,}" if f.is_integer() else f"${f:,.2f}"
        except ValueError:
            return s if s.startswith("$") else f"${s}"

    a, b = fmt(to_num_or_str(lo)), fmt(to_num_or_str(hi))
    if a and b:
        return f"{a} - {b}"
    return a or b


# ─── JSON-LD awareness ────────────────────────────────────────────────────────

def _jsonld_types(content_type_config: Any) -> tuple[str, ...]:
    cfg = content_type_config
    if isinstance(cfg, dict):
        jt = cfg.get("jsonld_types") or cfg.get("expected_schema_type")
        if jt:
            return tuple(jt) if isinstance(jt, (list, tuple)) else (str(jt),)
    # default: JobPosting (this module is job-only)
    return ("JobPosting",)


def _unwrap_jsonld(item: Any, jsonld_types: tuple[str, ...]) -> dict:
    """If item is a JSON-LD wrapper (@graph) return the first typed node;
    otherwise return item as a dict."""
    if not isinstance(item, dict):
        return item if isinstance(item, dict) else {}
    graph = item.get("@graph")
    if isinstance(graph, list):
        for node in graph:
            if isinstance(node, dict) and node.get("@type") in jsonld_types:
                return node
        for node in graph:
            if isinstance(node, dict):
                return node
    return item


def _is_jsonld_item(item: dict, jsonld_types: tuple[str, ...]) -> bool:
    t = item.get("@type")
    if t is None:
        return False
    return t in jsonld_types


# ─── field-map inference + application ────────────────────────────────────────

def infer_field_map(
    sample_items: list[dict],
    content_type_config: Any = None,
    *,
    prefer_jsonld: bool = True,
) -> dict[str, Optional[str]]:
    """Return ``{output_field: winning_candidate_or_None}``.

    For each output field, pick the candidate with the highest non-empty
    coverage across ``sample_items``; ties broken by alias order (the
    schema.org path is first, so it wins ties when ``prefer_jsonld``).
    Below ``MIN_SAMPLE_FOR_COVERAGE`` items, skip coverage and return
    ``alias[0]``.  If no candidate hits any item, the field maps to ``None``.
    """
    jsonld_types = _jsonld_types(content_type_config)
    sample = [_unwrap_jsonld(it, jsonld_types) for it in (sample_items or []) if isinstance(it, dict)]
    n = len(sample)

    out: dict[str, Optional[str]] = {}
    for field, candidates in JOB_ALIASES.items():
        if not candidates:
            out[field] = None
            continue
        if n < MIN_SAMPLE_FOR_COVERAGE:
            # Too few items for reliable coverage stats — pick the first
            # candidate that is actually present on any sample item (the
            # schema.org path is first, so it's preferred when present).
            chosen: Optional[str] = None
            for cand in candidates:
                if any(_present(_resolve_candidate(s, cand)) for s in sample):
                    chosen = cand
                    break
            out[field] = chosen
            continue
        best: Optional[str] = None
        best_score = -1.0
        for idx, cand in enumerate(candidates):
            hits = 0
            for s in sample:
                v = _resolve_candidate(s, cand)
                if _present(v):
                    hits += 1
            cov = hits / n
            # Tiny decreasing epsilon by index → earlier candidate (schema.org)
            # wins exact ties.  Bonus when the sample is JSON-LD and this is the
            # jsonld_key (idx 0) makes preference explicit.
            score = cov - idx * 1e-9
            if prefer_jsonld and idx == 0 and any(_is_jsonld_item(s, jsonld_types) for s in sample):
                score += 1e-6
            if score > best_score:
                best_score = score
                best = cand
        out[field] = best if best_score > 0 else None
    return out


def _normalize_value(field: str, val: Any) -> str:
    """Per-field normalization applied after extraction.

    ``val`` is the RAW resolved leaf (dict / list / scalar / None), or an
    already-joined string for composite/range candidates.
    """
    if val is None:
        return ""
    if isinstance(val, dict):
        val = _recognized_scalar(val)
        if val is None:
            return ""
    if isinstance(val, list):
        sep = "\n" if field == "requirements" else ", "
        parts = [(_as_str(e) if isinstance(e, (dict, list)) else str(e)).strip()
                 for e in val]
        parts = [p for p in parts if p]
        return sep.join(parts)
    if field == "posted_date":
        dt = parse_posted_date(val if isinstance(val, str) else str(val))
        return dt.date().isoformat() if dt else (str(val).strip() if val else "")
    if isinstance(val, (int, float, bool)):
        return str(val)
    return str(val).strip()


def apply_field_map(
    raw_item: dict,
    field_map: dict[str, Optional[str]],
    content_type_config: Any = None,
) -> dict[str, Any]:
    """Extract + normalize one item using an inferred ``field_map``.

    Returns a dict with every job output field (empty string when unmapped)
    and preserves direct/derived keys (``url``, ``src_url``, ``status_code``,
    ``scraped_at``, ``remarks``, ``location_raw``, ``posted_date_parsed``)
    already present on ``raw_item``.
    """
    jsonld_types = _jsonld_types(content_type_config)
    item = _unwrap_jsonld(raw_item, jsonld_types)

    out: dict[str, Any] = {f: "" for f in _JOB_OUTPUT_FIELDS}
    for field, cand in field_map.items():
        if field not in out or cand is None:
            continue
        out[field] = _normalize_value(field, _resolve_candidate(item, cand))

    # Derived keys used by the filter helpers in templates/job_scraper.py.
    out["location_raw"] = out.get("location", "")
    out["posted_date_parsed"] = out.get("posted_date", "")

    # Preserve direct fields from the raw item (set by the caller/template).
    for k in ("url", "src_url", "status_code", "scraped_at", "remarks", "id"):
        if k in raw_item and raw_item[k] not in (None, ""):
            out[k] = raw_item[k]
    return out


def map_jobs(
    sample_items: list[dict],
    raw_items: list[dict],
    content_type_config: Any = None,
) -> list[dict]:
    """Infer the field map once on ``sample_items`` and apply to every raw item.

    The single entry point templates and tests should call.
    """
    fmap = infer_field_map(sample_items, content_type_config)
    return [apply_field_map(it, fmap, content_type_config) for it in (raw_items or [])]
