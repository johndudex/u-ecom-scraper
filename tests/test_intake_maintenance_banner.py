"""[maintenance-lock] The intake UI must announce the hold and let admins lift it.

Behavior contract (pairs with tests/test_maintenance_lock.py):

- a maintenance banner exists in the page and is driven by real state —
  shown while the lock is ON (any user), hidden otherwise; the state
  refreshes whenever the library refetches (intake_jobs returns
  ``maintenance``) so the banner appears/disappears without a reload;
- superusers get a Maintenance toggle button that POSTs to the
  ``intake_maintenance`` endpoint and applies the returned state;
- the JS needs the endpoint URL exposed the same way as the other
  intake endpoints (the INTAKE_URLS map).

No JS runtime in the test container — source-pinned with the repo's
function-body extraction pattern (see test_intake_jobs_library_refresh.py).
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INTAKE_HTML = os.path.join(
    ROOT, "webapp", "scraper", "templates", "scraper", "intake.html"
)


def _intake_src() -> str:
    with open(INTAKE_HTML) as fh:
        return fh.read()


def _function_body(src: str, name: str) -> str:
    m = re.search(r"function %s\s*\([^)]*\)\s*\{" % re.escape(name), src)
    assert m, f"function {name} not found in intake.html"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


class TestMaintenanceBanner:
    def test_banner_element_exists(self):
        src = _intake_src()
        assert 'id="maintenance-banner"' in src, (
            "the page needs a #maintenance-banner element to announce the hold"
        )
        assert "maintenance" in src.lower()

    def test_apply_maintenance_sets_visibility_from_state(self):
        body = _function_body(_intake_src(), "_applyMaintenance")
        assert "maintenance-banner" in body
        assert "classList" in body or "hidden" in body, (
            "_applyMaintenance must toggle the banner's visibility"
        )
        assert "reason" in body, (
            "the banner should tell users WHY (the admin's reason) when given"
        )

    def test_apply_maintenance_runs_on_library_refresh_and_boot(self):
        src = _intake_src()
        refresh = _function_body(src, "refreshLibrary")
        assert "_applyMaintenance(" in refresh, (
            "the banner must refresh with the library — intake_jobs returns "
            "the maintenance state, so no separate poll is needed"
        )
        boot = re.search(r"refreshLibrary\(\);[\s\S]{0,400}", src)
        assert boot, "boot calls refreshLibrary"
        assert "_applyMaintenance(" in src, (
            "_applyMaintenance must exist and be called somewhere at boot"
        )

    def test_maintenance_url_exposed_to_js(self):
        src = _intake_src()
        assert re.search(r"maintenanceUrl:\s*\"\{% url 'intake_maintenance' %\}\"", src), (
            "INTAKE_URLS must expose the intake_maintenance endpoint"
        )


class TestMaintenanceToggle:
    def test_toggle_button_exists_for_superuser(self):
        src = _intake_src()
        assert 'id="maintenance-btn"' in src, (
            "superusers need a Maintenance toggle in the topbar"
        )
        # superuser-only: the button lives inside an is_superuser conditional
        m = re.search(r"\{% if is_superuser %\}[\s\S]*?maintenance-btn[\s\S]*?\{% endif %\}", src)
        assert m, "the Maintenance button must be wrapped in {% if is_superuser %}"

    def test_toggle_button_posts_and_applies_state(self):
        src = _intake_src()
        m = re.search(
            r"X\('#maintenance-btn'\)\.addEventListener\('click',[\s\S]*?\}\);", src
        )
        assert m, "#maintenance-btn needs a click handler"
        body = m.group(0)
        assert "maintenanceUrl" in body, "the handler must POST to the endpoint"
        assert "fetch(" in body or "XH" in body or "POST" in body, (
            "the handler must issue a POST"
        )
        assert "_applyMaintenance(" in body, (
            "the handler must apply the returned state"
        )


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
