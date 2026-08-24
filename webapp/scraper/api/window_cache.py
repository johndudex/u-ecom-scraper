"""Bounded LRU of output-file text (fold M10 residual).

read_output_page needs the raw bytes to slice page windows from the byte
index; without a cache every page request refetches the whole file
(aya-class 101 MB × sequential walks = worker starvation — the exact M10
failure the critique priced). This keeps the last N files' text in
process memory, keyed by (key, size): a size change (schema-prune rewrite
+ re-index) invalidates rather than serving stale bytes.

Deliberately in-process, not Redis: the text is only useful to the worker
slicing pages from it, and 2 gunicorn workers each hold their own copy —
bounded by max_bytes per process.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict


class WindowCache:
    def __init__(self, max_files: int = 4, max_bytes: int = 128 * 1024 * 1024):
        self.max_files = max_files
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, int], str] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def get(self, key: str, size: int, fetch) -> str:
        """Return the file text, caching when it fits the budget."""
        cache_key = (key, size)
        with self._lock:
            hit = self._entries.get(cache_key)
        if hit is not None:
            with self._lock:
                self._entries.move_to_end(cache_key)
            return hit
        text = fetch(key, size)
        if len(text.encode("utf-8", "replace")) > self.max_bytes:
            return text  # oversized: serve, never cache (no thrash)
        with self._lock:
            self._entries[cache_key] = text
            self._bytes += len(text.encode("utf-8", "replace"))
            while (len(self._entries) > self.max_files or self._bytes > self.max_bytes) and self._entries:
                _k, _v = self._entries.popitem(last=False)
                self._bytes -= len(_v.encode("utf-8", "replace"))
        return text


# One cache per process. 4 files / 128 MB — generous for current outputs
# (aya 101 MB), bounded below the 1g container limit alongside Django.
_cache = WindowCache(
    max_files=int(os.environ.get("OUTPUT_CACHE_FILES", "4")),
    max_bytes=int(os.environ.get("OUTPUT_CACHE_BYTES", str(128 * 1024 * 1024))),
)


def window_fetch(key: str, size: int) -> str:
    """Default fetcher: File Master read_text."""
    from ..api.output_index import _fm_read_text

    return _fm_read_text(key)
