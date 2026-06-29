#!/usr/bin/env python3
"""Scraper for Wild Secrets Australia (www.wildsecrets.com.au).

Extracts product data from product pages using HTTP requests + BeautifulSoup.
Primary extraction from JSON-LD structured data, with CSS fallbacks for
fields not in JSON-LD (original_price, description, full title).

Usage:
    python3 scraper_draft.py
    python3 scraper_draft.py --sample
    python3 scraper_draft.py --limit 10
    python3 scraper_draft.py --input custom_urls.json
    python3 scraper_draft.py --urls "https://www.wildsecrets.com.au/p/234785/..."
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITE_NAME = "Wild Secrets Australia"
SITE_URL = "https://www.wildsecrets.com.au"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
DELAY = 2.0  # seconds between requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, "wildsecrets-com-au.log")
os.makedirs(LOG_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
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
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Product:
    """Standard product data container."""

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
    brand: str = ""
    sku: str = ""
    description: str = ""
    images: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------


def extract_jsonld_product(html: str) -> Optional[dict]:
    """Extract the Product JSON-LD block from HTML.

    Args:
        html: Raw HTML string.

    Returns:
        Parsed JSON-LD Product dict, or None if not found.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for script_tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script_tag.string)
                if isinstance(data, dict) and data.get("@type") == "Product":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "Product":
                            return item
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception as e:
        logger.warning(f"Error parsing JSON-LD: {e}")
    return None


# ---------------------------------------------------------------------------
# Soft 404 detection
# ---------------------------------------------------------------------------

SOFT_404_PATTERNS = [
    "page not found",
    "product not found",
    "no longer available",
    "discontinued",
    "out of stock",
    "item unavailable",
    "404",
    "error",
]


def detect_soft_404(html: str, soup: BeautifulSoup, final_url: str, request_url: str) -> Optional[str]:
    """Detect soft 404 / deleted product pages.

    Args:
        html: Raw HTML string.
        soup: Parsed BeautifulSoup object.
        final_url: The URL after redirects.
        request_url: The originally requested URL.

    Returns:
        A remarks string if soft 404 detected, else None.
    """
    # Check if final URL diverged significantly from requested product URL
    parsed_request = urlparse(request_url)
    parsed_final = urlparse(final_url)
    if parsed_request.path != parsed_final.path:
        # Redirected to a different path — might be a search/category page
        if "/p/" not in parsed_final.path:
            return "Soft 404: redirected to non-product page"

    # Check page title
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True).lower()
        for pattern in SOFT_404_PATTERNS:
            if pattern in title_text:
                return f"Soft 404: page title contains '{pattern}'"

    # Check H1
    h1_tag = soup.find("h1")
    if h1_tag:
        h1_text = h1_tag.get_text(strip=True).lower()
        for pattern in SOFT_404_PATTERNS:
            if pattern in h1_text:
                return f"Soft 404: H1 contains '{pattern}'"

    # Check for JSON-LD Product type — if no Product JSON-LD, likely not a product page
    jsonld = extract_jsonld_product(html)
    if jsonld is None:
        # Double-check with body text
        body = soup.find("body")
        if body:
            body_text = body.get_text(strip=True).lower()[:500]
            for pattern in SOFT_404_PATTERNS:
                if pattern in body_text:
                    return f"Soft 404: no Product JSON-LD and body contains '{pattern}'"
        return "Soft 404: no Product JSON-LD found on page"

    return None


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def extract_title(soup: BeautifulSoup, jsonld: Optional[dict]) -> str:
    """Extract full product title. Prefer CSS (includes brand prefix and special chars).

    Args:
        soup: Parsed BeautifulSoup object.
        jsonld: Parsed JSON-LD Product dict (may be None).

    Returns:
        Product title string.
    """
    # CSS: .main-details h1.title span
    try:
        el = soup.select_one(".main-details h1.title span")
        if el:
            text = el.get_text(strip=True)
            if text and len(text) >= 5:
                return text
    except Exception:
        pass

    # Fallback: JSON-LD name
    if jsonld:
        name = jsonld.get("name", "")
        if name and len(name) >= 5:
            return name

    # Last resort: <title> tag
    try:
        title_tag = soup.find("title")
        if title_tag:
            text = title_tag.get_text(strip=True)
            # Format: "brand product-name | Wild Secrets Australia"
            if " | " in text:
                text = text.split(" | ")[0].strip()
            return text
    except Exception:
        pass

    return ""


def extract_price(soup: BeautifulSoup, jsonld: Optional[dict]) -> tuple[str, str]:
    """Extract price and currency. Use JSON-LD (rounded to 2 decimals), CSS fallback.

    Args:
        soup: Parsed BeautifulSoup object.
        jsonld: Parsed JSON-LD Product dict (may be None).

    Returns:
        Tuple of (price_with_currency_symbol, currency_code).
    """
    currency = "AUD"

    # Try JSON-LD first
    if jsonld:
        offers = jsonld.get("offers", {})
        if isinstance(offers, dict):
            price_val = offers.get("price", "")
            curr_val = offers.get("priceCurrency", "")
            if curr_val:
                currency = curr_val
            if price_val:
                try:
                    price_float = float(price_val)
                    price_str = f"{price_float:,.2f}"
                    currency_symbol = _currency_symbol(currency)
                    return f"{currency_symbol}{price_str}", currency
                except (ValueError, TypeError):
                    pass

    # CSS fallback: .main-details .price .amount + .cents
    try:
        main_details = soup.select_one(".main-details")
        if main_details:
            amount_el = main_details.select_one(".price .amount")
            cents_el = main_details.select_one(".price .cents span")
            if amount_el:
                amt_text = amount_el.get_text(strip=True).lstrip("$")
                cents_text = ""
                if cents_el:
                    cents_text = cents_el.get_text(strip=True)
                if amt_text:
                    try:
                        # amt_text may already have $ or just digits
                        amt_float = float(amt_text.replace(",", ""))
                        if cents_text:
                            cents_float = float(cents_text.lstrip(".").replace(",", ""))
                            price_float = amt_float + cents_float / 100
                        else:
                            price_float = amt_float
                        price_str = f"{price_float:,.2f}"
                        currency_symbol = _currency_symbol(currency)
                        return f"{currency_symbol}{price_str}", currency
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass

    return "", currency


def _currency_symbol(code: str) -> str:
    """Map ISO 4217 currency code to symbol."""
    symbols = {
        "AUD": "$",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "NZD": "$",
        "CAD": "$",
    }
    return symbols.get(code, code + " ")


def extract_original_price(soup: BeautifulSoup) -> str:
    """Extract original/compare-at price from CSS (not in JSON-LD).

    Args:
        soup: Parsed BeautifulSoup object.

    Returns:
        Original price string with currency symbol, or empty string if not on sale.
    """
    try:
        el = soup.select_one(".main-details .dont-pay span")
        if el:
            text = el.get_text(strip=True)
            if text and text.startswith("$"):
                return text
    except Exception:
        pass
    return ""


def extract_availability(soup: BeautifulSoup, jsonld: Optional[dict]) -> str:
    """Extract stock availability.

    Args:
        soup: Parsed BeautifulSoup object.
        jsonld: Parsed JSON-LD Product dict (may be None).

    Returns:
        Normalized availability: 'In Stock' or 'Out of Stock'.
    """
    # JSON-LD
    if jsonld:
        offers = jsonld.get("offers", {})
        if isinstance(offers, dict):
            avail = offers.get("availability", "")
            if "InStock" in avail:
                return "In Stock"
            if "Out" in avail or "OutOfStock" in avail:
                return "Out of Stock"

    # CSS fallback: search for text pattern in product-information-container
    try:
        container = soup.select_one(".product-information-container")
        if container:
            text = container.get_text()
            if re.search(r"\bin stock\b", text, re.IGNORECASE):
                return "In Stock"
            if re.search(r"\bout of stock\b", text, re.IGNORECASE):
                return "Out of Stock"
    except Exception:
        pass

    return ""


def extract_brand(soup: BeautifulSoup, jsonld: Optional[dict]) -> str:
    """Extract brand name.

    Args:
        soup: Parsed BeautifulSoup object.
        jsonld: Parsed JSON-LD Product dict (may be None).

    Returns:
        Brand name string.
    """
    # JSON-LD
    if jsonld:
        brand = jsonld.get("brand", {})
        if isinstance(brand, dict):
            name = brand.get("name", "")
            if name:
                return name
        elif isinstance(brand, str) and brand:
            return brand

    # CSS fallback
    try:
        el = soup.select_one(".main-details h6.brand a")
        if el:
            text = el.get_text(strip=True)
            if text:
                return text
    except Exception:
        pass

    return ""


def extract_sku(soup: BeautifulSoup, jsonld: Optional[dict]) -> str:
    """Extract SKU / item code.

    Args:
        soup: Parsed BeautifulSoup object.
        jsonld: Parsed JSON-LD Product dict (may be None).

    Returns:
        SKU string.
    """
    # JSON-LD
    if jsonld:
        sku = jsonld.get("sku", "")
        if sku:
            return str(sku)

    # CSS fallback: search for "Item Code:" pattern
    try:
        container = soup.select_one(".product-information-container")
        if container:
            text = container.get_text()
            m = re.search(r"Item Code:\s*(\S+)", text)
            if m:
                return m.group(1)
    except Exception:
        pass

    return ""


def extract_description(soup: BeautifulSoup) -> str:
    """Extract product description from CSS (.description p elements).

    Args:
        soup: Parsed BeautifulSoup object.

    Returns:
        Multi-paragraph description string joined by newlines.
    """
    try:
        desc_container = soup.select_one(".description")
        if desc_container:
            paragraphs = desc_container.find_all("p")
            texts = [p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)]
            if texts:
                return "\n".join(texts)
    except Exception:
        pass
    return ""


def extract_images(soup: BeautifulSoup, jsonld: Optional[dict]) -> list[str]:
    """Extract product image URLs from JSON-LD (scoped to product gallery).

    Args:
        soup: Parsed BeautifulSoup object.
        jsonld: Parsed JSON-LD Product dict (may be None).

    Returns:
        List of full image URLs.
    """
    images = []

    # JSON-LD images
    if jsonld:
        img_data = jsonld.get("image", [])
        if isinstance(img_data, str):
            img_data = [img_data]
        for img_url in img_data:
            if isinstance(img_url, str):
                # Fix protocol-relative URLs
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                # Only include product images (from media.exciteonlineservices.com.au)
                if "/products/" in img_url.lower():
                    images.append(img_url)

    # CSS fallback: images in product gallery / add-to-cart area
    if not images:
        try:
            gallery = soup.select_one("#add-to-cart-container") or soup.select_one(".product-gallery")
            if gallery:
                for img_tag in gallery.find_all("img"):
                    src = img_tag.get("src") or img_tag.get("data-src") or ""
                    if src:
                        if src.startswith("//"):
                            src = "https:" + src
                        src = urljoin(SITE_URL, src)
                        if "/products/" in src.lower():
                            images.append(src)
        except Exception:
            pass

    return images


def detect_sale_status(soup: BeautifulSoup) -> bool:
    """Check if the product is on sale based on CSS indicators.

    Args:
        soup: Parsed BeautifulSoup object.

    Returns:
        True if product is on sale.
    """
    try:
        # Check for .sale badge in .main-details .price
        sale_badge = soup.select_one(".main-details .price .sale")
        if sale_badge:
            return True

        # Check for "on-sale" class on price container
        price_container = soup.select_one(".main-details .price")
        if price_container and "on-sale" in price_container.get("class", []):
            return True

        # Check for .dont-pay container (original price display)
        dont_pay = soup.select_one(".main-details .dont-pay")
        if dont_pay:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main scraping function
# ---------------------------------------------------------------------------


def scrape_product(session: requests.Session, url: str, src_url: str, product_id: int) -> Product:
    """Scrape a single product page.

    Args:
        session: Requests session with headers configured.
        url: Product page URL.
        src_url: Source URL (from input_urls.json).
        product_id: Sequential product ID.

    Returns:
        Product dataclass with extracted data.
    """
    product = Product(id=product_id, url=url, src_url=src_url)

    try:
        time.sleep(DELAY)
        response = session.get(url, timeout=20, allow_redirects=True)
        product.status_code = response.status_code

        if response.status_code != 200:
            product.remarks = f"HTTP {response.status_code}"
            return product

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        final_url = response.url

        # Soft 404 detection
        soft_404 = detect_soft_404(html, soup, final_url, url)
        if soft_404:
            product.remarks = soft_404
            return product

        # Extract JSON-LD
        jsonld = extract_jsonld_product(html)

        # Title (CSS preferred over JSON-LD for full title with brand)
        product.title = extract_title(soup, jsonld)

        # Price and currency
        price, currency = extract_price(soup, jsonld)
        product.price = price
        product.currency = currency

        # Original price (CSS only)
        product.original_price = extract_original_price(soup)

        # If no original_price from CSS, ensure it's empty string
        if not product.original_price:
            product.original_price = ""

        # Availability
        product.availability = extract_availability(soup, jsonld)

        # Brand
        product.brand = extract_brand(soup, jsonld)

        # SKU
        product.sku = extract_sku(soup, jsonld)

        # Description
        product.description = extract_description(soup)

        # Images
        product.images = extract_images(soup, jsonld)

        # Remarks: note if on sale
        if detect_sale_status(soup):
            if product.remarks:
                product.remarks += "; On sale"
            else:
                product.remarks = "On sale"

        # Timestamp
        product.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    except requests.RequestException as e:
        product.remarks = f"Request error: {e}"
        logger.error(f"Request error for {url}: {e}")
    except Exception as e:
        product.remarks = f"Extraction error: {e}"
        logger.error(f"Extraction error for {url}: {e}")

    return product


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def save_results(results: list[Product], start_time: float) -> str:
    """Save results to output JSON file.

    Args:
        results: List of Product dataclasses.
        start_time: Unix timestamp when scraping started.

    Returns:
        Path to the output file.
    """
    import datetime

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    success = sum(1 for p in results if p.title and not p.remarks)
    failed = sum(1 for p in results if not p.title or p.remarks)

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "products": [
            {
                "id": p.id,
                "title": p.title,
                "price": p.price,
                "availability": p.availability,
                "original_price": p.original_price,
                "currency": p.currency,
                "url": p.url,
                "src_url": p.src_url,
                "location": p.location,
                "status_code": p.status_code,
                "scraped_at": p.scraped_at,
                "remarks": p.remarks,
                "brand": p.brand,
                "sku": p.sku,
                "description": p.description,
                "images": p.images,
            }
            for p in results
        ],
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed,
            "rate_limit_delay": DELAY,
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(f"Output saved to: {output_file}")
    return output_file


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=f"Scraper for {SITE_NAME}")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape only first 5 products")
    parser.add_argument("--limit", type=int, help="Max products to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="Do not use proxy (default)")
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Load URLs
    if args.urls:
        urls = args.urls
    elif args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
                urls = data["urls"]
        except Exception as e:
            logger.error(f"Failed to read input file {args.input}: {e}")
            sys.exit(1)
    else:
        # Default: read input_urls.json from script directory
        default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        try:
            with open(default_input, "r", encoding="utf-8") as f:
                data = json.load(f)
                urls = data["urls"]
        except Exception as e:
            logger.error(f"Failed to read default input file {default_input}: {e}")
            sys.exit(1)

    # Apply --sample and --limit
    if args.sample:
        urls = urls[:5]
        logger.info("Sample mode: scraping first 5 URLs")
    elif args.limit:
        urls = urls[: args.limit]
        logger.info(f"Limit mode: scraping up to {args.limit} URLs")

    if not urls:
        logger.error("No URLs to scrape. Exiting.")
        sys.exit(1)

    # Log start
    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Total products: {len(urls)}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Rate limit delay: {DELAY}s")
    logger.info("=" * 80)

    # Create HTTP session
    session = requests.Session()
    session.headers.update(HEADERS)

    # Scrape products
    results: list[Product] = []
    start_time = time.time()

    for idx, url in enumerate(urls, start=1):
        logger.info(f"[{idx}/{len(urls)}] Scraping: {url}")
        product = scrape_product(session, url, url, idx)
        results.append(product)

        status = "OK" if product.title and not product.remarks else f"ISSUE: {product.remarks}"
        logger.info(f"  -> {product.title[:80]} | {product.price} | {status}")

        # Progress every 25 products
        if len(results) % 25 == 0:
            percent = (len(results) / len(urls)) * 100
            logger.info(f"Progress: [{len(results)}/{len(urls)}] ({percent:.1f}%)")

    # Save results
    output_path = save_results(results, start_time)

    # Log completion
    success = sum(1 for p in results if p.title and not p.remarks)
    failed = sum(1 for p in results if not p.title or p.remarks)
    duration = round(time.time() - start_time, 2)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success}, Failed: {failed}")
    logger.info(f"Duration: {duration}s")
    logger.info(f"Output: {output_path}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
