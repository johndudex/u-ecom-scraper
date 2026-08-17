"""F18: final-retry sentinel must be set by the human_approval NODE, and
route_from_human_approval must return a plain node name (never Command) —
a conditional-edge path fn returning Command(update=...) raises
``TypeError: unhashable type: 'dict'`` on langgraph 1.2.10, killing the
resume task and losing the FINAL_RETRY_SENTINEL update.

Pure-python (no Django): route_from_human_approval is imported with a stubbed
package context.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_STUBS = {}


def _import_graph_fn():
    """Import webapp.agents.graph.route_from_human_approval with heavy deps stubbed."""
    if "route_fn" in _STUBS:
        return _STUBS["route_fn"]

    def _mk(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    # stub the import surface graph.py needs before Django/etc. Track what we
    # created so it can be removed afterward — a leaked empty `bs4`/`httpx`
    # stub in sys.modules breaks sibling tests that import the REAL modules
    # (e.g. pagination tests importing traversal.py → from bs4 import ...).
    _installed = []
    for name in [
        "django", "django.conf", "django.conf.settings",
        "langchain", "langchain_core", "celery", "celery.shared_task",
        "redis", "httpx", "bs4", "model_bakery",
    ]:
        if name not in sys.modules:
            _mk(name)
            _installed.append(name)

    # If webapp.agents.graph is already importable (container env), use it directly.
    try:
        from webapp.agents.graph import route_from_human_approval  # noqa: F401
        _STUBS["route_fn"] = route_from_human_approval
        return route_from_human_approval
    except Exception:
        pass

    # Otherwise load JUST the function source and exec it in isolation — the
    # function only needs `state`, `Command`, `logger`, `FINAL_RETRY_SENTINEL`.
    import re as _re
    import pathlib
    src = pathlib.Path(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "webapp/agents/graph.py")
    ).read_text()
    mfn = _re.search(r"^def route_from_human_approval\(.*?(?=^def |\Z)", src, _re.M | _re.S)
    assert mfn, "route_from_human_approval not found"
    fn_src = mfn.group(0)

    langgraph_types = types.SimpleNamespace(Command=lambda **kw: _FakeCommand(**kw))
    consts = types.SimpleNamespace(
        FINAL_RETRY_SENTINEL=99, MAX_TEST_RETRIES=3, FINAL_RETRY_FAILED="final_retry_failed",
    )
    import logging
    ns = {
        "Command": langgraph_types.Command,
        "FINAL_RETRY_SENTINEL": consts.FINAL_RETRY_SENTINEL,
        "logger": logging.getLogger("t.f18"),
        "__name__": "t_f18",
    }
    exec("import logging\nlogger = logging.getLogger('t.f18')\n" + fn_src, ns)
    _STUBS["route_fn"] = ns["route_from_human_approval"]
    for _n in _installed:
        sys.modules.pop(_n, None)
    return ns["route_from_human_approval"]


class _FakeCommand:
    def __init__(self, **kw):
        self.kw = kw

    def __hash__(self):  # mirrors dict-unhashability crash path
        raise TypeError("unhashable type: 'dict'")


class TestFinalRetrySentinelInNode:
    def test_node_sets_sentinel_for_final_retry_with_feedback(self):
        import importlib.util as iu
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "webapp/agents/nodes/human_approval.py")
        # Static check: the node source contains the sentinel-set logic.
        src = open(p).read()
        assert 'update["test_retry_count"] = FINAL_RETRY_SENTINEL' in src
        assert 'reason == "testing_exhausted"' in src
        assert '_label == "Provide feedback for final retry"' in src
        assert "and feedback" in src  # no-feedback case must NOT set sentinel

    def test_node_no_sentinel_without_feedback(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "webapp/agents/nodes/human_approval.py")
        src = open(p).read()
        # the guard chain requires feedback truthiness before setting the sentinel
        idx_label = src.index('_label == "Provide feedback for final retry"')
        idx_sentinel = src.index('update["test_retry_count"] = FINAL_RETRY_SENTINEL')
        idx_feedback = src.index("and feedback", idx_label)
        assert idx_label < idx_feedback < idx_sentinel


class TestPathFnReturnsPlainString:
    def _base_state(self, **over):
        s = {
            "interrupt_reason": "testing_exhausted",
            "human_response": {"decision": "approve", "label": "Provide feedback for final retry"},
            "human_feedback": "price selector is wrong",
            "test_retry_count": 3,
        }
        s.update(over)
        return s

    def test_final_retry_with_feedback_returns_str(self):
        fn = _import_graph_fn()
        out = fn(self._base_state())
        assert isinstance(out, str), f"expected str, got {type(out)}"
        assert out == "scraper_analyzer"

    def test_final_retry_without_feedback_returns_field_confirmation(self):
        fn = _import_graph_fn()
        out = fn(self._base_state(human_feedback=""))
        assert out == "field_confirmation"

    def test_continue_anyway_returns_field_confirmation(self):
        fn = _import_graph_fn()
        out = fn(self._base_state(human_response={"decision": "approve", "label": "Continue anyway"}))
        assert out == "field_confirmation"

    def test_cancel_returns_end(self):
        fn = _import_graph_fn()
        out = fn(self._base_state(human_response={"decision": "reject", "label": "Cancel"}))
        assert out == "__end__"

    def test_no_command_returns_anywhere(self):
        fn = _import_graph_fn()
        # Exhaustively: no plausible state may produce a Command (would crash the path fn).
        cases = [
            self._base_state(),
            self._base_state(human_feedback=""),
            self._base_state(human_response={"decision": "reject", "label": "Cancel"}),
            self._base_state(interrupt_reason="low_coverage",
                             human_response={"decision": "approve", "label": "Retry content analysis"}),
            self._base_state(interrupt_reason="budget_exhausted_product",
                             human_response={"decision": "approve", "label": "Continue"}),
        ]
        for st in cases:
            out = fn(st)
            assert out is None or isinstance(out, str), f"non-str return for {st.get('interrupt_reason')}: {type(out)}"
