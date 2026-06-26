import logging
import re
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

COUNTRY_CODES = frozenset(
    {
        "au",
        "nz",
        "ca",
        "de",
        "fr",
        "br",
        "mx",
        "jp",
        "sg",
        "hk",
        "in",
        "ie",
        "es",
        "it",
        "nl",
        "tr",
        "za",
        "se",
        "ch",
        "at",
        "be",
        "dk",
        "fi",
        "no",
        "pl",
        "pt",
        "ar",
        "cl",
        "pe",
        "us",
        "ru",
        "cn",
        "kr",
        "th",
        "id",
        "my",
        "ph",
        "vn",
        "ae",
        "sa",
        "il",
        "eg",
        "ng",
        "ke",
        "gh",
        "co",
        "cr",
        "gb",
    }
)

COUNTRY_REMAP = {
    "uk": "gb",
}

# Matches locale path segments like /en-us, /en-gb, /fr-fr, /de-de, /en_US.
# Captures the 2-letter country code in group "country".
_LOCALE_PATH_RE = re.compile(r"^[a-z]{2}[-_](?P<country>[a-z]{2})$", re.IGNORECASE)


def detect_country(url: str) -> Optional[str]:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if hostname:
        parts = hostname.rstrip(".").split(".")
        if len(parts) >= 2:
            tld = parts[-1].lower()
            if tld in COUNTRY_REMAP:
                return COUNTRY_REMAP[tld]
            if tld in COUNTRY_CODES:
                return tld

    # TLD did not resolve a country (e.g. ".com", ".net"). Fall back to a
    # locale prefix in the URL path, e.g. /en-us -> us, /fr-fr -> fr.
    path = (parsed.path or "").lstrip("/")
    first_segment = path.split("/", 1)[0] if path else ""
    match = _LOCALE_PATH_RE.match(first_segment)
    if match:
        country = match.group("country").lower()
        return COUNTRY_REMAP.get(country, country if country in COUNTRY_CODES else None)
    return None
