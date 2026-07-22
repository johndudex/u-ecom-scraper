# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Universal Ecommerce Scraper — an agentic scraper builder powered by LangGraph. Submit a URL and search term (or product URL list), and a multi-agent pipeline autonomously analyzes the target site, discovers product pages, generates a Python scraper, tests it, and extracts structured data. Runs inside Docker with human-in-the-loop approval gates.

## Commands

### Running the System

```bash
# Build and start all services
docker compose --profile full up --build -d

# View logs
docker compose logs -f celery-worker      # Scraping jobs
docker compose logs -f browser_service    # Browser automation
docker compose logs -f django             # Web UI

# Restart a single service
docker compose restart celery-worker

# Stop everything
docker compose --profile full down
```

### Development

```bash
# Linting
docker compose exec django ruff check webapp/ src/
docker compose exec django ruff format webapp/ src/

# Run tests
docker compose exec django pytest webapp/

# Create Django superuser
docker compose exec django python manage.py createsuperuser
```

### Access Points

| Service | URL | Purpose |
|---------|-----|---------|
| Django Web UI | http://localhost:8000 | Submit jobs, monitor progress, approve steps |
| Agent Playground | http://localhost:8000/agent-playground/ | Test agents in isolation |
| Flower (Celery) | http://localhost:5555 | Task queue monitoring |
| Browser Service | http://localhost:8001 | Page probing, scraper execution |
| Health Dashboard | http://localhost:8000/health/ | Service status |

## Architecture Overview

### Services

| Service | Role |
|---------|------|
| **Django** | Web UI, REST API, SSE streaming for live job logs, health dashboard |
| **PostgreSQL** | Persists jobs, sites, approvals, session logs, probe cache |
| **Redis** | Celery message broker, Django cache backend |
| **Celery Worker** | Executes LangGraph graph nodes (LLM agents + deterministic logic) |
| **Celery Beat** | Periodic scheduler — stuck-job watchdog, auto-retry |
| **Browser Service** | FastAPI managing two Chrome instances: Playwright MCP (agent browsing) + scraper execution. Exposes `/probe`, `/render`, `/scrape`, `/health` |

Browser Service decouples agents from Chrome — communication via HTTP/JSON only.

### Pipeline Flow (25 nodes)

> Node count is approximate — the graph registers ~26 nodes today (`code_review` is being removed in the current complexity audit; `dagster_converter` stays). The linear diagram below shows the main happy path and omits several branching/utility nodes (`update_tracker_analysis`, `validate_analysis`, `field_confirmation`, `dagster_converter`, `store_job_listings`, `human_approval`, `navigation_agent` fallback).

```
parse_command → check_tracker → setup_workspace → check_accessibility
  → site_analyzer → [navigation_explore → navigation_synthesize] (nav/search only)
  → product_analyzer → normalize_fields → validate_coverage
  → scraper_analyzer → code_writer → code_tester
  → [retry loop if failed]
  → pre_execution_approval → run_execution → cleanup
  → nav_skill_review → skill_learner → END
```

**Phases:**
1. **Setup** — parse_command, check_tracker (resume logic), setup_workspace, check_accessibility (7-step probe)
2. **Analysis** — site_analyzer (platform/anti-bot), navigation_explore (browse), navigation_synthesize (**deterministic** — no LLM), nav_skill_review, product_analyzer (field mapping)
3. **Validation** — update_tracker_analysis, validate_analysis (confidence check), normalize_fields, validate_coverage, field_confirmation
4. **Generation & Testing** — scraper_analyzer (**deterministic strategy** — no LLM), code_writer (Python), code_tester (validate). (The read-only `code_reviewer` LLM that sat before `code_tester` has been removed.)
5. **Execution & Cleanup** — pre_execution_approval, run_execution, cleanup, skill_learner

**Two-phase architecture for navigation jobs:** Phase 1 discovers product URLs by browsing; Phase 2 (the generated scraper) reuses discovery logic at runtime.

### Agent Table

LLM agents only. (`navigation_synthesize` and `scraper_analyzer` still exist as graph nodes but are now **deterministic** functions — they consume the same artifact filenames but no longer run an LLM. The `code_reviewer` LLM has been removed entirely.)

| Agent | Role | Input | Output |
|-------|------|-------|--------|
| **site_analyzer** | Probes site, detects platform/scraping method | URL, search criteria | `site_analysis.json` |
| **navigation_explore** | Browses via Playwright MCP, finds search/category | Homepage, search term | `navigation_findings.json` |
| **product_analyzer** | Maps extractable fields from sample page | Product page | `product_analysis.json` |
| **code_writer** | Generates Python scraper | Strategy + templates | `scraper_draft.py` |
| **code_tester** | Tests scraper, validates output | Scraper + sample URLs | `test_report.json` |
| **cleanup** | Moves scraper to production, updates tracker | Scraper + artifacts | `cleanup_report.json` |
| **skill_learner** | Identifies reusable patterns | All artifacts | `learning_report.json` |

### Content Types (Multi-Domain)

The system supports 11 page types across 6 content domains (see `docs/page-type-roadmap.md`):

| Domain | Content Type | Core Fields | Output Key |
|--------|-------------|------------|------------|
| Shopping | `product` | title, price, availability, currency | `products` |
| Articles | `article` | title, author, publish_date, content | `articles` |
| Jobs | `job_posting` | title, company, location, description | `jobs` |
| Forum | `forum_thread` | title, author, posts | `threads` |
| SERP | `serp` | rank, url, title, snippet | `results` |
| Generic | `page_content` | title, content | `pages` |

Registry in `src/content_types.py` defines fields, JSON-LD types, extraction hints. Site model has `site_type` and `output_schema` fields.

### LangGraph State (`ScrapeState`)

Central state in `webapp/agents/state.py` (TypedDict, all fields optional). Key fields:
- `job_id`, `url`, `site_slug`, `site_name` — Job identity
- `page_type` — One of 11 page types (default `"product"`)
- `input_mode` — `url_list|navigation|list_page|search_term`
- `search_criteria` — Query for navigation/search modes
- `site_type` — `shopping|articles|jobs|forum|general`
- `content_type_config` — Loaded from `src/content_types.py`
- `output_schema` — User-customizable schema
- `skip_*` flags — Resume control (`skip_site_analysis`, `skip_content_analysis`, etc.)
- `*_analysis` — Artifact JSONs from each phase
- `probe_result` — Cached connectivity from `check_accessibility`
- `interrupt_*` — Human-in-the-loop state
- `next_node_after_*` — Routing decisions
- `messages` — LangGraph message channel
- `agent_logs` — Accumulator for tool/LLM output

### Key Routing Nodes

- `check_tracker` — Sets `skip_*` flags if site has artifacts; auto-creates Site entry
- `check_accessibility` — Probes URL with 7-step escalation, detects captcha/Akamai, can END job early
- `_route_after_site_analyzer` — Routes to `navigation_explore` for `navigation|list_page|search_term`, else `update_tracker_analysis`
- `route_after_testing` — Routes back to `scraper_analyzer` (fix), `cleanup` (pass), or `human_approval` (exhausted)
- `route_from_human_approval` — Complex resume routing after user input

**Critical bug:** `_route_after_site_analyzer` must include `search_term` in condition. Missing it bypasses navigation entirely (see `docs/scraper_agents.md`).

### Human-in-the-Loop

Nodes use `langgraph.types.interrupt()` to pause:
- `check_tracker` — Re-scrape confirmation
- `validate_analysis` — Low confidence (< threshold)
- `validate_coverage` — Field coverage gaps
- `field_confirmation` — Missing fields acknowledgment
- `pre_execution_approval` — Pre-execution check (optional)
- `human_approval` — Generic handler (maps interrupt reasons to approval types)

Interrupt → approval type mapping in `webapp/scraper/services.py:INTERRUPT_TO_APPROVAL_TYPE`.

### Agent Factories (`webapp/agents/subagents.py`)

Each **LLM** phase is wrapped as a `create_react_agent(langchain)`:
- Factory functions (`create_site_analyzer`, etc.)
- System prompts from `.opencode/agents/{name}.md`
- Tool sets from `webapp/agents/tools/`
- Temperature from `AGENT_TEMPERATURES` map
- `AGENT_MAX_ITERATIONS` per agent
- Tool guards applied via `_apply_guards()`

**Not LLM agents** (deterministic functions, no `create_react_agent`): `scraper_analyzer`, `navigation_synthesize`. `code_reviewer` has been removed.

Message builders (`build_*_message`) inject context: probe cache, content type config, navigation analysis, test reports, human feedback.

**GLM Model Quirk:** GLM emits `v__` prefix on tool args (e.g., `v__command`). `_strip_v_prefix_from_tools()` monkey-patches `BaseTool._parse_input` globally.

### Browser Service (`browser_service/`)

FastAPI container with Chrome + Xvfb + SeleniumBase + Playwright:
- `probe.py` — 7-step escalation: direct HTTP → browser (no proxy) → browser (datacenter) → browser (residential)
- `browser_pool.py` — Chrome lifecycle, CDP endpoint management
- `scraper_runner.py` — Runs generated scrapers as subprocesses with fresh CDP
- `server.py` — `/probe`, `/render`, `/scrape`, `/health` endpoints

Generated scrapers run here via `run_scraper` tool (NOT in Celery container). `/scrape` dispatches to appropriate subprocess based on strategy.

### Input Modes

- `url_list` — User provides item URLs; skip navigation phases
- `navigation` — Discover via search + categories; requires `navigation_explore`
- `list_page` — User provides listing page; extract links from it
- `search_term` — Search by query; requires `navigation_explore`

**Two-phase scrapers** (for navigation/list_page/search_term):
- Phase 1: Navigate/search/paginate → discover item URLs
- Phase 2: Extract fields from each discovered item page

### Skills System (`.opencode/skills/`)

Reusable detection/technique modules:
- Platform skills — shopify-detection, sfcc-detection, algolia-detection, amazon-detection, kibo-detection
- Technique skills — jsonld-extraction, playwright-navigation, anti-bot-handling, proxy-config
- Multi-content skills — Expand with sections per domain (e.g., Shopify gets "Product Extraction" + "Article Extraction")

Agents see lightweight descriptions in system prompt, use `load_skill` tool for full content. `nav_skill_review` (post-cleanup) and `skill_learner` auto-append "## Learned:" sections.

### Templates (`templates/`)

Base scrapers per strategy. `code_writer` adapts these:
- `undetected_chromedriver_scraper.py` — SeleniumBase UC mode with `uc_open_with_reconnect()`
- `navigation_scraper.py` — Two-phase (discover → scrape)
- `http_navigation_scraper.py` — Two-phase HTTP navigation (discover → scrape, browser-free discovery)
- `api_scraper.py` — HTTP + JSON API
- `playwright_scraper.py` — Playwright browser automation
- `requests_scraper.py` — Simple HTTP requests
- `shopify_scraper.py` — Shopify-specific

The per-domain content templates (`article_scraper.py`, `forum_scraper.py`, `generic_content_scraper.py`, `akamai_stealth_scraper.py`) are dead/retired in the current audit and should not be referenced for new work.

## Important Development Notes

### Agent Playground (`/agent-playground/`)

Test agents in isolation (see `docs/testing_guide.md`). Test sequentially — each agent produces files the next needs:
1. `site_analyzer` — Produces `site_analysis.json`
2. `navigation_explore` — Produces `navigation_findings.json`
3. `product_analyzer` — Produces `product_analysis.json`
4. `code_writer` — Produces `scraper_draft.py`
5. `code_tester` — Produces `test_report.json`

`navigation_synthesize` and `scraper_analyzer` are deterministic steps now (no LLM), so they are not exercised here; their output files (`navigation_analysis.json`, `scraper_analysis.json`) are produced by plain functions and can be crafted/inspected directly when seeding the next agent.

**Do NOT clean workspace between sequential tests.** Each reads files written by the previous one.

### Resume Logic

`check_tracker` sets `skip_site_analysis`, `skip_content_analysis`, `skip_code_generation` on re-scrape. Flags checked throughout graph to jump ahead. For navigation jobs, navigation_explore runs even when site_analysis skipped (discoveries needed).

### Probe Caching

`check_accessibility` runs full probe once, caches in `state.probe_result`. Downstream agents should use cached data, NOT call `probe_page` again. Probe result includes captcha verification and method_that_worked.

### Budget Management

Agents have call budgets (e.g., `SITE_ANALYSIS_BUDGET = 10`). If exhausted without writing artifact → `human_approval` for budget escalation. Auto-extension if progress detected (5+ tool calls). Auto-escalation tries higher budget once before requiring approval.

### Routing with Commands

Many nodes return `Command(goto=..., update=...)` instead of dicts. This bypasses conditional edges. Used by: `check_accessibility`, `validate_analysis`, `validate_coverage`, `field_confirmation`, `pre_execution_approval`. The node itself decides where to go next.

### Tool Guards

`webapp/agents/subagents.py:_apply_guards()` adds wrappers:
- `require_non_akamai_tool` — Block if Akamai detected
- `require_same_domain` — Only allow URLs from original site
- `require_target_url` — Must use provided sample URL

Guards apply to `site_analyzer`, `product_analyzer`, `navigation_agent` (with variations). (`scraper_analyzer` no longer has guards — it is deterministic.)

### Workspace vs. Scrapers Directory

- `workspace/{site_slug}/` — Temporary artifacts during job run
- `scrapers/{site_slug}/` — Finalized scraper, input_urls.json, analysis/ (preserved)

`cleanup` moves artifacts from workspace to scrapers. `run_execution` writes output to `scrapers/{site_slug}/output_{datetime}.json`.

### Retry Loops

`route_after_testing` can loop: `code_tester → scraper_analyzer → code_writer → code_tester`. `test_retry_count` tracks cycles. Final retry (sentinel `FINAL_RETRY_SENTINEL`) triggers human approval with "Provide feedback for final retry". Final failure → cleanup without output.

### Field Renaming (Content Type Generalization)

Old → New (generic names):
- `product_url` → `sample_url`
- `product_analysis` → `content_analysis`
- `product_count` → `item_count`
- `skip_product_analysis` → `skip_content_analysis`

Code still uses old names in some places (gradual migration).

### Content Type Config

`src/content_types.py` registry drives content-type-specific behavior:
- `CONTENT_TYPES` dict maps content type → `ContentTypeConfig`
- `PAGE_TYPES` maps user-facing page types → content types
- Helper functions: `get_config_for_page_type()`, `get_core_fields()`, `get_output_schema()`

`check_tracker` auto-creates Site with `site_type` from job's content type. `normalize_fields` and `validate_coverage` read core fields from config.

### Django Settings

`webapp/config/settings.py`. Key:
- `PROJECT_ROOT` — Absolute path for file operations
- `CELERY_BROKER_URL` — Redis connection
- `ZAI_MAIN_MODEL`, `ZAI_SMALL_MODEL` — LLM model selection
- `PLAYWRIGHT_MCP_URL`, `BROWSER_SERVICE_URL` — Service endpoints

## References

- README.md — User-facing docs, quick start
- OVERVIEW.md — System overview, services, pipeline phases, design decisions
- docs/scraper_agents.md — Detailed pipeline audit, root cause analysis (CK UK case study)
- docs/testing_guide.md — Agent playground procedures, pass criteria, common failures
- docs/page-type-roadmap.md — Multi-content-type architecture, Phase 1/2/3 plans
- DESIGN.md — UI design system, tokens, Tailwind config
