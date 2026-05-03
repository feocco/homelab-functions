import json
import os
import unittest
from unittest.mock import patch

from homelab.client import HomelabFunctionsError, notify_joe


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
