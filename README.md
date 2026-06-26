# Universal Ecommerce Scraper

An agentic ecommerce product scraper builder powered by LangGraph. Submit a product URL, and the system analyzes the site, generates a tailored Python scraper, tests it, and executes it — all orchestrated through a stateful graph with human-in-the-loop approvals.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose v2](https://docs.docker.com/compose/install/)
- An LLM API key (ZAI / OpenAI-compatible endpoint)

## Quick Start

```bash
# Clone the repository
git clone <repo-url>
cd u-ecom-scraper

# Create environment file from template
cp .env.example .env

# Edit .env — fill in your API key (required)
#   ZAI_API_KEY=your-api-key-here
#   ZAI_BASE_URL=your-base-url-here  (if using a different provider)
#   DJANGO_SUPERUSER_PASSWORD=your-admin-password

# Build and start all services
docker compose --profile full up --build -d

# View logs
docker compose --profile full logs -f celery-worker
```

Once running, open **http://localhost:8000** and log in with `admin` / your `DJANGO_SUPERUSER_PASSWORD`.

### Login

An admin user is auto-created on first startup with:
- **Username:** `admin`
- **Password:** value of `DJANGO_SUPERUSER_PASSWORD` (defaults to `admin`)

To create additional users:

```bash
docker compose exec django python manage.py createsuperuser
```

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Django Web UI | 8000 | Submit jobs, monitor progress, approve steps |
| Flower (Celery Monitor) | 5555 | Task queue monitoring |
| Browser Service API | 8001 | Page probing, scraper execution |
| Playwright MCP | 8111 | Agent interactive browsing (SSE) |
| Chrome CDP (MCP) | 9222 | Playwright MCP browser |
| Chrome CDP (Scraper) | 9223 | Remote CDP for generated scrapers |

## Usage

### Managing Sites

All pages require login. Add websites via the **Sites** page:

1. Click **Sites** in the nav bar → **Add Site**
2. Enter the **site URL** (e.g. `https://www.nike.com`)
3. Optionally add a **sample URL** (a product page for analysis)
4. Paste a JSON array of product URLs or upload a `.json` file
5. Set the currency (or leave blank for auto-detect)

From the site detail page you can:
- **Run Full Pipeline** — triggers the complete scrape graph (analysis → code gen → test → execute)
- **Quick Re-run Scraper** — if a scraper has been generated, re-executes it directly (no LLM calls) to refresh data
- **Download Scraper** — download the generated `scraper.py`
- View all scrape jobs and output files for that site

### Submit a Scrape Job

1. Open **http://localhost:8000**
2. Enter a **site URL** (e.g. `https://www.nike.com`)
3. Optionally enter a **product listing URL** to auto-discover products
4. Click **Scrape**

### Monitor Progress

The pipeline runs through these stages:

```
Accessibility Check → Site Analysis → Product Analysis → Scraper Analysis
→ Code Generation → Testing → Field Confirmation → Execution → Cleanup
```

Each stage shows in real-time on the job page, including tool calls and agent logs.

### Human Approval

Some jobs require approval at certain stages (low confidence analysis, field confirmation, pre-execution). Approve via the web UI when prompted.

### Running Generated Scrapers Standalone

Scrapers are saved to `scrapers/{site-slug}/` and can be run independently:

```bash
# Inside the docker network
docker compose exec celery-worker python3 /app/scrapers/nike-com/scraper.py

# With custom input
python3 scrapers/nike-com/scraper.py --urls "https://nike.com/product/1" "https://nike.com/product/2"
```

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and edit:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ZAI_API_KEY` | Yes | — | LLM API key (OpenAI-compatible) |
| `ZAI_BASE_URL` | No | `https://api.z.ai/api/coding/paas/v4/` | LLM API base URL |
| `ZAI_MAIN_MODEL` | No | `glm-5-turbo` | Model for main agents |
| `ZAI_SMALL_MODEL` | No | `glm-5-turbo` | Model for small/utility agents |
| `SECRET_KEY` | No | dev default | Django secret key |
| `DB_PASSWORD` | No | `scraper` | PostgreSQL password |
| `DEBUG` | No | `False` | Django debug mode |
| `DJANGO_SUPERUSER_PASSWORD` | No | `admin` | Password for auto-created admin user |
| `PROXY_DATACENTER_USER` | No | — | Bright Data DC proxy username |
| `PROXY_DATACENTER_PASS` | No | — | Bright Data DC proxy password |
| `PROXY_RESIDENTIAL_USER` | No | — | Bright Data residential proxy username |
| `PROXY_RESIDENTIAL_PASS` | No | — | Bright Data residential proxy password |

### Proxy Configuration

Proxy credentials are passed as environment variables (picked up by `browser-service`). Alternatively, create `config/proxy.json` for local development (this file is gitignored):

```json
{
  "datacenter": {
    "username": "brd-customer-xxx-zone-xxx",
    "password": "xxx",
    "host": "brd.superproxy.io",
    "port": 22225
  },
  "residential": {
    "username": "brd-customer-xxx-zone-xxx",
    "password": "xxx",
    "host": "brd.superproxy.io",
    "port": 22225
  }
}
```

## Project Structure

```
u-ecom-scraper/
├── docker-compose.yml           # Single compose — all 7 services
├── Dockerfile                   # Django + Celery (python:3.12-slim)
├── .env.example                  # Environment variable template
├── webapp/                       # Django application
│   ├── agents/                   # LangGraph graph, agents, tools
│   │   ├── graph.py              # Graph assembly (19 nodes)
│   │   ├── state.py              # ScrapeState TypedDict
│   │   ├── subagents.py          # Agent factories + message builders
│   │   ├── llm.py                # LLM client setup
│   │   ├── nodes/                # Deterministic graph nodes
│   │   └── tools/                # Agent tools (probe_page, web_fetch, etc.)
│   ├── scraper/                  # Django app: models, views, tasks
│   │   ├── models.py             # ScrapeJob, Step, Approval, Site
│   │   ├── tasks.py              # Celery tasks
│   │   ├── services.py           # LangGraphService
│   │   └── views.py              # Django views + SSE streaming
│   └── config/                   # Django settings, Celery config, URLs
├── browser-service/              # Browser automation (FastAPI + Chrome)
│   ├── Dockerfile                # Chrome + Xvfb + SeleniumBase + Playwright
│   ├── server.py                 # /probe, /scrape, /health endpoints
│   ├── browser_pool.py           # Chrome lifecycle management
│   ├── probe.py                  # 7-step auto-escalating page probe
│   └── scraper_runner.py         # Run browser scrapers as subprocesses
├── src/                          # Shared libraries
│   ├── proxy.py                  # Proxy configuration + URL builder
│   └── page_analysis.py          # Common selectors, JSON-LD extraction
├── templates/                    # Scraper code templates (5 strategies)
├── scrapers/                     # Generated per-site scrapers + data
├── .opencode/                    # Agent prompts + skills
│   ├── agents/                   # 8 agent definition files
│   └── skills/                   # Platform detection + tool skills
├── data/                         # Site tracker seed data
├── docs/                         # Architecture documentation
└── tests/                        # Test suite
```

## Development

### UI Design System

The web UI uses a dark "operations console" theme with no static file build step:

- **Tailwind CSS** is loaded via CDN (`cdn.tailwindcss.com`) with a custom inline config in `webapp/scraper/templates/scraper/base.html`
- **Design tokens** (colors, spacing, typography) are defined inline in the `<style>` block of `base.html` — no separate CSS files to compile
- **Fonts**: Inter (UI) and JetBrains Mono (data/code) from Google Fonts
- **Django admin** is themed via `webapp/scraper/templates/admin/base_site.html`, which overrides `{% block extrastyle %}` with the same dark palette — no static file configuration needed

To customize the theme, edit the CSS variables and Tailwind config in `base.html`. All pages inherit from this base template. Key tokens:

| Token | Hex | Purpose |
|-------|-----|---------|
| `bg-void` | `#0B0F17` | Page background |
| `bg-base` | `#111827` | Panel/card surfaces |
| `bg-raised` | `#1A2332` | Hover/elevated |
| `accent` | `#22D3EE` | Primary actions, links, live states |
| `border-subtle` | `#1E293B` | Default borders |
| `text-primary` | `#F1F5F9` | Body text |

See `DESIGN.md` for the complete design system specification.

### Linting & Formatting

```bash
docker compose exec django ruff check webapp/ src/
docker compose exec django ruff format webapp/ src/
```

### Running Tests

```bash
docker compose exec django pytest webapp/
```

### Common Operations

```bash
# Rebuild after code changes
docker compose --profile full up --build -d

# View worker logs (follow)
docker compose logs -f celery-worker

# View browser service logs
docker compose logs -f browser-service

# Restart a single service
docker compose restart celery-worker

# Stop everything
docker compose --profile full down

# Reset database (destroys all data)
docker compose down -v
```

## Output Format

Each completed scrape produces a timestamped JSON file at `scrapers/{site-slug}/output_{datetime}.json`:

```json
{
  "site": {
    "name": "Nike",
    "url": "https://www.nike.com",
    "platform": "custom",
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
      "url": "https://www.nike.com/product/air-max-90"
    }
  ],
  "metadata": {
    "scraping_duration_seconds": 120,
    "failed_products": 0
  }
}
```

Output files are versioned with timestamps so you can track price changes across runs.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full architecture diagram including graph flow, inter-container communication, tool matrix, and design decisions.

## Skills System

The `.opencode/skills/` directory contains reusable detection and technique modules (Shopify, SFCC, Algolia, Kibo, Amazon, anti-bot handling, proxy config, etc.). These are loaded into agent prompts to help with platform detection and scraping strategy selection.
