"""Lazy public exports.

Package import stays free of aiohttp so `python -m homelab.mcp_server` can run
on a system Python that has not installed the HTTP server dependencies.
"""

from __future__ import annotations

import importlib
from typing import Any


_EXPORTS = {
    "HomeAssistantConfig": ("homelab.home_assistant", "HomeAssistantConfig"),
    "HomeAssistantError": ("homelab.home_assistant", "HomeAssistantError"),
    "HomeAssistantWebSocketClient": ("homelab.home_assistant", "HomeAssistantWebSocketClient"),
    "HomelabFunctionsClient": ("homelab.client", "HomelabFunctionsClient"),
    "HomelabFunctionsError": ("homelab.client", "HomelabFunctionsError"),
    "NotificationActionRouter": ("homelab.notification_actions", "NotificationActionRouter"),
    "get_service_catalog": ("homelab.client", "get_service_catalog"),
    "get_smoke_signal_targets": ("homelab.client", "get_smoke_signal_targets"),
    "get_workflow_report": ("homelab.client", "get_workflow_report"),
    "list_notifications": ("homelab.client", "list_notifications"),
    "list_workflow_reports": ("homelab.client", "list_workflow_reports"),
    "notify_jess": ("homelab.client", "notify_jess"),
    "notify_joe": ("homelab.client", "notify_joe"),
    "record_notification_action": ("homelab.client", "record_notification_action"),
    "record_workflow_report": ("homelab.client", "record_workflow_report"),
    "workflow_report_button": ("homelab.client", "workflow_report_button"),
    "websocket_url": ("homelab.home_assistant", "websocket_url"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
