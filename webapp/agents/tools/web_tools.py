"""Web fetching tools for LangGraph agent nodes.

Provides a single ``web_fetch`` tool that retrieves URL content using httpx
and returns it as text or markdown.
"""

import logging

import httpx

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def get_web_tools() -> list:
    """Return web fetching tools.

    Returns:
        List of LangChain BaseTool instances.
    """

    @tool
    def web_fetch(url: str, format: str = "markdown") -> str:
        """Fetch content from a URL and return it as text or markdown.

        HTTP URLs are automatically upgraded to HTTPS.  Follows redirects
        up to 5 hops.

        Args:
            url: The fully-qualified URL to fetch.
            format: Response format — ``"text"`` for raw text or
                ``"markdown"`` (default) for cleaned markdown.

        Returns:
            The page content, or an error message if the fetch fails.
        """
        logger.info("web_fetch: %s", url[:200])
        if not url.startswith(("http://", "https://")):
            return f"Invalid URL: {url}"

        try:
            with httpx.Client(
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as client:
                resp = client.get(url)

            if resp.status_code >= 400:
                return (
                    f"HTTP {resp.status_code} fetching {url}: "
                    f"{resp.text[:500]}"
                )

            content = resp.text

            if format == "text":
                return content[:30000] or "(empty page)"

            if "<" in content and "</" in content:
                try:
                    from bs4 import BeautifulSoup

                    soup = BeautifulSoup(content, "html.parser")
                    for tag in soup(
                        ["script", "style", "noscript", "link"]
                    ):
                        tag.decompose()
                    content = soup.get_text(separator="\n", strip=True)
                except Exception:
                    pass

            content = content[:30000] or "(empty page)"

            if len(content) > 4000:
                try:
                    from headroom import compress as _compress

                    cr = _compress(
                        [{"role": "tool", "content": content}],
                        model="glm-5-turbo",
                    )
                    compressed = cr.messages[0]["content"]
                    if len(content) - len(compressed) > 200:
                        logger.info(
                            "web_fetch compressed: %d → %d chars",
                            len(content),
                            len(compressed),
                        )
                        content = compressed
                except Exception:
                    pass

            return content

        except httpx.TimeoutException:
            return f"Request timed out after {DEFAULT_TIMEOUT}s: {url}"
        except Exception as e:
            return f"Error fetching {url}: {e}"

    return [web_fetch]
