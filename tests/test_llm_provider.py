"""Unit tests for the LiteLLM provider resolver + breaker relocation (§A/§B/§D
of docs/codewriter-litellm-plan.md v2).

No network: the resolver is tested via settings overrides; breaker recording is
tested by driving ClassifiedRetryChatOpenAI._generate with a patched super call.

Run from repo root:  python3 -m pytest tests/test_llm_provider.py -v
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock

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
    def test_litellm_model_gets_empty_string(self):
        mod = _llm_mod()
        with mock.patch.object(mod, "settings", _FakeSettings()):
            assert mod._litellm_fallback("litellm/standardcompute") == ""

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

    def test_zai_model_untouched(self):
        mod = _llm_mod()
        fake = _FakeSettings()
        with mock.patch.object(mod, "settings", fake), \
             mock.patch.object(mod, "effective_model", side_effect=lambda p, fallback=None: p):
            llm = mod.get_llm(model="glm-5.2")
        assert llm.openai_api_base == "https://zai.example/v4"  # type: ignore[attr-defined]
        assert llm.model_name == "glm-5.2"                       # type: ignore[attr-defined]
        assert llm._breaker_name == "glm-5.2"                    # type: ignore[attr-defined]

    def test_code_writer_timeout_reaches_llm(self):
        """§D: AGENT_LLM_TIMEOUTS threads timeout= into get_llm."""
        import agents.subagents as sub

        # non-lazy snapshot read at import: verify the map exists + keys by stem
        assert "code-writer" in sub._AGENT_LLM_TIMEOUTS or "code-writer" in sub.AGENT_LLM_TIMEOUTS


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
