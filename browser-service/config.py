import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get(
    "PROXY_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "proxy.json"),
)


class ProxyConfig:
    _instance: Optional["ProxyConfig"] = None

    def __init__(self):
        self.config = self._load_config()

    @classmethod
    def get(cls) -> "ProxyConfig":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_config(self) -> dict:
        env_config = self._load_from_env()
        if env_config:
            logger.info("Proxy config loaded from environment variables")
            return env_config

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("Proxy config loaded from %s", CONFIG_PATH)
            return data
        except FileNotFoundError:
            logger.warning("Proxy config not found at %s, proxy disabled", CONFIG_PATH)
            return {"provider": "none"}
        except json.JSONDecodeError as e:
            logger.error("Failed to parse proxy config: %s", e)
            return {"provider": "none"}

    def _load_from_env(self) -> Optional[dict]:
        dc_host = os.environ.get("PROXY_DATACENTER_HOST", "").strip()
        dc_user = os.environ.get("PROXY_DATACENTER_USER", "").strip()
        dc_pass = os.environ.get("PROXY_DATACENTER_PASS", "").strip()
        dc_port = os.environ.get("PROXY_DATACENTER_PORT", "22225").strip()

        res_host = os.environ.get("PROXY_RESIDENTIAL_HOST", "").strip()
        res_user = os.environ.get("PROXY_RESIDENTIAL_USER", "").strip()
        res_pass = os.environ.get("PROXY_RESIDENTIAL_PASS", "").strip()
        res_port = os.environ.get("PROXY_RESIDENTIAL_PORT", "22225").strip()

        if not dc_user and not res_user:
            return None

        config: dict = {"provider": "brightdata"}

        if dc_user:
            config["datacenter"] = {
                "host": dc_host or "brd.superproxy.io",
                "port": int(dc_port),
                "username": dc_user,
                "password": dc_pass,
            }

        if res_user:
            config["residential"] = {
                "host": res_host or "brd.superproxy.io",
                "port": int(res_port),
                "username": res_user,
                "password": res_pass,
            }

        return config

    def get_tier(self, tier: str) -> dict:
        return self.config.get(tier, {})

    def build_proxy_url(self, tier: str) -> Optional[str]:
        tier_data = self.config.get(tier, {})
        host = tier_data.get("host")
        username = tier_data.get("username")
        password = tier_data.get("password")
        port = tier_data.get("port")
        if not host or not username:
            return None
        return f"http://{username}:{password}@{host}:{port}"

    def build_proxy_string(self, tier: str) -> Optional[str]:
        tier_data = self.config.get(tier, {})
        host = tier_data.get("host")
        username = tier_data.get("username")
        password = tier_data.get("password")
        port = tier_data.get("port")
        if not host or not username:
            return None
        return f"{username}:{password}@{host}:{port}"

    def build_chrome_proxy_arg(self, tier: str) -> Optional[str]:
        url = self.build_proxy_url(tier)
        if not url:
            return None
        return f"--proxy-server={url}"

    def build_playwright_proxy(self, tier: str) -> Optional[dict]:
        tier_data = self.config.get(tier, {})
        host = tier_data.get("host")
        username = tier_data.get("username")
        password = tier_data.get("password")
        port = tier_data.get("port")
        if not host or not username:
            return None
        return {
            "server": f"http://{host}:{port}",
            "username": username,
            "password": password,
        }


def get_proxy_config() -> ProxyConfig:
    return ProxyConfig.get()
