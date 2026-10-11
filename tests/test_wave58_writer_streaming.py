"""[wave-58 guardian change 4] Writer-scoped streaming.

RCA A7 (batch 613-616): the writer's client was built NON-streaming unless
the model carried a ``litellm/`` prefix, so a slow generation produced ZERO
bytes until the whole per-call timeout expired — 615s of dead air against a
window that had less than that left (A7), invisible to the no-chunk
watchdog because the watchdog only sees a request that has started
delivering. Streaming keeps bytes flowing: the watchdog gets a heartbeat,
the transcript shows progress, and a hung provider is killed in
LLM_NO_CHUNK_TIMEOUT seconds instead of the full per-call budget.

Scope is the POINT of this wave: ONLY the writer lane streams. The analysis
and tester agents keep the litellm-only default — their calls are short and
their prompts small, so streaming buys them nothing.

Every assertion here is on the BUILT client object (guardian: behavior, not
a source-string pin).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "webapp")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("CODE_WRITER_EMBED_MODE", "off")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from unittest import mock  # noqa: E402

from agents import subagents as sub  # noqa: E402
from agents.llm import get_llm  # noqa: E402


def _client_of(agent):
    """Pull the ChatOpenAI client out of a built create_react_agent graph:
    the 'agent' PregelNode binds a RunnableCallable whose closure holds the
    prompt→model RunnableSequence; the model sits in the _ChatModelBinding
    step."""
    func = vars(agent.nodes["agent"].bound)["func"]
    cells = [c.cell_contents for c in (func.__closure__ or [])]
    seq = [c for c in cells if type(c).__name__ == "RunnableSequence"][0]
    binding = [
        s for s in seq.steps if type(s).__name__ == "_ChatModelBinding"
    ][0]
    return binding.bound


def _writer_agent(**over):
    return sub.create_code_writer(
        site_slug="streamp", template_code="x = 1\n",
        embed_mode="off", embed_kind="file", **over
    )


def _site_agent():
    return sub.create_site_analyzer(site_slug="streamp")


def test_writer_client_streams():
    """The built writer's client carries streaming=True (RCA A7 closed)."""
    assert _client_of(_writer_agent()).streaming is True


def test_other_agents_do_not_stream():
    """Writer-scoped ONLY: the site analyzer keeps the litellm-only
    default (its client is non-streaming on a non-litellp model)."""
    assert _client_of(_site_agent()).streaming is False


def test_get_llm_default_stays_litellm_only():
    """The helper's default behavior is unchanged for every existing
    caller: no streaming flag → litellm-only."""
    assert get_llm(streaming=None).streaming is False
    assert get_llm().streaming is False


def test_kill_switch_reverts_the_writer_lane(monkeypatch):
    """CODE_WRITER_STREAMING=0 is the rollback: the writer's client goes
    back to the litellm-only default with no code change."""
    monkeypatch.setenv("CODE_WRITER_STREAMING", "0")
    assert _client_of(_writer_agent()).streaming is False


@pytest.mark.parametrize("flag", ["false", "no", "off", " 0 "])
def test_kill_switch_accepts_the_obvious_spellings(monkeypatch, flag):
    monkeypatch.setenv("CODE_WRITER_STREAMING", flag)
    assert sub._code_writer_streams() is False


def test_no_chunk_watchdog_is_active_on_the_streaming_lane():
    """Streaming only pays off if the watchdog reads it: the writer's
    client must keep the classified-retry layer (whose streaming path
    raises LLMNoChunkTimeout) and the watchdog budget must stay finite."""
    from agents import llm as llm_mod

    client = _client_of(_writer_agent())
    assert llm_mod.LLMNoChunkTimeout is not None  # the watchdog class ships
    assert llm_mod._no_chunk_timeout() > 0  # 0 disables the guard
    # the streaming client still goes through the classified retry wrapper
    assert type(client).__name__ == "ClassifiedRetryChatOpenAI"


def test_writer_model_and_timeout_are_untouched():
    """Streaming is the ONLY lane change: model and per-call budget keep
    their wave-52 values (regression guard on the same client)."""
    from django.conf import settings

    client = _client_of(_writer_agent())
    assert client.model_name == settings.CODE_WRITER_MODEL.replace(
        "litellm/", ""
    )
    assert float(client.request_timeout) == float(
        settings.CODE_WRITER_LLM_TIMEOUT
    )


def test_writer_lane_carries_its_own_no_chunk_budget():
    """[guardian change 5] Streaming canary (writer-shaped prefill, n=5):
    TTFC median 38.7s, p95 141.3s — only 1.7x under the global 240s. The
    streaming lane must carry its own 300s budget, per-client, WITHOUT
    loosening the global for the short analysis lanes."""
    from agents import llm as llm_mod

    writer = _client_of(_writer_agent())
    assert getattr(writer, "_no_chunk_timeout_override", None) == 300.0
    assert llm_mod._no_chunk_timeout(writer) == 300.0
    # other lanes keep the global
    other = _client_of(_site_agent())
    assert getattr(other, "_no_chunk_timeout_override", None) is None
    assert llm_mod._no_chunk_timeout(other) == float(
        llm_mod.settings.LLM_NO_CHUNK_TIMEOUT
    )


def test_no_chunk_budget_override_is_env_tunable(monkeypatch):
    monkeypatch.setenv("WRITER_LLM_NO_CHUNK_TIMEOUT", "360")
    assert sub._writer_no_chunk_timeout() == 360.0
    monkeypatch.setenv("WRITER_LLM_NO_CHUNK_TIMEOUT", "nonsense")
    assert sub._writer_no_chunk_timeout() == 300.0  # fallback, never 0/nonsense


def test_streaming_flag_survives_the_build_when_litellm_routed():
    """A litellm-prefixed writer model already streamed; the explicit flag
    must not break that lane (routing resolves through the proxy)."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CODE_WRITER_STREAMING", None)
        try:
            client = get_llm(model="litellm/glm-5.3-flash", streaming=True)
        except RuntimeError:
            pytest.skip("litellm routing unavailable in this env")
    assert client.streaming is True
