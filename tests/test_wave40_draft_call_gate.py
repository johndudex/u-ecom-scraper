"""[wave-40 T6] AST pre-gate: helper CALL signatures in generated drafts.

Prod: 760 (unexpected kwarg 'post' on the create_fetch_json closure),
791 (multiple values for 'fetch_page'), 762 (re.error nothing to repeat) —
all crashed at execution after passing compile + F821."""

from agents.draft_safety import (
    DRAFT_CALL_VIOLATION_MARKER, draft_call_violation)

FETCHER_BAD = (
    "from src.http_fetch import create_fetch_json\n"
    "fetch_json = create_fetch_json()\n"
    "rows = fetch_json('https://x.example/api', post={'q': 1})\n"
)  # job 760 shape: closure takes (url, params=None, min_tier=0)

DISCOVERY_BAD = (
    "from src.listing_discovery import (\n"
    "    discover_listing_urls_with_retry, create_fetch_page)\n"
    "fetch_page = create_fetch_page()\n"
    "urls = discover_listing_urls_with_retry(\n"
    "    'https://x.example', fetch_page, fetch_page=fetch_page)\n"
)  # job 791 shape: fetch_page positional AND keyword

REGEX_BAD = "import re\nPAT = re.compile('a{2,1}')\n"  # job 762 shape

FETCHER_GOOD = (
    "from src.http_fetch import create_fetch_json\n"
    "fetch_json = create_fetch_json()\n"
    "rows = fetch_json('https://x.example/api', params={'q': 1})\n"
)

DISCOVERY_GOOD = (
    "from src.listing_discovery import (\n"
    "    discover_listing_urls_with_retry, create_fetch_page)\n"
    "fetch_page = create_fetch_page()\n"
    "urls = discover_listing_urls_with_retry('https://x.example', fetch_page)\n"
)


def test_unexpected_kwarg_is_caught():
    out = draft_call_violation(FETCHER_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "post" in out


def test_multiple_values_for_kwarg_is_caught():
    out = draft_call_violation(DISCOVERY_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)
    assert "fetch_page" in out


def test_bad_regex_literal_is_caught():
    out = draft_call_violation(REGEX_BAD)
    assert out.startswith(DRAFT_CALL_VIOLATION_MARKER)


def test_valid_registry_calls_pass():
    assert draft_call_violation(FETCHER_GOOD + "\nimport re\nre.findall(r'x+', html)\n") == ""
    assert draft_call_violation(DISCOVERY_GOOD) == ""


def test_draft_local_defs_shadow_the_registry():
    src = ("def fetch_json(url, post=None):\n"
           "    return {}\n"
           "fetch_json('https://x', post={'a': 1})\n")
    assert draft_call_violation(src) == ""


def test_starred_and_missing_args_stay_legal():
    src = (FETCHER_GOOD +
           "fetch_json(*parts)\n"
           "fetch_json()\n")
    assert draft_call_violation(src) == ""


def test_unknown_helpers_fall_open():
    assert draft_call_violation("my_invented_helper(url, post=1, bogus=2)\n") == ""


def test_findings_are_capped_at_five():
    calls = "\n".join(
        f"fetch_json('https://x', post={i})" for i in range(9))
    src = ("from src.http_fetch import create_fetch_json\n"
           "fetch_json = create_fetch_json()\n" + calls)
    out = draft_call_violation(src)
    assert out.count("\n") <= 5


def test_internal_error_falls_open(monkeypatch):
    from agents import draft_safety as ds
    monkeypatch.setattr(ds, "_helper_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert draft_call_violation("fetch_json('https://x', post=1)") == ""
