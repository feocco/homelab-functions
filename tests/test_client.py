import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from homelab.client import HomelabFunctionsError, list_notifications, notify_joe, record_notification_action


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ClientTests(unittest.TestCase):
    def test_requires_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(HomelabFunctionsError, "HOMELAB_FUNCTIONS_TOKEN"):
                notify_joe("Title", "Body")

    @patch("homelab.client.urlopen")
    def test_posts_notify_request(self, urlopen):
        urlopen.return_value = FakeResponse({"status": "sent", "ha_context_id": "ctx"})

        result = notify_joe(
            "Title",
            "Body",
            tag="tag",
            service_url="http://homelab-functions:8080",
            token="secret",
        )

        self.assertEqual(result, {"status": "sent", "ha_context_id": "ctx"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://homelab-functions:8080/v1/notify/joe")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"title": "Title", "message": "Body", "tag": "tag"},
        )

    @patch("homelab.client.urlopen")
    def test_records_notification_action(self, urlopen):
        urlopen.return_value = FakeResponse(
            {"status": "recorded", "notification_id": 12, "action_id": 99}
        )

        result = record_notification_action(
            "HASS_JANITOR_CONFIRM_UPDATE",
            tag="hass-janitor-update-confirm",
            group="hass-janitor",
            reply_text="run it",
            event={"sourceDeviceName": "Pixel"},
            service_url="http://homelab-functions:8080",
            token="secret",
        )

        self.assertEqual(result["notification_id"], 12)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://homelab-functions:8080/v1/notifications/actions",
        )
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "action": "HASS_JANITOR_CONFIRM_UPDATE",
                "tag": "hass-janitor-update-confirm",
                "group": "hass-janitor",
                "reply_text": "run it",
                "event": {"sourceDeviceName": "Pixel"},
            },
        )

    @patch("homelab.client.urlopen")
    def test_lists_notifications(self, urlopen):
        urlopen.return_value = FakeResponse({"notifications": [{"id": 1}]})

        result = list_notifications(
            group="hass-janitor",
            limit=10,
            service_url="http://homelab-functions:8080",
            token="secret",
        )

        self.assertEqual(result, {"notifications": [{"id": 1}]})
        request = urlopen.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            "http://homelab-functions:8080/v1/notifications",
        )
        self.assertEqual(parse_qs(parsed.query), {"group": ["hass-janitor"], "limit": ["10"]})
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
