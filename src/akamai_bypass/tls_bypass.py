import logging
import random
from typing import Optional

from curl_cffi import requests as curl_requests

from .config import AkamaiConfig, get_random_ua
from .cookie_manager import CookieManager

logger = logging.getLogger(__name__)


class TLSBypass:
    AKAMAI_COOKIE_URLS: dict[str, str] = {}

    def __init__(self, cookie_manager: CookieManager, config: Optional[AkamaiConfig] = None):
        self.cookie_mgr = cookie_manager
        self.config = config or AkamaiConfig()
        self.session: Optional[curl_requests.Session] = None

    def _create_session(self) -> curl_requests.Session:
        imp = random.choice(["chrome131", "chrome124", "chrome120", "chrome116"])
        session = curl_requests.Session(impersonate=imp)
        ua = self.config.user_agent or get_random_ua()
        self.config.user_agent = ua
        session.headers.update({
            "User-Agent": ua,
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })

        if self.config.proxy.enabled:
            session.proxies = {
                "http": self._build_proxy_url(),
                "https": self._build_proxy_url(),
            }

        return session

    def _build_proxy_url(self) -> str:
        p = self.config.proxy
        if p.username and p.password:
            return f"http://{p.username}:{p.password}@{p.server.replace('http://', '')}"
        return p.server

    def acquire_cookies(self, domain: str) -> Optional[dict]:
        self.session = self._create_session()
        base_url = self.AKAMAI_COOKIE_URLS.get(domain, f"https://{domain}/")

        try:
            logger.info("TLS: acquiring cookies for %s via %s", domain, base_url)
            resp = self.session.get(base_url, timeout=30, allow_redirects=True)

            logger.info(
                "TLS: status=%d, cookies=%s",
                resp.status_code,
                list(resp.cookies.keys()),
            )

            akamai_cookies = []
            for name, value in resp.cookies.items():
                akamai_cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                })

            if self.cookie_mgr.has_valid_abck(akamai_cookies):
                logger.info("TLS: got valid _abck cookie")
            else:
                logger.info("TLS: initial cookies acquired (may need browser challenge)")

            self.cookie_mgr.save_cookies(domain, akamai_cookies)

            return {
                "cookies": akamai_cookies,
                "status": resp.status_code,
                "headers": dict(resp.headers),
            }

        except Exception as e:
            logger.warning("TLS: cookie acquisition failed: %s", e)
            return None

    def fetch_url(self, url: str, cookies: Optional[list[dict]] = None) -> Optional[dict]:
        if not self.session:
            self.session = self._create_session()

        try:
            cookie_dict = {}
            if cookies:
                for c in cookies:
                    cookie_dict[c["name"]] = c["value"]

            resp = self.session.get(url, cookies=cookie_dict, timeout=30)
            logger.info("TLS API: %s -> %d", url[:100], resp.status_code)

            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "json" in content_type:
                    return {"data": resp.json(), "status": resp.status_code}
                return {"html": resp.text, "status": resp.status_code}
            elif resp.status_code == 403:
                logger.warning("TLS API: 403 Forbidden - Akamai blocked")
            return None

        except Exception as e:
            logger.warning("TLS API: request failed: %s", e)
            return None
