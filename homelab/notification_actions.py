from __future__ import annotations

"""Helpers for routing Home Assistant mobile notification action events."""

from typing import Any, Callable


ACTION_SEPARATOR = "::"
MOBILE_ACTION_EVENT = "mobile_app_notification_action"
ActionHandler = Callable[[str, dict[str, Any]], None]


class NotificationActionRouter:
    """Route Home Assistant mobile notification action events by action prefix.

    Typical usage inside a long-running service:

    ```python
    import homelab

    router = homelab.NotificationActionRouter()
    router.register("MY_SERVICE_APPROVE", handle_approval)

    async def handle_ha_event(event: dict) -> None:
        router.handle_event(event)
    ```

    Handlers receive the opaque action value and the full Home Assistant event.
    The router only parses `mobile_app_notification_action` events and ignores
    unrelated or malformed events.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ActionHandler] = {}

    @staticmethod
    def make_action(prefix: str, value: str) -> str:
        """Return an action id suitable for Home Assistant notification buttons."""

        prefix = clean_part(prefix, "prefix")
        value = clean_part(value, "value")
        return f"{prefix}{ACTION_SEPARATOR}{value}"

    def register(self, prefix: str, handler: ActionHandler) -> None:
        """Register a handler for action ids created with `make_action`."""

        self._handlers[clean_part(prefix, "prefix")] = handler

    def handle_event(self, event: dict[str, Any]) -> bool:
        """Handle one Home Assistant event and return whether it was routed."""

        if event.get("event_type") != MOBILE_ACTION_EVENT:
            return False
        data = event.get("data")
        if not isinstance(data, dict):
            return False
        action = data.get("action")
        if not isinstance(action, str):
            return False

        prefix, separator, value = action.partition(ACTION_SEPARATOR)
        if not separator or not prefix or not value:
            return False

        handler = self._handlers.get(prefix)
        if handler is None:
            return False

        handler(value, event)
        return True


def clean_part(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    if ACTION_SEPARATOR in cleaned:
        raise ValueError(f"{field} must not contain {ACTION_SEPARATOR!r}")
    return cleaned
