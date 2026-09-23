"""[wave-40 T13] checkpoint reuse helpers. Prod 769: tester discovered 20
products; execution re-ran discovery hours later, got 0 (empty_first_page),
exit 3 DISCOVERY_ZERO, job FAILED with the checkpoint sitting unread in the
workspace."""

import json
import time

from src.listing_discovery import (
    CHECKPOINT_FILENAME, CHECKPOINT_REUSED_STOP_REASON,
    checkpoint_coverage_patch, load_discovery_checkpoint,
    zero_discovery_rescuable)


def _write_checkpoint(tmp_path, urls, age_s=3600):
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text(json.dumps({"urls": urls, "count": len(urls),
                             "ts": time.time() - age_s}))
    return p


def test_rescuable_reasons_and_infra_exclusion():
    assert zero_discovery_rescuable("empty_first_page")
    assert zero_discovery_rescuable("empty_render")
    assert not zero_discovery_rescuable("navigate_unavailable")


def test_happy_path(tmp_path):
    p = _write_checkpoint(tmp_path, [f"https://x.example/p/{i}" for i in range(12)])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "ok" and out["raw_count"] == 12 and len(out["urls"]) == 12


def test_absent_and_unparseable(tmp_path):
    out = load_discovery_checkpoint(str(tmp_path / "nope.json"), "navigation",
                                    host="x.example")
    assert out["reason"] == "absent" and out["urls"] == []
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text("{bad")
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "unparseable"


def test_disabled_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_REUSE", "0")
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "disabled"


def test_stale_checkpoint_rejected_at_one_day_default(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"], age_s=86400 * 2)
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "stale"


def test_below_floor_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_CHECKPOINT_MIN_URLS", "5")
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "below_floor"


def test_cross_host_urls_filtered(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1",
                                     "https://evil.example/p/2"])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert all(u.startswith("https://x.example") for u in out["urls"])
    assert out["reason"] in ("ok", "filtered_empty")


def test_coverage_patch_preserves_fresh_verdict():
    fresh = {"discovered_urls": 0, "stop_reason": "empty_first_page"}
    patched = checkpoint_coverage_patch(fresh, {"urls": ["https://x/p/1"],
                                                "raw_count": 20})
    assert patched["stop_reason"] == CHECKPOINT_REUSED_STOP_REASON
    assert patched["fresh_stop_reason"] == "empty_first_page"
    assert patched["checkpoint_urls"] == 1


# ── T13 coverage additions (the brief's tests above are verbatim) ──────────
# The brief's file does not pin the ``empty`` / ``filtered_empty`` reasons,
# the max-URL cap, the non-phase-1 mode gate, or the fail-closed blank host —
# the task contract requires every branch reachable AND tested.

from src.listing_discovery import (  # noqa: E402
    CHECKPOINT_MAX_AGE_S_ENV, CHECKPOINT_MAX_URLS_ENV, CHECKPOINT_MIN_URLS_ENV)


def test_empty_payload_rejected(tmp_path):
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text(json.dumps({"urls": [], "count": 0, "ts": time.time()}))
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "empty" and out["urls"] == [] and out["raw_count"] == 0


def test_all_cross_host_is_filtered_empty(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://evil.example/p/1",
                                     "https://other.example/p/2"])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "filtered_empty" and out["urls"] == []
    assert out["dropped"] == 2 and out["raw_count"] == 2


def test_blank_host_fails_closed(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1",
                                     "https://x.example/p/2"])
    out = load_discovery_checkpoint(str(p), "navigation", host="")
    assert out["reason"] == "filtered_empty" and out["urls"] == []


def test_www_host_fold_is_same_host(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://www.x.example/p/1",
                                     "https://x.example/p/2"])
    out = load_discovery_checkpoint(str(p), "navigation", host="X.Example")
    assert out["reason"] == "ok" and len(out["urls"]) == 2 and out["dropped"] == 0


def test_sibling_subdomain_is_cross_host(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://cdn.x.example/p/1",
                                     "https://cdn.x.example/p/2"])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "filtered_empty"


def test_url_list_mode_never_reuses(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    assert load_discovery_checkpoint(str(p), "url_list",
                                     host="x.example")["reason"] == "disabled"


def test_missing_ts_is_stale_not_fresh(tmp_path):
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text(json.dumps({"urls": ["https://x.example/p/1"], "count": 1}))
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "stale"


def test_max_age_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_MAX_AGE_S_ENV, "10")
    fresh = _write_checkpoint(tmp_path, ["https://x.example/p/1",
                                         "https://x.example/p/2"], age_s=5)
    assert load_discovery_checkpoint(str(fresh), "navigation",
                                     host="x.example")["reason"] == "ok"
    old = _write_checkpoint(tmp_path, ["https://x.example/p/1",
                                       "https://x.example/p/2"], age_s=20)
    assert load_discovery_checkpoint(str(old), "navigation",
                                     host="x.example")["reason"] == "stale"


def test_min_urls_zero_disables_floor(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_MIN_URLS_ENV, "0")
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "ok" and len(out["urls"]) == 1


def test_default_floor_rejects_single_url(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1"])
    assert load_discovery_checkpoint(str(p), "navigation",
                                     host="x.example")["reason"] == "below_floor"


def test_max_url_cap_truncates_and_flags(tmp_path, monkeypatch):
    monkeypatch.setenv(CHECKPOINT_MAX_URLS_ENV, "3")
    p = _write_checkpoint(tmp_path, [f"https://x.example/p/{i}" for i in range(6)])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "ok" and len(out["urls"]) == 3
    assert out["capped"] is True and out["raw_count"] == 6 and out["dropped"] == 0


def test_cap_zero_or_unset_means_uncapped(tmp_path):
    p = _write_checkpoint(tmp_path, [f"https://x.example/p/{i}" for i in range(4)])
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "ok" and out["capped"] is False and len(out["urls"]) == 4


def test_age_and_dropped_are_reported(tmp_path):
    p = _write_checkpoint(tmp_path, ["https://x.example/p/1",
                                     "https://evil.example/p/2"], age_s=600)
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert 590 <= out["age_s"] <= 610
    assert out["dropped"] == 1 and out["raw_count"] == 2


def test_non_dict_payload_is_unparseable(tmp_path):
    p = tmp_path / CHECKPOINT_FILENAME
    p.write_text(json.dumps(["https://x.example/p/1"]))
    out = load_discovery_checkpoint(str(p), "navigation", host="x.example")
    assert out["reason"] == "unparseable"


def test_rescuable_is_case_insensitive_and_total():
    assert zero_discovery_rescuable("NAVIGATE_UNAVAILABLE") is False
    assert zero_discovery_rescuable("") is True
    assert zero_discovery_rescuable(None) is True
    assert zero_discovery_rescuable("max_pages_hit") is True


def test_coverage_patch_does_not_mutate_fresh():
    fresh = {"discovered_urls": 0, "stop_reason": "empty_first_page"}
    checkpoint_coverage_patch(fresh, {"urls": ["https://x/p/1"]})
    assert fresh == {"discovered_urls": 0, "stop_reason": "empty_first_page"}


def test_coverage_patch_falls_back_to_checkpoint_urls_key():
    patched = checkpoint_coverage_patch({}, {"checkpoint_urls": 7})
    assert patched["checkpoint_urls"] == 7
    assert patched["fresh_stop_reason"] is None
    assert patched["stop_reason"] == CHECKPOINT_REUSED_STOP_REASON
