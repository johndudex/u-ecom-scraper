import pytest

from src.geo import detect_country


# --- TLD-based detection (existing behavior) ---


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.amazon.co.uk", "gb"),
        ("https://example.com.au", "au"),
        ("https://shop.co.jp", "jp"),
        ("https://store.com.br", "br"),
        ("https://www.bulgari.com", None),
        ("https://example.net", None),
        ("https://localhost", None),
    ],
)
def test_detect_country_tld(url, expected):
    assert detect_country(url) == expected


# --- Path-locale detection (new behavior) ---


@pytest.mark.parametrize(
    "url,expected",
    [
        # Hyphenated locales on generic TLDs
        ("https://www.bulgari.com/en-us/", "us"),
        ("https://www.bulgari.com/en-us/jewelry/serpenti", "us"),
        ("https://shop.example.com/en-gb/products", "gb"),
        ("https://brand.com/fr-fr/bijoux", "fr"),
        ("https://brand.com/de-de/produkte", "de"),
        ("https://brand.com/en-ca/products", "ca"),
        # Underscore variant
        ("https://brand.com/en_US/products", "us"),
        # uk -> gb remap via path
        ("https://brand.com/en-uk/products", "gb"),
        # TLD takes precedence over path locale
        ("https://shop.co.uk/en-us/products", "gb"),
        # Non-locale first path segment -> None
        ("https://brand.com/products/jewelry", None),
        ("https://brand.com/api/v1/products", None),
        ("https://brand.com/shop", None),
        ("https://brand.com/", None),
        # Invalid locale segment (3-letter country) -> None
        ("https://brand.com/en-usa/products", None),
    ],
)
def test_detect_country_path_locale(url, expected):
    assert detect_country(url) == expected


def test_detect_country_handles_bad_input():
    assert detect_country("") is None
    assert detect_country("not-a-url") is None
    assert detect_country("http://") is None
