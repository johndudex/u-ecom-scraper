"""SCRATCH: verify the heavy-page MCP stall is the accessibility SNAPSHOT, not the
page itself. Times a depth-3 snapshot vs the compact _PAGE_STATE_JS evaluate on
aya's heavy listing page. If evaluate is fast + snapshot stalls → the _PAGE_STATE_JS
swap is the right fix.
"""
import sys
import time

sys.path.insert(0, "/app/experimental/nav_traversal")

from agents.tools.playwright_tools import create_playwright_tools_sync  # noqa: E402
from traversal import _PAGE_STATE_JS  # noqa: E402

URL = "https://www.ayahealthcare.com/travel-nursing-jobs"

tools = create_playwright_tools_sync(fresh=True)
nav = next(t for t in tools if getattr(t, "name", "") == "playwright_browser_navigate")
ev = next(t for t in tools if getattr(t, "name", "") == "playwright_browser_evaluate")
snap = next(t for t in tools if getattr(t, "name", "") == "playwright_browser_snapshot")
assert nav and ev and snap, "missing tools"

print(f"navigating to {URL} ...", flush=True)
nav.invoke({"url": URL})
time.sleep(5)

print("\n[1] compact _PAGE_STATE_JS evaluate:", flush=True)
t0 = time.time()
try:
    r = ev.invoke({"function": _PAGE_STATE_JS})
    dt = time.time() - t0
    content = r.content if hasattr(r, "content") else str(r)
    print(f"    OK in {dt:.1f}s  (result {len(str(content))} chars)", flush=True)
except Exception as exc:
    print(f"    FAILED in {time.time()-t0:.1f}s: {str(exc)[:120]}", flush=True)

print("\n[2] depth-3 accessibility snapshot:", flush=True)
t0 = time.time()
try:
    s = snap.invoke({"depth": 3})
    dt = time.time() - t0
    content = s.content if hasattr(s, "content") else str(s)
    print(f"    OK in {dt:.1f}s  (snapshot {len(str(content))} chars)", flush=True)
except Exception as exc:
    print(f"    FAILED in {time.time()-t0:.1f}s: {str(exc)[:120]}", flush=True)

print("\n=== VERDICT ===", flush=True)
print("if [1] fast and [2] slow/failed → _PAGE_STATE_JS swap fixes the stall", flush=True)
