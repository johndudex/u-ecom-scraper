"""Wave-52 candidate (7) — the fingerprint+LCR crack in _derive_strategy.

Job-523 (briscoes, third fire) exposed a crack BETWEEN two shipped guards:

- wave-28 (job-524 revolve): inside the ``_rendering == "browser"`` cascade,
  a ``fingerprint_*`` method_that_worked pins ``strategy = "http_requests"``
  because "the browser rungs after it never ran" — only TLS-HTTP is proven.
- wave-48 T2: a client-rendered listing (LCR stamp) flips http_requests to
  http_navigation, BUT the flip is explicitly inhibited on stealth/
  fingerprint methods (same revolve rationale).

Briscoes 523 measured BOTH halves of the revolve premise false: the listing
probe answered via ``fingerprint_chrome_none`` (TLS transport — access
proven) AND ``browser_traverse`` rendered the listing and extracted 20
product URLs from it (browser discovery proven), while the raw TLS-HTTP
shell carried ZERO ``/product/`` anchors (LCR stamped). The composed guards
still shipped ``http_requests`` — a strategy whose phase 1 can fetch the
shell but can never discover in it (the job-1082 starvation, one guard
downstream). The whole fingerprint-crackable class is locked out of the
ladder carrier that way.

The fix: when ``_rendering == "browser"`` (the traverse already proved the
browser lane), the LCR flip fires on fingerprint methods too. http_
navigation's /navigate applies the fingerprint server-side and the
access_recipe already carries ``fingerprint_profile``, so the revolve
concern (unproven vanilla playwright) stays honored. The STEALTH guard
stays absolute: those rungs mean every browser tier measured BLOCKED.
"""

BRISCOES_LISTING = "https://www.briscoes.co.nz/kitchen/appliances/air-fryers/"


def _state(method="fingerprint_chrome_none", rendering="browser", lcr=True):
    """The briscoes-523 measured shape: TLS rung reached the listing
    (needs_browser=False), the traverse rendered it, LCR stamped zero
    anchors in the raw shell."""
    state = {
        "url": BRISCOES_LISTING,
        "probe_result": {
            "connectivity": {"method_that_worked": method},
            "anti_bot": {"detected": False},
            "listing_connectivity": {
                "method_that_worked": method,
                "needs_browser": False,
                "listing_client_rendered": lcr,
                "client_rendered_reason": "0 item anchors in a 1MB shell",
            },
        },
        "navigation_analysis": {
            "rendering_verified": rendering,
            "search": {"form_method": "GET"},
        },
    }
    return state


class TestFingerprintLcrFlip:
    def test_briscoes_523_shape_flips_to_http_navigation(self):
        """The exact 523 measurement: fingerprint method + browser-proven
        rendering + LCR stamp → discovery rides the browser lane."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state())
        assert analysis["strategy"] == "http_navigation"
        assert "client-rendered" in analysis["strategy_justification"].lower()

    def test_revolve_shape_stays_http_requests(self):
        """Regression guard (job-524): fingerprint method WITHOUT the LCR
        stamp (no listing_client_rendered in the probe) stays on the
        measured TLS-HTTP transport, byte-identical to wave-28."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state(lcr=False))
        assert analysis["strategy"] == "http_requests"

    def test_unknown_rendering_stays_http_requests(self):
        """Regression guard: without the traverse's browser proof, the
        wave-28 revolve rule stands even with an LCR stamp — the flip only
        fires when the browser lane is measured."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state(rendering="unknown"))
        assert analysis["strategy"] == "http_requests"

    def test_plain_http_lcr_flip_unchanged(self):
        """The original wave-48 T2 path (direct_http + LCR, rendering
        unknown so no browser cascade runs) still flips."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state(method="direct_http", rendering="unknown"))
        assert analysis["strategy"] == "http_navigation"

    def test_form_post_discovery_stays_http_requests(self):
        """The locumtenens replay contract outranks the flip: http_
        navigation's phase 1 cannot replay a POST form."""
        from webapp.agents.graph import _derive_strategy

        state = _state()
        state["navigation_analysis"]["search"]["form_method"] = "POST"
        analysis = _derive_strategy(state)
        assert analysis["strategy"] == "http_requests"

    def test_stealth_probe_leaves_browser_lane(self):
        """Stealth methods already derive http_navigation inside the
        rendering=browser cascade (job-83 woolworths) — the flip must not
        downgrade them and the guard must stay absolute."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state(method="cloak_residential"))
        assert analysis["strategy"] == "http_navigation"

    def test_recipe_still_carries_fingerprint_profile_after_flip(self):
        """The revolve concern honored: the flipped strategy's access recipe
        keeps the measured fingerprint profile + tiers so /navigate (server-
        side fingerprint) and phase 2 ride what the probe proved."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state(method="fingerprint_safari184_residential"))
        assert analysis["strategy"] == "http_navigation"
        recipe = analysis["access_recipe"]
        assert recipe["fingerprint_profile"], "fingerprint profile lost on flip"
        assert recipe["discovery_proxy_tier"] in ("none", "datacenter", "residential")


def _citychic_524_state():
    """The exact citychic-524 measurement: rendering=browser + 20 traverse-
    extracted item URLs + fingerprint method, but NO LCR stamp (the raw TLS
    shell carried item anchors, so the static detector stayed silent)."""
    state = _state(lcr=False)
    state["navigation_analysis"]["item_links"] = {
        "url_examples": [
            "https://www.citychic.com.au/product/12345/dress.html"
        ] * 20,
    }
    return state


class TestTraverseItemsArm:
    """[job-524 citychic] The traverse rendering the listing and EXTRACTING
    item URLs from it is stronger discovery evidence than the static LCR
    stamp — the browser lane is proven end-to-end even when the raw shell
    happens to carry anchors."""

    def test_citychic_524_shape_flips_without_lcr(self):
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_citychic_524_state())
        assert analysis["strategy"] == "http_navigation"
        assert "traverse" in analysis["strategy_justification"].lower()
        assert "20 item urls" in analysis["strategy_justification"].lower()

    def test_rendered_but_zero_items_stays_http_requests(self):
        """The revolve discriminator: the browser RENDERED but extracted
        nothing (VerifyHuman.jsp death — the wave-28 measured shape). With
        no LCR stamp either, the flip must not fire."""
        from webapp.agents.graph import _derive_strategy

        state = _state(lcr=False)
        state["navigation_analysis"]["item_links"] = {"url_examples": []}
        analysis = _derive_strategy(state)
        assert analysis["strategy"] == "http_requests"

    def test_lcr_alone_still_flips(self):
        """The wave-48 static evidence alone (LCR stamp, no traverse items)
        still flips — (7b) widens, never narrows."""
        from webapp.agents.graph import _derive_strategy

        analysis = _derive_strategy(_state())
        assert analysis["strategy"] == "http_navigation"
