# PRODUCT.md

## What this is

**u-ecom-scraper** — an agentic scraper builder. Users configure a target site and a page type (product, article, job, forum, SERP, etc.), and a multi-agent pipeline (LangGraph) autonomously analyzes the site, builds a scraper, tests it, and extracts structured data. Jobs run with live agent logs, pipeline-phase timelines, tool-call traces, and human-approval interrupts.

## Register

**Product.** The interface serves the workflow, not a brand identity. Form follows function — but function here is complex and powerful, so the form should reflect that power rather than hide it.

## Users

**Small data team (2-5 people).** Operators who:
- Launch scrapes across multiple sites and monitor them concurrently
- Need to understand agent decisions at a glance (what phase, what tool, what result)
- Handle approval interrupts when agents hit budget exhaustion or ambiguity
- Share results and hand off jobs between team members
- Are technical but not necessarily reading agent logs line-by-line — they need summaries, status, and drill-down on demand

## Personality: "Operations console with edge"

The tool is powerful. The UI should make that legible.

- **Dense, not cluttered.** Information-rich layouts with clear visual hierarchy. Every pixel earns its place.
- **Mission-control energy.** Status colors, phase indicators, live-updating traces — the interface should feel like it's *alive* and *working*, not static.
- **Confident typography.** Strong size/weight contrast. Monospace where data lives (URLs, JSON, agent output), sans for navigation and prose.
- **One accent color, used with intent.** Not a rainbow. The accent marks actionable elements and live states; everything else is structural.
- **Depth through layering, not flatness.** Subtle elevation, borders, and contrast bands to separate regions — not card-grid soup.

### What it is NOT

- Not a generic SaaS dashboard (no Inter font, no indigo gradients, no 3-up stat cards with up-arrow tooltips)
- Not overly minimal — this is a powerful tool, the UI should signal that
- Not a spreadsheet — data needs structure, hierarchy, and breathing room

## Key surfaces

| Surface | Purpose | Priority |
|---------|---------|----------|
| **Dashboard / Home** | Launch new scrape, see active jobs at a glance | Hub — first impression |
| **Job list** | Filter/sort all jobs, spot failures, track status | Monitoring |
| **Job detail** | Live agent logs, phase timeline, tool calls, approval interrupts | Core workspace — highest density |
| **Site list / detail** | Manage configured sites, view scrape history | Configuration |
| **Approvals** | Review and act on agent interrupts | Workflow gate |
| **Probe tester** | Test extraction methods before committing | Diagnostic |

The **job detail** page is the center of gravity — it's where the agentic nature of the tool is most visible and most needs to be legible.

## Accessibility

Basic best practices: reasonable contrast (aim for 4.5:1 on body text), full keyboard navigation, visible focus states, semantic HTML. No formal compliance target, but no dark-pattern contrast either.

## Constraints

- Django templates (server-rendered) — no SPA framework
- Tailwind CSS (currently via CDN; may move to build step)
- Must work in the existing Django template structure under `webapp/scraper/templates/scraper/`
- No backend changes for the redesign — UI-only
