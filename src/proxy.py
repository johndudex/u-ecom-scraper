"""Shared proxy utility for all scrapers.

Reads proxy configuration from config/proxy.json and provides:
- Proxy dict construction for requests
- Proxy escalation (none -> datacenter -> residential)
- Residential cost warning and user confirmation
- Retry logic with cooldown
- SSL verification bypass for Bright Data
"""

import json
import logging
import os
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "proxy.json")


class ProxyConfig:
    """Manages proxy configuration and escalation."""

    _instance: Optional["ProxyConfig"] = None

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self.config = self._load_config()

    @classmethod
    def get_instance(cls, config_path: str = CONFIG_PATH) -> "ProxyConfig":
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance

    def _load_config(self) -> dict:
        env_config = self._load_from_env()
        if env_config:
            return env_config
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(f"Proxy config not found at {self.config_path}")
            return {
                "provider": "none",
                "datacenter": {"host": "", "port": 0, "username": "", "password": ""},
                "residential": {"host": "", "port": 0, "username": "", "password": ""},
                "strategy": {
                    "default": "none",
                    "escalation": ["datacenter", "residential"],
                    "datacenter_max_retries": 3,
                    "residential_max_retries": 2,
                    "ban_status_codes": [403, 503, 429],
                    "ban_text_markers": [],
                    "cooldown_seconds": {"datacenter": 10, "residential": 30},
                    "ssl_verify": False,
                    "request_timeout": 30,
                    "session_retry_delay": 5,
                    "user_agent_rotation": True,
                },
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse proxy config: {e}")
            return {"provider": "none", "strategy": {"default": "none"}}

    def _load_from_env(self) -> dict:
        dc_user = os.environ.get("PROXY_DATACENTER_USER", "").strip()
        res_user = os.environ.get("PROXY_RESIDENTIAL_USER", "").strip()
        if not dc_user and not res_user:
            return {}
        config: dict = {"provider": "brightdata"}
        if dc_user:
            config["datacenter"] = {
                "host": os.environ.get("PROXY_DATACENTER_HOST", "brd.superproxy.io").strip(),
                "port": int(os.environ.get("PROXY_DATACENTER_PORT", "22225").strip()),
                "username": dc_user,
                "password": os.environ.get("PROXY_DATACENTER_PASS", "").strip(),
            }
        if res_user:
            config["residential"] = {
                "host": os.environ.get("PROXY_RESIDENTIAL_HOST", "brd.superproxy.io").strip(),
                "port": int(os.environ.get("PROXY_RESIDENTIAL_PORT", "22225").strip()),
                "username": res_user,
                "password": os.environ.get("PROXY_RESIDENTIAL_PASS", "").strip(),
            }
        return config

    def reload(self) -> None:
        """Reload config from disk (use when user changes config/proxy.json)."""
        self.config = self._load_config()
        ProxyConfig._instance = None
        logger.info("Proxy config reloaded from %s", self.config_path)

    def get_proxy_dict(self, proxy_tier: str = "datacenter") -> Optional[dict]:
        """Build requests-compatible proxy dict for a given tier."""
        tier_key = proxy_tier if proxy_tier in self.config else "datacenter"
        tier = self.config.get(tier_key, {})
        host = tier.get("host")
        username = tier.get("username")
        password = tier.get("password")
        port = tier.get("port")

        if not host or not username:
            return None

        proxy_url = f"http://{username}:{password}@{host}:{port}"
        return {"http": proxy_url, "https": proxy_url}

    def get_requests_session_kwargs(self) -> dict:
        """Get kwargs for requests.Session() including SSL verify setting."""
        strategy = self.config.get("strategy", {})
        return {"verify": strategy.get("ssl_verify", False)}

    def is_banned(self, status_code: int, text: str = "") -> bool:
        """Check if response indicates a ban/block."""
        strategy = self.config.get("strategy", {})
        if status_code in strategy.get("ban_status_codes", [403, 503, 429]):
            return True
        for marker in strategy.get("ban_text_markers", []):
            if marker.lower() in text.lower():
                return True
        return False

    def get_escalation_tier(self) -> list:
        """Get ordered list of escalation tiers."""
        return self.config.get("strategy", {}).get("escalation", ["datacenter", "residential"])

    def get_max_retries(self, tier: str) -> int:
        """Get max retries for a given tier."""
        strategy = self.config.get("strategy", {})
        if tier == "residential":
            return strategy.get("residential_max_retries", 2)
        return strategy.get("datacenter_max_retries", 3)

    def get_cooldown(self, tier: str) -> int:
        """Get cooldown seconds between retries for a tier."""
        strategy = self.config.get("strategy", {})
        cooldowns = strategy.get("cooldown_seconds", {"datacenter": 10, "residential": 30})
        return cooldowns.get(tier, 10)

    def get_default_mode(self) -> str:
        """Get the default proxy mode (none/datacenter/residential)."""
        return self.config.get("strategy", {}).get("default", "none")

    def get_timeout(self) -> int:
        """Get request timeout."""
        return self.config.get("strategy", {}).get("request_timeout", 30)

    def get_retry_delay(self) -> int:
        """Get delay between retry attempts."""
        return self.config.get("strategy", {}).get("session_retry_delay", 5)

    def is_residential_expensive(self) -> tuple[bool, str]:
        """Check if residential tier has cost warning."""
        residential = self.config.get("residential", {})
        warning = residential.get("cost_warning", "")
        return bool(warning), warning


def get_proxy_config(config_path: str = CONFIG_PATH) -> ProxyConfig:
    """Get or create singleton ProxyConfig instance."""
    return ProxyConfig.get_instance(config_path)


def build_proxy_url(tier: str, config: Optional[ProxyConfig] = None) -> Optional[str]:
    """Build a proxy URL string for the given tier."""
    if config is None:
        config = get_proxy_config()
    tier_data = config.config.get(tier, {})
    host = tier_data.get("host")
    username = tier_data.get("username")
    password = tier_data.get("password")
    port = tier_data.get("port")
    if not host or not username:
        return None
    return f"http://{username}:{password}@{host}:{port}"


def should_warn_residential(tier: str, config: Optional[ProxyConfig] = None) -> bool:
    """Check if using residential proxy should trigger a warning."""
    if tier != "residential":
        return False
    if config is None:
        config = get_proxy_config()
    return config.is_residential_expensive()[0]


def warn_residential_usage(url: str, config: Optional[ProxyConfig] = None) -> None:
    """Log a prominent warning about residential proxy usage."""
    if config is None:
        config = get_proxy_config()
    _, warning = config.is_residential_expensive()
    logger.warning("=" * 70)
    logger.warning("RESIDENTIAL PROXY BEING USED - THIS IS EXPENSIVE")
    logger.warning("Target: %s", url)
    logger.warning("Reason: Datacenter proxy failed or was blocked")
    logger.warning("%s", warning)
    logger.warning("If this continues, consider switching to a cheaper proxy tier")
    logger.warning("=" * 70)


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]


def get_random_user_agent() -> str:
    """Return a random User-Agent string for proxy rotation."""
    return random.choice(USER_AGENTS)
