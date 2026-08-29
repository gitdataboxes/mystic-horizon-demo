"""Prompt assembly: Mustache renderer, builder, and variable computation."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from mystic.config import (
    get_agent_config,
    get_error_message,
    get_home,
    get_providers_config,
    identity_exists,
    logger,
    read_identity_raw,
    read_soul,
)
from mystic.db import (
    get_active_facts_by_person,
    get_all_pending_actions,
    get_failed_actions,
    get_pending_actions_by_person,
    get_person_by_id,
    get_recent_day_summaries,
    get_unfinalized_interactions,
    is_day_summary_finalized,
)
from mystic.interactions import describe_call, describe_interaction, format_interaction_brief
from mystic.types import Audience, Call, CallState, Channel, Direction, InteractionModality, Person, PromptVariables

# ── renderer ──────────────────────────────────────────────────────────────────

_TRUTHY_SECTION_RE = re.compile(r"\{\{#(\w+)\}\}([\s\S]*?)\{\{/\1\}\}")
_FALSEY_SECTION_RE = re.compile(r"\{\{\^(\w+)\}\}([\s\S]*?)\{\{/\1\}\}")
_VARIABLE_RE = re.compile(r"\{\{(\w+)\}\}")


def render(template: str, variables: Mapping[str, object]) -> str:
    result = _TRUTHY_SECTION_RE.sub(
        lambda match: match.group(2) if variables.get(match.group(1)) else "",
        template,
    )
    result = _FALSEY_SECTION_RE.sub(
        lambda match: "" if variables.get(match.group(1)) else match.group(2),
        result,
    )
    return _VARIABLE_RE.sub(
        lambda match: str(variables.get(match.group(1), "")),
        result,
    )


# ── builder ───────────────────────────────────────────────────────────────────

SEPARATOR = "\n\n---\n\n"


def build_prompt(
    audience: Audience,
    variables: PromptVariables | dict[str, object],
) -> str:

    template_vars = _to_template_vars(variables)
    prompts_dir = get_home() / "prompts"

    identity = _read_identity()
    soul = _read_soul()
    shared_segments = _read_prompt_files(prompts_dir / "shared")
    audience_segments = _read_prompt_files(prompts_dir / audience)

    parts = [
        _render_part(identity, template_vars),
        _render_part(soul, template_vars),
        *(_render_part(segment, template_vars) for segment in shared_segments),
        *(_render_part(segment, template_vars) for segment in audience_segments),
    ]
    return SEPARATOR.join(part for part in parts if part)


def _read_prompt_files(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [
        path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.suffix == ".md"
    ]


def _read_identity() -> str:
    try:
        return read_identity_raw() if identity_exists() else ""
    except OSError:
        return ""


def _read_soul() -> str:
    try:
        return read_soul()
    except OSError:
        return ""


def _render_part(template: str, variables: dict[str, object]) -> str:
    if not template:
        return ""
    return render(template, variables).strip()


def _to_template_vars(variables: PromptVariables | dict[str, object]) -> dict[str, object]:
    base = _coerce_mapping(variables)
    template_vars = dict(base)
    for key, value in list(base.items()):
        camel_key = _snake_to_camel(key)
        template_vars.setdefault(camel_key, value)
    return template_vars


def _coerce_mapping(variables: PromptVariables | dict[str, object]) -> dict[str, object]:
    if is_dataclass(variables):
        raw = asdict(variables)
        return cast(dict[str, object], raw)
    return dict(variables)


def _snake_to_camel(name: str) -> str:
    parts = name.split("_")
    if not parts:
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


# ── variables ─────────────────────────────────────────────────────────────────


def compute_variables(
    db: sqlite3.Connection,
    person: Person,
    audience: Audience,
    direction: Direction,
    active_calls: Mapping[str, CallState] | Iterable[CallState] | None,
    tunnel_url: str | None = None,
    *,
    channel: Channel,
    modality: InteractionModality,
    now: datetime | None = None,
) -> PromptVariables:
    agent = get_agent_config()
    providers = get_providers_config()
    tz = ZoneInfo(agent.hours.timezone)
    current = _coerce_now(now, tz)
    current_person = get_person_by_id(db, person.id) or person

    facts = get_active_facts_by_person(db, current_person.id)
    facts_text = "\n".join(
        f"- {fact.content} ({fact.type}, confidence: {fact.confidence})" for fact in facts
    )

    today_date = current.strftime("%Y-%m-%d")
    verbatim_recent_context = _build_verbatim_recent_context(
        get_unfinalized_interactions(db, current_person.id, today_date),
        tz,
    )
    recent_summary_lines: list[str] = []
    for summary in get_recent_day_summaries(db, current_person.id, 30):
        if not is_day_summary_finalized(summary) or summary.date == today_date:
            continue
        recent_summary_lines.append(f"- {summary.date}: {summary.summary}")
        if len(recent_summary_lines) >= 7:
            break
    recent_days_summary = "\n".join(recent_summary_lines)

    pending_actions = (
        get_all_pending_actions(db)
        if audience == "owner"
        else get_pending_actions_by_person(db, current_person.id)
    )
    pending_actions_text = "\n".join(
        _format_pending_action(action, tz) for action in pending_actions
    )

    failed_actions_text = ""
    if audience == "owner":
        failed_actions_text = "\n".join(
            f"- {action.intent} — {action.attempts}/{action.max_attempts} attempts, "
            f"result: {action.result or 'unknown'}"
            for action in get_failed_actions(db)
        )

    active_calls_text = ""
    if audience == "owner":
        active_calls_text = "\n".join(
            _format_active_call(call, int(current.timestamp() * 1000))
            for call in _iter_active_calls(active_calls)
        )

    caller_summary = _build_caller_summary(current_person.summary, facts_text)
    current_schedule = ""
    upcoming_schedule = ""
    try:
        from mystic.calendar import format_current_schedule, format_upcoming_schedule

        reference_ms = int(current.timestamp() * 1000)
        current_schedule = format_current_schedule(
            db,
            audience,
            tz,
            current_person.id,
            reference_ms,
        )
        upcoming_schedule = format_upcoming_schedule(
            db,
            audience,
            current_person.id,
            tz,
            7 * 24 * 60 * 60_000,
            reference_ms,
        )
    except Exception as exc:
        logger.warn("prompts.calendar.unavailable", error=get_error_message(exc))

    phone_setup_hint = ""
    if audience == "owner" and not providers.twilio:
        phone_setup_hint = (
            "Phone calls are not configured yet. If the owner asks about phone setup, help directly "
            "with the setup skills instead of only sending them to Settings: use read-setup or "
            "check-tailscale to find the next blocker, write-twilio-credentials to save credentials "
            "the owner provides, read-twilio-numbers to list numbers they already own, "
            "write-twilio-number to attach an owned number or buy a selected available number, "
            "and activate-tunnel after Twilio and Tailscale are ready. Never ask public callers "
            "for credentials, and do not repeat secret tokens back to the owner."
        )

    current_interaction = describe_interaction(
        direction=direction,
        channel=channel,
        modality=modality,
    )

    return PromptVariables(
        current_time=current.strftime("%I:%M %p"),
        day_of_week=current.strftime("%A"),
        full_date=f"{current.strftime('%B')} {current.day}, {current.year}",
        timezone=agent.hours.timezone,
        business_hours=_format_business_hours(
            start=agent.hours.start,
            end=agent.hours.end,
            timezone=agent.hours.timezone,
            days=agent.hours.days,
        ),
        agent_name=agent.agent.name,
        caller_name=current_person.name or "Unknown caller",
        caller_phone=current_person.phone,
        caller_summary=caller_summary,
        channel_label=current_interaction.channel_label,
        modality=current_interaction.modality_label,
        direction=current_interaction.direction_label,
        active_calls=active_calls_text,
        recent_days_summary=recent_days_summary,
        verbatim_recent_context=verbatim_recent_context,
        urgent_items="\n".join(
            f"- [URGENT] {action.intent}" for action in pending_actions if action.urgency == "high"
        ),
        pending_actions=pending_actions_text,
        failed_actions=failed_actions_text,
        current_schedule=current_schedule,
        upcoming_schedule=upcoming_schedule,
        tunnel_url=tunnel_url or "",
        webhook_secret="",
        phone_setup_hint=phone_setup_hint,
    )


def _coerce_now(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC).astimezone(tz)
    return now.astimezone(tz)


def _format_business_hours(*, start: int, end: int, timezone: str, days: list[str]) -> str:
    day_text = ", ".join(day.capitalize() for day in days)
    return f"{start}:00-{end}:00 {timezone} ({day_text})"


def _build_caller_summary(summary: str | None, facts_text: str) -> str:
    parts = [part for part in (summary or "", facts_text) if part]
    if len(parts) == 2:
        return f"{parts[0]}\n\nKnown facts:\n{parts[1]}"
    return parts[0] if parts else ""


def _build_verbatim_recent_context(interactions: list[Call], tz: ZoneInfo) -> str:
    parts: list[str] = []
    for interaction in interactions:
        transcript = (interaction.transcript or "").strip()
        if not transcript:
            continue
        started = datetime.fromtimestamp(interaction.started_at / 1000, tz)
        descriptor = describe_call(interaction)
        parts.append(
            f"[{started.strftime('%Y-%m-%d %I:%M %p')} {descriptor.label}]\n"
            f"{transcript}"
        )
    return "\n\n".join(parts)


def _format_pending_action(action: object, tz: ZoneInfo) -> str:
    due_at = getattr(action, "due_at")
    due = "ASAP" if due_at is None else _format_due_datetime(int(due_at), tz)
    person_id = getattr(action, "person_id")
    person_info = f" (person: {person_id})" if person_id else ""
    return (
        f"- {getattr(action, 'intent')}{person_info} — due: {due}, attempts: "
        f"{getattr(action, 'attempts')}/{getattr(action, 'max_attempts')}"
    )


def _format_due_datetime(timestamp_ms: int, tz: ZoneInfo) -> str:
    due = datetime.fromtimestamp(timestamp_ms / 1000, tz)
    hour_24 = due.hour
    hour_12 = hour_24 % 12 or 12
    am_pm = "AM" if hour_24 < 12 else "PM"
    return (
        f"{due.month}/{due.day}/{due.year}, "
        f"{hour_12}:{due.minute:02d}:{due.second:02d} {am_pm}"
    )


def _iter_active_calls(
    active_calls: Mapping[str, CallState] | Iterable[CallState] | None,
) -> list[CallState]:
    if active_calls is None:
        return []
    if isinstance(active_calls, Mapping):
        return list(active_calls.values())
    return list(active_calls)


def _format_active_call(call: CallState, now_ms: int) -> str:
    duration_seconds = max((now_ms - call.started_at) // 1000, 0)
    minutes = duration_seconds // 60
    seconds = duration_seconds % 60
    descriptor = describe_interaction(
        direction=call.direction,
        channel=call.channel,
        modality=call.modality,
    )
    return (
        f"- {call.person_name or 'Unknown'} "
        f"({format_interaction_brief(descriptor)}, {minutes}m {seconds}s)"
    )
