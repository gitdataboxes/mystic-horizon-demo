from __future__ import annotations

import asyncio
import json
import unittest
from typing import cast

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from mystic.calls import reset_active_calls
from mystic.config import get_providers_config
from mystic.db import close_database, initialize_schema, insert_action, open_database
from mystic.server import clear_rate_limit_store, create_app
from mystic.web import (
    SESSION_COOKIE,
    _SSE_CLIENTS,
    _shutdown_dashboard_streams,
    broadcast,
    build_session_cookie,
)
from tests.python_helpers import TempAppHome, seed_core_files

TUNNEL_URL = "https://test-machine.tail1234.ts.net"


class BroadcastUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _SSE_CLIENTS.clear()

    def tearDown(self) -> None:
        _SSE_CLIENTS.clear()

    async def test_broadcast_delivers_to_registered_queue(self) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue()
        _SSE_CLIENTS.add(queue)

        await broadcast("test", {"key": "val"})

        payload = queue.get_nowait()
        self.assertEqual(payload, 'event: test\ndata: {"key": "val"}\n\n')

    async def test_broadcast_discards_dead_clients_on_queue_full(self) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        queue.put_nowait("filler")
        _SSE_CLIENTS.add(queue)

        await broadcast("ping", {"n": 1})

        self.assertNotIn(queue, _SSE_CLIENTS)

    async def test_broadcast_delivers_to_multiple_clients(self) -> None:
        queues = [asyncio.Queue() for _ in range(3)]
        for q in queues:
            _SSE_CLIENTS.add(q)

        await broadcast("multi", {"ok": True})

        for q in queues:
            payload = q.get_nowait()
            self.assertEqual(payload, 'event: multi\ndata: {"ok": true}\n\n')

    async def test_shutdown_wakes_full_stream_queues(self) -> None:
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
        queue.put_nowait("filler")
        _SSE_CLIENTS.add(queue)

        await _shutdown_dashboard_streams(web.Application())

        self.assertIsNone(queue.get_nowait())


class FragmentChatBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        self.db = open_database(":memory:")
        initialize_schema(self.db)
        reset_active_calls(self.db)
        clear_rate_limit_store()
        self.app = create_app(self.db, TUNNEL_URL)
        _SSE_CLIENTS.clear()

    async def asyncTearDown(self) -> None:
        _SSE_CLIENTS.clear()
        clear_rate_limit_store()
        reset_active_calls(self.db)
        close_database(self.db)
        self.temp_home.__exit__(None, None, None)

    async def test_action_complete_broadcasts_activity_event(self) -> None:
        dashboard = get_providers_config().dashboard
        assert dashboard is not None
        cookie = build_session_cookie(dashboard.token)

        action = insert_action(self.db, intent="Test task", source="owner")

        spy_queue: asyncio.Queue[str] = asyncio.Queue()
        _SSE_CLIENTS.add(spy_queue)

        response = cast(web.Response, await self._invoke_app(
            "POST",
            f"/dashboard/f/action/{action.id}/complete",
            cookies={SESSION_COOKIE: cookie},
            body=json.dumps({"result": "Done"}),
            content_type="application/json",
        ))

        self.assertEqual(response.status, 200)
        body = json.loads(response.text or "")
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "completed")

        payload = spy_queue.get_nowait()
        self.assertIn("event: activity", payload)
        self.assertIn(action.id, payload)

    async def _invoke_app(
        self,
        method: str,
        path: str,
        *,
        cookies: dict[str, str] | None = None,
        body: str | None = None,
        content_type: str | None = None,
    ) -> web.Response:
        headers: dict[str, str] = {"Host": "localhost"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if content_type:
            headers["Content-Type"] = content_type
        request = make_mocked_request(
            method,
            path,
            headers=headers,
            app=self.app,
        )
        if body is not None:
            request._payload_writer = None  # type: ignore[assignment]
            request._read_bytes = body.encode("utf-8")  # type: ignore[attr-defined]
        match_info = await self.app.router.resolve(request)
        request._match_info = match_info  # type: ignore[attr-defined]
        result = await match_info.handler(request)
        return result  # type: ignore[return-value]


if __name__ == "__main__":
    unittest.main()
