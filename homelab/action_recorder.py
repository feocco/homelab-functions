from __future__ import annotations

"""Background recorder for Home Assistant mobile notification actions."""

import asyncio
import logging
from typing import Any, Callable

from .home_assistant import HomeAssistantConfig, HomeAssistantWebSocketClient
from .notification_ledger import NotificationLedger


LOGGER = logging.getLogger(__name__)
MOBILE_ACTION_EVENT = "mobile_app_notification_action"


def record_notification_action_event(
    ledger: NotificationLedger,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    """Record one Home Assistant mobile notification action event."""

    if event.get("event_type") != MOBILE_ACTION_EVENT:
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if not isinstance(action, str) or not action.strip():
        return None

    action_event: dict[str, Any] = {"action": action.strip(), "event": data}
    for field in ("tag", "group", "reply_text"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            action_event[field] = value.strip()

    return ledger.record_action(action_event)


class NotificationActionRecorder:
    """Listen to Home Assistant action events and persist them to the ledger."""

    def __init__(
        self,
        ledger: NotificationLedger,
        config: HomeAssistantConfig,
        *,
        client_factory: Callable[[HomeAssistantConfig], HomeAssistantWebSocketClient] = HomeAssistantWebSocketClient,
        reconnect_delay_seconds: float = 10,
    ) -> None:
        self.ledger = ledger
        self.config = config
        self.client_factory = client_factory
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.connected = False
        self.last_error = ""
        self.last_action_id: int | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                LOGGER.warning("Home Assistant notification action recorder disconnected: %s", exc)
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def _run_once(self) -> None:
        async with self.client_factory(self.config) as ha:
            ha.add_event_handler(self.handle_event)
            await ha.subscribe_events(MOBILE_ACTION_EVENT)
            self.connected = True
            self.last_error = ""
            LOGGER.info("Recording Home Assistant mobile notification action events")
            await ha.wait_closed()

    async def handle_event(self, event: dict[str, Any]) -> None:
        result = record_notification_action_event(self.ledger, event)
        if result is not None:
            self.last_action_id = result["action_id"]
