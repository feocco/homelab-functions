"""Pebble Index ring relay.

The phone posts multipart audio or JSON. This module turns that into a JSON
event, keeps the audio file locally until a short TTL, and forwards only the
JSON. It never logs the ring token, the webhook key, or the xAI key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, FormData


LOGGER = logging.getLogger("homelab-functions")

DEFAULT_GROK_PEBBLE_WEBHOOK_URL = (
    "https://api2.cursor.sh/automations/webhook/e3f09362-8a64-5de6-989c-c5d4b75e6238"
)
DEFAULT_AUDIO_TTL_SECONDS = 24 * 60 * 60
DEFAULT_STT_URL = "https://api.x.ai/v1/stt"
DEFAULT_STT_MODEL = "grok-voice-transcribe-2.0"
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_TEXT_FIELD_BYTES = 64 * 1024
MAX_TEXT_FIELDS = 32
MAX_EVENT_ID_LENGTH = 200

_AUTHORIZATION_RE = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")
_BEARER_PREFIX = "Bearer "


class PebbleRequestError(Exception):
    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class WebhookError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"webhook status {status}")
        self.status = status


@dataclass(frozen=True)
class Transcript:
    text: str
    duration_seconds: float | None


@dataclass(frozen=True)
class PebbleSettings:
    ring_token: str
    webhook_url: str
    webhook_key: str
    xai_api_key: str
    xai_stt_model: str
    audio_ttl_seconds: int
    timeout_seconds: float

    @classmethod
    def from_config(cls, config: Any) -> "PebbleSettings":
        return cls(
            ring_token=config.pebble_ring_token,
            webhook_url=config.grok_pebble_webhook_url,
            webhook_key=config.grok_pebble_webhook_key,
            xai_api_key=config.xai_api_key,
            xai_stt_model=config.xai_stt_model,
            audio_ttl_seconds=config.pebble_audio_ttl_seconds,
            timeout_seconds=config.request_timeout_seconds,
        )


class SecretRedactingFilter(logging.Filter):
    """Drop configured secrets and bearer values from log records."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if len(secret) >= 12)

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = self._redact(record.getMessage())
        record.msg = rendered
        record.args = ()
        return True

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return _AUTHORIZATION_RE.sub(r"\1[redacted]", text)


def install_secret_redaction(*secrets: str) -> None:
    redactor = SecretRedactingFilter(tuple(secrets))
    for name in ("homelab-functions", "aiohttp.access", "aiohttp.server", "aiohttp.web"):
        logger = logging.getLogger(name)
        logger.addFilter(redactor)


def bearer_token_matches(authorization: str, expected: str) -> bool:
    """Compare a bearer token in constant time, including when lengths differ."""

    presented = authorization[len(_BEARER_PREFIX) :] if authorization.startswith(_BEARER_PREFIX) else ""
    presented_digest = hashlib.sha256(presented.encode()).digest()
    expected_digest = hashlib.sha256(expected.encode()).digest()
    digest_matches = hmac.compare_digest(presented_digest, expected_digest)
    prefix_matches = authorization.startswith(_BEARER_PREFIX)
    return bool(expected) and prefix_matches and digest_matches


def timestamp_from_recorded_at(value: str) -> str:
    try:
        milliseconds = int(value)
    except ValueError as exc:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "recordedAt must be unix milliseconds",
            detail="recordedAt",
        ) from exc
    if milliseconds < 0:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "recordedAt must be unix milliseconds",
            detail="recordedAt",
        )
    moment = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_timestamp(value: str) -> str:
    text = value.strip()
    if text.isdigit():
        return timestamp_from_recorded_at(text)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "timestamp must be an ISO-8601 time",
            detail="timestamp",
        ) from exc
    if moment.tzinfo is None:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "timestamp must include a timezone",
            detail="timestamp",
        )
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_duration(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "audio_duration_seconds must be a number",
            detail="audio_duration_seconds",
        ) from exc
    if parsed < 0 or parsed > 24 * 60 * 60:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "audio_duration_seconds is out of range",
            detail="audio_duration_seconds",
        )
    return parsed


def _require_text(value: str, field: str, *, limit: int) -> str:
    text = value.strip()
    if not text:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            f"{field} must be a non-empty string",
            detail=field,
        )
    if len(text) > limit:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            f"{field} is too long",
            detail=field,
        )
    return text


class PebbleLedger:
    """Event-id dedupe plus local audio files. Dedupe rows outlive the audio TTL."""

    def __init__(self, path: str, audio_dir: str) -> None:
        self.path = path
        self.audio_dir = audio_dir
        self._lock = threading.Lock()
        database = Path(path)
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audio_files (
                    event_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    def claim(self, event_id: str) -> str:
        """Return ``new``, ``retry``, or ``duplicate``.

        ``retry`` means a previous attempt stored the id and then died before
        the forward was marked. The caller forwards again.
        """

        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO events (event_id, status, created_at, updated_at)
                    VALUES (?, 'pending', ?, ?)
                    """,
                    (event_id, now, now),
                )
                return "new"
            if row["status"] == "forwarded":
                return "duplicate"
            connection.execute(
                "UPDATE events SET updated_at = ? WHERE event_id = ?",
                (now, event_id),
            )
            return "retry"

    def mark_forwarded(self, event_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE events SET status = 'forwarded', updated_at = ? WHERE event_id = ?",
                (_utc_now(), event_id),
            )

    def release(self, event_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM events WHERE event_id = ? AND status = 'pending'",
                (event_id,),
            )

    def store_audio(self, event_id: str, audio: bytes, *, ttl_seconds: int, now: datetime) -> Path:
        root = Path(self.audio_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"{hashlib.sha256(event_id.encode()).hexdigest()}.m4a").resolve()
        if not path.is_relative_to(root):
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "audio path escaped the storage directory",
                detail="audio",
            )
        path.write_bytes(audio)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        created_at = now.isoformat(timespec="seconds")
        with self._transaction() as connection:
            connection.execute(
                    """
                    INSERT INTO audio_files (event_id, path, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        path = excluded.path,
                        expires_at = excluded.expires_at
                    """,
                    (event_id, str(path), expires_at, created_at),
                )
        return path

    def cleanup_expired(self, now: datetime) -> int:
        stamp = now.isoformat(timespec="seconds")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT event_id, path FROM audio_files WHERE expires_at <= ?",
                (stamp,),
            ).fetchall()
            for row in rows:
                _unlink_audio(row["path"], Path(self.audio_dir).resolve())
                connection.execute(
                    "DELETE FROM audio_files WHERE event_id = ?",
                    (row["event_id"],),
                )
            return len(rows)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection


class AiohttpJsonForwarder:
    """POST a JSON object. The body is the caller's object and nothing else."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    async def post_json(self, url: str, key: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        timeout = ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Authorization": f"{_BEARER_PREFIX}{key}",
            "Content-Type": "application/json",
        }
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.post(url, data=body, headers=headers, allow_redirects=False) as response:
                    await response.read()
                    if response.status >= 400 or 300 <= response.status < 400:
                        raise WebhookError(response.status)
        except asyncio.TimeoutError as exc:
            raise PebbleRequestError(
                HTTPStatus.GATEWAY_TIMEOUT,
                "webhook_timeout",
                "Timed out forwarding the pebble event",
            ) from exc
        except ClientError as exc:
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "webhook_failed",
                "Grok webhook request failed",
            ) from exc
        except WebhookError as exc:
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "webhook_failed",
                "Grok webhook rejected the pebble event",
            ) from exc


class XaiSpeechTranscriber:
    """Transcribe one audio file with xAI. The API key stays in the header."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        *,
        url: str = DEFAULT_STT_URL,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.url = url

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> Transcript:
        form = FormData()
        form.add_field("model", self.model)
        safe_name = Path(filename).name or "recording.m4a"
        form.add_field(
            "file",
            audio,
            filename=safe_name,
            content_type=content_type or "audio/mp4",
        )
        timeout = ClientTimeout(total=self.timeout_seconds)
        headers = {"Authorization": f"{_BEARER_PREFIX}{self.api_key}"}
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.url,
                    data=form,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    raw = await response.read()
                    if response.status != 200:
                        raise PebbleRequestError(
                            HTTPStatus.BAD_GATEWAY,
                            "transcription_failed",
                            "Speech-to-text request failed",
                        )
        except asyncio.TimeoutError as exc:
            raise PebbleRequestError(
                HTTPStatus.GATEWAY_TIMEOUT,
                "transcription_timeout",
                "Timed out transcribing audio",
            ) from exc
        except ClientError as exc:
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "transcription_failed",
                "Speech-to-text request failed",
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "transcription_failed",
                "Speech-to-text returned invalid JSON",
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise PebbleRequestError(
                HTTPStatus.BAD_GATEWAY,
                "transcription_failed",
                "Speech-to-text returned an empty transcript",
            )
        duration = payload.get("duration") if isinstance(payload, dict) else None
        duration_seconds = float(duration) if isinstance(duration, (int, float)) else None
        return Transcript(text=text.strip(), duration_seconds=duration_seconds)


def prepare_pebble_storage(ledger_path: str, audio_dir: str) -> None:
    """Drop expired audio when the process starts and a ledger is already present."""

    if Path(ledger_path).exists():
        PebbleLedger(ledger_path, audio_dir).cleanup_expired(datetime.now(timezone.utc))


async def read_pebble_request(request: Any) -> tuple[dict[str, Any], bytes | None, str, str]:
    content_type = request.content_type
    if content_type == "application/json":
        return await _read_json(request)
    if content_type == "multipart/form-data":
        return await _read_multipart(request)
    raise PebbleRequestError(
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        "unsupported_media_type",
        "Content-Type must be multipart/form-data or application/json",
    )


async def relay_event(
    request: Any,
    *,
    settings: PebbleSettings,
    ledger: PebbleLedger,
    forwarder: Any,
    transcriber: Any | None,
) -> dict[str, Any]:
    if not settings.ring_token:
        raise PebbleRequestError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "pebble_not_configured",
            "PEBBLE_RING_TOKEN is not configured",
        )
    authorization = request.headers.get("Authorization", "")
    if not bearer_token_matches(authorization, settings.ring_token):
        raise PebbleRequestError(
            HTTPStatus.UNAUTHORIZED,
            "unauthorized",
            "Invalid or missing pebble bearer token",
        )
    if not settings.webhook_key:
        raise PebbleRequestError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "webhook_not_configured",
            "GROK_PEBBLE_WEBHOOK_KEY is not configured",
        )

    now = datetime.now(timezone.utc)
    ledger.cleanup_expired(now)
    fields, audio, filename, content_type = await read_pebble_request(request)
    event = _normalize(fields, audio, request.headers)
    needs_transcript = event["transcript"] is None
    if needs_transcript and audio is None:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "transcription is required when no audio is attached",
            detail="transcription",
        )
    if needs_transcript and transcriber is None and not settings.xai_api_key:
        raise PebbleRequestError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "transcription_not_configured",
            "XAI_API_KEY is not configured",
        )
    duration = None if needs_transcript else _parse_duration(fields.get("audio_duration_seconds"))
    if audio is not None:
        ledger.store_audio(
            event["event_id"],
            audio,
            ttl_seconds=settings.audio_ttl_seconds,
            now=now,
        )
    claim = ledger.claim(event["event_id"])
    if claim == "duplicate":
        LOGGER.info("pebble relay duplicate event_id=%s", event["event_id"])
        return {"status": "duplicate", "event_id": event["event_id"], "duplicate": True}
    try:
        transcript_text = event["transcript"]
        if needs_transcript:
            active = transcriber or XaiSpeechTranscriber(
                settings.xai_api_key,
                settings.xai_stt_model,
                settings.timeout_seconds,
            )
            transcribed = await active.transcribe(audio or b"", filename=filename, content_type=content_type)
            transcript_text = transcribed.text
            duration = transcribed.duration_seconds
        payload = _forward_payload(event, transcript_text or "", duration, fields)
        await forwarder.post_json(settings.webhook_url, settings.webhook_key, payload)
    except Exception:
        ledger.release(event["event_id"])
        raise
    ledger.mark_forwarded(event["event_id"])
    LOGGER.info("pebble relay forwarded event_id=%s", event["event_id"])
    return {"status": "forwarded", "event_id": event["event_id"], "duplicate": False}


def _normalize(fields: dict[str, Any], audio: bytes | None, headers: Any) -> dict[str, Any]:
    event_type = fields.get("event_type") or fields.get("trigger") or headers.get("X-Index-Trigger")
    if not isinstance(event_type, str):
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "event type is required",
            detail="event_type",
        )
    event_type = _require_text(event_type, "event_type", limit=80)
    recorded_at = fields.get("recordedAt")
    timestamp_field = fields.get("timestamp")
    if recorded_at is None and timestamp_field is None:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "recordedAt or timestamp is required",
            detail="recordedAt",
        )
    if recorded_at is not None and not isinstance(recorded_at, str):
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "recordedAt must be a string",
            detail="recordedAt",
        )
    if timestamp_field is not None and not isinstance(timestamp_field, str):
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "timestamp must be a string",
            detail="timestamp",
        )
    timestamp = (
        timestamp_from_recorded_at(recorded_at)
        if isinstance(recorded_at, str)
        else normalize_timestamp(timestamp_field or "")
    )
    explicit_id = fields.get("event_id") or headers.get("X-Index-Delivery")
    if explicit_id is not None:
        if not isinstance(explicit_id, str):
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "event_id must be a string",
                detail="event_id",
            )
        event_id = _require_text(explicit_id, "event_id", limit=MAX_EVENT_ID_LENGTH)
    else:
        transcript_hint = fields.get("transcription") if isinstance(fields.get("transcription"), str) else ""
        material = transcript_hint or hashlib.sha256(audio or b"").hexdigest()
        client = fields.get("client") if isinstance(fields.get("client"), str) else ""
        identity = "\n".join((event_type, recorded_at or timestamp, client, material))
        event_id = hashlib.sha256(identity.encode()).hexdigest()
    transcript = fields.get("transcript") if isinstance(fields.get("transcript"), str) else None
    transcription = fields.get("transcription") if isinstance(fields.get("transcription"), str) else None
    chosen = transcript if transcript and transcript.strip() else transcription
    if chosen is not None and not chosen.strip():
        chosen = None
    return {
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "transcript": None if chosen is None else chosen.strip(),
    }


def _forward_payload(
    event: dict[str, Any],
    transcript: str,
    duration: float | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    original = {key: value for key, value in fields.items()}
    payload: dict[str, Any] = {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "timestamp": event["timestamp"],
        "transcript": transcript,
        "audio_duration_seconds": duration,
    }
    for key, value in original.items():
        if key not in payload:
            payload[key] = value
    payload["original"] = original
    return payload


async def _read_json(request: Any) -> tuple[dict[str, Any], None, str, str]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "Request body must be a JSON object",
        ) from exc
    if not isinstance(payload, dict):
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "Request body must be a JSON object",
        )
    if "audio" in payload:
        raise PebbleRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_request",
            "Send audio as multipart/form-data",
            detail="audio",
        )
    fields: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "JSON keys must be strings",
            )
        if value is None or isinstance(value, (dict, list)):
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                f"{key} must be a string or number",
                detail=key,
            )
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
            continue
        if isinstance(value, (int, float)) and key == "audio_duration_seconds":
            fields[key] = value
            continue
        if isinstance(value, (int, float)):
            fields[key] = str(value)
            continue
        if not isinstance(value, str):
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                f"{key} must be a string or number",
                detail=key,
            )
        fields[key] = value
    return fields, None, "", ""


async def _read_multipart(request: Any) -> tuple[dict[str, Any], bytes | None, str, str]:
    fields: dict[str, Any] = {}
    audio: bytes | None = None
    filename = "recording.m4a"
    content_type = "audio/mp4"
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        name = part.name or ""
        if len(fields) + (1 if audio is not None else 0) >= MAX_TEXT_FIELDS and name != "audio":
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Too many form fields",
            )
        if name == "audio" or part.filename:
            if name != "audio":
                raise PebbleRequestError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "Only the audio file part is accepted",
                    detail=name or "file",
                )
            audio = await _read_limited(part, MAX_AUDIO_BYTES, "audio")
            filename = Path(part.filename or "recording.m4a").name or "recording.m4a"
            content_type = part.headers.get("Content-Type", "audio/mp4").split(";", 1)[0].strip()
            continue
        raw = await _read_limited(part, MAX_TEXT_FIELD_BYTES, name or "field")
        try:
            fields[name] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PebbleRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Form fields must be UTF-8 text",
                detail=name or "field",
            ) from exc
    if audio == b"":
        audio = None
    return fields, audio, filename, content_type or "audio/mp4"


async def _read_limited(part: Any, limit: int, field: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await part.read_chunk(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            code_status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if field == "audio" else HTTPStatus.BAD_REQUEST
            raise PebbleRequestError(
                code_status,
                "audio_too_large" if field == "audio" else "invalid_request",
                "Audio payload is too large" if field == "audio" else f"{field} is too long",
                detail=field,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _unlink_audio(path_text: str, root: Path) -> None:
    try:
        path = Path(path_text).resolve()
    except OSError:
        return
    if not path.is_relative_to(root):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("pebble audio cleanup failed")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
