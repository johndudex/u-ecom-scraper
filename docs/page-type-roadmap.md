# Page Type & Multi-Content-Type Roadmap

Expands the scraper builder from product-only to support 11 page types across 6 content
domains (shopping, articles, jobs, forums, SERP, generic).

## Status

| Phase | Status | Start |
|-------|--------|-------|
| Phase 1: Content Type Generalization | **Complete** | 2026-06-14 |
| Phase 2: Navigation Analysis Agent | **Complete** | 2026-06-17 |
| Phase 3: SERP Support | Not started | — |

---

## Page Types & Content Domains

### 11 User-Facing Page Types

| # | Page Type | Content Domain | Input Mode | Output Key |
|---|-----------|---------------|------------|------------|
| 1 | Product | Shopping | URL list/file | `products` |
| 2 | Product List | Shopping | List page URL | `products` |
| 3 | Product Navigation | Shopping | Navigation criteria | `products` |
| 4 | Article | Articles | URL list/file | `articles` |
| 5 | Article List | Articles | List page URL | `articles` |
| 6 | Article Navigation | Articles | Navigation criteria | `articles` |
| 7 | Job Posting | Jobs | URL list/file | `jobs` |
| 8 | Job Navigation | Jobs | Navigation criteria | `jobs` |
| 9 | Forum Thread | Forum | URL list/file | `threads` |
| 10 | SERP | General | Search term | `results` |
| 11 | Page Content | General | URL list/file | `pages` |

### Content Type Registry (`src/content_types.py`)

Central registry driving all content-type-specific behavior. Each content type defines:
- Core/optional fields (for coverage validation and normalization)
- JSON-LD schema.org types to look for
- Output schema (key name + field definitions)
- Template family hint
- Extraction hints for agent prompts
- Applicable input modes

Default schemas are based on schema.org standards. Users can customize the output
schema per Site (stored as JSONField).

### 6 Content Domains

| Domain | Content Type | Core Fields | JSON-LD Types |
|--------|-------------|------------|--------------|
| Shopping | `product` | title, price, availability, original_price, currency, url, src_url | Product, Offer, AggregateOffer |
| Articles | `article` | title, author, publish_date, content, url | Article, NewsArticle, BlogPosting |
| Jobs | `job_posting` | title, company, location, description, url | JobPosting |
| Forum | `forum_thread` | title, author, posts, url | DiscussionForumPosting |
| SERP | `serp` | rank, url, title, snippet | — |
| Generic | `page_content` | title, content, url | WebPage |

Universal direct fields (set by scraper, all types): `status_code`, `scraped_at`, `remarks`

---

## Architecture

### Graph Design — 3 Functional Paths, 1 Graph

```
START → parse_command → check_tracker → setup_workspace → check_accessibility
                                                                    │
                                                    ┌───────────────┼───────────────┐
                                                    │               │               │
                                               url_list      navigation       search_term
                                                    │               │             (Phase 3)
                                                    │     ┌─────────▼───────┐
                                                    │     │navigation_agent │
                                                    │     │(analyze nav     │
                                                    │     │ patterns only)  │
                                                    │     └─────────┬───────┘
                                                    │               │
                                                    │       navigation_    │
                                                    │       analysis.json   │
                                                    │               │
                                                    └───────────────┼─────────────┘
                                                                    │
                                                    ┌───────────────▼───────────────┐
                                                    │  site_analyzer → content_     │
                                                    │  analyzer → scraper_analyzer  │
                                                    │  → code_writer → code_tester  │
                                                    │  → cleanup → skill_learner    │
                                                    └───────────────────────────────┘
```

**Navigation agent produces analysis, NOT URLs.** The code_writer builds a self-contained
two-phase scraper that navigates/searches/paginates at runtime, then scrapes each item.

### Homepage → Site → ScrapeJob Flow

```
User fills homepage form (page_type, url, search_criteria, etc.)
        │
        ▼
home() view → creates ScrapeJob (page_type, input_mode, search_criteria)
        │
        ▼
Celery run_scrape_task → _build_initial_state(job) → injects page_type into ScrapeState
        │
        ▼
check_tracker node:
  - Site exists? → load site_type, output_schema from Site
  - Site not found? → auto-create Site with site_type from job's content type
        │
        ▼
Pipeline runs (content-type-aware agents)
        │
        ▼
Cleanup agent → updates Site (status, product_count, fields_extracted, etc.)
```

**Key rule**: A Site is always created/updated when a scrape job runs. The homepage
creates a ScrapeJob directly; check_tracker ensures a matching Site entry exists and
carries the site_type/output_schema forward.

### Site Model Changes

```python
class Site(models.Model):
    # Existing fields (unchanged)
    url, name, slug, sample_url, input_urls, currency
    platform, scraping_method, status
    product_count, fields_extracted, has_scraper, default_scraper_path
    last_scraped_at, created_at, updated_at

    # NEW fields
    site_type = models.CharField(
        max_length=20,
        choices=SITE_TYPE_CHOICES,
        default="shopping",
    )
    output_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text="Custom output schema. Empty = use content type default.",
    )
```

Site types: `shopping`, `articles`, `jobs`, `forum`, `general`

### ScrapeJob Model Changes

```python
class ScrapeJob(models.Model):
    # Existing fields (unchanged)
    url, product_url, currency, status, graph_thread_id, celery_task_id
    site_name, platform, scraping_method, product_count, output_file, site_folder
    full_extraction, auto_queued, error_message
    created_at, started_at, completed_at

    # NEW fields
    page_type = models.CharField(
        max_length=30,
        default="product",
    )
    input_mode = models.CharField(
        max_length=15,
        choices=INPUT_MODE_CHOICES,
        default="url_list",
    )
    search_criteria = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Search/navigation criteria for navigation & SERP modes.",
    )
```

Input modes: `url_list`, `list_page`, `navigation`, `search_term`

### ScrapeState Changes

**Renamed fields** (generic names):
| Old Name | New Name |
|----------|----------|
| `product_url` | `sample_url` |
| `product_analysis` | `content_analysis` |
| `product_count` | `item_count` |
| `skip_product_analysis` | `skip_content_analysis` |
| `product_analysis_retries` | `content_analysis_retries` |

**New fields**:
| Field | Type | Description |
|-------|------|-------------|
| `page_type` | `str` | One of 11 page types (default `"product"`) |
| `input_mode` | `str` | `"url_list"` / `"list_page"` / `"navigation"` / `"search_term"` |
| `site_type` | `str` | `"shopping"` / `"articles"` / `"jobs"` / `"forum"` / `"general"` |
| `content_type_config` | `dict` | Loaded from content type registry |
| `search_criteria` | `str` | For navigation/search modes |
| `output_schema` | `dict` | User-customizable output schema |
| `navigation_analysis` | `dict` | Output of navigation agent (Phase 2) |

### Homepage Redesign

The homepage becomes a **dynamic form** with two sections:

**Section 1 — Type selection** (always visible):
Category cards organized by content domain:
```
Shopping           Articles           Jobs
─────              ────────            ─────
Product            Article            Job Posting
Product List       Article List       Job Navigation
Product Nav        Article Nav

Forum              Search             Generic
─────              ────────           ──────
Forum Thread       SERP               Page Content
```

**Section 2 — Dynamic form fields** (toggled by JS based on type):

| Input Mode | Fields |
|------------|--------|
| `url_list` | Website URL, Sample URL, URL list (paste/upload), Currency |
| `list_page` | Website URL, Listing Page URL(s), Currency |
| `navigation` | Website URL, Search Criteria text field |
| `search_term` | Search Target (Google/Bing/Site), Search Terms, Site URL |

Existing checkboxes (Re-scrape, Full extraction) remain where relevant.
Recent jobs list stays below the form.

### Skills Evolution

Skills answer **HOW to access** a platform — content type handles **WHAT to extract**.

**Existing skills adapt**:
- **Technique skills** (jsonld-extraction, playwright-navigation, anti-bot-handling, proxy-config): Expand descriptions/examples to cover all schema.org types. No structural change needed.
- **Platform skills** (shopify-detection, amazon-detection, sfcc-detection): Add content-type-specific sections (e.g., Shopify skill gets "Blog/Article Extraction" alongside existing "Product Extraction").

**New platform skills** (added organically by skill-learner):
- Articles: `wordpress-detection`, `ghost-detection`, `medium-detection`
- Jobs: `greenhouse-detection`, `lever-detection`, `workday-detection`
- Forums: `discourse-detection`, `phpbb-detection`
- SERP: `google-serp`, `bing-serp`

**Skill YAML enhancement** — optional `content_types` field:
```yaml
---
name: shopify-detection
content_types: [product, article]
---
```
Helps agents prioritize relevant skills. All skills remain visible to all agents (no filtering).

---

## Phase 1: Content Type Generalization

**Goal**: Support URL-list input for 5 content types (product, article, job_posting,
forum_thread, page_content). Existing product scraping preserved as default behavior.

### Tasks

- [x] Create `src/content_types.py` — ContentTypeConfig dataclass, CONTENT_TYPES registry, PAGE_TYPES mapping, helper functions (get_config_for_page_type, get_core_fields, get_output_schema, get_jsonld_types, get_extraction_hints)
- [x] Add `site_type` and `output_schema` fields to Site model + Django migration
- [x] Add `page_type`, `input_mode`, `search_criteria` fields to ScrapeJob model + Django migration
- [x] Add new fields to ScrapeState (`page_type`, `input_mode`, `site_type`, `content_type_config`, `search_criteria`, `output_schema`)
- [x] Rename ScrapeState fields: `product_url`→`sample_url`, `product_analysis`→`content_analysis`, `product_count`→`item_count`, `skip_product_analysis`→`skip_content_analysis`, `product_analysis_retries`→`content_analysis_retries`
- [x] Update `_build_initial_state()` in tasks.py — read new fields from job, load content_type_config from registry
- [x] Update `parse_command` node — initialize new state fields, rename old ones
- [x] Update `check_tracker` node — set `site_type` when auto-creating Site, load output_schema from Site
- [x] Update `normalize_fields` node — read core fields + mapping prompt from `content_type_config` instead of hardcoded product fields
- [x] Update `validate_coverage` node — read core fields from `content_type_config` instead of hardcoded set
- [x] Update `field_confirmation` node — use `output_key` from config instead of hardcoded `"products"`, display fields dynamically
- [x] Update `pre_execution_approval` node — use generic "items" language
- [x] Update `subagents.py` `build_site_analyzer_message` — inject concise content-type context (2-3 lines: field names, JSON-LD types, extraction hints)
- [x] Update `subagents.py` `build_product_analyzer_message` (→ `build_content_analyzer_message`) — same concise injection
- [x] Update `subagents.py` `build_code_writer_message` — include output schema, template family hint, input_mode context
- [x] Update `AGENT_PROMPT_MAP` — add `content_analyzer` mapping alongside existing `product_analyzer`
- [x] Update `.opencode/agents/product-analyzer.md` — make content-type-aware (or create `content-analyzer.md`)
- [x] Create `templates/article_scraper.py` — article field extraction, outputs `{"articles": [...]}`
- [x] Create `templates/job_scraper.py` — job posting fields, outputs `{"jobs": [...]}`
- [x] Create `templates/forum_scraper.py` — thread/posts extraction, outputs `{"threads": [...]}`
- [x] Create `templates/generic_content_scraper.py` — title + content, outputs `{"pages": [...]}`
- [x] Update `home.html` — add page type selector (categorized dropdown or card grid), dynamic form fields via JS, search criteria input for navigation mode
- [x] Update `site_form.html` — add site_type selector, output schema JSON editor
- [x] Update `views.py` `home()` — handle page_type, input_mode, search_criteria from POST
- [x] Update `views.py` `site_add()` — handle site_type, output_schema
- [x] Update `forms.py` `SiteForm` — add site_type field, output_schema JSON editor
- [x] Update `views.py` `site_scrape()` — copy site_type/output_schema to job
- [x] Update `tasks.py` `PHASE_MAP` and `AGENT_PHASE_MAP` — rename product phases to content phases
- [x] Update skill descriptions in existing skills — expand jsonld-extraction to mention all schema.org types, expand platform skills with multi-type sections
- [x] Update `base.html` navigation if needed
- [x] Write tests for content type registry, model changes, state initialization
- [x] Run lint + typecheck

### Deliverable
User selects "Article" type on homepage, pastes article URLs → system builds an article
scraper with article-specific fields (title, author, publish_date, content).

---

## Phase 2: Navigation Analysis Agent

**Goal**: Navigation agent analyzes site patterns, code_writer builds self-navigating
scraper. Supports list page and navigation input modes.

### Tasks

- [x] Create `.opencode/agents/navigation-agent.md` — system prompt for navigation analysis (explore site, find search/category/pagination/item-link patterns)
- [x] Create navigation agent tools or verify existing Playwright MCP tools are sufficient (navigate, click, type, snapshot, extract_links, scroll)
- [x] Add `navigation_agent` node to graph.py — LLM react agent with Playwright MCP tools
- [x] Add conditional edge: `site_analyzer → navigation_agent` when `input_mode in ("navigation", "list_page")`, else `site_analyzer → content_analyzer`
- [x] `navigation_agent` produces `workspace/{slug}/navigation_analysis.json`:
  ```json
  {
    "discovery_method": "search | category | url_pattern",
    "search": {
      "input_selector": "#search-box",
      "submit_selector": "button.search",
      "url_pattern": "/search?q={query}"
    },
    "categories": { "menu_selector": "nav.categories", "url_patterns": [...] },
    "pagination": { "type": "next_button | page_param | infinite_scroll", "selector": "...", "max_pages": null },
    "item_links": { "selector": "a.product-link", "url_pattern": "/product/{slug}" }
  }
  ```
- [x] Add `navigation_analysis` field to ScrapeState (with `_last_write_wins` reducer)
- [x] Update `build_code_writer_message` — inject navigation_analysis context when present. Code writer generates two-phase scraper: Phase 1 (navigate/paginate → discover URLs) → Phase 2 (scrape each URL)
- [x] Create `templates/navigation_scraper.py` — two-phase template blueprint (navigate + scrape)
- [x] Update `scraper_analyzer` — incorporate navigation_analysis into strategy verification
- [x] For list_page mode: simplified navigation analysis (skip search/category, only detect pagination + item link pattern)
- [x] Update homepage — show "Search Criteria" field for navigation mode, "Listing Page URL(s)" for list_page mode (already done in Phase 1)
- [x] Update `_build_initial_state()` — pass search_criteria to state, set skip_content_analysis for navigation/list_page modes
- [x] Add `navigation_analysis` path to `cleanup` node handling (artifact preserved in cleanup)
- [x] Update skill-learner to detect navigation patterns worth learning
- [x] Write tests for navigation analysis output (15 tests in TestNavigationAgent class)
- [x] Run lint + typecheck

### Deliverable
User selects "Product Navigation", enters "footwear sneakers" as criteria on nike.com →
navigation agent analyzes Nike's search → code_writer builds scraper that searches for
footwear, paginates, and scrapes each product.

---

## Phase 3: SERP Support

**Goal**: Scrape search engine results (Google/Bing) and site-internal search.

### Tasks

- [ ] Extend content type registry with SERP-specific extraction rules (rank, position, featured snippets, ads)
- [ ] Create `.opencode/agents/serp-agent.md` — system prompt for SERP analysis
- [ ] Add SERP-specific routing in graph: `input_mode == "search_term"` → `serp_agent` (simplified pipeline: analyze → write → test, skip full builder)
- [ ] External SERP: Google/Bing result page structure detection, pagination, rank extraction
- [ ] Internal SERP: reuse navigation agent's search analysis + site's own search functionality
- [ ] Create `templates/serp_scraper.py` — rank, URL, title, snippet extraction
- [ ] Update homepage — SERP-specific form fields (search target dropdown, search terms, optional site URL for internal search)
- [ ] Add `google-serp` and `bing-serp` skills (page structure, selectors, anti-bot handling)
- [ ] Optional: hybrid mode — follow SERP result URLs to extract full page content (combines Phase 1 pipeline)
- [ ] Write tests for SERP extraction
- [ ] Run lint + typecheck

### Deliverable
User enters "python web scraper" → selects Google as target → system scrapes search
results with rank, URL, title, snippet.

---

## Backward Compatibility

- Default `page_type = "product"`, `input_mode = "url_list"`, `site_type = "shopping"` — all existing behavior preserved
- Existing scrapers continue working without modification
- Old checkpoint data: new state fields default gracefully (empty string/dict)
- Output format: product scrapers still output `{"products": [...]}`, new types output type-specific keys

## Impact Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `src/content_types.py` | **NEW** | Content type registry + helpers |
| `webapp/scraper/models.py` | **MODIFY** | Add site_type, output_schema to Site; add page_type, input_mode, search_criteria to ScrapeJob |
| `webapp/agents/state.py` | **MODIFY** | Rename fields, add new fields |
| `webapp/agents/nodes/normalize_fields.py` | **MODIFY** | Read core fields from config |
| `webapp/agents/nodes/validate_coverage.py` | **MODIFY** | Read core fields from config |
| `webapp/agents/nodes/field_confirmation.py` | **MODIFY** | Dynamic output key and field display |
| `webapp/agents/nodes/pre_execution_approval.py` | **MODIFY** | Generic "items" language |
| `webapp/agents/nodes/parse_command.py` | **MODIFY** | Initialize new fields |
| `webapp/agents/nodes/check_tracker.py` | **MODIFY** | Set site_type on auto-create |
| `webapp/agents/graph.py` | **MODIFY** | Add navigation_agent node, conditional edges (Phase 2) |
| `webapp/agents/subagents.py` | **MODIFY** | Content-type context in build_*_message functions |
| `webapp/scraper/views.py` | **MODIFY** | Handle new form fields |
| `webapp/scraper/forms.py` | **MODIFY** | Add site_type to SiteForm |
| `webapp/scraper/tasks.py` | **MODIFY** | New fields in _build_initial_state |
| `webapp/scraper/templates/scraper/home.html` | **MODIFY** | Page type selector, dynamic fields |
| `webapp/scraper/templates/scraper/site_form.html` | **MODIFY** | Site type, output schema editor |
| `templates/article_scraper.py` | **NEW** | Article scraper template |
| `templates/job_scraper.py` | **NEW** | Job posting scraper template |
| `templates/forum_scraper.py` | **NEW** | Forum thread scraper template |
| `templates/generic_content_scraper.py` | **NEW** | Generic page content template |
| `templates/navigation_scraper.py` | **NEW** | Two-phase navigation scraper (Phase 2) |
| `.opencode/agents/navigation-agent.md` | **NEW** | Navigation agent prompt (Phase 2) |
| `.opencode/agents/product-analyzer.md` | **MODIFY** | Content-type-aware (or rename to content-analyzer.md) |
| `.opencode/skills/*/SKILL.md` | **MODIFY** | Expand with content-type sections |
| `docs/page-type-roadmap.md` | **NEW** | This file |
