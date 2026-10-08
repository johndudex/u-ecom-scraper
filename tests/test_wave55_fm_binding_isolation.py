"""[wave-55 gate fix] a late test must bind the REAL src.artifacts client.

The wave-55 gate came back RED on
``test_wave55_template_path_anchor.py::TestNoParentRelativeInsert::
test_the_runtime_served_fm_copy_is_clean_too`` — it passed isolated and died
only in the full suite, on
``AttributeError: module 'src.artifacts' has no attribute 'read_text'``.

Root cause is not the anchor pin: it is test hygiene in the two files that
exec run_execution's functions out of source. Both plant a 2-attribute
``ModuleType("src.artifacts")`` in ``sys.modules`` (so the function-local
``import src.artifacts`` in the extracted code resolves to something
harmless) and never take it down. pytest imports every module in ``tests/``
before the first test runs, so the stub shadows the real client for the whole
session and every later ``import src.artifacts`` — the anchor pin's — binds
the stub. Their own tests stay green, which is why nothing caught it until a
test that genuinely reads the File Master landed behind them.

The stubs are gone rather than scoped: test_f8 now binds its FM stand-in at
both binding sites inside the two tests that reach the fallback
(test_api_create_persist._patch_fm's idiom), and test_f9 never planted one.
A ``scope="module"`` stub fixture was not a safe middle ground — in a
mixed-rootdir invocation (``../tests/x.py`` next to ``tests/y.py``, which is
what the full-suite gate does) pytest requests that fixture for OTHER
modules' tests too, so the stub stayed bound straight through this canary.
This canary imports the client the way a late test does — at CALL time, never
at module level, so it sees whatever any file left behind — and asserts the
FM surface is intact.

Order-sensitive by design: run it BEHIND the files that used to plant.

    pytest ../tests/test_f8_f16_output_selection.py \\
           ../tests/test_f9_quality_gate.py \\
           ../tests/test_wave55_fm_binding_isolation.py

Alphabetical collection (f* < w*) puts it there in the gate's run too.
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

# The full FM surface. The stub carries only latest_output_key + read, so the
# first missing name identifies it unmistakably.
_FM_SURFACE = (
    "read",
    "read_text",
    "read_json",
    "write",
    "write_text",
    "write_json",
    "exists",
    "list_keys",
    "delete",
)


class TestTheLateImportBindsTheRealClient:
    def test_a_late_import_gets_the_full_fm_surface(self):
        import src.artifacts as artifacts

        missing = [n for n in _FM_SURFACE if not hasattr(artifacts, n)]
        assert not missing, (
            f"src.artifacts is missing {missing} — a 2-attr ModuleType stub "
            "is still bound at sys.modules['src.artifacts'], so this import "
            "got the run_execution-extraction stub instead of the client. A "
            "late test that reads the File Master dies on exactly this "
            "AttributeError (the wave-55 anchor pin is the one that caught "
            "it)."
        )

    def test_read_still_accepts_the_timeout_read_text_passes(self):
        """``read_text()`` calls ``read(key, timeout=...)``. A stub `read`
        spelled ``lambda key: ...`` turns every later FM read into a
        TypeError — which skills_store swallows into its image fallback, so
        the FM-first contract is defeated silently rather than with a
        crash."""
        import src.artifacts as artifacts

        params = inspect.signature(artifacts.read).parameters
        assert "timeout" in params, (
            f"src.artifacts.read is {inspect.signature(artifacts.read)} — it "
            "cannot take the timeout= read_text() passes, so every FM read "
            "raises TypeError and read_skill() quietly serves the image copy"
        )
