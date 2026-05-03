import unittest

from aiohttp.test_utils import AioHTTPTestCase

from homelab.server import (
    Config,
    build_service_data,
    create_app,
    split_ha_notify_service,
    validate_notification_payload,
    websocket_url,
)


class FakeHomeAssistantClient:
    def __init__(self):
        self.service_data = None

    async def send_notification(self, service_data):
        self.service_data = service_data
        return "context-123"


class ValidationTests(unittest.TestCase):
    def test_requires_title_and_message(self):
        with self.assertRaisesRegex(ValueError, "title is required"):
            validate_notification_payload({"message": "Body"})

        with self.assertRaisesRegex(ValueError, "message is required"):
            validate_notification_payload({"title": "Title"})

    def test_limits_buttons_to_three(self):
        with self.assertRaisesRegex(ValueError, "at most 3"):
            validate_notification_payload(
                {
                    "title": "Title",
                    "message": "Body",
                    "buttons": [
                        {"title": "One"},
                        {"title": "Two"},
                        {"title": "Three"},
                        {"title": "Four"},
                    ],
                }
            )

    def test_builds_home_assistant_mobile_payload(self):
        notification = validate_notification_payload(
            {
                "title": "Plant status",
                "message": "Ficus needs water.",
                "tag": "plant-ficus",
                "group": "plant-monitor",
                "url": "/lovelace/plants",
                "buttons": [{"title": "Open plants", "uri": "/lovelace/plants"}],
            }
        )

        self.assertEqual(
            build_service_data(notification),
            {
                "title": "Plant status",
                "message": "Ficus needs water.",
                "data": {
                    "tag": "plant-ficus",
                    "group": "plant-monitor",
                    "url": "/lovelace/plants",
                    "clickAction": "/lovelace/plants",
                    "actions": [
                        {
                            "title": "Open plants",
                            "action": "NOTIFY_JOE_OPEN_PLANTS",
                            "uri": "/lovelace/plants",
                        }
                    ],
                },
            },
        )

    def test_splits_notify_service(self):
        self.assertEqual(
            split_ha_notify_service("notify.mobile_app_pixel"),
            ("notify", "mobile_app_pixel"),
        )

    def test_builds_websocket_url(self):
        self.assertEqual(
            websocket_url("https://example.ui.nabu.casa"),
            "wss://example.ui.nabu.casa/api/websocket",
        )


class AppTests(AioHTTPTestCase):
    async def get_application(self):
        self.fake_ha = FakeHomeAssistantClient()
        config = Config(
            ha_url="https://example.ui.nabu.casa",
            ha_long_lived_token="ha-token",
            ha_notify_joe_service="notify.mobile_app_pixel",
            homelab_functions_token="secret",
        )
        return create_app(config, ha_client=self.fake_ha)

    async def test_health(self):
        response = await self.client.request("GET", "/health")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "homelab-functions")

    async def test_notify_requires_bearer_token(self):
        response = await self.client.request(
            "POST",
            "/v1/notify/joe",
            json={"title": "Title", "message": "Body"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    async def test_notify_calls_home_assistant_client(self):
        response = await self.client.request(
            "POST",
            "/v1/notify/joe",
            headers={"Authorization": "Bearer secret"},
            json={"title": "Title", "message": "Body", "tag": "test"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["ha_context_id"], "context-123")
        self.assertEqual(
            self.fake_ha.service_data,
            {
                "title": "Title",
                "message": "Body",
                "data": {"tag": "test"},
            },
        )
