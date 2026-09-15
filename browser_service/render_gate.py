"""[wave-32 A5] Render-gate semantics + override note — dependency-free.

Extracted from ``server.py`` (which imports fastapi/pydantic at module
level, so tests load THIS file directly via
``importlib.util.spec_from_file_location`` and never import the package).
Pure data in, pure bool/str out — nothing here touches a browser.

B1 (next tier) tightens the gate in this module: the satisfaction function
is the single seam for that change.
"""

# Content thresholds: a page shows "real content" when ANY of these hold.
# (A PDP satisfies via JSON-LD; a listing via the anchor count; either via a
# rendered price node. Challenge shells satisfy none — they are tiny and
# link-free by design.)
RENDER_GATE_MIN_ANCHORS = 25
RENDER_GATE_MIN_BODY = 20_000


def render_gate_satisfied(probe: dict, wait_for_hit: bool) -> bool:
    """[wave-32 B1] Content evidence ALONE decides.

    ``wait_for_hit`` is accepted for call-site compatibility and deliberately
    NOT consulted: a selector attach proves nothing about content — challenge
    shells carry arbitrary elements, and 587's antibot 429 was overridden on
    exactly that lie (plus the innerHTML body arm; server.py now measures
    innerText). The crocs-class save (429 status, real listing DOM) passes
    via anchors/jsonld/price/innerText with no selector involved.
    """
    if not isinstance(probe, dict):
        return False
    return bool(
        probe.get("jsonld_items", 0) > 0
        or probe.get("anchors", 0) >= RENDER_GATE_MIN_ANCHORS
        or probe.get("body_len", 0) >= RENDER_GATE_MIN_BODY
        or probe.get("has_price", False)
    )


def render_gate_arm(probe: dict, wait_for_hit: bool) -> str:
    """WHICH content signal satisfied the gate — first match in gate order,
    or ``"none"``. Recorded in the settle result so the override note (and
    any RCA) never has to guess. [wave-32 B1] ``wait_for_hit`` is no longer
    an arm — content or nothing.
    """
    if not isinstance(probe, dict):
        return "none"
    if probe.get("jsonld_items", 0) > 0:
        return "jsonld"
    if probe.get("anchors", 0) >= RENDER_GATE_MIN_ANCHORS:
        return "anchors"
    if probe.get("body_len", 0) >= RENDER_GATE_MIN_BODY:
        return "body_len"
    if probe.get("has_price", False):
        return "has_price"
    return "none"


def render_gate_note(blocked_type, status_code, arm: str, probe) -> str:
    """The content-over-status override note: names the satisfying arm and
    the probe counts that earned the override."""
    counts = ""
    if isinstance(probe, dict):
        counts = (
            f"anchors={probe.get('anchors', 0)} "
            f"jsonld={probe.get('jsonld_items', 0)} "
            f"body_len={probe.get('body_len', 0)} "
            f"has_price={bool(probe.get('has_price', False))}"
        )
    return (
        f"blocked_type={blocked_type!r} from status={status_code} overridden — "
        f"render gate verified content via arm={arm} ({counts})"
    )
