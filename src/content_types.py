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
             notes="Normalize to 'In Stock' / 'Out of Stock'"),
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
        optional_field_names=("salary", "requirements", "job_type", "apply_url"),
        direct_field_names=tuple(f.name for f in DIRECT_FIELDS),
        all_fields=JOB_FIELDS + DIRECT_FIELDS,
        extraction_hints=(
            "Look for job title, company name, location, salary range, "
            "requirements list, apply button/link."
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
