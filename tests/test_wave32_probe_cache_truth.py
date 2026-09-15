"""[wave-32 B2] The probe cache gets a way to be wrong.

587: the ladder saw Akamai on marimekko rungs, the probe-cache kept
``needs_akamai_bypass=False`` forever (only SUCCESS rungs ever write), the
next job's cache read said "no bypass needed", and the site poisoned a
second job. B2 pins the missing negative-direction write: akamai rungs
without a successful rung record ``needs_akamai_bypass=True`` — and ONLY
that field (C2: a previously-learned good method must survive).

Run: docker compose exec -T django sh -c "cd /app && pytest tests/test_wave32_probe_cache_truth.py -q"
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents.tools import probe_tools as pt  # noqa: E402
from scraper.models import ProbeCache  # noqa: E402

_counter = [0]


def _domain() -> str:
    _counter[0] += 1
    return f"akamai{_counter[0]}.example.com"


def _resp(payload: dict, status: int = 200):
    r = SimpleNamespace(status_code=status)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


def _drive(monkeypatch, url: str, rung_payload: dict):
    """Every rung and every akamai-bypass call returns ``rung_payload``."""

    def fake_post(target, json=None, **kw):
        return _resp(dict(rung_payload))

    monkeypatch.setattr(pt.httpx, "post", fake_post)
    # The LLM captcha classifier is orthogonal to B2 (cache writes) and must
    # never fire a real network call from a unit test.
    monkeypatch.setattr(
        pt, "_verify_captcha_free",
        lambda data: {"captcha_detected": False, "captcha_type": "",
                      "confidence": 0.0, "reasoning": "stub"},
    )
    return pt.run_probe_with_captcha_check(url, job_id=0)


@pytest.mark.django_db
def test_ladder_akamai_writes_needs_bypass(monkeypatch):
    """Akamai on every rung, no successful rung → the cache records
    needs_akamai_bypass=True (the write that never existed)."""
    domain = _domain()
    result = _drive(
        monkeypatch, f"https://{domain}/c/knitwear",
        {"success": False, "needs_akamai_bypass": True, "status_code": 403},
    )
    assert result["success"] is False
    row = ProbeCache.objects.get(domain=domain)
    assert row.needs_akamai_bypass is True, (
        "akamai rungs with no success must flip the cache's bypass flag"
    )


@pytest.mark.django_db
def test_negative_write_never_touches_method(monkeypatch):
    """C2: a previously-learned good method must SURVIVE the negative
    write — the write touches needs_akamai_bypass only."""
    domain = _domain()
    ProbeCache.objects.create(
        domain=domain, method="fingerprint_chrome_none",
        needs_akamai_bypass=False, captcha_detected=False,
    )
    _drive(
        monkeypatch, f"https://{domain}/c/knitwear",
        {"success": False, "needs_akamai_bypass": True, "status_code": 403},
    )
    row = ProbeCache.objects.get(domain=domain)
    assert row.method == "fingerprint_chrome_none", (
        "the negative write must never clobber a learned method"
    )
    assert row.needs_akamai_bypass is True
    assert row.captcha_detected is False, (
        "akamai-block is not captcha — the flag must not be smuggled in"
    )


@pytest.mark.django_db
def test_plain_failures_keep_legacy_captcha_write(monkeypatch):
    """Non-akamai all-fail keeps the legacy behavior untouched (captcha'd
    domain cache) — B2 adds a direction, it does not remove one."""
    domain = _domain()
    _drive(
        monkeypatch, f"https://{domain}/c/knitwear",
        {"success": False, "status_code": 403},
    )
    row = ProbeCache.objects.get(domain=domain)
    assert row.captcha_detected is True
    assert row.needs_akamai_bypass is False


@pytest.mark.django_db
def test_success_does_not_write_negative(monkeypatch):
    """A successful first rung writes the learned method — no negative
    flag (the akamai tail must not fire on success)."""
    domain = _domain()
    result = _drive(
        monkeypatch, f"https://{domain}/p/x",
        {"success": True, "status_code": 200, "title": "t",
         "method": "direct_http", "body_length": 5000,
         "needs_akamai_bypass": False},
    )
    assert result.get("success") is True
    row = ProbeCache.objects.get(domain=domain)
    assert row.needs_akamai_bypass is False
    assert row.method != "unknown"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
