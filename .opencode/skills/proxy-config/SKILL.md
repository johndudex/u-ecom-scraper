---
name: proxy-config
description: Proxy configuration, escalation strategy, and retry logic for ecommerce scraping using Bright Data proxies. Covers datacenter and residential tiers with automatic escalation.
license: MIT
compatibility: opencode
metadata:
  audience: code-writer, site-analyzer, product-analyzer
  workflow: scraping
  config_file: config/proxy.json
---

# Proxy Configuration & Escalation Strategy

## What I Do

Provide proxy integration for all scraping — Python scrapers and Playwright MCP. Supports automatic escalation from no-proxy → datacenter → residential based on blocking. Tracks credentials from a separate config file.

## Proxy Config Location

**All proxy credentials live in `config/proxy.json`** (project root). Change this single file to update all scrapers.

## Proxy Tiers (Escalation Order)

| Priority | Tier | When to Use | Cost |
|----------|------|-------------|-------|
| 1st | **No proxy** | No anti-bot, direct requests work | Free |
| 2nd | **Datacenter** | Site blocks direct IP or needs geo-targeting | Low |
| 3rd | **Residential** | Datacenter blocked, requires real IP | **HIGH — ask user first** |

## Bright Data Proxy Setup

### Config File: `config/proxy.json`

```json
{
  "provider": "bright_data",
  "datacenter": {
    "host": "brd.superproxy.io",
    "port": 33335,
    "username": "brd-customer-hl_ZONE-datacenter_scraperbuilderai",
    "password": "PASSWORD"
  },
  "residential": {
    "host": "brd.superproxy.io",
    "port": 33335,
    "username": "brd-customer-hl_ZONE-residential_scraperbuilderai",
    "password": "PASSWORD",
    "cost_warning": "Residential proxies are EXPENSIVE."
  },
  "strategy": {
    "default": "none",
    "escalation": ["datacenter", "residential"],
    "ban_status_codes": [403, 503, 429],
    "ban_text_markers": ["captcha", "robot check", "unusual activity"],
    "ssl_verify": false,
    "cooldown_seconds": { "datacenter": 10, "residential": 30 }
  }
}
```

### Python Scraper Integration

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.proxy import ProxyConfig, should_warn_residential, warn_residential_usage

proxy_config = ProxyConfig.get_instance()
```

### Proxy Dict for requests

```python
proxy_dict = proxy_config.get_proxy_dict("datacenter")
proxies = proxy_dict if proxy_dict else None
response = requests.get(url, proxies=proxies, timeout=proxy_config.get_timeout(), verify=proxy_config.config["strategy"]["ssl_verify"])
```

### Playwright MCP

Playwright MCP uses datacenter proxy by default (configured in `opencode.json`).
Update `opencode.json` if credentials change.

## ⚠️ Residential Proxy Policy

**RESIDENTIAL PROXIES ARE EXPENSIVE — ONLY USE AS LAST RESORT.**

Before using residential, check:
1. Has datacenter proxy already been tried and failed?
2. Is the block due to IP detection (vs rate limiting)?
3. Would increasing delay or retrying solve the issue?

When residential IS required:
- Log a prominent warning: `⚠️ RESIDENTIAL PROXY BEING USED — THIS IS EXPENSIVE`
- Include the URL and reason in the log
- Inform the user (via remarks or log message)

```python
if should_warn_residential("residential"):
    warn_residential_usage(url)
```

## Ban Detection & Escalation

```python
from src.proxy import ProxyConfig

proxy_config = ProxyConfig.get_instance()

if proxy_config.is_banned(status_code, response_text):
    current_tier = "datacenter"
    escalation = proxy_config.get_escalation_tier()
    next_index = escalation.index(current_tier) + 1
    if next_index < len(escalation):
        next_tier = escalation[next_index]
        logger.warning(f"Proxy blocked ({status_code}), escalating to {next_tier}")
    else:
        logger.error("All proxy tiers exhausted")
```

## Retry with Cooldown

```python
import time

max_retries = proxy_config.get_max_retries(tier)
cooldown = proxy_config.get_cooldown(tier)

for attempt in range(max_retries):
    try:
        response = requests.get(url, proxies=proxy_dict, timeout=30)
        if response.status_code == 200:
            return response
        if proxy_config.is_banned(response.status_code, response.text):
            logger.warning(f"Ban detected on attempt {attempt + 1}/{max_retries}")
            time.sleep(cooldown)
            continue
    except requests.RequestException:
        time.sleep(cooldown)
return None
```

## Rate Limiting with Proxies

| Without Proxy | With Datacenter | With Residential | Delay |
|--------------|---------------|----------------|-------|
| 1-2 seconds | 1 second | 0.5 second | |
| 3-5 seconds | 2 seconds | 1.5 seconds | |

## User-Agent Rotation

Bright Data rotates IPs automatically, but rotate User-Agent headers for additional protection:

```python
from src.proxy import get_random_user_agent

headers = {"User-Agent": get_random_user_agent()}
```

## Important Notes

1. **Config file:** `config/proxy.json` — single source of truth for credentials
2. **Playwright MCP:** Also configured in `opencode.json` — update BOTH if credentials change
3. **Residential cost:** ALWAYS ask before using residential proxies
4. **SSL verify:** Set to `false` for Bright Data proxies (self-signed certs)
5. **Timeout:** Use 30s timeout with proxies (slower than direct requests)
