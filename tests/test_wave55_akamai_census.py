"""[wave-55 H1 residual] the akamai arm states the census it measured.

``akamai_detected`` means "at least one rung returned an Akamai page", but
the akamai message arm read "All {len(methods)} probe methods across all
proxy tiers were blocked by Akamai" — the same lie-shape the honest-tail fix
removed from the captcha arm (a 2-akamai + 13x403 run is NOT "all Akamai").
The message now reports the measured census, plus what the remaining rungs
returned. Companion: the bypass BLOCKED step log carries the bypass rung's
own status code (it was already in ``rung_outcomes`` — the SessionLog line
omitted it, so a bypass death read as contentless with no diagnosis).

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest ../tests/test_wave55_akamai_census.py -q"
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
from langgraph.graph import END  # noqa: E402

from agents import graph  # noqa: E402
from agents.tools import probe_tools as pt  # noqa: E402
from scraper.models import ScrapeJob, SessionLog  # noqa: E402

_counter = [0]


def _domain() -> str:
    _counter[0] += 1
    return f"w55akamai{_counter[0]}.example.com"


def _resp(payload: dict, status: int = 200):
    r = SimpleNamespace(status_code=status)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


def _job(url: str) -> ScrapeJob:
    return ScrapeJob.objects.create(url=url, product_url=url)


def _probe_data(**over) -> dict:
    data = {
        "success": False, "not_found": False, "blocked": True,
        "captcha_detected": False, "captcha_hits": 0,
        "captcha_type": "", "captcha_confidence": 0.0,
        "captcha_reasoning": "", "akamai_detected": True,
        "akamai_method_count": 15, "status_code": 403,
        "method": "cloak_residential",
        "methods_tried": [f"rung{i}" for i in range(15)],
    }
    data.update(over)
    return data


def _run(job, probe_data, monkeypatch):
    monkeypatch.setattr(
        pt, "run_probe_with_captcha_check", lambda *a, **k: probe_data
    )
    monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)
    cmd = graph.check_accessibility(
        {"job_id": job.id, "url": job.url, "input_mode": "url_list"}, None
    )
    job.refresh_from_db()
    return cmd


@pytest.mark.django_db
class TestAkamaiMessageStatesTheCensus:
    def test_mixed_run_does_not_claim_all_methods(self, monkeypatch):
        """THE over-claim shape: 2 akamai rungs among 15 is not 'All 15
        probe methods ... blocked by Akamai'."""
        job = _job(f"https://{_domain()}/p/x")
        _run(job, _probe_data(akamai_method_count=2), monkeypatch)

        assert job.status == ScrapeJob.STATUS_AKAMAI_BLOCKED
        assert "2/15 probe methods returned Akamai pages" in job.error_message
        assert "All 15 probe methods" not in job.error_message

    def test_mixed_run_reports_the_ordinary_fail_remainder(self, monkeypatch):
        """The 13 rungs that returned neither Akamai nor captcha are named —
        a 403 edge denial is a different site-state from an Akamai page."""
        job = _job(f"https://{_domain()}/p/x")
        _run(job, _probe_data(akamai_method_count=2), monkeypatch)

        assert "13 returned no content and no captcha page" in job.error_message

    def test_mixed_run_names_the_captcha_rungs_too(self, monkeypatch):
        """Akamai and captcha rungs are disjoint; a run carrying both must
        not fold the captcha rungs into 'no captcha page'."""
        job = _job(f"https://{_domain()}/p/x")
        _run(
            job,
            _probe_data(akamai_method_count=2, captcha_detected=True,
                        captcha_hits=1, captcha_type="turnstile"),
            monkeypatch,
        )

        assert job.status == ScrapeJob.STATUS_AKAMAI_BLOCKED
        assert "2/15 probe methods returned Akamai pages" in job.error_message
        assert "1 returned captcha pages" in job.error_message
        # the captcha rung is not folded into the no-content remainder
        assert "12 returned no content and no captcha page" in job.error_message
        assert "14 returned no content" not in job.error_message

    def test_total_akamai_wall_reads_15_of_15(self, monkeypatch):
        """A genuine wall keeps the strong claim — just in census form."""
        job = _job(f"https://{_domain()}/p/x")
        _run(job, _probe_data(), monkeypatch)

        assert "15/15 probe methods returned Akamai pages" in job.error_message
        assert "All 15 probe methods" not in job.error_message

    def test_legacy_producer_without_a_count_keeps_the_old_claim(self, monkeypatch):
        """Producers predating the census carry no akamai_method_count — they
        keep the all-methods reading (as a census) rather than under-reporting
        a number we do not have."""
        job = _job(f"https://{_domain()}/p/x")
        data = _probe_data()
        del data["akamai_method_count"]
        _run(job, data, monkeypatch)

        assert job.status == ScrapeJob.STATUS_AKAMAI_BLOCKED
        assert "15/15 probe methods returned Akamai pages" in job.error_message


# ── the bypass BLOCKED step log carries the bypass rung's status code ──


@pytest.mark.django_db
def test_akamai_bypass_blocked_log_carries_the_bypass_status_code(monkeypatch):
    """The bypass rung's own status code was already in ``rung_outcomes`` but
    the [PROBE] step log said only 'BLOCKED — still no content' — no
    diagnosis on the surface the operator actually reads."""
    domain = _domain()

    def _post(url, json=None, timeout=None):
        if (json or {}).get("method", "").startswith("cloak_"):
            return _resp({"success": False, "status_code": 503})
        return _resp(
            {"success": False, "status_code": 403, "needs_akamai_bypass": True}
        )

    monkeypatch.setattr(pt.httpx, "post", _post)
    monkeypatch.setattr(
        pt, "_verify_captcha_free",
        lambda data: {"captcha_detected": False, "captcha_type": "",
                      "confidence": 0.0, "reasoning": "stub"},
    )

    job = _job(f"https://{domain}/p/x")
    result = pt.run_probe_with_captcha_check(job.url, job_id=job.id)
    assert result.get("akamai_detected") is True

    contents = list(
        SessionLog.objects.filter(job_id=job.id).values_list("content", flat=True)
    )
    assert (
        "[PROBE] direct_http Akamai bypass BLOCKED http=503 — still no content"
    ) in contents, (
        f"bypass BLOCKED line carries no status code; got "
        f"{[c for c in contents if 'bypass BLOCKED' in c]}"
    )
