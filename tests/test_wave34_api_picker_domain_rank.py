"""[wave-34 F1] The traversal API picker prefers a positive count, then the
job's own registrable domain, then schema richness.

Before wave-34 the rank was (has_count, n_keys) with NO domain term — on
widget-heavy storefronts the only verified capture was a third-party
marketing XHR (prod 614 mimco klaviyo / 620 canningvale useinsider / 626
whbm bluecore), which then tripped the contamination guard and killed
healthy walks. The count-first order is deliberate: a vendor-domain catalog
API (aya /job/search, count=26803) must keep beating an on-domain
taxonomy XHR (count=None) — that pair is the function's own design driver.
"""

import os
import re
import sys
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_src = open(
    os.path.join(ROOT, "experimental", "nav_traversal", "traversal.py")
).read()


def _score_fn(goal_url="https://www.tatcha.com/collections/shop-all"):
    """Extract the nested _score closure and bind it to a goal_url."""
    m = re.search(
        r"def _score\(api\):.*?(?=\n    best = max)",
        _src, re.S,
    )
    assert m, "_score closure not found"
    ns = {
        "mock": mock,
        "_registrable": __import__(
            "experimental.nav_traversal.traversal", fromlist=["_registrable"]
        )._registrable,
        "goal_url": goal_url,
        "__builtins__": __builtins__,
    }
    exec(compile("def _score(api):\n" + m.group(0).split("def _score(api):\n", 1)[1], "g:_s", "exec"), ns)
    return ns["_score"]


class TestPickerRank:
    def test_positive_count_beats_same_domain_without_count(self):
        score = _score_fn()
        aya_api = {"url": "https://AyaHealthcareWeb.example.com/job/search", "count": 26803, "sample_keys": ["x"] * 90}
        taxonomy = {"url": "https://www.tatcha.com/wp-json/joblookups", "count": None, "sample_keys": ["a", "b", "c"]}
        assert score(aya_api) > score(taxonomy)

    def test_same_domain_beats_off_domain_widget_at_equal_count(self):
        score = _score_fn("https://www.tatcha.com/collections/shop-all")
        first_party = {"url": "https://www.tatcha.com/api/products", "count": None, "sample_keys": ["title", "price"]}
        klaviyo = {"url": "https://static-forms.klaviyo.com/forms/api/v7/X/full-forms", "count": None, "sample_keys": ["form", "id", "name"]}
        assert score(first_party) > score(klaviyo)

    def test_positive_count_beats_off_domain_positive_count_tiebreak_domain(self):
        score = _score_fn("https://www.tatcha.com/collections/shop-all")
        first_party_api = {"url": "https://www.tatcha.com/api/products", "count": 10, "sample_keys": ["t"]}
        widget = {"url": "https://canningvale.api.useinsider.com/api/info/8127.24", "count": 5, "sample_keys": ["a", "b"]}
        assert score(first_party_api) > score(widget)

    def test_bool_count_is_not_a_positive_count(self):
        score = _score_fn()
        assert score({"url": "https://x.example.com/api", "count": True})[0] == 0

    def test_richness_is_last_tiebreak(self):
        score = _score_fn("https://www.tatcha.com/")
        a = {"url": "https://www.tatcha.com/api/a", "count": None, "sample_keys": ["a", "b"]}
        b = {"url": "https://vendor.example.com/api/b", "count": None, "sample_keys": ["a", "b", "c"]}
        # same count(0), same domain(0/1 vs 0) — first-party wins over richer
        assert score(a) > score(b)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
