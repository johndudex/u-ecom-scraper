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
    handle_control,
    verify_api_key,
)

logger = logging.getLogger("event_gateway")
app = FastAPI(title="Partner Event Gateway", version="1")

_TERMINAL_OPS = ("job.scraper_ready", "job.failed")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "event-gateway", "ts": time.time()}


@app.websocket("/ws/v1/jobs")
async def ws_jobs(ws: WebSocket, token: str = ""):
    """Auth order: subprotocol-carried API key, else single-use ?token=."""
    # X-API-Key via subprotocol (browsers can't set WS headers)
    raw_key = ""
    offered = ws.query_params.get("apikey") or ""
    if offered:
        raw_key = offered
    user_id = verify_api_key(raw_key) if raw_key else check_ws_token(token)
    if user_id is None:
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()

    subs: set[int] = set()
    state = {"last_send": time.monotonic()}

    async def reader():
        while True:
            raw = await ws.receive_text()
            out = await handle_control(user_id, raw, subs)
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
                await ws.send_text(payload)
                state["last_send"] = time.monotonic()
                try:
                    env = json.loads(payload)
                    if env.get("type") in _TERMINAL_OPS:
                        subs.discard(jid)  # retire; connection stays for other jobs
                except json.JSONDecodeError:
                    pass
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
                await ws.send_text(
                    json.dumps({"op": "heartbeat.ping", "data": {"ts": time.time()}})
                )
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
