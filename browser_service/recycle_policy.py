"""[wave-37 W37-OPS] Sustained-pressure proactive recycle policy.

Stdlib-only on purpose: server.py imports fastapi (absent from the django
test image), so the trigger arithmetic lives here and both server.py and
the tests load it directly.

Memory re-saturates within ~24h under batch load (3 events in 3 days:
99.2% @28.8h on 09-17, 1.00 @38.7h on 09-18). The REACTIVE gate (429
backpressure at 0.90) works but sheds live jobs. This is the OPTIONAL
containment: when the cgroup ratio stays >= BROWSER_RECYCLE_RATIO for
BROWSER_RECYCLE_SUSTAINED_S, the maintenance loop recycles the SCRAPER
Chrome only (never MCP — analyzer/tester sessions ride it) between
navigations, behind BROWSER_PROACTIVE_RECYCLE (default OFF — flag-off
keeps prod behavior identical until the tradeoff is consciously taken).
"""
from __future__ import annotations

import os


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in (
        "", "0", "false", "no", "off",
    )


BROWSER_PROACTIVE_RECYCLE = _env_flag("BROWSER_PROACTIVE_RECYCLE")
BROWSER_RECYCLE_RATIO = float(os.environ.get("BROWSER_RECYCLE_RATIO", "0.75"))
BROWSER_RECYCLE_SUSTAINED_S = float(
    os.environ.get("BROWSER_RECYCLE_SUSTAINED_S", "600")
)


# ── [wave-42 T2] idle MCP-Chrome recycle ─────────────────────────────────
# The MCP Chrome had NO idle lever (the W37 note above says "never MCP —
# analyzer/tester sessions ride it"). The idle-memory RCA changed the
# tradeoff: its floor (~250-400MB) plus the never-disposed walk-tab
# renderers are the single biggest avoidable slice of the fleet's idle RAM.
# The hazard that made it "never" was killing a LIVE walk — which the
# one-shot-SSE architecture made undetectable by connection checks alone
# (each tool call is a transient connection; the 150s+ LLM turns between
# calls read as idle). So the decision requires an explicit walk-claim
# window (celery POSTs /mcp/walk-claim with the node's whole budget as TTL)
# IN ADDITION to the live-client check. Flag-off keeps prod identical.
MCP_IDLE_RECYCLE = _env_flag("MCP_IDLE_RECYCLE")
MCP_IDLE_RECYCLE_COOLDOWN_S = float(
    os.environ.get("MCP_IDLE_RECYCLE_COOLDOWN_S", "21600")
)


def mcp_recycle_due(
    flag: bool,
    claim_until: float,
    now: float,
    last_recycle: float,
    cooldown_s: float,
    client_connected: bool,
) -> bool:
    """True exactly when the idle MCP recycle may fire.

    EVERY guard must hold:
      - ``flag`` — MCP_IDLE_RECYCLE (default OFF: prod identical until
        consciously enabled);
      - ``claim_until`` — no live walk-claim window (a crashed claimant's
        TTL self-heals when it lapses);
      - ``client_connected`` — no MCP tool call in flight right now
        (fail-safe: a connected client always blocks);
      - ``cooldown_s`` elapsed since the previous recycle (bounds how often
        the operation is even attempted).
    """
    if not flag:
        return False
    if claim_until and now < claim_until:
        return False
    if client_connected:
        return False
    return (now - last_recycle) >= cooldown_s


class SustainedPressureTracker:
    """Dwell tracker for "memory has been high for long enough".

    ``observe(ratio, now)`` is one maintenance-cycle observation. It returns
    True exactly on the cycle the recycle should fire. Arithmetic:
      - disabled → never fire, clock stays reset;
      - ratio None (unknowable) or below threshold → NOT sustained: reset
        the clock (a brief spike does not arm a delayed fire);
      - first sight above threshold → arm the clock, don't fire;
      - sustained for >= sustained_s → fire (the arm is consumed by the
        caller's recycle; while pressure holds, every subsequent eligible
        cycle fires again).
    """

    def __init__(
        self,
        ratio_threshold: float,
        sustained_s: float,
        enabled: bool,
    ) -> None:
        self.ratio_threshold = float(ratio_threshold)
        self.sustained_s = float(sustained_s)
        self.enabled = bool(enabled)
        self.high_since: float | None = None

    def observe(self, ratio: float | None, now: float) -> bool:
        if not self.enabled:
            self.high_since = None
            return False
        if ratio is None or ratio < self.ratio_threshold:
            self.high_since = None  # below threshold resets the clock
            return False
        if self.high_since is None:
            self.high_since = now  # first sight — arm, don't fire
            return False
        return (now - self.high_since) >= self.sustained_s

    def reset(self) -> None:
        self.high_since = None
