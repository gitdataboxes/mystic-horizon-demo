from __future__ import annotations

import base64
import json
import unittest
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from livekit import api

from mystic.config import LiveKitConfig
from mystic.livekit import (
    MYSTIC_HORIZON_AGENT_NAME,
    create_room,
    dispatch_agent_to_room,
    generate_token,
    parse_transcript_entries,
)

CONFIG = LiveKitConfig(
    host="127.0.0.1",
    port=7880,
    apiKey="API1234567890abcdef1234567890abcd",
    apiSecret="secret-value-which-is-long-enough-for-jwt-signing",
)


class FakeRoomService:
    def __init__(self) -> None:
        self.create_requests: list[object] = []

    async def create_room(self, request: object) -> object:
        self.create_requests.append(request)
        room = api.Room()
        room.name = getattr(request, "name")
        return room


class FakeAgentDispatchService:
    def __init__(self) -> None:
        self.create_requests: list[object] = []
        self.list_calls: list[str] = []
        self.list_results: list[list[object]] = []

    async def create_dispatch(self, request: object) -> object:
        self.create_requests.append(request)
        dispatch = api.AgentDispatch()
        dispatch.room = getattr(request, "room")
        dispatch.agent_name = getattr(request, "agent_name")
        return dispatch

    async def list_dispatch(self, room_name: str) -> list[object]:
        self.list_calls.append(room_name)
        if self.list_results:
            return self.list_results.pop(0)
        return []


class FakeLiveKitClient:
    def __init__(self) -> None:
        self.room = FakeRoomService()
        self.agent_dispatch = FakeAgentDispatchService()

    async def aclose(self) -> None:
        return None


class LiveKitRoomTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_room_creates_room_and_dispatches_agent(self) -> None:
        client = FakeLiveKitClient()

        @asynccontextmanager
        async def open_client(_config: LiveKitConfig):
            yield client

        with patch("mystic.livekit._open_livekit_api", new=open_client):
            room_name = await create_room(CONFIG, "call-123", {"callId": "call-123"})

        self.assertEqual(room_name, "call-call-123")
        self.assertEqual(len(client.room.create_requests), 1)
        self.assertEqual(len(client.agent_dispatch.create_requests), 1)
        room_request = client.room.create_requests[0]
        dispatch_request = client.agent_dispatch.create_requests[0]
        self.assertEqual(getattr(room_request, "name"), "call-call-123")
        self.assertEqual(json.loads(getattr(room_request, "metadata")), {"callId": "call-123"})
        self.assertEqual(getattr(room_request, "empty_timeout"), 300)
        self.assertEqual(getattr(room_request, "max_participants"), 3)
        self.assertEqual(getattr(dispatch_request, "room"), "call-call-123")
        self.assertEqual(getattr(dispatch_request, "agent_name"), MYSTIC_HORIZON_AGENT_NAME)

    async def test_dispatch_agent_to_room_detects_existing_assignment(self) -> None:
        client = FakeLiveKitClient()
        existing_dispatch = api.AgentDispatch()
        existing_dispatch.state.jobs.add(agent_name=MYSTIC_HORIZON_AGENT_NAME)
        client.agent_dispatch.list_results = [[existing_dispatch]]

        @asynccontextmanager
        async def open_client(_config: LiveKitConfig):
            yield client

        with patch("mystic.livekit._open_livekit_api", new=open_client):
            status = await dispatch_agent_to_room(
                CONFIG,
                "call-123",
                create_if_missing=False,
                wait_for_assignment_ms=1_000,
                poll_ms=10,
            )

        self.assertEqual(status, "exists")
        self.assertEqual(client.agent_dispatch.create_requests, [])
        self.assertEqual(client.agent_dispatch.list_calls, ["call-call-123"])

    async def test_dispatch_agent_to_room_can_require_assignment(self) -> None:
        client = FakeLiveKitClient()

        @asynccontextmanager
        async def open_client(_config: LiveKitConfig):
            yield client

        with patch("mystic.livekit._open_livekit_api", new=open_client):
            with self.assertRaises(RuntimeError):
                await dispatch_agent_to_room(
                    CONFIG,
                    "call-123",
                    create_if_missing=False,
                    require_assignment=True,
                    wait_for_assignment_ms=0,
                )

    async def test_generate_token_sets_room_grants_and_metadata(self) -> None:
        token = await generate_token(
            CONFIG,
            "call-call-123",
            "bridge-call-123",
            {"callId": "call-123"},
        )

        payload = _decode_jwt_payload(token)
        self.assertEqual(payload["sub"], "bridge-call-123")
        self.assertEqual(json.loads(payload["metadata"]), {"callId": "call-123"})
        self.assertEqual(payload["video"]["room"], "call-call-123")
        self.assertTrue(payload["video"]["roomJoin"])
        self.assertTrue(payload["video"]["canPublish"])
        self.assertTrue(payload["video"]["canSubscribe"])
        self.assertTrue(payload["video"]["canPublishData"])

    def test_parse_transcript_entries_preserves_multiline_text_messages(self) -> None:
        transcript = (
            "[0:00] Caller [text]: Plan the follow-up\n"
            "[0:05] Agent [text]: **Checklist**\n"
            "- confirm\n"
            "- send notes\n"
            "\n"
            "Thanks."
        )

        entries = parse_transcript_entries(transcript)

        self.assertEqual(
            entries,
            [
                {"speaker": "user", "text": "Plan the follow-up", "modality": "text"},
                {
                    "speaker": "agent",
                    "text": "**Checklist**\n- confirm\n- send notes\n\nThanks.",
                    "modality": "text",
                },
            ],
        )

    def test_parse_transcript_entries_replays_tool_events(self) -> None:
        transcript = (
            "[0:00] Caller [text]: Check my calendar\n"
            '[0:01] Tool [event]: {"type":"tool_started","name":"read-calendar","args_summary":"today"}\n'
            '[0:02] Tool [event]: {"type":"tool_completed","name":"read-calendar","duration_ms":950,"error":false}\n'
            "[0:03] Agent [text]: You are free after lunch."
        )

        entries = parse_transcript_entries(transcript)

        self.assertEqual(
            entries,
            [
                {"speaker": "user", "text": "Check my calendar", "modality": "text"},
                {"type": "tool_started", "name": "read-calendar", "args_summary": "today"},
                {"type": "tool_completed", "name": "read-calendar", "duration_ms": 950, "error": False},
                {"speaker": "agent", "text": "You are free after lunch.", "modality": "text"},
            ],
        )




def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise AssertionError(f"Unexpected token format: {token}")
    payload = parts[1] + ("=" * (-len(parts[1]) % 4))
    return json.loads(base64.urlsafe_b64decode(payload))


if __name__ == "__main__":
    unittest.main()
