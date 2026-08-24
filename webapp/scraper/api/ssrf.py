"""Create-time SSRF gate for partner callback URLs (sync_api.yaml).

Delivery-time re-validation (DNS rebinding between create and send) is the
1b dispatcher's hard requirement (critique B4) — this module is the shared
predicate both halves use.

Policy:
- https only, port 443 (explicit :443 fine, anything else rejected)
- literal IPs: public unicast only (private/loopback/link-local/reserved/
  multicast rejected, incl. v4-integer/hex spellings via ipaddress)
- hostnames: every A/AAAA record must be public — one private record
  rejects (no partial trust; rebinding setups rotate records)
- unresolvable → rejected (resolver injectable for tests; no network there)
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _bad_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # integer/hex spellings ("2130706433", "0x7f000001") — inet_aton
        # parses them; normalize through the same gate
        try:
            packed = socket.inet_aton(ip_str)
            addr = ipaddress.ip_address(packed)
        except (OSError, ValueError):
            return True
    return not addr.is_global


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return list({i[4][0] for i in infos})


def validate_callback_url(url: str, resolver=_resolve) -> str | None:
    """Return None when the URL passes the gate, else a human reason."""
    try:
        p = urlparse(url)
    except ValueError:
        return "callback_url is not a valid URL"
    if p.scheme != "https":
        return "callback_url must use https"
    if p.port is not None and p.port != 443:
        return "callback_url port must be 443 (https)"
    host = (p.hostname or "").strip("[]")
    if not host:
        return "callback_url has no host"

    if _looks_like_ip(host):
        if _bad_ip(host):
            return f"callback_url host {host} is not a public address"
        return None

    records = resolver(host)
    if not records:
        return f"callback_url host {host} does not resolve"
    bad = [r for r in records if _bad_ip(r.split("%")[0])]
    if bad:
        return f"callback_url host {host} resolves to a non-public address"
    return None


def _looks_like_ip(host: str) -> bool:
    if host.startswith("[") or ":" in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)  # integer/hex v4 spellings
        return True
    except OSError:
        return False
