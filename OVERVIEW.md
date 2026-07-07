# u-ecom-scraper — System Overview

## What It Does

An agentic scraper builder. Submit a URL and a search term (or a list of product URLs), and a multi-agent pipeline autonomously analyzes the target site, discovers product pages, generates a Python scraper, tests it, and extracts structured data. The pipeline runs inside Docker with human-in-the-loop approval gates at key decision points.

## How It Works

```
User submits URL via web UI
  → Django creates a ScrapeJob, enqueues a Celery task
  → Celery worker builds a LangGraph graph with job state
  → Graph executes 22 nodes sequentially with conditional branching:
      [setup] → [analysis] → [validation] → [generation + testing] → [execution]
  → Each LLM agent reads upstream artifacts, produces its artifact
  → Human approval interrupts at low-confidence or ambiguous points
  → Generated scraper runs (in-process or via browser service)
  → Results saved to scrapers/{site-slug}/output_{timestamp}.json
```

## Services

| Service | Role |
|---------|------|
| **Django** | Web UI, REST API, SSE streaming for live job logs, health dashboard. Serves all HTML templates. |
| **PostgreSQL** | Persists jobs, sites, approval records, session logs, probe cache. |
| **Redis** | Celery message broker and Django cache backend. |
| **Celery Worker** | Executes LangGraph graph nodes. Each node is a Python function — some invoke LLM agents, some are pure logic. |
| **Celery Beat** | Periodic scheduler — runs stuck-job watchdog and auto-retry. |
| **Flower** | Celery task monitoring UI at port 5555. |
| **Browser Service** | FastAPI service managing two Chrome instances: one for Playwright MCP (agent browsing) and one for scraper execution. Exposes `/probe` (auto-escalating page fetch), `/render` (HTML rendering), `/scrape` (run scraper subprocess), and `/health`. |

Browser Service communicates with agents over HTTP — agents never touch Chrome directly. The Playwright MCP endpoint (`/sse`) allows agents to navigate, click, and evaluate JS in a shared browser session.

## The Pipeline

### Phase 1: Setup
1. **parse_command** — Parses the job input into structured state (URL, slug, input mode, search criteria).
2. **check_tracker** — Checks if the site was previously scraped. Skips completed sites; resets in-progress sites on re-run.
3. **setup_workspace** — Creates `workspace/{slug}/` directory, restores preserved artifacts from previous runs.
4. **check_accessibility** — Quick HTTP HEAD check on the target URL. Fails early if the site is unreachable.

### Phase 2: Analysis
5. **site_analyzer** (LLM agent) — Probes the site with 7 escalation strategies (direct HTTP → Playwright → UC Chrome). Detects platform, anti-bot protection, scraping mechanism, and currency.
6. **navigation_explore** — For search/list-page jobs: uses Playwright MCP to browse the site, find the search form, type the query, and extract product links and category structure. Falls back to category browsing if search fails.
7. **navigation_synthesize** (LLM agent) — Converts raw navigation findings into structured analysis: search URL pattern, category links, pagination type, item selectors.
8. **nav_skill_review** — Compares navigation findings against existing skills to capture reusable patterns.
9. **product_analyzer** (LLM agent) — Deep-dives into a sample product page. Maps every extractable field (title, price, availability, etc.) with CSS selectors and extraction strategies.

### Phase 3: Validation
10. **update_tracker_analysis** — Persists analysis results to the site record.
11. **validate_analysis** — Checks site analysis confidence score. Low confidence triggers human approval.
12. **normalize_fields** — Maps raw fields to canonical schema using content type config.
13. **validate_coverage** — Checks that enough fields are covered. If coverage is low, sends analysis back to product_analyzer for a second pass.
14. **field_confirmation** — Interrupts for human review. Shows extracted fields alongside a sample of actual extracted data so the operator can verify correctness.

### Phase 4: Generation & Testing
15. **scraper_analyzer** (LLM agent) — Reads site analysis + product analysis + navigation analysis. Produces a scraping strategy document with exact selectors and extraction logic.
16. **code_writer** (LLM agent) — Reads the scraper analysis and writes a complete Python scraper. For navigation jobs, encodes the discovery URL and selectors from nav analysis. For URL-list jobs, reads from `input_urls.json`.
17. **code_tester** — Runs the generated scraper on 5 sample products. Validates that output fields are correct (not just present). Up to 3 auto-retry cycles (code_writer → code_tester → scraper_analyzer loop).
18. **route_after_testing** — Evaluates test results. If all retries exhausted, triggers human approval with "Provide feedback for final retry". Final failure → cleanup (no partial output).

### Phase 5: Execution & Cleanup
19. **pre_execution_approval** — Optional gate. For navigation jobs, confirms the discovery URL with the operator before running at scale.
20. **run_execution** — Executes the scraper. Browser-based scrapers go through browser_service; HTTP scrapers run in-process.
21. **cleanup** (LLM agent) — Moves scraper to `scrapers/{slug}/`, updates the site tracker, generates `input_urls.json` with discovered URLs.
22. **skill_learner** — Examines the completed scrape for reusable patterns. Proposes updates to skill files (with user approval).

## Agents

| Agent | Role | Input | Output |
|-------|------|-------|--------|
| **site_analyzer** | Probes target site, detects platform and scraping method | URL, search criteria | `site_analysis.json` |
| **navigation_explore** | Browses site via Playwright MCP, finds search/category structure | Homepage URL, search term | `navigation_findings.json` |
| **navigation_synthesize** | Converts raw findings into structured search/navigation strategy | Raw findings | `navigation_analysis.json` |
| **product_analyzer** | Maps extractable fields from a sample product page | Product page | `product_analysis.json` |
| **scraper_analyzer** | Synthesizes all analyses into a scraping strategy | All analysis artifacts | `scraper_analysis.json` |
| **code_writer** | Generates Python scraper from strategy | Strategy + templates | `scraper_draft.py` |
| **code_tester** | Tests scraper, validates output correctness | Scraper + sample URLs | `test_report.json` |
| **cleanup** | Moves scraper to production folder, updates tracker | Scraper + artifacts | `cleanup_report.json` |
| **skill_learner** | Identifies reusable patterns from completed scrape | All artifacts | `learning_report.json` |

## Key Design Decisions

**LangGraph over direct orchestration** — The pipeline is a stateful graph with conditional edges (not a linear script). This allows routing based on runtime state: skip navigation for URL-list jobs, retry failed analysis, interrupt for human approval at confidence thresholds, and branch to cleanup on exhaustion.

**Human-in-the-loop at gates, not everywhere** — Approvals only fire at decision points where automated confidence is below threshold: site analysis confidence, field coverage gaps, and test retry exhaustion. Most jobs run end-to-end without intervention.

**Two-phase architecture for navigation jobs** — Phase 1 (navigation_explore) discovers product URLs by browsing the site and extracting the search URL pattern. Phase 2 (the generated scraper) reuses that discovery logic at runtime. This means the scraper can find new products on future runs without a fixed URL list.

**Browser Service as a shared resource** — Two Chrome instances (MCP for agent browsing, scraper for execution) are managed by a single FastAPI service. Agents interact via HTTP/JSON, never directly with Chrome. This decoupling allows the browser to run in a different container with different capabilities (UC Chrome, proxies, Xvfb).

## Folder Map

```
u-ecom-scraper/
├── docker-compose.yml              # 7 services
├── webapp/
│   ├── agents/
│   │   ├── graph.py               # LangGraph assembly (22 nodes, conditional edges)
│   │   ├── state.py               # ScrapeState TypedDict — shared state across all nodes
│   │   ├── subagents.py           # Agent factories, prompt builders, message formatters
│   │   ├── constants.py           # Shared constants (retry limits, sentinels, status codes)
│   │   ├── decisions.py           # Human approval decision parsing
│   │   ├── llm.py                 # LLM client setup (ZAI / OpenAI-compatible)
│   │   ├── nodes/                 # 18 graph node modules (each file = one or more nodes)
│   │   └── tools/                 # Agent tools (probe_page, web_fetch, filesystem, shell)
│   ├── scraper/
│   │   ├── models.py              # Django models: ScrapeJob, Step, Site, Approval, ContentType
│   │   ├── tasks.py               # Celery task entry point, retry logic, output preservation
│   │   ├── views.py               # Web views, SSE streaming, job/site management, health API
│   │   ├── services.py            # Interrupt-to-approval-type mapping, approval routing
│   │   └── templates/             # HTML templates (job detail, site detail, approvals, health)
│   └── config/
│       └── settings.py            # Django settings, feature flags, API config
├── browser_service/
│   ├── server.py                 # FastAPI: /probe, /render, /scrape, /health
│   ├── probe.py                  # 7-step auto-escalating page probe
│   ├── browser_pool.py           # Chrome instance lifecycle
│   └── scraper_runner.py         # Run generated scrapers in Chrome subprocess
├── src/
│   ├── geo.py                    # Country detection from TLD
│   ├── content_types.py          # Content type definitions and choices
│   ├── proxy.py                  # Proxy configuration and URL builder
│   └── page_analysis.py          # Common CSS selectors and JSON-LD extraction
├── templates/                     # 11 scraper code templates (playwright, requests, shopify, etc.)
├── scrapers/                      # Per-site generated scrapers + output files
├── .opencode/
│   ├── agents/                   # 11 agent definition files (prompts, tools, instructions)
│   └── skills/                   # 15 reusable detection skills (Shopify, SFCC, Algolia, etc.)
├── data/                          # Site tracker (ecom-websites.json)
├── tests/                         # Test suite
└── docs/                          # Additional documentation
```
