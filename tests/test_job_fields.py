"""Unit tests for the generic job field-mapping resolver (src/job_fields.py).

No Django dependency — the resolver is pure.  The key test is
``TestGenericityAcrossPlatforms``: the SAME resolver + alias table picks
DIFFERENT source paths for two real platform fixtures (AMN API JSON vs a
schema.org JobPosting JSON-LD block) with no branching on site name.
"""

import json
import os
from datetime import datetime, timedelta

import pytest

from src.job_fields import (
    JOB_ALIASES,
    apply_field_map,
    infer_field_map,
    map_jobs,
    parse_posted_date,
    resolve_path,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


# ─── resolve_path ──────────────────────────────────────────────────────────────

class TestResolvePath:
    def test_simple_dotted(self):
        assert resolve_path({"a": {"b": 1}}, "a.b") == 1

    def test_missing_returns_none(self):
        assert resolve_path({"a": {"b": 1}}, "a.c") is None

    def test_list_picks_first_element_with_key(self):
        assert resolve_path({"a": [{"b": 1}, {"b": 2}]}, "a.b") == 1
        assert resolve_path({"a": [{"b": 2}]}, "a.b") == 2

    def test_string_org_is_leaf_value(self):
        # schema.org allows hiringOrganization to be a plain string
        assert resolve_path({"hiringOrganization": "Acme"}, "hiringOrganization.name") == "Acme"

    def test_dict_leaf_raw_returned(self):
        # resolve_path returns the raw leaf; callers coerce
        raw = resolve_path({"org": {"name": "Acme"}}, "org")
        assert isinstance(raw, dict)
        assert raw.get("name") == "Acme"


# ─── infer_field_map ──────────────────────────────────────────────────────────

class TestInferFieldMap:
    def test_picks_highest_coverage(self):
        # organization.name is empty for most; divisionCompany.companyName is full
        items = [
            {"organization": {"name": ""}, "divisionCompany": {"companyName": "Med Travelers"}}
            for _ in range(5)
        ]
        fm = infer_field_map(items)
        assert fm["company"] == "divisionCompany.companyName"

    def test_jsonld_candidate_wins_tie(self):
        # Both hiringOrganization.name and 'company' fully populated; JSON-LD
        # item should prefer the schema.org path.
        items = [
            {"@type": "JobPosting",
             "hiringOrganization": {"name": "Acme"}, "company": "Acme"}
            for _ in range(4)
        ]
        fm = infer_field_map(items)
        assert fm["company"] == "hiringOrganization.name"

    def test_no_candidate_returns_none(self):
        items = [{"title": "RN"} for _ in range(4)]  # no company anywhere
        fm = infer_field_map(items)
        assert fm["company"] is None

    def test_small_sample_falls_back_to_alias_first(self):
        # Below MIN_SAMPLE_FOR_COVERAGE -> alias[0] (the schema.org path)
        fm = infer_field_map([{"jobTitle": "RN", "title": "Nurse"}])
        assert fm["title"] == "title"

    def test_composite_location_when_split(self):
        items = [{"city": {"name": "Oxford"}, "state": {"name": "Alabama", "abbrev": "AL"}}
                 for _ in range(4)]
        fm = infer_field_map(items)
        assert fm["location"] is not None
        assert fm["location"].startswith("$compose:city.name")

    def test_geojson_location_is_treated_as_absent(self):
        # AMN-style: 'location' is a GeoJSON Point, not a place name.
        items = [{"location": {"type": "Point", "coordinates": [-85.8, 33.6]},
                  "city": {"name": "Oxford"}, "state": {"abbrev": "AL"}}
                 for _ in range(4)]
        fm = infer_field_map(items)
        assert fm["location"] == "$compose:city.name|state.abbrev"

    def test_graph_flattened(self):
        items = [{"@context": "http://schema.org",
                  "@graph": [{"@type": "JobPosting", "title": "RN",
                              "hiringOrganization": {"name": "Acme"}}]}
                 for _ in range(4)]
        fm = infer_field_map(items)
        assert fm["title"] == "title"
        assert fm["company"] == "hiringOrganization.name"


# ─── apply_field_map ──────────────────────────────────────────────────────────

class TestApplyFieldMap:
    def test_location_composite(self):
        fm = {"location": "$compose:city.name|state.abbrev"}
        out = apply_field_map({"city": {"name": "Oxford"}, "state": {"abbrev": "AL"}}, fm)
        assert out["location"] == "Oxford, AL"

    def test_location_single_string(self):
        fm = {"location": "formattedLocation"}
        out = apply_field_map({"formattedLocation": "Birmingham, AL"}, fm)
        assert out["location"] == "Birmingham, AL"

    def test_posted_date_iso_normalization(self):
        fm = {"posted_date": "datePosted"}
        out = apply_field_map({"datePosted": "2026-06-25T12:27:50+00:00"}, fm)
        assert out["posted_date"] == "2026-06-25"

    def test_posted_date_us_format(self):
        fm = {"posted_date": "datePosted"}
        out = apply_field_map({"datePosted": "06/25/2026"}, fm)
        assert out["posted_date"] == "2026-06-25"

    def test_posted_date_relative(self):
        fm = {"posted_date": "datePosted"}
        out = apply_field_map({"datePosted": "2 days ago"}, fm)
        assert out["posted_date"] == (datetime.now() - timedelta(days=2)).date().isoformat()

    def test_salary_range_builder(self):
        fm = {"salary": "$salrange:payRate.minPayRate|payRate.maxPayRate"}
        out = apply_field_map({"payRate": {"minPayRate": 1500, "maxPayRate": 2000}}, fm)
        assert out["salary"] == "$1,500 - $2,000"

    def test_employmenttype_list_joined(self):
        fm = {"job_type": "employmentType"}
        out = apply_field_map({"employmentType": ["FULL_TIME", "CONTRACTOR"]}, fm)
        assert out["job_type"] == "FULL_TIME, CONTRACTOR"

    def test_unmapped_field_is_empty_string(self):
        fm = {"company": None}
        out = apply_field_map({"title": "RN"}, fm)
        assert out["company"] == ""

    def test_direct_fields_preserved(self):
        fm = {"title": "title"}
        out = apply_field_map({"title": "RN", "url": "http://x", "status_code": 200}, fm)
        assert out["url"] == "http://x"
        assert out["status_code"] == 200

    def test_location_raw_and_posted_date_parsed_derived(self):
        fm = {"location": "$compose:city.name|state.abbrev", "posted_date": "datePosted"}
        out = apply_field_map({"city": {"name": "Oxford"}, "state": {"abbrev": "AL"},
                               "datePosted": "2026-06-25"}, fm)
        assert out["location_raw"] == "Oxford, AL"
        assert out["posted_date_parsed"] == "2026-06-25"


# ─── parse_posted_date ────────────────────────────────────────────────────────

class TestParsePostedDate:
    def test_iso_with_tz(self):
        assert parse_posted_date("2026-06-25T12:27:50+00:00").date() == datetime(2026, 6, 25).date()

    def test_none(self):
        assert parse_posted_date("") is None
        assert parse_posted_date(None) is None

    def test_relative(self):
        assert parse_posted_date("today").date() == datetime.now().date()


# ─── THE genericity proof ─────────────────────────────────────────────────────

class TestGenericityAcrossPlatforms:
    """One resolver, one alias table, two real platform fixtures → two different
    winning source paths for the SAME output field, with zero site branching."""

    @pytest.mark.parametrize("fixture, expected_company_path", [
        ("amn_job_item.json", "divisionCompany.companyName"),
        ("jsonld_job_item.json", "hiringOrganization.name"),
    ])
    def test_company_resolves_differently_per_platform(self, fixture, expected_company_path):
        item = _load(fixture)
        # Build a sample batch by repeating the single fixture.
        sample = [item] * 5
        fm = infer_field_map(sample)
        assert fm["company"] == expected_company_path, (
            f"{fixture}: expected company via {expected_company_path}, got {fm['company']}"
        )

    def test_amn_end_to_end_mapping(self):
        item = _load("amn_job_item.json")
        out = map_jobs([item], [item])[0]
        # Company is a real staffing division, not the empty organization.name.
        assert out["company"]
        assert out["company"] != "Point"  # not a GeoJSON artifact
        # Location is "City, ST", not a geo type.
        assert out["location"].endswith(", AL") or out["location"].endswith("AL")
        # posted_date normalized to ISO.
        assert out["posted_date"] and len(out["posted_date"]) == 10

    def test_jsonld_end_to_end_mapping(self):
        item = _load("jsonld_job_item.json")
        out = map_jobs([item], [item])[0]
        assert out["company"]  # hiringOrganization.name
        assert out["title"]

    def test_alias_table_has_all_core_job_fields(self):
        for f in ("title", "company", "location", "description", "posted_date"):
            assert f in JOB_ALIASES and JOB_ALIASES[f], f
