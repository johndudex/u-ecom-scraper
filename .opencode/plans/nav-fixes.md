# Navigation Pipeline Fixes — 4 Issues

## Fix A: HTML Content Logging + RENDER FAILED Detection
**File:** `webapp/agents/nodes/navigate_explore.py`
**Location:** `_do_explore_via_http()` around line 1959

### Changes:
After `homepage_html = fetch_fn(base_url)`, add validation:
```python
if not homepage_html or homepage_html.startswith("RENDER FAILED"):
    findings["errors"].append(
        f"Homepage fetch failed: {homepage_html[:200] if homepage_html else 'empty response'}"
    )
    logger.error("navigate_explore: homepage fetch returned error: %s",
        (homepage_html[:200] if homepage_html else "(empty)"))
    return findings

logger.info("navigate_explore: homepage HTML len=%d, first 100 chars: %s",
    len(homepage_html), repr(homepage_html[:100]))
```

Same pattern for search page fetch (around line 2070) and category page fetch (around line 2106).

## Fix B: Stop navigation_synthesize Hallucination
**File:** `webapp/agents/subagents.py`
**Location:** `build_navigation_synthesize_message()` around line 834

### Changes:
Replace lines 834-835:
```
- If the findings data is incomplete, make your best inference from what
  is available. Fill in ALL fields with reasonable values.
```
With:
```
- If the findings have 0 category links AND 0 product links, the
  exploration FAILED. Write discovery_method: "failed" and leave all
  selectors and url_examples empty. Do NOT fabricate URLs, selectors,
  or platform-specific details. Empty strings are better than wrong ones.
- Only include url_examples that appear in the findings JSON. Never
  invent URLs.
```

**File:** `webapp/agents/nodes/navigate_synthesize.py`
**Location:** `navigate_synthesize()` around line 55 (before LLM agent invocation)

### Changes:
Add pre-check: if findings have 0 links, skip LLM agent and use fallback:
```python
# Read findings first — if empty, skip LLM hallucination
findings_path = os.path.join(root, "workspace", slug, "navigation_findings.json")
if os.path.isfile(findings_path):
    with open(findings_path) as f:
        raw_findings = json.load(f)
    cat_links = raw_findings.get("homepage_nav", {}).get("category_links", [])
    prod_links = raw_findings.get("listing_page", {}).get("product_links", [])
    if not cat_links and not prod_links:
        logger.warning("navigate_synthesize: findings empty, using fallback (no hallucination)")
        return _fallback_synthesize(state, root, slug)
```

## Fix C: Try All Search URLs + Category Patterns
**File:** `webapp/agents/nodes/navigate_explore.py`

### Change 1: `_do_explore_via_http()` around line 2063
Replace `_build_search_url` (singular, returns first) with `_build_search_urls` (plural, returns all).
Loop through candidates, fetching each until one yields product links:
```python
if search_criteria:
    search_urls = _build_search_urls(search_form_info, search_criteria, base_url, findings["homepage_nav"])
    for search_url in search_urls[:5]:  # try up to 5 candidates
        findings["search_attempted"] = True
        search_html = fetch_fn(search_url)
        if not search_html or search_html.startswith("RENDER FAILED"):
            logger.warning("navigate_explore: search fetch failed for %s", search_url[:100])
            continue
        search_soup = BeautifulSoup(search_html[:500000], "html.parser")
        product_links = _extract_product_links_bs(search_soup, base_url)
        json_ld = _extract_json_ld(search_soup, base_url)
        # ... merge links ...
        if product_links:
            findings["listing_page"]["url"] = search_url
            findings["listing_page"]["product_links"] = product_links
            visited = True
            break
```

### Change 2: After search fails, try common category URL patterns
If locale prefix detected from base_url, try:
```python
if not visited:
    parsed_base = urlparse(base_url)
    locale_prefix = parsed_base.path.strip("/")  # e.g. "en-us"
    if locale_prefix:
        for cat in ["jewelry", "watches", "jewellery", "accessories", "fragrances", "leather-goods"]:
            cat_url = f"{parsed_base.scheme}://{parsed_base.netloc}/{locale_prefix}/{cat}"
            cat_html = fetch_fn(cat_url)
            if cat_html and not cat_html.startswith("RENDER FAILED"):
                cat_soup = BeautifulSoup(cat_html[:500000], "html.parser")
                cat_links_extracted = _extract_product_links_bs(cat_soup, base_url)
                json_ld = _extract_json_ld(cat_soup, base_url)
                if cat_links_extracted or (json_ld and json_ld.get("products")):
                    findings["listing_page"]["url"] = cat_url
                    findings["listing_page"]["product_links"] = cat_links_extracted
                    if json_ld.get("products"):
                        findings["listing_page"]["json_ld"] = json_ld
                    visited = True
                    break
```

## Fix D: Short-Circuit Site-Analyzer on UC Chrome
**File:** `webapp/agents/tools/guards.py`
**Location:** `_BLOCKED_BY_UC_CHROME` message around line 38

### Change:
Update message to be more directive:
```python
_BLOCKED_BY_UC_CHROME = (
    "BLOCKED: This site requires UC Chrome ({method}). "
    "ALL browser/HTTP tools are blocked. "
    "Use the pre-verified probe data already in your prompt to write "
    "your analysis directly. Do NOT attempt any more tool calls — "
    "proceed to write the output file."
)
```

**File:** `.opencode/agents/site-analyzer.md`
**Location:** Around line 25

### Change:
Add stronger instruction:
```
**CRITICAL:** If your first tool call returns "BLOCKED: ... ALL browser/HTTP tools are blocked",
STOP calling tools immediately. Use the pre-verified probe data from your
HumanMessage to write site_analysis.json. Every blocked call wastes ~5 seconds.
```
