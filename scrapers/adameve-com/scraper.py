#!/usr/bin/env python3
"""Adam & Eve product scraper.

Extracts product data from adameve.com product pages using HTTP requests.
All product data is server-rendered — no JavaScript execution needed.

Output fields: title, price, original_price, availability, currency, url,
src_url, location, status_code, scraped_at, remarks, plus optional fields
(images, brand, sku, category, rating, review_count, key_features,
specifications, color_variants).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITE_NAME = "Adam & Eve"
SITE_URL = "https://www.adameve.com"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
DELAY = 2.0  # seconds between requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "adameve-com.log")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

STANDARD_FIELDS = [
    "id", "title", "price", "availability", "original_price", "currency",
    "url", "src_url", "location", "status_code", "scraped_at", "remarks",
]

OPTIONAL_FIELDS = [
    "images", "brand", "sku", "category", "rating", "review_count",
    "key_features", "specifications", "color_variants", "description",
]


@dataclass
class Product:
    """Product data container with standard and optional fields."""

    id: int = 0
    title: str = ""
    price: str = ""
    availability: str = ""
    original_price: str = ""
    currency: str = ""
    url: str = ""
    src_url: str = ""
    location: str = ""
    status_code: int = 0
    scraped_at: str = ""
    remarks: str = ""
    # Optional fields
    images: list[str] = field(default_factory=list)
    brand: str = ""
    sku: str = ""
    category: list[str] = field(default_factory=list)
    rating: str = ""
    review_count: str = ""
    key_features: list[str] = field(default_factory=list)
    specifications: dict[str, str] = field(default_factory=dict)
    color_variants: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary, including only non-empty optional fields."""
        d: dict = {}
        for f in STANDARD_FIELDS:
            d[f] = getattr(self, f)
        for f in OPTIONAL_FIELDS:
            val = getattr(self, f)
            if val and val != "" and val != [] and val != {}:
                d[f] = val
        return d


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def get_meta_content(soup: BeautifulSoup, property_name: str) -> str:
    """Extract content attribute from a <meta> tag by property name."""
    tag = soup.find("meta", attrs={"property": property_name})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


def get_text_safe(el: Optional[Tag]) -> str:
    """Safely extract stripped text from a BeautifulSoup element."""
    if el is None:
        return ""
    return el.get_text(strip=True)


def normalize_availability(raw: str) -> str:
    """Normalize availability to 'In Stock' or 'Out of Stock'."""
    lower = raw.lower().strip()
    if lower in ("instock", "in stock", "in-stock", "in stock now"):
        return "In Stock"
    if lower in ("outofstock", "out of stock", "out-of-stock"):
        return "Out of Stock"
    if lower in ("preorder", "pre-order", "backorder", "back-order"):
        return "In Stock"  # Pre-order still means purchasable
    return "In Stock" if "instock" in lower else "Out of Stock"


def format_price(raw: str) -> str:
    """Ensure a price string includes the $ currency symbol."""
    raw = raw.strip()
    if not raw:
        return ""
    if not raw.startswith("$"):
        return f"${raw}"
    return raw


def detect_soft_404(soup: BeautifulSoup, final_url: str, requested_url: str) -> tuple[bool, str]:
    """Detect soft 404 / product-not-found pages.

    Returns (is_404, reason_string).
    """
    # Check URL redirect
    parsed_requested = urlparse(requested_url)
    parsed_final = urlparse(final_url)
    if (
        parsed_final.path != parsed_requested.path
        and parsed_final.path.find("/sp-") == -1
    ):
        return True, "Soft 404: redirected to non-product page"

    # Check for product indicators
    og_type = get_meta_content(soup, "og:type")
    if og_type and og_type.strip() != "product":
        return True, f"Soft 404: og:type is '{og_type}', not 'product'"

    # Check H1 / title for not-found indicators
    title_text = ""
    h1 = soup.find("h1")
    if h1:
        title_text = h1.get_text(strip=True).lower()
    not_found_phrases = [
        "not found", "unavailable", "discontinued",
        "no longer available", "page not found", "product not found",
        "item not found", "error",
    ]
    for phrase in not_found_phrases:
        if phrase in title_text:
            return True, f"Soft 404: H1 contains '{phrase}'"

    page_title = soup.title.get_text(strip=True).lower() if soup.title else ""
    for phrase in not_found_phrases:
        if phrase in page_title:
            return True, f"Soft 404: page title contains '{phrase}'"

    return False, ""


def extract_product_id_from_url(url: str) -> str:
    """Extract the numeric product ID from the URL pattern sp-{slug}-{id}.aspx."""
    match = re.search(r"sp-(?:.*?)-(\d+)\.aspx", url)
    if match:
        return match.group(1)
    return ""


def extract_rating_from_stars(soup: BeautifulSoup) -> str:
    """Extract rating from CSS star classes as fallback."""
    stars_container = soup.select_one(".pdp-star-ratings .ratingStars")
    if not stars_container:
        return ""
    spans = stars_container.find_all("span")
    if not spans:
        return ""
    total = 0.0
    for span in spans:
        classes = span.get("class", [])
        for cls in classes:
            if cls.startswith("ratingStar-"):
                try:
                    val = int(cls.replace("ratingStar-", ""))
                    total += val / 100.0
                except ValueError:
                    pass
                break
    if total > 0:
        return f"{total:.1f}"
    return ""


def extract_review_count_from_text(soup: BeautifulSoup) -> str:
    """Extract review count from parenthesized text as fallback."""
    rating_el = soup.select_one(".ae-star-rating")
    if rating_el:
        match = re.search(r"\(([\d,]+)\)", rating_el.get_text())
        if match:
            return match.group(1)
    return ""


def extract_rating_from_datalayer(html: str) -> str:
    """Extract rating from serverSideEvents metric2 in raw HTML."""
    match = re.search(r"serverSideEvents.*?\"metric2\":([\d.]+)", html, re.DOTALL)
    if match:
        return match.group(1)
    return ""


def extract_review_count_from_datalayer(html: str) -> str:
    """Extract review count from serverSideEvents metric4 in raw HTML."""
    match = re.search(r"serverSideEvents.*?\"metric4\":([\d]+)", html, re.DOTALL)
    if match:
        return match.group(1)
    return ""


def extract_sku_from_html(html: str) -> str:
    """Extract SKU from serverSideEvents JSON or 'Quote item XXX' text.

    Uses simple regex without greedy prefix to avoid multi-block matching.
    """
    # Primary: dataLayer Sku field (simple regex, no greedy .*? prefix)
    match = re.search(r'"Sku":\s*"([A-Z0-9]+)"', html)
    if match:
        return match.group(1)
    # Fallback: 'Quote item <span>E194</span>' pattern
    match = re.search(r"Quote\s+item[^A-Z0-9]*([A-Z0-9]+)", html)
    if match:
        return match.group(1)
    return ""


def extract_breadcrumbs_jsonld(soup: BeautifulSoup) -> list[str]:
    """Extract breadcrumbs from JSON-LD BreadcrumbList."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("@type") == "BreadcrumbList":
            items = data.get("itemListElement", [])
            return [item.get("name", "") for item in items if item.get("name")]
    return []


def extract_breadcrumbs_css(soup: BeautifulSoup) -> list[str]:
    """Extract breadcrumbs from breadcrumb links as fallback."""
    links = soup.select(".ae-breadcrumbs__link, .ae-breadcrumbs a")
    return [a.get_text(strip=True) for a in links if a.get_text(strip=True)]


def extract_first_dollar_amount(text: str) -> str:
    """Extract the first dollar amount from text, returning '$X.XX' format.

    Handles text like '$39.99-$49.99Save Up to $35.02 (71%)' and returns '$39.99'.
    """
    match = re.search(r"\$[\d,.]+", text)
    if match:
        return match.group(0)
    return ""


def extract_first_price_number(text: str) -> str:
    """Extract the first price number (e.g., '7.50') from text.

    Uses r'\\d+\\.\\d{2}' to get only the first price, avoiding concatenation
    issues with multi-price strings like '$7.50 - $11.00'.
    """
    match = re.search(r"\d+\.\d{2}", text)
    if match:
        return match.group(0)
    return ""


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def fetch_page(url: str, session: requests.Session) -> tuple[int, str, str]:
    """Fetch a page and return (status_code, html, final_url)."""
    try:
        time.sleep(DELAY)
        resp = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        return resp.status_code, resp.text, resp.url
    except requests.RequestException as e:
        logger.error(f"Request failed for {url}: {e}")
        return 0, "", url


def extract_product(
    url: str, src_url: str, product_id_num: int, session: requests.Session
) -> Product:
    """Extract all product data from a single product page URL."""
    product = Product(
        id=product_id_num,
        url=url,
        src_url=src_url,
        status_code=0,
        scraped_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    status_code, html, final_url = fetch_page(url, session)
    product.status_code = status_code

    if status_code == 0:
        product.remarks = "Failed to fetch page"
        return product

    if status_code != 200:
        product.remarks = f"HTTP {status_code}"
        return product

    if not html:
        product.remarks = "Empty response body"
        return product

    soup = BeautifulSoup(html, "html.parser")

    # --- Soft 404 detection ---
    is_404, reason = detect_soft_404(soup, final_url, url)
    if is_404:
        product.remarks = reason
        return product

    # --- Title ---
    title_el = soup.select_one(".ae-product-details h1.ae-h1")
    if not title_el:
        title_el = soup.select_one("h1.ae-h1")
    if not title_el:
        # Fallback to first h1
        title_el = soup.find("h1")
    product.title = get_text_safe(title_el)
    if not product.title:
        og_title = get_meta_content(soup, "og:title")
        if og_title:
            # Strip the " - Category | Adam & Eve" suffix
            product.title = re.sub(
                r"\s*[-–]\s*\S+\s*\|\s*Adam\s*&?\s*Eve\s*$", "", og_title
            )

    # --- Price (base / regular from og:price:amount) ---
    og_price = get_meta_content(soup, "og:price:amount")
    if og_price:
        product.price = format_price(og_price)
    else:
        # CSS fallback: normal price
        normal_price_el = soup.select_one(".ae-normal-price .ae-price--normal")
        if normal_price_el:
            product.price = format_price(get_text_safe(normal_price_el))

    # --- Sale price / promo price ---
    sale_price_el = soup.select_one(".jcpoffer-pricerange.v1")
    if sale_price_el:
        sale_text = get_text_safe(sale_price_el)
        # Extract ONLY the first price number to avoid concatenation crash
        sale_num = extract_first_price_number(sale_text)
        base_num = extract_first_price_number(product.price)
        if sale_num and base_num:
            try:
                if float(sale_num) < float(base_num):
                    product.original_price = product.price  # base becomes original
                    product.price = format_price(sale_num)
            except (ValueError, TypeError):
                logger.warning(
                    f"Could not compare sale/base prices: sale={sale_num}, base={base_num}"
                )
    else:
        # No sale price element — check for "was" price indicating sale via
        # .ae-sale-price .ae-price--was
        was_price_el = soup.select_one(".ae-sale-price .ae-price--was")
        if was_price_el:
            was_text = get_text_safe(was_price_el)
            if was_text:
                was_amount = extract_first_dollar_amount(was_text)
                if was_amount:
                    product.original_price = was_amount

    # --- Original price: try og:price:standard_amount as primary ---
    # This is the cleanest source for the standard/original price on sale items.
    # Only use it if we haven't already set original_price from the sale logic.
    if not product.original_price:
        og_standard = get_meta_content(soup, "og:price:standard_amount")
        if og_standard:
            product.original_price = format_price(og_standard)
        else:
            # Fallback: .ae-sale-price .ae-price--was.v1 (the .v1 modifier
            # ensures we get the clean element, not the one with marketing garbage)
            was_v1_el = soup.select_one(".ae-sale-price .ae-price--was.v1")
            if was_v1_el:
                was_v1_text = get_text_safe(was_v1_el)
                if was_v1_text:
                    was_amount = extract_first_dollar_amount(was_v1_text)
                    if was_amount:
                        product.original_price = was_amount

    # --- Currency ---
    product.currency = get_meta_content(soup, "og:price:currency")

    # --- Availability ---
    og_avail = get_meta_content(soup, "og:availability")
    product.availability = normalize_availability(og_avail)

    # --- Brand ---
    product.brand = get_meta_content(soup, "og:brand")

    # --- SKU ---
    product.sku = extract_sku_from_html(html)

    # --- Main image (from og:image) ---
    og_image = get_meta_content(soup, "og:image")

    # --- Images from carousel (DEDUPLICATED) ---
    carousel_imgs: list[str] = []
    carousel = soup.select_one(".ae-product-carousel")
    if carousel:
        for img in carousel.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src and "adameve.com/cms" in src:
                # Skip tiny thumbnails and tracking pixels
                if "1x1" not in src and "0x0" not in src:
                    carousel_imgs.append(src)
    # Deduplicate — carousel has both thumbnail strip and main display
    # with identical img src values
    if carousel_imgs:
        carousel_imgs = list(dict.fromkeys(carousel_imgs))
    if not carousel_imgs and og_image:
        carousel_imgs = [og_image]
    product.images = carousel_imgs

    # --- Description ---
    product.description = get_meta_content(soup, "og:description")

    # --- Breadcrumbs / Category ---
    product.category = extract_breadcrumbs_jsonld(soup)
    if not product.category:
        product.category = extract_breadcrumbs_css(soup)

    # --- Rating ---
    product.rating = extract_rating_from_datalayer(html)
    if not product.rating:
        product.rating = extract_rating_from_stars(soup)

    # --- Review count ---
    product.review_count = extract_review_count_from_datalayer(html)
    if not product.review_count:
        product.review_count = extract_review_count_from_text(soup)

    # --- Key Features ---
    # FIX: Accordion heading is <summary class="ae-accordion__title">,
    # NOT <h2 class="ae-accordion__title">. Use find(class_=...) to match
    # any element with the title class.
    accordions = soup.select(".ae-accordion")
    for accordion in accordions:
        heading = accordion.find(class_="ae-accordion__title")
        heading_text = get_text_safe(heading).lower() if heading else ""
        if "key features" in heading_text:
            content = accordion.find(
                "div", class_=re.compile("ae-accordion__content")
            )
            if content:
                lis = content.find_all("li")
                if lis:
                    product.key_features = [get_text_safe(li) for li in lis]
                else:
                    text = get_text_safe(content)
                    if text:
                        product.key_features = [text]
            break

    # --- Specifications ---
    # FIX: Same <summary> vs <h2> issue — use find(class_=...) instead
    # of find('h2', class_=...).
    for accordion in accordions:
        heading = accordion.find(class_="ae-accordion__title")
        heading_text = get_text_safe(heading).lower() if heading else ""
        if "specifications" in heading_text:
            table = accordion.find("table", class_=re.compile("ae-table"))
            if not table:
                table = accordion.find("table")
            if table:
                rows = table.find_all("tr")
                for row in rows:
                    th = row.find("th")
                    td = row.find("td")
                    if th and td:
                        key = get_text_safe(th)
                        val = get_text_safe(td)
                        if key:
                            product.specifications[key] = val
            break

    # --- Color variants ---
    color_swatch_els = soup.select(
        ".ae-swatch-selector__colors .ae-swatch-selector__container"
    )
    if color_swatch_els:
        product.color_variants = [
            get_text_safe(el) for el in color_swatch_els if get_text_safe(el)
        ]

    # --- URL verification ---
    product_url = get_meta_content(soup, "og:url")
    if product_url:
        product.url = product_url
    else:
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            product.url = canonical["href"]

    return product


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_urls(input_file: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} product scraper")
    parser.add_argument(
        "--input", type=str, default=None,
        help="Path to input URLs JSON file",
    )
    parser.add_argument(
        "--urls", type=str, nargs="+", default=None,
        help="Product URLs as CLI arguments",
    )
    parser.add_argument(
        "--sample", action="store_true", default=False,
        help="Scrape only 5 products (sample mode)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max products to scrape (0 = unlimited)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=True,
        help="Disable proxy (default: no proxy)",
    )
    return parser.parse_args()


def main() -> None:
    """Run the scraper."""
    args = parse_args()

    # Determine product URLs
    if args.urls:
        product_urls = args.urls
    elif args.input:
        product_urls = load_urls(args.input)
    else:
        default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        if os.path.exists(default_input):
            product_urls = load_urls(default_input)
        else:
            logger.error(
                "No input URLs found. Use --input, --urls, or place "
                "input_urls.json in script directory."
            )
            sys.exit(1)

    # Apply limits
    if args.sample and len(product_urls) > 5:
        product_urls = product_urls[:5]
        logger.info("Sample mode: limiting to 5 products")
    if args.limit > 0 and len(product_urls) > args.limit:
        product_urls = product_urls[:args.limit]
        logger.info(f"Limit mode: scraping {args.limit} products")

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped_urls: list[str] = []
    for u in product_urls:
        if u not in seen:
            seen.add(u)
            deduped_urls.append(u)
    product_urls = deduped_urls

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site URL: {SITE_URL}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Total products: {len(product_urls)}")
    logger.info(f"Delay between requests: {DELAY}s")
    logger.info("=" * 80)

    session = requests.Session()
    session.headers.update(HEADERS)

    results: list[dict] = []
    success = 0
    failed = 0
    start_time = time.time()

    for idx, url in enumerate(product_urls, start=1):
        logger.info(f"[{idx}/{len(product_urls)}] Scraping: {url}")
        try:
            product = extract_product(url, url, idx, session)
            product_dict = product.to_dict()
            results.append(product_dict)

            if product.remarks and (
                "Soft 404" in product.remarks or "Failed" in product.remarks
            ):
                failed += 1
                logger.warning(f"  ⚠ {product.remarks}")
            elif product.remarks.startswith("Exception:"):
                failed += 1
                logger.error(f"  ✗ {product.remarks}")
            else:
                success += 1
                logger.info(
                    f"  ✓ {product.title[:60]} | {product.price} | "
                    f"{product.availability}"
                )

        except Exception as e:
            failed += 1
            logger.error(f"  ✗ Error scraping {url}: {e}")
            # Preserve status_code from the product if extract_product
            # got far enough to fetch the page before crashing
            error_product = {
                "id": idx,
                "title": "",
                "price": "",
                "availability": "",
                "original_price": "",
                "currency": "",
                "url": url,
                "src_url": url,
                "location": "",
                "status_code": 0,  # Will try to recover below
                "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "remarks": f"Exception: {e}",
            }
            # Try to recover the actual HTTP status code if the exception
            # was raised inside extract_product after a successful fetch
            try:
                time.sleep(DELAY)
                probe_resp = session.head(
                    url, headers=HEADERS, timeout=15, allow_redirects=True
                )
                error_product["status_code"] = probe_resp.status_code
            except Exception:
                pass
            results.append(error_product)

        # Progress logging
        if idx % 25 == 0 or idx == len(product_urls):
            percent = (idx / len(product_urls)) * 100
            logger.info(f"Progress: [{idx}/{len(product_urls)}] ({percent:.1f}%)")

    duration = round(time.time() - start_time, 2)

    # Build output
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": results,
        "metadata": {
            "scraping_duration_seconds": duration,
            "failed_products": failed,
            "rate_limit_delay": DELAY,
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info(f"EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success}, Failed: {failed}")
    logger.info(f"Duration: {duration}s")
    logger.info(f"Output: {output_file}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
