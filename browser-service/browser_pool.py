import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

DISPLAY = os.environ.get("DISPLAY", ":98")
MCP_CDP_PORT = int(os.environ.get("MCP_CDP_PORT", "9222"))
SCRAPER_CDP_PORT = int(os.environ.get("SCRAPER_CDP_PORT", "9223"))
XVFB_RESOLUTION = os.environ.get("XVFB_RESOLUTION", "1920x1080x24")
STARTUP_TIMEOUT = int(os.environ.get("STARTUP_TIMEOUT", "45"))
CHROME_USER_DATA_DIR = "/tmp/chrome-profiles"


class BrowserPool:
    def __init__(self):
        self._xvfb_proc: Optional[subprocess.Popen] = None
        self._mcp_chrome_proc: Optional[subprocess.Popen] = None
        self._scraper_chrome_proc: Optional[subprocess.Popen] = None
        self._ready = False

    def startup(self) -> dict:
        errors = []

        self._start_xvfb(errors)
        self._start_mcp_chrome(errors)
        self._start_scraper_chrome(errors)

        if not errors:
            self._ready = True
            logger.info("Browser pool ready: Xvfb=%s, MCP Chrome=:%d, Scraper Chrome=:%d",
                         DISPLAY, MCP_CDP_PORT, SCRAPER_CDP_PORT)
        else:
            logger.error("Browser pool startup errors: %s", errors)

        return {"xvfb_running": self._xvfb_proc is not None,
                "mcp_chrome_running": self._mcp_chrome_proc is not None,
                "scraper_chrome_running": self._scraper_chrome_proc is not None,
                "errors": errors}

    def health(self) -> dict:
        return {
            "ready": self._ready,
            "xvfb_running": self._xvfb_proc is not None and self._xvfb_proc.poll() is None,
            "mcp_chrome_running": self._mcp_chrome_proc is not None and self._mcp_chrome_proc.poll() is None,
            "scraper_chrome_running": self._scraper_chrome_proc is not None and self._scraper_chrome_proc.poll() is None,
            "mcp_cdp_port": MCP_CDP_PORT,
            "scraper_cdp_port": SCRAPER_CDP_PORT,
            "display": DISPLAY,
        }

    def shutdown(self):
        for proc in [self._scraper_chrome_proc, self._mcp_chrome_proc, self._xvfb_proc]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
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
            )
            time.sleep(1)
            if self._xvfb_proc.poll() is not None:
                errors.append(f"Xvfb exited immediately with code {self._xvfb_proc.returncode}")
                self._xvfb_proc = None
        except FileNotFoundError:
            errors.append("Xvfb binary not found")
        except Exception as e:
            errors.append(f"Xvfb failed: {e}")

    def _start_mcp_chrome(self, errors: list):
        if not self._xvfb_proc:
            errors.append("Skipping MCP Chrome — Xvfb not running")
            return
        try:
            args = [
                "google-chrome-stable",
                f"--remote-debugging-port={MCP_CDP_PORT}",
                "--remote-debugging-address=0.0.0.0",
                "--remote-allow-origins=*",
                f"--display={DISPLAY}",
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
            self._mcp_chrome_proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, "DISPLAY": DISPLAY},
            )
            time.sleep(3)
            if self._mcp_chrome_proc.poll() is not None:
                stdout = self._mcp_chrome_proc.stdout.read().decode()[:500] if self._mcp_chrome_proc.stdout else ""
                errors.append(f"MCP Chrome exited immediately with code {self._mcp_chrome_proc.returncode}: {stdout}")
                self._mcp_chrome_proc = None
            else:
                self._wait_for_cdp(MCP_CDP_PORT, errors, "MCP Chrome")
        except Exception as e:
            errors.append(f"MCP Chrome failed: {e}")

    def _start_scraper_chrome(self, errors: list):
        if not self._xvfb_proc:
            errors.append("Skipping Scraper Chrome — Xvfb not running")
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
            self._scraper_chrome_proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, "DISPLAY": DISPLAY},
            )
            time.sleep(3)
            if self._scraper_chrome_proc.poll() is not None:
                stdout = self._scraper_chrome_proc.stdout.read().decode()[:500] if self._scraper_chrome_proc.stdout else ""
                errors.append(f"Scraper Chrome exited immediately with code {self._scraper_chrome_proc.returncode}: {stdout}")
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
