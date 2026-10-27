"""Callers of the protected actions and the ledger conventions each one owns.

`CONSUMERS` feeds the caller matrix on `GET /docs` and in
`docs/architecture.md`. Each use names an `ACCESS_VECTOR` operation id, so a
renamed or removed action fails at import instead of leaving stale docs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .access import ACCESS_VECTOR
from .client import WORKFLOW_REPORT_ACTION_PREFIX


@dataclass(frozen=True)
class ConsumerUse:
    operation_id: str
    condition: str = ""

    def __post_init__(self) -> None:
        if self.operation_id not in ACCESS_VECTOR:
            raise ValueError(f"Unknown operation in consumer matrix: {self.operation_id}")


@dataclass(frozen=True)
class ConsumerHelper:
    name: str
    arguments: str

    def call(self) -> str:
        return f"{self.name}({self.arguments})"


@dataclass(frozen=True)
class Consumer:
    """One caller row: which actions it uses and which ledger keys it owns.

    `groups`, `tags`, and `action_prefixes` are conventions for the shared
    notification ledger. The server does not enforce them.
    """

    slug: str
    name: str
    uses: tuple[ConsumerUse, ...]
    recipe_title: str
    recipe: str
    name_is_identifier: bool = True
    note: str = ""
    members: tuple[str, ...] = ()
    helpers: tuple[ConsumerHelper, ...] = ()
    groups: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    action_prefixes: tuple[str, ...] = ()

    def operation_ids(self) -> tuple[str, ...]:
        return tuple(use.operation_id for use in self.uses)

    def caller_label(self) -> str:
        label = f"`{self.name}`" if self.name_is_identifier else self.name
        if self.note:
            label = f"{label} {self.note}"
        if self.members:
            label = f"{label}: {', '.join(f'`{member}`' for member in self.members)}"
        return label


def _uses(*items: str | tuple[str, str]) -> tuple[ConsumerUse, ...]:
    return tuple(
        ConsumerUse(item) if isinstance(item, str) else ConsumerUse(*item) for item in items
    )


CONSUMERS: tuple[Consumer, ...] = (
    Consumer(
        slug="rosie",
        name="@Rosie",
        note="(Grok Bot)",
        uses=_uses(
            "notify_joe",
            ("notify_jess", "only after Joe confirmation"),
            ("list_notifications", "short-lived reply checks"),
        ),
        groups=("rosie",),
        tags=("rosie-date-night-*", "rosie-reminder-*"),
        action_prefixes=("ROSIE_",),
        recipe_title="Ask Joe first, then tell Jess",
        recipe='''import homelab

homelab.notify_joe(
    "Date night Friday?",
    "Rosie found a 7pm table. Confirm before I tell Jess.",
    tag="rosie-date-night-2026-10-30",
    group="rosie",
    buttons=[{"title": "Confirm", "action": "ROSIE_CONFIRM::date-night-2026-10-30"}],
)

# Short-lived reply check: the broker records Joe's button press in the ledger.
latest = homelab.list_notifications(tag="rosie-date-night-2026-10-30", limit=1)
actions = latest["notifications"][0]["actions"]
if any(item["action"].startswith("ROSIE_CONFIRM::") for item in actions):
    homelab.notify_jess(
        "Date night Friday",
        "Joe confirmed 7pm. Rosie booked the table.",
        tag="rosie-date-night-2026-10-30",
        group="rosie",
    )
''',
    ),
    Consumer(
        slug="homelab-sre-agent",
        name="homelab-sre-agent",
        uses=_uses("notify_joe", "record_notification_action", "list_notifications"),
        groups=("homelab-sre",),
        tags=("sre-incident-<id>",),
        action_prefixes=("SRE_APPROVE", "SRE_IGNORE"),
        recipe_title="Ask Joe to approve a remediation",
        recipe='''import homelab

homelab.notify_joe(
    "Disk 92% on nas-01",
    "Prune old snapshots?",
    tag="sre-incident-417",
    group="homelab-sre",
    buttons=[
        {"title": "Approve", "action": "SRE_APPROVE::417"},
        {"title": "Ignore", "action": "SRE_IGNORE::417"},
    ],
)

# The agent's own Home Assistant listener received the button press.
homelab.record_notification_action(
    "SRE_APPROVE::417",
    tag="sre-incident-417",
    group="homelab-sre",
)
history = homelab.list_notifications(group="homelab-sre", limit=20)
''',
    ),
    Consumer(
        slug="household-workflows",
        name="Household/runtime workflows",
        name_is_identifier=False,
        members=("cat-food-monitor", "laundry-monitor", "plant-monitor", "bedtime", "mealie-planner"),
        uses=_uses("notify_joe", "notify_jess", "record_workflow_report"),
        helpers=(ConsumerHelper("workflow_report_button", '"<workflow-slug>"'),),
        action_prefixes=(WORKFLOW_REPORT_ACTION_PREFIX,),
        recipe_title="Add a Report button and record the report",
        recipe='''import homelab

homelab.notify_joe(
    "Cat food monitor",
    "Morning food check failed.",
    tag="cat-food-monitor-morning",
    group="cat-food-monitor",
    buttons=[homelab.workflow_report_button("cat-food-monitor")],
)

# The workflow's listener received WORKFLOW_REPORT::cat-food-monitor.
homelab.record_workflow_report(
    "cat-food-monitor",
    "Bowl sensor read empty after the 7am feed.",
    source="mobile-action",
)
''',
    ),
    Consumer(
        slug="runtime-checkers",
        name="smoke-signal",
        note="& runtime checkers",
        uses=_uses("get_service_catalog", "get_smoke_signal_targets"),
        recipe_title="Load the runtime view of the homelab",
        recipe='''import homelab

services = homelab.get_service_catalog()["services"]
targets = homelab.get_smoke_signal_targets()["targets"]
''',
    ),
)


def consumer_matrix_markdown() -> str:
    """Return the caller matrix as the Markdown table in `docs/architecture.md`."""

    def cell(values: tuple[str, ...]) -> str:
        return ", ".join(f"`{value}`" for value in values) or "—"

    lines = [
        "| Caller | Actions | Group | Tags | Action prefixes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for consumer in CONSUMERS:
        actions = [
            f"`{use.operation_id}`" + (f" ({use.condition})" if use.condition else "")
            for use in consumer.uses
        ]
        actions += [f"`{helper.call()}`" for helper in consumer.helpers]
        lines.append(
            f"| {consumer.caller_label()} | {', '.join(actions)} | {cell(consumer.groups)} "
            f"| {cell(consumer.tags)} | {cell(consumer.action_prefixes)} |"
        )
    return "\n".join(lines)
