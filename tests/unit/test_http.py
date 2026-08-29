from __future__ import annotations

import asyncio
import json
import subprocess
import unittest
from typing import Mapping
from unittest.mock import patch

from mystic.http import (
    HttpResponse,
    TAILSCALE_FUNNEL_TIMEOUT_SECONDS,
    create_client,
    fetch_with_timeout,
    start_tunnel,
)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_returns_response_before_timeout(self) -> None:
        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://example.test")
            self.assertGreater(timeout, 0)
            return HttpResponse(status_code=200, content=b"ok", headers={"content-type": "text/plain"})

        response = await fetch_with_timeout("https://example.test", timeout_ms=500, timeout_label="test.request", transport=transport)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")

    async def test_fetch_raises_timeout_with_label(self) -> None:
        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            await asyncio.sleep(0.05)
            return HttpResponse(status_code=200, content=b"late")

        with self.assertRaisesRegex(TimeoutError, "test.timeout timed out after 10ms"):
            await fetch_with_timeout("https://example.test/hang", timeout_ms=10, timeout_label="test.timeout", transport=transport)

    async def test_client_encodes_json_body(self) -> None:
        captured_payload: bytes | None = None
        captured_headers: dict[str, str] = {}

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, url, timeout
            nonlocal captured_payload, captured_headers
            captured_payload = payload
            captured_headers = dict(headers)
            return HttpResponse(status_code=201, content=b"{\"ok\":true}")

        client = create_client(timeout_ms=250, transport=transport)
        response = await client.post("https://example.test", json_body={"ok": True})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(captured_payload, b"{\"ok\": true}")
        self.assertEqual(captured_headers["Content-Type"], "application/json")

    async def test_start_tunnel_uses_funnel_specific_timeout(self) -> None:
        calls: list[tuple[list[str], float]] = []

        def fake_run(
            args: list[str],
            *,
            capture_output: bool,
            text: bool,
            check: bool,
            timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            calls.append((args, timeout))
            if args[:3] == ["tailscale", "status", "--json"]:
                payload = {"Self": {"DNSName": "agent.tail1234.ts.net."}}
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch("mystic.http.subprocess.run", side_effect=fake_run):
            tunnel_url = await start_tunnel(3037)

        self.assertEqual(tunnel_url, "https://agent.tail1234.ts.net")
        self.assertEqual(calls[-1][0], ["tailscale", "funnel", "--bg", "--yes", "--https=443", "3037"])
        self.assertEqual(calls[-1][1], TAILSCALE_FUNNEL_TIMEOUT_SECONDS)

    async def test_start_tunnel_reports_activation_timeout(self) -> None:
        def fake_run(
            args: list[str],
            *,
            capture_output: bool,
            text: bool,
            check: bool,
            timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if args[:3] == ["tailscale", "status", "--json"]:
                payload = {"Self": {"DNSName": "agent.tail1234.ts.net."}}
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

        with patch("mystic.http.subprocess.run", side_effect=fake_run):
            expected = f"tailscale funnel activation timed out after {TAILSCALE_FUNNEL_TIMEOUT_SECONDS}s"
            with self.assertRaisesRegex(TimeoutError, expected):
                await start_tunnel(3037)
