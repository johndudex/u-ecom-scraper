import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class CookieManager:
    def __init__(self, cookie_dir: Optional[str] = None):
        self.cookie_dir = cookie_dir or "/app/data/akamai-cookies"
        os.makedirs(self.cookie_dir, exist_ok=True)

    def _cookie_file(self, domain: str) -> str:
        safe = domain.replace(".", "_").replace("/", "_").replace(":", "_")
        return os.path.join(self.cookie_dir, f"{safe}.json")

    def save_cookies(self, domain: str, cookies: list[dict]) -> None:
        payload = {
            "cookies": cookies,
            "saved_at": time.time(),
            "domain": domain,
        }
        path = self._cookie_file(domain)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("Saved %d cookies for %s", len(cookies), domain)

    def load_cookies(self, domain: str, max_age_hours: int = 4) -> Optional[list[dict]]:
        path = self._cookie_file(domain)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                payload = json.load(f)
            age = time.time() - payload.get("saved_at", 0)
            if age > max_age_hours * 3600:
                logger.info("Cookies for %s expired (%.0fh old)", domain, age / 3600)
                return None
            cookies = payload.get("cookies", [])
            logger.info("Loaded %d cookies for %s (age: %.0fh)", len(cookies), domain, age / 3600)
            return cookies
        except Exception as e:
            logger.warning("Failed to load cookies for %s: %s", domain, e)
            return None

    def has_valid_abck(self, cookies: list[dict]) -> bool:
        for c in cookies:
            if c.get("name") == "_abck":
                val = c.get("value", "")
                if len(val) > 30 and "~0." not in val:
                    return True
        return False

    def clear_cookies(self, domain: str) -> None:
        path = self._cookie_file(domain)
        if os.path.exists(path):
            os.remove(path)
            logger.info("Cleared cookies for %s", domain)
