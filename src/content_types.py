"""Content type registry for multi-content-type scraper support.

Centralizes per-content-type configuration: field definitions, JSON-LD types,
output schemas, extraction hints, and template families. Used by agents, nodes,
and templates to adapt behavior based on the content being scraped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class FieldDef:
    name: str
    label: str
    field_type: str  # text, number, datetime, list, url
    required: bool = False
    jsonld_key: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ContentTypeConfig:
    name: str
    label: str
    site_type: str
    output_key: str
    template_family: str
    jsonld_types: tuple[str, ...]
    core_field_names: tuple[str, ...]
    optional_field_names: tuple[str, ...]
    direct_field_names: tuple[str, ...]
    all_fields: tuple[FieldDef, ...] = field(default_factory=tuple)
    extraction_hints: str = ""
    input_modes: tuple[str, ...] = ("url_list",)

    @property
    def core_fields(self) -> tuple[str, ...]:
        return self.core_field_names

    @property
    def output_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "output_key": self.output_key,
            "content_type": self.name,
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.field_type,
                    "required": f.required,
                }
                for f in self.all_fields
            ],
        }
        return schema

    def mapping_prompt_fields(self) -> str:
        lines = []
        for f in self.all_fields:
            req = " (required)" if f.required else ""
            notes = f" — {f.notes}" if f.notes else ""
            lines.append(f"- {f.name}: {f.label}{req}{notes}")
        return "\n".join(lines)

    def to_agent_context(self) -> str:
        parts = [
            f"Content type: {self.label}",
            f"Output key: \"{self.output_key}\"",
            f"Core fields: {', '.join(self.core_field_names)}",
        ]
        if self.jsonld_types:
            parts.append(f"JSON-LD types to look for: {', '.join(self.jsonld_types)}")
        if self.extraction_hints:
            parts.append(f"Extraction hints: {self.extraction_hints}")
        return "\n".join(parts)


DIRECT_FIELDS = (
    FieldDef("url", "Page URL", "url", jsonld_key="url"),
    FieldDef("src_url", "Source URL", "url"),
    FieldDef("status_code", "HTTP Status Code", "number"),
    FieldDef("scraped_at", "Timestamp", "datetime"),
    FieldDef("remarks", "Remarks", "text"),
)

PRODUCT_FIELDS = (
    FieldDef("title", "Title", "text", required=True, jsonld_key="name"),
    FieldDef("price", "Price", "text", required=True, jsonld_key="offers.price"),
    FieldDef("availability", "Availability", "text", jsonld_key="offers.availability",
             notes="Normalize to 'in_stock' / 'out_of_stock' (template "
                   "normalizers emit these exact tokens)"),
    FieldDef("original_price", "Original Price", "text", jsonld_key="offers.highPrice",
             notes="Only map if a separate was/compare-at price exists"),
    FieldDef("currency", "Currency", "text", jsonld_key="offers.priceCurrency",
             notes="ISO 4217 code"),
    FieldDef("description", "Description", "text", jsonld_key="description"),
    FieldDef("brand", "Brand", "text", jsonld_key="brand.name"),
    FieldDef("images", "Images", "list", jsonld_key="image"),
    FieldDef("sku", "SKU", "text", jsonld_key="sku"),
    FieldDef("category", "Category", "text", jsonld_key="category"),
)

ARTICLE_FIELDS = (
    FieldDef("title", "Title", "text", required=True, jsonld_key="headline"),
    FieldDef("author", "Author", "text", jsonld_key="author.name"),
    FieldDef("publish_date", "Publish Date", "datetime", jsonld_key="datePublished"),
    FieldDef("content", "Content", "text", jsonld_key="articleBody"),
    FieldDef("images", "Images", "list", jsonld_key="image"),
    FieldDef("tags", "Tags", "list", jsonld_key="keywords"),
    FieldDef("category", "Category", "text", jsonld_key="articleSection"),
)

JOB_FIELDS = (
    FieldDef("title", "Title", "text", required=True, jsonld_key="title"),
    FieldDef("company", "Company", "text", jsonld_key="hiringOrganization.name"),
    FieldDef("location", "Location", "text", jsonld_key="jobLocation.address.addressLocality"),
    FieldDef("salary", "Salary", "text", jsonld_key="baseSalary.value.value"),
    FieldDef("description", "Description", "text", jsonld_key="description"),
    FieldDef("requirements", "Requirements", "text", jsonld_key="qualifications"),
    FieldDef("job_type", "Job Type", "text", jsonld_key="employmentType",
             notes="full-time, part-time, contract, etc."),
    FieldDef("apply_url", "Apply URL", "url", jsonld_key="url"),
    FieldDef("posted_date", "Posted Date", "datetime", jsonld_key="datePosted",
             notes="Date when job was posted, for filtering by age"),
)

FORUM_FIELDS = (
    FieldDef("title", "Title", "text", required=True, jsonld_key="headline"),
    FieldDef("author", "Author", "text", jsonld_key="author.name"),
    FieldDef("posts", "Posts", "list", jsonld_key="text",
             notes="Each post: author, content, timestamp"),
    FieldDef("views", "Views", "number"),
    FieldDef("replies", "Replies", "number"),
    FieldDef("last_activity", "Last Activity", "datetime"),
)

SERP_FIELDS = (
    FieldDef("rank", "Rank", "number", required=True),
    FieldDef("url", "URL", "url", required=True),
    FieldDef("title", "Title", "text", required=True),
    FieldDef("snippet", "Snippet", "text"),
)

PAGE_CONTENT_FIELDS = (
    FieldDef("title", "Title", "text", required=True),
    FieldDef("content", "Content", "text"),
    FieldDef("images", "Images", "list"),
    FieldDef("metadata", "Metadata", "text", notes="All meta tags"),
)

CONTENT_TYPES: dict[str, ContentTypeConfig] = {
    "product": ContentTypeConfig(
        name="product",
        label="Product",
        site_type="shopping",
        output_key="products",
        template_family="product",
        jsonld_types=("Product", "Offer", "AggregateOffer"),
        core_field_names=("title", "price", "availability", "currency", "url", "src_url"),
        optional_field_names=("original_price", "description", "brand", "images", "sku", "category"),
        direct_field_names=tuple(f.name for f in DIRECT_FIELDS),
        all_fields=PRODUCT_FIELDS + DIRECT_FIELDS,
        extraction_hints=(
            "Look for price elements, add-to-cart buttons, product galleries, "
            "SKU/model numbers, stock indicators."
        ),
        input_modes=("url_list", "list_page", "navigation"),
    ),
    "article": ContentTypeConfig(
        name="article",
        label="Article",
        site_type="articles",
        output_key="articles",
        template_family="article",
        jsonld_types=("Article", "NewsArticle", "BlogPosting", "TechArticle"),
        core_field_names=("title", "author", "publish_date", "content", "url"),
        optional_field_names=("images", "tags", "category"),
        direct_field_names=tuple(f.name for f in DIRECT_FIELDS),
        all_fields=ARTICLE_FIELDS + DIRECT_FIELDS,
        extraction_hints=(
            "Look for article body (articleBody, main content area), "
            "byline/author element, publication date, tags/categories."
        ),
        input_modes=("url_list", "list_page", "navigation"),
    ),
    "job_posting": ContentTypeConfig(
        name="job_posting",
        label="Job Posting",
        site_type="jobs",
        output_key="jobs",
        template_family="job",
        jsonld_types=("JobPosting",),
        core_field_names=("title", "company", "location", "description", "url"),
        optional_field_names=("salary", "requirements", "job_type", "apply_url", "posted_date"),
        direct_field_names=tuple(f.name for f in DIRECT_FIELDS),
        all_fields=JOB_FIELDS + DIRECT_FIELDS,
        extraction_hints=(
            "Look for job title, company name, location, salary range, "
            "requirements list, apply button/link. Posted date is important "
            "for filtering jobs by age (e.g., last 7 days)."
        ),
        input_modes=("url_list", "navigation"),
    ),
    "forum_thread": ContentTypeConfig(
        name="forum_thread",
        label="Forum Thread",
        site_type="forum",
        output_key="threads",
        template_family="forum",
        jsonld_types=("DiscussionForumPosting", "Question"),
        core_field_names=("title", "author", "posts", "url"),
        optional_field_names=("views", "replies", "last_activity"),
        direct_field_names=tuple(f.name for f in DIRECT_FIELDS),
        all_fields=FORUM_FIELDS + DIRECT_FIELDS,
        extraction_hints=(
            "Look for thread title, post containers (author + content + timestamp), "
            "reply counts, user avatars/names."
        ),
        input_modes=("url_list",),
    ),
    "serp": ContentTypeConfig(
        name="serp",
        label="SERP",
        site_type="general",
        output_key="results",
        template_family="serp",
        jsonld_types=(),
        core_field_names=("rank", "url", "title", "snippet"),
        optional_field_names=(),
        direct_field_names=("status_code", "scraped_at", "remarks"),
        all_fields=SERP_FIELDS + tuple(f for f in DIRECT_FIELDS if f.name in ("status_code", "scraped_at", "remarks")),
        extraction_hints=(
            "Extract search result entries: rank position, URL, title, snippet text. "
            "Handle pagination of search results."
        ),
        input_modes=("search_term",),
    ),
    "page_content": ContentTypeConfig(
        name="page_content",
        label="Page Content",
        site_type="general",
        output_key="pages",
        template_family="generic",
        jsonld_types=("WebPage",),
        core_field_names=("title", "content", "url"),
        optional_field_names=("images", "metadata"),
        direct_field_names=tuple(f.name for f in DIRECT_FIELDS),
        all_fields=PAGE_CONTENT_FIELDS + DIRECT_FIELDS,
        extraction_hints=(
            "Extract page title and main content text. "
            "Generic extraction — capture all meaningful visible content."
        ),
        input_modes=("url_list",),
    ),
}

PAGE_TYPE_MAP: dict[str, tuple[str, str]] = {
    "product": ("product", "url_list"),
    "product_list": ("product", "list_page"),
    "product_navigation": ("product", "navigation"),
    "article": ("article", "url_list"),
    "article_list": ("article", "list_page"),
    "article_navigation": ("article", "navigation"),
    "job_posting": ("job_posting", "url_list"),
    "job_navigation": ("job_posting", "navigation"),
    "forum_thread": ("forum_thread", "url_list"),
    "serp": ("serp", "search_term"),
    "page_content": ("page_content", "url_list"),
}

SITE_TYPE_CHOICES = [
    ("shopping", "Shopping"),
    ("articles", "Articles"),
    ("jobs", "Jobs"),
    ("forum", "Forum"),
    ("general", "General"),
]

INPUT_MODE_CHOICES = [
    ("url_list", "URL List"),
    ("list_page", "List Page"),
    ("navigation", "Navigation"),
    ("search_term", "Search Term"),
]


def get_content_type(page_type: str) -> Optional[ContentTypeConfig]:
    content_type_name, _ = PAGE_TYPE_MAP.get(page_type, (page_type, "url_list"))
    return CONTENT_TYPES.get(content_type_name)


def get_output_key_label(page_type: str) -> tuple[str, str]:
    """Derive (output_key, singular_label) from a job's page_type.

    output_key is the JSON key the scraper writes ("products", "jobs", ...);
    singular_label is for display ("product", "job", ...) with Django's
    pluralize filter: ``{{ count }} {{ label }}{{ count|pluralize }}``.
    Falls back to ("products", "product") for unknown/legacy page_types.
    """
    cfg = get_content_type(page_type)
    if cfg:
        # All 6 registered output_keys are simple English plurals.
        return (cfg.output_key, cfg.output_key.rstrip("s"))
    return ("products", "product")


# Standard bookkeeping fields always retained on every record (needed for the
# output to be traceable), regardless of the user's requested schema.
BOOKKEEPING_FIELDS: tuple[str, ...] = ("url", "src_url", "scraped_at", "status_code")


def schema_field_names(
    target_fields, output_schema: dict | None = None
) -> list[str]:
    """The user's requested schema field names (no bookkeeping).

    Priority: explicit ``target_fields`` (the intake UI chips) → the DB
    ``output_schema["fields"]`` names → ``[]`` (no schema → extract everything).
    """
    if target_fields:
        return [str(f) for f in target_fields if f]
    if output_schema and isinstance(output_schema, dict):
        fields = output_schema.get("fields")
        if isinstance(fields, list) and fields:
            names = [
                f.get("name")
                for f in fields
                if isinstance(f, dict) and f.get("name")
            ]
            if names:
                return [str(n) for n in names]
    return []


def resolve_allowed_fields(
    target_fields, output_schema: dict | None = None
) -> set[str] | None:
    """Set of field names a job's output records may contain (the enforced schema).

    ``schema_field_names(...)`` ∪ ``BOOKKEEPING_FIELDS``, or ``None`` when no
    explicit schema exists (→ caller skips pruning; today's "extract everything"
    behavior is preserved for schema-less jobs).
    """
    names = schema_field_names(target_fields, output_schema)
    if not names:
        return None
    return set(names) | set(BOOKKEEPING_FIELDS)


def _prune_value(value, node):
    """Prune ``value`` to the shape described by a nested-schema ``node``.

    ``node`` is ``{type, children}`` (from ``schema_validation.parse_nested_schema``).
    - node with non-empty ``children``:
        * dict value  → keep only keys in children, recurse each
        * list value  → apply the SAME node to each element (array-of-objects)
        * scalar/etc  → return unchanged (type mismatch: never crash, never null-fill)
    - leaf/missing/malformed node → return value unchanged (opaque blob).
    Builds fresh dicts/lists; never mutates input.
    """
    children = (node or {}).get("children") if isinstance(node, dict) else None
    if not children:
        return value  # leaf / unconstrained → opaque blob
    if isinstance(value, dict):
        return {k: _prune_value(v, children[k]) for k, v in value.items() if k in children}
    if isinstance(value, list):
        return [_prune_value(elem, node) for elem in value]  # array: each element gets the item shape
    return value  # schema says nested but value is scalar → keep verbatim


def prune_record_to_schema(record, allowed, schema_nested=None, bookkeeping=BOOKKEEPING_FIELDS):
    """Prune one output record. ``allowed`` is the top-level authority (the flat
    ``target_fields`` ∪ bookkeeping set from ``resolve_allowed_fields`` — this is
    the C4 fix: target_fields drives top-level admission, so chip edits always
    win over a stale schema_text). ``schema_nested`` (optional tree from
    ``schema_validation.parse_nested_schema``) supplies the recursive shape —
    inner-prune only for keys present in BOTH ``allowed`` and the tree.

    ``allowed`` falsy/None → no prune (extract-everything / schema-less jobs).
    Record is NEVER dropped — only keys/branches filtered. No null-fill.
    """
    if not isinstance(record, dict) or not allowed:
        return record
    keep_top = set(allowed) | set(bookkeeping)
    out = {}
    for k, v in record.items():
        if k not in keep_top:
            continue
        node = schema_nested.get(k) if schema_nested else None
        out[k] = _prune_value(v, node) if node is not None else v
    return out


def count_items_in_output(data: dict) -> int:
    """Count items in an output JSON, trying all known output keys.

    Handles content-type mismatches (e.g. page_type='product' but the file
    actually contains 'jobs'). Returns the max count across known keys.
    """
    if not isinstance(data, dict):
        return 0
    best = 0
    for key in ("products", "jobs", "articles", "threads", "results", "pages"):
        val = data.get(key)
        if isinstance(val, list) and len(val) > best:
            best = len(val)
    return best


def assess_query_match(query: str, items: list, title_field: str = "title") -> dict:
    """Assess how well extracted items match a search query (P0-12).

    Returns a report dict (NOT a filter — items are not dropped):
    {"query": query, "total": N, "matched": N, "off_target": N, "ambiguous": N,
     "off_target_sample": [...], "match_method": "title_whole_word"}

    Conservative: flags only CLEAR misses (title doesn't contain the query as a
    whole word/phrase). "Physician Associate" is off-target for "physician"
    (different role noun). Works for any site/content-type/query. Transparency
    over filtering — auto-dropping by title keywords is fragile (titles vary).
    """
    if not query or not items:
        return {}
    import re

    q = query.lower().strip()
    matched = off_target = ambiguous = 0
    samples = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = (item.get(title_field) or "").lower()
        # Whole-word/phrase match (not substring — avoids "physician" matching "physician associate").
        if re.search(r"\b" + re.escape(q) + r"\b", title):
            # Check it's not part of a longer role noun (e.g. "physician associate").
            # If the word right after the query is another noun word, it's ambiguous.
            after_match = re.search(r"\b" + re.escape(q) + r"\s+(\w+)", title)
            if after_match:
                next_word = after_match.group(1)
                role_words = {"associate", "assistant", "aide", "tech", "technician",
                              "nurse", "practitioner", "intern", "resident", "fellow"}
                if next_word in role_words:
                    ambiguous += 1
                    continue
            matched += 1
        elif q in title:
            ambiguous += 1  # substring but not whole word — uncertain
        else:
            off_target += 1
            if len(samples) < 5:
                samples.append({"title": item.get(title_field, "")[:80], "url": item.get("url", "")[:120]})
    total = matched + off_target + ambiguous
    return {
        "query": query,
        "total": total,
        "matched": matched,
        "off_target": off_target,
        "ambiguous": ambiguous,
        "off_target_sample": samples,
        "match_method": "title_whole_word_conservative",
    }


# Fields present on every item regardless of content type — not useful as a
# "is this a real item" signal, so excluded from output-filter fields.
_ALWAYS_PRESENT_FIELDS = {"title", "url", "src_url", "scraped_at", "status_code", "remarks", "id"}


def has_substantive_field(item: dict) -> bool:
    """True if the item has ≥1 substantive (non-bookkeeping) field with a value.

    Content-type-AGNOSTIC success predicate. An item is 'real' if it carries ANY
    data beyond the bookkeeping fields (url/src_url/scraped_at/status_code/remarks/id/title).
    This rescues non-product content types (people directories like lw.com, job
    boards, article archives) whose identifier field is 'Name'/'company'/'author'
    — NOT 'title'. The pipeline's ~7 hardcoded title/price success gates silently
    discard their extractions, scoring a 20/20 perfect extraction as 0/FAIL.
    This predicate replaces those gates so success is recognized regardless of
    the content type.
    """
    if not isinstance(item, dict):
        return False
    return any(k not in _ALWAYS_PRESENT_FIELDS and v for k, v in item.items())


def output_filter_fields(content_type: str) -> list[str]:
    """Fields whose presence indicates a real item of this content type — used by
    output filters to drop nav/category pages and extraction failures (e.g. a
    product detail page has a price; a job has a company/location; a category page
    has neither). Derived from the content type's core fields minus the
    always-present ones, so it stays in sync with the registry and is generic
    (no per-content-type hardcoding at call sites). Returns [] for unknown types
    (callers then keep every item with a title).
    """
    cfg = CONTENT_TYPES.get(content_type)
    if not cfg:
        return []
    return [f for f in cfg.core_field_names if f not in _ALWAYS_PRESENT_FIELDS]


def get_content_type_for_site_type(site_type: str) -> Optional[ContentTypeConfig]:
    for config in CONTENT_TYPES.values():
        if config.site_type == site_type:
            return config
    return None


def resolve_page_type(page_type: str) -> tuple[str, str]:
    """Return (content_type_name, input_mode) for a given page_type."""
    return PAGE_TYPE_MAP.get(page_type, (page_type, "url_list"))


def all_page_type_choices() -> list[tuple[str, str]]:
    """Return (page_type, label) pairs grouped for form dropdowns."""
    groups = {
        "Shopping": [("product", "Product"), ("product_list", "Product List"),
                     ("product_navigation", "Product Navigation")],
        "Articles": [("article", "Article"), ("article_list", "Article List"),
                     ("article_navigation", "Article Navigation")],
        "Jobs": [("job_posting", "Job Posting"), ("job_navigation", "Job Navigation")],
        "Forum": [("forum_thread", "Forum Thread")],
        "Search": [("serp", "SERP")],
        "Generic": [("page_content", "Page Content")],
    }
    choices = []
    for group_label, items in groups.items():
        for value, label in items:
            choices.append((value, f"{group_label}: {label}"))
    return choices
