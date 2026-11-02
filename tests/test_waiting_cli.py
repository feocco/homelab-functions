import json
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from homelab.waiting_cli import DEMO_TOKEN, DemoWaitingSource, demo_app, item_problems, verify
from test_access_vector_parity import BackgroundApp


ROOT = Path(__file__).resolve().parents[1]


class VerifyCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.background = BackgroundApp(demo_app(0, DEMO_TOKEN, self.tmp.name, refresh_seconds=3600))
        self.url = self.background.start()

    def tearDown(self):
        self.background.close()
        self.tmp.cleanup()

    def test_verify_with_exercise_passes_against_the_demo(self):
        report = verify(self.url, DEMO_TOKEN, exercise=True)
        self.assertTrue(report["ok"], json.dumps(report, indent=2))
        names = [check["name"] for check in report["checks"]]
        for expected in (
            "HTML, SSE, snapshot JSON, and /v1/waiting are one document",
            "exactly the seven inbox categories, in order",
            "every item has a need, a why, one primary action, and a source link",
            "agents cannot mark Joe's items done",
            "approve/merge cannot be cleared from the inbox",
            "Joe marking an item done removes it from every view",
            "later feedback goes to the same agent",
        ):
            self.assertIn(expected, names)

    def test_verify_fails_with_the_wrong_token(self):
        report = verify(self.url, "wrong-token")
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][-1]["name"], "inbox sign-in sets a session cookie")

    def test_cli_prints_json_and_sets_the_exit_code(self):
        result = subprocess.run(
            [sys.executable, "-m", "homelab.waiting_cli", "verify", "--url", self.url, "--token", DEMO_TOKEN],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])


class DemoSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_covers_every_category_then_adds_and_removes_items(self):
        base = await DemoWaitingSource(phase_every=1000).fetch()
        source = DemoWaitingSource(phase_every=1)
        arrived, answered, cycled = [await source.fetch() for _ in range(3)]
        self.assertEqual({item.id for item in cycled.items}, {item.id for item in base.items})

        self.assertEqual(
            Counter(item.category.value for item in base.items),
            Counter(["approve_merge", "deploy", "secret", "infra", "question", "copy", "evidence"]),
        )
        self.assertEqual(len(base.rejected), 1)
        for item in base.items:
            self.assertEqual(item_problems(item.to_json()), [], item.id)
        new = {item.id for item in arrived.items} - {item.id for item in base.items}
        self.assertEqual(new, {"feocco/homelab-config#943:approve_merge"})
        gone = {item.category.value for item in arrived.items} - {item.category.value for item in answered.items}
        self.assertEqual(gone, {"question"})

    def test_item_problems_catches_incomplete_items(self):
        complete = {
            "category": "copy",
            "need": "Run it",
            "why": "Because",
            "action": {"type": "copy", "label": "Copy", "value": "echo hi"},
            "source": {"label": "feocco/x#1", "url": "https://github.com/feocco/x/pull/1"},
        }
        self.assertEqual(item_problems(complete), [])
        merge = {**complete, "category": "approve_merge", "action": {"type": "open", "label": "Merge", "url": "https://example.test"}}
        self.assertIn("approve/merge must only open the pull request on GitHub", item_problems(merge))
        self.assertIn("missing why", item_problems({**complete, "why": ""}))


if __name__ == "__main__":
    unittest.main()
