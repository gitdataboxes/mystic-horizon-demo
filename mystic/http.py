"""HTTP client and Tailscale tunnel helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Awaitable, Mapping, cast
from urllib import request as urllib_request

from mystic.config import TwilioConfig, logger

DEFAULT_TIMEOUT_MS = 15_000
TAILSCALE_STATUS_TIMEOUT_SECONDS = 10
TAILSCALE_FUNNEL_TIMEOUT_SECONDS = 60
TAILSCALE_COMMAND_TIMEOUT_SECONDS = TAILSCALE_STATUS_TIMEOUT_SECONDS


def _empty_headers() -> dict[str, str]:
    return {}


@dataclass(slots=True)
class HttpResponse:
    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=_empty_headers)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


TransportResult = Awaitable[HttpResponse] | HttpResponse
RequestTransport = Callable[[str, str, Mapping[str, str], bytes | None, float], TransportResult]


class AsyncHttpClient:
    def __init__(self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS, transport: RequestTransport | None = None) -> None:
        self.timeout_ms = timeout_ms
        self.transport = transport or _default_transport

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        data: bytes | str | None = None,
        timeout_ms: int | None = None,
        timeout_label: str = "request",
    ) -> HttpResponse:
        payload = _normalize_payload(data=data, json_body=json_body)
        merged_headers: dict[str, str] = dict(headers or {})
        if json_body is not None:
            merged_headers.setdefault("Content-Type", "application/json")

        timeout = (timeout_ms if timeout_ms is not None else self.timeout_ms) / 1000
        result: TransportResult = self.transport(method.upper(), url, merged_headers, payload, timeout)
        coro = cast(Awaitable[HttpResponse], result) if inspect.isawaitable(result) else _immediate(result)
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{timeout_label} timed out after {int(timeout * 1000)}ms") from exc

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        return None


def create_client(*, timeout_ms: int = DEFAULT_TIMEOUT_MS, transport: RequestTransport | None = None) -> AsyncHttpClient:
    return AsyncHttpClient(timeout_ms=timeout_ms, transport=transport)


async def fetch_with_timeout(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    data: bytes | str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    timeout_label: str = "request",
    transport: RequestTransport | None = None,
) -> HttpResponse:
    client = create_client(timeout_ms=timeout_ms, transport=transport)
    return await client.request(
        method,
        url,
        headers=headers,
        json_body=json_body,
        data=data,
        timeout_ms=timeout_ms,
        timeout_label=timeout_label,
    )


async def _immediate(value: HttpResponse) -> HttpResponse:
    return value


def _normalize_payload(*, data: bytes | str | None, json_body: Any | None) -> bytes | None:
    if data is not None and json_body is not None:
        raise ValueError("Pass either data or json_body, not both")
    if isinstance(data, str):
        return data.encode("utf-8")
    if data is not None:
        return data
    if json_body is not None:
        return json.dumps(json_body).encode("utf-8")
    return None


async def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: bytes | None,
    timeout: float,
) -> HttpResponse:
    def do_request() -> HttpResponse:
        req = urllib_request.Request(url=url, method=method, data=payload, headers=dict(headers))
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw_headers = {key: value for key, value in response.headers.items()}
            return HttpResponse(status_code=response.getcode(), content=response.read(), headers=raw_headers)

    return await asyncio.to_thread(do_request)


def check_tailscale_ready() -> tuple[bool, str]:
    """Check if tailscale is installed, running, and authenticated."""
    if not shutil.which("tailscale"):
        return False, "not installed"

    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=TAILSCALE_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, "not installed"
    except subprocess.TimeoutExpired:
        return False, "status check timed out"

    if result.returncode != 0:
        return False, "daemon not running"
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "unable to parse status"
    if not status.get("Self", {}).get("Online"):
        return False, "not authenticated"
    return True, ""


def _subprocess_output(
    result: subprocess.CompletedProcess[str] | subprocess.CalledProcessError | subprocess.TimeoutExpired,
) -> str:
    streams = (
        getattr(result, "stderr", None),
        getattr(result, "stdout", None),
        getattr(result, "output", None),
    )
    for value in streams:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace").strip()
        elif isinstance(value, str):
            text = value.strip()
        else:
            text = ""
        if text:
            return text
    return ""


def get_tailscale_hostname() -> str:
    """Return the machine's stable *.ts.net FQDN."""
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=TAILSCALE_COMMAND_TIMEOUT_SECONDS,
    )
    status = json.loads(result.stdout)
    return status["Self"]["DNSName"].rstrip(".")


def get_tailscale_funnel_status() -> tuple[bool, str]:
    """Return the current Tailscale Funnel status text when available."""
    if not shutil.which("tailscale"):
        return False, "not installed"

    try:
        result = subprocess.run(
            ["tailscale", "funnel", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=TAILSCALE_STATUS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, "not installed"
    except subprocess.TimeoutExpired:
        return False, "funnel status check timed out"

    output = _subprocess_output(result)
    if result.returncode != 0:
        return False, output or "funnel status unavailable"
    return True, output or "not configured"


def tailscale_funnel_matches_port(status_text: str, hostname: str, port: int) -> bool:
    """Best-effort check that Funnel already proxies the public host to this port."""
    if not status_text.strip():
        return False
    normalized = status_text.lower()
    host = hostname.rstrip(".").lower()
    if host not in normalized:
        return False
    port_text = str(port)
    return (
        f":{port_text}" in normalized
        or f"127.0.0.1 {port_text}" in normalized
        or f"localhost {port_text}" in normalized
        or f" {port_text}" in normalized
    )


async def start_tunnel(port: int) -> str:
    """Enable Tailscale Funnel on the given port. Returns stable public URL."""
    hostname = get_tailscale_hostname()
    funnel_ready, funnel_status = get_tailscale_funnel_status()
    if funnel_ready and tailscale_funnel_matches_port(funnel_status, hostname, port):
        logger.info("tunnel.connected", provider="tailscale", url=f"https://{hostname}", reused=True)
        return f"https://{hostname}"

    def activate() -> None:
        try:
            subprocess.run(
                ["tailscale", "funnel", "--bg", "--yes", "--https=443", str(port)],
                check=True,
                capture_output=True,
                text=True,
                timeout=TAILSCALE_FUNNEL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"tailscale funnel activation timed out after {TAILSCALE_FUNNEL_TIMEOUT_SECONDS}s"
            raise TimeoutError(message) from exc
        except subprocess.CalledProcessError as exc:
            output = _subprocess_output(exc)
            message = "tailscale funnel activation failed"
            if output:
                message = f"{message}: {output}"
            raise RuntimeError(message) from exc

    await asyncio.to_thread(activate)
    logger.info("tunnel.connected", provider="tailscale", url=f"https://{hostname}")
    return f"https://{hostname}"


def stop_tunnel() -> None:
    """Disable Tailscale Funnel."""
    subprocess.run(["tailscale", "funnel", "off"], capture_output=True, check=False)
    logger.info("tunnel.stopped", provider="tailscale")


async def patch_twilio_phone_webhook(
    twilio_config: TwilioConfig,
    tunnel_url: str,
) -> None:
    from mystic.calls import update_phone_webhook

    if not twilio_config.phoneNumberSid:
        logger.warn("tunnel.twilio.patch.skip", reason="no phoneNumberSid")
        return

    voice_url = f"{tunnel_url.rstrip('/')}/webhook/twilio/voice"
    status_url = f"{tunnel_url.rstrip('/')}/webhook/twilio/status"
    await update_phone_webhook(
        twilio_config,
        twilio_config.phoneNumberSid,
        voice_url,
        status_url,
    )
    logger.info("tunnel.twilio.patched", voiceUrl=voice_url)
