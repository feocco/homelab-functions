import asyncio
import base64
import unittest
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory

from homelab.cursor_agents import CursorAgentsClient
from homelab.json_http import JsonResponse
from homelab.waiting import (
    FEEDBACK_DISABLED_MESSAGE,
    CursorCallError,
    FeedbackError,
    FeedbackService,
    FeedbackStore,
    WaitingBoard,
)
from waiting_support import CURSOR_KEY, FakeCursor, FakeTransport, StaticSource, make_item, raise_transport


ITEM = make_item(14)
REVIEW_ONLY_LAUNCH_FIELDS = {"agentId", "name", "prompt", "repos", "autoCreatePR", "workOnCurrentBranch"}
OTHER = make_item(15, repo="feocco/homelab-config")


def timeout_error():
    return CursorCallError("timed out", code="cursor_unreachable", maybe_delivered=True)


def rejected_error():
    return CursorCallError("Repository not connected", code="validation_error", maybe_delivered=False, status=400)


class FeedbackHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "waiting-feedback.sqlite3")
        self.cursor = FakeCursor()
        self.service = await self.build(self.cursor)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def build(self, cursor):
        store = FeedbackStore(self.path)
        board = WaitingBoard(StaticSource([ITEM, OTHER]), store, refresh_seconds=60, feedback_enabled=cursor is not None)
        await board.refresh()
        return FeedbackService(store, board, cursor)

    def agent_ids(self):
        return {launch["agent_id"] for launch in self.cursor.launches} | {
            agent_id for agent_id, _ in self.cursor.follow_ups
        }


class CreateVersusFollowUpTests(FeedbackHarness):
    async def test_first_feedback_launches_and_later_feedback_follows_up_on_the_same_agent(self):
        first = await self.service.submit(ITEM.id, "Rename the flag.", "request-one")
        second = await self.service.submit(ITEM.id, "Also add a test.", "request-two")

        self.assertEqual(first["delivery"], "launch")
        self.assertEqual(second["delivery"], "followup")
        self.assertEqual(first["agent"]["id"], second["agent"]["id"])
        self.assertEqual(len(self.cursor.launches), 1)
        self.assertEqual(self.cursor.follow_ups[0][0], first["agent"]["id"])
        self.assertIn("Rename the flag.", self.cursor.launches[0]["prompt"])
        self.assertIn("Also add a test.", self.cursor.follow_ups[0][1])
        linked = self.service.board.payload()["items"][0]["feedback"]
        self.assertEqual((linked["agent_id"], linked["state"], linked["sent_count"]), (first["agent"]["id"], "launched", 2))

    async def test_each_item_gets_its_own_agent(self):
        one = await self.service.submit(ITEM.id, "First item.", None)
        two = await self.service.submit(OTHER.id, "Second item.", None)
        self.assertNotEqual(one["agent"]["id"], two["agent"]["id"])
        self.assertEqual(len(self.cursor.launches), 2)

    async def test_concurrent_feedback_on_one_item_launches_exactly_one_agent(self):
        self.cursor.gate = asyncio.Event()
        submits = [
            asyncio.create_task(self.service.submit(ITEM.id, f"Note {index}", f"request-{index:04d}"))
            for index in range(5)
        ]
        await asyncio.sleep(0.01)
        self.cursor.gate.set()
        results = await asyncio.gather(*submits)

        self.assertEqual(len(self.cursor.launches), 1)
        self.assertEqual(len(self.cursor.follow_ups), 4)
        self.assertEqual(len({result["agent"]["id"] for result in results}), 1)
        self.assertEqual(sorted(result["delivery"] for result in results), ["followup"] * 4 + ["launch"])

    async def test_mapping_survives_a_restart(self):
        first = await self.service.submit(ITEM.id, "Before restart.", None)
        restarted_cursor = FakeCursor()
        restarted = await self.build(restarted_cursor)
        after = await restarted.submit(ITEM.id, "After restart.", None)

        self.assertEqual(after["delivery"], "followup")
        self.assertEqual(after["agent"]["id"], first["agent"]["id"])
        self.assertEqual(restarted_cursor.launches, [])
        self.assertEqual(restarted.board.payload()["items"][0]["feedback"]["agent_id"], first["agent"]["id"])

    async def test_two_processes_claiming_one_item_share_the_first_agent_id(self):
        store_a, store_b = FeedbackStore(self.path), FeedbackStore(self.path)
        first = store_a.claim_agent(ITEM.id, "bc-first", "request-a", "prompt a")
        second = store_b.claim_agent(ITEM.id, "bc-second", "request-b", "prompt b")
        self.assertEqual((first.agent_id, second.agent_id), ("bc-first", "bc-first"))


class IdempotencyTests(FeedbackHarness):
    async def test_resubmitting_a_request_id_does_not_call_cursor_again(self):
        first = await self.service.submit(ITEM.id, "Only once.", "same-request")
        again = await self.service.submit(ITEM.id, "Only once.", "same-request")
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["delivery"], "launch")
        self.assertEqual((len(self.cursor.launches), len(self.cursor.follow_ups)), (1, 0))

    async def test_reusing_a_request_id_for_other_feedback_is_a_conflict(self):
        await self.service.submit(ITEM.id, "Original.", "same-request")
        with self.assertRaises(FeedbackError) as caught:
            await self.service.submit(ITEM.id, "Different.", "same-request")
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.CONFLICT, "request_conflict"))


class FailureTests(FeedbackHarness):
    async def test_unconfirmed_launch_retries_with_the_same_agent_id(self):
        self.cursor.launch_failures.append(timeout_error())
        with self.assertRaises(FeedbackError) as caught:
            await self.service.submit(ITEM.id, "Launch me.", "launch-request")
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.GATEWAY_TIMEOUT, "launch_unconfirmed"))
        self.assertEqual(self.service.board.payload()["items"][0]["feedback"]["state"], "launching")

        retried = await self.service.submit(ITEM.id, "Launch me.", "launch-request")
        self.assertEqual(retried["delivery"], "launch")
        self.assertEqual(len(self.cursor.launches), 2)
        self.assertEqual(len(self.agent_ids()), 1)
        self.assertEqual(retried["agent"]["id"], self.cursor.launches[0]["agent_id"])

    async def test_rejected_launch_then_new_feedback_relaunches_once_and_follows_up(self):
        self.cursor.launch_failures.append(rejected_error())
        with self.assertRaises(FeedbackError) as caught:
            await self.service.submit(ITEM.id, "First try.", "first-request")
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.BAD_GATEWAY, "cursor_rejected"))

        later = await self.service.submit(ITEM.id, "Second note.", "second-request")
        self.assertEqual(later["delivery"], "followup")
        self.assertIn("First try.", self.cursor.launches[-1]["prompt"])
        self.assertEqual(len(self.agent_ids()), 1)
        replay = await self.service.submit(ITEM.id, "First try.", "first-request")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(self.service.board.payload()["items"][0]["feedback"]["sent_count"], 2)

    async def test_unconfirmed_follow_up_is_not_resent_automatically(self):
        await self.service.submit(ITEM.id, "Launch.", None)
        self.cursor.follow_up_failures.append(timeout_error())
        for _ in range(2):
            with self.assertRaises(FeedbackError) as caught:
                await self.service.submit(ITEM.id, "Maybe delivered.", "maybe-request")
            self.assertEqual(caught.exception.code, "delivery_unconfirmed")
        self.assertEqual(self.cursor.follow_ups, [])

    async def test_busy_agent_is_reported_and_can_be_retried(self):
        await self.service.submit(ITEM.id, "Launch.", None)
        self.cursor.follow_up_failures.append(
            CursorCallError("busy", code="agent_busy", maybe_delivered=False, status=409)
        )
        with self.assertRaises(FeedbackError) as caught:
            await self.service.submit(ITEM.id, "Later.", "busy-request")
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.CONFLICT, "agent_busy"))
        retried = await self.service.submit(ITEM.id, "Later.", "busy-request")
        self.assertEqual(retried["delivery"], "followup")

    async def test_unknown_item_is_rejected_without_calling_cursor(self):
        with self.assertRaises(FeedbackError) as caught:
            await self.service.submit("feocco/not-waiting#1", "Hi.", None)
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.NOT_FOUND, "unknown_item"))
        self.assertEqual(self.cursor.launches, [])


class DisabledTests(FeedbackHarness):
    async def test_missing_cursor_key_disables_feedback_clearly(self):
        service = await self.build(None)
        with self.assertRaises(FeedbackError) as caught:
            await service.submit(ITEM.id, "Hello.", None)
        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("CURSOR_API_KEY", str(caught.exception))
        feedback = service.board.payload()["feedback"]
        self.assertEqual((feedback["enabled"], feedback["message"]), (False, FEEDBACK_DISABLED_MESSAGE))


class PullRequestSyncTests(FeedbackHarness):
    async def test_feedback_pr_link_appears_once_cursor_reports_it(self):
        result = await self.service.submit(ITEM.id, "Open a PR.", None)
        await self.service.sync_pull_requests()
        self.assertIsNone(self.service.board.payload()["items"][0]["feedback"]["pr_url"])
        self.cursor.pr_urls[result["agent"]["id"]] = "https://github.com/feocco/homelab-functions/pull/64"
        version = self.service.board.payload()["version"]
        await self.service.sync_pull_requests()
        payload = self.service.board.payload()
        self.assertGreater(payload["version"], version)
        self.assertEqual(payload["items"][0]["feedback"]["pr_url"], "https://github.com/feocco/homelab-functions/pull/64")


def cursor_api(transport):
    return CursorAgentsClient(CURSOR_KEY, transport, base_url="https://api.cursor.test")


class CursorClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_uses_basic_auth_and_only_review_safe_fields(self):
        transport = FakeTransport()
        transport.on(
            lambda r: r.method == "POST" and r.url.endswith("/v1/agents"),
            lambda r: JsonResponse(201, {"agent": {"id": "bc-1", "url": "https://cursor.com/agents/bc-1"}}),
        )
        url = await cursor_api(transport).launch(agent_id="bc-1", name="Waiting feedback", prompt="Do it", item=ITEM)

        self.assertEqual(url, "https://cursor.com/agents/bc-1")
        (request,) = transport.requests
        self.assertEqual(request.headers["Authorization"], "Basic " + base64.b64encode(f"{CURSOR_KEY}:".encode()).decode())
        self.assertEqual(set(request.payload), REVIEW_ONLY_LAUNCH_FIELDS)
        self.assertEqual(request.payload["agentId"], "bc-1")
        self.assertIs(request.payload["autoCreatePR"], True)
        self.assertIs(request.payload["workOnCurrentBranch"], False)
        self.assertEqual(
            request.payload["repos"],
            [{"url": "https://github.com/feocco/homelab-functions", "startingRef": "cursor/fake-14"}],
        )

    async def test_cross_repository_prs_launch_from_the_pr_url(self):
        transport = FakeTransport()
        transport.on(lambda r: True, lambda r: JsonResponse(201, {}))
        fork = make_item(16, cross_repository=True)
        url = await cursor_api(transport).launch(agent_id="bc-2", name="n", prompt="p", item=fork)
        self.assertEqual(url, "https://cursor.com/agents/bc-2")
        self.assertEqual(transport.requests[0].payload["repos"][0]["prUrl"], fork.url)
        self.assertNotIn("startingRef", transport.requests[0].payload["repos"][0])

    async def test_busy_agent_falls_back_to_steer(self):
        transport = FakeTransport()
        transport.on(
            lambda r: r.payload.get("delivery") == "steer",
            lambda r: JsonResponse(202, {"delivery": "queued"}),
        )
        transport.on(lambda r: True, lambda r: JsonResponse(409, {"error": {"code": "agent_busy", "message": "busy"}}))
        delivery = await cursor_api(transport).follow_up("bc-1", "More")
        self.assertEqual(delivery, "steer")
        self.assertEqual([r.url for r in transport.requests], ["https://api.cursor.test/v1/agents/bc-1/runs"] * 2)
        self.assertEqual(transport.requests[1].payload, {"prompt": {"text": "More"}, "delivery": "steer"})

    async def test_busy_agent_without_steer_reports_busy(self):
        transport = FakeTransport()
        transport.on(
            lambda r: r.payload.get("delivery") == "steer",
            lambda r: JsonResponse(403, {"error": {"code": "feature_unavailable", "message": "no steer"}}),
        )
        transport.on(lambda r: True, lambda r: JsonResponse(409, {"error": {"code": "agent_busy", "message": "busy"}}))
        with self.assertRaises(CursorCallError) as caught:
            await cursor_api(transport).follow_up("bc-1", "More")
        self.assertEqual(caught.exception.code, "agent_busy")

    async def test_errors_classify_delivery_certainty_and_never_echo_the_key(self):
        cases = (
            (lambda r: JsonResponse(500, {"error": "boom"}), True),
            (lambda r: JsonResponse(400, {"error": {"code": "validation_error", "message": "bad"}}), False),
            (raise_transport(maybe_delivered=True), True),
            (raise_transport(maybe_delivered=False), False),
        )
        for respond, maybe_delivered in cases:
            transport = FakeTransport()
            transport.on(lambda r: True, respond)
            with self.assertRaises(CursorCallError) as caught:
                await cursor_api(transport).follow_up("bc-1", "More")
            self.assertEqual(caught.exception.maybe_delivered, maybe_delivered)
            self.assertNotIn(CURSOR_KEY, str(caught.exception))

    async def test_pull_request_url_reads_the_latest_run(self):
        transport = FakeTransport()
        transport.on(lambda r: r.url.endswith("/v1/agents/bc-1"), lambda r: JsonResponse(200, {"latestRunId": "run-9"}))
        transport.on(
            lambda r: r.url.endswith("/runs/run-9"),
            lambda r: JsonResponse(
                200,
                {"git": {"branches": [{"prUrl": "javascript:alert(1)"}, {"prUrl": "https://github.com/feocco/x/pull/5"}]}},
            ),
        )
        self.assertEqual(await cursor_api(transport).pull_request_url("bc-1"), "https://github.com/feocco/x/pull/5")
        self.assertTrue(all(r.method == "GET" for r in transport.requests))


class NoAutoApprovalTests(FeedbackHarness):
    """Feedback may only launch, follow up, or read. It never approves, merges, or deploys."""

    async def test_full_feedback_flow_sends_only_launch_follow_up_and_read_calls(self):
        transport = FakeTransport()
        transport.on(lambda r: r.method == "POST" and r.url.endswith("/v1/agents"), lambda r: JsonResponse(201, {}))
        transport.on(lambda r: r.method == "POST" and r.url.endswith("/runs"), lambda r: JsonResponse(201, {}))
        transport.on(lambda r: r.method == "GET", lambda r: JsonResponse(200, {}))
        service = await self.build(cursor_api(transport))

        await service.submit(ITEM.id, "Please merge this and deploy it.", None)
        await service.submit(ITEM.id, "Approve it too.", None)
        await service.sync_pull_requests()

        calls = [(r.method, r.url.removeprefix("https://api.cursor.test")) for r in transport.requests]
        agent_id = transport.requests[0].payload["agentId"]
        self.assertEqual(
            calls,
            [("POST", "/v1/agents"), ("POST", f"/v1/agents/{agent_id}/runs"), ("GET", f"/v1/agents/{agent_id}")],
        )
        launch = transport.requests[0].payload
        self.assertEqual(set(launch), REVIEW_ONLY_LAUNCH_FIELDS)
        self.assertIn("Do not approve, merge, or deploy anything.", launch["prompt"]["text"])
        self.assertIn("Do not approve, merge, or deploy anything.", transport.requests[1].payload["prompt"]["text"])
        self.assertEqual(set(transport.requests[1].payload), {"prompt"})
        self.assertIn("never approves, merges, or deploys", service.board.payload()["feedback"]["policy"])


if __name__ == "__main__":
    unittest.main()
