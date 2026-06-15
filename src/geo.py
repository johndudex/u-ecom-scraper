import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

COUNTRY_CODES = frozenset({
    "au", "nz", "ca", "de", "fr", "br", "mx", "jp", "sg", "hk",
    "in", "ie", "es", "it", "nl", "tr", "za", "se", "ch", "at",
    "be", "dk", "fi", "no", "pl", "pt", "ar", "cl", "pe", "us",
    "ru", "cn", "kr", "th", "id", "my", "ph", "vn", "ae", "sa",
    "il", "eg", "ng", "ke", "gh", "co", "cr",
})

COUNTRY_REMAP = {
    "uk": "gb",
}


def detect_country(url: str) -> Optional[str]:
    hostname = urlparse(url).hostname or ""
    if not hostname:
        return None
    parts = hostname.rstrip(".").split(".")
    if len(parts) < 2:
        return None
    tld = parts[-1].lower()
    if tld in COUNTRY_REMAP:
        return COUNTRY_REMAP[tld]
    if tld in COUNTRY_CODES:
        return tld
    return None
