"""Per-key Redis fixed-window rate limiting (sync_api.yaml x-rate-limits).

10 req/s sustained, burst 30. Uses the project's shared redis client
(scraper.services._get_redis — plain `redis` lib via CELERY_BROKER_URL);
django_redis is NOT installed in this deployment. Redis-down = fail-open
(limits are protection, not auth).
"""
from __future__ import annotations

import time

RATE_RPS = 10
RATE_BURST = 30

# wave-41: dedicated budget for POST /api/v1/discover-fields — one call
# costs a browser render (~25s) + an LLM pass (~20s), ~1000x check-site.
DISCOVERY_RPM = 6
DISCOVERY_LOCK_TTL = 45  # > httpx budget (30s); a lock never outlives its request


def _conn():
    from scraper.services import _get_redis

    return _get_redis()


def check_rate_limit(key_prefix: str) -> int | None:
    """Return Retry-After seconds if over limit, else None (fail-open)."""
    try:
        conn = _conn()
        now = time.time()
        burst_key = f"rl:{key_prefix}:b:{int(now)}"
        sus_key = f"rl:{key_prefix}:s:{int(now // 60)}"
        pipe = conn.pipeline()
        pipe.incr(burst_key)
        pipe.expire(burst_key, 5)
        pipe.incr(sus_key)
        pipe.expire(sus_key, 70)
        burst, _, sustained, _ = pipe.execute()
        if burst > RATE_BURST:
            return 1  # next 1s window
        if sustained > RATE_RPS * 60:
            return 60 - int(now % 60)
        return None
    except Exception:
        return None


def check_discovery_rate(key_prefix: str) -> int | None:
    """Dedicated budget for POST /api/v1/discover-fields (wave-41):
    6 req/min per key AND 1 concurrent render per key — the global 10 r/s
    window alone would let a partner stack 30 renders against a browser
    service that runs 3. Returns Retry-After seconds if over either limit
    (and takes the slot lock), else None. Fail-open like the global
    limiter: limits are protection, not auth."""
    try:
        conn = _conn()
        now = time.time()
        minute_key = f"rl:discover:{key_prefix}:m:{int(now // 60)}"
        lock_key = f"rl:discover:{key_prefix}:lock"
        pipe = conn.pipeline()
        pipe.incr(minute_key)
        pipe.expire(minute_key, 70)
        pipe.set(lock_key, "1", nx=True, ex=DISCOVERY_LOCK_TTL)
        count, _, got_lock = pipe.execute()
        if not got_lock:
            return DISCOVERY_LOCK_TTL  # bounded: lock never outlives a render
        if int(count) > DISCOVERY_RPM:
            # over the window — drop the slot we just took so the queued
            # retry isn't blocked by a lock held by nobody
            conn.delete(lock_key)
            return 60 - int(now % 60)
        return None
    except Exception:
        return None


def release_discovery_slot(key_prefix: str) -> None:
    """Free the per-key concurrent-render slot (handler finally-block)."""
    try:
        _conn().delete(f"rl:discover:{key_prefix}:lock")
    except Exception:
        pass
