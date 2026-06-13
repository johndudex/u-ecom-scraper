# Architecture

## Overview

A **LangGraph-powered, containerized ecommerce product scraper** that takes a product URL as input, analyzes the target site via LLM agents, generates a Python scraper, tests it, and executes it — all orchestrated through a stateful graph with human-in-the-loop approvals.

## Data Flow

```
User submits URL via Django web UI
  → Celery task picks up the job
  → LangGraph graph executes:
      parse_command → check_tracker → setup_workspace
      → site_analyzer (LLM agent)
      → product_analyzer (LLM agent)
      → scraper_analyzer (LLM agent)
      → code_writer (LLM agent)
      → code_tester (LLM agent)
      [human approvals at various points]
      → run_execution (dispatches scraper to browser-service or runs locally)
      → cleanup
      → skill_learner (LLM agent)
  → Results saved to scrapers/{site_slug}/
```

## Containers

| Container | Image Base | Mem Limit | Purpose |
|-----------|-----------|-----------|---------|
| `django` | python:3.12-slim | 512m | Web UI, API, SSE streaming |
| `celery-worker` | python:3.12-slim | 512m | Graph execution, LLM agent orchestration |
| `celery-beat` | python:3.12-slim | 128m | Periodic task scheduler |
| `flower` | python:3.12-slim | 128m | Celery task monitoring UI |
| `browser-service` | python:3.12-slim + Chrome | 1536m | All browser operations: probing, scraping, CDP for MCP |
| `postgres` | postgres:16-alpine | 256m | Database |
| `redis` | redis:7-alpine | 64m | Message broker + cache |

## Graph Flow

```
START
  ↓
parse_command
  ↓
check_tracker ──(Command)──→ [setup_workspace | human_approval | __end__]
  ↓ (normal path)
setup_workspace
  ↓
site_analyzer
  ↓
update_tracker_analysis
  ↓
validate_analysis ──(Command)──→ [product_analyzer | human_approval | code_writer]
  ↓ (normal path)
product_analyzer
  ↓
normalize_fields
  ↓
validate_coverage ──(Command)──→ [scraper_analyzer | human_approval | code_tester]
  ↓ (normal path)
scraper_analyzer
  ↓
code_writer
  ↓
code_tester
  ↓
route_after_testing ──(conditional)──→ [field_confirmation | scraper_analyzer | human_approval]
  ↓ (pass)
field_confirmation ──(Command)──→ [pre_execution_approval | product_analyzer]
  ↓
pre_execution_approval ──(Command)──→ [run_execution | cleanup]
  ↓
run_execution
  ↓
cleanup
  ↓
route_after_cleanup ──(conditional)──→ [skill_learner | __end__]
  ↓
skill_learner
  ↓
END
```

## LLM Agents (7)

| Agent | Purpose | Budget (tool calls) | Temperature |
|-------|---------|---------------------|-------------|
| `site_analyzer` | Detect platform, anti-bot, scraping mechanism | 30/50 | 0.2 |
| `product_analyzer` | Map extractable product fields, selectors | 50/70 | 0.2 |
| `scraper_analyzer` | Determine optimal scraping strategy + proxy | 30 | 0.2 |
| `code_writer` | Generate Python scraper code from analysis | — | 0.1 |
| `code_tester` | Test scraper on samples, validate field correctness | — | 0.1 |
| `cleanup` | Move scraper to per-site folder, update tracker | — | 0.1 |
| `skill_learner` | Extract reusable patterns from completed scrapes | — | 0.1 |

## Deterministic Nodes (11)

| Node | Purpose |
|------|---------|
| `parse_command` | Extract URL, slug, flags from input |
| `check_tracker` | Read TrackedSite from DB, set skip flags, route |
| `setup_workspace` | Create dirs, move output files, clean stale artifacts |
| `update_tracker_analysis` | Write site analysis results to tracker |
| `validate_analysis` | Check analysis completeness, interrupt if low confidence |
| `normalize_fields` | LLM-powered field name normalization |
| `validate_coverage` | Check field coverage, interrupt if too low |
| `field_confirmation` | Show extracted fields to user for approval |
| `pre_execution_approval` | Final approval before running scraper |
| `run_execution` | Dispatch scraper: HTTP locally, browser via browser-service |
| `human_approval` | Generic interrupt resolver |
| `route_after_testing` | Route: pass → field_confirmation, fail → scraper_analyzer (max 3 retries) |
| `route_after_cleanup` | Route: → skill_learner or __end__ |

## browser-service

The single browser service provides Chrome instances and HTTP endpoints:

### Chrome Instances

| Instance | CDP Port | Proxy | Purpose |
|----------|----------|-------|---------|
| `chrome-mcp` | 9222 | none | Playwright MCP interactive browsing |
| `chrome-scraper` | 9223 | none | Remote CDP for generated Playwright scrapers |
| `chrome-uc-*` | ephemeral | per-request | UC Chrome for probe escalation |

### FastAPI Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Chrome alive, Xvfb running, uptime |
| `/probe` | POST | Auto-escalating page probe (7-step chain) |
| `/scrape` | POST | Run a browser scraper script as subprocess |

### Probe Escalation Chain

```
1. Direct HTTP (httpx, no proxy)         → 10s timeout
2. Playwright (no proxy)                → 20s timeout
3. Playwright (datacenter proxy)       → 20s timeout
4. Playwright (residential proxy)      → 20s timeout
5. SeleniumBase UC (no proxy)           → 30s timeout
6. SeleniumBase UC (datacenter proxy)  → 30s timeout
7. SeleniumBase UC (residential proxy) → 30s timeout
```

Returns on first success. Single-concurrency lock prevents parallel probe conflicts.

## Inter-Container Communication

| From | To | Method | Purpose |
|------|-----|--------|---------|
| `celery-worker` | `browser-service` | HTTP POST | Page probing + scraper execution |
| `celery-worker` | `browser-service` | HTTP/SSE | Playwright MCP interactive tools |
| `browser-service` | Bright Data | HTTPS | Proxy for protected sites |
| `celery-worker` | `postgres` | psycopg2 | Graph checkpoints, job state |
| `celery-worker` | `redis` | Celery broker | Task dispatch |
| `django` | `redis` | Pub/Sub | SSE status updates |

## Scraping Strategies

| Strategy | Execution Location | Connection Method |
|----------|-------------------|-------------------|
| `requests` | celery-worker (local) | Direct HTTP |
| `playwright` | celery-worker (remote) | Connects to browser-service via CDP |
| `seleniumbase_uc` | browser-service (subprocess) | Runs inside browser-service container |
| `api` | celery-worker (local) | Direct HTTP (Shopify, Algolia, etc.) |

## Human Approval Points

| Point | Trigger | What user approves |
|-------|---------|-------------------|
| Re-scrape | Site already complete in tracker | Whether to re-scrape |
| Low confidence | Analysis confidence < 0.7 | Whether to continue |
| Low coverage | Field coverage < 80% | Whether to continue |
| Validation failed | Code tester fails 3x | Whether to retry |
| Field confirmation | Before execution | Confirm extracted fields look correct |
| Pre-execution | Final gate | Approve running the scraper |

## Probe Result Caching

The `probe_page` tool caches its first successful result in `ScrapeState.probe_result`. Downstream agents receive the cached result in their prompt instead of re-probing. Maximum 1 probe per job (was up to 3 before caching).

## Output Format

Each scrape produces a versioned JSON file at `scrapers/{site_slug}/output_{timestamp}.json`:

```json
{
  "site": { "name": "...", "url": "...", "platform": "...", "scraped_at": "..." },
  "products": [
    { "id": 1, "title": "...", "price": "...", "availability": "...", ... }
  ],
  "metadata": { "scraping_duration_seconds": 120, "failed_products": 0 }
}
```

## Per-Site Folder Structure

```
scrapers/{site_slug}/
├── analysis/                          # Preserved analysis artifacts
│   ├── site_analysis.json
│   ├── product_analysis.json
│   ├── scraper_analysis.json
│   └── test_report.json
├── scraper.py                         # Generated scraper
├── input_urls.json                    # Product URLs to scrape
└── output_2026-06-10_120000.json      # Versioned output (never deleted)
```

## Key Design Decisions

- **State checkpointed to PostgreSQL** — jobs can be resumed after human-in-the-loop interrupts
- **browser-service is single point of failure** — mitigated by `restart: unless-stopped`, health checks, and Celery retries
- **MCP Chrome runs without proxy** — for sites needing proxy, agents use `probe_page` (which handles per-request escalation)
- **Site tracker migrated to Django ORM** — atomic updates, no race conditions (legacy `data/ecom-websites.json` kept as seed data)
