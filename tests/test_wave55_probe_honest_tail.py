"""[wave-55 H1] The all-fail probe tail must not mint a captcha verdict.

2026-10-05 prod audit: 61 of 62 ``captcha_blocked`` rows were false. The
ladder's all-fail tail hardcoded ``captcha_detected=True``, so a 403 edge
denial on every rung (job 1220 skinceuticals: 15 rungs, ZERO "returned
captcha" lines) ended the job as a captcha wall, wrote a poison
``cloak_residential`` ProbeCache row, and silently deleted the target from
the retryable backlog. The tail now reports what it measured:

- zero captcha rungs AND zero akamai rungs → a distinct ``probe_failed``
  verdict (job fails honestly, retryable, census in the error message);
- >=1 real captcha rung → captcha_blocked with the honest hit count;
- akamai rungs → akamai_blocked exactly as before.

Run: docker compose exec -T django bash -lc "cd /app/webapp && pytest ../tests/test_wave55_probe_honest_tail.py -q"
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
from django.contrib.auth.models import User  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from langgraph.graph import END  # noqa: E402

from agents import graph  # noqa: E402
from agents.tools import probe_tools as pt  # noqa: E402
from scraper.models import ProbeCache, ScrapeJob, SessionLog  # noqa: E402
from scraper.views import PROBE_FAILED_MSG_PREFIX  # noqa: E402

_counter = [0]


def _domain() -> str:
    _counter[0] += 1
    return f"w55probe{_counter[0]}.example.com"


def _resp(payload: dict, status: int = 200):
    r = SimpleNamespace(status_code=status)
    r.json = lambda: payload
    r.raise_for_status = lambda: None
    return r


def _no_captcha(data):
    return {"captcha_detected": False, "captcha_type": "",
            "confidence": 0.0, "reasoning": "stub"}


def _job(url: str) -> ScrapeJob:
    return ScrapeJob.objects.create(url=url, product_url=url)


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("w55_probe_admin", password="x")


@pytest.fixture
def admin_client(superuser):
    c = Client()
    c.force_login(superuser)
    return c


def _row(html, needle):
    for frag in html.split("<tr>"):
        if needle in frag:
            for row in frag.split("</tr>"):
                if needle in row:
                    return row
    raise AssertionError(f"no table row containing {needle!r}")


# ── Sub-fix 1: the honest tail ──────────────────────────────────────────


@pytest.mark.django_db
class TestHonestAllFailTail:
    def test_all_403_no_captcha_yields_probe_failed(self, monkeypatch):
        """THE job-1220 shape: every rung answers 403, no rung shows a captcha
        page → probe_failed, NOT captcha_detected."""
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        result = pt.run_probe_with_captcha_check(
            f"https://{_domain()}/p/x", job_id=0
        )

        assert result.get("success") is False
        assert result.get("probe_failed") is True, (
            "an all-fail ladder with zero captcha evidence is a probe "
            "failure, not a captcha wall"
        )
        assert result.get("captcha_detected") is False
        assert result.get("captcha_hits") == 0
        assert not result.get("blocked"), (
            "nothing was detected — 'blocked' would read as an anti-bot wall"
        )

    def test_probe_failed_error_carries_rung_census(self, monkeypatch):
        """The operator diagnoses from the census: the error must name the
        rungs and what each one returned, not a bare method list."""
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        result = pt.run_probe_with_captcha_check(
            f"https://{_domain()}/p/x", job_id=0
        )

        assert "[PROBE-FAILED 0/15 captcha]" in result["error"], result["error"]
        assert "direct_http http=403" in result["error"]
        assert "cloak_residential http=403" in result["error"]
        assert result.get("rung_outcomes"), "per-rung census must be machine-readable too"

    def test_probe_failed_does_not_claim_a_captcha_type(self, monkeypatch):
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        result = pt.run_probe_with_captcha_check(
            f"https://{_domain()}/p/x", job_id=0
        )

        assert result.get("captcha_type") == ""
        assert result.get("captcha_confidence") == 0.0

    def test_real_captcha_rung_still_yields_captcha_blocked(self, monkeypatch):
        """Regression pin for the honest side: one rung that DOES return a
        captcha page keeps the captcha verdict (captcha_blocked is still
        reachable — H1 is a mislabel fix, not a captcha-verdict removal)."""
        state = {"n": 0}

        def captcha_on_first(data):
            state["n"] += 1
            if state["n"] == 1:
                return {"captcha_detected": True, "captcha_type": "turnstile",
                        "confidence": 0.9, "reasoning": "challenge page"}
            return {"captcha_detected": False, "captcha_type": "",
                    "confidence": 0.0, "reasoning": "stub"}

        seen = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            seen["n"] += 1
            if seen["n"] == 1:
                return _resp({"success": True, "status_code": 200, "title": "t",
                              "body_length": 9000, "method": "direct_http",
                              "jsonld": [], "meta": {}, "selector_results": {}})
            return _resp({"success": False, "status_code": 403})

        monkeypatch.setattr(pt.httpx, "post", fake_post)
        monkeypatch.setattr(pt, "_verify_captcha_free", captcha_on_first)

        result = pt.run_probe_with_captcha_check(
            f"https://{_domain()}/p/x", job_id=0
        )

        assert result.get("probe_failed") is not True
        assert result.get("captcha_detected") is True
        assert result.get("captcha_hits") == 1
        assert result.get("captcha_type") == "turnstile"
        assert len(result.get("methods_tried", [])) == 15, (
            "the ladder kept walking after the captcha rung — the census "
            "stays complete"
        )

    def test_error_prefix_matches_the_retry_board_seam(self, monkeypatch):
        """[round-11 gap 2] The retryable-board flag keys on a string match
        between two modules (probe_tools.py:870 mints the prefix,
        scraper/views.py:42 PROBE_FAILED_MSG_PREFIX consumes it). If the
        producer's spelling drifts, every probe-failed row silently loses its
        probe_failed flag and badge while staying STATUS_FAILED — nothing
        else fails. This joins the two ends."""
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        result = pt.run_probe_with_captcha_check(
            f"https://{_domain()}/p/x", job_id=0
        )

        assert result["error"].startswith(PROBE_FAILED_MSG_PREFIX), (
            f"producer prefix drifted: probe_tools mints "
            f"{result['error'][:30]!r}, views.py flags rows on "
            f"{PROBE_FAILED_MSG_PREFIX!r} — those rows lose their retryable flag"
        )

    def test_akamai_rungs_keep_the_akamai_verdict(self, monkeypatch):
        """The wave-32 B2 akamai tail is untouched: akamai evidence on every
        rung is a real wall, never demoted to a plain probe failure."""
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403,
                 "needs_akamai_bypass": True}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        result = pt.run_probe_with_captcha_check(
            f"https://{_domain()}/p/x", job_id=0
        )

        assert result.get("probe_failed") is not True
        assert result.get("akamai_detected") is True
        assert result.get("akamai_method_count") == 15, (
            "one akamai verdict per ladder rung (bypass rungs are retries, "
            "not extra rungs)"
        )
        assert "cloak_residential" in result.get("methods_tried", [])


# ── Sub-fix 2: the graph routes on the honest verdict ───────────────────


@pytest.mark.django_db
class TestGraphRouting:
    def test_probe_failed_ends_job_as_plain_failed(self, monkeypatch):
        """An all-fail no-captcha probe ends the job with STATUS_FAILED and a
        [PROBE-FAILED n/m captcha] error — retryable backlog, not a wall."""
        job = _job(f"https://{_domain()}/p/x")
        monkeypatch.setattr(
            pt, "run_probe_with_captcha_check",
            lambda *a, **k: {
                "success": False, "probe_failed": True, "not_found": False,
                "blocked": False, "captcha_detected": False, "captcha_hits": 0,
                "captcha_type": "", "captcha_confidence": 0.0,
                "captcha_reasoning": "", "akamai_detected": False,
                "akamai_method_count": 0, "status_code": 403,
                "method": "cloak_residential", "methods_tried": [
                    "direct_http", "fingerprint_chrome_none"],
                "rung_outcomes": ["direct_http http=403",
                                  "fingerprint_chrome_none http=403"],
                "error": ("[PROBE-FAILED 0/2 captcha] all probe methods failed "
                          "(direct_http http=403, fingerprint_chrome_none http=403)"),
            },
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        cmd = graph.check_accessibility(
            {"job_id": job.id, "url": job.url, "input_mode": "url_list"}, None
        )

        assert cmd.goto == END
        assert cmd.update["error_message"].startswith("[PROBE-FAILED 0/2 captcha]")
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED, (
            "a probe failure is an honest failure — the operator can retry it"
        )
        assert job.error_message.startswith("[PROBE-FAILED 0/2 captcha]")
        assert job.completed_at is not None

    def test_probe_failed_fallback_msg_keeps_the_seam_prefix(self, monkeypatch):
        """[round-12 gap 2] graph.py mints the [PROBE-FAILED prefix a second
        time as a fallback for a producer dict that arrives without "error".
        Same drift class as the probe_tools.py:870 literal the retry board
        keys on: if either spelling drifts, the row stays STATUS_FAILED but
        loses its probe_failed flag and badge. This drives the fallback arm
        and joins it to the consumer constant."""
        job = _job(f"https://{_domain()}/p/x")
        monkeypatch.setattr(
            pt, "run_probe_with_captcha_check",
            lambda *a, **k: {
                "success": False, "probe_failed": True, "not_found": False,
                "blocked": False, "captcha_detected": False, "captcha_hits": 0,
                "captcha_type": "", "captcha_confidence": 0.0,
                "captcha_reasoning": "", "akamai_detected": False,
                "akamai_method_count": 0, "status_code": 403,
                "method": "cloak_residential",
                "methods_tried": ["direct_http", "fingerprint_chrome_none"],
                # NB: deliberately NO "error" key — the fallback literal at
                # graph.py's probe_failed arm is what must carry the prefix.
            },
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        cmd = graph.check_accessibility(
            {"job_id": job.id, "url": job.url, "input_mode": "url_list"}, None
        )

        assert cmd.goto == END
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_FAILED
        assert job.error_message.startswith(PROBE_FAILED_MSG_PREFIX), (
            f"the graph's no-error fallback minted "
            f"{job.error_message[:30]!r} but views.py flags rows on "
            f"{PROBE_FAILED_MSG_PREFIX!r} — those rows lose their retryable "
            "flag while staying STATUS_FAILED"
        )

    def test_one_captcha_rung_reports_the_honest_count(self, monkeypatch):
        """The templated 'All 15 probe methods returned captcha pages' lie is
        gone: the message reports the measured hit count."""
        job = _job(f"https://{_domain()}/p/x")
        monkeypatch.setattr(
            pt, "run_probe_with_captcha_check",
            lambda *a, **k: {
                "success": False, "not_found": False, "blocked": True,
                "captcha_detected": True, "captcha_hits": 1,
                "captcha_type": "turnstile", "captcha_confidence": 0.9,
                "captcha_reasoning": "challenge", "akamai_detected": False,
                "akamai_method_count": 0, "status_code": 200,
                "method": "cloak_residential",
                "methods_tried": [f"rung{i}" for i in range(15)],
            },
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        graph.check_accessibility(
            {"job_id": job.id, "url": job.url, "input_mode": "url_list"}, None
        )

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_CAPTCHA_BLOCKED
        assert "1/15 probe methods returned captcha pages" in job.error_message
        assert "All 15 probe methods returned captcha pages" not in job.error_message

    def test_full_captcha_census_reports_15_of_15(self, monkeypatch):
        job = _job(f"https://{_domain()}/p/x")
        monkeypatch.setattr(
            pt, "run_probe_with_captcha_check",
            lambda *a, **k: {
                "success": False, "not_found": False, "blocked": True,
                "captcha_detected": True, "captcha_hits": 15,
                "captcha_type": "recaptcha", "captcha_confidence": 0.9,
                "captcha_reasoning": "", "akamai_detected": False,
                "akamai_method_count": 0, "status_code": 200,
                "method": "cloak_residential",
                "methods_tried": [f"rung{i}" for i in range(15)],
            },
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        graph.check_accessibility(
            {"job_id": job.id, "url": job.url, "input_mode": "url_list"}, None
        )

        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_CAPTCHA_BLOCKED
        assert "15/15 probe methods returned captcha pages" in job.error_message

    def test_akamai_wall_still_routes_akamai_blocked(self, monkeypatch):
        """The honest tail can now report akamai with captcha_detected=False —
        that must STILL end the job as akamai_blocked, never sail through."""
        job = _job(f"https://{_domain()}/p/x")
        monkeypatch.setattr(
            pt, "run_probe_with_captcha_check",
            lambda *a, **k: {
                "success": False, "not_found": False, "blocked": True,
                "captcha_detected": False, "captcha_hits": 0,
                "captcha_type": "", "captcha_confidence": 0.0,
                "captcha_reasoning": "", "akamai_detected": True,
                "akamai_method_count": 15, "status_code": 403,
                "method": "cloak_residential",
                "methods_tried": [f"rung{i}" for i in range(15)],
            },
        )
        monkeypatch.setattr(graph, "_persist_probe_summary", lambda *a, **k: None)

        cmd = graph.check_accessibility(
            {"job_id": job.id, "url": job.url, "input_mode": "url_list"}, None
        )

        assert cmd.goto == END
        job.refresh_from_db()
        assert job.status == ScrapeJob.STATUS_AKAMAI_BLOCKED


# ── Sub-fix 3: failed rungs log their status code ───────────────────────


@pytest.mark.django_db
def test_failed_rung_log_carries_status_code(monkeypatch):
    """'[PROBE] direct_http FAILED' told the operator nothing; the rung line
    now says what the wire returned ('direct_http FAILED http=403')."""
    job = _job(f"https://{_domain()}/p/x")
    monkeypatch.setattr(
        pt.httpx, "post",
        lambda url, json=None, timeout=None: _resp(
            {"success": False, "status_code": 403}),
    )
    monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

    pt.run_probe_with_captcha_check(job.url, job_id=job.id)

    contents = list(
        SessionLog.objects.filter(job_id=job.id).values_list("content", flat=True)
    )
    assert "[PROBE] direct_http FAILED http=403" in contents
    assert "[PROBE] cloak_residential FAILED http=403" in contents


# ── Sub-fix 4: the all-fail cache write is field-scoped ─────────────────


@pytest.mark.django_db
class TestAllFailCacheWrite:
    def test_write_never_mints_a_method(self, monkeypatch):
        """The poison shape: the last-tried rung is not a method that worked.
        The all-fail write must not invent one."""
        domain = _domain()
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        pt.run_probe_with_captcha_check(f"https://{domain}/p/x", job_id=0)

        row = ProbeCache.objects.get(domain=domain)
        assert row.method == "", (
            "an all-fail ladder learned no method — writing the last rung's "
            "name is how 32 poison rows read as 'cloak_residential works'"
        )
        assert row.captcha_detected is False
        assert row.needs_akamai_bypass is False

    def test_write_never_clobbers_a_learned_method(self, monkeypatch):
        """wave-32 C2 parity with the akamai tail: a previously-learned good
        method on the domain must survive the negative write."""
        domain = _domain()
        ProbeCache.objects.create(
            domain=domain, method="fingerprint_chrome_none",
            needs_akamai_bypass=False, captcha_detected=False,
        )
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        pt.run_probe_with_captcha_check(f"https://{domain}/p/x", job_id=0)

        row = ProbeCache.objects.get(domain=domain)
        assert row.method == "fingerprint_chrome_none"

    def test_cache_read_ignores_an_empty_method_row(self, monkeypatch):
        """[round-12 gap 1] The field-scoped write leaves a row whose method
        is "" (an all-fail ladder learned none — and the akamai tail has
        minted such rows since wave-32). _get_cached_method must refuse it:
        handing back method="" sends probe_page down /probe-single with
        method:"", browser-service answers that 400, and every probe on the
        domain dies with zero rungs attempted for the whole 4h cache TTL."""
        domain = _domain()
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        pt.run_probe_with_captcha_check(f"https://{domain}/p/x", job_id=0)
        assert ProbeCache.objects.get(domain=domain).method == ""

        assert pt._get_cached_method(domain) is None, (
            "an empty-method row is not a method that works — returning it "
            "turns the probe tool into a 400 for the cache TTL on exactly "
            "the walled domains this wave makes retryable"
        )

    def test_probe_page_does_not_pin_the_empty_method(self, monkeypatch):
        """[round-12 gap 1] Tool-level pin on the same row shape: with an
        empty-method cache row on the domain, probe_page must still take the
        full-ladder /probe endpoint, never POST /probe-single with
        method:"" (browser_service/server.py rejects that 400 before any
        rung runs)."""
        domain = _domain()
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)
        pt.run_probe_with_captcha_check(f"https://{domain}/p/x", job_id=0)

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _resp({"success": True, "status_code": 200, "title": "t",
                          "body_length": 9000, "method": "direct_http",
                          "proxy_tier": "none", "needs_akamai_bypass": False,
                          "jsonld": [], "meta": {}, "selector_results": {}})

        monkeypatch.setattr(pt.httpx, "post", fake_post)
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        pt.get_probe_tools()[0].invoke({"url": f"https://{domain}/p/y"})

        assert captured["url"].endswith("/probe"), (
            f"an empty-method row must not route probe_page at /probe-single, "
            f"got {captured['url']}"
        )
        assert "method" not in captured["json"], (
            f"an empty-method row must not be sent as the probe method, "
            f"got {captured['json']}"
        )

    def test_captcha_evidenced_fail_still_flags_the_domain(self, monkeypatch):
        domain = _domain()
        state = {"n": 0}

        def captcha_on_first(data):
            state["n"] += 1
            if state["n"] == 1:
                return {"captcha_detected": True, "captcha_type": "turnstile",
                        "confidence": 0.9, "reasoning": "challenge page"}
            return {"captcha_detected": False, "captcha_type": "",
                    "confidence": 0.0, "reasoning": "stub"}

        seen = {"n": 0}

        def fake_post(url, json=None, timeout=None):
            seen["n"] += 1
            if seen["n"] == 1:
                return _resp({"success": True, "status_code": 200, "title": "t",
                              "body_length": 9000, "method": "direct_http",
                              "jsonld": [], "meta": {}, "selector_results": {}})
            return _resp({"success": False, "status_code": 403})

        monkeypatch.setattr(pt.httpx, "post", fake_post)
        monkeypatch.setattr(pt, "_verify_captcha_free", captcha_on_first)

        pt.run_probe_with_captcha_check(f"https://{domain}/p/x", job_id=0)

        row = ProbeCache.objects.get(domain=domain)
        assert row.captcha_detected is True, (
            "a ladder that DID see a captcha page keeps the captcha flag"
        )


# ── Sub-fix 4 (render half): /probe-cache/ shows the honest row ─────────


@pytest.mark.django_db
class TestProbeCacheRender:
    """[round-11 gap 1] The field-scoped write leaves an all-fail row with
    method='' + captcha_detected=False. On /probe-cache/ that must be
    distinguishable from a healthy empty row: an em-dash method chip and an
    explicit captcha column, so the operator can audit the poison rows
    against the honest ones on the page the deletes are performed on."""

    def test_all_fail_row_renders_honest_on_probe_cache(
        self, admin_client, monkeypatch
    ):
        domain = _domain()
        monkeypatch.setattr(
            pt.httpx, "post",
            lambda url, json=None, timeout=None: _resp(
                {"success": False, "status_code": 403}),
        )
        monkeypatch.setattr(pt, "_verify_captcha_free", _no_captcha)

        pt.run_probe_with_captcha_check(f"https://{domain}/p/x", job_id=0)
        # a real captcha wall on a second domain, for contrast
        ProbeCache.objects.create(
            domain=_domain(), method="cloak_residential",
            captcha_detected=True,
        )

        html = admin_client.get(reverse("probe_cache")).content.decode()
        row = _row(html, domain)

        assert "captcha seen" in _row(html, "cloak_residential"), (
            "the captcha column must flag a domain that DID see a captcha page"
        )
        assert "cloak_residential" in _row(html, "cloak_residential")
        assert "none" in row, "the captcha column must say what the row measured"
        assert "captcha seen" not in row, (
            "an all-fail ladder with zero captcha rungs must not read as a "
            "captcha domain"
        )
        assert ">" + chr(8212) + "</code>" in row, (
            "an empty method must render an em-dash chip, not a blank one"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
