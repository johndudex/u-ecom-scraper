import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.proxy import (
    ProxyConfig,
    build_proxy_url,
    get_proxy_config,
    get_random_user_agent,
    should_warn_residential,
    warn_residential_usage,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    ProxyConfig._instance = None
    yield
    ProxyConfig._instance = None


@pytest.fixture()
def proxy_config(tmp_path):
    config_data = {
        "provider": "bright_data",
        "datacenter": {
            "host": "brd.superproxy.io",
            "port": 33335,
            "username": "test-dc-user",
            "password": "test-dc-pass",
        },
        "residential": {
            "host": "brd.superproxy.io",
            "port": 33335,
            "username": "test-res-user",
            "password": "test-res-pass",
            "cost_warning": "Residential proxies are EXPENSIVE.",
        },
        "strategy": {
            "default": "none",
            "escalation": ["datacenter", "residential"],
            "datacenter_max_retries": 3,
            "residential_max_retries": 2,
            "ban_status_codes": [403, 503, 429],
            "ban_text_markers": ["captcha", "robot check", "blocked"],
            "cooldown_seconds": {"datacenter": 10, "residential": 30},
            "ssl_verify": False,
            "request_timeout": 30,
            "session_retry_delay": 5,
            "user_agent_rotation": True,
        },
    }
    config_file = tmp_path / "proxy.json"
    config_file.write_text(json.dumps(config_data))
    config = ProxyConfig.get_instance(str(config_file))
    return config


class TestProxyConfigLoading:
    def test_loads_valid_config(self, proxy_config):
        assert proxy_config.config["provider"] == "bright_data"
        assert proxy_config.config["datacenter"]["username"] == "test-dc-user"

    def test_missing_file_returns_defaults(self):
        config = ProxyConfig("/nonexistent/path/proxy.json")
        assert config.config["provider"] == "none"
        assert config.get_default_mode() == "none"

    def test_invalid_json_returns_none_provider(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid")
        config = ProxyConfig(str(bad_file))
        assert config.config["provider"] == "none"

    def test_singleton_returns_same_instance(self, tmp_path):
        config_data = {"provider": "test"}
        config_file = tmp_path / "proxy.json"
        config_file.write_text(json.dumps(config_data))
        a = ProxyConfig.get_instance(str(config_file))
        b = ProxyConfig.get_instance(str(config_file))
        assert a is b

    def test_reload_clears_singleton(self, proxy_config):
        assert ProxyConfig._instance is not None
        proxy_config.reload()
        assert ProxyConfig._instance is None


class TestGetProxyDict:
    def test_datacenter_proxy_dict(self, proxy_config):
        result = proxy_config.get_proxy_dict("datacenter")
        assert result is not None
        assert "http" in result
        assert "https" in result
        assert "test-dc-user" in result["http"]
        assert "test-dc-pass" in result["http"]
        assert "brd.superproxy.io:33335" in result["http"]

    def test_residential_proxy_dict(self, proxy_config):
        result = proxy_config.get_proxy_dict("residential")
        assert result is not None
        assert "test-res-user" in result["http"]

    def test_missing_host_returns_none(self, proxy_config):
        proxy_config.config["datacenter"]["host"] = ""
        result = proxy_config.get_proxy_dict("datacenter")
        assert result is None

    def test_missing_username_returns_none(self, proxy_config):
        proxy_config.config["datacenter"]["username"] = ""
        result = proxy_config.get_proxy_dict("datacenter")
        assert result is None


class TestBanDetection:
    def test_ban_by_status_code(self, proxy_config):
        assert proxy_config.is_banned(403, "") is True
        assert proxy_config.is_banned(503, "") is True
        assert proxy_config.is_banned(429, "") is True

    def test_no_ban_ok_status(self, proxy_config):
        assert proxy_config.is_banned(200, "all good") is False
        assert proxy_config.is_banned(301, "") is False

    def test_ban_by_text_marker(self, proxy_config):
        assert proxy_config.is_banned(200, "Please solve this CAPTCHA") is True
        assert proxy_config.is_banned(200, "robot check detected") is True
        assert proxy_config.is_banned(200, "Your access has been blocked") is True

    def test_no_ban_normal_text(self, proxy_config):
        assert proxy_config.is_banned(200, "Welcome to our store") is False


class TestEscalation:
    def test_escalation_order(self, proxy_config):
        tiers = proxy_config.get_escalation_tier()
        assert tiers == ["datacenter", "residential"]

    def test_max_retries_datacenter(self, proxy_config):
        assert proxy_config.get_max_retries("datacenter") == 3

    def test_max_retries_residential(self, proxy_config):
        assert proxy_config.get_max_retries("residential") == 2

    def test_cooldown_datacenter(self, proxy_config):
        assert proxy_config.get_cooldown("datacenter") == 10

    def test_cooldown_residential(self, proxy_config):
        assert proxy_config.get_cooldown("residential") == 30

    def test_cooldown_unknown_tier(self, proxy_config):
        assert proxy_config.get_cooldown("unknown") == 10


class TestConfigAccessors:
    def test_default_mode(self, proxy_config):
        assert proxy_config.get_default_mode() == "none"

    def test_timeout(self, proxy_config):
        assert proxy_config.get_timeout() == 30

    def test_retry_delay(self, proxy_config):
        assert proxy_config.get_retry_delay() == 5

    def test_ssl_verify(self, proxy_config):
        kwargs = proxy_config.get_requests_session_kwargs()
        assert kwargs["verify"] is False

    def test_residential_is_expensive(self, proxy_config):
        is_expensive, warning = proxy_config.is_residential_expensive()
        assert is_expensive is True
        assert "EXPENSIVE" in warning


class TestStandaloneFunctions:
    def test_build_proxy_url(self, proxy_config):
        url = build_proxy_url("datacenter", proxy_config)
        assert url == "http://test-dc-user:test-dc-pass@brd.superproxy.io:33335"

    def test_build_proxy_url_missing_host(self, proxy_config):
        proxy_config.config["datacenter"]["host"] = ""
        url = build_proxy_url("datacenter", proxy_config)
        assert url is None

    def test_should_warn_residential_true(self, proxy_config):
        assert should_warn_residential("residential", proxy_config) is True

    def test_should_warn_residential_false_non_residential(self, proxy_config):
        assert should_warn_residential("datacenter", proxy_config) is False

    def test_warn_residential_usage_logs_warning(self, proxy_config):
        with patch("src.proxy.logger") as mock_logger:
            warn_residential_usage("https://example.com", proxy_config)
            mock_logger.warning.assert_called()
            log_messages = [str(call) for call in mock_logger.warning.call_args_list]
            assert any("EXPENSIVE" in m for m in log_messages)

    def test_get_random_user_agent(self):
        ua = get_random_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 0
        assert "Mozilla" in ua

    def test_get_random_user_agent_variety(self):
        agents = set()
        for _ in range(100):
            agents.add(get_random_user_agent())
        assert len(agents) > 1

    def test_warn_residential_usage_no_config(self):
        with patch("src.proxy.get_proxy_config") as mock_get:
            mock_config = MagicMock()
            mock_config.is_residential_expensive.return_value = (True, "Warning!")
            mock_get.return_value = mock_config
            warn_residential_usage("https://test.com")
            mock_get.assert_called_once()
