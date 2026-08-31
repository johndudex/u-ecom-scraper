"""Unit tests for the LiteLLM provider resolver + breaker relocation (§A/§B/§D
of docs/codewriter-litellm-plan.md v2).

No network: the resolver is tested via settings overrides; breaker recording is
tested by driving ClassifiedRetryChatOpenAI._generate with a patched super call.

Run from repo root:  python3 -m pytest tests/test_llm_provider.py -v
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest.mock as mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))


class _FakeSettings:
    """Minimal settings object for the lazy getattr reads in llm.py."""

    def __init__(self, **overrides):
        self.ZAI_BASE_URL = "https://zai.example/v4"
        self.ZAI_API_KEY = "zai-key"
        self.ZAI_MAIN_MODEL = "glm-5-turbo"
        self.ZAI_SMALL_MODEL = "glm-5-turbo"
        self.ZAI_FALLBACK_MODEL = "glm-5-turbo"
        self.LITELLM_ENABLED = True
        self.LITELLM_BASE_URL = "https://litellm.example/v1"
        self.LITELLM_API_KEY = "litellm-key"
        self.LITELLM_MODEL_PREFIXES = "litellm/"
        self.LITELLM_FALLBACK_MODEL = ""
        self.LLM_CLASSIFIED_RETRY = True
        self.LLM_REQUEST_TIMEOUT = 300
        self.LLM_MAX_RETRIES = 5
        for k, v in overrides.items():
            setattr(self, k, v)


def _llm_mod():
    import importlib

    import agents.llm as llm

    importlib.reload(llm)  # drop module-level breaker state references cleanly
    return llm


class TestProviderResolver:
    def _resolved(self, fake, model):
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", fake):
            return mod._provider_for(model)

    def test_litellm_prefix_routes_to_proxy_with_strip(self):
        base, key, name = self._resolved(_FakeSettings(), "litellm/standardcompute")
        assert base == "https://litellm.example/v1"
        assert key == "litellm-key"
        assert name == "standardcompute"

    def test_bare_model_routes_to_zai(self):
        base, key, name = self._resolved(_FakeSettings(), "glm-5-turbo")
        assert base == "https://zai.example/v4"
        assert key == "zai-key"
        assert name == "glm-5-turbo"

    def test_disabled_flag_routes_to_zai(self):
        fake = _FakeSettings(LITELLM_ENABLED=False)
        base, _, name = self._resolved(fake, "litellm/standardcompute")
        assert base == "https://zai.example/v4"
        assert name == "litellm/standardcompute"  # unstripped — ZAI would 404, loud

    def test_missing_key_routes_to_zai(self):
        fake = _FakeSettings(LITELLM_API_KEY="")
        base, _, _ = self._resolved(fake, "litellm/standardcompute")
        assert base == "https://zai.example/v4"

    def test_custom_prefix_list(self):
        fake = _FakeSettings(LITELLM_MODEL_PREFIXES="litellm/,proxy/")
        _, _, name = self._resolved(fake, "proxy/foo")
        assert name == "foo"

    def test_multiple_prefixes_do_not_shadow_bare(self):
        fake = _FakeSettings(LITELLM_MODEL_PREFIXES="litellm/,proxy/")
        base, _, name = self._resolved(fake, "glm-5.2")
        assert base == "https://zai.example/v4" and name == "glm-5.2"


class TestLitellmFallbackParam:
    def test_litellm_model_unconfigured_falls_to_zai_main(self):
        # [T3.13g] LITELLM_FALLBACK_MODEL unset → the breaker's fallback is
        # Z.AI direct (ZAI_MAIN_MODEL), NOT "" — "" disables the breaker's
        # swap entirely, which left a dead litellm proxy with no way back.
        # Cross-provider-safe: _provider_for resolves the endpoint FROM the
        # swapped name, so a ZAI name routes to ZAI direct with streaming off.
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", _FakeSettings()):
            assert mod._litellm_fallback("litellm/standardcompute") == "glm-5-turbo"

    def test_litellm_model_explicit_none_opts_out(self):
        # LITELLM_FALLBACK_MODEL=none → "" — the only way to get the old
        # no-swap behavior (effective_model's "" sentinel = leave model as-is).
        mod = _llm_mod()
        with mock.patch.object(
            mod, "settings", _FakeSettings(LITELLM_FALLBACK_MODEL="none")
        ):
            assert mod._litellm_fallback("litellm/standardcompute") == ""

    def test_litellm_model_configured_value_honored(self):
        mod = _llm_mod()
        with mock.patch.object(
            mod, "settings", _FakeSettings(LITELLM_FALLBACK_MODEL="litellm/backup")
        ):
            assert mod._litellm_fallback("litellm/standardcompute") == "litellm/backup"

    def test_zai_model_gets_none(self):
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", _FakeSettings()):
            assert mod._litellm_fallback("glm-5-turbo") is None


class TestEffectiveModelSemantics:
    """llm_breaker.effective_model fallback=None/""/value — the or-trap guard."""

    def _mod(self):
        import importlib

        import agents.llm_breaker as br

        importlib.reload(br)
        return br

    def test_fallback_none_uses_configured_default(self):
        br = self._mod()
        with mock.patch.object(br, "_config", return_value=(True, 4, 60, "glm-5-turbo")):
            with mock.patch.object(br, "is_tripped", return_value=True):
                assert br.effective_model("glm-5.2", fallback=None) == "glm-5-turbo"

    def test_fallback_empty_string_never_swaps(self):
        br = self._mod()
        with mock.patch.object(br, "_config", return_value=(True, 4, 60, "glm-5-turbo")):
            with mock.patch.object(br, "is_tripped", return_value=True):
                assert br.effective_model("litellm/standardcompute", fallback="") == "litellm/standardcompute"

    def test_fallback_value_used_verbatim(self):
        br = self._mod()
        with mock.patch.object(br, "_config", return_value=(True, 4, 60, "glm-5-turbo")):
            with mock.patch.object(br, "is_tripped", return_value=True):
                assert br.effective_model("glm-5.2", fallback="glm-mini") == "glm-mini"

    def test_not_tripped_returns_primary(self):
        br = self._mod()
        with mock.patch.object(br, "_config", return_value=(True, 4, 60, "glm-5-turbo")):
            with mock.patch.object(br, "is_tripped", return_value=False):
                assert br.effective_model("glm-5.2") == "glm-5.2"


class TestBreakerRecordingInRetryLayer:
    """§B: ClassifiedRetryChatOpenAI records under the CONFIGURED name.

    Patch point: the SUPERCLASS ``_generate`` (what the retry wrapper calls via
    ``super()._generate``) — patching ``type(llm)._generate`` would replace the
    wrapper itself and the code under test would never run.
    """

    def _make(self, mod, model="litellm/standardcompute"):
        llm = mod.ClassifiedRetryChatOpenAI(
            model_name=model,
            openai_api_base="https://litellm.example/v1",
            openai_api_key="k",
            max_retries=0,
        )
        llm._breaker_name = model
        return llm

    def test_success_records_under_configured_key(self):
        mod = _llm_mod()
        llm = self._make(mod)
        from langchain_openai import ChatOpenAI

        ok = mock.Mock()
        with mock.patch.object(ChatOpenAI, "_generate", lambda self, *a, **k: ok), \
             mock.patch.object(mod, "record_success") as rs:
            llm._generate([("user", "hi")])
            rs.assert_called_once_with("litellm/standardcompute")

    def test_transient_failure_records_failure(self):
        mod = _llm_mod()
        llm = self._make(mod)
        import openai
        from langchain_openai import ChatOpenAI

        def boom(*a, **k):
            raise openai.APITimeoutError(request=mock.Mock())

        # transient budget 0 → first failure raises straight through
        with mock.patch.object(mod, "_retry_settings", return_value={
                "transient_max": 0, "ratelimit_max": 0,
                "backoff_base": 0.0, "backoff_cap": 0.0}), \
             mock.patch.object(ChatOpenAI, "_generate", side_effect=boom), \
             mock.patch.object(mod, "record_failure") as rf:
            try:
                llm._generate([("user", "hi")])
                raised = False
            except openai.APITimeoutError:
                raised = True
            assert raised
            rf.assert_called_once_with("litellm/standardcompute")

    def test_caller_bug_does_not_trip(self):
        mod = _llm_mod()
        llm = self._make(mod)
        import openai
        from langchain_openai import ChatOpenAI

        def boom(*a, **k):
            raise openai.BadRequestError(
                message="bad", response=mock.Mock(), body=None)

        with mock.patch.object(mod, "_retry_settings", return_value={
                "transient_max": 0, "ratelimit_max": 0,
                "backoff_base": 0.0, "backoff_cap": 0.0}), \
             mock.patch.object(ChatOpenAI, "_generate", side_effect=boom), \
             mock.patch.object(mod, "record_failure") as rf:
            try:
                llm._generate([("user", "hi")])
            except openai.BadRequestError:
                pass
            rf.assert_not_called()


class TestGetLlmWiring:
    """get_llm: swap-then-resolve ordering, breaker key set, timeout honored."""

    def test_litellm_model_wired_to_proxy_with_breaker_key(self):
        mod = _llm_mod()
        fake = _FakeSettings(CODE_WRITER_MODEL="litellm/standardcompute")
        with mock.patch.object(mod, "settings", fake), \
             mock.patch.object(mod, "effective_model", side_effect=lambda p, fallback=None: p):
            llm = mod.get_llm(model="litellm/standardcompute", temperature=0.4, timeout=600)
        assert llm.openai_api_base == "https://litellm.example/v1"  # type: ignore[attr-defined]
        assert llm.model_name == "standardcompute"                   # type: ignore[attr-defined]
        assert llm._breaker_name == "litellm/standardcompute"        # type: ignore[attr-defined]
        # Streaming REQUIRED for litellm: proxy gateway 504s non-streaming
        # generations > ~60s (measured; reasoning model thinks longer).
        assert llm.streaming is True                                 # type: ignore[attr-defined]

    def test_zai_model_untouched(self):
        mod = _llm_mod()
        fake = _FakeSettings()
        with mock.patch.object(mod, "settings", fake), \
             mock.patch.object(mod, "effective_model", side_effect=lambda p, fallback=None: p):
            llm = mod.get_llm(model="glm-5.2")
        assert llm.openai_api_base == "https://zai.example/v4"  # type: ignore[attr-defined]
        assert llm.model_name == "glm-5.2"                       # type: ignore[attr-defined]
        assert llm._breaker_name == "glm-5.2"                    # type: ignore[attr-defined]
        assert llm.streaming is False                            # type: ignore[attr-defined]

    def test_code_writer_timeout_reaches_llm(self):
        """§D: AGENT_LLM_TIMEOUTS threads timeout= into get_llm."""
        import agents.subagents as sub

        # non-lazy snapshot read at import: verify the map exists + keys by stem
        assert "code-writer" in sub._AGENT_LLM_TIMEOUTS or "code-writer" in sub.AGENT_LLM_TIMEOUTS


# ── S1 classified retry ladder (job12-fix-plan-FINAL §S1) ────────────────────


class _FakeResponse:
    """Minimal httpx.Response stand-in — only status_code/headers/request read."""

    def __init__(self, status_code=429, headers=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.request = mock.Mock()


def _rate_limit(body=None, headers=None):
    import openai

    return openai.RateLimitError(
        message="rate limited", response=_FakeResponse(429, headers), body=body
    )


def _rate_limit_every_call(**kw):
    """side_effect factory — a Mock side_effect that *returns* an exception
    would hand it back as a result value, not raise it."""

    def _raise(*_a, **_k):
        raise _rate_limit(**kw)

    return _raise


def _retry_cfg(**over):
    """Explicit ladder config (the shape ``_retry_settings`` must return)."""
    cfg = {
        "transient_max": 2,
        "ratelimit_max": 6,
        "backoff_base": 1.5,
        "ratelimit_base": 2.0,
        "backoff_cap": 30.0,
        "backoff_floor": 1.0,
    }
    cfg.update(over)
    return cfg


class TestRateLimitLadderSettings:
    """Rate-limit class gets its own ladder; the transient class is locked."""

    def test_default_settings(self):
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", _FakeSettings()):
            cfg = mod._retry_settings()
        assert cfg["ratelimit_max"] == 6  # was 3 — job 12 burned it in ~7.5s
        assert cfg["ratelimit_base"] == 2.0  # was the shared 1.5
        assert cfg["backoff_floor"] == 1.0  # NEW: uniform(0, x) could sleep ~0s
        assert cfg["backoff_cap"] == 30.0
        # transient class UNCHANGED (regression lock)
        assert cfg["transient_max"] == 2
        assert cfg["backoff_base"] == 1.5

    def test_settings_file_defaults_match_the_ladder(self):
        """``_retry_settings``'s getattr fallbacks are NOT what prod reads —
        ``config/settings.py``'s decouple defaults always shadow them. Lock those
        so a config revert can't silently re-arm the 3-attempt ladder that job 12
        burned through."""
        import re
        from pathlib import Path

        src = (Path(ROOT) / "webapp" / "config" / "settings.py").read_text(encoding="utf-8")

        def default(key):
            found = re.search(rf'^{key} = config\("{key}", default=([^,]+),', src, re.M)
            assert found, f"{key} missing from settings.py"
            return float(found.group(1))

        assert default("LLM_RETRY_RATELIMIT_MAX") == 6.0
        assert default("LLM_RETRY_RATELIMIT_BASE") == 2.0
        assert default("LLM_RETRY_BACKOFF_FLOOR") == 1.0
        assert default("LLM_RETRY_BACKOFF_CAP") == 30.0
        assert default("LLM_RETRY_TRANSIENT_MAX") == 2.0
        assert default("LLM_RETRY_BACKOFF_BASE") == 1.5

    def test_per_class_delay_bounds(self):
        """transient keeps uniform(0, backoff_base·2^n); rate-limit is
        floor..min(cap, ratelimit_base·2^n). Patching random.uniform to hand
        back its bounds makes the ceiling arithmetic observable."""
        mod = _llm_mod()
        with mock.patch("random.uniform", lambda lo, hi: (lo, hi)):
            assert mod._backoff_delay(1, _retry_cfg(), "transient") == (0.0, 3.0)
            assert mod._backoff_delay(3, _retry_cfg(), "transient") == (0.0, 12.0)
            assert mod._backoff_delay(1, _retry_cfg(), "rate_limit") == (1.0, 4.0)
            assert mod._backoff_delay(4, _retry_cfg(), "rate_limit") == (1.0, 30.0)
            assert mod._backoff_delay(6, _retry_cfg(), "rate_limit") == (1.0, 30.0)


class TestRateLimitLadderBehavior:
    def _exhaust(self, mod, fn, cfg_over=None, **kw):
        import openai

        delays = []
        with pytest.raises(openai.RateLimitError):
            mod._retry_classified_sync(
                fn, _retry_cfg(**(cfg_over or {})), sleep=delays.append, **kw
            )
        return delays

    def test_six_attempts_then_raises(self):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        delays = self._exhaust(mod, fn)
        assert fn.call_count == 7  # original call + 6 retries
        assert len(delays) == 6

    def test_backoff_doubles_from_base_2_and_caps_at_30(self):
        mod = _llm_mod()
        bounds = []
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        with mock.patch("random.uniform", lambda lo, hi: bounds.append((lo, hi)) or hi):
            self._exhaust(mod, fn)
        assert bounds == [
            (1.0, 4.0),
            (1.0, 8.0),
            (1.0, 16.0),
            (1.0, 30.0),  # base 2.0 → 32, capped
            (1.0, 30.0),
            (1.0, 30.0),
        ]

    def test_floor_enforced(self):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        delays = self._exhaust(mod, fn)
        assert len(delays) == 6
        assert all(d >= 1.0 for d in delays), delays

    def test_cap_enforced(self):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        delays = self._exhaust(mod, fn, {"ratelimit_base": 1000.0})
        assert all(d <= 30.0 for d in delays), delays

    def test_worst_case_bounded_under_job_wall_clock(self):
        """6 × 30s = 180s, 17% of the 900s job wall."""
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        delays = self._exhaust(mod, fn, {"backoff_cap": 30.0})
        assert sum(delays) <= 6 * 30.0


class TestTransientClassUnchanged:
    """Lock the transient ladder so a future edit can't silently move it."""

    def test_two_retries_then_raises(self):
        import openai

        mod = _llm_mod()
        fn = mock.Mock(side_effect=openai.APITimeoutError(request=mock.Mock()))
        delays = []
        with pytest.raises(openai.APITimeoutError):
            mod._retry_classified_sync(fn, _retry_cfg(), sleep=delays.append)
        assert fn.call_count == 3  # original + 2 retries
        assert len(delays) == 2
        assert all(0.0 <= d <= min(30.0, 1.5 * 2 ** (i + 1)) for i, d in enumerate(delays))

    def test_no_floor_on_transient(self):
        """The floor is a rate-limit-class feature; transient keeps uniform(0, x)."""
        mod = _llm_mod()
        with mock.patch("random.uniform", lambda lo, hi: (lo, hi)):
            lo, _hi = mod._backoff_delay(1, _retry_cfg(), "transient")
        assert lo == 0.0


class TestRetryAfterJitter:
    def test_honored_within_twenty_percent(self):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call(headers={"retry-after": "10"}))
        delays = self._exhaust(mod, fn)
        assert len(delays) == 6
        assert all(8.0 <= d <= 12.0 for d in delays), delays

    def _exhaust(self, mod, fn):
        import openai

        delays = []
        with pytest.raises(openai.RateLimitError):
            mod._retry_classified_sync(fn, _retry_cfg(), sleep=delays.append)
        return delays

    def test_jittered_value_clamped_to_floor_and_cap(self):
        mod = _llm_mod()
        with mock.patch("random.uniform", lambda lo, hi: hi):  # top of jitter window
            assert mod._backoff_delay(1, _retry_cfg(), "rate_limit", retry_after=10.0) == 12.0
            assert mod._backoff_delay(1, _retry_cfg(), "rate_limit", retry_after=60.0) == 30.0
            assert mod._backoff_delay(1, _retry_cfg(), "rate_limit", retry_after=0.2) == 1.0


class TestExhaustionRecord:
    """Exhaustion must land somewhere greppable (job 12's 429 never did)."""

    def _capture(self, caplog, mod, fn, cfg=None, **kw):
        import openai

        with caplog.at_level(logging.ERROR, logger="agents.llm"), \
                pytest.raises(openai.RateLimitError):
            mod._retry_classified_sync(fn, _retry_cfg(**(cfg or {})), sleep=lambda d: None, **kw)
        recs = [r for r in caplog.records if r.getMessage().startswith("llm-retry-exhausted")]
        assert len(recs) == 1
        return recs[0]

    def test_rate_limit_exhaustion_emits_error(self, caplog):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call(body={"error": {"code": 1302}}))
        rec = self._capture(caplog, mod, fn, model="glm-5.2")
        assert rec.levelno == logging.ERROR
        msg = rec.getMessage()
        assert "class=rate_limit" in msg
        assert "model=glm-5.2" in msg
        assert "RateLimitError" in msg
        assert "code=1302" in msg  # Z.AI's 1302, not just "429"
        assert "attempts=6" in msg
        assert "slept=" in msg

    def test_total_sleep_accumulated(self, caplog):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        with mock.patch("random.uniform", lambda lo, hi: hi):  # 4+8+16+30+30+30
            rec = self._capture(caplog, mod, fn)
        assert "slept=118.0s" in rec.getMessage()

    def test_model_placeholder_when_unknown(self, caplog):
        mod = _llm_mod()
        fn = mock.Mock(side_effect=_rate_limit_every_call())
        rec = self._capture(caplog, mod, fn)
        assert "model=?" in rec.getMessage()

    def test_transient_exhaustion_emits_record_too(self, caplog):
        import openai

        mod = _llm_mod()
        fn = mock.Mock(side_effect=openai.APITimeoutError(request=mock.Mock()))
        with caplog.at_level(logging.ERROR, logger="agents.llm"), \
                pytest.raises(openai.APITimeoutError):
            mod._retry_classified_sync(fn, _retry_cfg(), sleep=lambda d: None, model="glm-5.2")
        recs = [r for r in caplog.records if r.getMessage().startswith("llm-retry-exhausted")]
        assert len(recs) == 1
        msg = recs[0].getMessage()
        assert "class=transient" in msg and "attempts=2" in msg


class TestAsyncLadder:
    def test_async_backoff_is_actually_awaited(self):
        """asyncio.sleep handed to a sync helper leaks a bare coroutine → the
        async path had NO backoff. The await must happen in the async loop."""
        import openai

        mod = _llm_mod()
        delays = []

        async def sleeper(d):
            delays.append(d)

        async def boom():
            raise _rate_limit()

        async def run():
            with pytest.raises(openai.RateLimitError):
                await mod._retry_classified_async(boom, _retry_cfg(), asleep=sleeper)

        asyncio.run(run())
        assert len(delays) == 6
        assert all(d >= 1.0 for d in delays)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


class TestMisconfigGuard:
    """Prefix set but routing unavailable → LOUD failure, never silent Z.AI 400."""

    def test_litellm_prefix_without_key_raises(self):
        mod = _llm_mod()
        fake = _FakeSettings(LITELLM_API_KEY="", CODE_WRITER_MODEL="litellm/standardcompute")
        with mock.patch.object(mod, "settings", fake), \
             mock.patch.object(mod, "effective_model", side_effect=lambda p, fallback=None: p):
            try:
                mod.get_llm(model="litellm/standardcompute")
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "LITELLM_API_KEY" in str(e)

    def test_litellm_prefix_disabled_raises(self):
        mod = _llm_mod()
        fake = _FakeSettings(LITELLM_ENABLED=False, LITELLM_API_KEY="k")
        with mock.patch.object(mod, "settings", fake), \
             mock.patch.object(mod, "effective_model", side_effect=lambda p, fallback=None: p):
            try:
                mod.get_llm(model="litellm/standardcompute")
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "LITELLM_ENABLED" in str(e)

    def test_bare_zai_model_unaffected_by_guard(self):
        mod = _llm_mod()
        fake = _FakeSettings(LITELLM_API_KEY="")  # no key, but no prefix either
        with mock.patch.object(mod, "settings", fake), \
             mock.patch.object(mod, "effective_model", side_effect=lambda p, fallback=None: p):
            llm = mod.get_llm(model="glm-5-turbo")  # must NOT raise
            assert llm.openai_api_base == "https://zai.example/v4"


# ── Streaming retry lane [job-84 chemistwarehouse] ───────────────────────────


class TestStreamingRetryLane:
    """``streaming=True`` models (every litellm call — get_llm sets it to dodge
    the proxy's ~60s non-stream 504) route invoke() through ``_stream``/
    ``_astream``, NOT ``_generate``: job 84's code_writer died on the FIRST
    mid-stream transport error because the classified retry only wrapped
    ``_generate``. The streaming lane must carry the same classified retry +
    breaker recording, and bare httpx transport errors must classify transient.
    """

    def _make(self, mod, model="litellm/standardcompute"):
        llm = mod.ClassifiedRetryChatOpenAI(
            model_name=model,
            openai_api_base="https://litellm.example/v1",
            openai_api_key="k",
            max_retries=0,
        )
        llm._breaker_name = model
        return llm

    @staticmethod
    def _chunk(text):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        return ChatGenerationChunk(message=AIMessageChunk(content=text))

    def test_remote_protocol_error_is_classified_transient(self):
        import httpx

        mod = _llm_mod()
        # job 84's actual death: openai's SSE reader has no `except httpx.*`,
        # so the proxy tearing down the chunked response surfaced as a BARE
        # httpx exception that missed every openai-based arm.
        assert isinstance(
            httpx.RemoteProtocolError("incomplete chunked read"),
            mod._TRANSIENT_ERRORS,
        )
        assert isinstance(httpx.ConnectError("reset"), mod._TRANSIENT_ERRORS)

    def test_stream_retries_transient_and_yields_same_chunks(self):
        import httpx
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)
        a, b = self._chunk("hel"), self._chunk("lo")
        attempts = []

        def flaky_super_stream(self_, messages, stop=None, run_manager=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.RemoteProtocolError("peer closed connection")
            return iter([a, b])

        with mock.patch("random.uniform", lambda lo, hi: 0.0), \
             mock.patch.object(ChatOpenAI, "_stream", flaky_super_stream):
            got = list(llm._stream([("user", "hi")]))
        assert len(attempts) == 2, "the transient death must be retried"
        assert got == [a, b]

    def test_mid_stream_death_after_partial_chunks_is_retried(self):
        """A stream that yields THEN dies raises from iteration — the retry
        wraps the full consumption, so only the complete second stream is
        returned (an LLM stream cannot be resumed mid-flight)."""
        import httpx
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)
        attempts = []

        def flaky_super_stream(self_, messages, stop=None, run_manager=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                yield self._chunk("par")
                raise httpx.RemoteProtocolError("incomplete chunked read")
            yield self._chunk("full")

        with mock.patch("random.uniform", lambda lo, hi: 0.0), \
             mock.patch.object(ChatOpenAI, "_stream", flaky_super_stream):
            got = [c.message.content for c in llm._stream([("user", "hi")])]
        assert len(attempts) == 2
        assert got == ["full"]

    def test_stream_records_success_under_configured_key(self):
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)

        with mock.patch.object(
            ChatOpenAI, "_stream",
            lambda self_, *a, **k: iter([self._chunk("ok")]),
        ), mock.patch.object(mod, "record_success") as rs:
            list(llm._stream([("user", "hi")]))
        rs.assert_called_once_with("litellm/standardcompute")

    def test_stream_exhaustion_records_failure_and_raises(self):
        import httpx
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)

        def always_dies(self_, messages, stop=None, run_manager=None, **kwargs):
            raise httpx.RemoteProtocolError("teardown")

        with mock.patch.object(mod, "_retry_settings", return_value=_retry_cfg(
                transient_max=0)), \
             mock.patch.object(ChatOpenAI, "_stream", always_dies), \
             mock.patch.object(mod, "record_failure") as rf:
            with pytest.raises(httpx.RemoteProtocolError):
                list(llm._stream([("user", "hi")]))
        rf.assert_called_once_with("litellm/standardcompute")

    def test_stream_caller_bug_not_recorded_and_not_retried(self):
        import openai
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)
        calls = []

        def bad_request(self_, messages, stop=None, run_manager=None, **kwargs):
            calls.append(1)
            raise openai.BadRequestError(message="bad", response=mock.Mock(), body=None)

        with mock.patch.object(mod, "_retry_settings", return_value=_retry_cfg()), \
             mock.patch.object(ChatOpenAI, "_stream", bad_request), \
             mock.patch.object(mod, "record_failure") as rf:
            with pytest.raises(openai.BadRequestError):
                list(llm._stream([("user", "hi")]))
        assert len(calls) == 1, "caller bugs must fail fast, no retry"
        rf.assert_not_called()

    def test_astream_retries_transient(self):
        import httpx
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)
        a = self._chunk("x")
        attempts = []

        async def flaky_super_astream(self_, messages, stop=None, run_manager=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.RemoteProtocolError("boom")
            yield a

        async def run():
            return [c async for c in llm._astream([("user", "hi")])]

        with mock.patch("random.uniform", lambda lo, hi: 0.0), \
             mock.patch.object(ChatOpenAI, "_astream", flaky_super_astream):
            got = asyncio.run(run())
        assert len(attempts) == 2
        assert got == [a]

    def test_stream_override_survives_bind_tools(self):
        """bind_tools returns a RunnableBinding that delegates to the wrapped
        model's _stream — the retry must apply there too (the react loop only
        ever calls the bound runnable)."""
        import httpx
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI

        mod = _llm_mod()
        llm = self._make(mod)

        @tool
        def noop() -> str:
            """does nothing"""
            return ""

        attempts = []

        def flaky_super_stream(self_, messages, stop=None, run_manager=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise httpx.RemoteProtocolError("teardown")
            return iter([self._chunk("ok")])

        bound = llm.bind_tools([noop])
        with mock.patch("random.uniform", lambda lo, hi: 0.0), \
             mock.patch.object(ChatOpenAI, "_stream", flaky_super_stream):
            # the public .stream() unwraps ChatGenerationChunks to messages
            got = [c.content for c in bound.stream([("user", "hi")])]
        assert len(attempts) == 2, "bound runnable must route through the retry lane"
        assert "ok" in got  # public .stream() also appends a trailing aggregate chunk
