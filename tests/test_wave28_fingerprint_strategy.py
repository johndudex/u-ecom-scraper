"""Wave-28 — fingerprint-aware strategy derivation + writer transport brief.

Job-524/523 (revolve, list_page) failed 2x1800s in code_writer: the probe
ladder stopped at ``fingerprint_chrome_none`` (plain ``direct_http`` blocked,
curl_cffi TLS impersonation answered with 1.16MB of real listing HTML), but
``_derive_strategy``'s browser-rendering cascade fell through to bare
``playwright`` — the browser the navigator had just measured dying on Akamai
``VerifyHuman.jsp``. The writer then spent both 1800s attempts re-deriving
transport from src/ and never wrote a draft.

Why ``fingerprint`` must NOT join ``STEALTH_METHOD_PREFIXES``: the stealth
prefixes mean "every browser rung ran and was BLOCKED" (job-83 woolworths —
ladder order is direct_http → fingerprint rungs → playwright → cloak per
tier), so they map to ``http_navigation`` (cloak browser). ``fingerprint_*``
means only "plain-HTTP TLS was rejected; impersonated HTTP works" — the
browser rungs AFTER it never ran. The proven transport is HTTP, so the
strategy is ``http_requests`` riding ``src/http_fetch``'s ladder (whose last
tier IS the curl_cffi fingerprint tier — the job-66 birkenstock shape).
"""

import pytest

JOB524_LISTING = "https://www.revolve.com/dresses/drp.jsp"


class TestDeriveStrategyFingerprintFlavor:
    """Mirror of the job-83 class: strategy follows the flavor the probe PROVED."""

    def _state(self, method="fingerprint_chrome_none", form="GET", rendering="browser"):
        return {
            "url": JOB524_LISTING,
            "probe_result": {
                "connectivity": {"method_that_worked": method},
                # The 524 shape: the fingerprint rung answered clean, so the
                # probe never raised the anti-bot flag.
                "anti_bot": {"detected": False},
            },
            "navigation_analysis": {
                "rendering_verified": rendering,
                "search": {"form_method": form},
            },
        }

    def test_fingerprint_probe_never_picks_playwright(self):
        """The job-524 shape: browser rendering signal + fingerprint-proven
        HTTP. Bare playwright must not be derived — the only measured-working
        transport is TLS-impersonated HTTP."""
        from webapp.agents.graph import _derive_strategy

        for method in (
            "fingerprint_chrome_none",
            "fingerprint_safari184_none",
            "fingerprint_chrome_residential",
        ):
            analysis = _derive_strategy(self._state(method=method))
            assert analysis["strategy"] == "http_requests", (
                method,
                analysis["strategy"],
            )

    def test_fingerprint_unknown_rendering_picks_http_requests(self):
        """No navigation ran (rendering unknown): a fingerprint-proven site
        still lands on the HTTP strategy, not the browser-backed default."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(self._state(rendering="unknown"))
        assert analysis["strategy"] == "http_requests"

    def test_fingerprint_residential_tier_survives(self):
        """T3.4 tier wipe must not strip a fingerprint-proven tier: the ladder
        ran every rung below ``fingerprint_*_residential`` and they all failed,
        so the residential identity is measured evidence, not a guess."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(self._state(method="fingerprint_safari184_residential"))
        assert analysis["strategy"] == "http_requests"
        assert analysis["proxy_tier"] == "residential"

    def test_fingerprint_recipe_names_profile_not_cloak(self):
        """The access recipe must carry the TLS-impersonation identity and must
        NOT claim the cloak browser (the revolve browser is VerifyHuman-blocked;
        run_execution stages cloak off this field)."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(self._state(method="fingerprint_chrome_none"))
        recipe = analysis["access_recipe"]
        assert recipe["fingerprint_profile"] == "chrome"
        assert recipe["stealth"] == "none"
        assert recipe["needs_browser"] is False

    def test_plain_http_probe_still_wins(self):
        """Regression lock: a genuinely plain-HTTP site keeps the pre-existing
        derivation (direct_http → http_requests)."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(
            self._state(method="direct_http", rendering="unknown")
        )
        assert analysis["strategy"] == "http_requests"
        assert analysis["access_recipe"]["fingerprint_profile"] in (None, "")

    def test_fingerprint_suppresses_reassessment_override(self):
        """Job-114 semantics extended: a product-analyzer OPINION ('recommend
        playwright') cannot overturn the probe's fingerprint measurement."""
        from webapp.agents.graph import _derive_strategy

        state = self._state()
        state["product_analysis"] = {
            "mechanism_reassessment": {
                "recommended": "playwright",
                "reason": "listing looked JS-heavy",
            }
        }
        analysis = _derive_strategy(state)
        assert analysis["strategy"] == "http_requests"
        justification = analysis["strategy_justification"]
        assert "ignored" in justification
        # T1.10: the suppression text must not carry the token that
        # _suppress_mechanism_reassessment greps for.
        assert "mechanism_reassessment" not in justification

    def test_fingerprint_listing_browser_evidence_still_upgrades(self):
        """Precedence lock (wave-17 S4): when the LISTING probe measured that
        only a browser rung reaches the listing, discovery rides the browser —
        the fingerprint PDP recipe does not outrank listing evidence."""
        from webapp.agents.graph import _derive_strategy

        state = self._state()
        state["probe_result"]["listing_connectivity"] = {
            "method_that_worked": "cloak_residential",
            "needs_browser": True,
        }
        analysis = _derive_strategy(state)
        assert analysis["strategy"] == "http_navigation"


class TestAntiBotEnforcementFingerprint:
    """_enforce_anti_bot_strategy rewrites http_requests → http_navigation for
    bot-protected sites. On a fingerprint-proven site that rewrite replaces the
    MEASURED-working transport with a browser the site may refuse outright."""

    def test_enforce_leaves_fingerprint_http_requests(self, tmp_path, monkeypatch):
        import webapp.agents.graph as graph

        monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
        analysis = {
            "strategy": "http_requests",
            "scraping_mechanism": "http_requests",
            "scraping_method": "http_requests",
            "recommended_strategy": "http_requests",
            "proxy_tier": "none",
            "connectivity": {"method_that_worked": "fingerprint_chrome_none"},
            # Challenge seen on a blocked rung — the flag is true, but the
            # fingerprint measurement is still the working transport.
            "anti_bot": {"detected": True},
        }
        out = graph._enforce_anti_bot_strategy(analysis, "revolve", "scraper_analysis.json")
        assert out["strategy"] == "http_requests"
        assert out["scraping_mechanism"] == "http_requests"


class TestWriterTransportBrief:
    """T2: the writer seed names the measured transport + the http_fetch recipe
    so code_writer never re-derives transport by reading src/ (the 524 writer
    burned 8+ file reads and a scratch probe run on exactly this)."""

    def _state(self):
        return {
            "site_slug": "revolve",
            "url": JOB524_LISTING,
            "input_mode": "list_page",
            "probe_result": {
                "connectivity": {"method_that_worked": "fingerprint_chrome_none"},
                "anti_bot": {"detected": False},
            },
            "site_analysis": {},
            "scraper_analysis": {
                "strategy": "http_requests",
                "proxy_tier": "none",
                "method_that_worked": "fingerprint_chrome_none",
                "access_recipe": {
                    "stealth": "none",
                    "proxy_tier": "none",
                    "discovery_proxy_tier": "none",
                    "needs_browser": False,
                    "fingerprint_profile": "chrome",
                    "evidence": "pdp_method=fingerprint_chrome_none; listing_method=not_probed",
                },
            },
            "navigation_analysis": {"rendering_verified": "browser"},
        }

    def test_brief_present_for_fingerprint_site(self):
        from webapp.agents.subagents import build_code_writer_message

        msgs = build_code_writer_message(self._state())
        text = "\n".join(str(getattr(m, "content", m)) for m in msgs)
        assert "fingerprint_chrome_none" in text
        assert "TLS-impersonated" in text or "curl_cffi" in text
        assert "src/http_fetch" in text

    def test_brief_absent_for_plain_http_site(self):
        """No fingerprint evidence → the seed is unchanged (no transport brief)."""
        from webapp.agents.subagents import build_code_writer_message

        state = self._state()
        state["probe_result"]["connectivity"]["method_that_worked"] = "direct_http"
        state["scraper_analysis"]["method_that_worked"] = "direct_http"
        state["scraper_analysis"]["access_recipe"]["fingerprint_profile"] = None
        msgs = build_code_writer_message(state)
        text = "\n".join(str(getattr(m, "content", m)) for m in msgs)
        assert "TLS-impersonated" not in text
        assert "curl_cffi" not in text
