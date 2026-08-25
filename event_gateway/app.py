"""FastAPI app: the /ws/v1/jobs websocket + /health.

Protocol per async_api.yaml. Event fan-out subscribes to the emit()
Redis channel per subscribed job; terminal events close subscriptions
(not the connection — a partner's other jobs keep streaming).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402

from gateway import (  # noqa: E402
    HEARTBEAT_SECONDS,
    check_ws_token,
    event_allowed,
    handle_control_sync,
    heartbeat_ping_frame,
    retire_subscription,
    verify_api_key,
)

logger = logging.getLogger("event_gateway")


def _run_control_sync(user_id: int, raw: str, subs: set, filters: dict) -> str | None:
    """Sync control-frame handler (psycopg inside) — run in the executor so
    DB dials never block the event loop."""
    return handle_control_sync(user_id, raw, subs, filters)
app = FastAPI(title="Partner Event Gateway", version="1")

_TERMINAL_OPS = ("job.scraper_ready", "job.failed")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "event-gateway", "ts": time.time()}


@app.websocket("/ws/v1/jobs")
async def ws_jobs(ws: WebSocket, token: str = ""):
    """Auth order: subprotocol-carried API key, else single-use ?token=."""
    # X-API-Key via subprotocol (browsers can't set WS headers)
    import asyncio as _aio

    raw_key = ws.query_params.get("apikey") or ""
    loop = _aio.get_running_loop()
    # psycopg is sync — never block the event loop with DB dials (a burst
    # of handshakes + healthchecks once stalled the first ack >5s)
    user_id = await loop.run_in_executor(
        None, verify_api_key, raw_key
    ) if raw_key else await loop.run_in_executor(None, check_ws_token, token)
    if user_id is None:
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()

    subs: set[int] = set()
    filters: dict[int, set[str] | None] = {}  # job_id → event filter (None = default)
    state = {"last_send": time.monotonic()}

    async def reader():
        while True:
            raw = await ws.receive_text()
            out = await _aio.get_running_loop().run_in_executor(
                None, _run_control_sync, user_id, raw, subs, filters
            )
            if out is not None:
                await ws.send_text(out)
                state["last_send"] = time.monotonic()

    async def redis_pump():
        import redis.asyncio as aioredis

        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        pubsub = r.pubsub()
        await pubsub.psubscribe("job:*:envelope")
        try:
            while True:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if not msg or msg.get("type") not in ("pmessage", "message"):
                    continue
                channel = msg["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                parts = channel.split(":")
                if len(parts) != 3 or parts[0] != "job":
                    continue
                try:
                    jid = int(parts[1])
                except ValueError:
                    continue
                if jid not in subs:
                    continue
                payload = msg["data"]
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", "replace")
                try:
                    env = json.loads(payload)
                except json.JSONDecodeError:
                    env = None
                # subscribe.events filter (B6-2): silently drop non-listed
                # types. Unparseable payloads have no type to test — forward
                # them rather than swallow (matches pre-filter behavior).
                if env is not None and not event_allowed(filters, jid, env.get("type", "")):
                    continue
                await ws.send_text(payload)
                state["last_send"] = time.monotonic()
                if env is not None and env.get("type") in _TERMINAL_OPS:
                    # retire; connection stays for other jobs
                    retire_subscription(jid, subs, filters)
        finally:
            try:
                await pubsub.close()
                await r.aclose()
            except Exception:
                pass

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - state["last_send"] >= HEARTBEAT_SECONDS:
                await ws.send_text(heartbeat_ping_frame())
                state["last_send"] = time.monotonic()

    tasks = [
        asyncio.create_task(reader(), name="reader"),
        asyncio.create_task(redis_pump(), name="pump"),
        asyncio.create_task(heartbeat(), name="heartbeat"),
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                logger.exception("ws task %s failed", t.get_name(), exc_info=exc)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
