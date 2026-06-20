from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class NotificationLedger:
    path: str

    def __post_init__(self) -> None:
        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    tag TEXT,
                    group_name TEXT,
                    url TEXT,
                    ha_context_id TEXT,
                    payload_json TEXT NOT NULL,
                    service_data_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id INTEGER,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    tag TEXT,
                    group_name TEXT,
                    reply_text TEXT,
                    event_json TEXT NOT NULL,
                    FOREIGN KEY(notification_id) REFERENCES notifications(id)
                );

                CREATE TABLE IF NOT EXISTS workflow_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workflow_slug TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source TEXT,
                    notification_id INTEGER,
                    event_json TEXT NOT NULL
                );
                """
            )

    def record_sent(
        self,
        notification: dict[str, Any],
        service_data: dict[str, Any],
        *,
        ha_context_id: str | None,
    ) -> dict[str, Any]:
        timestamp = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notifications (
                    created_at, updated_at, status, title, message, tag, group_name,
                    url, ha_context_id, payload_json, service_data_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    timestamp,
                    "sent",
                    notification["title"],
                    notification["message"],
                    notification.get("tag"),
                    notification.get("group"),
                    notification.get("url"),
                    ha_context_id,
                    stable_json(notification),
                    stable_json(service_data),
                ),
            )
            notification_id = int(cursor.lastrowid)

        return self.get_notification(notification_id) or {}

    def record_action(self, action_event: dict[str, Any]) -> dict[str, Any]:
        timestamp = utc_now()
        tag = action_event.get("tag")
        group = action_event.get("group")
        event_json = stable_json(action_event.get("event") or {})
        existing = self._existing_action(action=action_event["action"], tag=tag, group=group, event_json=event_json)
        if existing is not None:
            return existing

        notification_id = self._latest_matching_notification_id(tag=tag, group=group)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO notification_actions (
                    notification_id, created_at, action, tag, group_name, reply_text, event_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    timestamp,
                    action_event["action"],
                    tag,
                    group,
                    action_event.get("reply_text"),
                    event_json,
                ),
            )
            action_id = int(cursor.lastrowid)
            if notification_id is not None:
                connection.execute(
                    "UPDATE notifications SET status = ?, updated_at = ? WHERE id = ?",
                    ("responded", timestamp, notification_id),
                )

        return {
            "status": "recorded",
            "action_id": action_id,
            "notification_id": notification_id,
        }

    def _existing_action(
        self,
        *,
        action: str,
        tag: str | None,
        group: str | None,
        event_json: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, notification_id FROM notification_actions
                WHERE action = ?
                  AND COALESCE(tag, '') = COALESCE(?, '')
                  AND COALESCE(group_name, '') = COALESCE(?, '')
                  AND event_json = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (action, tag, group, event_json),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": "recorded",
            "action_id": int(row["id"]),
            "notification_id": row["notification_id"],
        }

    def list_notifications(
        self,
        *,
        limit: int = 50,
        group: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        clauses: list[str] = []
        params: list[Any] = []
        if group:
            clauses.append("group_name = ?")
            params.append(group)
        if tag:
            clauses.append("tag = ?")
            params.append(tag)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM notifications
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def record_workflow_report(self, report: dict[str, Any]) -> dict[str, Any]:
        timestamp = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO workflow_reports (
                    created_at, updated_at, status, workflow_slug, summary,
                    source, notification_id, event_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    timestamp,
                    "reported",
                    report["workflow_slug"],
                    report["summary"],
                    report.get("source"),
                    report.get("notification_id"),
                    stable_json(report.get("event") or {}),
                ),
            )
            report_id = int(cursor.lastrowid)

        return self.get_workflow_report(report_id) or {}

    def list_workflow_reports(
        self,
        *,
        limit: int = 50,
        workflow: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        clauses: list[str] = []
        params: list[Any] = []
        if workflow:
            clauses.append("workflow_slug = ?")
            params.append(workflow)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM workflow_reports
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [self._workflow_report_from_row(row) for row in rows]

    def get_workflow_report(self, report_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
        return self._workflow_report_from_row(row) if row else None

    def get_notification(self, notification_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        return self._notification_from_row(row) if row else None

    def _latest_matching_notification_id(
        self,
        *,
        tag: str | None,
        group: str | None,
    ) -> int | None:
        if not tag:
            return None
        clauses = ["tag = ?"]
        params: list[Any] = [tag]
        if group:
            clauses.append("group_name = ?")
            params.append(group)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT id FROM notifications
                WHERE {' AND '.join(clauses)}
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return int(row["id"]) if row else None

    def _notification_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        actions = self._actions_for_notification(int(row["id"]))
        return {
            "id": int(row["id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "title": row["title"],
            "message": row["message"],
            "tag": row["tag"],
            "group": row["group_name"],
            "url": row["url"],
            "ha_context_id": row["ha_context_id"],
            "payload": json.loads(row["payload_json"]),
            "service_data": json.loads(row["service_data_json"]),
            "actions": actions,
        }

    def _actions_for_notification(self, notification_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notification_actions
                WHERE notification_id = ?
                ORDER BY id ASC
                """,
                (notification_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "created_at": row["created_at"],
                "action": row["action"],
                "tag": row["tag"],
                "group": row["group_name"],
                "reply_text": row["reply_text"],
                "event": json.loads(row["event_json"]),
            }
            for row in rows
        ]

    def _workflow_report_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "workflow_slug": row["workflow_slug"],
            "summary": row["summary"],
            "source": row["source"],
            "notification_id": row["notification_id"],
            "event": json.loads(row["event_json"]),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
