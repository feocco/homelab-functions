import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from homelab.waiting_cli import DEMO_TOKEN, DemoWaitingSource, demo_app, demo_items, verify
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

    def test_verify_with_feedback_passes_against_the_demo(self):
        report = verify(self.url, DEMO_TOKEN, exercise_feedback=True)
        self.assertTrue(report["ok"], json.dumps(report, indent=2))
        names = [check["name"] for check in report["checks"]]
        self.assertIn("HTML, SSE, snapshot JSON, and /v1/waiting are one document", names)
        self.assertIn("later feedback goes to the same agent", names)

    def test_verify_fails_with_the_wrong_token(self):
        report = verify(self.url, "wrong-token")
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][-1]["name"], "dashboard sign-in sets a session cookie")

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
    async def test_demo_adds_new_items_over_time(self):
        source = DemoWaitingSource(reveal_every=1)
        counts = [len(await source.fetch()) for _ in range(4)]
        base = len(demo_items())
        self.assertEqual(counts, [base + 1, base + 2, base, base + 1])


if __name__ == "__main__":
    unittest.main()
