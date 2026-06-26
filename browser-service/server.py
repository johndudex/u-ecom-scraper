import asyncio
import glob
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
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
CDP_LIVENESS_INTERVAL = 60
CDP_MAX_CONSECUTIVE_FAILURES = 2

PERSISTENT_CHROME_PIDS: set[int] = set()


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
                None, browser_pool.check_cdp_liveness
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
                                None, browser_pool.restart_chrome, label
                            )
                            logger.info("CDP auto-restart %s result: %s", label, res)
                            # reset counter on successful restart (errors list empty)
                            if not res.get("errors"):
                                failures[key] = 0
                        except Exception:
                            logger.exception("CDP auto-restart %s raised", label)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CDP liveness loop error")


async def _cleanup_chrome_artifacts():
    _collect_persistent_pids()
    killed = _kill_orphan_chrome()
    cleaned = _clean_chrome_profile_cache()
    if killed or cleaned:
        logger.info(
            "Cleanup: killed %d orphan Chrome processes, cleaned %d profile dirs",
            killed,
            cleaned,
        )


def _collect_persistent_pids():
    PERSISTENT_CHROME_PIDS.clear()
    try:
        h = browser_pool.health()
        for key in ("mcp_pid", "scraper_pid"):
            pid = h.get(key)
            if pid:
                PERSISTENT_CHROME_PIDS.add(pid)
    except Exception:
        pass


def _kill_orphan_chrome() -> int:
    killed = 0
    try:
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
                if pid in PERSISTENT_CHROME_PIDS or pid == 1:
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
    cache_dirs = [
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/Service Worker/CacheStorage",
        "Default/Service Worker/ScriptCache",
    ]
    for profile_root in glob.glob("/tmp/chrome-profiles/*/"):
        for cache_dir in cache_dirs:
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
        description="One of: direct_http, playwright_none, playwright_datacenter, playwright_residential, uc_chrome_none, uc_chrome_datacenter, uc_chrome_residential"
    )
    timeout: int = Field(default=60, ge=10, le=120)
    country: Optional[str] = Field(default=None)


class ScrapeRequest(BaseModel):
    scraper_path: str
    args: Optional[list[str]] = Field(default_factory=list)
    timeout: int = Field(default=3600, ge=30, le=7200)
    env_overrides: Optional[dict[str, str]] = Field(default_factory=dict)


class RenderRequest(BaseModel):
    url: str
    timeout: int = Field(default=120, ge=10, le=300)
    start_method: Optional[str] = Field(default=None)
    country: Optional[str] = Field(default=None)
    accept_language: Optional[str] = Field(default=None)


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
                    b"Host: browser-service:", b"Host: localhost:"
                ).replace(
                    b"Host: u-ecom-scraper-browser-service-1:", b"Host: localhost:"
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
    logger.info("Starting browser-service...")
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

    mcp_process = None
    try:
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
        ]
        logger.info("Starting Playwright MCP: %s", " ".join(mcp_cmd))
        mcp_log = open("/tmp/mcp-stdout.log", "w")
        mcp_process = subprocess.Popen(
            mcp_cmd,
            stdout=mcp_log,
            stderr=mcp_log,
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
        else:
            logger.info(
                "Playwright MCP started (PID %d) on 0.0.0.0:8111 -> CDP 127.0.0.1:%s",
                mcp_process.pid,
                mcp_internal_port,
            )
    except Exception as e:
        logger.error("Failed to start Playwright MCP: %s", e)
        mcp_process = None

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    liveness_task = asyncio.create_task(_periodic_cdp_liveness())
    try:
        yield
    finally:
        cleanup_task.cancel()
        liveness_task.cancel()

    if mcp_process and mcp_process.poll() is None:
        logger.info("Stopping Playwright MCP (PID %d)...", mcp_process.pid)
        mcp_process.terminate()
        try:
            mcp_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mcp_process.kill()
    for s in proxy_servers:
        s.close()
    logger.info("Shutting down browser-service...")
    browser_pool.shutdown()


app = FastAPI(
    title="Browser Service",
    description="Unified browser automation service for ecommerce scraping",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    h = browser_pool.health()
    liveness = await asyncio.get_event_loop().run_in_executor(
        None, browser_pool.check_cdp_liveness
    )
    config = get_proxy_config()
    dc_available = bool(config.build_proxy_url("datacenter"))
    res_available = bool(config.build_proxy_url("residential"))
    # Healthy requires ready + at least one CDP endpoint responding.
    cdp_ok = liveness.get("mcp_cdp_alive") or liveness.get("scraper_cdp_alive")
    status = "ok" if (h["ready"] and cdp_ok) else "degraded"
    return JSONResponse(
        {
            "status": status,
            **h,
            **liveness,
            "proxy_datacenter": "available" if dc_available else "not configured",
            "proxy_residential": "available" if res_available else "not configured",
            "uptime_seconds": time.monotonic(),
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
        None, browser_pool.restart_chrome, request.label
    )
    status_code = 200 if not result.get("errors") else 500
    return JSONResponse(result, status_code=status_code)


@app.post("/probe")
async def probe(request: ProbeRequest):
    async with PROBE_LOCK:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: run_probe(
                    url=request.url,
                    render_js=request.render_js,
                    timeout=request.timeout,
                    start_method=request.start_method,
                    country=request.country,
                ),
            )
            if result and result.get("needs_akamai_bypass"):
                logger.info(
                    "Akamai detected for %s, releasing probe lock and escalating",
                    request.url[:100],
                )
            return JSONResponse(content=result)
        except Exception as exc:
            logger.exception("Probe failed for %s", request.url[:200])
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": str(exc)[:500]},
            )


@app.post("/probe-single")
async def probe_single(request: SingleProbeRequest):
    from .probe import _try_direct_http, _try_playwright, _try_uc_chrome
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
        "uc_chrome_none": lambda: _try_uc_chrome(
            request.url, "none", min(request.timeout, 40)
        ),
        "uc_chrome_datacenter": lambda: _try_uc_chrome(
            request.url, "datacenter", min(request.timeout, 40), country=country
        ),
        "uc_chrome_residential": lambda: _try_uc_chrome(
            request.url, "residential", min(request.timeout, 40), country=country
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
        result = await loop.run_in_executor(None, method_map[method])
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
            result = await loop.run_in_executor(
                None,
                lambda: render_page(
                    url=request.url,
                    timeout=request.timeout,
                    start_method=request.start_method,
                    country=request.country,
                    accept_language=request.accept_language,
                ),
            )
            return JSONResponse(content=result)
        except Exception as exc:
            logger.exception("Render failed for %s", request.url[:200])
            return JSONResponse(
                status_code=500,
                content={"success": False, "html": "", "error": str(exc)[:500]},
            )


@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    async with PROBE_LOCK:
        if not os.path.isfile(request.scraper_path):
            return JSONResponse(
                status_code=404,
                content={
                    "returncode": -1,
                    "stderr": f"Scraper not found: {request.scraper_path}",
                    "stdout": "",
                    "output_file": "",
                    "product_count": 0,
                    "duration": 0,
                },
            )
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: run_scraper_script(
                    scraper_path=request.scraper_path,
                    args=request.args,
                    timeout=request.timeout,
                    env_overrides=request.env_overrides,
                ),
            )
            return JSONResponse(content=result)
        except Exception as exc:
            logger.exception("Scrape failed for %s", request.scraper_path)
            return JSONResponse(
                status_code=500,
                content={
                    "returncode": -1,
                    "stderr": str(exc),
                    "stdout": "",
                    "output_file": "",
                    "product_count": 0,
                    "duration": 0,
                },
            )


@app.get("/cdp-endpoint")
async def cdp_endpoint():
    return {
        "mcp": f"http://127.0.0.1:{browser_pool.health().get('mcp_cdp_port', 9222)}",
        "scraper": f"http://127.0.0.1:{browser_pool.health().get('scraper_cdp_port', 9223)}",
    }
