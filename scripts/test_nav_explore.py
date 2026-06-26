"""Test navigation_explore agent against calvinklein.co.uk with site_analysis."""
import os
import sys
import json
import shutil
import logging
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app/webapp")
os.chdir("/app")
django.setup()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

SRC_SLUG = "calvinklein-co-uk"
SLUG = "calvinklein-co-uk-test5"
WS = os.path.join("workspace", SLUG)
SRC_WS = os.path.join("workspace", SRC_SLUG)

if os.path.isdir(WS):
    shutil.rmtree(WS)
os.makedirs(WS, exist_ok=True)

src_sa = os.path.join(SRC_WS, "site_analysis.json")
dst_sa = os.path.join(WS, "site_analysis.json")
if os.path.exists(src_sa):
    shutil.copy2(src_sa, dst_sa)
    logger.info("Copied site_analysis.json")
else:
    logger.warning("No site_analysis.json at %s", src_sa)

from agents.nodes.navigate_explore import navigate_explore

state = {
    "job_id": 0,
    "url": "https://www.calvinklein.co.uk",
    "site_slug": SLUG,
    "search_criteria": "watches",
    "input_mode": "navigation",
    "sample_url": "",
    "product_url": "",
}

logger.info("Starting navigate_explore (slug=%s)...", SLUG)
try:
    result = navigate_explore(state)
    logger.info("Result keys: %s", list(result.keys())[:10])
except Exception as exc:
    logger.exception("navigate_explore failed: %s", exc)

findings_path = os.path.join(WS, "navigation_findings.json")
if os.path.exists(findings_path):
    with open(findings_path) as f:
        findings = json.load(f)
    prod_links = findings.get("listing_page", {}).get("product_links", [])
    logger.info("Product links found: %d", len(prod_links))
    for i, p in enumerate(prod_links[:10]):
        logger.info("  %d. %s -> %s", i + 1, p.get("text", "")[:60], p.get("href", "")[:120])
    json_ld = findings.get("listing_page", {}).get("json_ld", {})
    if json_ld:
        logger.info("JSON-LD products: %d", len(json_ld.get("products", [])))
        for j, p in enumerate(json_ld["products"][:3]):
            logger.info("  JL %d. %s -> %s", j + 1, p.get("text", "")[:60], p.get("href", "")[:120])
    logger.info("Errors: %s", findings.get("errors", []))
    logger.info("URL: %s", findings.get("listing_page", {}).get("url", ""))
    logger.info("Method: %s", findings.get("method", ""))
    logger.info("Search attempted: %s", findings.get("search_attempted", False))
else:
    logger.error("No findings file")
