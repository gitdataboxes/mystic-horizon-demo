"""Flat types module — all shared runtime, database, and skill types."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

# ── Core types ───────────────────────────────────────────────────────────────

Audience = Literal["owner", "public"]
Modality = Literal["voice", "text"]
Direction = Literal["inbound", "outbound"]
InteractionModality = Literal["voice", "text", "mixed"]
Channel = Literal["dashboard", "phone", "sms", "cli"]

FactType = Literal["identity", "preference", "relationship", "context"]
FactSource = Literal["mid-call", "caller", "owner", "post-call", "agent", "cli"]

ActionUrgency = Literal["normal", "high"]
ActionSource = FactSource
ActionStatus = Literal["pending", "in_progress", "completed", "failed", "cancelled"]

SchedulerDecision = Literal["act", "wait", "cancel", "escalate", "notify"]
SatisfactionStatus = Literal["satisfied", "partial", "not_satisfied"]
LogLevel = Literal["debug", "info", "warn", "error"]


def _empty_object_dict() -> dict[str, object]:
    return {}


@dataclass(slots=True)
class Person:
    id: str
    phone: str
    name: str | None
    summary: str | None
    first_seen: int
    last_seen: int


@dataclass(slots=True)
class Call:
    id: str
    external_id: str | None
    person_id: str
    direction: Direction
    channel: Channel
    modality: InteractionModality
    audience: Audience
    action_id: str | None
    transcript: str | None
    summary: str | None
    facts_extracted: int
    commitments_extracted: int
    extraction_retries: int
    extraction_error: str | None
    last_extraction_attempt_at: int | None
    started_at: int
    answered_at: int | None
    ended_at: int | None
    duration: int | None


@dataclass(slots=True)
class GameScore:
    id: str
    name: str
    score: int
    wave: int
    created_at: int


@dataclass(slots=True)
class DaySummary:
    id: str
    person_id: str
    date: str
    summary: str | None
    facts_extracted: int
    commitments_extracted: int
    extraction_error: str | None
    created_at: int
    updated_at: int


@dataclass(slots=True)
class TranscriptChunk:
    id: str
    call_id: str
    person_id: str
    content: str
    chunk_index: int
    embedding: bytes | None
    created_at: int


@dataclass(slots=True)
class Fact:
    id: str
    person_id: str
    call_id: str | None
    source_text: str | None
    type: FactType
    content: str
    confidence: float
    source: FactSource
    embedding: bytes | None
    verified_at: int
    created_at: int
    superseded_at: int | None


@dataclass(slots=True)
class Action:
    id: str
    person_id: str | None
    call_id: str | None
    source_text: str | None
    intent: str
    context: str | None
    due_at: int | None
    urgency: ActionUrgency
    source: ActionSource
    status: ActionStatus
    attempts: int
    max_attempts: int
    last_attempted_at: int | None
    result: str | None
    created_at: int
    updated_at: int
    start_at: int | None = None
    end_at: int | None = None
    hub_event_id: str | None = None
    hub_sync_status: str | None = None
    hub_sync_attempts: int = 0


@dataclass(slots=True)
class ExternalEvent:
    id: str
    ics_uid: str
    ics_url: str
    title: str
    start_at: int
    end_at: int
    all_day: bool
    created_at: int
    updated_at: int
    description: str | None = None
    location: str | None = None


@dataclass(slots=True)
class FaqChunk:
    id: str
    file_path: str
    heading: str | None
    content: str
    embedding: bytes | None
    updated_at: int


@dataclass(slots=True)
class Identity:
    name: str
    creature: str
    vibe: str
    emoji: str


@dataclass(slots=True)
class JournalEntry:
    timestamp: int
    file_type: str
    trigger: str
    note: str
    content: str


@dataclass(slots=True)
class CallState:
    call_id: str
    person_id: str
    person_name: str | None
    audience: Audience
    direction: Direction
    channel: Channel
    modality: InteractionModality
    started_at: int
    answered_at: int | None = None


@dataclass(slots=True)
class SkillContext:
    audience: Audience
    direction: Direction
    channel: Channel
    modality: InteractionModality
    call_id: str
    person_id: str
    source: FactSource


@dataclass(slots=True)
class PromptVariables:
    current_time: str = ""
    day_of_week: str = ""
    full_date: str = ""
    timezone: str = ""
    business_hours: str = ""
    agent_name: str = ""
    caller_name: str = ""
    caller_phone: str = ""
    caller_summary: str = ""
    channel_label: str = ""
    modality: str = ""
    direction: str = ""
    active_calls: str = ""
    recent_days_summary: str = ""
    verbatim_recent_context: str = ""
    urgent_items: str = ""
    pending_actions: str = ""
    failed_actions: str = ""
    current_schedule: str = ""
    upcoming_schedule: str = ""
    tunnel_url: str = ""
    webhook_secret: str = ""
    phone_setup_hint: str = ""


@dataclass(slots=True)
class SearchResult:
    id: str
    content: str
    score: float
    metadata: dict[str, object] = field(default_factory=_empty_object_dict)


@dataclass(slots=True)
class SchedulerJudgment:
    id: str
    decision: SchedulerDecision
    reason: str
    wait_until: str | None = None


@dataclass(slots=True)
class SatisfactionJudgment:
    id: str
    status: SatisfactionStatus
    confidence: float
    reason: str


@dataclass(slots=True)
class ExtractedFact:
    content: str
    type: FactType
    confidence: float
    source_text: str


@dataclass(slots=True)
class ExtractedCommitment:
    content: str
    intent: str
    due: str | None
    urgency: ActionUrgency


@dataclass(slots=True)
class LogEntry:
    ts: str
    level: LogLevel
    event: str
    data: dict[str, object] = field(default_factory=_empty_object_dict)


# ── Source derivation ────────────────────────────────────────────────────────

from typing import Mapping


def derive_source(
    audience: Audience,
    skill_name: str,
    context: Mapping[str, bool] | None = None,
) -> FactSource:
    flags = context or {}
    if flags.get("isExtraction"):
        return "post-call"
    if flags.get("isScheduler"):
        return "agent"
    if flags.get("isCli"):
        return "cli"
    if audience == "public" and skill_name == "write-action":
        return "caller"
    if audience == "owner":
        return "owner"
    return "mid-call"


# ── Skill types ──────────────────────────────────────────────────────────────

SkillKind = Literal["cognitive", "operational"]
ContextDimension = Literal[
    "identity",
    "soul",
    "person",
    "actions",
    "call-origin",
    "recent-calls",
    "transcript",
]
InvokeSource = Literal["owner", "public", "pipeline", "scheduler"]


@dataclass(slots=True, frozen=True)
class SkillParameters:
    required: tuple[str, ...]
    properties: dict[str, str]


@dataclass(slots=True, frozen=True)
class SkillMetadata:
    name: str
    description: str
    kind: SkillKind
    invoke: tuple[InvokeSource, ...]
    modality: tuple[Modality, ...] | None
    context: tuple[ContextDimension, ...] | None
    output_format: str | None
    parameters: SkillParameters | None
    gotchas: str | None
    json_mode: bool
    soul_as_data: bool
    prompt_template: str | None
    path: Path
    has_handler: bool


@dataclass(slots=True, frozen=True)
class PersonContext:
    name: str | None
    summary: str | None
    facts: list[str]


@dataclass(slots=True, frozen=True)
class CallOriginContext:
    direction: Direction
    audience: Audience
    channel: Channel
    modality: InteractionModality
    action_intent: str | None = None
    action_id: str | None = None


@dataclass(slots=True, frozen=True)
class SelfContext:
    identity: str | None = None
    soul: str | None = None
    person: PersonContext | None = None
    actions: list[str] | None = None
    call_origin: CallOriginContext | None = None
    recent_calls: list[str] | None = None
    transcript: str | None = None
    tool_context: str | None = None


@dataclass(slots=True, frozen=True)
class OperationalContext:
    audience: Audience
    call_id: str
    person_id: str
    source: FactSource
    tool_context: str | None = None
    audio_source: Any | None = None


@dataclass(slots=True, frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolResult:
    tool_call_id: str
    result: str


class CognitiveHandler(Protocol):
    async def __call__(
        self,
        system_prompt: str,
        data: str,
        params: dict[str, Any],
        options: dict[str, Any],
    ) -> str: ...


class OperationalHandler(Protocol):
    async def __call__(
        self,
        db: sqlite3.Connection,
        context: OperationalContext,
        params: dict[str, Any],
    ) -> str: ...


SkillRegistry: TypeAlias = dict[str, SkillMetadata]
