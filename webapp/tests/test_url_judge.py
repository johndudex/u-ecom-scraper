"""Unit tests for the LLM pre-visit listing-URL selector (webapp/agents/nodes/url_judge.py).

The LLM is mocked — these test the parsing/normalization/safe-default contract,
not model behavior. Run inside the Django container::

    docker compose exec django bash -c "cd /app/webapp && python -m pytest tests/test_url_judge.py -q"
"""

import os
import sys

_WEBAPP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WEBAPP not in sys.path:
    sys.path.insert(0, _WEBAPP)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

if not getattr(django, "_setup_done_", False):
    django.setup()


# Aya-style candidate nav links (subset of the real homepage extraction).
AYA = [
    {"href": "https://www.ayahealthcare.com/travel-nursing/", "text": "Travel"},
    {"href": "https://www.ayahealthcare.com/travel-nursing/travel-nursing-jobs/", "text": "Search jobs"},
    {"href": "https://www.ayahealthcare.com/healthcare-jobs/allied/type/travel/", "text": "Search jobs"},
    {"href": "https://www.ayahealthcare.com/travel-nursing/travel-nurse-pay/", "text": "Pay & benefits"},
    {"href": "https://www.ayahealthcare.com/travel-nursing/travel-nurse-housing/", "text": "Housing"},
    {"href": "https://www.ayahealthcare.com/healthcare-jobs/nursing/", "text": "Nursing jobs"},
]


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    def invoke(self, messages):
        return _FakeResp(self._content)


def _patch_llm(monkeypatch, content):
    import agents.llm as _llm

    monkeypatch.setattr(_llm, "get_small_llm", lambda temperature=0.0: _FakeLLM(content))


def test_judge_parses_fenced_json_and_ranks(monkeypatch):
    """Realistic GLM response (```json fence): 'Search jobs' URLs correct, marketing wrong."""
    response = (
        "```json\n"
        '{"ranking": ['
        '{"url": "https://www.ayahealthcare.com/travel-nursing/travel-nursing-jobs/", '
        '"text": "Search jobs", "verdict": "correct", "confidence": 0.9, "reason": "job listing"}, '
        '{"url": "https://www.ayahealthcare.com/healthcare-jobs/allied/type/travel/", '
        '"text": "Search jobs", "verdict": "correct", "confidence": 0.85, "reason": "job listing"}, '
        '{"url": "https://www.ayahealthcare.com/travel-nursing/", "text": "Travel", '
        '"verdict": "wrong", "confidence": 0.95, "reason": "marketing hub"}, '
        '{"url": "https://www.ayahealthcare.com/travel-nursing/travel-nurse-pay/", '
        '"text": "Pay & benefits", "verdict": "wrong", "confidence": 0.9, "reason": "pay info"}'
        '], "model_notes": "ok"}\n'
        "```"
    )
    _patch_llm(monkeypatch, response)
    from agents.nodes.url_judge import judge_candidate_urls, ranked_correct

    j = judge_candidate_urls(AYA, "job_posting", "nursing", "https://www.ayahealthcare.com/")
    correct = ranked_correct(j)
    assert any("travel-nursing-jobs" in u for u in correct)
    assert any("healthcare-jobs/allied" in u for u in correct)
    assert not any("travel-nursing/" == u.rstrip("/").split("ayahealthcare.com")[-1] for u in correct)
    assert not any("travel-nurse-pay" in u for u in correct)
    assert j["model_notes"] == "ok"
    # ranking order preserved (best-first)
    assert j["ranking"][0]["url"].endswith("travel-nursing-jobs/")


def test_judge_normalizes_verdict_case_and_confidence(monkeypatch):
    response = (
        '{"ranking": [{"url": "https://x/jobs", "text": "Jobs", "verdict": "CORRECT", '
        '"confidence": "0.7", "reason": "r"}], "model_notes": ""}'
    )
    _patch_llm(monkeypatch, response)
    from agents.nodes.url_judge import judge_candidate_urls

    j = judge_candidate_urls([{"href": "https://x/jobs", "text": "Jobs"}], "job_posting", "nursing", "https://x")
    assert j["ranking"][0]["verdict"] == "correct"
    assert j["ranking"][0]["confidence"] == 0.7


def test_judge_bare_json_without_fence(monkeypatch):
    _patch_llm(monkeypatch, '{"ranking": [{"url": "https://x/collection", "verdict": "correct", '
                            '"confidence": 0.8, "reason": ""}], "model_notes": ""}')
    from agents.nodes.url_judge import judge_candidate_urls, ranked_correct

    j = judge_candidate_urls([{"href": "https://x/collection", "text": "Shop"}], "product", "shoes", "https://x")
    assert ranked_correct(j) == ["https://x/collection"]


def test_judge_malformed_returns_safe_default(monkeypatch):
    _patch_llm(monkeypatch, "Sorry, I can't help with that.")
    from agents.nodes.url_judge import judge_candidate_urls

    j = judge_candidate_urls(AYA, "job_posting", "nursing", "https://x")
    assert j["ranking"] == []
    assert "error" in j


def test_judge_invoke_exception_safe(monkeypatch):
    import agents.llm as _llm

    class _Boom:
        def invoke(self, messages):
            raise RuntimeError("network down")

    monkeypatch.setattr(_llm, "get_small_llm", lambda temperature=0.0: _Boom())
    from agents.nodes.url_judge import judge_candidate_urls

    j = judge_candidate_urls([{"href": "https://x/jobs", "text": "Jobs"}], "job_posting", "nursing", "https://x")
    assert j["ranking"] == []
    assert "error" in j


def test_judge_empty_candidates_skips_llm():
    from agents.nodes.url_judge import judge_candidate_urls

    j = judge_candidate_urls([], "job_posting", "nursing", "https://x")
    assert j["ranking"] == []
    assert "error" not in j  # no LLM call attempted


def test_ranked_correct_orders_by_confidence_and_dedups():
    from agents.nodes.url_judge import ranked_correct

    j = {
        "ranking": [
            {"url": "https://x/a", "verdict": "correct", "confidence": 0.4},
            {"url": "https://x/b", "verdict": "correct", "confidence": 0.9},
            {"url": "https://x/a", "verdict": "correct", "confidence": 0.95},  # dup of a
            {"url": "https://x/c", "verdict": "wrong", "confidence": 0.99},
        ]
    }
    assert ranked_correct(j) == ["https://x/a", "https://x/b"]
