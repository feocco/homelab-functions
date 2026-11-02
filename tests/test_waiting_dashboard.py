import asyncio
import base64
import hashlib
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from homelab.server import create_app
from homelab.waiting import CATEGORY_LABELS, CATEGORY_ORDER, Category, WaitingSourceError, run_refresh_loop
from homelab.waiting_web import BOARD_KEY
from waiting_support import TOKEN, FakeCursor, StaticSource, make_item, waiting_config


ORIGIN = {"Origin": "http://waiting.test"}


SECRET_ID = make_item(14, category=Category.SECRET).id
MERGE_ID = "feocco/homelab-config#15:approve_merge"
COPY_ID = make_item(16, category=Category.COPY, need="Run the fake apply").id


class DashboardHarness(unittest.IsolatedAsyncioTestCase):
    items = (
        make_item(14, category=Category.SECRET),
        make_item(15, repo="feocco/homelab-config", category=Category.APPROVE_MERGE, need="Approve and merge “Fake”"),
        make_item(16, category=Category.COPY, need="Run the fake apply"),
    )
    cursor_enabled = True
    config_overrides: dict = {"dashboard_public_url": "http://waiting.test"}

    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.source = StaticSource(list(self.items))
        self.cursor = FakeCursor() if self.cursor_enabled else None
        app = create_app(
            waiting_config(Path(self.tmp.name), **self.config_overrides),
            waiting_source=self.source,
            cursor_agents=self.cursor,
        )
        self.http = TestClient(TestServer(app), headers={"Host": "waiting.test"})
        await self.http.start_server()
        self.board = app[BOARD_KEY]
        await self.board.refresh()

    async def asyncTearDown(self):
        await self.http.close()
        self.tmp.cleanup()

    async def sign_in(self, token=TOKEN):
        with patch("homelab.waiting_web.SIGN_IN_FAILURE_DELAY_SECONDS", 0):
            return await self.http.post(
                "/waiting/session",
                data={"token": token},
                headers=ORIGIN,
                allow_redirects=False,
            )

    async def first_events(self, count, path="/waiting/events"):
        events = []
        async with self.http.get(path) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            name = None
            while len(events) < count:
                line = (await asyncio.wait_for(response.content.readline(), 5)).decode().rstrip("\n")
                if line.startswith("event: "):
                    name = line[len("event: ") :]
                elif line.startswith("data: "):
                    events.append((name, json.loads(line[len("data: ") :])))
        return events


class SessionBoundaryTests(DashboardHarness):
    async def test_signed_out_routes_show_nothing(self):
        page = await self.http.get("/waiting")
        body = await page.text()
        self.assertEqual(page.status, 200)
        self.assertIn('name="token"', body)
        self.assertNotIn("Fake pull request", body)
        self.assertNotIn('id="snapshot"', body)
        for method, path in (("GET", "/waiting/snapshot"), ("GET", "/waiting/events"), ("POST", "/waiting/feedback")):
            response = await self.http.request(method, path, headers=ORIGIN)
            self.assertEqual(response.status, 401, path)
            self.assertEqual((await response.json())["error"]["code"], "unauthorized")

    async def test_sign_in_sets_a_strict_http_only_cookie(self):
        response = await self.sign_in()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/waiting")
        cookie = response.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/waiting", "Max-Age=2592000"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn(TOKEN, cookie)
        page = await self.http.get("/waiting")
        self.assertIn('id="snapshot"', await page.text())

    async def test_wrong_token_and_cross_origin_sign_in_are_refused(self):
        wrong = await self.sign_in("not-the-token")
        self.assertEqual(wrong.status, 401)
        self.assertIn("did not match", await wrong.text())
        self.assertNotIn("Set-Cookie", wrong.headers)
        cross = await self.http.post("/waiting/session", data={"token": TOKEN}, headers={"Origin": "https://evil.test"})
        self.assertEqual(cross.status, 403)
        self.assertEqual((await self.http.get("/waiting/snapshot")).status, 401)

    async def test_bearer_and_session_do_not_open_each_others_routes(self):
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual((await self.http.get("/waiting/snapshot", headers=bearer)).status, 401)
        await self.sign_in()
        self.assertEqual((await self.http.get("/v1/waiting")).status, 401)
        self.assertEqual((await self.http.get("/waiting/snapshot")).status, 200)

    async def test_signed_in_feedback_still_needs_same_origin(self):
        await self.sign_in()
        for path, body in (
            ("/waiting/feedback", {"item_id": SECRET_ID, "feedback": "Hi"}),
            ("/waiting/handled", {"item_id": SECRET_ID}),
        ):
            for headers in ({}, {"Origin": "https://evil.test"}):
                response = await self.http.post(path, json=body, headers=headers)
                self.assertEqual(response.status, 403, path)
        self.assertEqual(self.cursor.launches, [])
        self.assertIn(SECRET_ID, [item["id"] for item in self.board.payload()["items"]])

    async def test_sign_out_clears_the_session(self):
        await self.sign_in()
        response = await self.http.post("/waiting/sign-out", headers=ORIGIN, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual((await self.http.get("/waiting/snapshot")).status, 401)

    async def test_root_redirects_only_on_the_dashboard_host(self):
        dashboard = await self.http.get("/", allow_redirects=False)
        self.assertEqual((dashboard.status, dashboard.headers["Location"]), (302, "/waiting"))
        other = await self.http.get("/", headers={"Host": "homelab-functions.home.feocco.com"}, allow_redirects=False)
        self.assertEqual(other.status, 404)


class OneDocumentTests(DashboardHarness):
    async def test_html_sse_snapshot_and_v1_are_the_same_document(self):
        await self.sign_in()
        html = await (await self.http.get("/waiting")).text()
        embedded = json.loads(re.search(r'<script type="application/json" id="snapshot">(.*?)</script>', html).group(1))
        snapshot = await (await self.http.get("/waiting/snapshot")).json()
        v1 = await (await self.http.get("/v1/waiting", headers={"Authorization": f"Bearer {TOKEN}"})).json()
        [(event, streamed)] = await self.first_events(1)

        self.assertEqual(event, "snapshot")
        self.assertEqual(embedded, snapshot)
        self.assertEqual(snapshot, v1)
        self.assertEqual(v1, streamed)
        self.assertEqual(v1["schema"], "waiting-on-me.v2")
        self.assertEqual([item["id"] for item in v1["items"]], [MERGE_ID, SECRET_ID, COPY_ID])
        self.assertEqual(
            [(entry["id"], entry["label"], entry["count"]) for entry in v1["categories"]],
            [(category.value, CATEGORY_LABELS[category], int(category in (Category.APPROVE_MERGE, Category.SECRET, Category.COPY))) for category in CATEGORY_ORDER],
        )
        self.assertEqual(v1["total"], 3)
        for item in v1["items"]:
            self.assertTrue(item["need"] and item["why"])
            self.assertTrue(item["source"]["url"].startswith("https://github.com/"))
            self.assertIn(item["action"]["type"], ("open", "copy"))
        copy = next(item for item in v1["items"] if item["id"] == COPY_ID)
        self.assertEqual(copy["action"], {"type": "copy", "label": "Copy", "value": "echo fake"})

    async def test_page_csp_pins_its_only_script_and_style(self):
        await self.sign_in()
        response = await self.http.get("/waiting")
        html = await response.text()
        policy = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        styles = re.findall(r"<style>(.*?)</style>", html, re.S)
        self.assertEqual((len(scripts), len(styles)), (1, 1))
        for directive, source in (("script-src", scripts[0]), ("style-src", styles[0])):
            digest = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
            self.assertIn(f"{directive} 'sha256-{digest}'", policy)
        self.assertNotRegex(html, r"\son[a-z]+=")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_hostile_titles_cannot_break_out_of_the_embedded_snapshot(self):
        self.source.items = [make_item(20, need="</script><script>alert(1)</script>")]
        await self.board.refresh()
        await self.sign_in()
        html = await (await self.http.get("/waiting")).text()
        self.assertNotIn("<script>alert(1)", html)
        embedded = json.loads(re.search(r'id="snapshot">(.*?)</script>', html).group(1))
        self.assertEqual(embedded["items"][0]["need"], "</script><script>alert(1)</script>")


class LiveUpdateTests(DashboardHarness):
    async def test_stream_sends_the_current_snapshot_then_each_change(self):
        await self.sign_in()
        stream = asyncio.create_task(self.first_events(2))
        await asyncio.sleep(0.05)
        arrival = make_item(30, category=Category.QUESTION, need="Arrived later?")
        self.source.items.append(arrival)
        await self.board.refresh()
        (first_name, first), (second_name, second) = await stream

        self.assertEqual((first_name, second_name), ("snapshot", "snapshot"))
        self.assertGreater(second["version"], first["version"])
        self.assertNotIn(arrival.id, [item["id"] for item in first["items"]])
        self.assertIn(arrival.id, [item["id"] for item in second["items"]])
        self.assertEqual(second, await (await self.http.get("/waiting/snapshot")).json())

    async def test_idle_stream_sends_heartbeats(self):
        await self.sign_in()
        with patch("homelab.waiting_web.HEARTBEAT_SECONDS", 0.05):
            events = await self.first_events(3)
        self.assertEqual([name for name, _ in events], ["snapshot", "ping", "ping"])

    async def test_streams_end_when_the_server_shuts_down(self):
        await self.sign_in()
        async with self.http.get("/waiting/events") as response:
            await response.content.readline()
            await asyncio.sleep(0.05)
            self.assertEqual(self.board.subscriber_count(), 1)
            self.board.close()
            remaining = await asyncio.wait_for(response.content.read(), 5)
        self.assertNotIn(b"event: snapshot\ndata", remaining.split(b"\n\n")[-1])
        await asyncio.sleep(0.05)
        self.assertEqual(self.board.subscriber_count(), 0)

    async def test_items_handled_on_github_leave_the_stream(self):
        await self.sign_in()
        stream = asyncio.create_task(self.first_events(2))
        await asyncio.sleep(0.05)
        self.source.items = [item for item in self.source.items if item.id != SECRET_ID]
        await self.board.refresh()
        (_, before), (_, after) = await stream
        self.assertIn(SECRET_ID, [item["id"] for item in before["items"]])
        self.assertNotIn(SECRET_ID, [item["id"] for item in after["items"]])

    async def test_joe_marking_done_removes_the_item_live_everywhere(self):
        await self.sign_in()
        stream = asyncio.create_task(self.first_events(2))
        await asyncio.sleep(0.05)
        response = await self.http.post("/waiting/handled", json={"item_id": COPY_ID}, headers=ORIGIN)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "handled", "item_id": COPY_ID})
        (_, before), (_, after) = await stream
        self.assertIn(COPY_ID, [item["id"] for item in before["items"]])
        self.assertNotIn(COPY_ID, [item["id"] for item in after["items"]])
        v1 = await (await self.http.get("/v1/waiting", headers={"Authorization": f"Bearer {TOKEN}"})).json()
        self.assertNotIn(COPY_ID, [item["id"] for item in v1["items"]])
        await self.board.refresh()
        self.assertNotIn(COPY_ID, [item["id"] for item in self.board.payload()["items"]])

    async def test_only_a_signed_in_human_can_mark_done_and_never_for_merges(self):
        bearer = await self.http.post(
            "/waiting/handled",
            json={"item_id": SECRET_ID},
            headers={**ORIGIN, "Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(bearer.status, 401)
        await self.sign_in()
        merge = await self.http.post("/waiting/handled", json={"item_id": MERGE_ID}, headers=ORIGIN)
        self.assertEqual(merge.status, 409)
        self.assertEqual((await merge.json())["error"]["code"], "merge_on_github")
        unknown = await self.http.post("/waiting/handled", json={"item_id": "feocco/x#1:secret:0123456789"}, headers=ORIGIN)
        self.assertEqual(unknown.status, 404)
        invalid = await self.http.post("/waiting/handled", json={"item_id": "not an id"}, headers=ORIGIN)
        self.assertEqual(invalid.status, 400)
        self.assertEqual(
            sorted(item["id"] for item in self.board.payload()["items"]),
            sorted((MERGE_ID, SECRET_ID, COPY_ID)),
        )

    async def test_page_offers_one_primary_action_and_a_source_link_per_card(self):
        await self.sign_in()
        html = await (await self.http.get("/waiting")).text()
        template = re.search(r'<template id="item-template">(.*?)</template>', html, re.S).group(1)
        self.assertEqual(template.count('class="button primary action-open"'), 1)
        self.assertEqual(template.count('class="button primary action-copy"'), 1)
        self.assertEqual(template.count('class="source"'), 1)
        self.assertNotIn("merge", template.lower().replace("merged or closed on github", ""))

    async def test_refresh_loop_refreshes_on_its_interval(self):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        later = make_item(17, category=Category.EVIDENCE, need="Check the fake trace")
        self.source.items.append(later)
        fetches = self.source.fetches
        with self.assertRaises(asyncio.CancelledError):
            await run_refresh_loop(self.board, sleep=fake_sleep)
        self.assertEqual(self.source.fetches - fetches, 2)
        self.assertTrue(all(seconds > 3500 for seconds in sleeps))
        self.assertIn(later.id, [item["id"] for item in self.board.payload()["items"]])


class DegradedStateTests(DashboardHarness):
    async def test_github_errors_keep_the_last_good_items(self):
        good = self.board.payload()
        self.source.error = WaitingSourceError("GitHub GraphQL returned HTTP 502.")
        await self.board.refresh()
        payload = self.board.payload()
        self.assertEqual(payload["github"]["status"], "error")
        self.assertEqual(payload["github"]["message"], "GitHub GraphQL returned HTTP 502.")
        self.assertEqual(payload["items"], good["items"])
        self.assertEqual(payload["github"]["refreshed_at"], good["github"]["refreshed_at"])

    async def test_unexpected_errors_are_reported_generically(self):
        self.source.error = RuntimeError(f"leaked {TOKEN}")
        with self.assertLogs("homelab-functions", level="ERROR"):
            await self.board.refresh()
        self.assertEqual(self.board.payload()["github"]["message"], "Unexpected error while reading GitHub.")

    async def test_health_reports_dashboard_state(self):
        health = await (await self.http.get("/health")).json()
        self.assertEqual(
            (health["waiting_github_status"], health["waiting_feedback_enabled"], health["waiting_demo"]),
            ("ok", True, False),
        )


class UnconfiguredTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_github_and_cursor_settings_degrade_clearly(self):
        with TemporaryDirectory() as tmp:
            config = waiting_config(
                Path(tmp),
                dashboard_gh_app_id="5572135",
                dashboard_gh_owner="feocco",
                cursor_api_key="",
            )
            http = TestClient(TestServer(create_app(config)))
            await http.start_server()
            try:
                payload = await (
                    await http.get("/v1/waiting", headers={"Authorization": f"Bearer {TOKEN}"})
                ).json()
                feedback = await http.post(
                    "/v1/waiting/feedback",
                    json={"item_id": SECRET_ID, "feedback": "Hi"},
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                feedback_error = (await feedback.json())["error"]
            finally:
                await http.close()
        self.assertEqual(payload["github"]["status"], "unconfigured")
        self.assertEqual(
            payload["github"]["message"],
            "GitHub is not configured. Set DASHBOARD_GH_APP_INSTALLATION_ID, DASHBOARD_GH_APP_KEY on homelab-functions.",
        )
        self.assertEqual(payload["items"], [])
        self.assertFalse(payload["feedback"]["enabled"])
        self.assertIn("CURSOR_API_KEY", payload["feedback"]["message"])
        self.assertEqual(feedback.status, 503)
        self.assertEqual(feedback_error["code"], "feedback_unavailable")


class HttpsCookieTests(DashboardHarness):
    config_overrides = {"dashboard_public_url": "https://waiting.home.feocco.com"}

    async def test_https_dashboards_mark_the_cookie_secure(self):
        response = await self.sign_in()
        self.assertEqual(response.status, 303)
        self.assertIn("Secure", response.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
