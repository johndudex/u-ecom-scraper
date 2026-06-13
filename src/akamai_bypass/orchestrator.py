import logging
from typing import Optional

from .config import AkamaiConfig
from .cookie_manager import CookieManager
from .tls_bypass import TLSBypass
from .bypass import AkamaiBypass
from .uc_bypass import UndetectedChromeBypass

logger = logging.getLogger(__name__)


class AkamaiOrchestrator:
    """
    3-layer Akamai bypass:
    Layer 1: TLS fingerprint (curl_cffi) — fast cookie acquisition
    Layer 2: Playwright stealth — full browser with anti-fingerprinting
    Layer 3: SeleniumBase UC — undetected Chrome fallback
    """

    def __init__(self, config: Optional[AkamaiConfig] = None):
        self.config = config or AkamaiConfig()

    async def probe(self, url: str) -> Optional[dict]:
        domain = url.split("//")[-1].split("/")[0]
        logger.info("AkamaiOrchestrator: starting probe for %s", url)

        # Layer 1: TLS pre-warm
        logger.info("Layer 1: TLS fingerprint pre-warming")
        cookie_mgr = CookieManager(self.config.cookie_dir)
        tls = TLSBypass(cookie_mgr, self.config)
        tls_result = tls.acquire_cookies(domain)
        if tls_result:
            logger.info(
                "TLS: got %d cookies, status %d",
                len(tls_result.get("cookies", [])),
                tls_result.get("status", 0),
            )

        # Layer 2: Playwright stealth
        logger.info("Layer 2: Playwright stealth browser")
        result = await self._run_playwright(url)
        if result and result.get("html_length", 0) > 5000:
            logger.info("Playwright stealth: success! HTML length: %d", result["html_length"])
            return result

        # Layer 3: UC Chrome fallback
        logger.info("Layer 3: Undetected Chrome fallback")
        uc = UndetectedChromeBypass(self.config)
        result = uc.get_page(url)
        if result and result.get("html_length", 0) > 5000:
            logger.info("UC Chrome: success! HTML length: %d", result["html_length"])
            return result

        logger.warning("All Akamai bypass layers failed for %s", url[:100])
        return None

    async def _run_playwright(self, url: str) -> Optional[dict]:
        bypass = AkamaiBypass(self.config)
        try:
            result = await bypass.get_page(url)
            return result
        except Exception as e:
            logger.warning("Playwright stealth error: %s", e)
            return None
        finally:
            await bypass.close()

    @staticmethod
    def detect_akamai_signals(html: str, status_code: int = 0) -> bool:
        lower = html[:5000].lower()
        signals = [
            "akamai",
            "sec-if-cpt-container",
            "sec-cpt-if",
            "_abck",
            "akamai-sw",
            "akamai_beacon",
            "sensor_data",
            "/akam/",
            "reference #",
        ]
        found = [s for s in signals if s in lower]
        if found:
            logger.info("Akamai signals detected: %s", found)
            return True

        if status_code == 403 and len(html) < 5000:
            logger.info("Akamai suspected: 403 with short body (%d)", len(html))
            return True

        return False
