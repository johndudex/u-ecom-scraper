"""Partner event emission (async_api.yaml — the outbox pattern).

emit() is the single entry point: graph/tasks call it at state transitions;
it writes an EventOutbox row in the caller's transaction and schedules the
Redis fan-out via transaction.on_commit. Only created_via="api" jobs emit
(critique M4 — internal intake traffic stays out of the outbox).
"""
from .emitter import emit, new_event_id  # noqa: F401
