import asyncio
import logging
import random
import time
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page

from .config import AkamaiConfig, get_random_ua
from .cookie_manager import CookieManager
from .stealth import StealthBrowser

logger = logging.getLogger(__name__)


class AkamaiBypass:
    def __init__(self, config: Optional[AkamaiConfig] = None):
        self.config = config or AkamaiConfig()
        self.cookie_mgr = CookieManager(self.config.cookie_dir)
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None

    async def launch(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        stealth = StealthBrowser(self.config)
        self.browser = await stealth.create_browser(self._playwright)
        self.context = await stealth.create_context(self.browser)
        self.page = await self.context.new_page()

        await self.page.add_init_script("""
            () => {
                const origAppendChild = Element.prototype.appendChild;
                Element.prototype.appendChild = function(child) {
                    if (child.tagName === 'IFRAME' && child.id && child.id.includes('sec')) {
                        child.style.display = 'block';
                        child.style.width = '100%';
                        child.style.height = '100%';
                    }
                    return origAppendChild.call(this, child);
                };
            }
        """)

    async def close(self) -> None:
        for resource in [self.page, self.context, self.browser]:
            if resource:
                try:
                    await resource.close()
                except Exception:
                    pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self.page = None
        self.context = None
        self.browser = None
        self._playwright = None

    async def get_page(self, url: str) -> Optional[dict]:
        domain = _extract_domain(url)
        for attempt in range(self.config.retry_count):
            try:
                if not self.browser:
                    await self.launch()

                if self.config.reuse_cookies:
                    await self._load_saved_cookies(domain)

                await self._human_navigate(url)

                if await self._is_blocked():
                    logger.info("Attempt %d: block detected, solving challenge...", attempt + 1)
                    await self._solve_challenge()
                    if await self._is_blocked():
                        logger.info("Attempt %d: still blocked, restarting browser", attempt + 1)
                        await self._restart_browser()
                        continue

                cookies = await self.context.cookies()
                self.cookie_mgr.save_cookies(domain, cookies)

                return await self._extract_page_data(url)

            except Exception as e:
                logger.warning("Attempt %d error: %s", attempt + 1, e)
                await self._restart_browser()
                if attempt < self.config.retry_count - 1:
                    await asyncio.sleep(self.config.retry_delay)

        return None

    async def _human_navigate(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.config.behavior.page_load_timeout)
        await self._simulate_human_behavior()
        await self._wait_for_content()
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

    async def _wait_for_content(self, max_wait: float = 30.0) -> None:
        start = time.time()
        while time.time() - start < max_wait:
            try:
                html = await self.page.content()
                hlen = len(html)

                if hlen > 10000 and "sec-if-cpt-container" not in html:
                    title = await self.page.title()
                    logger.info("Real content loaded (HTML: %d, Title: %s)", hlen, title[:80])
                    return

                if "sec-if-cpt-container" in html or hlen < 5000:
                    logger.info("Challenge page (HTML: %d), waiting...", hlen)
                    await self._simulate_human_behavior()
                    try:
                        captcha_btn = await self.page.query_selector(
                            "#sec-cpt-if, #sec-cpt-intensive, button.sec-bc-button"
                        )
                        if captcha_btn:
                            logger.info("Found challenge button, clicking...")
                            await captcha_btn.click(timeout=3000)
                            await asyncio.sleep(5)
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    continue

                await asyncio.sleep(1.0)
            except Exception as e:
                logger.debug("Content check error: %s", e)
                await asyncio.sleep(2.0)
                continue

        logger.warning("Wait timeout after %.0fs", max_wait)

    async def _simulate_human_behavior(self) -> None:
        cfg = self.config.behavior
        await asyncio.sleep(random.uniform(1.0, 2.5))

        viewport = self.page.viewport_size or {"width": 1920, "height": 1080}
        w, h = viewport["width"], viewport["height"]

        for _ in range(cfg.mouse_movements):
            x = random.randint(100, w - 100)
            y = random.randint(100, h - 100)
            await self.page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.1, 0.4))

        for i in range(cfg.scroll_count):
            scroll_y = random.randint(200, 600)
            direction = 1 if i % 2 == 0 else -1
            steps = random.randint(3, 8)
            for _ in range(steps):
                delta = scroll_y // steps * direction
                await self.page.mouse.wheel(0, delta)
                await asyncio.sleep(random.uniform(0.1, 0.3))

        await asyncio.sleep(random.uniform(0.5, 1.5))

    async def _solve_challenge(self) -> None:
        logger.info("Attempting Akamai challenge solve...")
        try:
            for attempt in range(5):
                await asyncio.sleep(random.uniform(3.0, 6.0))
                await self._simulate_human_behavior()

                captcha_btn = await self.page.query_selector(
                    "#sec-cpt-if, #sec-cpt-intensive, button.sec-bc-button, #sec-bc-button"
                )
                if captcha_btn:
                    logger.info("Clicking challenge button (attempt %d)", attempt + 1)
                    try:
                        await captcha_btn.click(timeout=5000)
                    except Exception:
                        await self.page.evaluate("(el) => el.click()", captcha_btn)
                    await asyncio.sleep(5)

                html = await self.page.content()
                if len(html) > 10000 and "sec-if-cpt-container" not in html:
                    logger.info("Challenge solved!")
                    return

                logger.info("Still on challenge page (HTML: %d)", len(html))

        except Exception as e:
            logger.warning("Challenge solve error: %s", e)

    async def _is_blocked(self) -> bool:
        try:
            content = await self.page.content()
            hlen = len(content)

            if hlen < 5000 and "sec-if-cpt-container" in content:
                return True
            if hlen < 3000:
                return True

            title = await self.page.title()
            if any(kw in title.lower() for kw in ["access denied", "blocked", "forbidden", "just a moment"]):
                return True

            body_text = await self.page.evaluate(
                "() => document.body?.innerText?.substring(0, 1000) || ''"
            )
            if any(
                kw in body_text.lower()
                for kw in ["access denied", "you have been blocked", "blocked your access", "reference #"]
            ):
                return True

        except Exception:
            pass
        return False

    async def _load_saved_cookies(self, domain: str) -> None:
        cookies = self.cookie_mgr.load_cookies(domain, self.config.max_cookie_age_hours)
        if cookies:
            if self.cookie_mgr.has_valid_abck(cookies):
                logger.info("Loading valid Akamai cookies for %s", domain)
                try:
                    await self.context.add_cookies(cookies)
                except Exception as e:
                    logger.warning("Failed to load cookies: %s", e)
                    self.cookie_mgr.clear_cookies(domain)
            else:
                logger.info("Saved cookies lack valid _abck, clearing")
                self.cookie_mgr.clear_cookies(domain)

    async def _restart_browser(self) -> None:
        try:
            await self.close()
        except Exception:
            pass
        self.config.user_agent = get_random_ua()
        await asyncio.sleep(random.uniform(2.0, 5.0))

    async def _extract_page_data(self, url: str) -> dict:
        for retry in range(3):
            try:
                title = await self.page.title()
                content = await self.page.content()
                break
            except Exception:
                if retry < 2:
                    await asyncio.sleep(2.0)
                else:
                    raise

        cookies = []
        try:
            cookies = await self.context.cookies()
        except Exception:
            pass

        return {
            "url": url,
            "title": title,
            "html": content,
            "html_length": len(content),
            "cookies": cookies,
            "timestamp": time.time(),
            "method": "akamai_playwright_stealth",
        }


def _extract_domain(url: str) -> str:
    return url.split("//")[-1].split("/")[0]
