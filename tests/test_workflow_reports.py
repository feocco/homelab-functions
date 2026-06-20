from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from homelab.notification_ledger import NotificationLedger


class WorkflowReportLedgerTests(unittest.TestCase):
    def test_records_report_with_event_metadata(self):
        with TemporaryDirectory() as tmpdir:
            ledger = NotificationLedger(str(Path(tmpdir) / "notifications.sqlite3"))

            report = ledger.record_workflow_report(
                {
                    "workflow_slug": "cat-food-monitor",
                    "summary": "The morning check did not run.",
                    "source": "mobile-action",
                    "notification_id": 12,
                    "event": {"sourceDeviceName": "Pixel"},
                }
            )

            self.assertEqual(report["status"], "reported")
            self.assertEqual(report["workflow_slug"], "cat-food-monitor")
            self.assertEqual(report["summary"], "The morning check did not run.")
            self.assertEqual(report["source"], "mobile-action")
            self.assertEqual(report["notification_id"], 12)
            self.assertEqual(report["event"], {"sourceDeviceName": "Pixel"})

    def test_lists_reports_with_limit_clamped_to_one_hundred(self):
        with TemporaryDirectory() as tmpdir:
            ledger = NotificationLedger(str(Path(tmpdir) / "notifications.sqlite3"))
            for index in range(101):
                ledger.record_workflow_report(
                    {
                        "workflow_slug": "cat-food-monitor",
                        "summary": f"Issue {index}",
                    }
                )

            reports = ledger.list_workflow_reports(workflow="cat-food-monitor", limit=999)

            self.assertEqual(len(reports), 100)
            self.assertEqual(reports[0]["summary"], "Issue 100")
