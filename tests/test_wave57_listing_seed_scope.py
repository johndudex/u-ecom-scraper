"""[wave-57 class 1] Listing-shaped url_list seeds EXTEND scope — job #566.

jjshouse #566: 19 submitted seeds were LISTING urls (``/all/...``,
``/collection/...``, several with ``?sort=``). The scraper discovered the
catalog behind them and extracted 913 item-shaped priced rows — and the C2
scope gate failed the run ("908/913 output rows are not submitted URLs").
Under any sane reading that was a full-catalog success fired in the wrong
mode.

Fix: when the seed set reads as listings (GUARDIAN-corrected rule), the
submitted listings EXTEND the discovery scope — output rows are judged by
item-shape + same registrable host, not by membership in the submitted set.
Genuine item-shaped seed sets keep strict membership: the gate is NOT
weakened for real url_list.

Set rule (guardian checkpoint 1): loose iff NO seed is strongly item-shaped
AND >=1 seed is positively listing-shaped. Weak/ambiguous plain slugs are
inert — they neither block the loosening nor cause it.

Namespace set is measured zero-collateral on the promoted corpus
(``all, collection, catalog, catalogue`` + terminal nav tokens + the C3
nav-position fail arm). ``shop`` is deliberately EXCLUDED: 928 corpus PDPs
(``/shop/designers/<x>/<slug>``) classify listing-shaped under it. There is
NO query arm: ``/slug?sort=new`` must not lift an ambiguous plain slug.
"""

import json

import pytest

from src.discovery_quality import (
    row_url_is_item_shaped,
    seed_url_is_listing_shaped,
    seeds_are_listing_shaped,
)
from webapp.agents.nodes.run_execution import _url_list_scope_gate


# The #566 payload, verbatim shapes. NOTE: one seed
# (/shop/accessories-shoes) is ambiguous — weak, not listing-shaped — and
# that is exactly the shape the set-level rule must tolerate (the fix has
# to fix its own regression target).
JJHOUSE_SEEDS = [
    "https://www.jjshouse.com/all/bridesmaid-dresses",
    "https://www.jjshouse.com/all/mother-of-the-bride-dresses",
    "https://www.jjshouse.com/collection/girls-dresses",
    "https://www.jjshouse.com/shop/accessories-shoes",
    "https://www.jjshouse.com/all/swatch",
    "https://www.jjshouse.com/all/bridesmaid-dresses?sort=new-arrivals",
]


def _write_output(tmp_path, rows, meta=None):
    out = tmp_path / "output_run.json"
    payload = {
        "site": "jjshouse-com",
        "products": rows,
        "metadata": meta or {},
    }
    out.write_text(json.dumps(payload), encoding="utf-8")
    return str(out)


def _row(i):
    return {
        "url": (
            "https://www.jjshouse.com/product/jjshouse-dress-g29%05d" % i
        ),
        "title": "JJ's House Dress %d" % i,
        "price": 99.0,
    }


class TestSeedShapeClassifier:
    def test_jjhouse_listing_seeds_are_listing_shaped(self):
        for u in (
            "https://www.jjshouse.com/all/bridesmaid-dresses",
            "https://www.jjshouse.com/collection/girls-dresses",
            "https://www.jjshouse.com/all/swatch",
            "https://www.jjshouse.com/all/bridesmaid-dresses?sort=new-arrivals",
        ):
            assert seed_url_is_listing_shaped(u), u

    def test_jjhouse_seed_set_is_listing_shaped(self):
        """The #566 set (with its one ambiguous /shop/ member) must read as
        listings — the weak seed neither blocks nor causes."""
        assert seeds_are_listing_shaped(JJHOUSE_SEEDS)

    def test_shop_is_excluded_guardian_corpus(self):
        """928 corpus PDPs (/shop/designers/<x>/<slug>) must never read as
        listings: a PDP-paste job keeps row-membership demanded."""
        assert not seed_url_is_listing_shaped(
            "https://www.renttherunway.com/shop/designers/brandi/dress-slug"
        )
        assert not seed_url_is_listing_shaped(
            "https://www.jjshouse.com/shop/accessories-shoes"
        )

    def test_strong_item_seeds_are_never_listing_shaped(self):
        for u in (
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "https://shop.com/p/12345",
            "https://shop.com/alyshia-black-2",
            "https://shop.com/item/foo-112697.aspx",
        ):
            assert not seed_url_is_listing_shaped(u), u

    def test_query_arm_must_not_lift_an_ambiguous_slug(self):
        """Guardian pin (3): a sort/page query on an ambiguous plain slug
        stays ambiguous — NO standalone query arm exists."""
        assert not seed_url_is_listing_shaped(
            "https://wonderbly.com/personalized-books/the-name-of-the-book"
            "?sort=new"
        )

    def test_ambiguous_plain_slug_defaults_strict(self):
        assert not seed_url_is_listing_shaped(
            "https://wonderbly.com/personalized-books/the-name-of-the-book"
        )

    def test_namespace_and_terminal_signals_are_listing_shaped(self):
        assert seed_url_is_listing_shaped("https://shop.com/new-in")
        assert seed_url_is_listing_shaped(
            "https://shop.com/collections/sale?sort=new"
        )
        assert seed_url_is_listing_shaped("https://shop.com/catalog/dresses")
        assert seed_url_is_listing_shaped(
            "https://shop.com/catalogue/shoes?sort=price"
        )

    def test_empty_set_is_not_listing_shaped(self):
        assert not seeds_are_listing_shaped([])
        assert not seeds_are_listing_shaped(["", "  "])

    def test_strong_item_member_blocks_the_set(self):
        assert not seeds_are_listing_shaped(
            JJHOUSE_SEEDS + ["https://www.jjshouse.com/product/x-g1"]
        )

    def test_all_ambiguous_set_stays_strict(self):
        """No positive listing signal anywhere — membership stays demanded
        (the renttherunway PDP-paste shape: every seed a plain-slug PDP)."""
        assert not seeds_are_listing_shaped([
            "https://www.renttherunway.com/shop/designers/brandi/dress-slug",
            "https://www.renttherunway.com/shop/designers/marchesa/gown-x",
        ])

    def test_item_shape_floor_still_judges_rows(self):
        # The row floor is untouched: PDP rows pass, shells fail.
        assert row_url_is_item_shaped(_row(1)["url"])
        assert not row_url_is_item_shaped("https://www.jjshouse.com/#main")


class TestScopeGateListingSeeds:
    def test_566_shape_completes(self, tmp_path):
        """19 listing seeds, 913 discovered item rows — the run must pass
        the scope gate (rows judged by item-shape + host, not membership)."""
        out = _write_output(tmp_path, [_row(i) for i in range(913)])
        assert _url_list_scope_gate(out, JJHOUSE_SEEDS, "url_list") == ""

    def test_seeds_themselves_among_rows_still_pass(self, tmp_path):
        """Discovered rows that INCLUDE the submitted listing urls (plus
        deeper items) are the normal listing-seed run shape."""
        rows = [_row(i) for i in range(5)]
        rows.append({
            "url": JJHOUSE_SEEDS[0], "title": "listing", "price": 1,
        })
        out = _write_output(tmp_path, rows)
        assert _url_list_scope_gate(out, JJHOUSE_SEEDS, "url_list") == ""

    def test_offhost_rows_still_violate(self, tmp_path):
        rows = [_row(i) for i in range(5)]
        rows.append(
            {"url": "https://other-shop.com/product/x-g1", "title": "x",
             "price": 1}
        )
        out = _write_output(tmp_path, rows)
        verdict = _url_list_scope_gate(out, JJHOUSE_SEEDS, "url_list")
        assert "scope violation" in verdict

    def test_shell_rows_still_violate(self, tmp_path):
        rows = [_row(i) for i in range(5)]
        rows.append(
            {"url": "https://www.jjshouse.com/#main", "title": "shell",
             "price": 1}
        )
        out = _write_output(tmp_path, rows)
        verdict = _url_list_scope_gate(out, JJHOUSE_SEEDS, "url_list")
        assert "scope violation" in verdict

    def test_zero_rows_still_fails(self, tmp_path):
        out = _write_output(tmp_path, [])
        verdict = _url_list_scope_gate(out, JJHOUSE_SEEDS, "url_list")
        assert "zero-yield" in verdict

    def test_item_seeds_keep_strict_membership(self, tmp_path):
        """The inverse shape: item-shaped seeds + unsubmitted rows must stay
        a scope violation (the job-1227 class this gate exists for)."""
        seeds = [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "https://www.jjshouse.com/product/jjshouse-odetta-g293497",
        ]
        rows = [_row(i) for i in range(9)]
        out = _write_output(tmp_path, rows)
        verdict = _url_list_scope_gate(out, seeds, "url_list")
        assert "scope violation" in verdict

    def test_item_seeds_membership_still_passes(self, tmp_path):
        seeds = [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
        ]
        rows = [{
            "url": "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
            "title": "Cesia", "price": 99.0,
        }]
        out = _write_output(tmp_path, rows)
        assert _url_list_scope_gate(out, seeds, "url_list") == ""

    def test_shop_pdp_paste_keeps_strict_membership(self, tmp_path):
        """Guardian corpus guard: renttherunway-class PDP pastes (plain
        slugs under /shop/designers/) keep the membership gate even when
        the run discovered same-host item rows."""
        seeds = [
            "https://www.renttherunway.com/shop/designers/brandi/dress-slug",
            "https://www.renttherunway.com/shop/designers/marchesa/gown-x",
        ]
        rows = [{
            "url": (
                "https://www.renttherunway.com/shop/designers/other/"
                "different-dress-9"
            ),
            "title": "not submitted", "price": 1.0,
        }]
        out = _write_output(tmp_path, rows)
        assert "scope violation" in _url_list_scope_gate(
            out, seeds, "url_list"
        )

    def test_mixed_seeds_stay_strict(self, tmp_path):
        seeds = JJHOUSE_SEEDS + [
            "https://www.jjshouse.com/product/jjshouse-cesia-g298054",
        ]
        rows = [_row(i) for i in range(5)]
        out = _write_output(tmp_path, rows)
        assert "scope violation" in _url_list_scope_gate(
            out, seeds, "url_list"
        )

    def test_other_modes_unaffected(self, tmp_path):
        out = _write_output(tmp_path, [_row(i) for i in range(3)])
        assert _url_list_scope_gate(out, JJHOUSE_SEEDS, "list_page") == ""
        assert _url_list_scope_gate(out, JJHOUSE_SEEDS, "") == ""

    def test_missing_file_stays_noop(self, tmp_path):
        assert _url_list_scope_gate(
            str(tmp_path / "nope.json"), JJHOUSE_SEEDS, "url_list"
        ) == ""


@pytest.mark.parametrize("url,expected", [
    ("https://www.jjshouse.com/all/bridesmaid-dresses", True),
    ("https://www.jjshouse.com/collection/prom-hoco-dresses", True),
    ("https://www.jjshouse.com/catalog/evening-dresses", True),
    ("https://www.jjshouse.com/all/final-sale-items", True),
    ("https://www.jjshouse.com/shop/accessories-shoes", False),
    ("https://www.jjshouse.com/product/jjshouse-dress-g298054", False),
    ("https://wonderbly.com/personalized-books/kids/age/0-3", False),
    (
        "https://wonderbly.com/personalized-books/the-name-of-the-book"
        "?sort=new",
        False,
    ),
])
def test_classifier_table(url, expected):
    assert seed_url_is_listing_shaped(url) is expected
