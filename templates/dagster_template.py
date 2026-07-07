class BaseTlsScraper(BaseScraper):
    """
    Universally handles all tls_client rotation, session management,
    and proxy networking logic away from individual retailer scripts.
    """
    def __init__(self, proxy=None, brightdata_proxy=None, bypass=None, log=None):
        '''
        no modification needed.
        '''
        super().__init__(proxy, brightdata_proxy, bypass, log)
        self.brightdata_proxy = brightdata_proxy.as_brightdata_proxy() if brightdata_proxy else None
        self.local = threading.local()
 
    def get_thread_session(self):
        '''
        no modification needed.
        '''
        if not hasattr(self.local, "session"):
            self.local.session = tls_client.Session(
                client_identifier=random.choice(TLS_CLIENTS),
                random_tls_extension_order=True,
            )
        return self.local.session
 
    def make_request(self, session, url, proxy=None, headers=None):
        '''
        no modification needed.
        '''
        # Fallback to standard headers if your retailer class doesn't override them
        default_headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "en-GB,en;q=0.9,en-US;q=0.8,pt;q=0.7,ta;q=0.6",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        use_headers = headers if headers is not None else default_headers
        try:
            resp = session.get(
                url, headers=use_headers, allow_redirects=True,
                proxy=proxy, timeout_seconds=30, insecure_skip_verify=True,
            )
            return resp, resp.status_code, None
        except Exception as e:
            if "too many redirects" in str(e).lower():
                raise Exception("too_many_redirects")
            return None, None, str(e)
 
    def _fetch(self, url: str, proxy: str, attempt: int = 1):
 
        if attempt > 5:
            if self.log:
                self.log.error(f"Max retries exceeded for {url}")
            return None, 0
 
        session = self.get_thread_session()
        session.cookies.clear()
 
        if not hasattr(self.local, "url_count"):
            self.local.url_count = 0
 
        if self.local.url_count >= 50:
            self.local.session = tls_client.Session(
                client_identifier=random.choice(TLS_CLIENTS),
                random_tls_extension_order=True,
            )
            session = self.local.session
            self.local.url_count = 0
 
        self.local.url_count += 1
 
        resp, status, error_msg = self.make_request(session, url, proxy=proxy)
 
        if resp and status == 200:
            response_text_lower = resp.text.lower()
 
            if any(
                keyword in response_text_lower for keyword in BOT_PROTECTION_KEYWORDS
            ):
                error_msg = "click-fraud detection"
                status = 403  # Intercept and treat as blocked to force rotation
                resp = None
 
        if error_msg:
            err = error_msg.lower()
            if "too_many_redirects" in err:
                raise Exception("too_many_redirects")
 
            if any(keyword in err for keyword in RETRYABLE_ERROR_KEYWORDS):
                sleep_time = random.uniform(0.5, 1.5)
                if self.log:
                    self.log.warning(
                        f"Connection/fraud error on attempt {attempt}. Sleeping {sleep_time:.1f}s"
                    )
                time.sleep(sleep_time)
                # Force rotation on click fraud
                if "click-fraud" in err:
                    self.local.url_count = 50
                return self._fetch(url, proxy, attempt=attempt + 1)
 
            elif "466" in err or "too many requests" in err:
                base_sleep = 2 ** (attempt + 1)
                total_sleep = base_sleep + random.uniform(0, base_sleep)
                if self.log:
                    self.log.warning(
                        f"Proxy 466 Congestion. Sleeping {total_sleep:.1f}s"
                    )
                time.sleep(total_sleep)
                return self._fetch(url, proxy, attempt=attempt + 1)
            else:
                if self.log:
                    self.log.warning(f"Error on attempt {attempt}: {error_msg}")
                time.sleep(random.uniform(3, 7))
                return self._fetch(url, proxy, attempt=attempt + 1)
 
        if resp and status == 200:
            return resp.text, resp.status_code
 
        if status in {404, 410}:
            return None, status
 
        if status in {403, 405}:
            if self.log:
                self.log.warning(f"Scraper blocked with {status}. Rotating session.")
            self.local.url_count = 50  # Force rotation on next loop
            return self._fetch(url, proxy, attempt=attempt + 1)
 
        if status in {429, 466, 444}:
            base_sleep = 2 ** (attempt + 1)
            total_sleep = base_sleep + random.uniform(0, base_sleep)
            if self.log:
                self.log.warning(f"{status} response. Sleeping {total_sleep:.1f}s")
            time.sleep(total_sleep)
            return self._fetch(url, proxy, attempt=attempt + 1)
 
        if status in {500, 502, 503, 504, 202}:
            if self.log:
                self.log.warning(f"Server error {status} on attempt {attempt}.")
            time.sleep(random.uniform(3, 7))
            return self._fetch(url, proxy, attempt=attempt + 1)
 
        return None, status
 
    def discover_urls(self) -> list[str]:
        '''
        Phase 1: Discover all item/page URLs to scrape.

        For navigation jobs (search/category/filter-driven sites):
        Iterate filter combinations (e.g. state × profession), build listing
        URLs via the site's URL pattern, paginate, and collect + dedup item URLs.
        Use self._fetch() for HTTP-based sites, or a browser/render service
        for JS-rendered sites.

        For URL-list jobs (URLs provided directly):
        Return the input URL list as-is.

        The pipeline calls this to get the URL list, then calls scrape_one(url)
        for each URL.
        '''
        pass

    def scrape_one(self, url: str) -> dict:
        '''
        Phase 2: Parse a single item page and return extracted fields.

        Fetch the page (via self._fetch for HTTP, or a browser for JS-rendered
        sites), then extract structured fields (title, price, company, location,
        etc.) using CSS selectors, regex, JSON-LD, or API parsing.

        Returns a dict of field name → value, e.g.:
            {"title": "...", "company": "...", "location": "...", "url": url}
        '''
        pass