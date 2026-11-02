"""Fakes shared by the Waiting on me tests. All data here is fake."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homelab.json_http import JsonResponse, TransportError
from homelab.server import Config
from homelab.waiting import Category, CursorCallError, Inbox, InboxItem, PrimaryAction


TOKEN = "waiting-test-token-0123456789"
CURSOR_KEY = "cursor-test-key-0123456789"


def make_item(
    number: int = 14,
    *,
    repo: str = "feocco/homelab-functions",
    category: Category = Category.SECRET,
    need: str = "Add the fake key",
    why: str = "The fake service cannot start without it.",
    updated_at: str = "2026-11-01T12:00:00+00:00",
    head_ref: str | None = None,
    pr_url: str | None = None,
) -> InboxItem:
    source_url = f"https://github.com/{repo}/pull/{number}"
    if category is Category.APPROVE_MERGE:
        item_id = f"{repo}#{number}:approve_merge"
        action = PrimaryAction("open", "Review and merge on GitHub", url=source_url)
    else:
        item_id = f"{repo}#{number}:{category.value}:{hashlib.sha1(need.encode()).hexdigest()[:10]}"
        if category is Category.COPY:
            action = PrimaryAction("copy", "Copy", value="echo fake")
        else:
            action = PrimaryAction("open", "Open", url=source_url)
    return InboxItem(
        id=item_id,
        category=category,
        need=need,
        why=why,
        action=action,
        source_url=source_url,
        source_label=f"{repo}#{number}",
        repo=repo,
        updated_at=updated_at,
        head_ref=head_ref,
        pr_url=pr_url,
    )


class StaticSource:
    owner = "feocco"

    def __init__(self, items: list[InboxItem] | None = None) -> None:
        self.items = list(items or [])
        self.error: Exception | None = None
        self.fetches = 0

    async def fetch(self) -> Inbox:
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return Inbox(list(self.items))


@dataclass
class Recorded:
    method: str
    url: str
    headers: dict[str, str]
    payload: Any


class FakeTransport:
    """Answers each request with the first handler whose predicate matches."""

    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.handlers: list[tuple[Callable[[Recorded], bool], Callable[[Recorded], JsonResponse]]] = []

    def on(self, predicate: Callable[[Recorded], bool], respond: Callable[[Recorded], JsonResponse]) -> None:
        self.handlers.append((predicate, respond))

    async def request(self, method: str, url: str, *, headers: dict[str, str], payload: Any | None = None) -> JsonResponse:
        recorded = Recorded(method, url, dict(headers), payload)
        self.requests.append(recorded)
        for predicate, respond in self.handlers:
            if predicate(recorded):
                return respond(recorded)
        raise AssertionError(f"unexpected request {method} {url}")


def raise_transport(maybe_delivered: bool) -> Callable[[Recorded], JsonResponse]:
    def respond(_request: Recorded) -> JsonResponse:
        raise TransportError("timed out" if maybe_delivered else "could not connect", maybe_delivered=maybe_delivered)

    return respond


@dataclass
class FakeCursor:
    """In-memory `CursorAgents`. Queue failures to script Cursor's behavior."""

    launches: list[dict[str, Any]] = field(default_factory=list)
    follow_ups: list[tuple[str, str]] = field(default_factory=list)
    launch_failures: list[CursorCallError] = field(default_factory=list)
    follow_up_failures: list[CursorCallError] = field(default_factory=list)
    existing_agents: set[str] = field(default_factory=set)
    gate: asyncio.Event | None = None

    async def launch(self, *, agent_id: str, name: str, prompt: str, item: InboxItem) -> str:
        if self.gate is not None:
            await self.gate.wait()
        self.launches.append({"agent_id": agent_id, "name": name, "prompt": prompt, "item": item.id})
        if self.launch_failures:
            failure = self.launch_failures.pop(0)
            if failure.maybe_delivered:
                self.existing_agents.add(agent_id)
            raise failure
        if agent_id in self.existing_agents:
            raise CursorCallError("exists", code="agent_id_conflict", maybe_delivered=False, status=409)
        self.existing_agents.add(agent_id)
        return f"https://cursor.com/agents/{agent_id}"

    async def follow_up(self, agent_id: str, prompt: str) -> str:
        if self.gate is not None:
            await self.gate.wait()
        if self.follow_up_failures:
            raise self.follow_up_failures.pop(0)
        self.follow_ups.append((agent_id, prompt))
        return "followup"


def waiting_config(tmp: Path, **overrides: Any) -> Config:
    values: dict[str, Any] = {
        "ha_url": "https://example.ui.nabu.casa",
        "ha_long_lived_token": "ha-token",
        "ha_notify_joe_service": "notify.mobile_app_pixel",
        "ha_notify_jess_service": "notify.mobile_app_jess",
        "homelab_functions_token": TOKEN,
        "notification_ledger_path": str(tmp / "notifications.sqlite3"),
        "notification_action_recorder_enabled": False,
        "pebble_ledger_path": str(tmp / "pebble.sqlite3"),
        "pebble_audio_dir": str(tmp / "pebble-audio"),
        "dashboard_public_url": "https://waiting.home.feocco.com",
        "waiting_feedback_path": str(tmp / "waiting-feedback.sqlite3"),
        "dashboard_refresh_seconds": 3600,
    }
    values.update(overrides)
    return Config(**values)
