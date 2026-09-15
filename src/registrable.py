"""Registrable-domain comparison — the canonical host gate comparator.

[wave-32 B3] Promoted from ``webapp/agents/nodes/run_execution.py``'s
``_registrable_of`` (kept there as the browser_service-context copy; this
module is the tested canonical for the webapp side — views.py and the
partner API import from here). Semantics intentionally match the pipeline's
F17 seed filter: subdomains (shop.marimekko.com) and www fold to the same
registrable domain, so the gate accepts exactly what F17 would keep and
declines exactly what it would drop.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Two-part public suffixes the pipeline recognizes (same list as F17).
TWO_PART_TLDS = (
    ".co.uk", ".org.uk", ".com.au", ".co.nz", ".co.za", ".com.br",
    ".co.jp", ".com.sg", ".com.mx",
)


def registrable_of(url_val: str) -> str:
    """Best-effort registrable domain (lowercased, www-stripped). '' on
    failure — callers treat '' as "cannot judge" and do not gate on it.
    """
    try:
        host = (urlparse(url_val).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        for tld in TWO_PART_TLDS:
            if host.endswith(tld):
                pre = host[: -len(tld)].rstrip(".")
                return f"{pre.split('.')[-1]}{tld}" if pre else host
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""
