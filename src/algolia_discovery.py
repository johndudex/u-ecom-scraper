"""[wave-50 T1] Catalog discovery for Algolia-backed client-rendered listings.

Job 1116 (wonderbly) root cause: the site's listing is Astro client-rendered —
the SSR HTML carries ZERO product hrefs — while the real catalog lives in an
Algolia index the page itself queries. The prod draft's Algolia query worked
(nbHits=86) but its hit→URL fallback only knew ``productUrl/product_url/url/
slug/handle``; this index carries slugs under ``solidusSlug`` and
``prismicProductSlug``. Zero URLs converted, the writer shipped SSR nav
anchors as "product URLs", and Phase 2 extracted junk (titles + defaulted
currency, empty price).

This module is the generic fix: extract the public search creds from the page
HTML (they ship to every visitor's browser — Astro serializes them into
astro-island ``props``), walk the index's pages, and convert every hit to a
PDP URL through a wide key fallback whose slug branch resolves the site's PDP
path prefix by sitemap grep + HEAD verification.

Transport is injectable (``fetch_fn`` / ``head_fn`` / ``sitemap_fn``) so tests
run on recorded payloads; the defaults use ``requests``. Every failure mode
degrades to an empty result — a caller falls back to its normal discovery
path, never crashes.
"""

from __future__ import annotations

import html as _html
import json as _json
import logging
import re as _re
import time as _time
from urllib.parse import quote as _quote
from urllib.parse import urljoin as _urljoin
from urllib.parse import urlparse as _urlparse

logger = logging.getLogger(__name__)

# Key families, in priority order. Absolute-URL keys win over path-absolute,
# which win over bare slugs; objectID is the last resort (it is an opaque key
# on most indices, but on Solidus-derived ones it is often the slug itself).
_ABSOLUTE_URL_KEYS = ("productUrl", "product_url", "url", "permalink")
_PATH_KEYS = ("url", "pathname", "path", "link")
_SLUG_KEYS = ("solidusSlug", "prismicProductSlug", "slug", "handle", "objectID")

# Candidate PDP prefixes tried by HEAD when the sitemap doesn't answer.
# "/products/" first: solidusSlug is a Solidus key and /products/<slug> is
# Solidus's default route.
_PREFIX_CANDIDATES = ("/products/", "/p/", "")

_HITS_PER_PAGE = 250
_REQUEST_TIMEOUT_S = 30


# ── creds extraction ─────────────────────────────────────────────────────────


def _props_tuple_values(props_raw: str) -> dict:
    """astro-island ``props`` → a flat {name: leaf_value} dict.

    Astro serializes each prop as ``[type_code, value]`` tuples (strings are
    ``[0,"..."]``); the attribute is HTML-escaped. Anything that doesn't parse
    returns {}.
    """
    try:
        parsed = _json.loads(_html.unescape(props_raw))
    except Exception:
        return {}
    flat: dict = {}
    if not isinstance(parsed, dict):
        return flat
    for name, spec in parsed.items():
        if isinstance(spec, list) and len(spec) == 2 and isinstance(spec[1], str):
            flat[name] = spec[1]
        elif isinstance(spec, str):
            flat[name] = spec
    return flat


# Credential-bearing keys, matched case-insensitively (island 7 of the live
# wonderbly page uses ``appID``, not ``appId``) and at ANY nesting depth (the
# ``algoliaIndex`` rides inside a regions array, not a top-level prop).
_CRED_KEY_MAP = {
    "appid": "app_id",
    "application_id": "app_id",
    "algoliaapplicationid": "app_id",
    "apikey": "api_key",
    "algoliaapikey": "api_key",
    "searchkey": "api_key",
    "algoliaindex": "index_name",
    "indexname": "index_name",
    "index_name": "index_name",
}


def _deep_collect(obj, merged: dict) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            norm = _CRED_KEY_MAP.get(str(key).lower())
            if norm and not merged.get(norm):
                # Astro tuples: the prop value is "[0,"the string"]"
                leaf = value
                if (
                    isinstance(leaf, list)
                    and len(leaf) == 2
                    and isinstance(leaf[1], str)
                ):
                    leaf = leaf[1]
                if isinstance(leaf, str) and leaf:
                    merged[norm] = leaf
            _deep_collect(value, merged)
    elif isinstance(obj, list):
        for item in obj:
            _deep_collect(item, merged)


def _creds_from_values(values: dict) -> dict | None:
    app_id = values.get("app_id") or ""
    api_key = values.get("api_key") or ""
    if not (app_id and api_key):
        return None
    return {
        "app_id": str(app_id).strip(),
        "api_key": str(api_key).strip(),
        "index_name": str(values.get("index_name") or "").strip(),
    }


def extract_algolia_creds(html: str) -> dict | None:
    """``{app_id, api_key, index_name}`` from a listing page's HTML, or None.

    Handles the two shapes client-side Algolia integrations ship:
    (a) astro-island ``props`` attributes (wonderbly — HTML-escaped JSON
    tuples; appId/apiKey/algoliaIndex may ride DIFFERENT islands at ANY
    nesting depth, so values are merged across every island), and
    (b) inline scripts — ``algoliasearch('APP','KEY')`` init calls plus an
    ``algoliaIndex``/``indexName`` config key.
    ``index_name`` may be "" when the page splits config from credentials;
    discovery can still proceed if the caller supplies it.
    """
    if not html:
        return None
    # (a) astro-island props attributes — merged, complete set wins early
    merged: dict = {}
    for m in _re.finditer(r'props="([^"]+)"', html):
        try:
            parsed = _json.loads(_html.unescape(m.group(1)))
        except Exception:
            continue
        _deep_collect(parsed, merged)
        creds = _creds_from_values(merged)
        if creds and creds.get("index_name"):
            return creds
    creds = _creds_from_values(merged)
    if creds:
        return creds
    # (b) inline algoliasearch(...) init + index name anywhere in the page
    init = _re.search(
        r"""algoliasearch\(\s*['"]([A-Za-z0-9]{4,14})['"]\s*,\s*"""
        r"""['"]([A-Za-z0-9_-]{12,})['"]\s*\)""",
        html,
    )
    if init:
        idx = _re.search(
            r'"(?:algoliaIndex|indexName|index_name)"\s*:\s*"([^"]+)"', html
        )
        return {
            "app_id": init.group(1),
            "api_key": init.group(2),
            "index_name": idx.group(1) if idx else "",
        }
    return None


def extract_taxon_query(html: str) -> str:
    """The listing's taxon path (wonderbly island 7: ``taxonQuery``), "" if
    absent — feeds ``build_taxon_filter`` so discovery mirrors what the page
    shows instead of scraping the whole index."""
    if not html:
        return ""
    for m in _re.finditer(r'props="([^"]+)"', html):
        try:
            parsed = _json.loads(_html.unescape(m.group(1)))
        except Exception:
            continue
        found: dict = {}

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() == "taxonquery":
                        leaf = value
                        if (
                            isinstance(leaf, list)
                            and len(leaf) == 2
                            and isinstance(leaf[1], str)
                        ):
                            leaf = leaf[1]
                        if isinstance(leaf, str) and leaf:
                            found.setdefault("taxonquery", leaf)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(parsed)
        if found.get("taxonquery"):
            return found["taxonquery"]
    flat = _re.search(r'"taxonQuery"\s*:\s*"([^"]+)"', html)
    return flat.group(1) if flat else ""


# ── hit → URL conversion ─────────────────────────────────────────────────────


def _clean(value) -> str:
    return str(value).strip() if isinstance(value, (str, int)) else ""


def _hit_to_url(hit: dict) -> tuple[str, str] | None:
    """Classify one Algolia hit → (kind, value).

    kind is ``url`` (absolute — use as-is), ``path`` (join to base_url), or
    ``slug`` (needs the site's PDP prefix). None when the hit carries nothing
    addressable.
    """
    if not isinstance(hit, dict):
        return None
    for key in _ABSOLUTE_URL_KEYS:
        val = _clean(hit.get(key))
        if val.startswith(("http://", "https://")):
            return ("url", val)
    for key in _PATH_KEYS:
        val = _clean(hit.get(key))
        if val.startswith("/") and len(val) > 1:
            return ("path", val)
    for key in _SLUG_KEYS:
        val = _clean(hit.get(key))
        if val and " " not in val and "/" not in val:
            return ("slug", val)
    return None


# ── PDP prefix resolution ────────────────────────────────────────────────────


def _locs_from_sitemap(body: str) -> list[str]:
    return _re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body or "")


def _resolve_prefix_from_sitemap(base_url, slug, sitemap_fn) -> str:
    """First sitemap loc containing ``/slug`` → its directory path, or ""."""
    try:
        body = sitemap_fn(_urljoin(base_url, "/sitemap.xml")) or ""
    except Exception:
        return ""
    locs = _locs_from_sitemap(body)
    marker = f"/{slug}"
    for loc in locs:
        if marker in loc:
            path = _urlparse(loc).path
            return path.rsplit("/", 1)[0] + "/"
    # sitemap index: scan a few child sitemaps before giving up
    children = [loc for loc in locs if loc.endswith(".xml")][:3]
    for child in children:
        try:
            body = sitemap_fn(child) or ""
        except Exception:
            continue
        for loc in _locs_from_sitemap(body):
            if marker in loc:
                path = _urlparse(loc).path
                return path.rsplit("/", 1)[0] + "/"
    return ""


class _PrefixResolver:
    """Resolves and caches ONE PDP path prefix per run.

    Order: cached winner → HEAD-verified candidates → sitemap grep. The first
    slug pays the probing cost; every later slug reuses the answer.
    """

    def __init__(self, base_url, head_fn, sitemap_fn):
        self.base_url = base_url
        self.head_fn = head_fn
        self.sitemap_fn = sitemap_fn
        self.prefix: str | None = None
        # A prefix proven by HEAD needs no further confirmation; a sitemap
        # prefix is trusted-but-verified on the first URL it produces.
        self.prefix_head_verified = False
        self.verified: set[str] = set()

    def url_for(self, slug: str) -> str:
        if self.prefix is not None:
            return _urljoin(self.base_url, self.prefix + slug)
        prefix = ""
        if self.head_fn is not None:
            for cand in _PREFIX_CANDIDATES:
                try:
                    if self.head_fn(_urljoin(self.base_url, cand + slug)) == 200:
                        prefix = cand
                        self.prefix_head_verified = True
                        break
                except Exception:
                    continue
        if not prefix and self.sitemap_fn is not None:
            prefix = _resolve_prefix_from_sitemap(self.base_url, slug, self.sitemap_fn)
        if prefix:
            self.prefix = prefix
            return _urljoin(self.base_url, prefix + slug)
        return ""

    def check(self, url: str) -> bool:
        """One cheap verification of a derived URL (first slug of the run)."""
        if url in self.verified:
            return True
        ok = True
        if self.head_fn is not None:
            try:
                ok = self.head_fn(url) == 200
            except Exception:
                ok = False
        if ok:
            self.verified.add(url)
        return ok


# ── index queries ────────────────────────────────────────────────────────────


def _default_fetch(url: str, body: dict, headers: dict) -> dict | None:
    try:
        import requests

        resp = requests.post(
            url, data=_json.dumps(body), headers=headers, timeout=_REQUEST_TIMEOUT_S
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(
            "algolia_discovery: query status %s: %s", resp.status_code, resp.text[:200]
        )
    except Exception as exc:
        logger.warning("algolia_discovery: query failed: %s", exc)
    return None


def _default_head(url: str) -> int:
    try:
        import requests

        return requests.get(url, timeout=15, allow_redirects=True).status_code
    except Exception:
        return 0


def _default_sitemap(url: str) -> str:
    try:
        import requests

        return requests.get(url, timeout=15).text
    except Exception:
        return ""


def _endpoint(app_id: str) -> str:
    return f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"


def _query(creds: dict, params: str, fetch_fn) -> dict | None:
    body = {
        "requests": [{"indexName": creds.get("index_name") or "", "params": params}]
    }
    headers = {
        "Content-Type": "application/json",
        "X-Algolia-Application-Id": creds["app_id"],
        "X-Algolia-API-Key": creds["api_key"],
    }
    data = fetch_fn(_endpoint(creds["app_id"]), body, headers)
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    return first if isinstance(first, dict) else None


def build_taxon_filter(creds: dict, taxon_query: str, fetch_fn=None) -> str:
    """``taxon_path:"Kids > Age > 0-3"`` — the facet filter matching the
    listing's taxon, or "" (unfiltered is correct for firstn scope).

    Probes the index's facet set (hitsPerPage=0) and prefers the known taxon
    attribute names before accepting any facet whose values contain the path.
    """
    if not (creds and taxon_query):
        return ""
    fetch = fetch_fn or _default_fetch
    probe = _query(
        creds, "hitsPerPage=0&page=0&query=&facets=*&maxValuesPerFacet=1000", fetch
    )
    facets = (probe or {}).get("facets")
    if not isinstance(facets, dict):
        return ""
    wanted = _re.sub(r"\s+", " ", str(taxon_query)).strip().lower()
    # taxon_path-style attributes first, then the Algolia hierarchical
    # ladder (wonderbly's index has no taxon_path — the taxon rides
    # categories.lvl2, " > "-separated, e.g. "Kids > Age > 0-3": 86 hits).
    for attr in (
        "taxon_path",
        "category_path",
        "taxonomies",
        "categories",
        "taxon",
        "categories.lvl0",
        "categories.lvl1",
        "categories.lvl2",
        "categories.lvl3",
    ):
        values = facets.get(attr)
        if not isinstance(values, dict):
            continue
        for value in values:
            if _re.sub(r"\s+", " ", str(value)).strip().lower() == wanted:
                return f'{attr}:"{value}"'
    return ""


def discover_catalog_urls(
    base_url: str,
    creds: dict,
    max_items: int = 20,
    query: str = "",
    filter_str: str = "",
    hits_per_page: int = _HITS_PER_PAGE,
    max_pages: int = 8,
    delay_s: float = 0.4,
    fetch_fn=None,
    head_fn=None,
    sitemap_fn=None,
) -> dict:
    """Walk the index and return product URLs.

    Returns ``{"urls": [...], "nbHits": int, "collected": int,
    "stop_reason": str, "pdp_prefix": str}``. ``urls`` is deduped, in index
    order, capped at ``max_items``. Any transport/shape surprise degrades to a
    partial (or empty) result — never raises.
    """
    fetch = fetch_fn or _default_fetch
    heads = head_fn or _default_head
    sitemaps = sitemap_fn or _default_sitemap
    resolver = _PrefixResolver(base_url, heads, sitemaps)
    out: dict = {
        "urls": [],
        "nbHits": 0,
        "collected": 0,
        "stop_reason": "",
        "pdp_prefix": "",
    }
    if not (creds and creds.get("app_id") and creds.get("api_key")):
        out["stop_reason"] = "no_creds"
        return out
    try:
        base_url = (base_url or "").rstrip("/")
    except Exception:
        base_url = ""
    if not base_url:
        out["stop_reason"] = "no_base_url"
        return out

    seen: set[str] = set()
    collected = 0
    stop_reason = ""
    for page in range(max_pages):
        params = (
            f"hitsPerPage={int(hits_per_page)}&page={page}&query={_quote(query or '')}"
        )
        if filter_str:
            params += f"&filters={_quote(filter_str)}"
        result = _query(creds, params, fetch)
        if result is None:
            stop_reason = stop_reason or "query_failed"
            break
        out["nbHits"] = int(result.get("nbHits") or 0)
        hits = result.get("hits")
        if not isinstance(hits, list) or not hits:
            stop_reason = stop_reason or "empty_page"
            break
        for hit in hits:
            classified = _hit_to_url(hit)
            if classified is None:
                continue
            kind, val = classified
            if kind == "url":
                url = val
            elif kind == "path":
                url = _urljoin(base_url, val)
            else:
                url = resolver.url_for(val)
            if not url or url in seen:
                continue
            if (
                kind == "slug"
                and not resolver.prefix_head_verified
                and not resolver.verified
                and resolver.check(url)
            ):
                logger.info("algolia_discovery: PDP prefix verified via %s", url)
            seen.add(url)
            out["urls"].append(url)
            collected += 1
            if collected >= max_items:
                break
        if collected >= max_items:
            stop_reason = "max_items"
            break
        if len(hits) < hits_per_page or collected >= out["nbHits"] > 0:
            stop_reason = "exhausted"
            break
        if delay_s:
            _time.sleep(delay_s)
    out["collected"] = collected
    out["pdp_prefix"] = resolver.prefix or ""
    out["stop_reason"] = stop_reason or ("max_pages" if collected else "no_urls")
    if not collected:
        out["urls"] = []
    return out
