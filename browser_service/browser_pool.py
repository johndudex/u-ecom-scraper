import logging
import os
import signal
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

DISPLAY = os.environ.get("DISPLAY", ":98")
MCP_CDP_PORT = int(os.environ.get("MCP_CDP_PORT", "9222"))
SCRAPER_CDP_PORT = int(os.environ.get("SCRAPER_CDP_PORT", "9223"))
XVFB_RESOLUTION = os.environ.get("XVFB_RESOLUTION", "1920x1080x24")
STARTUP_TIMEOUT = int(os.environ.get("STARTUP_TIMEOUT", "45"))
CHROME_USER_DATA_DIR = "/tmp/chrome-profiles"
# W6/W9: when set, the Scraper Chrome (9223) is NOT started at boot — the
# /scrape path launches it on first use (ensure_scraper_chrome). /health
# treats a deliberately-unstarted Chrome as healthy (scraper_not_required) —
# without that, lazy + the stricter AND = 503 from boot → the compose
# healthcheck fails → depends_on service_healthy blocks django/celery from
# ever starting. Default ON (the persistent Scraper Chrome serves only the
# deprecated /scrape path; ~250-400MB of idle RSS for nothing); set
# SCRAPER_CHROME_LAZY=0 via env to restore eager start without a rebuild.
SCRAPER_CHROME_LAZY = os.environ.get("SCRAPER_CHROME_LAZY", "1") == "1"
# W9 headless mode — scoped to the MCP Chrome ONLY (the Scraper Chrome is
# deleted in Phase D of the rework plan; headless+UA polish there is sunk
# cost — its guards get fixed as part of lazy anyway). Skips Xvfb, appends
# --headless=new, drops the --display flag and the DISPLAY env. The MCP
# Chrome already pins an explicit user-agent (below); without that override
# headless advertises "HeadlessChrome" and gets flagged.
MCP_HEADLESS = os.environ.get("CHROME_HEADLESS", "0") == "1"
# B1-6: a promoted (resident) Scraper Chrome with no work for this long is
# stopped again by the maintenance cycle — it relaunches lazily on the next
# /scrape, so recycling is invisible to callers but releases its RSS floor.
SCRAPER_RECYCLE_IDLE_S = float(os.environ.get("SCRAPER_RECYCLE_IDLE_S", "1800"))

# B1-1: both resident Chromes used to spawn with stdout=PIPE and NO reader —
# the 64KB pipe fills, Chrome blocks on its next log write (its CDP IO thread
# among them) and the instance presents as "process alive, CDP dead": the prod
# wedge signature (mcp_cdp_alive=false for 8+h while mcp_process=true). They
# now drain to a rotated file — the pipe never exists, Chrome never blocks,
# and the log stays inspectable. Same one-generation rotation shape as
# server.py's MCP-node log.
CHROME_LOG_MAX_BYTES = 10 * 1024 * 1024


def _chrome_log_path(label: str) -> str:
    return f"/tmp/chrome-{label}-stdout.log"


def _open_chrome_log(label: str):
    """Open the append-mode drain file for a persistent Chrome's stdout,
    rotating an oversized log aside first. Returns DEVNULL if /tmp refuses —
    a logging outage must never block the launch itself."""
    path = _chrome_log_path(label)
    try:
        if os.path.exists(path) and os.path.getsize(path) > CHROME_LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        return open(path, "ab")
    except OSError:
        return subprocess.DEVNULL


def _tail_chrome_log(label: str, n: int = 500) -> str:
    """Last ``n`` chars of a Chrome's drain file (for exit-immediately errors)."""
    path = _chrome_log_path(label)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 2000))
            return fh.read().decode("utf-8", "replace")[-n:]
    except OSError:
        return ""


class BrowserPool:
    def __init__(self):
        self._xvfb_proc: Optional[subprocess.Popen] = None
        self._mcp_chrome_proc: Optional[subprocess.Popen] = None
        self._scraper_chrome_proc: Optional[subprocess.Popen] = None
        # W6: distinguishes "scraper Chrome was never launched" (lazy_idle) from
        # "was launched and died" (down). Set by any launch attempt — boot or
        # W9's lazy ensure — so a failed launch is never mistaken for lazy.
        self._scraper_chrome_started = False
        self._ready = False
        # Serializes restart_chrome across concurrent callers (the CDP liveness
        # loop, /restart-cdp, and the scraper retry path can all call it). Without
        # this, two callers racing terminate()+restart() on the same Chrome
        # produce "NoneType" / "Opening in existing browser session" 500s and
        # corrupt the singleton state. RLock (not Lock): W9's
        # restart_chrome→ensure_scraper_chrome nests a second acquire on the
        # SAME thread — a plain Lock would self-deadlock there.
        self._restart_lock = threading.RLock()

    def startup(self) -> dict:
        errors = []

        # W9: Xvfb is required only when actually headed. CHROME_HEADLESS=1
        # boots with no Xvfb at all; an empty DISPLAY without the flag still
        # errors loudly below (the old silent "Railway mode" log line was
        # aspirational — the guards bailed and killed the whole pool).
        if DISPLAY and not MCP_HEADLESS:  # local compose: Xvfb + headed Chrome
            self._start_xvfb(errors)
        else:
            logger.info(
                "Xvfb skipped (%s) — Chrome must run with --headless=new",
                "CHROME_HEADLESS=1" if MCP_HEADLESS else "DISPLAY empty",
            )
        self._start_mcp_chrome(errors)
        if SCRAPER_CHROME_LAZY:
            # W9: the Scraper Chrome serves only the deprecated /scrape path —
            # don't pay ~250-400MB of idle RSS at boot; ensure_scraper_chrome()
            # launches it on first use.
            logger.info(
                "SCRAPER_CHROME_LAZY=1 — Scraper Chrome not started at boot "
                "(launched on first /scrape via ensure_scraper_chrome)"
            )
        else:
            self._start_scraper_chrome(errors)

        if not errors:
            self._ready = True
            logger.info(
                "Browser pool ready: Xvfb=%s, MCP Chrome=:%d, scraper_chrome_state=%s",
                DISPLAY,
                MCP_CDP_PORT,
                self.scraper_chrome_state(),
            )
        else:
            logger.error("Browser pool startup errors: %s", errors)

        return {
            "xvfb_running": self._xvfb_proc is not None,
            "mcp_chrome_running": self._mcp_chrome_proc is not None,
            "scraper_chrome_running": self._scraper_chrome_proc is not None,
            "errors": errors,
        }

    def health(self) -> dict:
        # proc-is-None checks come BEFORE .poll() everywhere below — None is
        # precisely the lazy initial state post-W9, and None.poll() raises.
        mcp_alive = (
            self._mcp_chrome_proc is not None and self._mcp_chrome_proc.poll() is None
        )
        scraper_alive = (
            self._scraper_chrome_proc is not None
            and self._scraper_chrome_proc.poll() is None
        )
        return {
            "ready": self._ready,
            "xvfb_running": self._xvfb_proc is not None
            and self._xvfb_proc.poll() is None,
            "mcp_chrome_running": mcp_alive,
            "scraper_chrome_running": scraper_alive,
            "scraper_chrome_state": self.scraper_chrome_state(),
            "mcp_cdp_port": MCP_CDP_PORT,
            "scraper_cdp_port": SCRAPER_CDP_PORT,
            "display": DISPLAY,
            "mcp_pid": self._mcp_chrome_proc.pid if self._mcp_chrome_proc else None,
            "scraper_pid": self._scraper_chrome_proc.pid
            if self._scraper_chrome_proc
            else None,
        }

    def scraper_chrome_state(self) -> str:
        """``"up" | "down" | "lazy_idle"`` (W6).

        Lets /health consumers distinguish a deliberately-unstarted Chrome
        (SCRAPER_CHROME_LAZY, never launched) from a dead one — the two look
        identical (proc is None) to ``health()``.
        """
        proc = self._scraper_chrome_proc
        if proc is not None and proc.poll() is None:
            return "up"
        if self._scraper_chrome_started:
            return "down"
        return "lazy_idle"

    def scraper_not_required(self) -> bool:
        """True while the lazy Scraper Chrome has not yet been launched.

        Feeds /health's lazy-aware AND: ``mcp_cdp_alive AND (scraper_cdp_alive
        OR scraper_not_required())``. False whenever LAZY is off (today's
        always-started behavior) or the Chrome has been launched at least once
        (then its CDP liveness is required, as before).
        """
        return bool(SCRAPER_CHROME_LAZY) and self.scraper_chrome_state() == "lazy_idle"

    def scraper_chrome_required(self) -> bool:
        """The W9 predicate: False while LAZY=1 and never ensured.

        One predicate drives every consumer — check_cdp_liveness skips the
        scraper leg, the liveness loop never auto-restarts a never-started
        Chrome, W6's AND falls through, and /scrape's ensure launches on
        demand.
        """
        return not self.scraper_not_required()

    def ensure_scraper_chrome(self) -> bool:
        """Launch the Scraper Chrome on first use (W9 lazy start).

        Idempotent and lock-guarded: safe to call from every /scrape
        (executor) task — the common case (already up) is a µs state check
        that never touches the lock. Uses the restart lock so a concurrent
        restart/ensure can't race two launches; RLock because restart_chrome
        legitimately nests this call.
        """
        if self.scraper_chrome_state() == "up":
            return True
        with self._restart_lock:
            if self.scraper_chrome_state() == "up":  # lost the race — done
                return True
            errors: list = []
            self._start_scraper_chrome(errors)
            if errors:
                logger.error("ensure_scraper_chrome failed: %s", errors)
                return False
            logger.info("ensure_scraper_chrome: Scraper Chrome launched (lazy start)")
            return True

    def stop_scraper_chrome(self) -> None:
        """Tear down the resident Scraper Chrome and return it to the lazy
        state (B1-6 idle recycling — the maintenance cycle calls this once the
        Chrome has sat idle past SCRAPER_RECYCLE_IDLE_S).

        Resets ``_scraper_chrome_started`` so ``scraper_chrome_state()`` reads
        ``lazy_idle`` again (not ``down``): /health stays green and the
        liveness loop keeps skipping a deliberately-unstarted Chrome.
        ``ensure_scraper_chrome()`` relaunches on the next /scrape, so
        recycling is invisible to callers.
        """
        with self._restart_lock:
            proc = self._scraper_chrome_proc
            if proc and proc.poll() is None:
                logger.info("stop_scraper_chrome: terminating PID %d", proc.pid)
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(proc)
                except Exception:
                    pass
            self._scraper_chrome_proc = None
            self._scraper_chrome_started = False
            logger.info("Scraper Chrome stopped (returned to lazy_idle)")

    def xvfb_alive(self) -> bool:
        """True while the Xvfb process (if one was started) is still running."""
        proc = self._xvfb_proc
        return proc is not None and proc.poll() is None

    def check_cdp_liveness(self) -> dict:
        """Actually probe CDP endpoints via HTTP (process alive != CDP responsive).

        Returns dict with ``mcp_cdp_alive`` / ``scraper_cdp_alive`` booleans and
        the response times in ms (``-1`` on failure).

        W9: the scraper leg is SKIPPED while the lazy Chrome has never been
        launched — probing it guarantees 3 consecutive failures ~45s after
        every boot, and the auto-restart would launch the Chrome the lazy
        start just skipped (the "saved" 250-400MB bought back inside a
        minute, from inside a blocking restart-lock hold). Reports
        ``scraper_cdp_alive: None`` (not-applicable) in that state.
        """
        import httpx

        def _probe(port: int) -> tuple[bool, int]:
            try:
                t0 = time.monotonic()
                resp = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                return (resp.status_code == 200, elapsed_ms)
            except Exception:
                return (False, -1)

        mcp_ok, mcp_ms = _probe(MCP_CDP_PORT)
        if not self.scraper_chrome_required():
            return {
                "mcp_cdp_alive": mcp_ok,
                "scraper_cdp_alive": None,
                "mcp_cdp_latency_ms": mcp_ms,
                "scraper_cdp_latency_ms": None,
                "scraper_chrome_state": "lazy_idle",
            }
        scraper_ok, scraper_ms = _probe(SCRAPER_CDP_PORT)
        return {
            "mcp_cdp_alive": mcp_ok,
            "scraper_cdp_alive": scraper_ok,
            "mcp_cdp_latency_ms": mcp_ms,
            "scraper_cdp_latency_ms": scraper_ms,
        }

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """Escalate a wedged pool process to SIGKILL of its whole GROUP.

        Pool children are spawned with ``start_new_session=True`` (their own
        session/group), so killpg takes the entire Chrome/Xvfb tree — including
        renderer/GPU/utility children that ``proc.kill()`` alone would orphan.
        Falls back to per-PID kill if the group is already gone.
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError as exc:
                logger.debug("fallback kill failed for pid %s: %s", proc.pid, exc)

    def restart_chrome(self, label: str = "all") -> dict:
        """Restart one or both Chrome instances without killing Xvfb or the
        FastAPI server.

        Args:
            label: ``"mcp"``, ``"scraper"``, or ``"all"``.

        Returns a summary dict describing the action taken.
        """
        logger.warning("restart_chrome(label=%s) invoked — recovering CDP", label)

        result: dict = {"label": label, "restarted": [], "errors": []}
        errors: list = result["errors"]

        def _do_restart(proc_attr: str, starter_name: str, display_label: str):
            proc = getattr(self, proc_attr)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(proc)
                except Exception:
                    pass
            setattr(self, proc_attr, None)
            # small grace period for socket cleanup
            time.sleep(1)
            starter = getattr(self, starter_name)
            starter(errors)
            if not errors or display_label not in " ".join(errors):
                result["restarted"].append(display_label.lower())

        # Hold the restart lock for the whole op so concurrent callers can't
        # tear down / relaunch Chrome simultaneously. (Blocking acquire — restart
        # is rare and the multi-second Chrome startup already dominates latency.
        # RLock: the lazy branch below nests ensure_scraper_chrome, which
        # acquires the same lock on this thread.)
        with self._restart_lock:
            # Always run restarts off the event loop thread so async callers
            # (FastAPI handlers) don't block on the multi-second Chrome startup.
            if label in ("mcp", "all"):
                _do_restart("_mcp_chrome_proc", "_start_mcp_chrome", "MCP")
            if label in ("scraper", "all"):
                if self.scraper_chrome_state() == "lazy_idle":
                    # W9: nothing to tear down — an explicit restart of a
                    # never-started lazy Chrome is operator intent to bring it
                    # up; ensure (not force-restart).
                    if self.ensure_scraper_chrome():
                        result["restarted"].append("scraper")
                else:
                    _do_restart("_scraper_chrome_proc", "_start_scraper_chrome", "Scraper")

            if not result["errors"]:
                logger.info(
                    "restart_chrome(%s) completed successfully: %s",
                    label,
                    result["restarted"],
                )
            else:
                logger.error(
                    "restart_chrome(%s) finished with errors: %s", label, result["errors"]
                )

            return result

    def shutdown(self):
        for proc in [self._scraper_chrome_proc, self._mcp_chrome_proc, self._xvfb_proc]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._kill_process_tree(proc)
                except Exception:
                    pass
        logger.info("Browser pool shut down")

    def _start_xvfb(self, errors: list):
        try:
            lock_file = f"/tmp/.X{DISPLAY.strip(':')}-lock"
            if os.path.exists(lock_file):
                os.remove(lock_file)
            for d in [f"{CHROME_USER_DATA_DIR}/mcp", f"{CHROME_USER_DATA_DIR}/scraper"]:
                os.makedirs(d, exist_ok=True)

            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", DISPLAY, "-screen", "0", XVFB_RESOLUTION],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(1)
            if self._xvfb_proc.poll() is not None:
                errors.append(
                    f"Xvfb exited immediately with code {self._xvfb_proc.returncode}"
                )
                self._xvfb_proc = None
        except FileNotFoundError:
            errors.append("Xvfb binary not found")
        except Exception as e:
            errors.append(f"Xvfb failed: {e}")

    def _start_mcp_chrome(self, errors: list):
        # W9: Xvfb is required only when headed. In headless mode (no Xvfb by
        # design) this guard must NOT bail — the old unconditional check made
        # "Railway mode" aspirational: clearing DISPLAY killed the whole pool.
        if not self._xvfb_proc and not MCP_HEADLESS:
            errors.append("Skipping MCP Chrome — no Xvfb and CHROME_HEADLESS unset")
            return
        try:
            args = [
                "google-chrome-stable",
                f"--remote-debugging-port={MCP_CDP_PORT}",
                "--remote-debugging-address=0.0.0.0",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-component-extensions-with-background-pages",
                "--disable-features=AutomationControlled,TranslateUI",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-save-password-bubble",
                "--disable-sync",
                "--disable-translate",
                "--disable-client-side-phishing-detection",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-background-networking",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1920,1080",
                f"--user-data-dir={CHROME_USER_DATA_DIR}/mcp",
                "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            ]
            mcp_headless = MCP_HEADLESS or not DISPLAY
            if mcp_headless:
                args.append("--headless=new")
            else:
                args.append(f"--display={DISPLAY}")
            # B1-1: drain stdout to a rotated file, never PIPE — an unread
            # PIPE fills at 64KB and blocks Chrome's CDP thread (the wedge).
            # The parent-side handle closes right after spawn: the child holds
            # its own dup, and nothing here ever reads the handle.
            _log_fh = _open_chrome_log("mcp")
            try:
                self._mcp_chrome_proc = subprocess.Popen(
                    args,
                    stdout=_log_fh,
                    stderr=subprocess.STDOUT,
                    # headless: no --display flag above and NO DISPLAY in the env —
                    # a stray empty DISPLAY var pointing at no X server used to leak in
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if not (mcp_headless and k == "DISPLAY")
                    },
                    start_new_session=True,
                )
            finally:
                if _log_fh is not subprocess.DEVNULL:
                    _log_fh.close()
            time.sleep(3)
            if self._mcp_chrome_proc.poll() is not None:
                errors.append(
                    f"MCP Chrome exited immediately with code {self._mcp_chrome_proc.returncode}: "
                    f"{_tail_chrome_log('mcp')}"
                )
                self._mcp_chrome_proc = None
            else:
                self._wait_for_cdp(MCP_CDP_PORT, errors, "MCP Chrome")
        except Exception as e:
            errors.append(f"MCP Chrome failed: {e}")

    def _start_scraper_chrome(self, errors: list):
        # DEPRECATED: this Scraper Chrome instance (port 9223) exists only to
        # serve the /scrape subprocess execution path for legacy Playwright
        # scrapers. It is STAGED for removal alongside /scrape + scraper_runner
        # once callers migrate to /navigate (which launches its own ephemeral
        # browsers and does not need this persistent Chrome). Leave running for
        # now. See docs/browser-service-rework-plan.md.
        self._scraper_chrome_started = True  # any attempt: down, never lazy_idle
        # W9: same guard rule as the MCP Chrome — Xvfb required only when
        # headed. (In a CHROME_HEADLESS deployment the launcher below will
        # still fail its CDP wait, honestly: headless mode is a /navigate-
        # only deployment; the Scraper Chrome is Phase-D sunk cost.)
        if not self._xvfb_proc and not MCP_HEADLESS:
            errors.append("Skipping Scraper Chrome — no Xvfb and CHROME_HEADLESS unset")
            return
        try:
            args = [
                "google-chrome-stable",
                f"--remote-debugging-port={SCRAPER_CDP_PORT}",
                "--remote-debugging-address=0.0.0.0",
                "--remote-allow-origins=*",
                f"--display={DISPLAY}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled,TranslateUI",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-background-networking",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1920,1080",
                f"--user-data-dir={CHROME_USER_DATA_DIR}/scraper",
            ]
            if not DISPLAY:
                args.append("--headless=new")
            # B1-1: same pipe-wedge fix as the MCP Chrome — drain to file.
            _log_fh = _open_chrome_log("scraper")
            try:
                self._scraper_chrome_proc = subprocess.Popen(
                    args,
                    stdout=_log_fh,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "DISPLAY": DISPLAY},
                    start_new_session=True,
                )
            finally:
                if _log_fh is not subprocess.DEVNULL:
                    _log_fh.close()
            time.sleep(3)
            if self._scraper_chrome_proc.poll() is not None:
                errors.append(
                    f"Scraper Chrome exited immediately with code {self._scraper_chrome_proc.returncode}: "
                    f"{_tail_chrome_log('scraper')}"
                )
                self._scraper_chrome_proc = None
            else:
                self._wait_for_cdp(SCRAPER_CDP_PORT, errors, "Scraper Chrome")
        except Exception as e:
            errors.append(f"Scraper Chrome failed: {e}")

    def _wait_for_cdp(self, port: int, errors: list, label: str):
        import httpx

        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(2)
        errors.append(f"{label} CDP not ready on port {port} after {STARTUP_TIMEOUT}s")


browser_pool = BrowserPool()
