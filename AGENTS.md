# AGENTS.md

This document provides essential information for agentic coding agents working in this repository.

## Using the Universal Ecommerce Scraper Workflow

This repository uses a multi-agent workflow for scraping product data from ecommerce websites. The agents are configured for **OpenCode** and can be invoked using `@agent-name` syntax.

### Primary Agent

**@scrape-site** - The main coordinator that orchestrates the complete ecommerce scraping workflow.

Invoked by:
```
@scrape-site --url="https://www.shop.com"
```

Optional flags:
```
--product-url="https://www.shop.com/shop/all"    # Product listing URL (auto-discovered if omitted)
--sample-only                                      # Scrape only 5 products (test mode)
```

The orchestrator coordinates:
- Site Analyzer (@site-analyzer) - Analyzes website structure, detects platform, selects scraping mechanism
- Product Analyzer (@product-analyzer) - Maps all extractable product fields with exact selectors
- Code Writer (@code-writer) - Generates Python scraper code
- Code Tester (@code-tester) - Tests scraper on samples, validates field correctness
- Cleanup (@cleanup) - Moves scraper to per-site folder, updates tracker, cleans workspace
- Skill Learner (@skill-learner) - Examines scrapes for reusable patterns, proposes skill updates (always asks user first)

### Subagents

You can also invoke these directly if needed:

**@site-analyzer** - Analyzes ecommerce websites to detect platform and scraping mechanism
- Mode: Subagent (read + bash ask)
- Uses Playwright MCP for browser-based discovery
- Outputs: `workspace/{site_slug}/site_analysis.json`

**@product-analyzer** - Deep-dives into product pages to map extractable fields
- Mode: Subagent (read + bash ask)
- Uses Playwright MCP for structured page analysis
- Outputs: `workspace/{site_slug}/product_analysis.json`

**@code-writer** - Generates Python scraper code based on analysis
- Mode: Subagent (can write + bash)
- Reads templates from `/templates/`
- Outputs: `workspace/{site_slug}/scraper_draft.py`

**@code-tester** - Tests scraper on sample products
- Mode: Subagent (can write + bash)
- Validates field CORRECTNESS (not just presence)
- Outputs: `workspace/{site_slug}/test_report.json`

**@cleanup** - Finalizes scraping workflow
- Mode: Subagent (can write + bash)
- Moves scraper to per-site folder `scrapers/{site_slug}/`, updates tracker, cleans workspace
- Outputs: `workspace/{site_slug}/cleanup_report.json`

**@skill-learner** - Captures reusable knowledge from completed scrapes
- Mode: Subagent (can write, read-only bash)
- Examines artifacts + scraper code after each successful scrape
- Compares against existing skills in `.opencode/skills/`
- Proposes new skills or updates to existing ones
- **ALWAYS asks user before creating/modifying skill files**
- Outputs: `workspace/{site_slug}/learning_report.json`

**nav-skill-review** (pipeline-only) - Auto-applies navigation learnings during pipeline
- Mode: Subagent (read + write + edit + skills)
- Runs automatically after `navigation_synthesize`, before `scraper_analyzer`
- Compares raw navigation findings against existing skills
- **Auto-applies** reusable patterns by appending `## Learned:` sections to skills
- Non-blocking: failures don't halt the pipeline
- Outputs: `workspace/{site_slug}/nav_learning_report.json`

## Site Tracker

The site tracker lives at: `data/ecom-websites.json`

This file tracks all websites that have been scraped. The orchestrator automatically:
- Checks if a site exists before scraping (prevents duplicates)
- Adds new sites with `status: "in_progress"`
- Updates to `status: "complete"` after successful scraping
- Records product count, fields extracted, and timestamps

## Per-Site Folder Structure

Each scraped website gets its own folder under `scrapers/`:

```
scrapers/
├── nike-com/
│   ├── scraper.py                         # Generated scraper
│   ├── input_urls.json                    # Product URLs to scrape
│   ├── output_2026-06-06_120000.json      # Scrape run 1
│   ├── output_2026-06-20_150000.json      # Scrape run 2 (price tracking)
│   └── output_2026-07-05_100000.json      # Scrape run 3
├── shopify-store/
│   ├── scraper.py
│   ├── input_urls.json
│   └── output_2026-06-10_090000.json
└── amazon-com/
    ├── scraper.py
    ├── input_urls.json
    └── output_2026-06-15_180000.json
```

**Output files are versioned with UTC timestamps** (`output_YYYY-MM-DD_HHMMSS.json`) so you can track price changes over time. Each scrape run creates a new file.

### input_urls.json format:
```json
{
  "urls": [
    "https://www.nike.com/product/air-max-90",
    "https://www.nike.com/product/air-force-1"
  ]
}
```

### output.json format:
```json
{
  "site": {
    "name": "Nike",
    "url": "https://www.nike.com",
    "platform": "custom",
    "scraping_method": "playwright",
    "scraped_at": "2026-06-06T12:00:00Z"
  },
  "products": [
    {
      "id": 1,
      "title": "Nike Air Max 90",
      "price": "$129.99",
      "availability": "In Stock",
      "original_price": "$159.99",
      "currency": "USD",
      "url": "https://www.nike.com/product/air-max-90",
      "src_url": "https://www.nike.com/shop/all",
      "location": "",
      "status_code": 200,
      "scraped_at": "2026-06-06T12:00:05Z",
      "remarks": ""
    }
  ],
  "metadata": {
    "scraping_duration_seconds": 120,
    "failed_products": 0,
    "rate_limit_delay": 2.0
  }
}
```

## Output Fields

The scraper extracts whatever is available from each product page. Standard output fields:

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Sequential product ID |
| `title` | text | Product title/name |
| `price` | text | Current selling price |
| `availability` | text | Stock status (e.g. "In Stock", "Out of Stock") |
| `original_price` | text | Price before discount (strikethrough/was price). Empty if not on sale. |
| `currency` | text | Currency code (e.g. "USD", "EUR") |
| `url` | text | Direct URL to the product page |
| `src_url` | text | Source URL where product was discovered |
| `location` | text | Product location (e.g. warehouse, store) |
| `status_code` | number | HTTP status code from fetching the page |
| `scraped_at` | timestamp | ISO-8601 timestamp of when product was scraped |
| `remarks` | text | Any notes, warnings, or extraction issues |

## Build / Lint / Test Commands

### Running Scrapers
```bash
# Via orchestrator (recommended)
@scrape-site --url="https://www.shopify-store.com"

# Run generated scraper directly (reads input_urls.json from same folder)
python3 scrapers/nike-com/scraper.py

# Run with explicit input file
python3 scrapers/nike-com/scraper.py --input input_urls.json

# Run with URLs as CLI argument
python3 scrapers/nike-com/scraper.py --urls "https://shop.com/p1" "https://shop.com/p2"
```

### Linting & Type Checking
```bash
ruff check scrapers/
ruff check --fix scrapers/
ruff format scrapers/
mypy scrapers/
```

### Running Tests
```bash
pytest
pytest -v
pytest --cov=src --cov-report=term-missing
```

## Code Style Guidelines

### Imports
Order imports: stdlib, third-party, local. Use absolute imports for local modules.

```python
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
```

### Type Hints
Use type hints consistently. Use `Optional[T]` for nullable values.

### Data Structures
Use `@dataclass` for data containers.

### Naming Conventions
- Classes: `PascalCase`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`

### Error Handling
Use try/except with logging. Don't suppress exceptions silently.

```python
try:
    response = requests.get(url, timeout=15)
    response.raise_for_status()
except Exception as e:
    logger.error(f"Failed to fetch {url}: {e}")
    return None
```

### Logging
Use logging extensively.

```python
logger.info(f"Progress: [{current}/{total}] ({percent:.1f}%)")
logger.error(f"Failed to process {url}: {e}")
```

### Formatting
- Line length: 100 characters
- Indent: 4 spaces (no tabs)
- Quote style: Double quotes
- Use `ruff format` to auto-format

## Workflow Artifacts

Temporary artifacts live in `workspace/{site_slug}/`:
- `site_analysis.json` - Site structure and scraping mechanism
- `product_analysis.json` - Product page field mapping
- `scraper_draft.py` - Generated scraper (before cleanup moves it)
- `test_report.json` - Validation results and feedback
- `cleanup_report.json` - Cleanup actions summary
- `learning_report.json` - Skill learning findings and proposals

**Final output per site:**
- `scrapers/{site_slug}/scraper.py` - Production-ready scraper
- `scrapers/{site_slug}/input_urls.json` - Product URLs to scrape
- `scrapers/{site_slug}/output_{datetime}.json` - Scraped product data (versioned)

**Live logs:**
- `logs/{site_slug}.log` - Real-time progress and errors

**Site tracker:**
- `data/ecom-websites.json` - All tracked sites with status

## Project Structure

```
u-ecom-scraper/
├── opencode.json                    # Main config (agents + Playwright MCP)
├── AGENTS.md                        # This file
├── .opencode/
│   ├── agents/                      # Agent definitions (7 agents)
│   │   ├── scrape-site.md           # PRIMARY - Orchestrator
│   │   ├── site-analyzer.md         # SUBAGENT - Website analysis
│   │   ├── product-analyzer.md      # SUBAGENT - Product page analysis
│   │   ├── code-writer.md           # SUBAGENT - Code generation
│   │   ├── code-tester.md           # SUBAGENT - Testing & validation
│   │   ├── cleanup.md               # SUBAGENT - Cleanup & tracker
│   │   └── skill-learner.md         # SUBAGENT - Skill learning
│   └── skills/                      # Reusable knowledge (4+ skills, grows over time)
│       ├── shopify-detection/SKILL.md
│       ├── playwright-navigation/SKILL.md
│       ├── anti-bot-handling/SKILL.md
│       └── proxy-config/SKILL.md
├── templates/                       # Scraper code templates (4 templates)
│   ├── playwright_scraper.py
│   ├── requests_scraper.py
│   ├── shopify_scraper.py
│   └── api_scraper.py
├── scrapers/                        # Per-site scraper folders
│   └── {site_slug}/
│       ├── scraper.py               # Generated scraper
│       ├── input_urls.json          # Product URLs
│       └── output_{datetime}.json  # Scraped data (versioned)
├── src/                             # Shared source code
├── data/
│   ├── ecom-websites.json           # Site tracker
│   └── output/                      # Legacy output (archived)
│       └── archive/
├── workspace/                        # Temporary inter-agent artifacts
├── logs/                            # Scraper execution logs
└── tests/                           # Validation tests
```

## Important Notes

- Python 3.10+ required
- Playwright MCP is mandatory for site-analyzer and product-analyzer agents
- `requests` and `beautifulsoup4` required for HTTP-based scrapers
- `playwright` required for browser-based scrapers
- Always use `ruff` for linting and formatting
- Handle exceptions with appropriate logging
- Rate limiting is critical - always respect the site's bandwidth
- **Debug Auto-Login**: `DEBUG_AUTO_LOGIN=True` in docker-compose.yml automatically authenticates as the first superuser. This allows curl/CDP access to authenticated pages (including Django admin) without manual login cookies. To take screenshots via CDP, use `http://host.docker.internal:8000/` (not localhost) from the browser-service container.
- **Admin Theme**: Django admin theme is controlled by `admin/base_site.html`. It kills `theme.js`, `dark_mode.css`, `responsive.css`, and `dashboard.css` at the template block level. The float-based layout from `base.css` is neutralized via CSS reset. Custom `admin/index.html` prevents `dashboard.css` loading. Nav sidebar width set to 232px to match main app.
- **Lint/Typecheck**: `ruff check scrapers/` and `ruff format scrapers/`
- **Tests**: `docker compose exec django sh -c "cd /app/webapp && PYTHONPATH=/app/webapp:/app python -m pytest /app/tests/ -v"`
