"""Unit tests for webapp/agents/tools/browser_http.post_scrape_with_retry (W8).

Every webapp-side /scrape caller used a bare httpx.post + raise_for_status()
(or no status check at all), so browser-service backpressure (429) and
memory-pressure 502s surfaced as opaque HTTPStatusErrors, silently-empty
output_content, or "successful" re-runs with zero products. The helper
pins one bounded-retry policy: retry 429/502/503/504 + transport errors,
never 404 ("source invalid" is a distinct signal), never anything else,
always inside a total time budget.
"""

import time
import types

import httpx
import pytest
from agents.tools import browser_http as bh


class _Resp:
    def __init__(self, status, payload=None, headers=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("invalid json")
        return self._payload


def _install(monkeypatch, responses, sleeps=None):
    """Script httpx.post + time.sleep (the fake sleep ADVANCES the monotonic
    clock — the budget math only works if sleeping consumes time).
    ``responses``: list of response/exception, consumed per attempt."""
    calls = {"count": 0}
    recorded_sleeps = sleeps if sleeps is not None else []

    class _Clock:
        now = time.monotonic()

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, s):
            recorded_sleeps.append(s)
            cls.now += s

    def fake_post(url, json=None, timeout=None):
        calls["count"] += 1
        item = responses[calls["count"] - 1]
        if isinstance(item, Exception):
            raise item
        return item

    fake_httpx = types.SimpleNamespace(post=fake_post, codes=httpx.codes)
    monkeypatch.setattr(bh, "httpx", fake_httpx)
    monkeypatch.setattr(bh, "time", types.SimpleNamespace(
        monotonic=_Clock.monotonic, sleep=_Clock.sleep,
    ))
    return calls, recorded_sleeps


URL = "http://browser_service:8001/scrape"
PAYLOAD = {"scraper_source": "print(1)", "timeout": 300}


class TestSuccessPaths:
    def test_first_attempt_success(self, monkeypatch):
        _calls, sleeps = _install(
            monkeypatch, [_Resp(200, {"output_content": "[]", "product_count": 3})]
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        assert res.data["product_count"] == 3
        assert res.attempts == 1
        assert res.error_class == "ok"
        assert sleeps == []

    def test_transport_error_then_success(self, monkeypatch):
        calls, _ = _install(
            monkeypatch,
            [httpx.ConnectError("connection refused"), _Resp(200, {"product_count": 1})],
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        assert res.attempts == 2
        assert calls["count"] == 2

    def test_429_then_success_reports_backpressure(self, monkeypatch):
        _install(
            monkeypatch,
            [_Resp(429, {"retry_after": 2}), _Resp(200, {"product_count": 5})],
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        # The 429 stays visible even though the call eventually succeeded.
        assert res.throttled is True
        assert res.error_class == "ok"
        assert res.attempts == 2


class TestRetryPolicy:
    def test_429_502_503_504_are_retried(self, monkeypatch):
        calls, _ = _install(
            monkeypatch,
            [
                _Resp(429, {}),
                _Resp(502, {}),
                _Resp(503, {}),
                _Resp(504, {}),
                _Resp(200, {"product_count": 1}),
            ],
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360, max_attempts=5)
        assert res.ok is True
        assert calls["count"] == 5

    def test_other_statuses_are_fatal_never_retried(self, monkeypatch):
        for status in (400, 401, 500, 501):
            calls, _ = _install(monkeypatch, [_Resp(status, {})])
            res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360, max_attempts=3)
            assert res.ok is False
            assert res.attempts == 1
            assert res.error_class == "fatal"
            assert calls["count"] == 1

    def test_404_is_source_invalid_never_retried(self, monkeypatch):
        calls, sleeps = _install(monkeypatch, [_Resp(404, {})])
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360, max_attempts=3)
        assert res.ok is False
        assert res.attempts == 1
        assert res.error_class == "not_found"
        assert "source invalid" in (res.error or "")
        assert calls["count"] == 1
        assert sleeps == []

    def test_exhausted_retries_stay_transient(self, monkeypatch):
        _install(monkeypatch, [_Resp(502, {})] * 3)
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360, max_attempts=3)
        assert res.ok is False
        assert res.transient is True
        assert res.status == 502
        assert res.attempts == 3
        assert res.error_class == "transient"

    def test_non_json_200_is_fatal(self, monkeypatch):
        _install(monkeypatch, [_Resp(200, json_error=True)])
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is False
        assert res.error_class == "fatal"
        assert "non-JSON" in (res.error or "")


class TestRetryAfter:
    def test_header_wins_over_body(self, monkeypatch):
        sleeps = []
        _install(
            monkeypatch,
            [_Resp(429, {"retry_after": 45}, headers={"Retry-After": "7"}), _Resp(200, {})],
            sleeps=sleeps,
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        assert sleeps[0] == 7

    def test_body_used_without_header(self, monkeypatch):
        sleeps = []
        _install(
            monkeypatch,
            [_Resp(429, {"retry_after": 12}), _Resp(200, {})],
            sleeps=sleeps,
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        assert sleeps[0] == 12

    def test_retry_after_clamped_to_cap(self, monkeypatch):
        sleeps = []
        _install(
            monkeypatch,
            [_Resp(429, {"retry_after": 600}), _Resp(200, {})],
            sleeps=sleeps,
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        assert sleeps[0] == bh.MAX_RETRY_AFTER_S

    def test_transport_retry_uses_default_backoff(self, monkeypatch):
        sleeps = []
        _install(
            monkeypatch,
            [httpx.ConnectError("refused"), _Resp(200, {})],
            sleeps=sleeps,
        )
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360)
        assert res.ok is True
        assert sleeps[0] == bh.DEFAULT_RETRY_AFTER_S


class TestBudget:
    def test_budget_short_circuits_remaining_attempts(self, monkeypatch):
        """A long Retry-After must not put a second attempt outside the budget."""
        calls, _ = _install(
            monkeypatch,
            [_Resp(429, {"retry_after": 60}), _Resp(200, {})],
        )
        res = bh.post_scrape_with_retry(
            URL, PAYLOAD, timeout=360, total_budget_s=10, max_attempts=3
        )
        assert res.ok is False
        assert res.attempts == 1
        assert res.throttled is True
        assert res.error_class == "throttled"
        assert calls["count"] == 1

    def test_fast_failures_all_fit_in_budget(self, monkeypatch):
        calls, _ = _install(
            monkeypatch,
            [_Resp(429, {"retry_after": 1}), _Resp(429, {"retry_after": 1}), _Resp(200, {})],
        )
        res = bh.post_scrape_with_retry(
            URL, PAYLOAD, timeout=360, total_budget_s=3600, max_attempts=3
        )
        assert res.ok is True
        assert res.attempts == 3
        assert calls["count"] == 3

    def test_default_budget_is_timeout_plus_slack(self):
        # Behavioral contract, asserted structurally: the default must leave
        # real headroom beyond the scrape timeout, not 1.0x.
        assert bh.DEFAULT_BUDGET_SLACK_S >= 120


class TestNeverRaises:
    def test_transport_errors_become_transient_result(self, monkeypatch):
        _install(monkeypatch, [httpx.ConnectError("x")] * 3)
        res = bh.post_scrape_with_retry(URL, PAYLOAD, timeout=360, max_attempts=3)
        assert res.ok is False
        assert res.transient is True
        assert res.status is None
        assert res.error  # human-readable summary present

    def test_error_class_buckets(self):
        assert bh.ScrapeResult(ok=True).error_class == "ok"
        assert bh.ScrapeResult(status=404, error="x").error_class == "not_found"
        assert bh.ScrapeResult(throttled=True, error="x").error_class == "throttled"
        assert bh.ScrapeResult(transient=True, error="x").error_class == "transient"
        assert bh.ScrapeResult(error="x").error_class == "fatal"


class TestAttemptTimeoutSlicing:
    """QW-5 (job-312): retries must get a REAL window, not a sliver.

    A retry attempt sliced to near-zero always fails and reads as a scrape
    defect → false strategy cascade. Attempt 1 keeps the full timeout,
    middle attempts are capped by the remaining budget, and the final
    attempt is floored at FINAL_ATTEMPT_MIN_S even past the nominal budget
    (bounded overshoot: budget + one attempt)."""

    def test_attempt1_keeps_full_timeout(self):
        deadline = 10_000.0 + time.monotonic()
        assert bh._attempt_timeout(660.0, deadline, 1, 3) == 660.0

    def test_middle_attempt_capped_by_remaining(self):
        deadline = 240.0 + time.monotonic()
        # real clock ticks microseconds between here and the helper — allow it
        assert bh._attempt_timeout(660.0, deadline, 2, 3) == pytest.approx(
            240.0, abs=1e-2
        )

    def test_middle_attempt_keeps_min_budget_floor(self):
        deadline = 2.0 + time.monotonic()
        assert bh._attempt_timeout(660.0, deadline, 2, 3) == bh.MIN_ATTEMPT_BUDGET_S

    def test_final_attempt_floored_past_budget(self):
        deadline = time.monotonic() - 1  # budget exhausted
        assert bh._attempt_timeout(660.0, deadline, 3, 3) == bh.FINAL_ATTEMPT_MIN_S

    def test_final_attempt_never_exceeds_caller_timeout(self):
        deadline = time.monotonic() - 1
        # A caller that asked for 120s per attempt must not be stretched to 300s.
        assert bh._attempt_timeout(120.0, deadline, 3, 3) == 120.0

    def test_final_attempt_with_plenty_remaining_stays_capped(self):
        deadline = 500.0 + time.monotonic()
        assert bh._attempt_timeout(660.0, deadline, 3, 3) == pytest.approx(
            500.0, abs=1e-2
        )

    def test_exhausted_budget_final_attempt_still_fires(self, monkeypatch):
        """Integration: short-circuit exempts the final attempt, which runs
        with the QW-5 floor even though the nominal budget is spent."""
        recorded_timeouts = []

        def fake_post(url, json=None, timeout=None):
            recorded_timeouts.append(timeout)
            if len(recorded_timeouts) < 3:
                return _Resp(429, {"retry_after": 2})
            return _Resp(200, {"product_count": 4})

        class _Clock:
            now = 0.0

            @classmethod
            def monotonic(cls):
                return cls.now

            @classmethod
            def sleep(cls, s):
                cls.now += s

        fake_httpx = types.SimpleNamespace(post=fake_post, codes=httpx.codes)
        monkeypatch.setattr(bh, "httpx", fake_httpx)
        monkeypatch.setattr(bh, "time", types.SimpleNamespace(
            monotonic=_Clock.monotonic, sleep=_Clock.sleep,
        ))

        res = bh.post_scrape_with_retry(
            URL, PAYLOAD, timeout=660.0, total_budget_s=8.0, max_attempts=3
        )
        assert res.ok is True
        assert res.attempts == 3
        # attempt 1: full timeout; attempt 2: remaining budget (8 - 2s sleep);
        # attempt 3: QW-5 floor despite the budget being spent (4s left, and
        # the second retry_after sleep takes it below zero).
        assert recorded_timeouts[0] == 660.0
        assert recorded_timeouts[1] == 6.0
        assert recorded_timeouts[2] == bh.FINAL_ATTEMPT_MIN_S


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
