"""[wave-55 H1#7 residual] the probe-tester cache payload carries the flag.

``/probe-cache/`` renders a Captcha column since the H1 fix, but the probe
TESTER's own cache read — ``probe_tester_cached_method`` — returned only
``method`` + ``needs_akamai_bypass``, so a poison row (``captcha_detected``
True from a real captcha rung) was invisible on that surface. The UI guards
on ``data.method`` before it renders anything, so the flag has to ride the
same payload to ever be visible there.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest tests/test_wave55_probe_tester_cache_payload.py -q"
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.urls import reverse  # noqa: E402
from scraper.models import ProbeCache  # noqa: E402
from scraper.views import probe_tester_cached_method  # noqa: E402

RF = RequestFactory()

_counter = [0]


def _domain() -> str:
    _counter[0] += 1
    return f"w55ptester{_counter[0]}.example.com"


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("w55_ptester_admin", password="x")


def _get(domain: str):
    request = RF.get(
        reverse("probe_tester_cached_method") + f"?domain={domain}",
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    request.user = User.objects.get(username="w55_ptester_admin")
    return probe_tester_cached_method(request)


def _body(response) -> dict:
    return json.loads(response.content)


@pytest.mark.django_db
class TestCachedMethodPayloadCarriesCaptcha:
    def test_poison_row_reports_captcha_detected(self, superuser):
        """A captcha'd cache row must say so — needs_akamai_bypass alone
        conflates two different walls."""
        domain = _domain()
        ProbeCache.objects.create(
            domain=domain, method="cloak_residential",
            needs_akamai_bypass=False, captcha_detected=True,
        )
        assert _body(_get(domain))["captcha_detected"] is True

    def test_clean_row_reports_captcha_detected_false(self, superuser):
        domain = _domain()
        ProbeCache.objects.create(
            domain=domain, method="fingerprint_chrome_none",
            needs_akamai_bypass=False, captcha_detected=False,
        )
        payload = _body(_get(domain))
        assert payload["captcha_detected"] is False
        assert payload["method"] == "fingerprint_chrome_none"

    def test_all_fail_row_reports_the_honest_false(self, superuser):
        """The wave-55 field-scoped write leaves method="" +
        captcha_detected=False on an all-fail domain — the payload says
        exactly that (the UI's `if (data.method)` guard keeps hiding it,
        which is the point: an empty method is not a method)."""
        domain = _domain()
        ProbeCache.objects.create(
            domain=domain, method="",
            needs_akamai_bypass=False, captcha_detected=False,
        )
        payload = _body(_get(domain))
        assert payload["method"] == ""
        assert payload["captcha_detected"] is False

    def test_no_row_still_returns_method_none(self, superuser):
        assert _body(_get(_domain())) == {"method": None}
