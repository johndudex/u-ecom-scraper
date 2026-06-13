import logging
import time
from typing import Optional

from seleniumbase import SB

from .config import AkamaiConfig
from .cookie_manager import CookieManager

logger = logging.getLogger(__name__)


class UndetectedChromeBypass:
    def __init__(self, config: Optional[AkamaiConfig] = None):
        self.config = config or AkamaiConfig()
        self.cookie_mgr = CookieManager(self.config.cookie_dir)

    def get_page(self, url: str) -> Optional[dict]:
        domain = url.split("//")[-1].split("/")[0]

        sb_kwargs = {
            "uc": True,
            "headless": self.config.headless,
            "locale_code": "en",
        }
        if self.config.proxy.enabled:
            proxy_addr = self.config.proxy.server.replace("http://", "")
            if self.config.proxy.username:
                proxy_addr = f"{self.config.proxy.username}:{self.config.proxy.password}@{proxy_addr}"
            sb_kwargs["proxy"] = proxy_addr

        try:
            with SB(**sb_kwargs) as sb:
                logger.info("UC Chrome: launching for %s", url[:100])

                saved = self.cookie_mgr.load_cookies(domain, self.config.max_cookie_age_hours)
                if saved and self.config.reuse_cookies:
                    sb.open("about:blank")
                    for c in saved:
                        try:
                            sb.execute_cdp_cmd("Network.setCookie", {
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c.get("domain", domain),
                                "path": c.get("path", "/"),
                            })
                        except Exception:
                            pass
                    logger.info("UC: loaded %d saved cookies", len(saved))

                sb.uc_open_with_reconnect(url, reconnect_time=3)

                sb.sleep(2)
                for _ in range(3):
                    sb.scroll_to_bottom()
                    sb.sleep(1)
                    sb.scroll_to_top()
                    sb.sleep(1)

                sb.sleep(2)

                title = sb.get_title()
                html = sb.get_page_source()

                if self._is_blocked(title, html):
                    logger.info("UC: still blocked, trying manual solve...")
                    sb.uc_gui_click_captcha()
                    sb.sleep(5)
                    title = sb.get_title()
                    html = sb.get_page_source()

                    if self._is_blocked(title, html):
                        logger.warning("UC: could not bypass block")
                        return None

                cookies = sb.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
                self.cookie_mgr.save_cookies(domain, cookies)

                return {
                    "url": url,
                    "title": title,
                    "html": html,
                    "html_length": len(html),
                    "cookies": cookies,
                    "timestamp": time.time(),
                    "method": "akamai_uc_chrome",
                }

        except Exception as e:
            logger.warning("UC Chrome error: %s", e)
            return None

    def _is_blocked(self, title: str, html: str) -> bool:
        signals = ["access denied", "blocked", "forbidden", "just a moment"]
        lower = (title + " " + html[:2000]).lower()
        return any(s in lower for s in signals)
