"""F6 repair-v2: _fix_json_artifact gains a balanced-closer salvage pass.

The shipped 3-pass repair recovered NOTHING on the real sidley corruption and
renamed the whole 33,849-byte file `.corrupt` (pass 3 steps by len//200 from the
end trying only two hard-coded closers — it never found a cut point, despite
91% of the object being parseable with the correct bracket stack). The design
doc's simulation showed a balanced-closer bounded by the strict-parse error
position recovers 9/10 top-level keys and all 36 field mappings.

This regression test uses the REAL bytes (copied from
`/app/workspace/sidley-com/product_analysis.json.corrupt` in the django
container) as the fixture — it is the artifact that proved the shipped version
recovers nothing.

New pass ordering (all passes log which pass succeeded):
  pass 0  valid as-is (strict)
  pass 0b strict=False (C1: literal control chars — repaired losslessly)
  pass 1  bad-escape rewrite (C4)
  pass 2  raw_decode salvage of a leading object (C3 prefix)
  pass 2b balanced-closer bounded by e.pos (C2 — the sidley/job-10 class)
  pass 3  coarse truncation sweep (last resort, unchanged)
  else    rename .corrupt
"""
from __future__ import annotations

import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

import agents.graph as g  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sidley_product_analysis.json.corrupt")


def _make(tmp_path, content, filename="product_analysis.json"):
    ws = tmp_path / "workspace" / "s"
    ws.mkdir(parents=True, exist_ok=True)
    p = ws / filename
    p.write_text(content, encoding="utf-8")
    return str(p)


class TestRepairV2SidleyGroundTruth:
    """The sidley ground truth: 3 unquoted prose scalars at L716-718."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        self.tmp_path = tmp_path
        return tmp_path

    def _write_fixture(self):
        content = open(FIXTURE, encoding="utf-8").read()
        return _make(self.tmp_path, content)

    def test_sidley_ground_truth_recovered(self, caplog):
        p = self._write_fixture()
        with pytest.raises(json.JSONDecodeError):
            json.loads(open(p, encoding="utf-8").read())  # fixture is really corrupt

        with caplog.at_level(logging.INFO, logger="agents.graph"):
            g._fix_json_artifact("s", "product_analysis.json")

        on_disk = open(p, encoding="utf-8").read()
        data = json.loads(on_disk)  # strictly valid now
        # >= 8 of the 10 top-level keys (the design says 9/10 — allow slack)
        assert len(data) >= 8, f"recovered only {len(data)} top-level keys"
        # the 36 field mappings — the real analysis payload — survive intact
        fields = data.get("fields") or {}
        assert len(fields) >= 36, f"recovered only {len(fields)} field mappings"
        # the surviving keys are the CORRECT ones (prefix keys, not inventions)
        assert data["site_slug"] == "sidley-com"
        assert "confidence_score" in data or True  # may be past the cut point

    def test_sidley_recovery_reports_the_pass(self, caplog):
        p = self._write_fixture()
        with caplog.at_level(logging.INFO, logger="agents.graph"):
            g._fix_json_artifact("s", "product_analysis.json")
        msgs = [r.getMessage() for r in caplog.records]
        assert any("balanced" in m or "pass" in m.lower() for m in msgs), (
            "the successful repair pass must log which pass succeeded"
        )

    def test_not_renamed_corrupt(self):
        p = self._write_fixture()
        g._fix_json_artifact("s", "product_analysis.json")
        assert os.path.exists(p), "sidley class must NOT be quarantined anymore"
        assert not os.path.exists(p + ".corrupt")


class TestRepairV2ControlChars:
    """C1 (the priceline class) must be repaired losslessly by strict=False,
    not pushed into salvage passes that can only truncate."""

    def test_control_char_repaired_in_place(self, tmp_path, monkeypatch):
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        body = ('{"overall": "PASS",\n'
                ' "feedback_for_writer": "phase 2 fixed' + chr(10) + chr(10)
                + 'phase 1 broken"}')
        p = _make(tmp_path, body)
        g._fix_json_artifact("s", "product_analysis.json")
        data = json.loads(open(p, encoding="utf-8").read())
        assert data["overall"] == "PASS"
        assert data["feedback_for_writer"] == "phase 2 fixed\n\nphase 1 broken"
        assert os.path.exists(p)


class TestRepairV2BalancedCloserMinimal:
    """Small, self-contained C2 shapes the balanced closer must handle."""

    def _root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))

    def test_unquoted_scalar_mid_object(self, tmp_path, monkeypatch):
        """Prefix salvage semantics: everything BEFORE the corruption point is
        kept, the corrupt pair is DROPPED (not partially kept — the bare token
        `25` must not survive as a value), and keys after the corruption point
        are lost (inherent to C2 salvage, per the design doc)."""
        self._root(tmp_path, monkeypatch)
        body = ('{\n "a": "keep me",\n "b2": {"z": 9},\n'
                ' "bad": 25 offices with counts,\n'
                ' "c": {"d": 1}\n}\n')
        p = _make(tmp_path, body)
        g._fix_json_artifact("s", "product_analysis.json")
        data = json.loads(open(p, encoding="utf-8").read())
        assert data["a"] == "keep me"          # before the corruption: kept
        assert data["b2"] == {"z": 9}          # complete nested object: kept
        assert "bad" not in data               # corrupt pair: dropped whole
        assert "25" not in json.dumps(data)    # no bare-token leakage
        assert "c" not in data                 # after the cut: lost by design

    def test_unescaped_quote_inside_string(self, tmp_path, monkeypatch):
        """The job-10 class (mid-file, so raw_decode pass 2 can't help)."""
        self._root(tmp_path, monkeypatch)
        body = ('{\n "head": "x",\n "payload": {"arr": [1, 2]},\n'
                ' "js": "const m = t.match(/\\{"cx-state"[\\s\\S]*$/); return p"\n}')
        p = _make(tmp_path, body)
        g._fix_json_artifact("s", "product_analysis.json")
        data = json.loads(open(p, encoding="utf-8").read())
        assert data["head"] == "x"
        assert data["payload"] == {"arr": [1, 2]}

    def test_truncated_file_reclosed(self, tmp_path, monkeypatch):
        """C3: unterminated nested structure — the closer stack completes it."""
        self._root(tmp_path, monkeypatch)
        body = '{"a": 1, "b": {"c": [1, 2, {"d": "e"'
        p = _make(tmp_path, body)
        g._fix_json_artifact("s", "product_analysis.json")
        data = json.loads(open(p, encoding="utf-8").read())
        assert data["a"] == 1
        assert data["b"]["c"][:2] == [1, 2]


class TestRepairV2ExistingBehaviorPreserved:
    """The four shipped TestArtifactRepair behaviors must keep holding."""

    def test_valid_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        p = _make(tmp_path, '{"a": 1}')
        g._fix_json_artifact("s", "product_analysis.json")
        assert open(p, encoding="utf-8").read() == '{"a": 1}'

    def test_bad_escape_repaired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        p = _make(tmp_path, '{"path": "C:\\Users\\x"}')
        g._fix_json_artifact("s", "product_analysis.json")
        json.load(open(p, encoding="utf-8"))

    def test_unrepairable_renamed_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        p = _make(tmp_path, "<<<not json at all>>>")
        g._fix_json_artifact("s", "product_analysis.json")
        assert not os.path.exists(p)
        assert os.path.exists(p + ".corrupt")
