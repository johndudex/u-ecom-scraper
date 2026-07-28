"""File Master — a tiny artifact store over HTTP.

Owns the cross-service artifacts (the `scrapers/{slug}/...` tree that django
serves to users and browser_service re-executes) on a single persistent volume.
The worker writes; django reads; browser_service never touches it (it receives
scraper source in the /scrape request body and returns output in the response).

Kept deliberately minimal: a typed key→bytes store with PUT/GET/HEAD/LIST/
DELETE + /health. No auth (Railway private network only), no DB, no business
logic. Keys are logical paths like ``scrapers/{slug}/scraper.py`` and are stored
verbatim under ``/data/{key}``.

If a shared filesystem is ever re-introduced (or S3 desired), only this service's
internals change — the callers keep using ``src/artifacts.py``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("file_master")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ROOT = Path(os.environ.get("FILE_MASTER_ROOT", "/data")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="File Master", version="1")


def _safe_path(key: str) -> Path:
    """Resolve ``key`` under ROOT, rejecting path traversal (``..`` / absolute)."""
    if not key or key.startswith("/"):
        raise HTTPException(status_code=400, detail="invalid key")
    # {key:path} is URL-decoded; reject any backslash escape tricks.
    if "\x00" in key:
        raise HTTPException(status_code=400, detail="invalid key")
    candidate = (ROOT / key).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid key")
    return candidate


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "root": str(ROOT)}


@app.put("/artifacts/{key:path}")
async def put_artifact(key: str, request: Request) -> dict:
    p = _safe_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    # Atomic-ish: write to a sibling tmp then replace, so a partial PUT never
    # leaves a half-written artifact for readers.
    tmp = p.with_suffix(p.suffix + ".partial")
    tmp.write_bytes(body)
    os.replace(tmp, p)
    logger.info("PUT %s (%d bytes)", key, len(body))
    return {"ok": True, "key": key, "size": len(body)}


@app.get("/artifacts/{key:path}")
async def get_artifact(key: str) -> Response:
    p = _safe_path(key)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return Response(
        p.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


@app.head("/artifacts/{key:path}")
async def head_artifact(key: str) -> Response:
    p = _safe_path(key)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return Response(headers={"Content-Length": str(p.stat().st_size)})


@app.get("/list")
async def list_keys(prefix: str = Query("")) -> dict:
    base = _safe_path(prefix) if prefix else ROOT
    if not base.exists():
        return {"keys": []}
    root = base if base.is_dir() else base.parent
    keys = [
        str(p.relative_to(ROOT))
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".partial")
    ]
    return {"keys": sorted(keys)}


@app.delete("/artifacts/{key:path}")
async def delete_artifact(key: str) -> dict:
    p = _safe_path(key)
    if p.is_file():
        p.unlink()
        logger.info("DELETE %s", key)
        return {"ok": True, "key": key, "deleted": True}
    return {"ok": True, "key": key, "deleted": False}


@app.get("/stream/{key:path}")
async def stream_artifact(key: str) -> StreamingResponse:
    """Streaming variant for large artifacts (range/chunked by uvicorn)."""
    p = _safe_path(key)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")

    def _gen():
        with p.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_gen(), media_type="application/octet-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "file_master.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("FILE_MASTER_PORT", "8002")),
        log_level="info",
    )
