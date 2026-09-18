"""[wave-37 W37-NEW-F] A sign-in redirect during discovery is a SITE-SIDE
stop (auth_wall), not a draft traceback. Prod 652 goodreads: Phase-1
discovery crashed on the login redirect and the job burned cascade arms
"fixing" an unfixable wall.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from webapp.agents.graph import classify_discovery_failure  # noqa: E402


def test_signin_redirect_is_auth_wall():
    assert classify_discovery_failure(
        "Sign in to Goodreads — https://www.goodreads.com/user/sign_in?redirect=…"
    ) == "auth_wall"


def test_create_account_prompt_is_auth_wall():
    assert classify_discovery_failure(
        "Please log in or create an account to continue"
    ) == "auth_wall"


def test_traceback_body_with_login_redirect_is_auth_wall():
    # the realistic 652 shape: the failure text is the probe's traceback, and
    # the site-side evidence lives in the URL inside it.
    assert classify_discovery_failure(
        "Traceback (most recent call last):\n"
        "  File \"scraper_draft.py\", line 88, in discover\n"
        "    r.raise_for_status()\n"
        "requests.exceptions.HTTPError: 302 — redirected to "
        "https://www.goodreads.com/user/sign_in?redirect=https%3A%2F%2Fwww.goodreads.com%2Flist"
    ) == "auth_wall"


def test_plain_500_is_not_auth_wall():
    assert classify_discovery_failure("HTTP 500 Internal Server Error") != "auth_wall"


def test_plain_code_crash_is_none():
    assert (
        classify_discovery_failure(
            "AttributeError: 'NoneType' object has no attribute 'find'"
        )
        is None
    )


def test_empty_is_none():
    assert classify_discovery_failure("") is None
