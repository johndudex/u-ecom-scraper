"""HTTP client for the File Master artifact store.

Used by the celery-worker (writes published artifacts) and django (reads them
for serving). browser_service does NOT use this — it receives scraper source in
the /scrape request body and returns output in the response.

Keys are logical paths: ``scrapers/{slug}/scraper.py``,
``scrapers/{slug}/output_2026-...json``, ``scrapers/{slug}/analysis/...json``.
The File Master stores them verbatim under its ``/data`` volume.

This module is pure-python (httpx only) — no Django import — so it serves both
the Celery worker and Django without setup differences. ``FILE_MASTER_URL`` is
read lazily so importing the module is always safe.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx

_DEFAULT_TIMEOUT = 120.0


def _base() -> str:
    url = os.environ.get("FILE_MASTER_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "FILE_MASTER_URL is not set — the File Master artifact service is "
            "required for cross-service artifact access."
        )
    return url


def _key_url(key: str) -> str:
    # httpx encodes the path segments; keys are already logical paths.
    return f"{_base()}/artifacts/{key.lstrip('/')}"


def write(key: str, data: bytes) -> int:
    """Upload ``data`` (raw bytes) to ``key``. Returns byte count stored."""
    r = httpx.put(_key_url(key), content=data, timeout=_DEFAULT_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"artifacts.write({key}) -> HTTP {r.status_code}: {r.text[:300]}")
    return int(r.json().get("size", len(data)))


def write_text(key: str, text: str) -> int:
    return write(key, text.encode("utf-8"))


def write_json(key: str, obj: object) -> int:
    return write_text(key, json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def read(key: str) -> bytes:
    """Return the bytes at ``key``. Raises FileNotFoundError if missing."""
    r = httpx.get(_key_url(key), timeout=_DEFAULT_TIMEOUT)
    if r.status_code == 404:
        raise FileNotFoundError(key)
    if r.status_code >= 400:
        raise RuntimeError(f"artifacts.read({key}) -> HTTP {r.status_code}")
    return r.content


def read_text(key: str) -> str:
    return read(key).decode("utf-8")


def read_json(key: str):
    return json.loads(read_text(key))


def exists(key: str) -> bool:
    r = httpx.head(_key_url(key), timeout=30.0)
    return r.status_code == 200


def list_keys(prefix: str = "") -> list[str]:
    r = httpx.get(f"{_base()}/list", params={"prefix": prefix}, timeout=30.0)
    if r.status_code >= 400:
        raise RuntimeError(f"artifacts.list_keys({prefix}) -> HTTP {r.status_code}")
    return r.json().get("keys", [])


def delete(key: str) -> bool:
    r = httpx.delete(_key_url(key), timeout=30.0)
    if r.status_code >= 400:
        raise RuntimeError(f"artifacts.delete({key}) -> HTTP {r.status_code}")
    return bool(r.json().get("deleted"))


def stream_url(key: str) -> str:
    """URL for the File Master's streaming endpoint (large artifacts)."""
    return f"{_base()}/stream/{key.lstrip('/')}"


def public_url(key: str) -> str:
    """A django-proxied URL (so end-users never hit the File Master directly).

    Django mounts a view at ``/fm/artifact/<key>`` that streams the bytes from
    the File Master. Workers should pass this URL when artifacts are surfaced to
    end-users; otherwise use the logical key + the django resolver.
    """
    return f"/fm/artifact/{key.lstrip('/')}"


# ── key helpers ──────────────────────────────────────────────────────────────

def scrapers_key(slug: str, *parts: str) -> str:
    """Build a ``scrapers/{slug}/...`` key."""
    segs = [p.strip("/") for p in parts if p]
    return "/".join(["scrapers", slug, *segs])


def scrapers_output_glob(slug: str) -> str:
    """Prefix for listing a site's outputs: ``scrapers/{slug}/`` (filter ``output_*.json`` client-side)."""
    return f"scrapers/{slug}/"


def latest_output_key(slug: str) -> Optional[str]:
    """Newest ``scrapers/{slug}/output_*.json`` key, or None."""
    keys = [k for k in list_keys(f"scrapers/{slug}/") if k.split("/")[-1].startswith("output_") and k.endswith(".json")]
    return sorted(keys)[-1] if keys else None
