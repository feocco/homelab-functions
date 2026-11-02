import asyncio
import io
import json
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from homelab.pebble import (
    DEFAULT_GROK_PEBBLE_WEBHOOK_URL,
    EVENT_FRESHNESS_WINDOW,
    MAX_AUDIO_BYTES,
    MAX_PEBBLE_BODY_BYTES,
    PebbleLedger,
    PebbleRequestError,
    XaiSpeechTranscriber,
    bearer_token_matches,
    timestamp_from_recorded_at,
)
from homelab.server import Config, create_app


RING = "pebble-ring-token-do-not-log"
WEBHOOK_KEY = "grok-pebble-webhook-key-do-not-log"
WORK_WEBHOOK_URL = "https://work.example/grok-pebble-webhook"
WORK_WEBHOOK_KEY = "grok-pebble-work-webhook-key-do-not-log"
XAI_KEY = "xai-test-key-do-not-log"
BROKER = "broker-token-do-not-share"
AUDIO = b"AUDIO-SENTINEL-9f3c-not-audio"


def recorded_at(*, offset: timedelta = timedelta()) -> str:
    moment = datetime.now(timezone.utc) + offset
    return str(int(moment.timestamp() * 1000))


RECORDED_AT = recorded_at()


class FakeForwarder:
    def __init__(self, *, fail_times: int = 0):
        self.calls = []
        self.fail_times = fail_times

    async def post_json(self, url, key, payload):
        self.calls.append({"url": url, "key": key, "payload": payload})
        if len(self.calls) <= self.fail_times:
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "webhook_failed",
                "Grok webhook rejected the pebble event",
            )


class BlockingForwarder(FakeForwarder):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post_json(self, url, key, payload):
        self.calls.append({"url": url, "key": key, "payload": payload})
        self.started.set()
        await self.release.wait()


class FakeTranscriber:
    def __init__(self, *, text="lights off", duration=1.5, fail: bool = False):
        self.calls = []
        self.text = text
        self.duration = duration
        self.fail = fail

    async def transcribe(self, audio, *, filename, content_type):
        self.calls.append({"audio": audio, "filename": filename, "content_type": content_type})
        if self.fail:
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "transcription_failed",
                "Speech-to-text request failed",
            )
        from homelab.pebble import Transcript

        return Transcript(text=self.text, duration_seconds=self.duration)


def make_config(tmp: Path, **overrides) -> Config:
    values = dict(
        ha_url="https://example.ui.nabu.casa",
        ha_long_lived_token="ha-long-lived-token",
        ha_notify_joe_service="notify.mobile_app_pixel",
        ha_notify_jess_service="notify.mobile_app_jess",
        homelab_functions_token=BROKER,
        notification_ledger_path=str(tmp / "notifications.sqlite3"),
        notification_action_recorder_enabled=False,
        homelab_catalog_path=str(tmp / "catalog.json"),
        homelab_smoke_signal_targets_path=str(tmp / "targets.json"),
        pebble_ring_token=RING,
        grok_pebble_webhook_key=WEBHOOK_KEY,
        grok_pebble_work_webhook_url=WORK_WEBHOOK_URL,
        grok_pebble_work_webhook_key=WORK_WEBHOOK_KEY,
        xai_api_key=XAI_KEY,
        pebble_ledger_path=str(tmp / "pebble.sqlite3"),
        pebble_audio_dir=str(tmp / "audio"),
        pebble_audio_ttl_seconds=86400,
    )
    values.update(overrides)
    for name in ("catalog.json", "targets.json"):
        path = tmp / name
        if not path.exists():
            path.write_text("{}", encoding="utf-8")
    return Config(**values)


def app_for(tmp: Path, forwarder=None, transcriber=None, **overrides):
    return create_app(
        make_config(tmp, **overrides),
        pebble_forwarder=forwarder,
        pebble_transcriber=transcriber,
    )


class BearerCompareTests(unittest.TestCase):
    def test_compare_rejects_a_different_length_without_raising(self):
        self.assertFalse(bearer_token_matches("Bearer short", RING))
        self.assertFalse(bearer_token_matches(RING, RING))
        self.assertTrue(bearer_token_matches(f"Bearer {RING}", RING))
        self.assertFalse(bearer_token_matches("", RING))
        self.assertFalse(bearer_token_matches(f"Bearer {RING}", ""))


class ConfigDefaultTests(unittest.TestCase):
    def test_webhook_url_defaults_when_unset(self):
        env = {
            "HA_URL": "https://ha.example",
            "HA_LONG_LIVED_TOKEN": "ha-long-lived-token",
            "HA_NOTIFY_JOE_SERVICE": "notify.joe",
            "HA_NOTIFY_JESS_SERVICE": "notify.jess",
            "HOMELAB_FUNCTIONS_TOKEN": BROKER,
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.grok_pebble_webhook_url, DEFAULT_GROK_PEBBLE_WEBHOOK_URL)
        self.assertEqual(config.grok_pebble_work_webhook_url, "")
        self.assertEqual(config.grok_pebble_work_webhook_key, "")
        self.assertEqual(config.pebble_audio_ttl_seconds, 86400)
        self.assertEqual(config.pebble_ring_token, "")

    def test_blank_work_webhook_env_stays_unset(self):
        env = {
            "HA_URL": "https://ha.example",
            "HA_LONG_LIVED_TOKEN": "ha-long-lived-token",
            "HA_NOTIFY_JOE_SERVICE": "notify.joe",
            "HA_NOTIFY_JESS_SERVICE": "notify.jess",
            "HOMELAB_FUNCTIONS_TOKEN": BROKER,
            "GROK_PEBBLE_WORK_WEBHOOK_URL": "  ",
            "GROK_PEBBLE_WORK_WEBHOOK_KEY": "   ",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.grok_pebble_work_webhook_url, "")
        self.assertEqual(config.grok_pebble_work_webhook_key, "")
        self.assertEqual(config.grok_pebble_webhook_url, DEFAULT_GROK_PEBBLE_WEBHOOK_URL)


class PebbleRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.forwarder = FakeForwarder()
        self.transcriber = FakeTranscriber()
        self.client = TestClient(
            TestServer(app_for(self.root, self.forwarder, self.transcriber))
        )
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_auth_rejects_missing_wrong_shared_and_short_tokens(self):
        missing = await self.client.post("/v1/pebble", json=self.json_body())
        self.assertEqual(missing.status, 401)
        wrong = await self.client.post(
            "/v1/pebble",
            json=self.json_body(),
            headers={"Authorization": "Bearer not-the-ring-token"},
        )
        self.assertEqual(wrong.status, 401)
        shared = await self.client.post(
            "/v1/pebble",
            json=self.json_body(),
            headers={"Authorization": f"Bearer {BROKER}"},
        )
        self.assertEqual(shared.status, 401)
        short = await self.client.post(
            "/v1/pebble",
            json=self.json_body(),
            headers={"Authorization": "Bearer x"},
        )
        self.assertEqual(short.status, 401)
        notify = await self.client.post(
            "/v1/notify/joe",
            json={"title": "Hi", "message": "There"},
            headers={"Authorization": f"Bearer {RING}"},
        )
        self.assertEqual(notify.status, 401)
        self.assertEqual(self.forwarder.calls, [])

    async def test_json_forwards_only_allowlisted_fields(self):
        response = await self.post_json(
            self.json_body(note="must-not-leave-host", audio_duration_seconds=2.25)
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body, {"status": "forwarded", "event_id": "json-1", "duplicate": False})
        payload = self.forwarder.calls[0]["payload"]
        self.assertEqual(self.forwarder.calls[0]["key"], WORK_WEBHOOK_KEY)
        self.assertEqual(self.forwarder.calls[0]["url"], WORK_WEBHOOK_URL)
        self.assertEqual(payload["event_id"], "json-1")
        self.assertEqual(payload["event_type"], "double-click-hold")
        self.assertEqual(payload["timestamp"], timestamp_from_recorded_at(RECORDED_AT))
        self.assertEqual(payload["transcript"], "Hello from json")
        self.assertEqual(payload["audio_duration_seconds"], 2.25)
        self.assertEqual(payload["transcription"], "Hello from json")
        self.assertEqual(payload["recordedAt"], RECORDED_AT)
        self.assertEqual(payload["client"], "ring")
        self.assertNotIn("note", payload)
        self.assertNotIn("note", payload["original"])
        self.assertEqual(
            set(payload),
            {
                "audio_duration_seconds",
                "client",
                "event_id",
                "event_type",
                "original",
                "recordedAt",
                "timestamp",
                "transcript",
                "transcription",
            },
        )
        self.assertNotIn("audio", payload)
        self.assertNotIn("must-not-leave-host", json.dumps(payload))
        self.assertNotIn(AUDIO.decode(), json.dumps(payload))
        self.assertEqual(self.transcriber.calls, [])

    async def test_multipart_with_transcription_does_not_transcribe_or_forward_audio(self):
        response = await self.post_multipart(
            transcription="Hold lights",
            recordedAt=RECORDED_AT,
            client="ring",
            audio=AUDIO,
            headers={
                "X-Index-Trigger": "single-click-hold",
                "X-Index-Delivery": "delivery-1",
            },
        )
        self.assertEqual(response.status, 200)
        payload = self.forwarder.calls[0]["payload"]
        self.assertEqual(payload["event_id"], "delivery-1")
        self.assertEqual(payload["event_type"], "single-click-hold")
        self.assertEqual(payload["transcript"], "Hold lights")
        self.assertIsNone(payload["audio_duration_seconds"])
        self.assertNotIn(AUDIO.decode(), json.dumps(payload))
        self.assertEqual(self.transcriber.calls, [])
        stored = list((self.root / "audio").glob("*.m4a"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), AUDIO)

    async def test_audio_without_transcription_uses_the_transcriber(self):
        response = await self.post_multipart(
            recordedAt=RECORDED_AT,
            client="ring",
            audio=AUDIO,
            headers={"X-Index-Trigger": "double-click-hold", "X-Index-Delivery": "delivery-audio"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.transcriber.calls[0]["audio"], AUDIO)
        self.assertEqual(self.transcriber.calls[0]["content_type"], "audio/mp4")
        payload = self.forwarder.calls[0]["payload"]
        self.assertEqual(payload["transcript"], "lights off")
        self.assertEqual(payload["audio_duration_seconds"], 1.5)
        self.assertNotIn("transcription", payload["original"])
        self.assertNotIn(AUDIO.decode(), json.dumps(payload))

    async def test_duplicate_event_does_not_forward_again_after_restart(self):
        first = await self.post_json(self.json_body())
        self.assertEqual(first.status, 200)
        second = await self.post_json(self.json_body())
        self.assertEqual(second.status, 200)
        self.assertEqual(await second.json(), {"status": "duplicate", "event_id": "json-1", "duplicate": True})
        await self.client.close()
        restarted = TestClient(TestServer(app_for(self.root, self.forwarder, self.transcriber)))
        await restarted.start_server()
        try:
            third = await restarted.post(
                "/v1/pebble",
                json=self.json_body(),
                headers={"Authorization": f"Bearer {RING}"},
            )
            self.assertEqual(await third.json(), {"status": "duplicate", "event_id": "json-1", "duplicate": True})
        finally:
            await restarted.close()
        self.assertEqual(len(self.forwarder.calls), 1)
        self.assertEqual(self.transcriber.calls, [])

    async def test_duplicate_audio_does_not_overwrite_the_stored_recording(self):
        headers = {
            "X-Index-Trigger": "single-click-hold",
            "X-Index-Delivery": "dedupe-before-audio",
        }
        first = await self.post_multipart(
            transcription="keep the first recording",
            recordedAt=RECORDED_AT,
            audio=AUDIO,
            headers=headers,
        )
        stored = list((self.root / "audio").glob("*.m4a"))
        self.assertEqual(first.status, 200)
        self.assertEqual(len(stored), 1)

        replacement = b"REPLACEMENT-AUDIO-MUST-NOT-BE-PERSISTED"
        duplicate = await self.post_multipart(
            transcription="keep the first recording",
            recordedAt=RECORDED_AT,
            audio=replacement,
            headers=headers,
        )
        self.assertEqual(duplicate.status, 200)
        self.assertTrue((await duplicate.json())["duplicate"])
        self.assertEqual(stored[0].read_bytes(), AUDIO)
        current = list((self.root / "audio").glob("*.m4a"))
        self.assertEqual(current, stored)
        self.assertNotIn(replacement, [path.read_bytes() for path in current])

    async def test_concurrent_duplicate_does_not_forward_or_persist_twice(self):
        await self.client.close()
        blocking = BlockingForwarder()
        self.client = TestClient(TestServer(app_for(self.root, blocking, self.transcriber)))
        await self.client.start_server()
        headers = {
            "X-Index-Trigger": "single-click-hold",
            "X-Index-Delivery": "concurrent-dedupe",
        }
        first_task = asyncio.create_task(
            self.post_multipart(
                transcription="first",
                recordedAt=RECORDED_AT,
                audio=AUDIO,
                headers=headers,
            )
        )
        await blocking.started.wait()
        try:
            duplicate = await self.post_multipart(
                transcription="second",
                recordedAt=RECORDED_AT,
                audio=b"SECOND-AUDIO-MUST-NOT-BE-PERSISTED",
                headers=headers,
            )
        finally:
            blocking.release.set()
        first = await first_task

        self.assertEqual(first.status, 200)
        self.assertEqual(duplicate.status, 409)
        self.assertEqual((await duplicate.json())["error"]["code"], "event_in_progress")
        completed_duplicate = await self.post_multipart(
            transcription="third",
            recordedAt=RECORDED_AT,
            audio=b"THIRD-AUDIO-MUST-NOT-BE-PERSISTED",
            headers=headers,
        )
        self.assertEqual(completed_duplicate.status, 200)
        self.assertTrue((await completed_duplicate.json())["duplicate"])
        self.assertEqual(len(blocking.calls), 1)
        stored = list((self.root / "audio").glob("*.m4a"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), AUDIO)

    async def test_same_multipart_without_a_delivery_id_dedupes(self):
        fields = dict(transcription="same words", recordedAt=RECORDED_AT, client="ring")
        headers = {"X-Index-Trigger": "single-click-hold"}
        first = await self.post_multipart(**fields, headers=headers)
        second = await self.post_multipart(**fields, headers=headers)
        self.assertEqual(first.status, 200)
        self.assertEqual((await second.json())["duplicate"], True)
        self.assertEqual(len(self.forwarder.calls), 1)
        self.assertEqual(
            self.forwarder.calls[0]["payload"]["event_id"],
            self.forwarder.calls[0]["payload"]["event_id"],
        )

    async def test_ttl_cleanup_removes_audio_and_keeps_the_event_id(self):
        await self.client.close()
        self.client = TestClient(
            TestServer(
                app_for(
                    self.root,
                    self.forwarder,
                    self.transcriber,
                    pebble_audio_ttl_seconds=0,
                )
            )
        )
        await self.client.start_server()
        created = await self.post_multipart(
            transcription="expire me",
            recordedAt=RECORDED_AT,
            client="ring",
            audio=AUDIO,
            headers={"X-Index-Trigger": "single-click-hold", "X-Index-Delivery": "ttl-1"},
        )
        self.assertEqual(created.status, 200)
        self.assertEqual(len(list((self.root / "audio").glob("*.m4a"))), 1)
        follow = await self.post_json(self.json_body(event_id="other-event"))
        self.assertEqual(follow.status, 200)
        self.assertEqual(list((self.root / "audio").glob("*.m4a")), [])
        again = await self.post_json(
            {
                "event_id": "ttl-1",
                "event_type": "single-click-hold",
                "recordedAt": RECORDED_AT,
                "transcription": "expire me",
            }
        )
        self.assertEqual((await again.json())["duplicate"], True)
        self.assertEqual(len(self.forwarder.calls), 2)

    async def test_startup_cleanup_removes_expired_audio_before_a_request(self):
        ledger = PebbleLedger(str(self.root / "pebble.sqlite3"), str(self.root / "audio"))
        past = datetime.now(timezone.utc) - timedelta(hours=48)
        path = ledger.store_audio("old-event", AUDIO, ttl_seconds=0, now=past)
        self.assertTrue(path.exists())
        create_app(make_config(self.root))
        self.assertFalse(path.exists())

    async def test_startup_releases_an_interrupted_pending_claim(self):
        await self.client.close()
        ledger = PebbleLedger(str(self.root / "pebble.sqlite3"), str(self.root / "audio"))
        self.assertEqual(ledger.claim("interrupted-event"), "new")
        self.client = TestClient(
            TestServer(app_for(self.root, self.forwarder, self.transcriber))
        )
        await self.client.start_server()

        response = await self.post_json(self.json_body(event_id="interrupted-event"))
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "forwarded")

    async def test_webhook_failure_can_be_retried(self):
        await self.client.close()
        flaky = FakeForwarder(fail_times=1)
        self.client = TestClient(TestServer(app_for(self.root, flaky, self.transcriber)))
        await self.client.start_server()
        failed = await self.post_json(self.json_body())
        self.assertEqual(failed.status, 502)
        self.assertEqual((await failed.json())["error"]["code"], "webhook_failed")
        retried = await self.post_json(self.json_body())
        self.assertEqual((await retried.json())["status"], "forwarded")
        self.assertEqual(len(flaky.calls), 2)

    async def test_rejects_timestamps_outside_the_freshness_window(self):
        self.assertEqual(EVENT_FRESHNESS_WINDOW, timedelta(minutes=5))
        outside = timedelta(minutes=6)
        stale = await self.post_multipart(
            transcription="stale event",
            recordedAt=recorded_at(offset=-outside),
            audio=AUDIO,
            headers={
                "X-Index-Trigger": "single-click-hold",
                "X-Index-Delivery": "stale-event",
            },
        )
        self.assertEqual(stale.status, 400)
        stale_error = (await stale.json())["error"]
        self.assertEqual(stale_error["detail"], "recordedAt")
        self.assertIn("freshness window", stale_error["message"])

        future = datetime.now(timezone.utc) + outside
        future_response = await self.post_json(
            {
                "event_id": "future-event",
                "event_type": "single-click-hold",
                "timestamp": future.isoformat(),
                "transcription": "future event",
            }
        )
        self.assertEqual(future_response.status, 400)
        self.assertEqual((await future_response.json())["error"]["detail"], "timestamp")
        self.assertEqual(self.forwarder.calls, [])
        self.assertEqual(list((self.root / "audio").glob("*.m4a")), [])

    async def test_transcription_failure_does_not_stick_as_a_duplicate(self):
        self.transcriber.fail = True
        failed = await self.post_multipart(
            recordedAt=RECORDED_AT,
            client="ring",
            audio=AUDIO,
            headers={"X-Index-Trigger": "single-click-hold", "X-Index-Delivery": "stt-1"},
        )
        self.assertEqual(failed.status, 502)
        self.assertEqual(self.forwarder.calls, [])
        self.transcriber.fail = False
        retried = await self.post_multipart(
            recordedAt=RECORDED_AT,
            client="ring",
            audio=AUDIO,
            headers={"X-Index-Trigger": "single-click-hold", "X-Index-Delivery": "stt-1"},
        )
        self.assertEqual((await retried.json())["status"], "forwarded")
        self.assertEqual(len(self.forwarder.calls), 1)

    async def test_failure_paths_for_body_and_configuration(self):
        plain = await self.client.post(
            "/v1/pebble",
            data="hello",
            headers={"Authorization": f"Bearer {RING}", "Content-Type": "text/plain"},
        )
        self.assertEqual(plain.status, 415)
        audio_json = await self.post_json({"audio": "aGVsbG8=", "transcription": "nope"})
        self.assertEqual(audio_json.status, 400)
        self.assertEqual((await audio_json.json())["error"]["detail"], "audio")
        missing_text = await self.post_json({"event_type": "single-click-hold", "recordedAt": RECORDED_AT})
        self.assertEqual(missing_text.status, 400)
        self.assertEqual((await missing_text.json())["error"]["detail"], "transcription")
        missing_time = await self.post_json({"event_type": "single-click-hold", "transcription": "hi"})
        self.assertEqual(missing_time.status, 400)
        overflowing_time = await self.post_json(
            {
                "event_type": "single-click-hold",
                "recordedAt": "9" * 100,
                "transcription": "hi",
            }
        )
        self.assertEqual(overflowing_time.status, 400)
        self.assertEqual((await overflowing_time.json())["error"]["detail"], "recordedAt")
        huge = await self.post_multipart(
            recordedAt=RECORDED_AT,
            audio=b"y" * (MAX_AUDIO_BYTES + 1),
            headers={"X-Index-Trigger": "single-click-hold"},
        )
        self.assertEqual(huge.status, 413)
        self.assertEqual((await huge.json())["error"]["code"], "audio_too_large")
        self.assertEqual(self.forwarder.calls, [])

    async def test_nine_mib_body_cap_is_scoped_to_the_pebble_route(self):
        over_default = self.json_body(padding="x" * (1024 * 1024))
        accepted = await self.post_json(over_default)
        self.assertEqual(accepted.status, 200)
        self.assertNotIn("padding", self.forwarder.calls[0]["payload"])

        ordinary_route = await self.client.post(
            "/v1/notify/joe",
            data=b"x" * (1024 * 1024 + 1),
            headers={
                "Authorization": f"Bearer {BROKER}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(ordinary_route.status, 413)

        oversized = await self.client.post(
            "/v1/pebble",
            data=b"x" * (MAX_PEBBLE_BODY_BYTES + 1),
            headers={
                "Authorization": f"Bearer {RING}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(oversized.status, 413)
        self.assertEqual((await oversized.json())["error"]["code"], "request_too_large")
        self.assertEqual(len(self.forwarder.calls), 1)

    async def test_chunked_multipart_body_cannot_bypass_the_nine_mib_cap(self):
        boundary = "pebble-body-limit"
        first = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="first.m4a"\r\n'
            "Content-Type: audio/mp4\r\n\r\n"
        ).encode()
        second = (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="audio"; filename="second.m4a"\r\n'
            "Content-Type: audio/mp4\r\n\r\n"
        ).encode()
        body = (
            first
            + b"a" * MAX_AUDIO_BYTES
            + second
            + b"b" * (2 * 1024 * 1024)
            + f"\r\n--{boundary}--\r\n".encode()
        )

        async def chunks():
            for offset in range(0, len(body), 64 * 1024):
                yield body[offset : offset + 64 * 1024]

        response = await self.client.post(
            "/v1/pebble",
            data=chunks(),
            headers={
                "Authorization": f"Bearer {RING}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"]["code"], "request_too_large")
        self.assertEqual(self.forwarder.calls, [])

    async def test_missing_secrets_fail_closed(self):
        await self.client.close()
        unconfigured = TestClient(
            TestServer(app_for(self.root, self.forwarder, self.transcriber, pebble_ring_token=""))
        )
        await unconfigured.start_server()
        try:
            response = await unconfigured.post(
                "/v1/pebble",
                json=self.json_body(),
                headers={"Authorization": f"Bearer {RING}"},
            )
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "pebble_not_configured")
        finally:
            await unconfigured.close()

        no_hook = TestClient(
            TestServer(
                app_for(self.root, self.forwarder, self.transcriber, grok_pebble_webhook_key="")
            )
        )
        await no_hook.start_server()
        try:
            response = await no_hook.post(
                "/v1/pebble",
                json=self.json_body(event_type="single-click-hold"),
                headers={"Authorization": f"Bearer {RING}"},
            )
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "webhook_not_configured")
        finally:
            await no_hook.close()

        no_xai = TestClient(
            TestServer(app_for(self.root, self.forwarder, None, xai_api_key=""))
        )
        await no_xai.start_server()
        try:
            response = await no_xai.post(
                "/v1/pebble",
                data=self.multipart(recordedAt=RECORDED_AT, audio=AUDIO),
                headers={
                    "Authorization": f"Bearer {RING}",
                    "X-Index-Trigger": "single-click-hold",
                    "X-Index-Delivery": "needs-xai",
                },
            )
            self.assertEqual(response.status, 503)
            self.assertEqual((await response.json())["error"]["code"], "transcription_not_configured")
        finally:
            await no_xai.close()
        self.assertEqual(self.forwarder.calls, [])

    async def test_logs_redact_secrets_and_omit_audio(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("homelab-functions")
        previous = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info("probe %s %s %s %s %s", RING, WEBHOOK_KEY, WORK_WEBHOOK_KEY, XAI_KEY, BROKER)
            logger.info("Authorization: Bearer %s", RING)
            response = await self.post_multipart(
                transcription="secret-free transcript",
                recordedAt=RECORDED_AT,
                client="ring",
                audio=AUDIO,
                headers={"X-Index-Trigger": "single-click-hold", "X-Index-Delivery": "log-1"},
            )
            self.assertEqual(response.status, 200)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        text = stream.getvalue()
        for secret in (RING, WEBHOOK_KEY, WORK_WEBHOOK_KEY, XAI_KEY, BROKER):
            self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)
        self.assertNotIn(AUDIO.decode(), text)
        self.assertNotIn("secret-free transcript", text)

    async def test_event_ids_are_escaped_before_logging(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("homelab-functions")
        previous = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            event_id = "line-one\nforged-entry"
            forwarded = await self.post_json(self.json_body(event_id=event_id))
            duplicate = await self.post_json(self.json_body(event_id=event_id))
            self.assertEqual(forwarded.status, 200)
            self.assertTrue((await duplicate.json())["duplicate"])
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        text = stream.getvalue()
        self.assertEqual(text.count(r'event_id="line-one\nforged-entry"'), 2)
        self.assertNotIn("event_id=line-one\nforged-entry", text)

    async def test_real_forwarder_posts_json_with_bearer_and_no_audio(self):
        captured = {}

        async def take(request: web.Request) -> web.Response:
            captured["authorization"] = request.headers.get("Authorization")
            captured["content_type"] = request.content_type
            captured["body"] = await request.json()
            return web.json_response({"ok": True})

        hook = web.Application()
        hook.router.add_post("/hook", take)
        server = TestServer(hook)
        await server.start_server()
        await self.client.close()
        self.client = TestClient(
            TestServer(
                app_for(
                    self.root,
                    None,
                    self.transcriber,
                    grok_pebble_webhook_url=str(server.make_url("/hook")),
                )
            )
        )
        await self.client.start_server()
        try:
            response = await self.post_multipart(
                transcription="forward me",
                recordedAt=RECORDED_AT,
                client="ring",
                audio=AUDIO,
                headers={"X-Index-Trigger": "single-click-hold", "X-Index-Delivery": "live-1"},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(captured["authorization"], f"Bearer {WEBHOOK_KEY}")
            self.assertEqual(captured["content_type"], "application/json")
            self.assertEqual(captured["body"]["transcript"], "forward me")
            self.assertNotIn(AUDIO.decode(), json.dumps(captured["body"]))
            self.assertNotIn("audio", captured["body"])
        finally:
            await server.close()

    async def test_real_transcriber_posts_model_before_the_file(self):
        seen = {}

        async def take(request: web.Request) -> web.Response:
            raw = await request.read()
            seen["authorization"] = request.headers.get("Authorization")
            seen["body"] = raw
            return web.json_response({"text": "from xai", "duration": 2.5, "language": "en"})

        stub = web.Application()
        stub.router.add_post("/stt", take)
        server = TestServer(stub)
        await server.start_server()
        transcriber = XaiSpeechTranscriber(
            XAI_KEY,
            "grok-voice-transcribe-2.0",
            5,
            url=str(server.make_url("/stt")),
        )
        try:
            result = await transcriber.transcribe(AUDIO, filename="clip.m4a", content_type="audio/mp4")
        finally:
            await server.close()
        self.assertEqual(result.text, "from xai")
        self.assertEqual(result.duration_seconds, 2.5)
        self.assertEqual(seen["authorization"], f"Bearer {XAI_KEY}")
        body = seen["body"]
        self.assertLess(body.find(b'name="model"'), body.find(b'name="file"'))
        self.assertIn(b"grok-voice-transcribe-2.0", body)
        self.assertIn(AUDIO, body)
        self.assertNotIn(XAI_KEY.encode(), body)

    async def test_single_press_and_hold_forwards_to_the_personal_webhook(self):
        response = await self.post_json(
            self.json_body(event_id="single-1", event_type="Single-Click-Hold")
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {"status": "forwarded", "event_id": "single-1", "duplicate": False},
        )
        call = self.forwarder.calls[0]
        self.assertEqual(call["url"], DEFAULT_GROK_PEBBLE_WEBHOOK_URL)
        self.assertEqual(call["key"], WEBHOOK_KEY)
        self.assertNotEqual(call["url"], WORK_WEBHOOK_URL)
        payload = call["payload"]
        self.assertEqual(payload["event_type"], "Single-Click-Hold")
        self.assertEqual(payload["transcript"], "Hello from json")
        self.assertNotIn(RING, json.dumps(payload))
        self.assertNotIn(WEBHOOK_KEY, json.dumps(payload))
        self.assertNotIn(WORK_WEBHOOK_KEY, json.dumps(payload))
        self.assertNotIn(AUDIO.decode(), json.dumps(payload))

    async def test_double_press_and_hold_forwards_only_to_the_work_webhook(self):
        header = await self.post_multipart(
            transcription="double from header",
            recordedAt=RECORDED_AT,
            client="ring",
            headers={"X-Index-Trigger": "double-click-hold", "X-Index-Delivery": "double-header"},
        )
        self.assertEqual(header.status, 200)
        body_trigger = await self.post_multipart(
            trigger="double-click-hold",
            transcription="double from field",
            recordedAt=RECORDED_AT,
            client="ring",
            headers={"X-Index-Trigger": "single-click-hold", "X-Index-Delivery": "double-field"},
        )
        self.assertEqual(body_trigger.status, 200)
        self.assertEqual(
            [call["url"] for call in self.forwarder.calls],
            [WORK_WEBHOOK_URL, WORK_WEBHOOK_URL],
        )
        self.assertEqual(
            [call["key"] for call in self.forwarder.calls],
            [WORK_WEBHOOK_KEY, WORK_WEBHOOK_KEY],
        )
        self.assertEqual(
            [call["payload"]["event_type"] for call in self.forwarder.calls],
            ["double-click-hold", "double-click-hold"],
        )
        self.assertNotIn(DEFAULT_GROK_PEBBLE_WEBHOOK_URL, [call["url"] for call in self.forwarder.calls])
        dumped = json.dumps([call["payload"] for call in self.forwarder.calls])
        self.assertNotIn(RING, dumped)
        self.assertNotIn(WEBHOOK_KEY, dumped)
        self.assertNotIn(WORK_WEBHOOK_KEY, dumped)

    async def test_double_press_does_not_require_the_personal_webhook(self):
        await self.client.close()
        self.client = TestClient(
            TestServer(
                app_for(
                    self.root,
                    self.forwarder,
                    self.transcriber,
                    grok_pebble_webhook_key="",
                )
            )
        )
        await self.client.start_server()
        response = await self.post_json(
            self.json_body(event_id="work-only", event_type="double-click-hold")
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.forwarder.calls[0]["url"], WORK_WEBHOOK_URL)
        self.assertEqual(self.forwarder.calls[0]["key"], WORK_WEBHOOK_KEY)
        self.assertNotEqual(self.forwarder.calls[0]["url"], DEFAULT_GROK_PEBBLE_WEBHOOK_URL)

    async def test_body_trigger_beats_a_conflicting_header(self):
        response = await self.post_multipart(
            trigger="single-click-hold",
            transcription="personal wins",
            recordedAt=RECORDED_AT,
            headers={"X-Index-Trigger": "double-click-hold", "X-Index-Delivery": "body-wins"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.forwarder.calls[0]["url"], DEFAULT_GROK_PEBBLE_WEBHOOK_URL)
        self.assertEqual(self.forwarder.calls[0]["key"], WEBHOOK_KEY)
        self.assertEqual(self.forwarder.calls[0]["payload"]["event_type"], "single-click-hold")

    async def test_unknown_press_counts_are_rejected_and_not_forwarded(self):
        cases = (
            "test-event",
            "single-click",
            "double-press-and-hold",
            "2",
            "triple-click-hold",
        )
        for event_type in cases:
            with self.subTest(event_type=event_type):
                response = await self.post_json(
                    self.json_body(event_id=f"unknown-{event_type}", event_type=event_type)
                )
                self.assertEqual(response.status, 400)
                body = await response.json()
                self.assertEqual(body["error"]["code"], "unknown_press_count")
                self.assertNotIn(WEBHOOK_KEY, json.dumps(body))
                self.assertNotIn(WORK_WEBHOOK_KEY, json.dumps(body))
                self.assertNotIn(DEFAULT_GROK_PEBBLE_WEBHOOK_URL, json.dumps(body))
        self.assertEqual(self.forwarder.calls, [])
        retry = await self.post_json(self.json_body(event_id="unknown-test-event", event_type="test-event"))
        self.assertEqual(retry.status, 400)
        self.assertEqual((await retry.json())["error"]["code"], "unknown_press_count")
        recovered = await self.post_json(
            self.json_body(event_id="unknown-test-event", event_type="single-click-hold")
        )
        self.assertEqual(recovered.status, 200)
        self.assertEqual(self.forwarder.calls[0]["url"], DEFAULT_GROK_PEBBLE_WEBHOOK_URL)

    async def test_missing_work_webhook_fails_closed_without_personal_fallback(self):
        await self.client.close()
        cases = (
            {"grok_pebble_work_webhook_url": "", "grok_pebble_work_webhook_key": ""},
            {"grok_pebble_work_webhook_url": "", "grok_pebble_work_webhook_key": WORK_WEBHOOK_KEY},
            {"grok_pebble_work_webhook_url": WORK_WEBHOOK_URL, "grok_pebble_work_webhook_key": ""},
        )
        expected_names = (
            ("GROK_PEBBLE_WORK_WEBHOOK_URL", "GROK_PEBBLE_WORK_WEBHOOK_KEY"),
            ("GROK_PEBBLE_WORK_WEBHOOK_URL",),
            ("GROK_PEBBLE_WORK_WEBHOOK_KEY",),
        )
        for index, (overrides, names) in enumerate(zip(cases, expected_names, strict=True)):
            with self.subTest(missing=names):
                self.forwarder.calls.clear()
                stream = io.StringIO()
                handler = logging.StreamHandler(stream)
                logger = logging.getLogger("homelab-functions")
                logger.addHandler(handler)
                client = TestClient(
                    TestServer(app_for(self.root, self.forwarder, self.transcriber, **overrides))
                )
                await client.start_server()
                try:
                    payload = self.json_body(
                        event_id=f"work-missing-{index}",
                        event_type="double-click-hold",
                    )
                    first = await client.post(
                        "/v1/pebble",
                        json=payload,
                        headers={"Authorization": f"Bearer {RING}"},
                    )
                    second = await client.post(
                        "/v1/pebble",
                        json=payload,
                        headers={"Authorization": f"Bearer {RING}"},
                    )
                    single = await client.post(
                        "/v1/pebble",
                        json=self.json_body(
                            event_id=f"still-personal-{index}",
                            event_type="single-click-hold",
                        ),
                        headers={"Authorization": f"Bearer {RING}"},
                    )
                    outcomes = (
                        (first.status, await first.json()),
                        (second.status, await second.json()),
                    )
                    single_status = single.status
                finally:
                    logger.removeHandler(handler)
                    await client.close()
                for status, body in outcomes:
                    self.assertEqual(status, 503)
                    self.assertEqual(body["error"]["code"], "work_webhook_not_configured")
                    for name in names:
                        self.assertIn(name, body["error"]["message"])
                    encoded = json.dumps(body)
                    self.assertNotIn(WEBHOOK_KEY, encoded)
                    self.assertNotIn(WORK_WEBHOOK_KEY, encoded)
                    self.assertNotIn(DEFAULT_GROK_PEBBLE_WEBHOOK_URL, encoded)
                    self.assertNotIn(WORK_WEBHOOK_URL, encoded)
                text = stream.getvalue()
                self.assertIn("work_webhook_not_configured", text)
                self.assertIn("press_count=2", text)
                self.assertIn("destination=work", text)
                for name in names:
                    self.assertIn(name, text)
                self.assertNotIn(WEBHOOK_KEY, text)
                self.assertNotIn(WORK_WEBHOOK_KEY, text)
                self.assertNotIn(DEFAULT_GROK_PEBBLE_WEBHOOK_URL, text)
                self.assertNotIn(RING, text)
                self.assertEqual(single_status, 200)
                self.assertEqual(len(self.forwarder.calls), 1)
                self.assertEqual(self.forwarder.calls[0]["url"], DEFAULT_GROK_PEBBLE_WEBHOOK_URL)
                self.assertEqual(self.forwarder.calls[0]["key"], WEBHOOK_KEY)
                self.assertEqual(self.forwarder.calls[0]["payload"]["event_id"], f"still-personal-{index}")

    async def test_real_work_forwarder_posts_the_work_bearer_only(self):
        captured = {"personal": 0, "work": None}

        async def personal(_request: web.Request) -> web.Response:
            captured["personal"] += 1
            return web.json_response({"ok": True})

        async def work(request: web.Request) -> web.Response:
            captured["work"] = {
                "authorization": request.headers.get("Authorization"),
                "body": await request.json(),
            }
            return web.json_response({"ok": True})

        hook = web.Application()
        hook.router.add_post("/personal", personal)
        hook.router.add_post("/work", work)
        server = TestServer(hook)
        await server.start_server()
        await self.client.close()
        self.client = TestClient(
            TestServer(
                app_for(
                    self.root,
                    None,
                    self.transcriber,
                    grok_pebble_webhook_url=str(server.make_url("/personal")),
                    grok_pebble_work_webhook_url=str(server.make_url("/work")),
                )
            )
        )
        await self.client.start_server()
        try:
            response = await self.post_json(
                self.json_body(event_id="live-double", event_type="double-click-hold")
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(captured["personal"], 0)
            self.assertEqual(captured["work"]["authorization"], f"Bearer {WORK_WEBHOOK_KEY}")
            self.assertEqual(captured["work"]["body"]["event_type"], "double-click-hold")
            self.assertEqual(captured["work"]["body"]["transcript"], "Hello from json")
            self.assertNotIn("audio", captured["work"]["body"])
            dumped = json.dumps(captured["work"]["body"])
            self.assertNotIn(AUDIO.decode(), dumped)
            self.assertNotIn(RING, dumped)
            self.assertNotIn(WEBHOOK_KEY, dumped)
            self.assertNotIn(WORK_WEBHOOK_KEY, dumped)
        finally:
            await server.close()

    def json_body(self, **extra):
        payload = {
            "event_id": "json-1",
            "event_type": "double-click-hold",
            "recordedAt": RECORDED_AT,
            "transcription": "Hello from json",
            "client": "ring",
        }
        payload.update(extra)
        return payload

    def multipart(self, *, audio=None, **fields) -> FormData:
        form = FormData(default_to_multipart=True)
        for key, value in fields.items():
            form.add_field(key, value)
        if audio is not None:
            form.add_field("audio", audio, filename="clip.m4a", content_type="audio/mp4")
        return form

    async def post_json(self, payload):
        return await self.client.post(
            "/v1/pebble",
            json=payload,
            headers={"Authorization": f"Bearer {RING}"},
        )

    async def post_multipart(self, *, audio=None, headers=None, **fields):
        merged = {"Authorization": f"Bearer {RING}"}
        if headers:
            merged.update(headers)
        return await self.client.post("/v1/pebble", data=self.multipart(audio=audio, **fields), headers=merged)


if __name__ == "__main__":
    unittest.main()
