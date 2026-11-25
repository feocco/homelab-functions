import ast
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from homelab.credits import (
    BODY_LIMIT,
    CreditSnapshot,
    is_credit_error,
    parse_credit_report,
    render_push,
)
from homelab.notification_ledger import NotificationLedger
from homelab.pebble import XaiSpeechTranscriber
from homelab.server import Config, HomeAssistantError, create_app


TOKEN = "credit-token-do-not-leak"
JOE_SERVICE = "notify.mobile_app_pixel"
REDACTED_PREFIX = (
    '{"code":"The caller does not have permission to execute the specified operation","error":"Your team 3088e1fd-'
)
PUBLIC_BODY = (
    "has either used all available credits or reached its monthly spending limit. "
    "To continue making API requests, please purchase more credits or raise your spending limit."
)
CREDIT_BODY = REDACTED_PREFIX + PUBLIC_BODY
PERMISSION_ONLY = "The caller does not have permission to execute the specified operation"
XAI_OUT = (
    "Balance $0.00. Pirate Radio conversions are paused, and Pebble voice notes "
    "and Home Assistant Grok voice will fail. Add credits in the xAI console; "
    "I'll tell you when it's working again."
)
XAI_OUT_UNKNOWN = (
    "Pirate Radio conversions are paused. Add credits in the xAI console; "
    "I'll tell you when it's working again."
)
XAI_LOW = (
    "$3.40 left, about 2 days at your recent use ($1.60/day). "
    "At $0, Pirate Radio conversions pause."
)
XAI_RECOVERED = "Balance $20.00. Pirate Radio conversions resumed."
XAI_REMINDER = (
    "Pirate Radio conversions are paused. 3 credit errors since it ran out. "
    "Add credits in the xAI console; I'll tell you when it's working again."
)
XAI_REMINDER_THREE = (
    "Pirate Radio conversions are paused, and Pebble voice notes and "
    "Home Assistant Grok voice will fail. 4 credit errors since it ran out. "
    "Add credits in the xAI console; I'll tell you when it's working again."
)
T0 = "2026-11-20T15:04:00+00:00"
T1 = "2026-11-20T15:05:00+00:00"
T2 = "2026-11-20T15:06:00+00:00"
T24 = "2026-11-21T15:04:00+00:00"
T24_PLUS = "2026-11-21T15:05:00+00:00"


class FakeHomeAssistant:
    def __init__(self, *, fail_first: bool = False, timeout_first: bool = False):
        self.calls = []
        self.fail_first = fail_first
        self.timeout_first = timeout_first
        self.failures = 0

    async def send_notification(self, service_data, *, notify_service):
        if self.fail_first and self.failures == 0:
            self.failures += 1
            raise HomeAssistantError(f"notify down {TOKEN}")
        if self.timeout_first and self.failures == 0:
            self.failures += 1
            raise TimeoutError(TOKEN)
        self.calls.append(
            {
                "notify_service": notify_service,
                "service_data": json.loads(json.dumps(service_data)),
            }
        )
        return "context-credits"


def write_config(tmp: Path) -> Config:
    catalog = tmp / "catalog.json"
    targets = tmp / "targets.json"
    catalog.write_text("{}", encoding="utf-8")
    targets.write_text("{}", encoding="utf-8")
    return Config(
        ha_url="https://example.ui.nabu.casa",
        ha_long_lived_token="ha-token",
        ha_notify_joe_service=JOE_SERVICE,
        ha_notify_jess_service="notify.mobile_app_jess",
        homelab_functions_token=TOKEN,
        notification_ledger_path=str(tmp / "notifications.sqlite3"),
        notification_action_recorder_enabled=False,
        homelab_catalog_path=str(catalog),
        homelab_smoke_signal_targets_path=str(targets),
        xai_api_key="xai-test-key",
        pebble_ring_token="pebble-ring-token",
        grok_pebble_webhook_key="webhook-key",
        grok_pebble_work_webhook_url="https://work.example/hook",
        grok_pebble_work_webhook_key="work-key",
        pebble_ledger_path=str(tmp / "pebble.sqlite3"),
        pebble_audio_dir=str(tmp / "audio"),
    )


class CreditHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.fake = FakeHomeAssistant()
        self.client = TestClient(
            TestServer(create_app(write_config(Path(self.tmp.name)), ha_client=self.fake))
        )
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def report(self, payload, *, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return await self.client.post("/v1/credits/report", json=payload, headers=headers)

    async def status(self, *, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return await self.client.get("/v1/credits/status", headers=headers)

    def push(self, index=0):
        return self.fake.calls[index]["service_data"]

    async def test_xai_credit_body_dedupes_reminds_and_recovers(self):
        first = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": CREDIT_BODY,
                "observed_at": T0,
            }
        )
        self.assertEqual(first.status, 200)
        body = await first.json()
        self.assertTrue(body["classified"])
        self.assertEqual(body["previous_state"], "ok")
        self.assertEqual(body["state"], "out")
        self.assertEqual(body["push_kind"], "out")
        self.assertEqual(body["error_count"], 1)
        self.assertEqual(self.push()["title"], "xAI is out of credits")
        self.assertEqual(self.push()["message"], XAI_OUT_UNKNOWN)
        self.assertEqual(self.push()["data"]["group"], "api-credits")
        self.assertEqual(self.push()["data"]["tag"], "api-credits-xai")
        self.assertEqual(self.push()["data"]["ttl"], 0)
        self.assertEqual(self.push()["data"]["priority"], "high")
        self.assertEqual(
            self.push()["data"]["actions"],
            [{"title": "Open billing", "action": "URI", "uri": "https://console.x.ai"}],
        )

        second = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 429,
                "body": CREDIT_BODY,
                "observed_at": T1,
            }
        )
        self.assertEqual(second.status, 200)
        second_body = await second.json()
        self.assertEqual(second_body["state"], "out")
        self.assertFalse(second_body["pushed"])
        self.assertEqual(second_body["error_count"], 2)
        self.assertEqual(len(self.fake.calls), 1)

        reminder = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": CREDIT_BODY,
                "observed_at": T24,
            }
        )
        self.assertEqual(reminder.status, 200)
        reminder_body = await reminder.json()
        self.assertEqual(reminder_body["push_kind"], "reminder")
        self.assertEqual(reminder_body["error_count"], 3)
        self.assertEqual(self.push(1)["title"], "xAI is still out of credits")
        self.assertEqual(self.push(1)["message"], XAI_REMINDER)

        quiet = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": CREDIT_BODY,
                "observed_at": T24_PLUS,
            }
        )
        self.assertFalse((await quiet.json())["pushed"])
        self.assertEqual(len(self.fake.calls), 2)

        recovered = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "success",
                "balance_usd": 20,
                "observed_at": "2026-11-21T16:00:00+00:00",
            }
        )
        self.assertEqual(recovered.status, 200)
        recovered_body = await recovered.json()
        self.assertEqual(recovered_body["state"], "ok")
        self.assertEqual(recovered_body["push_kind"], "recovered")
        self.assertEqual(self.push(2)["title"], "xAI credits are back")
        self.assertEqual(self.push(2)["message"], XAI_RECOVERED)

    async def test_reminder_names_every_service_seen_while_out(self):
        first = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "used all available credits",
                "observed_at": T0,
            }
        )
        self.assertEqual(self.push()["message"], XAI_OUT_UNKNOWN)
        self.assertEqual((await first.json())["error_count"], 1)

        for service, when in (("pebble", T1), ("home-assistant", T2)):
            response = await self.report(
                {
                    "provider": "xai",
                    "service": service,
                    "kind": "error",
                    "status": 403,
                    "body": "used all available credits",
                    "observed_at": when,
                }
            )
            self.assertFalse((await response.json())["pushed"])
        self.assertEqual(len(self.fake.calls), 1)

        reminder = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "used all available credits",
                "observed_at": T24,
            }
        )
        body = await reminder.json()
        self.assertEqual(body["push_kind"], "reminder")
        self.assertEqual(body["error_count"], 4)
        self.assertEqual(body["services"], ["pirate-radio", "pebble", "home-assistant"])
        self.assertEqual(self.push(1)["title"], "xAI is still out of credits")
        self.assertEqual(self.push(1)["message"], XAI_REMINDER_THREE)
        self.assertEqual(len(self.fake.calls), 2)

    async def test_balance_zero_after_low_names_every_service_seen(self):
        low = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "balance",
                "balance_usd": 3.4,
                "daily_burn_usd": 1.6,
                "observed_at": T0,
            }
        )
        self.assertEqual(low.status, 200)
        self.assertEqual((await low.json())["state"], "low")
        self.assertEqual(self.push()["title"], "xAI credits running low")
        self.assertEqual(self.push()["message"], XAI_LOW)

        for service, when in (("pebble", T1), ("home-assistant", T2)):
            response = await self.report(
                {
                    "provider": "xai",
                    "service": service,
                    "kind": "balance",
                    "balance_usd": 3.4,
                    "observed_at": when,
                }
            )
            self.assertFalse((await response.json())["pushed"])
        self.assertEqual(len(self.fake.calls), 1)

        out = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "balance",
                "balance_usd": 0,
                "observed_at": "2026-11-20T15:07:00+00:00",
            }
        )
        self.assertEqual((await out.json())["push_kind"], "out")
        self.assertEqual(self.push(1)["title"], "xAI is out of credits")
        self.assertEqual(self.push(1)["message"], XAI_OUT)
        self.assertEqual(len(self.fake.calls), 2)

    async def test_low_hysteresis_and_balance_recovery(self):
        await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "balance",
                "balance_usd": 3.4,
                "daily_burn_usd": 1.6,
                "observed_at": T0,
            }
        )
        held = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "balance",
                "balance_usd": 5.5,
                "observed_at": T1,
            }
        )
        held_body = await held.json()
        self.assertEqual(held_body["state"], "low")
        self.assertFalse(held_body["pushed"])
        recovered = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "balance",
                "balance_usd": 6,
                "observed_at": T2,
            }
        )
        self.assertEqual((await recovered.json())["state"], "ok")
        self.assertEqual(self.push(1)["message"], "Balance $6.00. Pirate Radio conversions resumed.")

    async def test_x_floor_is_two_dollars(self):
        at_floor = await self.report(
            {
                "provider": "x",
                "service": "x-api",
                "kind": "balance",
                "balance_usd": 2,
                "observed_at": T0,
            }
        )
        self.assertEqual((await at_floor.json())["state"], "ok")
        self.assertEqual(self.fake.calls, [])
        below = await self.report(
            {
                "provider": "x",
                "service": "x-api",
                "kind": "balance",
                "balance_usd": 1.5,
                "observed_at": T1,
            }
        )
        self.assertEqual((await below.json())["state"], "low")
        self.assertEqual(self.push()["title"], "X credits running low")
        self.assertEqual(
            self.push()["data"]["actions"],
            [{"title": "Open billing", "action": "URI", "uri": "https://console.x.com"}],
        )

    async def test_failed_notify_is_retried_once(self):
        await self.client.close()
        self.fake = FakeHomeAssistant(fail_first=True)
        self.client = TestClient(
            TestServer(create_app(write_config(Path(self.tmp.name)), ha_client=self.fake))
        )
        await self.client.start_server()
        failed = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "used all available credits",
                "observed_at": T0,
            }
        )
        self.assertEqual(failed.status, 502)
        self.assertNotIn(TOKEN, json.dumps(await failed.json()))
        self.assertEqual(self.fake.calls, [])
        current = await self.status()
        provider = (await current.json())["providers"][0]
        self.assertEqual(provider["state"], "out")
        self.assertIsNone(provider["last_push_at"])
        retried = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "used all available credits",
                "observed_at": T1,
            }
        )
        self.assertEqual(retried.status, 200)
        self.assertTrue((await retried.json())["pushed"])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.push()["message"], XAI_OUT_UNKNOWN)

    async def test_notify_timeout_is_retried_once(self):
        await self.client.close()
        self.fake = FakeHomeAssistant(timeout_first=True)
        self.client = TestClient(
            TestServer(create_app(write_config(Path(self.tmp.name)), ha_client=self.fake))
        )
        await self.client.start_server()
        timed_out = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "used all available credits",
                "observed_at": T0,
            }
        )
        self.assertEqual(timed_out.status, 504)
        self.assertNotIn(TOKEN, json.dumps(await timed_out.json()))
        self.assertEqual(self.fake.calls, [])
        retried = await self.report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "used all available credits",
                "observed_at": T1,
            }
        )
        self.assertEqual(retried.status, 200)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.push()["message"], XAI_OUT_UNKNOWN)

    async def test_missing_token_and_bad_provider(self):
        missing = await self.report(
            {"provider": "xai", "service": "pirate-radio", "kind": "error"},
            token=None,
        )
        self.assertEqual(missing.status, 401)
        unauthenticated = await self.status(token=None)
        self.assertEqual(unauthenticated.status, 401)
        rejected = await self.report(
            {"provider": "anthropic", "service": "pirate-radio", "kind": "error"}
        )
        self.assertEqual(rejected.status, 400)
        self.assertEqual((await rejected.json())["error"]["code"], "invalid_request")

    async def test_fresh_balance_recovery_from_out(self):
        await self.report(
            {
                "provider": "openai",
                "service": "openai-proxy",
                "kind": "error",
                "status": 429,
                "body": '{"error":{"code":"insufficient_quota"}}',
                "observed_at": T0,
            }
        )
        recovered = await self.report(
            {
                "provider": "openai",
                "service": "openai-proxy",
                "kind": "balance",
                "balance_usd": 20,
                "observed_at": T1,
            }
        )
        self.assertEqual((await recovered.json())["state"], "ok")
        self.assertEqual(self.push(1)["title"], "OpenAI credits are back")
        self.assertEqual(self.push(1)["message"], "Balance $20.00. openai-proxy resumed.")


class ClassifierTests(unittest.TestCase):
    def test_xai_real_body_matches_and_generic_permission_does_not(self):
        for status in (403, 429):
            report = parse_credit_report(
                {
                    "provider": "xai",
                    "service": "pirate-radio",
                    "kind": "error",
                    "status": status,
                    "body": CREDIT_BODY,
                }
            )
            self.assertTrue(is_credit_error(report.provider, report.status, report.body))
        permission = parse_credit_report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": PERMISSION_ONLY,
            }
        )
        self.assertFalse(is_credit_error(permission.provider, permission.status, permission.body))
        generic = parse_credit_report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": "Forbidden",
            }
        )
        self.assertFalse(is_credit_error(generic.provider, generic.status, generic.body))

    def test_other_provider_signatures(self):
        cases = (
            ("openai", 429, '{"error":{"code":"insufficient_quota"}}', True),
            ("openai", 429, '{"error":{"code":"credit_balance_exhausted"}}', True),
            ("openai", 429, '{"error":{"code":"monthly_spend_limit_exceeded"}}', True),
            ("openai", 429, '{"error":{"code":"rate_limit_exceeded"}}', False),
            ("openai", 400, '{"error":{"code":"insufficient_quota"}}', False),
            ("x", 402, "", True),
            ("x", 403, "Spend cap reached", True),
            ("x", 403, "You have hit your spending limit", True),
            ("x", 403, "Your account is out of credits", True),
            ("x", 403, "Missing credential", False),
            ("x", 403, "Forbidden", False),
            ("cursor", None, '{"error":"usage_limit"}', True),
            ("cursor", None, "rate_limit", False),
        )
        for provider, status, body, expected in cases:
            payload = {
                "provider": provider,
                "service": "caller",
                "kind": "error",
                "body": body,
            }
            if status is not None:
                payload["status"] = status
            report = parse_credit_report(payload)
            self.assertEqual(
                is_credit_error(report.provider, report.status, report.body),
                expected,
                payload,
            )

    def test_match_past_500_characters_is_dropped(self):
        report = parse_credit_report(
            {
                "provider": "xai",
                "service": "pirate-radio",
                "kind": "error",
                "status": 403,
                "body": ("x" * BODY_LIMIT) + "used all available credits",
            }
        )
        self.assertEqual(len(report.body or ""), BODY_LIMIT)
        self.assertFalse(is_credit_error(report.provider, report.status, report.body))

    def test_rendered_reminder_counts_four_errors(self):
        snapshot = CreditSnapshot(
            provider="xai",
            state="out",
            balance_usd=None,
            daily_burn_usd=None,
            services=("pirate-radio",),
            error_count=4,
            last_transition_at=None,
            last_push_at=None,
            last_push_kind=None,
            updated_at=None,
        )
        push = render_push("reminder", snapshot)
        self.assertEqual(
            push.message,
            "Pirate Radio conversions are paused. 4 credit errors since it ran out. "
            "Add credits in the xAI console; I'll tell you when it's working again.",
        )
        three = render_push(
            "out",
            replace(
                snapshot,
                balance_usd=0,
                services=("pirate-radio", "pebble", "home-assistant"),
            ),
        )
        self.assertEqual(three.message, XAI_OUT)
        self.assertEqual(len(three.buttons), 1)


class MigrationTests(unittest.TestCase):
    def test_existing_ledger_gains_one_credit_table(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "notifications.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    tag TEXT,
                    group_name TEXT,
                    url TEXT,
                    ha_context_id TEXT,
                    payload_json TEXT NOT NULL,
                    service_data_json TEXT NOT NULL
                );
                CREATE TABLE notification_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id INTEGER,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    tag TEXT,
                    group_name TEXT,
                    reply_text TEXT,
                    event_json TEXT NOT NULL
                );
                CREATE TABLE workflow_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workflow_slug TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source TEXT,
                    notification_id INTEGER,
                    event_json TEXT NOT NULL
                );
                """
            )
            connection.close()
            NotificationLedger(str(path))
            NotificationLedger(str(path))
            check = sqlite3.connect(path)
            tables = {
                row[0]
                for row in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            versions = [row[0] for row in check.execute("SELECT version FROM schema_migrations")]
            columns = [
                row[1]
                for row in check.execute("PRAGMA table_info(credit_provider_state)")
            ]
            check.close()
            fresh = Path(tmp) / "fresh.sqlite3"
            NotificationLedger(str(fresh))
            NotificationLedger(str(fresh))
            fresh_db = sqlite3.connect(fresh)
            fresh_versions = [
                row[0] for row in fresh_db.execute("SELECT version FROM schema_migrations")
            ]
            fresh_db.close()
        self.assertIn("credit_provider_state", tables)
        self.assertEqual(versions, [1])
        self.assertEqual(
            columns,
            [
                "provider",
                "state",
                "balance_usd",
                "daily_burn_usd",
                "services_json",
                "error_count",
                "last_transition_at",
                "last_push_at",
                "last_push_kind",
                "updated_at",
            ],
        )
        self.assertEqual(fresh_versions, [1])


class BillingBoundaryTests(unittest.TestCase):
    def test_credit_module_does_not_call_a_billing_api(self):
        source = Path("homelab/credits.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertFalse(any(name.startswith("aiohttp") or name.startswith("urllib") for name in imported))
        self.assertNotIn("api.x.ai", source)
        self.assertNotIn("purchase", source.lower())
        self.assertNotIn("checkout", source.lower())
        self.assertIn("https://console.x.ai", source)


class PebbleCreditHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.fake = FakeHomeAssistant()
        self.responses = []
        self.stub = web.Application()
        self.stub.router.add_post("/stt", self._stt)
        self.stub_server = TestServer(self.stub)
        await self.stub_server.start_server()
        transcriber = XaiSpeechTranscriber(
            "xai-test-key",
            "grok-voice-transcribe-2.0",
            5,
            url=str(self.stub_server.make_url("/stt")),
        )
        self.forwarder_calls = []

        class Forwarder:
            async def post_json(inner_self, url, key, payload):
                self.forwarder_calls.append(payload)

        self.client = TestClient(
            TestServer(
                create_app(
                    write_config(Path(self.tmp.name)),
                    ha_client=self.fake,
                    pebble_forwarder=Forwarder(),
                    pebble_transcriber=transcriber,
                )
            )
        )
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.stub_server.close()
        self.tmp.cleanup()

    async def _stt(self, request):
        await request.read()
        status, text = self.responses.pop(0)
        return web.Response(status=status, text=text, content_type="text/plain")

    async def post_audio(self, delivery):
        form = FormData()
        form.add_field("recordedAt", str(int(datetime.now(timezone.utc).timestamp() * 1000)))
        form.add_field("client", "ring")
        form.add_field("audio", b"AUDIO-SENTINEL", filename="clip.m4a", content_type="audio/mp4")
        return await self.client.post(
            "/v1/pebble",
            data=form,
            headers={
                "Authorization": "Bearer pebble-ring-token",
                "X-Index-Trigger": "single-click-hold",
                "X-Index-Delivery": delivery,
            },
        )

    async def test_stt_credit_error_pushes_once_and_hides_the_body(self):
        self.responses.append((403, "Forbidden"))
        generic = await self.post_audio("generic")
        generic_body = await generic.json()
        self.assertEqual(generic.status, 502)
        self.assertEqual(generic_body["error"]["code"], "transcription_failed")
        self.assertNotIn("Forbidden", json.dumps(generic_body))
        self.assertEqual(self.fake.calls, [])

        self.responses.append((403, CREDIT_BODY))
        credit = await self.post_audio("credit")
        credit_body = await credit.json()
        self.assertEqual(credit.status, 502)
        self.assertNotIn("used all available credits", json.dumps(credit_body))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn("Pebble voice notes are paused.", self.fake.calls[0]["service_data"]["message"])
        self.assertNotIn("Balance", self.fake.calls[0]["service_data"]["message"])

        self.responses.append((429, CREDIT_BODY))
        again = await self.post_audio("credit-again")
        self.assertEqual(again.status, 502)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.forwarder_calls, [])
