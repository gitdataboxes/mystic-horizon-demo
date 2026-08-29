"""Action lifecycle, scheduler, and satisfaction checks."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, tzinfo
import sys
from typing import Any, Mapping, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mystic.llm import parse_json
from mystic.config import (
    get_agent_config,
    get_error_message,
    get_providers_config,
    get_smtp_config,
    get_tunnel_url,
    identity_exists,
    logger,
    soul_exists,
)
from mystic.db import (
    get_action_by_id,
    get_all_active_facts_by_person,
    get_call_by_id,
    get_due_actions,
    get_open_actions_by_person,
    get_person_by_id,
    increment_action_attempts,
    insert_action,
    now_ms,
    reset_action_to_pending,
    start_action_attempt as start_action_attempt_in_db,
    update_action_context,
    update_action_due_at,
    update_action_status,
    upsert_person,
)
from mystic.types import (
    Action,
    CallOriginContext,
    PersonContext,
    SatisfactionJudgment,
    SchedulerDecision,
    SchedulerJudgment,
    SelfContext,
)

# ── lifecycle ──────────────────────────────────────────────────────────────────

DEFAULT_ACTION_RETRY_DELAY_MS = 60 * 60 * 1000


def _detect_notifier() -> tuple[str, str] | None:
    if sys.platform.startswith("linux"):
        path = shutil.which("notify-send")
        if path:
            return ("linux", path)
    if sys.platform == "darwin":
        path = shutil.which("osascript")
        if path:
            return ("macos", path)
    return None


_notifier = _detect_notifier()


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def notify(title: str, body: str) -> bool:
    if _notifier is None:
        logger.info("notify.fallback", title=title, body=body)
        return False

    notifier_kind, notifier_path = _notifier
    if notifier_kind == "linux":
        command = (
            notifier_path,
            "--app-name=mystic-horizon",
            title,
            body,
        )
    else:
        script = (
            f'display notification "{_escape_applescript(body)}" '
            f'with title "{_escape_applescript(title)}"'
        )
        command = (notifier_path, "-e", script)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            logger.warn("notify.timeout", title=title, notifier=notifier_kind)
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return True
        if process.returncode not in (None, 0):
            logger.warn(
                "notify.failed",
                title=title,
                notifier=notifier_kind,
                returncode=process.returncode,
            )
            return False
    except Exception as exc:
        logger.warn(
            "notify.error",
            title=title,
            notifier=notifier_kind,
            error=get_error_message(exc),
        )
        return False
    return True


async def send_email(to: str, subject: str, body: str) -> None:
    """Send an email via configured SMTP. Raises on failure."""
    config = get_smtp_config()
    if config is None:
        raise RuntimeError("SMTP not configured")

    def _send() -> None:
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = config.from_address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        smtp_cls = smtplib.SMTP_SSL if config.use_tls and config.port == 465 else smtplib.SMTP
        with smtp_cls(config.host, config.port, timeout=30) as server:
            if config.use_tls and config.port != 465:
                server.starttls()
            server.login(config.username, config.password)
            server.send_message(message)

    await asyncio.to_thread(_send)


def complete_action(db: sqlite3.Connection, action_id: str, reason: str) -> None:
    update_action_status(db, action_id, "completed", reason)
    logger.info("action.completed", actionId=action_id, reason=reason)


def attempt_action(db: sqlite3.Connection, action_id: str) -> bool:
    increment_action_attempts(db, action_id)

    action = get_action_by_id(db, action_id)
    if action is None:
        return False

    if action.attempts >= action.max_attempts:
        fail_action(db, action_id, f"Max attempts reached ({action.max_attempts})")
        return True

    return False


def start_action_attempt(db: sqlite3.Connection, action_id: str) -> None:
    start_action_attempt_in_db(db, action_id)
    logger.info("action.in_progress", actionId=action_id)


def fail_action(db: sqlite3.Connection, action_id: str, result: str) -> None:
    update_action_status(db, action_id, "failed", result)
    logger.warn("action.failed", actionId=action_id, result=result)


def cancel_action(db: sqlite3.Connection, action_id: str, reason: str) -> None:
    update_action_status(db, action_id, "cancelled", reason)
    logger.info("action.cancelled", actionId=action_id, reason=reason)


def reschedule_action(db: sqlite3.Connection, action_id: str, due_at: int | None) -> None:
    update_action_due_at(db, action_id, due_at)
    logger.info(
        "action.rescheduled",
        actionId=action_id,
        dueAt=_format_due_at(due_at),
    )


def finalize_in_progress_action(
    db: sqlite3.Connection,
    action_id: str,
    note: str,
    due_at: int | None = None,
) -> None:
    action = get_action_by_id(db, action_id)
    if action is None or action.status != "in_progress":
        return

    retry_due_at = due_at if due_at is not None else now_ms() + DEFAULT_ACTION_RETRY_DELAY_MS
    if action.attempts >= action.max_attempts:
        fail_action(
            db,
            action_id,
            f"{note} Max attempts reached ({action.max_attempts}).",
        )
        return

    reset_action_to_pending(db, action_id, retry_due_at, note)
    logger.info(
        "action.requeued",
        actionId=action_id,
        dueAt=_format_due_at(retry_due_at),
        note=note,
    )


def append_action_context(db: sqlite3.Connection, action_id: str, note: str) -> None:
    action = get_action_by_id(db, action_id)
    if action is None:
        return

    prefix = f"[{_utc_timestamp()}] {note}"
    updated_context = f"{action.context}\n{prefix}" if action.context else prefix
    update_action_context(db, action_id, updated_context)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_due_at(due_at: int | None) -> str:
    if due_at is None:
        return "ASAP"
    return datetime.fromtimestamp(due_at / 1000, UTC).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


# ── scheduler ─────────────────────────────────────────────────────────────────

SCHEDULER_INTERVAL_MS = 60_000

OutboundCallStarter = Callable[[sqlite3.Connection, Action, str], Awaitable[str | None]]

_scheduler_task: asyncio.Task[None] | None = None
_is_running = False


def start_scheduler(
    db: sqlite3.Connection,
    tunnel_url: str,
    *,
    interval_ms: int = SCHEDULER_INTERVAL_MS,
    initiate_outbound_call: OutboundCallStarter | None = None,
) -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return

    logger.info("scheduler.started")
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(
            db,
            tunnel_url,
            interval_ms=interval_ms,
            initiate_outbound_call=initiate_outbound_call,
        )
    )


def stop_scheduler() -> None:
    global _scheduler_task
    task = _scheduler_task
    _scheduler_task = None
    if task is None:
        return
    if not task.done():
        task.cancel()
    logger.info("scheduler.stopped")


async def drain_scheduler(timeout_ms: int = 0) -> None:
    global _scheduler_task
    task = _scheduler_task
    _scheduler_task = None
    if task is None:
        return

    if not task.done():
        task.cancel()
        logger.info("scheduler.stopped")

    try:
        if timeout_ms > 0:
            await asyncio.wait_for(task, timeout=timeout_ms / 1000)
        else:
            await task
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        logger.warn("scheduler.stop.timeout", timeoutMs=timeout_ms)
    except Exception as exc:
        logger.warn("scheduler.stop.error", error=get_error_message(exc))


async def scheduler_tick(
    db: sqlite3.Connection,
    tunnel_url: str,
    *,
    initiate_outbound_call: OutboundCallStarter | None = None,
) -> None:
    global _is_running
    if _is_running:
        return
    _is_running = True

    try:
        tunnel_url = get_tunnel_url() or tunnel_url
        from mystic.calendar import check_reminders, maybe_retry_hub_sync, maybe_sync

        await maybe_sync(db)
        await check_reminders(db)
        await maybe_retry_hub_sync(db)
        due_actions = get_due_actions(db)
        if not due_actions:
            return

        bootstrap_completed = identity_exists() and soul_exists()
        has_twilio = get_providers_config().twilio is not None
        actionable: list[Action] = []
        auto_cancelled = 0
        for action in due_actions:
            if bootstrap_completed and is_bootstrap_action(action):
                cancel_action(
                    db,
                    action.id,
                    "Bootstrap already completed (IDENTITY.md and SOUL.md exist).",
                )
                auto_cancelled += 1
                continue
            if is_bootstrap_action(action) and not has_twilio:
                reschedule_action(db, action.id, now_ms() + DEFAULT_ACTION_RETRY_DELAY_MS)
                continue
            actionable.append(action)

        if not actionable:
            return

        logger.info(
            "scheduler.tick",
            dueCount=len(actionable),
            autoCancelledBootstrap=auto_cancelled,
        )

        judgments = await get_scheduler_judgments(db, actionable)
        for judgment in judgments:
            action = next((candidate for candidate in actionable if candidate.id == judgment.id), None)
            if action is None:
                continue
            try:
                await apply_decision(
                    db,
                    action,
                    judgment,
                    tunnel_url,
                    initiate_outbound_call=initiate_outbound_call,
                )
            except Exception as exc:
                logger.error(
                    "scheduler.apply.error",
                    actionId=action.id,
                    decision=judgment.decision,
                    error=get_error_message(exc),
                )
    except Exception as exc:
        logger.error("scheduler.error", error=get_error_message(exc))
    finally:
        _is_running = False


def is_bootstrap_action(action: Action) -> bool:
    intent = action.intent.lower()
    context = (action.context or "").lower()
    return intent == "get to know owner" or "bootstrap" in intent or "bootstrap" in context


async def get_scheduler_judgments(
    db: sqlite3.Connection,
    actions: list[Action],
) -> list[SchedulerJudgment]:
    from mystic.skills import execute_cognitive_skill

    agent = get_agent_config()
    tz = _get_timezone(agent.hours.timezone)
    now = datetime.now(tz)
    current_time = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    day_of_week = now.strftime("%A")
    days = ", ".join(day.capitalize() for day in agent.hours.days)
    business_hours = (
        f"{agent.hours.start}:00-{agent.hours.end}:00 {agent.hours.timezone} ({days})"
    )

    lines: list[str] = []
    for action in actions:
        person = get_person_by_id(db, action.person_id) if action.person_id else None
        due_text = _format_local_time(action.due_at, tz) if action.due_at is not None else "ASAP"
        last_seen = _format_local_date(person.last_seen, tz) if person else "never"
        lines.append(
            "\n".join(
                (
                    f"- [{action.id}] {action.intent}",
                    f"  Person: {person.name if person and person.name else 'Unknown'} (last contact: {last_seen})",
                    f"  Due: {due_text}, Urgency: {action.urgency}",
                    f"  Attempts: {action.attempts}/{action.max_attempts}",
                )
            )
        )

    data = (
        f"Current time: {current_time} ({day_of_week})\n"
        f"Business hours: {business_hours}\n\n"
        f"Due actions:\n{chr(10).join(lines)}\n\n"
        "Evaluate the actions above and return your judgment."
    )
    raw = await execute_cognitive_skill("judge-schedule", SelfContext(), data)
    return _parse_judgments(raw)


async def apply_decision(
    db: sqlite3.Connection,
    action: Action,
    judgment: SchedulerJudgment,
    tunnel_url: str,
    *,
    initiate_outbound_call: OutboundCallStarter | None = None,
) -> None:
    logger.info(
        "scheduler.judgment",
        actionId=action.id,
        decision=judgment.decision,
        reason=judgment.reason,
    )

    if judgment.decision == "act":
        if is_bootstrap_action(action):
            from mystic.calls import get_default_voice_id, initiate_bootstrap_call

            providers = get_providers_config()
            agent = get_agent_config()
            if providers.twilio and agent.owner.phone and action.person_id:
                await initiate_bootstrap_call(
                    db=db,
                    twilio_config=providers.twilio,
                    livekit_config=providers.livekit,
                    customer_phone=agent.owner.phone,
                    person_id=action.person_id,
                    action_id=action.id,
                    voice_id=agent.agent.voiceId or get_default_voice_id(providers.tts),
                    tunnel_url=tunnel_url,
                )
            return

        agent = get_agent_config()
        if agent.owner.phone is not None:
            starter = _resolve_call_initiator(initiate_outbound_call)
            call_id = await starter(db, action, tunnel_url)
        else:
            from mystic.calls import initiate_local_escalation

            call_id = await initiate_local_escalation(db, action)
            if call_id:
                logger.info("scheduler.act.local.bridge", actionId=action.id, callId=call_id)
        if not call_id:
            exhausted = attempt_action(db, action.id)
            if not exhausted:
                reschedule_action(db, action.id, now_ms() + DEFAULT_ACTION_RETRY_DELAY_MS)
        return

    if judgment.decision == "notify":
        await notify(f"Action due: {action.intent}", judgment.reason)
        reschedule_action(db, action.id, now_ms() + DEFAULT_ACTION_RETRY_DELAY_MS)
        return

    if judgment.decision == "wait":
        wait_until = _parse_wait_until(judgment.wait_until)
        reschedule_action(
            db,
            action.id,
            wait_until if wait_until is not None else now_ms() + DEFAULT_ACTION_RETRY_DELAY_MS,
        )
        return

    if judgment.decision == "cancel":
        cancel_action(db, action.id, judgment.reason)
        return

    escalation_action_id = await escalate_to_owner(
        db,
        action,
        tunnel_url,
        judgment.reason,
        initiate_outbound_call=initiate_outbound_call,
    )
    cancel_action(
        db,
        action.id,
        f"Escalated to owner via action {escalation_action_id}: {judgment.reason}",
    )


async def escalate_to_owner(
    db: sqlite3.Connection,
    action: Action,
    tunnel_url: str,
    reason: str,
    *,
    initiate_outbound_call: OutboundCallStarter | None = None,
) -> str:
    agent = get_agent_config()
    if agent.owner.phone is not None:
        owner = upsert_person(db, agent.owner.phone)
    else:
        from mystic.calls import LOCAL_OWNER_PHONE

        owner = upsert_person(db, LOCAL_OWNER_PHONE)
    escalation_action = insert_action(
        db,
        person_id=owner.id,
        intent=f"Escalation: {action.intent} - {reason}",
        context=f"Original action {action.id} escalated. {action.context or ''}".strip(),
        source="agent",
        urgency="high",
    )
    logger.info(
        "scheduler.escalate",
        originalActionId=action.id,
        escalationActionId=escalation_action.id,
        reason=reason,
    )

    if agent.owner.phone is not None:
        starter = _resolve_call_initiator(initiate_outbound_call)
        await starter(db, escalation_action, tunnel_url)
    else:
        from mystic.calls import initiate_local_escalation

        call_id = await initiate_local_escalation(db, escalation_action)
        if call_id:
            logger.info("scheduler.escalate.local.bridge", escalationActionId=escalation_action.id, callId=call_id)
        else:
            logger.info("scheduler.escalate.local.pending", escalationActionId=escalation_action.id)
    return escalation_action.id


async def _scheduler_loop(
    db: sqlite3.Connection,
    tunnel_url: str,
    *,
    interval_ms: int,
    initiate_outbound_call: OutboundCallStarter | None,
) -> None:
    try:
        await scheduler_tick(
            db,
            tunnel_url,
            initiate_outbound_call=initiate_outbound_call,
        )
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await scheduler_tick(
                db,
                tunnel_url,
                initiate_outbound_call=initiate_outbound_call,
            )
    except asyncio.CancelledError:
        raise


def _parse_judgments(raw: str) -> list[SchedulerJudgment]:
    parsed = parse_json(raw)
    if isinstance(parsed, Mapping):
        parsed = next((v for v in cast(Mapping[str, object], parsed).values() if isinstance(v, list)), parsed)
    if not isinstance(parsed, list):
        raise ValueError("Scheduler response must be a JSON array")

    judgments: list[SchedulerJudgment] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, Mapping):
            continue
        item_map = cast(Mapping[str, object], item)
        decision_obj = item_map.get("decision")
        if decision_obj not in {"act", "wait", "cancel", "escalate", "notify"}:
            continue
        judgments.append(
            SchedulerJudgment(
                id=str(item_map.get("id", "")),
                decision=cast(SchedulerDecision, decision_obj),
                reason=str(item_map.get("reason", "")),
                wait_until=str(item_map.get("wait_until")) if item_map.get("wait_until") else None,
            )
        )
    return judgments


def _resolve_call_initiator(
    initiate_outbound_call: OutboundCallStarter | None,
) -> OutboundCallStarter:
    if initiate_outbound_call is not None:
        return initiate_outbound_call

    try:
        from mystic.calls import initiate_outbound_call as default_initiator
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on later phases.
        raise RuntimeError("Outbound call initiation is not available yet") from exc

    return default_initiator


def _parse_wait_until(value: str | None) -> int | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _get_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return UTC


def _format_local_time(timestamp_ms: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_local_date(timestamp_ms: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz).strftime("%Y-%m-%d")


# ── satisfaction ──────────────────────────────────────────────────────────────


async def check_satisfaction(
    db: sqlite3.Connection,
    call_id: str,
    person_id: str,
) -> None:
    from mystic.skills import execute_cognitive_skill

    open_actions = get_open_actions_by_person(db, person_id)
    if not open_actions:
        return

    call = get_call_by_id(db, call_id)
    if call is None:
        return

    person = get_person_by_id(db, person_id)
    person_name = person.name if person and person.name else "the caller"
    recent_facts = [
        f"- {fact.content}"
        for fact in get_all_active_facts_by_person(db, person_id)
        if fact.call_id == call_id
    ]
    formatted_actions = [
        f"[{action.id}] {action.intent} (due: {_format_due_at(action.due_at)}, attempts: {action.attempts})"
        for action in open_actions
    ]

    self_context = SelfContext(
        person=PersonContext(name=person_name, summary=None, facts=[]),
        actions=formatted_actions,
        call_origin=CallOriginContext(
            direction=call.direction,
            audience=call.audience,
            channel=call.channel,
            modality=call.modality,
        ),
    )
    data = (
        f"Call summary: {call.summary or 'No summary available yet'}\n\n"
        f"Facts extracted from this call:\n{chr(10).join(recent_facts) or 'None'}\n\n"
        f"Transcript:\n{call.transcript or 'No transcript available'}"
    )

    try:
        raw = await execute_cognitive_skill("check-satisfaction", self_context, data)
        for judgment in _parse_satisfaction_judgments(raw):
            action = next((candidate for candidate in open_actions if candidate.id == judgment.id), None)
            if action is None:
                continue

            if judgment.status == "satisfied":
                complete_action(db, judgment.id, judgment.reason)
                logger.info(
                    "satisfaction.resolved",
                    actionId=judgment.id,
                    confidence=judgment.confidence,
                    reason=judgment.reason,
                )
            elif judgment.status == "partial":
                append_action_context(
                    db,
                    judgment.id,
                    f"Partially addressed in call {call_id}: {judgment.reason}",
                )
                logger.info(
                    "satisfaction.partial",
                    actionId=judgment.id,
                    confidence=judgment.confidence,
                    reason=judgment.reason,
                )
            else:
                logger.debug(
                    "satisfaction.unchanged",
                    actionId=judgment.id,
                    reason=judgment.reason,
                )
    except Exception as exc:
        logger.error(
            "satisfaction.failed",
            callId=call_id,
            personId=person_id,
            error=get_error_message(exc),
        )


def _parse_satisfaction_judgments(raw: str) -> list[SatisfactionJudgment]:
    parsed = parse_json(raw)
    if not isinstance(parsed, list):
        raise ValueError("Satisfaction response must be a JSON array")

    judgments: list[SatisfactionJudgment] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, Mapping):
            continue
        item_map = cast(Mapping[str, object], item)
        status_obj = item_map.get("status")
        if status_obj not in {"satisfied", "partial", "not_satisfied"}:
            continue
        confidence_obj = item_map.get("confidence")
        confidence = float(confidence_obj) if isinstance(confidence_obj, (int, float)) else 0.0
        judgments.append(
            SatisfactionJudgment(
                id=str(item_map.get("id", "")),
                status=cast(Any, status_obj),
                confidence=confidence,
                reason=str(item_map.get("reason", "")),
            )
        )
    return judgments
