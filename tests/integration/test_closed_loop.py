"""Closed-loop smoke test: inbound call → extraction → scheduler → outbound → extraction.

This single test proves the product promise end-to-end: a caller creates a
commitment, the scheduler decides to act on it, an outbound call is placed,
and the action is finalized after the outbound call ends.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, patch

from mystic.actions import scheduler_tick
from mystic.calls import (
    handle_answered_outbound,
    handle_end_of_call_report,
    handle_incoming_call,
    set_extraction_pipeline,
)
from mystic.db import get_action_by_id, get_actions_by_call_id, get_due_actions, get_call_by_external_id, get_all_active_facts_by_person, get_person_by_phone, get_chunks_by_call_id
from mystic.memory import run_extraction_pipeline
from tests.integration.helpers import (
    PUBLIC_PHONE,
    SAMPLE_TRANSCRIPT,
    make_cognitive_skill_runner,
    make_transcript_embeddings,
)
from tests.python_helpers import make_embedding

OUTBOUND_TRANSCRIPT = (
    "Agent confirmed the dentist appointment for Friday at 3 PM. "
    "Caller said they would be there."
)


def _extraction_patches():
    """Common mock bundle for extraction pipeline externals."""
    return (
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(side_effect=make_transcript_embeddings),
        ),
        patch(
            "mystic.memory.embed_chunks",
            new=AsyncMock(return_value=[make_embedding([0.7, 0.1])]),
        ),
    )


async def test_closed_loop_inbound_to_scheduler_to_outbound(integration_env) -> None:
    """
    Full closed loop: inbound call creates a commitment via extraction,
    scheduler decides to act, outbound call is placed, and extraction
    finalizes the action.
    """
    db = integration_env.db
    tunnel = integration_env.tunnel_url

    # ------------------------------------------------------------------ #
    # PHASE 1: Inbound call arrives, extraction produces a commitment     #
    # ------------------------------------------------------------------ #

    inbound_extraction_done = asyncio.Event()

    async def inbound_extraction_pipeline(
        db: sqlite3.Connection,
        call_id: str,
        person_id: str,
        transcript: str,
    ) -> None:
        await run_extraction_pipeline(db, call_id, person_id, transcript)
        inbound_extraction_done.set()

    set_extraction_pipeline(inbound_extraction_pipeline)

    embed_transcript_patch, embed_facts_patch = _extraction_patches()

    with (
        patch("mystic.calls.create_room", new=AsyncMock(return_value="lk-room-inbound")),
        patch(
            "mystic.memory.execute_cognitive_skill",
            new=AsyncMock(
                side_effect=make_cognitive_skill_runner({
                    "summarize-call": '{"summary":"Caller needs dentist appointment scheduled."}',
                    "extract-facts": (
                        '{"facts":[{"content":"Needs dentist appointment Friday 3 PM",'
                        '"type":"context","confidence":0.9,'
                        '"source_text":"I need to see the dentist Friday at 3"}]}'
                    ),
                    "extract-commitments": (
                        '{"commitments":[{"content":"Call the dentist to schedule Friday 3 PM appointment",'
                        '"intent":"Schedule dentist appointment",'
                        '"due":null,"urgency":"normal"}]}'
                    ),
                    "summarize-person": '{"summary":"Needs a dentist appointment Friday at 3 PM."}',
                })
            ),
        ),
        patch(
            "mystic.skills.execute_cognitive_skill",
            new=AsyncMock(return_value="[]"),
        ),
        embed_transcript_patch,
        embed_facts_patch,
    ):
        result = await handle_incoming_call(db, PUBLIC_PHONE, "CA-loop-inbound", tunnel)
        assert "twiml" in result
        assert "<Stream" in result["twiml"]

        await handle_end_of_call_report(db, "CA-loop-inbound", SAMPLE_TRANSCRIPT, 55)
        await asyncio.wait_for(inbound_extraction_done.wait(), timeout=2)

    # --- Assert Phase 1 state ---
    person = get_person_by_phone(db, PUBLIC_PHONE)
    inbound_call = get_call_by_external_id(db, "CA-loop-inbound")
    assert person is not None
    assert inbound_call is not None
    assert inbound_call.summary == "Caller needs dentist appointment scheduled."
    assert inbound_call.facts_extracted == 1
    assert inbound_call.commitments_extracted == 1
    assert inbound_call.duration == 55

    post_call_actions = [
        a for a in get_actions_by_call_id(db, inbound_call.id) if a.source == "post-call"
    ]
    assert len(post_call_actions) == 1
    commitment_action = post_call_actions[0]
    assert commitment_action.intent == "Schedule dentist appointment"
    assert commitment_action.status == "pending"

    facts = get_all_active_facts_by_person(db, person.id)
    assert any("dentist" in f.content.lower() for f in facts)

    chunks = get_chunks_by_call_id(db, inbound_call.id)
    assert len(chunks) >= 1

    # ------------------------------------------------------------------ #
    # PHASE 2: Scheduler fires, decides "act", outbound call initiated    #
    # ------------------------------------------------------------------ #

    # The action has due_at=None which means "ASAP" — it should be picked up
    due = get_due_actions(db)
    assert any(a.id == commitment_action.id for a in due)

    outbound_call_id_holder: list[str] = []

    async def fake_initiate_outbound(db_arg, action, tunnel_url):
        """Minimal outbound stub: create call + mark action in_progress."""
        from mystic.calls import initiate_outbound_call

        with (
            patch(
                "mystic.calls.assemble_outbound_context",
                new=AsyncMock(return_value="Outbound prompt for closed-loop test"),
            ),
            patch("mystic.calls.create_room", new=AsyncMock(return_value="lk-room-outbound")),
            patch("mystic.calls.make_outbound_call", new=AsyncMock(return_value="CA-loop-outbound")),
        ):
            call_id = await initiate_outbound_call(db_arg, action, tunnel_url)
            if call_id:
                outbound_call_id_holder.append(call_id)
            return call_id

    with patch(
        "mystic.skills.execute_cognitive_skill",
        new=AsyncMock(
            return_value=json.dumps([{
                "id": commitment_action.id,
                "decision": "act",
                "reason": "Due now, within business hours",
            }])
        ),
    ):
        await scheduler_tick(db, tunnel, initiate_outbound_call=fake_initiate_outbound)

    # --- Assert Phase 2 state ---
    assert len(outbound_call_id_holder) == 1
    outbound_call = get_call_by_external_id(db, "CA-loop-outbound")
    assert outbound_call is not None
    assert outbound_call.direction == "outbound"
    assert outbound_call.action_id == commitment_action.id

    acted_action = get_action_by_id(db, commitment_action.id)
    assert acted_action is not None
    assert acted_action.status == "in_progress"
    assert acted_action.attempts == 1

    # ------------------------------------------------------------------ #
    # PHASE 3: Outbound call answered, ends, extraction finalizes action  #
    # ------------------------------------------------------------------ #

    outbound_extraction_done = asyncio.Event()

    async def outbound_extraction_pipeline(
        db: sqlite3.Connection,
        call_id: str,
        person_id: str,
        transcript: str,
    ) -> None:
        await run_extraction_pipeline(db, call_id, person_id, transcript)
        outbound_extraction_done.set()

    set_extraction_pipeline(outbound_extraction_pipeline)

    embed_transcript_patch2, embed_facts_patch2 = _extraction_patches()

    with (
        patch(
            "mystic.memory.execute_cognitive_skill",
            new=AsyncMock(
                side_effect=make_cognitive_skill_runner({
                    "summarize-call": '{"summary":"Confirmed dentist appointment for Friday 3 PM."}',
                    "extract-facts": (
                        '{"facts":[{"content":"Dentist appointment confirmed Friday 3 PM",'
                        '"type":"context","confidence":0.95,'
                        '"source_text":"Confirmed for Friday at 3 PM"}]}'
                    ),
                    "extract-commitments": '{"commitments":[]}',
                    "summarize-person": '{"summary":"Has confirmed dentist appointment Friday 3 PM."}',
                })
            ),
        ),
        patch(
            "mystic.skills.execute_cognitive_skill",
            new=AsyncMock(return_value="[]"),
        ),
        embed_transcript_patch2,
        embed_facts_patch2,
    ):
        handle_answered_outbound(db, "CA-loop-outbound")
        await handle_end_of_call_report(db, "CA-loop-outbound", OUTBOUND_TRANSCRIPT, 38)
        await asyncio.wait_for(outbound_extraction_done.wait(), timeout=2)

    # --- Assert Phase 3: closed loop ---
    final_outbound_call = get_call_by_external_id(db, "CA-loop-outbound")
    assert final_outbound_call is not None
    assert final_outbound_call.answered_at is not None
    assert final_outbound_call.ended_at is not None
    assert final_outbound_call.duration == 38
    assert final_outbound_call.summary == "Confirmed dentist appointment for Friday 3 PM."
    assert final_outbound_call.facts_extracted == 1
    assert final_outbound_call.commitments_extracted == 1

    final_action = get_action_by_id(db, commitment_action.id)
    assert final_action is not None
    # Action was in_progress, call ended → finalize_in_progress_action requeues
    assert final_action.status == "pending"
    assert final_action.attempts == 1
    assert "Call completed without fully resolving the action." in (final_action.result or "")

    # Facts accumulated across both calls
    all_facts = get_all_active_facts_by_person(db, person.id)
    fact_contents = [f.content.lower() for f in all_facts]
    assert any("dentist" in c for c in fact_contents)

    # Person summary was updated
    updated_person = get_person_by_phone(db, PUBLIC_PHONE)
    assert updated_person is not None
    assert updated_person.summary is not None
    assert "dentist" in updated_person.summary.lower()

    # Outbound call also has transcript chunks
    outbound_chunks = get_chunks_by_call_id(db, final_outbound_call.id)
    assert len(outbound_chunks) >= 1
