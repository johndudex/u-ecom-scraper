import logging
import os
import random
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

COOKIE_DIR = os.path.join(
    os.environ.get("AKAMAI_COOKIE_DIR", "/app/data/akamai-cookies")
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
]


@dataclass
class AkamaiProxyConfig:
    enabled: bool = False
    server: str = ""
    username: str = ""
    password: str = ""


@dataclass
class BehaviorConfig:
    min_delay: float = 2.0
    max_delay: float = 6.0
    scroll_count: int = 3
    mouse_movements: int = 5
    typing_delay_min: float = 0.05
    typing_delay_max: float = 0.15
    page_load_timeout: int = 30000


@dataclass
class AkamaiConfig:
    proxy: AkamaiProxyConfig = field(default_factory=AkamaiProxyConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    headless: bool = True
    user_agent: Optional[str] = None
    retry_count: int = 3
    retry_delay: float = 10.0
    cookie_dir: str = field(default_factory=lambda: COOKIE_DIR)
    max_cookie_age_hours: int = 4
    reuse_cookies: bool = True


def get_random_ua() -> str:
    return random.choice(USER_AGENTS)


def build_akamai_config_from_proxy_tier(tier: str = "none") -> AkamaiConfig:
    cfg = AkamaiConfig()
    if tier == "none":
        return cfg

    host = os.environ.get(f"PROXY_{tier.upper()}_HOST", "").strip()
    port = os.environ.get(f"PROXY_{tier.upper()}_PORT", "22225").strip()
    user = os.environ.get(f"PROXY_{tier.upper()}_USER", "").strip()
    passwd = os.environ.get(f"PROXY_{tier.upper()}_PASS", "").strip()

    if not host or not user:
        host = os.environ.get("PROXY_DATACENTER_HOST", "").strip()
        port = os.environ.get("PROXY_DATACENTER_PORT", "22225").strip()
        user = os.environ.get("PROXY_DATACENTER_USER", "").strip()
        passwd = os.environ.get("PROXY_DATACENTER_PASS", "").strip()

    if host and user:
        cfg.proxy = AkamaiProxyConfig(
            enabled=True,
            server=f"http://{host}:{port}",
            username=user,
            password=passwd,
        )
    return cfg
