"""[wave-32 C3] Fingerprint field/fields repair — multi-field verdicts.

``_remediation_fingerprint`` (and its writer_memory twin) keys on
``remediation.field`` only. The tester's multi-field verdicts carry
``remediation.fields`` (a LIST — the same shape the remap arm consumes at
``remediation.get("fields")``), so every multi-field diagnosis hashes with
``field=""``: distinct field sets collide, and the wave-29 B5 retry lines
seeded from the first verdict never match the second (exact/degraded
matching is keyed on the same hex). Repair: the field component is
``rem.get("field")`` or the sorted comma-join of ``rem.get("fields")`` —
restoring wave-22 B3's grace and wave-29 B5's matching for multi-field
verdicts. Single-field behavior is byte-identical.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import importlib  # noqa: E402

rat = importlib.import_module("webapp.agents.nodes.route_after_testing")
wm = importlib.import_module("src.writer_memory")


def _report(fields=None, field=None, issue_types=("WRONG_TYPE",)):
    rem = {"target": "mapping"}
    if field is not None:
        rem["field"] = field
    if fields is not None:
        rem["fields"] = fields
    return {
        "remediation": rem,
        "issues": [{"issue_type": t} for t in issue_types],
        "crash_error": "",
    }


class TestMultiFieldFingerprint:
    def test_multi_field_fingerprint_matches(self):
        """A multi-field verdict and its equivalent comma-joined single-field
        verdict hash IDENTICALLY (B3 grace + B5 matching restored)."""
        fp_fields = rat._remediation_fingerprint(
            _report(fields=["price", "title"])
        )
        fp_joined = rat._remediation_fingerprint(_report(field="price,title"))
        assert fp_fields, "multi-field verdict must produce a fingerprint"
        assert fp_fields == fp_joined

    def test_distinct_field_sets_differ(self):
        """price+title and price+availability are DIFFERENT asks — they must
        not collide on field='' any more."""
        a = rat._remediation_fingerprint(_report(fields=["price", "title"]))
        b = rat._remediation_fingerprint(_report(fields=["price", "availability"]))
        assert a != b

    def test_single_field_behavior_unchanged(self):
        assert rat._remediation_fingerprint(_report(field="price")) == (
            rat._remediation_fingerprint(_report(field="price"))
        )

    def test_writer_memory_twin_keys_fields_too(self):
        """The writer_memory mirror must agree with the router's fingerprint
        for multi-field verdicts (B5 exact/degraded matching key)."""
        rep = _report(fields=["price", "title"])
        assert wm.structural_fingerprint(rep) == rat._remediation_fingerprint(rep)
        parts = wm.fingerprint_parts(rep)
        assert parts["field"] == "price,title"
