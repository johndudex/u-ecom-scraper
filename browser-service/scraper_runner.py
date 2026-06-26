import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "/app")
DISPLAY = os.environ.get("DISPLAY", ":98")


def run_scraper_script(
    scraper_path: str,
    args: Optional[list[str]] = None,
    timeout: int = 3600,
    env_overrides: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    cmd = ["python3", scraper_path]
    if args:
        cmd.extend(args)
    if "--xvfb" not in cmd:
        cmd.append("--xvfb")

    env = {
        **os.environ,
        "DISPLAY": DISPLAY,
        "PROJECT_ROOT": PROJECT_ROOT,
        "BROWSER_CDP_ENDPOINT": f"http://127.0.0.1:9223",
        "PYTHONUNBUFFERED": "1",
    }
    if env_overrides:
        env.update(env_overrides)

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.dirname(scraper_path) or PROJECT_ROOT,
            env=env,
        )
        elapsed = round(time.time() - start, 2)
        logger.info("Scraper exited code %d in %ds", result.returncode, elapsed)

        output_file = _find_output_file(os.path.dirname(scraper_path))
        product_count = _count_products(output_file) if output_file else 0

        return {
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "output_file": output_file,
            "product_count": product_count,
            "duration": elapsed,
        }

    except subprocess.TimeoutExpired:
        logger.error("Scraper timed out after %ds", timeout)
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"Timed out after {timeout}s",
            "output_file": "",
            "product_count": 0,
            "duration": timeout,
        }

    except Exception as exc:
        logger.exception("Scraper execution failed")
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "output_file": "",
            "product_count": 0,
            "duration": round(time.time() - start, 2),
        }


def _find_output_file(site_folder: str) -> str:
    if not site_folder or not os.path.isdir(site_folder):
        return ""
    try:
        candidates = sorted(
            [
                os.path.join(site_folder, f)
                for f in os.listdir(site_folder)
                if f.startswith("output_") and f.endswith(".json")
            ]
        )
        return candidates[-1] if candidates else ""
    except Exception:
        return ""


def _count_products(output_path: str) -> int:
    if not output_path or not os.path.isfile(output_path):
        return 0
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return len(data.get("products", []))
    except Exception:
        return 0
