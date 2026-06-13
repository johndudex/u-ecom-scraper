import logging

from playwright.async_api import Browser, BrowserContext, Playwright

from .config import AkamaiConfig, get_random_ua

logger = logging.getLogger(__name__)

INJECTION_SCRIPT = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' },
            ];
            plugins.length = 3;
            return plugins;
        }
    });

    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    const originalQuery = window.navigator.permissions?.query;
    if (originalQuery) {
        window.navigator.permissions.query = (params) => {
            if (params.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            return originalQuery(params);
        };
    }

    window.chrome = {
        runtime: { connect: () => {}, sendMessage: () => {} },
        loadTimes: () => {},
        csi: () => {},
        app: { isInstalled: false },
    };

    const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return getParameterOrig.call(this, param);
    };

    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        if (this.width === 0 || this.height === 0) return origToDataURL.apply(this, arguments);
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            const noise = Math.random() * 0.01;
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] = Math.min(255, Math.max(0, imageData.data[i] + noise));
            }
            ctx.putImageData(imageData, 0, 0);
        }
        return origToDataURL.apply(this, arguments);
    };

    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

    const origToString = Function.prototype.toString;
    const nativeFunctions = new Set(['navigator.webdriver']);
    Function.prototype.toString = function() {
        if (nativeFunctions.has(this.name)) return `function ${this.name}() { [native code] }`;
        return origToString.call(this);
    };

    Object.defineProperty(navigator, 'connection', {
        get: () => ({
            effectiveType: '4g',
            rtt: 50,
            downlink: 10,
            saveData: false,
        })
    });

    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
        const data = origGetChannelData.call(this, channel);
        if (data.length > 0) {
            data[0] += Math.random() * 0.0001;
        }
        return data;
    };

    const origNow = performance.now.bind(performance);
    const perfStart = origNow();
    performance.now = function() {
        return origNow() - perfStart + Math.random() * 0.001;
    };

    Date.prototype.getTimezoneOffset = function() { return 0; };

    const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
    if (origContentWindow) {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const win = origContentWindow.get.call(this);
                if (win) {
                    try {
                        Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined });
                    } catch(e) {}
                }
                return win;
            }
        });
    }
}
"""

SCRIPTS_TO_REMOVE = [
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "doubleclick.net",
    "adservice.google",
]


class StealthBrowser:
    def __init__(self, config: AkamaiConfig):
        self.config = config

    async def create_browser(self, pw: Playwright) -> Browser:
        ua = self.config.user_agent or get_random_ua()
        self.config.user_agent = ua

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
            f"--user-agent={ua}",
        ]

        proxy = None
        if self.config.proxy.enabled:
            proxy = {
                "server": self.config.proxy.server,
                "username": self.config.proxy.username or None,
                "password": self.config.proxy.password or None,
            }

        browser = await pw.chromium.launch(
            headless=self.config.headless,
            args=launch_args,
            proxy=proxy,
        )
        logger.info("Stealth browser launched (headless=%s, proxy=%s)", self.config.headless, self.config.proxy.enabled)
        return browser

    async def create_context(self, browser: Browser) -> BrowserContext:
        ua = self.config.user_agent or get_random_ua()

        context = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            device_scale_factor=1.0,
            is_mobile=False,
            has_touch=False,
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
            },
        )

        await context.add_init_script(INJECTION_SCRIPT)
        await context.route("**/*", self._intercept_requests)

        return context

    async def _intercept_requests(self, route) -> None:
        url = route.request.url
        for blocked in SCRIPTS_TO_REMOVE:
            if blocked in url:
                await route.abort()
                return
        headers = await route.request.all_headers()
        await route.continue_(headers=headers)
