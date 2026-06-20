import asyncio
import os
import unittest
from unittest.mock import patch

from homelab.home_assistant import (
    HomeAssistantConfig,
    HomeAssistantError,
    HomeAssistantWebSocketClient,
    websocket_url,
)
from homelab.notification_ledger import NotificationLedger


class NotificationActionRecorderTests(unittest.TestCase):
    def test_records_mobile_notification_action_event(self):
        from homelab.action_recorder import record_notification_action_event

        with self.subTest("synthetic mobile action is attached to latest matching notification"):
            from tempfile import TemporaryDirectory
            from pathlib import Path

            with TemporaryDirectory() as tmpdir:
                ledger = NotificationLedger(str(Path(tmpdir) / "notifications.sqlite3"))
                notification = ledger.record_sent(
                    {
                        "title": "Updates available",
                        "message": "One update needs review.",
                        "tag": "hass-janitor-update-confirm",
                        "group": "hass-janitor",
                    },
                    {"title": "Updates available", "message": "One update needs review."},
                    ha_context_id="context-123",
                )

                result = record_notification_action_event(
                    ledger,
                    {
                        "event_type": "mobile_app_notification_action",
                        "data": {
                            "action": "HASS_JANITOR_CONFIRM_UPDATE::token",
                            "tag": "hass-janitor-update-confirm",
                            "group": "hass-janitor",
                            "sourceDeviceName": "Pixel",
                        },
                    },
                )

                record = ledger.get_notification(notification["id"])

        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["notification_id"], notification["id"])
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "responded")
        self.assertEqual(record["actions"][0]["action"], "HASS_JANITOR_CONFIRM_UPDATE::token")
        self.assertEqual(record["actions"][0]["event"]["sourceDeviceName"], "Pixel")


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

    def test_websocket_client_exposes_wait_closed(self):
        client = HomeAssistantWebSocketClient(
            HomeAssistantConfig(
                ha_url="https://example.ui.nabu.casa",
                ha_long_lived_token="token",
            )
        )

        self.assertTrue(callable(client.wait_closed))


class FakeSession:
    def __init__(self, websocket=None, error=None, **kwargs):
        self.websocket = websocket
        self.error = error
        self.closed = False

    async def ws_connect(self, url):
        if self.error:
            raise self.error
        return self.websocket

    async def close(self):
        self.closed = True


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def receive_json(self):
        return self.messages.pop(0)

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class HomeAssistantWebSocketClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        return HomeAssistantWebSocketClient(
            HomeAssistantConfig(
                ha_url="https://example.ui.nabu.casa",
                ha_long_lived_token="token",
            )
        )

    async def test_connect_closes_session_when_websocket_connect_fails(self):
        session = FakeSession(error=RuntimeError("connect failed"))
        client = self.make_client()

        with patch("homelab.home_assistant.ClientSession", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                await client.connect()

        self.assertTrue(session.closed)
        self.assertIsNone(client._session)
        self.assertIsNone(client._ws)

    async def test_connect_closes_websocket_and_session_when_auth_fails(self):
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_invalid", "message": "bad token"},
            ]
        )
        session = FakeSession(websocket=websocket)
        client = self.make_client()

        with patch("homelab.home_assistant.ClientSession", return_value=session):
            with self.assertRaisesRegex(HomeAssistantError, "bad token"):
                await client.connect()

        self.assertTrue(websocket.closed)
        self.assertTrue(session.closed)
        self.assertIsNone(client._session)
        self.assertIsNone(client._ws)

    async def test_event_handler_can_make_request_on_same_websocket(self):
        websocket = FakeWebSocket([])
        client = self.make_client()
        client._ws = websocket
        handler_results = []

        async def handler(_event):
            handler_results.append(await client.call_service("script", "turn_on", {"entity_id": "script.test"}))

        client.add_event_handler(handler)

        client._dispatch_event({"event_type": "mobile_app_notification_action"})
        while not websocket.sent:
            await asyncio.sleep(0)
        client._finish_pending({"type": "result", "id": 1, "success": True, "result": {"ok": True}})
        await asyncio.gather(*client._event_tasks)

        self.assertEqual(handler_results, [{"ok": True}])
        self.assertIn(
            {
                "id": 1,
                "type": "call_service",
                "domain": "script",
                "service": "turn_on",
                "service_data": {"entity_id": "script.test"},
            },
            websocket.sent,
        )
