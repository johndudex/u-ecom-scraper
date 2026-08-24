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
