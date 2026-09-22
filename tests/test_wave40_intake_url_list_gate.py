"""[wave-40 T5] url_list intake: a coerced PDP is SEEDED (repair), a truly
empty url_list is REJECTED (422). Prod 775-780, 784, 789, 793-796, 799,
802-804, 806, 811-814 — 21 instant fails in one aarya batch — died in ~4s in
setup_workspace because nothing seeded the coerced URL. 758 birkenstock is
the counter-proof that a seeded coerced PDP works (COMPLETED)."""

from src.intake_url_list import (
    coerced_seed_url, missing_url_list_reason as reason)


def test_url_list_with_no_urls_is_rejected():
    out = reason("url_list", "", False, False, "")
    assert out and "url" in out.lower()


def test_seed_url_rescues_repair_on_coercion():
    assert reason("url_list", "", False, False,
                  "https://x.example/p/tee-1") == ""


def test_url_list_with_a_url_passes():
    assert reason("url_list", "https://x.example/p/1", False, False, "") == ""


def test_site_urls_still_rescue_an_empty_textarea():
    assert reason("url_list", "", True, False, "") == ""


def test_fm_file_still_rescues():
    assert reason("url_list", "", False, True, "") == ""


def test_fm_unknown_fails_open():
    assert reason("url_list", "", False, None, "") == ""


def test_non_url_list_modes_never_rejected():
    for mode in ("navigation", "list_page", "search_term", ""):
        assert reason(mode, "", False, False, "") == ""


def test_whitespace_only_lines_do_not_count():
    assert reason("url_list", "  \n  \n", False, False, "")


def test_coerced_seed_url_returns_pdp():
    assert coerced_seed_url(
        "https://x.example/p/tee-1", "url_list", ""
    ) == "https://x.example/p/tee-1"


def test_coerced_seed_url_needs_url_list_mode_and_empty_textarea():
    assert coerced_seed_url("https://x.example/p/1", "navigation", "") == ""
    assert coerced_seed_url(
        "https://x.example/p/1", "url_list", "https://x.example/p/2") == ""
    assert coerced_seed_url("levis 501", "url_list", "") == ""
