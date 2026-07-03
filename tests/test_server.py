import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp.test_utils import AioHTTPTestCase

from homelab.server import (
    Config,
    build_service_data,
    create_app,
    split_ha_notify_service,
    validate_notification_payload,
    validate_workflow_report_payload,
    websocket_url,
)


class FakeHomeAssistantClient:
    def __init__(self):
        self.service_data = None
        self.notify_service = None

    async def send_notification(self, service_data, *, notify_service):
        self.service_data = service_data
        self.notify_service = notify_service
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

    def test_builds_text_input_action_payload(self):
        notification = validate_notification_payload(
            {
                "title": "Bedtime follow-up",
                "message": "Why skipped?",
                "buttons": [
                    {
                        "title": "Reply",
                        "action": "BEDTIME_MISSED_REASON::token",
                        "behavior": "textInput",
                        "textInputButtonTitle": "Send",
                        "textInputPlaceholder": "Why didn't you use it?",
                    }
                ],
            }
        )

        self.assertEqual(
            build_service_data(notification)["data"]["actions"],
            [
                {
                    "title": "Reply",
                    "action": "BEDTIME_MISSED_REASON::token",
                    "behavior": "textInput",
                    "textInputButtonTitle": "Send",
                    "textInputPlaceholder": "Why didn't you use it?",
                }
            ],
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

    def test_validates_workflow_report_payload(self):
        report = validate_workflow_report_payload(
            {
                "workflow_slug": "cat-food-monitor",
                "summary": "The morning check did not run.",
                "source": "mobile-action",
                "notification_id": 12,
                "event": {"sourceDeviceName": "Pixel"},
            }
        )

        self.assertEqual(
            report,
            {
                "workflow_slug": "cat-food-monitor",
                "summary": "The morning check did not run.",
                "source": "mobile-action",
                "notification_id": 12,
                "event": {"sourceDeviceName": "Pixel"},
            },
        )

    def test_requires_workflow_report_slug_and_summary(self):
        with self.assertRaisesRegex(ValueError, "workflow_slug is required"):
            validate_workflow_report_payload({"summary": "Broken"})

        with self.assertRaisesRegex(ValueError, "summary is required"):
            validate_workflow_report_payload({"workflow_slug": "cat-food-monitor"})


class AppTests(AioHTTPTestCase):
    async def get_application(self):
        self.fake_ha = FakeHomeAssistantClient()
        self.tmpdir = TemporaryDirectory()
        self.catalog_path = Path(self.tmpdir.name) / "service-catalog.json"
        self.smoke_targets_path = Path(self.tmpdir.name) / "smoke-signal-targets.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "schema": "homelab-service-catalog.v1",
                    "services": [{"service": "grafana", "host": "macmini", "enabled": True}],
                }
            ),
            encoding="utf-8",
        )
        self.smoke_targets_path.write_text(
            json.dumps(
                {
                    "schema": "homelab-smoke-signal-targets.v1",
                    "targets": [{"name": "Grafana", "url": "http://grafana.local/api/health"}],
                }
            ),
            encoding="utf-8",
        )
        config = Config(
            ha_url="https://example.ui.nabu.casa",
            ha_long_lived_token="ha-token",
            ha_notify_joe_service="notify.mobile_app_pixel",
            ha_notify_jess_service="notify.mobile_app_jwellz2",
            homelab_functions_token="secret",
            homelab_catalog_path=str(self.catalog_path),
            homelab_smoke_signal_targets_path=str(self.smoke_targets_path),
            notification_ledger_path=str(Path(self.tmpdir.name) / "notifications.sqlite3"),
            notification_action_recorder_enabled=False,
        )
        return create_app(config, ha_client=self.fake_ha)

    async def tearDownAsync(self):
        await super().tearDownAsync()
        self.tmpdir.cleanup()

    async def test_health(self):
        response = await self.client.request("GET", "/health")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "homelab-functions")

    async def test_docs_page(self):
        response = await self.client.request("GET", "/docs")
        body = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("homelab-functions", body)
        self.assertIn("/openapi.json", body)
        self.assertIn("Authorization: Bearer $HOMELAB_FUNCTIONS_TOKEN", body)
        self.assertIn("does not store tokens", body)

    async def test_openapi_document(self):
        response = await self.client.request("GET", "/openapi.json")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["openapi"], "3.1.0")
        self.assertEqual(payload["info"]["title"], "homelab-functions")
        self.assertIn("/health", payload["paths"])
        self.assertIn("/v1/notify/joe", payload["paths"])
        self.assertIn("/v1/catalog/services", payload["paths"])
        self.assertIn("/v1/catalog/smoke-signal-targets", payload["paths"])
        self.assertIn("/v1/workflow-reports/{report_id}", payload["paths"])
        self.assertEqual(
            payload["paths"]["/v1/notify/joe"]["post"]["security"],
            [{"bearerAuth": []}],
        )
        self.assertIn("bearerAuth", payload["components"]["securitySchemes"])

    async def test_catalog_services_requires_bearer_token(self):
        response = await self.client.request("GET", "/v1/catalog/services")
        payload = await response.json()

        self.assertEqual(response.status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    async def test_catalog_services_returns_generated_catalog(self):
        response = await self.client.request(
            "GET",
            "/v1/catalog/services",
            headers={"Authorization": "Bearer secret"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["schema"], "homelab-service-catalog.v1")
        self.assertEqual(payload["services"][0]["service"], "grafana")

    async def test_smoke_signal_targets_returns_generated_targets(self):
        response = await self.client.request(
            "GET",
            "/v1/catalog/smoke-signal-targets",
            headers={"Authorization": "Bearer secret"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["schema"], "homelab-smoke-signal-targets.v1")
        self.assertEqual(payload["targets"][0]["name"], "Grafana")

    async def test_catalog_response_handles_missing_file(self):
        self.catalog_path.unlink()

        response = await self.client.request(
            "GET",
            "/v1/catalog/services",
            headers={"Authorization": "Bearer secret"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 503)
        self.assertEqual(payload["error"]["code"], "catalog_unavailable")

    async def test_catalog_response_handles_malformed_json(self):
        self.smoke_targets_path.write_text("{not-json", encoding="utf-8")

        response = await self.client.request(
            "GET",
            "/v1/catalog/smoke-signal-targets",
            headers={"Authorization": "Bearer secret"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 503)
        self.assertEqual(payload["error"]["code"], "catalog_unavailable")

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
        self.assertEqual(self.fake_ha.notify_service, "notify.mobile_app_pixel")

    async def test_notify_jess_calls_jess_home_assistant_service(self):
        response = await self.client.request(
            "POST",
            "/v1/notify/jess",
            headers={"Authorization": "Bearer secret"},
            json={"title": "Dinner plan", "message": "Please review.", "group": "mealie-planner"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(self.fake_ha.notify_service, "notify.mobile_app_jwellz2")
        self.assertEqual(
            self.fake_ha.service_data,
            {
                "title": "Dinner plan",
                "message": "Please review.",
                "data": {"group": "mealie-planner"},
            },
        )

    async def test_notify_records_sent_notification(self):
        response = await self.client.request(
            "POST",
            "/v1/notify/joe",
            headers={"Authorization": "Bearer secret"},
            json={
                "title": "Updates available",
                "message": "Two updates need review.",
                "tag": "hass-janitor-update-confirm",
                "group": "hass-janitor",
                "buttons": [{"title": "Update now", "action": "HASS_JANITOR_CONFIRM_UPDATE"}],
            },
        )
        sent_payload = await response.json()

        history_response = await self.client.request(
            "GET",
            "/v1/notifications?group=hass-janitor",
            headers={"Authorization": "Bearer secret"},
        )
        history_payload = await history_response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(history_response.status, 200)
        self.assertIsInstance(sent_payload["notification_id"], int)
        self.assertEqual(len(history_payload["notifications"]), 1)
        record = history_payload["notifications"][0]
        self.assertEqual(record["id"], sent_payload["notification_id"])
        self.assertEqual(record["title"], "Updates available")
        self.assertEqual(record["tag"], "hass-janitor-update-confirm")
        self.assertEqual(record["group"], "hass-janitor")
        self.assertEqual(record["status"], "sent")
        self.assertEqual(record["ha_context_id"], "context-123")
        self.assertEqual(record["actions"], [])

    async def test_records_notification_action_and_marks_latest_matching_notification(self):
        notify_response = await self.client.request(
            "POST",
            "/v1/notify/joe",
            headers={"Authorization": "Bearer secret"},
            json={
                "title": "Updates available",
                "message": "Two updates need review.",
                "tag": "hass-janitor-update-confirm",
                "group": "hass-janitor",
            },
        )
        notify_payload = await notify_response.json()

        action_response = await self.client.request(
            "POST",
            "/v1/notifications/actions",
            headers={"Authorization": "Bearer secret"},
            json={
                "action": "HASS_JANITOR_CONFIRM_UPDATE",
                "tag": "hass-janitor-update-confirm",
                "group": "hass-janitor",
                "reply_text": "run it",
                "event": {"sourceDeviceName": "Pixel"},
            },
        )
        action_payload = await action_response.json()

        history_response = await self.client.request(
            "GET",
            "/v1/notifications?group=hass-janitor",
            headers={"Authorization": "Bearer secret"},
        )
        history_payload = await history_response.json()

        self.assertEqual(action_response.status, 200)
        self.assertEqual(action_payload["notification_id"], notify_payload["notification_id"])
        record = history_payload["notifications"][0]
        self.assertEqual(record["status"], "responded")
        self.assertEqual(len(record["actions"]), 1)
        self.assertEqual(record["actions"][0]["action"], "HASS_JANITOR_CONFIRM_UPDATE")
        self.assertEqual(record["actions"][0]["reply_text"], "run it")

    async def test_workflow_report_requires_bearer_token(self):
        response = await self.client.request(
            "POST",
            "/v1/workflow-reports",
            json={"workflow_slug": "cat-food-monitor", "summary": "Broken"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    async def test_records_workflow_report(self):
        response = await self.client.request(
            "POST",
            "/v1/workflow-reports",
            headers={"Authorization": "Bearer secret"},
            json={
                "workflow_slug": "cat-food-monitor",
                "summary": "Bowl sensor stayed empty after refill.",
                "source": "mobile-action",
                "notification_id": 42,
                "event": {"sourceDeviceName": "Pixel"},
            },
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "reported")
        self.assertIsInstance(payload["report_id"], int)
        self.assertEqual(payload["report"]["workflow_slug"], "cat-food-monitor")
        self.assertEqual(payload["report"]["summary"], "Bowl sensor stayed empty after refill.")
        self.assertEqual(payload["report"]["source"], "mobile-action")
        self.assertEqual(payload["report"]["notification_id"], 42)
        self.assertEqual(payload["report"]["event"], {"sourceDeviceName": "Pixel"})

    async def test_workflow_report_rejects_invalid_request(self):
        response = await self.client.request(
            "POST",
            "/v1/workflow-reports",
            headers={"Authorization": "Bearer secret"},
            json={"workflow_slug": "cat-food-monitor"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["detail"], "summary")

    async def test_lists_workflow_reports_by_workflow(self):
        await self.client.request(
            "POST",
            "/v1/workflow-reports",
            headers={"Authorization": "Bearer secret"},
            json={"workflow_slug": "cat-food-monitor", "summary": "First issue"},
        )
        await self.client.request(
            "POST",
            "/v1/workflow-reports",
            headers={"Authorization": "Bearer secret"},
            json={"workflow_slug": "plant-monitor", "summary": "Other issue"},
        )

        response = await self.client.request(
            "GET",
            "/v1/workflow-reports?workflow=cat-food-monitor&limit=20",
            headers={"Authorization": "Bearer secret"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(len(payload["reports"]), 1)
        self.assertEqual(payload["reports"][0]["workflow_slug"], "cat-food-monitor")
        self.assertEqual(payload["reports"][0]["summary"], "First issue")

    async def test_gets_workflow_report_by_id(self):
        create_response = await self.client.request(
            "POST",
            "/v1/workflow-reports",
            headers={"Authorization": "Bearer secret"},
            json={"workflow_slug": "cat-food-monitor", "summary": "Needs investigation"},
        )
        create_payload = await create_response.json()

        response = await self.client.request(
            "GET",
            f"/v1/workflow-reports/{create_payload['report_id']}",
            headers={"Authorization": "Bearer secret"},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["report"]["id"], create_payload["report_id"])
        self.assertEqual(payload["report"]["summary"], "Needs investigation")
