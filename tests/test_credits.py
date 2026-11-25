import ast
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from homelab.credits import (
    BODY_LIMIT,
    CreditSnapshot,
    is_credit_error,
    parse_credit_report,
    render_push,
)
from homelab.notification_ledger import NotificationLedger


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


