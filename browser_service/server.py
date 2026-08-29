import asyncio
import glob
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import functools  # F1: run_in_executor takes no kwargs — bind via partial

from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from .browser_pool import browser_pool
from .config import get_proxy_config
from .probe import run_probe, render_page
from .scraper_runner import run_scraper_script

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

PROBE_LOCK = asyncio.Lock()
AKAMAI_SEMAPHORE = asyncio.Semaphore(2)

CLEANUP_INTERVAL = 1800
CDP_LIVENESS_INTERVAL = 15
# Require N consecutive probe misses before restarting Chrome. The old value of
# 1 meant a single transient 3s-timeout probe blip (Chrome busy serving a real
# /scrape, or a momentary stall) SIGTERM'd a healthy Chrome — and since the
# liveness loop is NOT gated on /scrape activity, this manufactured a ~30-min
# restart storm that showed up as "CDP liveness DOWN" in 81 events with ZERO
# real Chrome crashes in 111 jobs. 3 tolerates blips without masking a real
# sustained death (3 × 15s = 45s of confirmed unresponsiveness).
CDP_MAX_CONSECUTIVE_FAILURES = 3

PERSISTENT_CHROME_PIDS: set[int] = set()
mcp_process: Optional[subprocess.Popen] = None

# ── /navigate config ─────────────────────────────────────────────────────
# Process start (wall clock) — /health's uptime. time.monotonic() was WRONG
# there: it reads as time since an arbitrary epoch (host boot), so the metric
# reported the RAILWAY HOST's uptime, not this process's (a 97-day "uptime"
# on a service deployed yesterday). Restarts are now actually visible.
PROCESS_START = time.time()
# Independent of PROBE_LOCK and /scrape. Bounds concurrent ephemeral browsers
# to the memory budget; excess callers get 429 + retry_after.
NAVIGATE_MAX_CONCURRENT = int(os.environ.get("NAVIGATE_MAX_CONCURRENT", "3"))
NAVIGATE_MAX_QUEUE = int(os.environ.get("NAVIGATE_MAX_QUEUE", "4"))
MAX_NAVIGATE_HTML = 2_000_000
MAX_NAVIGATE_ACTIONS = 20
NAVIGATE_SEMAPHORE = asyncio.Semaphore(NAVIGATE_MAX_CONCURRENT)

# ── W4: dedicated executors ──────────────────────────────────────────────
# The default executor shared ONE pool between /navigate (multi-minute
# browser launches), /scrape (multi-minute subprocesses), /health's liveness
# probe, and the restart path. Under load, navigate/scrape work filled the
# pool and /health's probe NEVER RAN (dispatch on a saturated executor) —
# the blind-by-construction failure mode from the 502-window post-mortem.
# POOL INVARIANT: no endpoint may block on a call that itself runs on the
# same pool. The live instance is scraper_runner's crash-retry path POSTing
# /restart-cdp from inside a SCRAPE worker: /restart-cdp must therefore
# never share a pool with /scrape (it gets its own single thread; a restart
# is rare and serialized by browser_pool._restart_lock anyway).
NAVIGATE_EXECUTOR = ThreadPoolExecutor(
    max_workers=NAVIGATE_MAX_CONCURRENT, thread_name_prefix="navigate"
)
# W4 admission control: /scrape is admit-or-429 (no queueing — a queued
# caller's timeout clock lies because it starts at admission, not arrival).
SCRAPE_MAX_CONCURRENT = int(os.environ.get("SCRAPE_MAX_CONCURRENT", "2"))
SCRAPE_MAX_QUEUE = 0
SCRAPE_EXECUTOR = ThreadPoolExecutor(
    max_workers=SCRAPE_MAX_CONCURRENT, thread_name_prefix="scrape"
)
MISC_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="misc")
RESTART_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restart")
# Own slot so /health can NEVER be starved by probe/render work. W6 removes
# this dispatch entirely (cached liveness); until then it stays truthful.
HEALTH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health")

# W4 /navigate pre-launch memory gate: refuse the fork when the cgroup is
# already near its ceiling — Errno 11 (fork EAGAIN) under pressure was the
# root cause of the prod 502 windows. Falls OPEN when the cgroup files are
# unreadable (non-Linux cgroup v2 layouts); set ≤0 to disable.
NAVIGATE_MEMORY_GATE_RATIO = float(os.environ.get("NAVIGATE_MEMORY_GATE_RATIO", "0.85"))


def _cgroup_memory_ratio() -> float | None:
    """Current cgroup memory usage / limit, or None when undeterminable.

    Reads cgroup v2 first (/sys/fs/cgroup/memory.{current,max}), then v1.
    ``max``/unlimited limits yield None (no meaningful ratio).
    """
    try:
        with open("/sys/fs/cgroup/memory.current") as fh:
            current = int(fh.read().strip())
        with open("/sys/fs/cgroup/memory.max") as fh:
            raw = fh.read().strip()
        if raw == "max":
            return None
        limit = int(raw)
        return (current / limit) if limit > 0 else None
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as fh:
            current = int(fh.read().strip())
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as fh:
            limit = int(fh.read().strip())
        # v1 reports a huge sentinel for "no limit"
        if limit <= 0 or limit > (1 << 60):
            return None
        return current / limit
    except (OSError, ValueError):
        return None


def _derived_retry_after(base: float | None = None) -> int:
    """Backpressure retry hint, clamped to [15, 60]s.

    The old hardcoded ``retry_after: 5`` manufactured retry storms: five
    seconds is inside the window where the memory pressure that caused the
    rejection is still building, so every client came back at the worst
    possible moment.
    """
    value = 15.0 if base is None else float(base)
    return int(min(max(value, 15.0), 60.0))


def _backpressure(
    status_code: int,
    error: str,
    retry_after: float | None = None,
    **extra,
) -> JSONResponse:
    """429/503 rejection with the retry hint in BOTH header and body.

    Older clients read only the body's ``retry_after``; the W8 template (and
    anything HTTP-standards-compliant) reads only the header. Emit both.
    """
    derived = _derived_retry_after(retry_after)
    content = {"success": False, "error": error, "retry_after": derived}
    content.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"Retry-After": str(derived)},
    )
# PIDs of chrome processes belonging to in-flight /navigate calls. The orphan
# killer (every CLEANUP_INTERVAL) must not SIGKILL these. Timestamped
# (monotonic) so the gate can distinguish a live browser from a leaked entry —
# the in-flight counter alone is decremented by the endpoint's finally, which
# runs on wait_for timeout while the executor thread's browser is STILL
# RUNNING (prod observed a 92.7s response against a 75s deadline).
NAVIGATE_ACTIVE_PIDS: dict[int, float] = {}
# How long a tracked PID stays protective: the per-call ceiling (NavigateRequest
# timeout ≤ 180s, + 30s wait_for slack) plus grace. Past this the entry is a
# leak and stops protecting (same deadline-failsafe shape as SCRAPE_PROTECTION_GRACE_S).
_NAVIGATE_PID_MAX_AGE_S = 180 + 120
# Active + queued navigate calls (for queue-full backpressure and the orphan
# killer safety gate).
_navigate_in_flight = 0


def _track_navigate_pids(pids) -> None:
    """Register /navigate session PIDs (timestamped)."""
    now = time.monotonic()
    for pid in pids:
        NAVIGATE_ACTIVE_PIDS[int(pid)] = now


def _untrack_navigate_pids(pids) -> None:
    """Release /navigate session PIDs after their browser is closed."""
    for pid in pids:
        NAVIGATE_ACTIVE_PIDS.pop(int(pid), None)


def _proc_state(pid: int) -> Optional[str]:
    """Single-char process state from /proc/<pid>/stat, or None if gone.

    Zombie-aware: a zombie still satisfies ``os.kill(pid, 0)``, so liveness
    checks must read the state field instead (a zombie holds its PID until
    reaped — with uvicorn as PID 1 pre-tini that can be indefinitely).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
        # comm can contain spaces/parens — everything after the LAST ')' is fixed-format.
        return data.rsplit(")", 1)[1].strip().split()[0]
    except (OSError, IndexError):
        return None


def _navigate_protection_active() -> bool:
    """True while any /navigate may still own a live ephemeral browser.

    Two layers:
    - the in-flight counter (covers queued calls),
    - the per-PID registry with liveness + age checks (covers the
      counter-decrement race above — the browser outliving its HTTP call).

    Dead PIDs (gone or zombie) are pruned as a side effect; entries older
    than ``_NAVIGATE_PID_MAX_AGE_S`` are treated as leaked and stop
    protecting so a bug can never permanently disable the orphan killer.
    """
    now = time.monotonic()
    dead: list[int] = []
    for pid, ts in NAVIGATE_ACTIVE_PIDS.items():
        if now - ts > _NAVIGATE_PID_MAX_AGE_S:
            logger.warning(
                "navigate pid registry: PID %d tracked %.0fs (max %ds) — treating as leaked",
                pid,
                now - ts,
                _NAVIGATE_PID_MAX_AGE_S,
            )
            dead.append(pid)
            continue
        state = _proc_state(pid)
        if state is None or state == "Z":
            dead.append(pid)
    for pid in dead:
        NAVIGATE_ACTIVE_PIDS.pop(pid, None)
    return _navigate_in_flight > 0 or bool(NAVIGATE_ACTIVE_PIDS)

# ── /scrape in-flight registry (F1) ────────────────────────────────────────
# A running scraper subprocess drives the SHARED scraper Chrome; the orphan
# killer must not reap that Chrome's children mid-run (prod 325/328/334).
# Keyed by request-id with a monotonic deadline: the endpoint wraps the run in
# wait_for(timeout+120) so the executor thread can outlive the HTTP response —
# the entry is released by the guarded runner only when the child is reaped,
# and the deadline is a failsafe so a wedged holder can never permanently
# disable the killer.
SCRAPE_IN_FLIGHT: dict[str, float] = {}
SCRAPE_PROTECTION_GRACE_S = 600.0


def _scrape_protection_active() -> bool:
    now = time.monotonic()
    stale = [rid for rid, dl in SCRAPE_IN_FLIGHT.items() if now > dl]
    for rid in stale:
        SCRAPE_IN_FLIGHT.pop(rid, None)
        logger.warning("scrape protection for %s expired (leaked holder?)", rid)
    return bool(SCRAPE_IN_FLIGHT)


def _run_scrape_guarded(rid: str, **kwargs):
    """Executor wrapper: hold the kill-cycle protection until the child exits."""
    try:
        from .scraper_runner import run_scraper_script

        return run_scraper_script(**kwargs)
    finally:
        SCRAPE_IN_FLIGHT.pop(rid, None)


async def _start_mcp_process() -> bool:
    """Start (or restart) the Playwright MCP server process.
    Returns True on success, False on failure. Sets module-level ``mcp_process``."""
    global mcp_process
    try:
        if mcp_process and mcp_process.poll() is None:
            logger.info("Stopping stale MCP process (PID %d)...", mcp_process.pid)
            try:
                os.killpg(os.getpgid(mcp_process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                mcp_process.kill()
            try:
                mcp_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(mcp_process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    mcp_process.kill()
        mcp_process = None

        mcp_internal_port = os.environ.get("MCP_CDP_PORT", "19222")
        mcp_cmd = [
            "npx",
            "@playwright/mcp",
            "--cdp-endpoint",
            f"http://127.0.0.1:{mcp_internal_port}",
            "--port",
            "8111",
            "--host",
            "0.0.0.0",
            "--allowed-hosts",
            "*",
            # Defense-in-depth: explicit Playwright timeouts so the MCP server
            # returns a proper error (fast) on stuck actions/navigations instead
            # of hanging until the SSE client's 90s backstop. NOTE: these do NOT
            # cover `evaluate()` or `browser_snapshot` (no Playwright default) —
            # those are bounded client-side by the SSE timeout.
            "--timeout-action", "10000",      # 10s for click/fill/check
            "--timeout-navigation", "45000",   # 45s for page.goto/waitForURL
        ]
        logger.info("Starting Playwright MCP: %s", " ".join(mcp_cmd))
        mcp_log = open("/tmp/mcp-stdout.log", "w")
        mcp_process = subprocess.Popen(
            mcp_cmd,
            stdout=mcp_log,
            stderr=mcp_log,
            start_new_session=True,
        )
        await asyncio.sleep(3)
        if mcp_process.poll() is not None:
            mcp_log.close()
            try:
                with open("/tmp/mcp-stdout.log") as f:
                    stderr_output = f.read()
            except Exception:
                stderr_output = "(no log)"
            logger.error(
                "Playwright MCP failed to start (exit %d): %s",
                mcp_process.returncode,
                stderr_output[:500],
            )
            mcp_process = None
            return False
        else:
            logger.info(
                "Playwright MCP started (PID %d) on 0.0.0.0:8111 -> CDP 127.0.0.1:%s",
                mcp_process.pid,
                mcp_internal_port,
            )
            return True
    except Exception as e:
        logger.error("Failed to start Playwright MCP: %s", e)
        mcp_process = None
        return False


async def _periodic_cleanup():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            await _cleanup_chrome_artifacts()
        except Exception:
            logger.exception("Periodic cleanup failed")


async def _periodic_cdp_liveness():
    """Background self-healing loop.

    Every ``CDP_LIVENESS_INTERVAL`` seconds we actually probe the CDP HTTP
    endpoints (not just process liveness). After ``CDP_MAX_CONSECUTIVE_FAILURES``
    consecutive failures we automatically restart the affected Chrome instance.
    """
    failures = {"mcp": 0, "scraper": 0}
    while True:
        await asyncio.sleep(CDP_LIVENESS_INTERVAL)
        try:
            liveness = await asyncio.get_event_loop().run_in_executor(
                MISC_EXECUTOR, browser_pool.check_cdp_liveness
            )

            for label, alive, key in (
                ("mcp", liveness.get("mcp_cdp_alive"), "mcp"),
                ("scraper", liveness.get("scraper_cdp_alive"), "scraper"),
            ):
                if alive:
                    if failures[key] > 0:
                        logger.info(
                            "CDP liveness: %s recovered after %d failed probes",
                            label,
                            failures[key],
                        )
                    failures[key] = 0
                else:
                    failures[key] += 1
                    logger.warning(
                        "CDP liveness: %s DOWN (consecutive failures=%d/%d)",
                        label,
                        failures[key],
                        CDP_MAX_CONSECUTIVE_FAILURES,
                    )
                    if failures[key] >= CDP_MAX_CONSECUTIVE_FAILURES:
                        logger.error(
                            "CDP liveness: auto-restarting %s Chrome after %d consecutive failures",
                            label,
                            failures[key],
                        )
                        try:
                            res = await asyncio.get_event_loop().run_in_executor(
                                RESTART_EXECUTOR, browser_pool.restart_chrome, label
                            )
                            logger.info("CDP auto-restart %s result: %s", label, res)
                            # For MCP Chrome, also restart the Playwright MCP process
                            # so it gets a fresh CDP WebSocket to the new Chrome instance.
                            if label == "mcp" and not res.get("errors"):
                                await asyncio.sleep(2)
                                mcp_ok = await _start_mcp_process()
                                if not mcp_ok:
                                    logger.error("MCP process failed to restart after Chrome restart")
                                    res.setdefault("errors", []).append("mcp_restart_failed")
                            # reset counter on successful restart (errors list empty)
                            if not res.get("errors"):
                                failures[key] = 0
                        except Exception:
                            logger.exception("CDP auto-restart %s raised", label)

            # Also check MCP process liveness independent of CDP.
            # MCP node process can crash even when Chrome CDP is fine.
            if mcp_process is not None and mcp_process.poll() is not None:
                logger.error(
                    "CDP liveness: MCP process (PID %d) exited with code %d, restarting...",
                    mcp_process.pid, mcp_process.returncode,
                )
                await asyncio.sleep(1)
                await _start_mcp_process()

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CDP liveness loop error")


def _cleanup_chrome_artifacts_sync():
    # F1: snapshot + kill atomically under the restart lock so a Chrome that
    # restarts mid-cycle can't have its fresh (un-allowlisted) PID killed.
    # Non-blocking: a restart in progress means the tree is being torn down
    # anyway — skip the cycle (orphans reaped next interval).
    if not browser_pool._restart_lock.acquire(blocking=False):
        logger.info("cleanup: skipping kill cycle (Chrome restart in progress)")
        _clean_chrome_profile_cache()
        return
    try:
        _collect_persistent_pids()
        killed = _kill_orphan_chrome()
    finally:
        browser_pool._restart_lock.release()
    cleaned = _clean_chrome_profile_cache()
    if killed or cleaned:
        logger.info(
            "Cleanup: killed %d orphan Chrome processes, cleaned %d profile dirs",
            killed,
            cleaned,
        )


async def _cleanup_chrome_artifacts():
    # W5: this body used to run directly ON the event loop (async def, zero
    # awaits) — a slow profile-cache rmtree blocked every endpoint for its
    # whole duration. It now occupies one MISC worker instead.
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(MISC_EXECUTOR, _cleanup_chrome_artifacts_sync)


def _proc_children(pid: int) -> set[int]:
    """Direct children of pid via /proc (Linux). Empty on any failure."""
    kids: set[int] = set()
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            try:
                with open(f"/proc/{pid}/task/{tid}/children") as fh:
                    kids.update(int(x) for x in fh.read().split())
            except OSError:
                continue
    except OSError:
        pass
    return kids


def _collect_persistent_pids():
    """F1: protect the FULL process tree of both persistent Chromes.

    The old version collected only the two top-level launcher PIDs — the
    orphan killer then SIGKILLed ~20 renderer/GPU/utility children every
    30-min cycle, CDP went DOWN, and both Chromes auto-restarted (prod's
    permanent kill→restart loop that crashed jobs 325/328/334 and hung
    272's product_analyzer mid-browse).
    """
    PERSISTENT_CHROME_PIDS.clear()
    try:
        h = browser_pool.health()
        roots = [h.get(k) for k in ("mcp_pid", "scraper_pid")]
        for root in roots:
            if not root:
                continue
            # BFS the process tree (children lists change as Chrome spawns
            # helpers; walk until fixpoint with a hard depth bound).
            seen: set[int] = set()
            frontier = [root]
            for _ in range(32):
                new = [p for p in frontier if p not in seen]
                if not new:
                    break
                for p in new:
                    seen.add(p)
                nxt: set[int] = set()
                for p in new:
                    nxt |= _proc_children(p)
                frontier = list(nxt - seen)
            for p in seen:
                PERSISTENT_CHROME_PIDS.add(p)
    except Exception:
        pass


def _kill_orphan_chrome() -> int:
    killed = 0
    try:
        # Safety gate: if any /navigate call is in flight — or any tracked
        # session PID is still alive — skip the kill cycle entirely. Ephemeral
        # browsers spawn child chrome processes that pgrep matches, and we
        # cannot reliably enumerate every child PID. The per-PID allowlist is
        # the precision layer; this gate is the hard guarantee. Navigate calls
        # are short (<=180s); real orphans get reaped on the next
        # CLEANUP_INTERVAL cycle. The gate is liveness-based (see
        # _navigate_protection_active): a live browser whose HTTP call already
        # timed out must NOT be killed just because the counter moved on.
        nav_active = _navigate_protection_active()
        if nav_active:
            logger.info(
                "kill_orphan_chrome: skipping (%d navigate call(s) in flight, %d live session PID(s))",
                _navigate_in_flight,
                len(NAVIGATE_ACTIVE_PIDS),
            )
            return 0
        # F1: same gate for /scrape — a running scraper drives the shared
        # Chrome; killing its children mid-run is the prod 325/328/334 crash.
        if _scrape_protection_active():
            logger.info("kill_orphan_chrome: skipping (scrape in flight)")
            return 0
        result = subprocess.run(
            ["pgrep", "-f", "chrome"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                pid_str = line.strip()
                if not pid_str:
                    continue
                pid = int(pid_str)
                if (
                    pid in PERSISTENT_CHROME_PIDS
                    or pid == 1
                    or pid in NAVIGATE_ACTIVE_PIDS
                ):
                    continue
                try:
                    os.kill(pid, 9)
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass
    return killed


def _clean_chrome_profile_cache() -> int:
    cleaned = 0
    # W5: bound the whole sweep — a profile dir packed with Service Worker
    # storage used to rmtree for minutes (on the executor since the sync
    # extraction, on the LOOP before it). Whatever is left is caught next
    # cycle; this runs every CLEANUP_INTERVAL anyway.
    deadline = time.monotonic() + 20.0
    cache_dirs = [
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/Service Worker/CacheStorage",
        "Default/Service Worker/ScriptCache",
    ]
    for profile_root in glob.glob("/tmp/chrome-profiles/*/"):
        for cache_dir in cache_dirs:
            if time.monotonic() > deadline:
                logger.warning(
                    "cleanup: profile cache sweep hit its 20s budget (%d dirs cleaned)",
                    cleaned,
                )
                return cleaned
            full_path = os.path.join(profile_root, cache_dir)
            if os.path.isdir(full_path):
                try:
                    shutil.rmtree(full_path)
                    cleaned += 1
                except Exception:
                    pass
    return cleaned


class ProbeRequest(BaseModel):
    url: str
    render_js: bool = True
    timeout: int = Field(default=120, ge=10, le=300)
    start_method: Optional[str] = Field(default=None)
    country: Optional[str] = Field(default=None)


class AkamaiProbeRequest(BaseModel):
    url: str
    proxy_tier: str = Field(default="none")
    timeout: int = Field(default=120, ge=10, le=300)


class SingleProbeRequest(BaseModel):
    url: str
    method: str = Field(
        description=(
            "One of: direct_http, direct_http_datacenter, direct_http_residential, "
            "playwright_none, playwright_datacenter, playwright_residential, "
            "cloak_none, cloak_datacenter, cloak_residential. "
            "(uc_chrome_* are accepted as deprecated aliases for cloak_*.)"
        )
    )
    timeout: int = Field(default=60, ge=10, le=120)
    country: Optional[str] = Field(default=None)


class ScrapeRequest(BaseModel):
    # Stateless /scrape: the caller sends the scraper SOURCE (not a filesystem
    # path). browser_service stages it to a private /tmp dir, runs it, and
    # returns the output CONTENT in the response — so it needs no shared volume
    # and no File Master access.
    scraper_source: str
    scraper_name: str = "scraper.py"   # filename to stage as (for SCRIPT_DIR output naming)
    # Sibling files the scraper reads (input_urls.json, discovery_config.json,
    # etc.) — staged alongside the scraper in the same /tmp dir. Without these,
    # url_list scrapers fail ("No such file: input_urls.json").
    extra_files: Optional[dict[str, str]] = Field(default_factory=dict)
    args: Optional[list[str]] = Field(default_factory=list)
    timeout: int = Field(default=3600, ge=30, le=7200)
    env_overrides: Optional[dict[str, str]] = Field(default_factory=dict)
    # Cap run_scraper_script's Chrome-crash retries. Callers that only need a
    # crash/not-crash signal (the code_tester discovery probe) pass max_retries=1
    # so a slow discovery doesn't fan out to 3×timeout of orphaned subprocess
    # work after the /scrape wait_for already returned — those orphans share the
    # single Scraper Chrome (port 9223) and wedge subsequent scrapers (run_execution).
    max_retries: int = Field(default=3, ge=1, le=5)


class RenderRequest(BaseModel):
    url: str
    timeout: int = Field(default=120, ge=10, le=300)
    start_method: Optional[str] = Field(default=None)
    country: Optional[str] = Field(default=None)
    accept_language: Optional[str] = Field(default=None)


class NavigateAction(BaseModel):
    """A single action to perform after ``page.goto``. Applied in order.

    types: ``fill`` | ``select`` | ``click`` | ``wait`` | ``sleep`` | ``press``
    | ``evaluate``.
      - fill/select: ``selector`` + ``value``
      - click: ``selector``
      - wait: ``value`` = load state (domcontentloaded|load|networkidle)
      - sleep: ``value`` (ms, parsed as int) or ``timeout`` ms
      - press: ``selector`` (target, default body) + ``value`` (key, e.g. Enter)
      - evaluate: ``value`` = raw JS to ``page.evaluate``
    """

    type: str
    selector: Optional[str] = None
    value: Optional[str] = None
    timeout: Optional[int] = 10000  # ms


class NavigateRequest(BaseModel):
    url: str
    actions: list[NavigateAction] = Field(default_factory=list)
    extract: dict[str, str] = Field(default_factory=dict)  # name -> CSS selector
    method_hint: str = "auto"  # "auto" | "playwright" | "cloak"
    stealth: str = "auto"  # "auto" | "cloak" | "none"
    proxy_tier: str = "none"  # "none" | "datacenter" | "residential"
    country: Optional[str] = None
    timeout: int = Field(default=60, ge=5, le=180)  # seconds, per-call
    return_what: str = "all"  # "all" | "html" | "data" | "none"
    wait_until: str = "domcontentloaded"
    cookies: Optional[list[dict]] = None


MCP_CDP_PORT = int(os.environ.get("MCP_CDP_PORT", "9222"))
SCRAPER_CDP_PORT = int(os.environ.get("SCRAPER_CDP_PORT", "9223"))
CDP_FORWARD_MCP = int(os.environ.get("CDP_FORWARD_MCP", "9222"))
CDP_FORWARD_SCRAPER = int(os.environ.get("CDP_FORWARD_SCRAPER", "9223"))


async def _tcp_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def _start_cdp_proxy(public_port: int, internal_port: int, label: str):
    async def handle_client(client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", internal_port
            )
            header_data = await client_reader.read(65536)
            if header_data:
                patched = header_data.replace(
                    b"Host: browser_service:", b"Host: localhost:"
                ).replace(
                    b"Host: u-ecom-scraper-browser_service-1:", b"Host: localhost:"
                )
                if b"Host:" not in header_data and b"GET " in header_data:
                    first_line = header_data.split(b"\r\n")[0]
                    patched = header_data.replace(first_line, first_line, 1)
                    patched = b"Host: localhost\r\n" + patched
                upstream_writer.write(patched)
                await upstream_writer.drain()
            await asyncio.gather(
                _tcp_proxy(client_reader, upstream_writer),
                _tcp_proxy(upstream_reader, client_writer),
            )
        except Exception:
            pass
        finally:
            client_writer.close()

    server = await asyncio.start_server(handle_client, "0.0.0.0", public_port)
    logger.info(
        "CDP proxy: 0.0.0.0:%d -> 127.0.0.1:%d (%s)", public_port, internal_port, label
    )
    return server


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting browser_service...")
    startup_result = browser_pool.startup()
    if startup_result.get("errors"):
        logger.warning("Browser pool started with errors: %s", startup_result["errors"])
    else:
        logger.info("Browser pool started successfully")

    proxy_servers = []
    if MCP_CDP_PORT != CDP_FORWARD_MCP:
        s = await _start_cdp_proxy(CDP_FORWARD_MCP, MCP_CDP_PORT, "MCP")
        proxy_servers.append(s)
    if SCRAPER_CDP_PORT != CDP_FORWARD_SCRAPER:
        s = await _start_cdp_proxy(CDP_FORWARD_SCRAPER, SCRAPER_CDP_PORT, "Scraper")
        proxy_servers.append(s)

    await _start_mcp_process()

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    liveness_task = asyncio.create_task(_periodic_cdp_liveness())
    try:
        yield
    finally:
        cleanup_task.cancel()
        liveness_task.cancel()

    if mcp_process and mcp_process.poll() is None:
        logger.info("Stopping Playwright MCP (PID %d)...", mcp_process.pid)
        try:
            os.killpg(os.getpgid(mcp_process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            mcp_process.terminate()
        try:
            mcp_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(mcp_process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                mcp_process.kill()
    for s in proxy_servers:
        s.close()
    logger.info("Shutting down browser_service...")
    browser_pool.shutdown()


app = FastAPI(
    title="Browser Service",
    description="Unified browser automation service for ecommerce scraping",
    version="2.0.0",
    lifespan=lifespan,
)


def _cloak_info() -> dict:
    """CloakBrowser binary status (cheap — no browser launch).

    Used by ``/health`` to surface whether the stealth Chromium is installed.
    """
    try:
        from cloakbrowser import binary_info

        info = binary_info() or {}
        return {
            "available": bool(info.get("installed")),
            "version": info.get("version"),
            "platform": info.get("platform"),
            "binary_path": info.get("path") or info.get("binary_path"),
        }
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/health")
async def health():
    h = browser_pool.health()
    liveness = await asyncio.get_event_loop().run_in_executor(
        HEALTH_EXECUTOR, browser_pool.check_cdp_liveness
    )
    config = get_proxy_config()
    dc_available = bool(config.build_proxy_url("datacenter"))
    res_available = bool(config.build_proxy_url("residential"))
    # Check MCP process is alive (not just Chrome CDP port).
    mcp_pid = mcp_process.pid if mcp_process and mcp_process.poll() is None else None
    mcp_process_alive = mcp_pid is not None
    # Healthy requires ready + at least one CDP endpoint responding + MCP alive.
    cdp_ok = liveness.get("mcp_cdp_alive") or liveness.get("scraper_cdp_alive")
    status = "ok" if (h["ready"] and cdp_ok and mcp_process_alive) else "degraded"
    # /navigate slot accounting (independent of PROBE_LOCK / /scrape)
    nav_busy = min(_navigate_in_flight, NAVIGATE_MAX_CONCURRENT)
    nav_queued = max(0, _navigate_in_flight - NAVIGATE_MAX_CONCURRENT)
    return JSONResponse(
        {
            "status": status,
            **h,
            **liveness,
            "mcp_pid": mcp_pid,
            "mcp_process_alive": mcp_process_alive,
            "proxy_datacenter": "available" if dc_available else "not configured",
            "proxy_residential": "available" if res_available else "not configured",
            "cloak": _cloak_info(),
            "navigate_slots_busy": nav_busy,
            "navigate_slots_total": NAVIGATE_MAX_CONCURRENT,
            "navigate_queued": nav_queued,
            "uptime_seconds": time.time() - PROCESS_START,
        },
        status_code=200 if status == "ok" else 503,
    )


class RestartCdpRequest(BaseModel):
    label: str = Field("all", pattern="^(mcp|scraper|all)$")


@app.post("/restart-cdp")
async def restart_cdp(request: RestartCdpRequest):
    """Manually restart one or both Chrome instances without restarting the
    container. Useful when callers detect a hung CDP session that the
    background liveness loop hasn't caught yet.
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        RESTART_EXECUTOR, browser_pool.restart_chrome, request.label
    )
    if request.label in ("mcp", "all") and not result.get("errors"):
        await asyncio.sleep(2)
        mcp_ok = await _start_mcp_process()
        if not mcp_ok:
            result.setdefault("errors", []).append("mcp_restart_failed")
    status_code = 200 if not result.get("errors") else 500
    return JSONResponse(result, status_code=status_code)


@app.post("/probe")
async def probe(request: ProbeRequest):
    async with PROBE_LOCK:
        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    MISC_EXECUTOR,
                    lambda: run_probe(
                        url=request.url,
                        render_js=request.render_js,
                        timeout=request.timeout,
                        start_method=request.start_method,
                        country=request.country,
                    ),
                ),
                timeout=request.timeout + 60,
            )
            if result and result.get("needs_akamai_bypass"):
                logger.info(
                    "Akamai detected for %s, releasing probe lock and escalating",
                    request.url[:100],
                )
            return JSONResponse(content=result)
        except asyncio.TimeoutError:
            logger.error("Probe timed out for %s (lock released)", request.url[:200])
            return JSONResponse(
                status_code=504,
                content={"success": False, "error": "Probe timed out"},
            )
        except Exception as exc:
            logger.exception("Probe failed for %s", request.url[:200])
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": str(exc)[:500]},
            )


@app.post("/probe-single")
async def probe_single(request: SingleProbeRequest):
    from .probe import _try_direct_http, _try_playwright, _try_cloak
    from src.geo import detect_country as _detect_country

    method = request.method
    country = request.country or _detect_country(request.url)
    method_map = {
        "direct_http": lambda: _try_direct_http(
            request.url, min(request.timeout, 15), "none"
        ),
        "direct_http_datacenter": lambda: _try_direct_http(
            request.url, min(request.timeout, 15), "datacenter", country=country
        ),
        "direct_http_residential": lambda: _try_direct_http(
            request.url, min(request.timeout, 15), "residential", country=country
        ),
        "playwright_none": lambda: _try_playwright(
            request.url, "none", min(request.timeout, 25)
        ),
        "playwright_datacenter": lambda: _try_playwright(
            request.url, "datacenter", min(request.timeout, 35), country=country
        ),
        "playwright_residential": lambda: _try_playwright(
            request.url, "residential", min(request.timeout, 35), country=country
        ),
        "cloak_none": lambda: _try_cloak(
            request.url, "none", min(request.timeout, 45)
        ),
        "cloak_datacenter": lambda: _try_cloak(
            request.url, "datacenter", min(request.timeout, 45), country=country
        ),
        "cloak_residential": lambda: _try_cloak(
            request.url, "residential", min(request.timeout, 45), country=country
        ),
        # DEPRECATED aliases: ``uc_chrome`` was removed (consolidated onto cloak,
        # its documented successor). Kept so cached ProbeCache ``method`` values
        # naming ``uc_chrome_*`` still resolve on /probe-single instead of 400'ing.
        # The result payload reports ``cloak_*`` as the method that worked.
        "uc_chrome_none": lambda: _try_cloak(
            request.url, "none", min(request.timeout, 45)
        ),
        "uc_chrome_datacenter": lambda: _try_cloak(
            request.url, "datacenter", min(request.timeout, 45), country=country
        ),
        "uc_chrome_residential": lambda: _try_cloak(
            request.url, "residential", min(request.timeout, 45), country=country
        ),
    }

    if method not in method_map:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": f"Unknown method: {method}. Valid: {list(method_map.keys())}",
            },
        )

    try:
        loop = asyncio.get_event_loop()
        start = time.monotonic()
        result = await loop.run_in_executor(MISC_EXECUTOR, method_map[method])
        elapsed = round(time.monotonic() - start, 2)

        if result is None:
            return JSONResponse(
                content={
                    "success": False,
                    "method": method,
                    "proxy_tier": "none",
                    "status_code": 0,
                    "title": "",
                    "body_length": 0,
                    "needs_browser": True,
                    "blocked": True,
                    "jsonld": [],
                    "meta": {},
                    "selector_results": {},
                    "error": "Method returned no result",
                    "elapsed": elapsed,
                }
            )

        result["elapsed"] = elapsed
        return JSONResponse(content=result)

    except Exception as exc:
        logger.exception(
            "Single probe failed for %s method=%s", request.url[:80], method
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "method": method,
                "error": str(exc)[:500],
                "elapsed": 0,
            },
        )


@app.post("/probe-akamai")
async def probe_akamai(request: AkamaiProbeRequest):
    # DEPRECATED: /probe-akamai + src/akamai_bypass are superseded by the cloak
    # stealth path. Akamai detection in run_probe already routes to _try_cloak.
    # Kept (not deleted) because it has callers and needs A/B testing against
    # cloak before removal. See docs/browser-service-rework-plan.md.
    logger.warning(
        "/probe-akamai is deprecated — akamai detection now routes to cloak in "
        "run_probe; src/akamai_bypass is slated for removal pending A/B testing; "
        "see docs/browser-service-rework-plan.md"
    )
    from src.akamai_bypass.config import build_akamai_config_from_proxy_tier
    from src.akamai_bypass.orchestrator import AkamaiOrchestrator
    from src.page_analysis import (
        extract_jsonld,
        extract_meta_tags,
        extract_title,
        is_blocked,
    )

    async with AKAMAI_SEMAPHORE:
        try:
            cfg = build_akamai_config_from_proxy_tier(request.proxy_tier)
            cfg.headless = True
            orchestrator = AkamaiOrchestrator(cfg)

            result = await asyncio.wait_for(
                orchestrator.probe(request.url),
                timeout=request.timeout,
            )

            if result and result.get("html"):
                html = result["html"]
                blocked = is_blocked(html[:5000])
                jsonld = extract_jsonld(html)
                meta = extract_meta_tags(html)
                title = result.get("title", "") or extract_title(html)

                selector_results = "Skipped — Akamai bypass probe"
                has_content = len(html) > 5000 and not blocked

                if has_content:
                    try:
                        import lxml.html

                        tree = lxml.html.fromstring(html)
                        results = []
                        from src.page_analysis import COMMON_SELECTORS

                        for name, selector in COMMON_SELECTORS.items():
                            try:
                                elements = tree.cssselect(selector)
                                if not elements:
                                    results.append(f"  {name} ({selector}): NOT FOUND")
                                    continue
                                first_text = ""
                                for el in elements[:3]:
                                    text = (el.text_content() or "").strip()[:100]
                                    if text:
                                        first_text = text
                                        break
                                if first_text:
                                    results.append(
                                        f'  {name} ({selector}): "{first_text}" [found: {len(elements)}]'
                                    )
                                else:
                                    results.append(
                                        f"  {name} ({selector}): EMPTY [found: {len(elements)}]"
                                    )
                            except Exception as exc:
                                results.append(f"  {name} ({selector}): ERROR - {exc}")
                        selector_results = "\n".join(results)
                    except Exception as e:
                        logger.warning(
                            "Selector testing failed for Akamai probe: %s", e
                        )
                        selector_results = f"Selector test error: {e}"

                return JSONResponse(
                    content={
                        "success": has_content,
                        "method": result.get("method", "akamai_bypass"),
                        "proxy_tier": request.proxy_tier,
                        "status_code": 200,
                        "title": title[:200],
                        "body_length": len(html),
                        "needs_browser": True,
                        "blocked": blocked,
                        "jsonld": jsonld,
                        "meta": meta,
                        "selector_results": selector_results,
                        "error": ""
                        if has_content
                        else "Akamai bypass succeeded but content still blocked or empty",
                    }
                )

            return JSONResponse(
                content={
                    "success": False,
                    "method": "akamai_bypass",
                    "proxy_tier": request.proxy_tier,
                    "status_code": 0,
                    "title": "",
                    "body_length": 0,
                    "needs_browser": True,
                    "blocked": True,
                    "jsonld": [],
                    "meta": {},
                    "selector_results": {},
                    "error": "All Akamai bypass layers failed",
                }
            )

        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "success": False,
                    "error": f"Akamai probe timed out after {request.timeout}s",
                },
            )
        except Exception as exc:
            logger.exception("Akamai probe failed for %s", request.url[:200])
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": str(exc)[:500]},
            )


@app.post("/render")
async def render(request: RenderRequest):
    """Fetch a page and return the full HTML via the correct access method.

    Uses the same escalation chain as /probe but returns raw HTML content
    instead of metadata. Used by agents that need the full page DOM (e.g.
    navigation_explore for extracting category links, search forms).
    """
    async with PROBE_LOCK:
        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    MISC_EXECUTOR,
                    lambda: render_page(
                        url=request.url,
                        timeout=request.timeout,
                        start_method=request.start_method,
                        country=request.country,
                        accept_language=request.accept_language,
                    ),
                ),
                timeout=request.timeout + 60,
            )
            return JSONResponse(content=result)
        except asyncio.TimeoutError:
            logger.error("Render timed out for %s (lock released)", request.url[:200])
            return JSONResponse(
                status_code=504,
                content={"success": False, "html": "", "error": "Render timed out"},
            )
        except Exception as exc:
            logger.exception("Render failed for %s", request.url[:200])
            return JSONResponse(
                status_code=500,
                content={"success": False, "html": "", "error": str(exc)[:500]},
            )


@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    # DEPRECATED: /scrape + scraper_runner.py + the Scraper Chrome (port 9223)
    # are the ACTIVE execution path for legacy Playwright scrapers, so they are
    # STAGED for removal, not deleted. Migrate callers to /navigate. See
    # docs/browser-service-rework-plan.md.
    logger.warning(
        "/scrape is deprecated — migrate to /navigate; see docs/browser-service-rework-plan.md"
    )
    # Step 5 Phase A — observation: log every /scrape invocation so we can
    # track when the subprocess model is no longer needed and safely remove it.
    logger.info(
        "DEPRECATED /scrape invoked (stateless): scraper_name=%s args=%s source=%dB",
        request.scraper_name,
        getattr(request, "args", [])[:5],
        len(request.scraper_source or ""),
    )
    # W4 admission control: admit-or-429 (SCRAPE_MAX_QUEUE=0 — a queued
    # caller's timeout clock starts at admission, not arrival, so queueing
    # lies). Occupancy is read from SCRAPE_IN_FLIGHT: entries are popped by
    # _run_scrape_guarded only when the child is REAPED — the executor thread
    # outlives this response on wait_for timeout, so a response-lifetime
    # counter would under-count and hide real pool saturation.
    _now = time.monotonic()
    scrape_busy = sum(1 for dl in SCRAPE_IN_FLIGHT.values() if dl > _now)
    if scrape_busy >= SCRAPE_MAX_CONCURRENT + SCRAPE_MAX_QUEUE:
        logger.warning(
            "/scrape rejected (busy=%d/%d) — backpressure",
            scrape_busy, SCRAPE_MAX_CONCURRENT,
        )
        return _backpressure(
            429,
            f"scrape concurrency limit reached ({scrape_busy}/{SCRAPE_MAX_CONCURRENT})",
            15,
            busy=scrape_busy,
        )
    # Stateless staging: write the caller-supplied source to a private /tmp dir
    # (one per call — no cross-call collision), run it, capture output CONTENT,
    # then clean up. No shared filesystem, no File Master access.
    import uuid
    import shutil
    run_dir = os.path.join("/tmp", f"scrape_{uuid.uuid4().hex}")
    try:
        os.makedirs(run_dir, exist_ok=True)
        scraper_path = os.path.join(run_dir, request.scraper_name or "scraper.py")
        try:
            with open(scraper_path, "w", encoding="utf-8") as f:
                f.write(request.scraper_source or "")
            # Stage sibling files (input_urls.json, discovery_config.json, etc.)
            for fname, content in (request.extra_files or {}).items():
                safe = os.path.basename(fname)  # no path traversal
                if safe and safe != request.scraper_name:
                    with open(os.path.join(run_dir, safe), "w", encoding="utf-8") as ef:
                        ef.write(content)
        except OSError as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "returncode": -1,
                    "stderr": f"Failed to stage scraper source: {exc}",
                    "stdout": "",
                    "output_content": "",
                    "output_name": "",
                    "product_count": 0,
                    "duration": 0,
                },
            )
        try:
            loop = asyncio.get_event_loop()
            # F1: register kill-cycle protection keyed by the run dir's uuid,
            # with a deadline failsafe (timeout × retries + grace) so a wedged
            # executor thread can never permanently disable the orphan killer.
            # Released by _run_scrape_guarded only when the child is reaped —
            # NOT by this endpoint's finally (the executor thread outlives the
            # HTTP response on wait_for timeout).
            rid = os.path.basename(run_dir)
            SCRAPE_IN_FLIGHT[rid] = (
                time.monotonic()
                + request.timeout * max(1, request.max_retries)
                + SCRAPE_PROTECTION_GRACE_S
            )
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    SCRAPE_EXECUTOR,
                    functools.partial(
                        _run_scrape_guarded,
                        rid=rid,
                        scraper_path=scraper_path,
                        args=request.args,
                        timeout=request.timeout,
                        env_overrides=request.env_overrides,
                        max_retries=request.max_retries,
                    ),
                ),
                timeout=request.timeout + 120,
            )
            return JSONResponse(content=result)
        except asyncio.TimeoutError:
            logger.error("Scraper timed out for %s (lock released)", scraper_path)
            return JSONResponse(
                status_code=504,
                content={
                    "returncode": -1,
                "stderr": f"Timed out after {request.timeout + 120}s",
                "stdout": "",
                "output_content": "",
                "output_name": "",
                "product_count": 0,
                "duration": request.timeout + 120,
            },
        )
    except Exception as exc:
        logger.exception("Scrape failed for %s", scraper_path)
        return JSONResponse(
            status_code=500,
            content={
                "returncode": -1,
                "stderr": str(exc),
                "stdout": "",
                "output_content": "",
                "output_name": "",
                "product_count": 0,
                "duration": 0,
            },
        )
    finally:
        # Stateless: reap the staged /tmp dir (output content already captured
        # into the response). Best-effort — a racing subprocess on timeout may
        # hold a file open; /tmp is ephemeral anyway.
        import shutil as _shutil
        _shutil.rmtree(run_dir, ignore_errors=True)


# ── POST /navigate ───────────────────────────────────────────────────────
# Stateless per-page browser endpoint. Launches an ephemeral browser, runs a
# small action script, extracts requested selectors, returns HTML + data + the
# final (post-actions) URL. Independent of PROBE_LOCK and /scrape.


class _ChromeDeathError(Exception):
    """Raised when a Chrome/CDP crash is detected mid-navigate. Maps to 503."""


def _snapshot_chrome_pids() -> set[int]:
    """Best-effort snapshot of chrome PIDs (for orphan-killer coordination)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "chrome"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return {int(p) for p in result.stdout.split() if p.strip()}
    except Exception:
        pass
    return set()


def _apply_navigate_action(page, action: "NavigateAction") -> None:
    """Apply one NavigateAction to the page. Raises on error (caller stops)."""
    t = action.type
    sel = action.selector
    val = action.value
    ms = action.timeout if action.timeout is not None else 10000

    if t == "fill":
        if not sel:
            raise ValueError("fill action requires a selector")
        page.fill(sel, val or "", timeout=ms)
    elif t == "select":
        if not sel:
            raise ValueError("select action requires a selector")
        page.select_option(sel, val or "", timeout=ms)
    elif t == "click":
        if not sel:
            raise ValueError("click action requires a selector")
        page.click(sel, timeout=ms)
    elif t == "wait":
        state = val or "domcontentloaded"
        page.wait_for_load_state(state, timeout=ms)
    elif t == "sleep":
        try:
            duration = int(val) if val else ms
        except (TypeError, ValueError):
            duration = ms
        page.wait_for_timeout(duration)
    elif t == "press":
        target = sel or "body"
        page.press(target, val or "Enter", timeout=ms)
    elif t == "evaluate":
        if not val:
            raise ValueError("evaluate action requires a value (JS script)")
        page.evaluate(val)
    else:
        raise ValueError(f"Unknown action type: {t!r}")


def _run_navigate_sync(
    url: str,
    actions: list,
    extract: dict,
    method: str,
    proxy_tier: str,
    country: Optional[str],
    stealth: str,
    timeout: int,
    return_what: str,
    wait_until: str,
    cookies: Optional[list[dict]],
) -> dict:
    """Synchronous navigate worker (runs in the thread executor).

    Returns a NavigateResponse-shaped dict. Raises :class:`_ChromeDeathError`
    on Chrome crash (caller maps to 503 + retry_after).
    """
    from .probe import _classify_block, _extract_page_data, _launch_page
    from .scraper_runner import _is_chrome_death

    ctx = None
    session_pids: set[int] = set()
    errors: list[str] = []
    actions_run = 0

    try:
        # Snapshot chrome PIDs before launch so we can protect our new browser
        # from _kill_orphan_chrome. Best-effort; the in-flight counter is the
        # hard guarantee.
        before = _snapshot_chrome_pids()
        ctx = _launch_page(
            method=method,
            proxy_tier=proxy_tier,
            country=country,
            stealth=stealth,
            timeout=timeout,
        )
        page = ctx.page
        after = _snapshot_chrome_pids()
        session_pids = after - before
        if session_pids:
            _track_navigate_pids(session_pids)

        # Set cookies on the browser context (before goto, for session continuity)
        if cookies:
            try:
                page.context.add_cookies(cookies)
            except Exception as exc:
                errors.append(f"cookies: {type(exc).__name__}: {str(exc)[:200]}")

        # Navigate
        try:
            resp = page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            status_code = resp.status if resp else 0
        except Exception as exc:
            err_str = str(exc)
            if _is_chrome_death(err_str):
                raise _ChromeDeathError(err_str)
            raise

        # Execute actions in order. On per-action error: record + STOP further
        # actions, but still extract (the page may have useful partial state).
        for i, action in enumerate(actions[:MAX_NAVIGATE_ACTIONS]):
            try:
                _apply_navigate_action(page, action)
                actions_run += 1
            except Exception as exc:
                errors.append(
                    f"action[{i}] {action.type}: {type(exc).__name__}: {str(exc)[:200]}"
                )
                break

        # Settle for any late DOM/JS updates
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Extract (always captures html internally for classification)
        extracted = _extract_page_data(page, extract, return_what, MAX_NAVIGATE_HTML)

        # Capture post-action state
        try:
            final_url = page.url
        except Exception:
            final_url = url
        try:
            title = (page.title() or "")[:500]
        except Exception:
            title = ""
        try:
            final_cookies = page.context.cookies()
        except Exception:
            final_cookies = []

        # Classify on the captured html (always present internally)
        blocked_type = _classify_block(extracted["html"], status_code)

        # Only include html in the response when the caller asked for it
        include_html = return_what in ("all", "html")

        return {
            "success": True,
            "url": final_url,
            "status_code": status_code,
            "title": title,
            "html": extracted["html"] if include_html else "",
            "html_truncated": extracted["html_truncated"] if include_html else False,
            "data": extracted["data"],
            "blocked": blocked_type is not None,
            "blocked_type": blocked_type,
            "method_used": ctx.method,
            "stealth_used": ctx.stealth_used,
            "cookies": final_cookies,
            "actions_run": actions_run,
            "errors": errors,
            "error": None,
        }

    except _ChromeDeathError:
        raise
    except Exception as exc:
        err_str = str(exc)
        if _is_chrome_death(err_str):
            raise _ChromeDeathError(err_str)
        raise
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        # Remove our session PIDs AFTER the browser is closed (they're dead now,
        # so removing them can't strand another active navigate call).
        if session_pids:
            _untrack_navigate_pids(session_pids)


@app.post("/navigate")
async def navigate(request: NavigateRequest):
    """Launch an ephemeral browser, navigate, run actions, extract, tear down.

    Each call is an isolated unit: fresh browser, no CDP attach, no reuse of the
    persistent Scraper Chrome. Concurrency is bounded by NAVIGATE_SEMAPHORE;
    excess callers get 429 + retry_after. A Chrome crash returns 503 + retry_after.
    """
    global _navigate_in_flight

    # Validate stealth: a non-{auto,cloak,none} value (e.g. an unfilled
    # "{STEALTH}" placeholder that leaked through) used to fall through silently
    # to playwright (no cloak) and block anti-bot sites. Reject it loudly so the
    # bug surfaces at the first request instead of as a 0-item result.
    if request.stealth and request.stealth not in ("auto", "cloak", "none"):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": (
                    f"Unsupported stealth: {request.stealth!r}. "
                    "Use 'auto', 'cloak', or 'none'."
                ),
            },
        )

    # Resolve method_hint
    method = request.method_hint
    if method == "auto":
        method = "cloak" if request.stealth == "cloak" else "playwright"
    if method not in ("playwright", "cloak"):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": (
                    f"Unsupported method_hint: {request.method_hint!r}. "
                    "Use 'auto', 'playwright', or 'cloak'."
                ),
            },
        )

    if request.return_what not in ("all", "html", "data", "none"):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": (
                    f"Invalid return_what: {request.return_what!r}. "
                    "Use 'all', 'html', 'data', or 'none'."
                ),
            },
        )

    # W4 memory gate: refuse the fork BEFORE the browser launch when the
    # cgroup is already near its ceiling — Errno 11 fork failures under
    # memory pressure were the root cause of the prod 502 windows, and a
    # rejected launch is far cheaper than a half-launched browser. Falls
    # open when the ratio can't be read (None).
    if NAVIGATE_MEMORY_GATE_RATIO > 0:
        mem_ratio = _cgroup_memory_ratio()
        if mem_ratio is not None and mem_ratio >= NAVIGATE_MEMORY_GATE_RATIO:
            logger.warning(
                "navigate: memory gate tripped (ratio=%.2f ≥ %.2f) for %s",
                mem_ratio, NAVIGATE_MEMORY_GATE_RATIO, request.url[:100],
            )
            return _backpressure(
                429,
                (
                    f"memory pressure ({mem_ratio:.0%} of cgroup limit) — "
                    "refusing new browser launch"
                ),
                30,
                error_class="memory_pressure",
                mem_ratio=round(mem_ratio, 3),
            )

    # Queue-full backpressure (active + queued)
    if _navigate_in_flight >= NAVIGATE_MAX_CONCURRENT + NAVIGATE_MAX_QUEUE:
        return _backpressure(
            429, "navigate concurrency limit reached", 15,
            navigate_in_flight=_navigate_in_flight,
        )

    _navigate_in_flight += 1
    start = time.monotonic()
    try:
        async with NAVIGATE_SEMAPHORE:
            loop = asyncio.get_event_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        NAVIGATE_EXECUTOR,
                        _run_navigate_sync,
                        request.url,
                        request.actions,
                        request.extract,
                        method,
                        request.proxy_tier,
                        request.country,
                        request.stealth,
                        request.timeout,
                        request.return_what,
                        request.wait_until,
                        request.cookies,
                    ),
                    timeout=request.timeout + 30,
                )
                result["elapsed"] = round(time.monotonic() - start, 2)
                return JSONResponse(content=result)
            except _ChromeDeathError as exc:
                logger.warning(
                    "navigate: Chrome crash for %s: %s",
                    request.url[:100],
                    str(exc)[:200],
                )
                return _backpressure(
                    503,
                    "browser crash during navigate",
                    15,
                    elapsed=round(time.monotonic() - start, 2),
                    error_class="chrome_crash",
                )
            except asyncio.TimeoutError:
                logger.warning("navigate: timed out for %s", request.url[:200])
                return JSONResponse(
                    status_code=408,
                    content={
                        "success": False,
                        "error": f"navigate timed out after {request.timeout + 30}s",
                        "elapsed": round(time.monotonic() - start, 2),
                    },
                )
            except Exception as exc:
                logger.exception("navigate: failed for %s", request.url[:200])
                # If the exception looks like a Chrome death we missed, still 503
                from .scraper_runner import _is_chrome_death

                if _is_chrome_death(str(exc)):
                    return _backpressure(
                        503,
                        "browser crash during navigate",
                        15,
                        elapsed=round(time.monotonic() - start, 2),
                        error_class="chrome_crash",
                    )
                return JSONResponse(
                    status_code=502,
                    content={
                        "success": False,
                        "error": str(exc)[:500],
                        "elapsed": round(time.monotonic() - start, 2),
                    },
                )
    finally:
        _navigate_in_flight -= 1
