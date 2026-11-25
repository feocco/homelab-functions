"""Provider credit state.

The classifier and the reducer are pure. Callers parse HTTP or an upstream
error into a `CreditReport`, then apply `reduce_credit_state`. Nothing in
this module buys credits or calls a billing endpoint. The billing URL is
only the label on the notification button.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
import re
from typing import Any


BODY_LIMIT = 500
CREDIT_GROUP = "api-credits"
REMINDER_AFTER = timedelta(hours=24)
PROVIDER_IDS = ("xai", "x", "openai", "cursor")
SERVICE_ORDER = ("pirate-radio", "pebble", "home-assistant")
SERVICE_PHRASES = {
    "pirate-radio": "Pirate Radio conversions",
    "pebble": "Pebble voice notes",
    "home-assistant": "Home Assistant Grok voice",
}

_XAI_BODY = re.compile(
    r"used all available credits|monthly spending limit|doesn't have any credits",
    re.IGNORECASE,
)
_OPENAI_BODY = re.compile(
    r"credit_balance_exhausted|insufficient_quota|"
    r"(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+_)?spend_limit_exceeded(?![A-Za-z0-9_])"
)
_X_BODY = re.compile(
    r"spend cap|spending cap|spending limit|\bcredits?\b",
    re.IGNORECASE,
)
_CURSOR_BODY = re.compile(r"usage_limit")


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    display_name: str
    low_floor_usd: float
    billing_url: str
    closing: str


PROVIDERS = (
    ProviderSpec(
        provider="xai",
        display_name="xAI",
        low_floor_usd=5.0,
        billing_url="https://console.x.ai",
        closing="Add credits in the xAI console; I'll tell you when it's working again.",
    ),
    ProviderSpec(
        provider="x",
        display_name="X",
        low_floor_usd=2.0,
        billing_url="https://console.x.com",
        closing="Add credits in the X console; I'll tell you when it's working again.",
    ),
    ProviderSpec(
        provider="openai",
        display_name="OpenAI",
        low_floor_usd=5.0,
        billing_url="https://platform.openai.com/settings/organization/billing/overview",
        closing="Add credits in the OpenAI console; I'll tell you when it's working again.",
    ),
    ProviderSpec(
        provider="cursor",
        display_name="Cursor",
        low_floor_usd=5.0,
        billing_url="https://cursor.com/dashboard/billing",
        closing="Add credits in the Cursor dashboard; I'll tell you when it's working again.",
    ),
)
PROVIDER_BY_ID = {spec.provider: spec for spec in PROVIDERS}


@dataclass(frozen=True)
class _Rule:
    provider: str
    statuses: frozenset[int] | None
    body: re.Pattern[str] | None


_RULES = (
    _Rule("xai", frozenset({403, 429}), _XAI_BODY),
    _Rule("openai", frozenset({429}), _OPENAI_BODY),
    _Rule("x", frozenset({402}), None),
    _Rule("x", frozenset({403}), _X_BODY),
    _Rule("cursor", None, _CURSOR_BODY),
)


class CreditInputError(ValueError):
    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class CreditReport:
    provider: str
    service: str
    kind: str
    status: int | None
    body: str | None
    balance_usd: float | None
    daily_burn_usd: float | None
    observed_at: datetime


@dataclass(frozen=True)
class CreditSnapshot:
    provider: str
    state: str
    balance_usd: float | None
    daily_burn_usd: float | None
    services: tuple[str, ...]
    error_count: int
    last_transition_at: str | None
    last_push_at: str | None
    last_push_kind: str | None
    updated_at: str | None


@dataclass(frozen=True)
class CreditPush:
    title: str
    message: str
    tag: str
    group: str
    buttons: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class CreditReduction:
    snapshot: CreditSnapshot
    push: CreditPush | None
    push_kind: str | None
    classified: bool
    previous_state: str
    persist: bool


def empty_snapshot(provider: str) -> CreditSnapshot:
    return CreditSnapshot(
        provider=provider,
        state="ok",
        balance_usd=None,
        daily_burn_usd=None,
        services=(),
        error_count=0,
        last_transition_at=None,
        last_push_at=None,
        last_push_kind=None,
        updated_at=None,
    )


def is_credit_error(provider: str, status: int | None, body: str | None) -> bool:
    """Return whether this upstream error is a credit outage for that provider."""

    text = (body or "")[:BODY_LIMIT]
    for rule in _RULES:
        if rule.provider != provider:
            continue
        if rule.statuses is not None and status not in rule.statuses:
            continue
        if rule.body is None:
            return True
        if rule.body.search(text):
            return True
    return False


def low_line_usd(provider: str, daily_burn_usd: float | None) -> float:
    floor = PROVIDER_BY_ID[provider].low_floor_usd
    if daily_burn_usd is None or daily_burn_usd <= 0:
        return floor
    return max(floor, 3 * daily_burn_usd)


def parse_credit_report(payload: Any, *, now: datetime | None = None) -> CreditReport:
    """Parse one report body. The body is truncated to 500 characters."""

    if not isinstance(payload, dict):
        raise CreditInputError("Request body must be a JSON object")
    allowed = {
        "provider",
        "service",
        "kind",
        "status",
        "body",
        "balance_usd",
        "daily_burn_usd",
        "observed_at",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CreditInputError(
            f"Unknown fields: {', '.join(unknown)}",
            field=unknown[0],
        )
    provider = _required_enum(payload, "provider", PROVIDER_IDS)
    service = _service(payload)
    kind = _required_enum(payload, "kind", ("error", "success", "balance"))
    status = _optional_status(payload)
    body = _optional_body(payload)
    balance = _optional_number(payload, "balance_usd")
    burn = _optional_number(payload, "daily_burn_usd", minimum=0)
    observed_at = _observed_at(payload, now=now or datetime.now(timezone.utc))
    return CreditReport(
        provider=provider,
        service=service,
        kind=kind,
        status=status,
        body=body,
        balance_usd=balance,
        daily_burn_usd=burn,
        observed_at=observed_at,
    )


def reduce_credit_state(previous: CreditSnapshot, report: CreditReport) -> CreditReduction:
    """Apply one report to one provider snapshot."""

    credit_error = report.kind == "error" and is_credit_error(
        report.provider,
        report.status,
        report.body,
    )
    classified = report.kind != "error" or credit_error
    balance = report.balance_usd if report.balance_usd is not None else previous.balance_usd
    burn = report.daily_burn_usd if report.daily_burn_usd is not None else previous.daily_burn_usd
    line = low_line_usd(report.provider, burn)
    recovery = line + 1.0
    if classified:
        new_state = _next_state(
            previous.state,
            report.kind,
            credit_error,
            report.balance_usd,
            line,
            recovery,
        )
        services = previous.services
        if _tracks_service(report, credit_error, line):
            services = _with_service(services, report.service)
        if credit_error and new_state == "out" and previous.state != "out":
            error_count = 1
        elif credit_error and previous.state == "out":
            error_count = previous.error_count + 1
        else:
            error_count = previous.error_count
    else:
        new_state = previous.state
        services = previous.services
        error_count = previous.error_count

    push_services = services
    push_count = error_count
    if new_state == "ok" and previous.state in {"low", "out"}:
        services = ()
        error_count = 0
    when = format_timestamp(report.observed_at)
    transition_at = when if new_state != previous.state else previous.last_transition_at
    changed = (
        new_state != previous.state
        or services != previous.services
        or error_count != previous.error_count
        or balance != previous.balance_usd
        or burn != previous.daily_burn_usd
        or transition_at != previous.last_transition_at
    )
    snapshot = (
        CreditSnapshot(
            provider=report.provider,
            state=new_state,
            balance_usd=balance,
            daily_burn_usd=burn,
            services=services,
            error_count=error_count,
            last_transition_at=transition_at,
            last_push_at=previous.last_push_at,
            last_push_kind=previous.last_push_kind,
            updated_at=when,
        )
        if changed
        else previous
    )
    push_kind = _push_kind(previous, new_state, report.observed_at)
    push = None
    if push_kind is not None:
        push = render_push(
            push_kind,
            replace(
                snapshot,
                state=new_state,
                services=push_services,
                error_count=push_count,
                balance_usd=balance,
                daily_burn_usd=burn,
            ),
        )
    return CreditReduction(
        snapshot=snapshot,
        push=push,
        push_kind=push_kind,
        classified=classified,
        previous_state=previous.state,
        persist=changed,
    )


def render_push(kind: str, snapshot: CreditSnapshot) -> CreditPush:
    spec = PROVIDER_BY_ID[snapshot.provider]
    phrases = [SERVICE_PHRASES.get(service, service) for service in snapshot.services]
    if kind == "out":
        title = f"{spec.display_name} is out of credits"
        message = _out_message(spec, snapshot, phrases, reminder=False)
    elif kind == "reminder":
        title = f"{spec.display_name} is still out of credits"
        message = _out_message(spec, snapshot, phrases, reminder=True)
    elif kind == "low":
        title = f"{spec.display_name} credits running low"
        message = _low_message(snapshot, phrases)
    elif kind == "recovered":
        title = f"{spec.display_name} credits are back"
        message = _recovered_message(snapshot, phrases)
    else:
        raise ValueError(f"Unknown push kind: {kind}")
    return CreditPush(
        title=title,
        message=message,
        tag=credit_tag(snapshot.provider),
        group=CREDIT_GROUP,
        buttons=(billing_button(snapshot.provider),),
    )


def credit_tag(provider: str) -> str:
    return f"{CREDIT_GROUP}-{provider}"


def billing_button(provider: str) -> dict[str, str]:
    return {
        "title": "Open billing",
        "action": "URI",
        "uri": PROVIDER_BY_ID[provider].billing_url,
    }


def credit_status_document(snapshots: list[CreditSnapshot]) -> dict[str, Any]:
    by_id = {item.provider: item for item in snapshots}
    providers = []
    for provider in PROVIDER_IDS:
        snapshot = by_id.get(provider) or empty_snapshot(provider)
        spec = PROVIDER_BY_ID[provider]
        providers.append(
            {
                "provider": provider,
                "display_name": spec.display_name,
                "state": snapshot.state,
                "balance_usd": snapshot.balance_usd,
                "daily_burn_usd": snapshot.daily_burn_usd,
                "low_line_usd": low_line_usd(provider, snapshot.daily_burn_usd),
                "services": list(snapshot.services),
                "error_count": snapshot.error_count,
                "last_transition_at": snapshot.last_transition_at,
                "last_push_at": snapshot.last_push_at,
                "last_push_kind": snapshot.last_push_kind,
                "updated_at": snapshot.updated_at,
            }
        )
    return {"providers": providers}


def reduction_payload(
    report: CreditReport,
    reduction: CreditReduction,
    *,
    pushed: bool,
    notification_id: int | None,
) -> dict[str, Any]:
    return {
        "provider": report.provider,
        "kind": report.kind,
        "classified": reduction.classified,
        "previous_state": reduction.previous_state,
        "state": reduction.snapshot.state,
        "pushed": pushed,
        "push_kind": reduction.push_kind,
        "notification_id": notification_id,
        "error_count": reduction.snapshot.error_count,
        "services": list(reduction.snapshot.services),
    }


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def snapshot_from_mapping(row: Mapping[str, Any]) -> CreditSnapshot:
    services = json.loads(row["services_json"])
    return CreditSnapshot(
        provider=str(row["provider"]),
        state=str(row["state"]),
        balance_usd=None if row["balance_usd"] is None else float(row["balance_usd"]),
        daily_burn_usd=None if row["daily_burn_usd"] is None else float(row["daily_burn_usd"]),
        services=tuple(services),
        error_count=int(row["error_count"]),
        last_transition_at=row["last_transition_at"],
        last_push_at=row["last_push_at"],
        last_push_kind=row["last_push_kind"],
        updated_at=row["updated_at"],
    )


def snapshot_parameters(snapshot: CreditSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.provider,
        snapshot.state,
        snapshot.balance_usd,
        snapshot.daily_burn_usd,
        json.dumps(list(snapshot.services), separators=(",", ":")),
        snapshot.error_count,
        snapshot.last_transition_at,
        snapshot.last_push_at,
        snapshot.last_push_kind,
        snapshot.updated_at,
    )


def money(value: float) -> str:
    return f"${value:.2f}"


def _tracks_service(report: CreditReport, credit_error: bool, line: float) -> bool:
    if credit_error:
        return True
    if report.kind != "balance" or report.balance_usd is None:
        return False
    return report.balance_usd <= 0 or report.balance_usd < line


def _next_state(
    previous: str,
    kind: str,
    credit_error: bool,
    reported_balance: float | None,
    line: float,
    recovery: float,
) -> str:
    if credit_error:
        return "out"
    if kind == "success":
        if previous == "out":
            return "ok"
        if previous == "low" and reported_balance is not None and reported_balance >= recovery:
            return "ok"
        return previous
    if reported_balance is None:
        return previous
    if reported_balance <= 0:
        return "out"
    if previous == "out":
        return "ok" if reported_balance >= recovery else "out"
    if previous == "low":
        return "ok" if reported_balance >= recovery else "low"
    if reported_balance < line:
        return "low"
    return "ok"


def _push_kind(previous: CreditSnapshot, new_state: str, observed_at: datetime) -> str | None:
    if new_state != previous.state:
        if new_state == "out":
            return "out"
        if new_state == "low":
            return "low"
        if new_state == "ok":
            return "recovered"
        return None
    if new_state == "out":
        if previous.last_push_at is None or previous.last_push_kind not in {"out", "reminder"}:
            return "out"
        last_push = datetime.fromisoformat(previous.last_push_at)
        if observed_at - last_push >= REMINDER_AFTER:
            return "reminder"
        return None
    if new_state == "low":
        if previous.last_push_kind != "low":
            return "low"
        return None
    if previous.last_push_kind in {"out", "low", "reminder"}:
        return "recovered"
    return None


def _out_message(
    spec: ProviderSpec,
    snapshot: CreditSnapshot,
    phrases: list[str],
    *,
    reminder: bool,
) -> str:
    parts: list[str] = []
    if snapshot.balance_usd is not None:
        parts.append(f"Balance {money(snapshot.balance_usd)}. ")
    parts.append(_out_impact(phrases))
    if reminder and snapshot.error_count > 1:
        parts.append(f"{snapshot.error_count} credit errors since it ran out. ")
    parts.append(spec.closing)
    return "".join(parts)


def _out_impact(phrases: list[str]) -> str:
    if not phrases:
        return ""
    if len(phrases) == 1:
        return f"{phrases[0]} are paused. "
    rest = " and ".join(phrases[1:])
    return f"{phrases[0]} are paused, and {rest} will fail. "


def _low_message(snapshot: CreditSnapshot, phrases: list[str]) -> str:
    balance = 0.0 if snapshot.balance_usd is None else snapshot.balance_usd
    joined = _join_phrases(phrases)
    if snapshot.daily_burn_usd is not None and snapshot.daily_burn_usd > 0 and snapshot.balance_usd is not None:
        return (
            f"{money(balance)} left, {_pace(balance, snapshot.daily_burn_usd)} "
            f"at your recent use ({money(snapshot.daily_burn_usd)}/day). "
            f"At $0, {joined} pause."
        )
    return f"{money(balance)} left. At $0, {joined} pause."


def _recovered_message(snapshot: CreditSnapshot, phrases: list[str]) -> str:
    parts: list[str] = []
    if snapshot.balance_usd is not None:
        parts.append(f"Balance {money(snapshot.balance_usd)}.")
    if phrases:
        parts.append(f"{_join_phrases(phrases)} resumed.")
    return " ".join(parts)


def _pace(balance: float, burn: float) -> str:
    days = round(balance / burn)
    if days <= 0:
        return "under 1 day"
    unit = "day" if days == 1 else "days"
    return f"about {days} {unit}"


def _join_phrases(phrases: list[str]) -> str:
    if not phrases:
        return ""
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"


def _with_service(services: tuple[str, ...], service: str) -> tuple[str, ...]:
    return _order_services((*services, service))


def _order_services(services: tuple[str, ...]) -> tuple[str, ...]:
    present = set(services)
    ordered = [item for item in SERVICE_ORDER if item in present]
    ordered.extend(sorted(item for item in present if item not in SERVICE_ORDER))
    return tuple(ordered)


def _required_enum(payload: Mapping[str, Any], field: str, allowed: tuple[str, ...]) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(allowed)
        raise CreditInputError(f"{field} must be one of: {choices}", field=field)
    return value


def _service(payload: Mapping[str, Any]) -> str:
    value = payload.get("service")
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise CreditInputError("service must be 1 to 80 characters on one line", field="service")
    service = value.strip()
    if not service or len(service) > 80:
        raise CreditInputError("service must be 1 to 80 characters on one line", field="service")
    return service


def _optional_status(payload: Mapping[str, Any]) -> int | None:
    if "status" not in payload or payload["status"] is None:
        return None
    value = payload["status"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 599:
        raise CreditInputError("status must be an integer from 100 to 599", field="status")
    return value


def _optional_body(payload: Mapping[str, Any]) -> str | None:
    if "body" not in payload or payload["body"] is None:
        return None
    value = payload["body"]
    if not isinstance(value, str):
        raise CreditInputError("body must be a string", field="body")
    return value[:BODY_LIMIT]


def _optional_number(
    payload: Mapping[str, Any],
    field: str,
    *,
    minimum: float | None = None,
) -> float | None:
    if field not in payload or payload[field] is None:
        return None
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CreditInputError(f"{field} must be a number", field=field)
    number = float(value)
    if not math.isfinite(number):
        raise CreditInputError(f"{field} must be finite", field=field)
    if minimum is not None and number < minimum:
        raise CreditInputError(f"{field} must be at least {minimum}", field=field)
    return number


def _observed_at(payload: Mapping[str, Any], *, now: datetime) -> datetime:
    if "observed_at" not in payload or payload["observed_at"] is None:
        return now.astimezone(timezone.utc)
    value = payload["observed_at"]
    if not isinstance(value, str) or not value.strip():
        raise CreditInputError("observed_at must be an ISO-8601 timestamp", field="observed_at")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CreditInputError("observed_at must be an ISO-8601 timestamp", field="observed_at") from exc
    if moment.tzinfo is None:
        raise CreditInputError("observed_at must include a timezone", field="observed_at")
    return moment.astimezone(timezone.utc)
