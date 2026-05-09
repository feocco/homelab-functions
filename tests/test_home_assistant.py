import os
import unittest
from unittest.mock import patch

from homelab.home_assistant import HomeAssistantConfig, HomeAssistantError, websocket_url


class HomeAssistantHelperTests(unittest.TestCase):
    def test_builds_websocket_url(self):
        self.assertEqual(
            websocket_url("https://example.ui.nabu.casa"),
            "wss://example.ui.nabu.casa/api/websocket",
        )
        self.assertEqual(
            websocket_url("http://homeassistant.local:8123"),
            "ws://homeassistant.local:8123/api/websocket",
        )
        self.assertEqual(
            websocket_url("wss://example.ui.nabu.casa/api/websocket"),
            "wss://example.ui.nabu.casa/api/websocket",
        )

    def test_rejects_unsupported_url_scheme(self):
        with self.assertRaisesRegex(HomeAssistantError, "HA_URL"):
            websocket_url("ftp://example")

    def test_config_from_env_requires_home_assistant_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(HomeAssistantError, "HA_URL, HA_LONG_LIVED_TOKEN"):
                HomeAssistantConfig.from_env()

    def test_config_from_env_reads_home_assistant_values(self):
        with patch.dict(
            os.environ,
            {
                "HA_URL": "https://example.ui.nabu.casa",
                "HA_LONG_LIVED_TOKEN": "token",
                "HA_REQUEST_TIMEOUT_SECONDS": "12.5",
            },
            clear=True,
        ):
            config = HomeAssistantConfig.from_env()

        self.assertEqual(config.ha_url, "https://example.ui.nabu.casa")
        self.assertEqual(config.ha_long_lived_token, "token")
        self.assertEqual(config.request_timeout_seconds, 12.5)
