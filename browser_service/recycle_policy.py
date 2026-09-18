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
