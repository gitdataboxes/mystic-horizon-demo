"""LiveKit server lifecycle, room helpers, and Twilio media stream bridge."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import signal
import sys
import tarfile
import time
import threading
from array import array
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib import request as urllib_request

from aiohttp import ClientTimeout
from livekit import api, rtc

from mystic.audio import (
    CallRecorder,
    CHANNELS,
    FRAME_DURATION_MS,
    downsample_16k_to_8k,
    mulaw_decode,
    mulaw_encode,
    SAMPLE_RATE,
    upsample_8k_to_16k,
)
from mystic.config import LiveKitConfig, get_error_message, get_shared_home, logger
from mystic.db import now_ms

# ── binary lifecycle ──────────────────────────────────────────────────────────

LIVEKIT_VERSION = "1.7.2"
LIVEKIT_CHECKSUMS: dict[str, str] = {
    "linux_amd64": "7669b1a112449e71ff80cb82460dae7e526e92b3d81e15c70f66a030fac62f4a",
    "linux_arm64": "482ced7026cbf4c661ab262d04e2d1ba4a723a478bd87028cd27a8a4bcf38035",
}
LIVEKIT_STARTUP_TIMEOUT_MS = 10_000
LIVEKIT_STOP_TIMEOUT_S = 2.0
LIVEKIT_KILL_TIMEOUT_S = 1.0
LIVEKIT_HEALTH_POLL_MS = 200
LIVEKIT_TCP_TIMEOUT_MS = 300
_STDERR_TAIL_LIMIT = 4_000
_SEMVER_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")


@dataclass(slots=True)
class ListeningProcess:
    pid: int
    command: str


@dataclass(slots=True)
class ResolvedLiveKitBinary:
    path: Path
    version: str


def get_platform_system() -> str:
    return platform.system()


def get_binary_path() -> Path:
    return get_shared_home() / "bin" / "livekit-server"


def get_system_binary_path() -> Path | None:
    system_path = shutil.which("livekit-server")
    if not system_path:
        return None
    return Path(system_path)


def get_brew_path() -> Path | None:
    brew_path = shutil.which("brew")
    if not brew_path:
        return None
    return Path(brew_path)


def resolve_livekit_binary_path() -> Path | None:
    system_path = get_system_binary_path()
    if system_path is not None:
        return system_path

    managed_path = get_binary_path()
    if managed_path.exists():
        return managed_path
    return None


def get_livekit_missing_message() -> str:
    system = get_platform_system()
    if system == "Darwin":
        return (
            "livekit-server not found. Install LiveKit with Homebrew "
            "(brew install livekit) and ensure livekit-server is on PATH."
        )
    return (
        "livekit-server not found. Run 'mystic init' to install it, or install "
        "LiveKit separately and ensure livekit-server is on PATH."
    )


def _parse_semver(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.search(value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _version_output_suffix(output: str) -> str:
    text = output.strip()
    if not text:
        return ""
    compact = " ".join(text.split())
    return f": {compact}"


def get_livekit_binary_version(binary_path: Path) -> str:
    result = subprocess.run(
        [str(binary_path), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    version = _parse_semver(output)
    if result.returncode != 0 or version is None:
        raise RuntimeError(
            f"Unable to determine livekit-server version from {binary_path}"
            f"{_version_output_suffix(output)}"
        )
    return ".".join(str(part) for part in version)


def validate_livekit_version(version: str) -> None:
    current = _parse_semver(version)
    minimum = _parse_semver(LIVEKIT_VERSION)
    if current is None or minimum is None:
        raise RuntimeError(f"Invalid LiveKit version: {version}")
    if current[0] != minimum[0]:
        raise RuntimeError(
            f"Unsupported livekit-server version {version}. "
            f"Mystic Horizon currently supports LiveKit {minimum[0]}.x and "
            f"pins Linux to {LIVEKIT_VERSION}."
        )
    if current < minimum:
        raise RuntimeError(
            f"livekit-server version {version} is older than the minimum "
            f"supported version {LIVEKIT_VERSION}."
        )


def validate_livekit_binary(binary_path: Path) -> str:
    try:
        version = get_livekit_binary_version(binary_path)
        validate_livekit_version(version)
    except RuntimeError as exc:
        raise RuntimeError(f"Unsupported livekit-server at {binary_path}: {exc}") from exc
    return version


def is_managed_livekit_binary(binary_path: Path) -> bool:
    return binary_path == get_binary_path()


def resolve_supported_livekit_binary() -> ResolvedLiveKitBinary | None:
    binary_path = resolve_livekit_binary_path()
    if binary_path is None:
        return None
    version = validate_livekit_binary(binary_path)
    return ResolvedLiveKitBinary(path=binary_path, version=version)


def install_livekit_with_homebrew() -> None:
    brew_path = get_brew_path()
    if brew_path is None:
        raise RuntimeError(
            "Homebrew is required to install LiveKit on macOS. "
            "Install Homebrew from https://brew.sh and then run 'brew install livekit'."
        )
    try:
        subprocess.run([str(brew_path), "install", "livekit"], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Failed to install LiveKit with Homebrew. Try manually: brew install livekit") from exc


def find_brew_livekit_binary() -> Path | None:
    system_path = get_system_binary_path()
    if system_path is not None:
        return system_path

    brew_path = get_brew_path()
    if brew_path is None:
        return None

    result = subprocess.run(
        [str(brew_path), "--prefix", "livekit"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip()
    if not prefix:
        return None
    candidate = Path(prefix) / "bin" / "livekit-server"
    return candidate if candidate.exists() else None


def ensure_managed_livekit_symlink(source_path: Path) -> Path:
    managed_path = get_binary_path()
    if managed_path.is_dir():
        raise RuntimeError(f"Managed livekit-server path is a directory: {managed_path}")

    managed_path.parent.mkdir(parents=True, exist_ok=True)
    if managed_path.exists() or managed_path.is_symlink():
        try:
            if managed_path.samefile(source_path):
                return managed_path
        except OSError:
            pass
        managed_path.unlink()

    managed_path.symlink_to(source_path)
    return managed_path


def _ensure_macos_livekit_binary() -> str:
    source_path = get_system_binary_path() or get_binary_path()
    if not source_path.exists():
        install_livekit_with_homebrew()
        source_path = find_brew_livekit_binary() or get_binary_path()

    if not source_path.exists():
        raise RuntimeError(
            "Homebrew installed LiveKit but livekit-server was not found. "
            "Check that your Homebrew installation is healthy and try again."
        )

    version = validate_livekit_binary(source_path)
    managed_path = ensure_managed_livekit_symlink(source_path)
    logger.info("livekit.macos.ready", path=str(managed_path), version=version)
    return str(managed_path)


def get_platform_arch() -> tuple[str, str]:
    system = get_platform_system()
    machine = platform.machine().lower()
    os_name = "darwin" if system == "Darwin" else "linux"
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    return os_name, arch


def get_download_url() -> str:
    os_name, arch = get_platform_arch()
    return (
        "https://github.com/livekit/livekit/releases/download/"
        f"v{LIVEKIT_VERSION}/livekit_{LIVEKIT_VERSION}_{os_name}_{arch}.tar.gz"
    )


async def ensure_livekit_binary() -> str:
    system = get_platform_system()
    if system == "Darwin":
        return await asyncio.to_thread(_ensure_macos_livekit_binary)

    try:
        resolved_binary = await asyncio.to_thread(resolve_supported_livekit_binary)
    except RuntimeError as exc:
        resolved_path = resolve_livekit_binary_path()
        if system != "Linux" or resolved_path is None or not is_managed_livekit_binary(resolved_path):
            raise
        logger.warn("livekit.binary.invalid_managed", path=str(resolved_path), error=str(exc))
        if resolved_path.exists() or resolved_path.is_symlink():
            resolved_path.unlink()
        resolved_binary = None
    if resolved_binary is not None:
        logger.info("livekit.binary.ready", path=str(resolved_binary.path), version=resolved_binary.version)
        return str(resolved_binary.path)

    if system != "Linux":
        raise RuntimeError(
            "Automatic LiveKit installation is only supported on Linux. "
            "Install livekit-server manually and ensure it is on PATH."
        )

    binary_path = get_binary_path()
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    url = get_download_url()
    logger.info("livekit.downloading", url=url)
    await asyncio.to_thread(_download_binary, url, binary_path)
    version = await asyncio.to_thread(validate_livekit_binary, binary_path)
    logger.info("livekit.downloaded", path=str(binary_path), version=version)
    return str(binary_path)


def _verify_checksum(data: bytes) -> None:
    os_name, arch = get_platform_arch()
    key = f"{os_name}_{arch}"
    expected = LIVEKIT_CHECKSUMS.get(key)
    if expected is None:
        logger.warn("livekit.checksum.skipped", platform=key)
        return
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"Checksum mismatch for livekit-server {key}: "
            f"expected {expected}, got {actual}"
        )


def _download_binary(url: str, binary_path: Path) -> None:
    with urllib_request.urlopen(url, timeout=60) as response:
        status = getattr(response, "status", 200)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Failed to download livekit-server: {status}")
        archive_bytes = response.read()

    _verify_checksum(archive_bytes)

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        member = next(
            (
                candidate
                for candidate in archive.getmembers()
                if Path(candidate.name).name == "livekit-server" and candidate.isfile()
            ),
            None,
        )
        if member is None:
            raise RuntimeError("Downloaded archive did not contain livekit-server")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise RuntimeError("Failed to extract livekit-server from archive")
        binary_path.write_bytes(extracted.read())

    os.chmod(binary_path, 0o755)


def start_livekit_server(config: LiveKitConfig) -> subprocess.Popen[bytes]:
    existing = get_listening_process(config.port)
    if existing is not None:
        if "livekit-server" in existing.command:
            # Orphaned livekit-server from a previous run — kill it.
            logger.warn("livekit.server.orphan.killing", pid=existing.pid, port=config.port)
            try:
                os.kill(existing.pid, signal.SIGKILL)
            except OSError:
                pass
            for _ in range(20):
                if get_listening_process(config.port) is None:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    f"Failed to kill orphaned livekit-server (pid {existing.pid}) "
                    f"on {config.host}:{config.port}."
                )
        else:
            raise RuntimeError(
                f"Port {config.host}:{config.port} is already in use by pid {existing.pid} "
                f"({existing.command or 'unknown process'})."
            )

    resolved_binary = resolve_supported_livekit_binary()
    if resolved_binary is None:
        raise RuntimeError(get_livekit_missing_message())
    rtc_tcp_port = config.port + 1
    rtc_udp_port = config.port + 2
    args = [
        str(resolved_binary.path),
        "--dev",
        "--bind",
        config.host,
        "--port",
        str(config.port),
        "--rtc.tcp_port",
        str(rtc_tcp_port),
        "--udp-port",
        str(rtc_udp_port),
        "--keys",
        f"{config.apiKey.strip()}: {config.apiSecret.strip()}",
    ]

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _start_output_capture(proc)
    logger.info("livekit.server.started", host=config.host, port=config.port)
    return proc


def _start_output_capture(proc: LiveKitProcess) -> None:
    stream = proc.stdout or proc.stderr
    if stream is None:
        return

    output_tail = [""]
    setattr(proc, "_mystic_output_tail", output_tail)

    def drain() -> None:
        while True:
            chunk = stream.read(1024)
            if not chunk:
                break
            output_tail[0] = (output_tail[0] + chunk.decode("utf-8", errors="replace"))[
                -_STDERR_TAIL_LIMIT:
            ]

    threading.Thread(target=drain, daemon=True).start()


async def wait_for_livekit_server(
    config: LiveKitConfig,
    proc: LiveKitProcess,
    timeout_ms: int = LIVEKIT_STARTUP_TIMEOUT_MS,
) -> None:
    deadline = now_ms() + timeout_ms
    while now_ms() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            stderr_tail = _get_stderr_tail(proc)
            suffix = f": {stderr_tail}" if stderr_tail else ""
            raise RuntimeError(f"livekit-server exited before startup (code {exit_code}){suffix}")
        reachable = await asyncio.to_thread(
            is_tcp_reachable,
            config.host,
            config.port,
            LIVEKIT_TCP_TIMEOUT_MS,
        )
        if reachable:
            return
        await asyncio.sleep(LIVEKIT_HEALTH_POLL_MS / 1000)

    raise TimeoutError(f"Timed out waiting for livekit-server on {config.host}:{config.port}")


def is_tcp_reachable(host: str, port: int, timeout_ms: int) -> bool:
    timeout_seconds = timeout_ms / 1000
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def get_listening_process(port: int) -> ListeningProcess | None:
    try:
        lsof = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if lsof.returncode != 0 or not lsof.stdout.strip():
        return None

    pid_text = next((line.strip() for line in lsof.stdout.splitlines() if line.strip()), "")
    if not pid_text:
        return None
    try:
        pid = int(pid_text)
    except ValueError:
        return None

    ps = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return ListeningProcess(
        pid=pid,
        command=ps.stdout.strip() if ps.returncode == 0 else "",
    )


def stop_livekit_server(proc: LiveKitProcess) -> None:
    if proc.poll() is not None:
        return

    forced = False
    proc.terminate()
    try:
        proc.wait(timeout=LIVEKIT_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        forced = True
        proc.kill()
        try:
            proc.wait(timeout=LIVEKIT_KILL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warn("livekit.server.stop.timeout")
    logger.info("livekit.server.stopped", forced=forced)


def generate_livekit_keys() -> dict[str, str]:
    return {
        "apiKey": f"API{secrets.token_hex(8)}",
        "apiSecret": secrets.token_urlsafe(32),
    }


def _get_stderr_tail(proc: LiveKitProcess) -> str:
    tail = getattr(proc, "_mystic_output_tail", None)
    if not isinstance(tail, list) or not tail:
        return ""
    first_item = cast(object, tail[0])
    if not isinstance(first_item, str):
        return ""
    return first_item.strip()


# ── room helpers ──────────────────────────────────────────────────────────────

MYSTIC_HORIZON_AGENT_NAME = "mystic-horizon-agent"
LIVEKIT_OP_TIMEOUT_MS = 30_000


def _livekit_url(config: LiveKitConfig) -> str:
    return f"http://{config.host}:{config.port}"


@asynccontextmanager
async def _open_livekit_api(config: LiveKitConfig) -> AsyncIterator[api.LiveKitAPI]:
    client = api.LiveKitAPI(
        _livekit_url(config),
        config.apiKey,
        config.apiSecret,
        timeout=ClientTimeout(total=LIVEKIT_OP_TIMEOUT_MS / 1000),
    )
    try:
        yield client
    finally:
        await client.aclose()


async def _with_timeout(awaitable: Any, label: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=LIVEKIT_OP_TIMEOUT_MS / 1000)
    except TimeoutError as exc:
        raise TimeoutError(f"{label} timed out after {LIVEKIT_OP_TIMEOUT_MS}ms") from exc


async def _create_agent_dispatch(
    dispatch_service: Any,
    room_name: str,
) -> str:
    request = api.CreateAgentDispatchRequest(
        agent_name=MYSTIC_HORIZON_AGENT_NAME,
        room=room_name,
    )
    try:
        created = await _with_timeout(dispatch_service.create_dispatch(request), "createDispatch")
    except Exception as exc:
        message = str(exc).lower()
        if "already exists" in message:
            logger.debug(
                "livekit.agent.dispatch.exists",
                room=room_name,
                agentName=MYSTIC_HORIZON_AGENT_NAME,
            )
            return "exists"
        raise

    logger.info(
        "livekit.agent.dispatched",
        room=room_name,
        agentName=MYSTIC_HORIZON_AGENT_NAME,
        jobs=_count_dispatch_jobs([created]),
    )
    return "created"


async def _count_assigned_jobs(dispatch_service: Any, room_name: str) -> int:
    dispatches = await _with_timeout(dispatch_service.list_dispatch(room_name), "listDispatch")
    return _count_dispatch_jobs(dispatches)


def _count_dispatch_jobs(dispatches: object) -> int:
    if not isinstance(dispatches, list):
        return 0
    jobs = 0
    for dispatch in dispatches:
        state = getattr(dispatch, "state", None)
        if state is None:
            continue
        state_jobs = getattr(state, "jobs", None)
        if state_jobs is None:
            continue
        jobs += len(state_jobs)
    return jobs


async def _wait_for_dispatch_assignment(
    dispatch_service: Any,
    room_name: str,
    timeout_ms: int,
    poll_ms: int,
) -> int:
    if timeout_ms <= 0:
        return 0

    deadline = now_ms() + timeout_ms
    last_jobs = -1
    while now_ms() < deadline:
        jobs = await _count_assigned_jobs(dispatch_service, room_name)
        if jobs != last_jobs:
            logger.debug("livekit.agent.dispatch.jobs", room=room_name, jobs=jobs)
            last_jobs = jobs
        if jobs > 0:
            return jobs
        await asyncio.sleep(poll_ms / 1000)
    return 0


async def verify_dispatch_assignment(
    config: LiveKitConfig,
    room_name: str,
    timeout_ms: int = 5_000,
    poll_ms: int = 500,
) -> bool:
    """Check whether the agent dispatch for *room_name* has been assigned."""
    async with _open_livekit_api(config) as client:
        jobs = await _wait_for_dispatch_assignment(
            client.agent_dispatch,
            room_name,
            timeout_ms,
            poll_ms,
        )
    return jobs > 0


async def create_room(
    config: LiveKitConfig,
    call_id: str,
    metadata: Mapping[str, object] | None = None,
) -> str:
    room_name = f"call-{call_id}"
    await create_named_room(config, room_name, metadata)
    return room_name


async def create_named_room(
    config: LiveKitConfig,
    room_name: str,
    metadata: Mapping[str, object] | None = None,
    *,
    empty_timeout: int = 300,
    max_participants: int = 3,
) -> str:
    request = api.CreateRoomRequest(
        name=room_name,
        metadata=json.dumps(dict(metadata)) if metadata else "",
        empty_timeout=empty_timeout,
        max_participants=max_participants,
    )

    async with _open_livekit_api(config) as client:
        await _with_timeout(client.room.create_room(request), "createRoom")
        await _create_agent_dispatch(client.agent_dispatch, room_name)

    logger.info("livekit.room.created", room=room_name)
    return room_name


async def dispatch_agent_to_room(
    config: LiveKitConfig,
    call_id: str,
    *,
    wait_for_assignment_ms: int = 8_000,
    poll_ms: int = 500,
    create_attempts: int = 3,
    create_if_missing: bool = True,
    require_assignment: bool = False,
) -> str:
    room_name = f"call-{call_id}"
    return await dispatch_agent_to_named_room(
        config,
        room_name,
        wait_for_assignment_ms=wait_for_assignment_ms,
        poll_ms=poll_ms,
        create_attempts=create_attempts,
        create_if_missing=create_if_missing,
        require_assignment=require_assignment,
    )


async def dispatch_agent_to_named_room(
    config: LiveKitConfig,
    room_name: str,
    *,
    wait_for_assignment_ms: int = 8_000,
    poll_ms: int = 500,
    create_attempts: int = 3,
    create_if_missing: bool = True,
    require_assignment: bool = False,
) -> str:

    async with _open_livekit_api(config) as client:
        dispatch_service = client.agent_dispatch

        if not create_if_missing:
            assigned_jobs = await _wait_for_dispatch_assignment(
                dispatch_service,
                room_name,
                wait_for_assignment_ms,
                poll_ms,
            )
            if assigned_jobs > 0:
                logger.info(
                    "livekit.agent.dispatch.assigned",
                    room=room_name,
                    assignedJobs=assigned_jobs,
                    attempt=0,
                )
            else:
                logger.warn(
                    "livekit.agent.dispatch.unassigned",
                    room=room_name,
                    createAttempts=0,
                    waitForAssignmentMs=wait_for_assignment_ms,
                )
                if require_assignment:
                    raise RuntimeError(f"Agent dispatch did not assign a worker to room '{room_name}'")
            return "exists"

        last_status = "exists"
        for attempt in range(1, create_attempts + 1):
            existing_jobs = await _count_assigned_jobs(dispatch_service, room_name)
            if existing_jobs > 0:
                logger.info(
                    "livekit.agent.dispatch.assigned",
                    room=room_name,
                    assignedJobs=existing_jobs,
                    attempt=attempt,
                )
                return "exists"

            last_status = await _create_agent_dispatch(dispatch_service, room_name)
            assigned_jobs = await _wait_for_dispatch_assignment(
                dispatch_service,
                room_name,
                wait_for_assignment_ms,
                poll_ms,
            )
            if assigned_jobs > 0:
                logger.info(
                    "livekit.agent.dispatch.assigned",
                    room=room_name,
                    assignedJobs=assigned_jobs,
                    attempt=attempt,
                )
                return last_status

            if attempt < create_attempts:
                logger.warn(
                    "livekit.agent.dispatch.retry",
                    room=room_name,
                    attempt=attempt,
                    createAttempts=create_attempts,
                )

    logger.warn(
        "livekit.agent.dispatch.unassigned",
        room=room_name,
        createAttempts=create_attempts,
        waitForAssignmentMs=wait_for_assignment_ms,
    )
    if require_assignment:
        raise RuntimeError(f"Agent dispatch did not assign a worker to room '{room_name}'")
    return last_status


async def room_has_active_agent(
    config: LiveKitConfig,
    room_name: str,
) -> bool:
    """Return True if *room_name* currently has a dispatched agent worker."""
    try:
        async with _open_livekit_api(config) as client:
            jobs = await _count_assigned_jobs(client.agent_dispatch, room_name)
        return jobs > 0
    except Exception:
        return False


async def delete_room(
    config: LiveKitConfig,
    room_name: str,
) -> None:
    request = api.DeleteRoomRequest(room=room_name)
    async with _open_livekit_api(config) as client:
        await _with_timeout(client.room.delete_room(request), "deleteRoom")
    logger.debug("livekit.room.deleted", room=room_name)


async def generate_token(
    config: LiveKitConfig,
    room_name: str,
    participant_name: str,
    metadata: Mapping[str, object] | None = None,
) -> str:
    token = api.AccessToken(config.apiKey, config.apiSecret).with_identity(participant_name)
    if metadata:
        token = token.with_metadata(json.dumps(dict(metadata)))
    token = token.with_grants(
        api.VideoGrants(
            room=room_name,
            room_join=True,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
    )
    return token.to_jwt()


# ── audio bridge ──────────────────────────────────────────────────────────────

MAX_PENDING_TWILIO_FRAMES = 100


class TwilioWebSocket(Protocol):
    @property
    def closed(self) -> bool: ...

    def send_str(self, data: str) -> object: ...


class LiveKitProcess(Protocol):
    stdout: Any | None
    stderr: Any | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class AudioBridge:
    def __init__(
        self,
        twilio_ws: TwilioWebSocket,
        livekit_config: LiveKitConfig,
        *,
        call_id: str | None = None,
        recorder: CallRecorder | None = None,
    ) -> None:
        self._twilio_ws = twilio_ws
        self._livekit_config = livekit_config
        self._call_id = call_id
        self._recorder = recorder
        self._stream_sid: str | None = None
        self._room: rtc.Room | None = None
        self._audio_source: rtc.AudioSource | None = None
        self._local_track_sid: str | None = None
        self._pending_frames: deque[list[int]] = deque()
        self._drain_task: asyncio.Task[None] | None = None
        self._track_tasks: set[asyncio.Task[None]] = set()
        self._connect_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._dropped_frames = 0

    async def start(self) -> None:
        if self._call_id is not None:
            await self._ensure_livekit_connected()
        else:
            logger.debug("bridge.waiting-for-call-id")

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_impl())
        await self._stop_task

    async def handle_twilio_message(self, raw_message: str) -> None:
        if self._stopped:
            return

        try:
            raw_payload = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            logger.error(
                "bridge.twilio.parse.error",
                callId=self._call_id,
                error=get_error_message(exc),
            )
            return

        if not isinstance(raw_payload, dict):
            logger.error("bridge.twilio.parse.error", callId=self._call_id, error="message is not an object")
            return
        message = cast(dict[str, object], raw_payload)

        event = message.get("event")
        if event == "connected":
            logger.debug("bridge.twilio.connected", callId=self._call_id)
            return

        try:
            if event == "start":
                await self._handle_start_event(message)
                return

            if event == "media":
                await self._handle_media_event(message)
                return

            if event == "stop":
                logger.info("bridge.twilio.stream.stopped", callId=self._call_id)
                await self.stop()
        except Exception as exc:
            logger.error(
                "bridge.twilio.parse.error",
                callId=self._call_id,
                error=get_error_message(exc),
            )

    async def _handle_start_event(self, message: dict[str, object]) -> None:
        start = message.get("start")
        if not isinstance(start, dict):
            raise ValueError("Twilio start event missing start payload")
        start_payload = cast(dict[str, object], start)

        stream_sid = start_payload.get("streamSid")
        if isinstance(stream_sid, str) and stream_sid:
            self._stream_sid = stream_sid

        if self._call_id is None:
            custom_parameters = start_payload.get("customParameters")
            if isinstance(custom_parameters, dict):
                custom_payload = cast(dict[str, object], custom_parameters)
                call_id = custom_payload.get("callId")
                if isinstance(call_id, str) and call_id:
                    self._call_id = call_id

        logger.info(
            "bridge.twilio.stream.started",
            callId=self._call_id,
            streamSid=self._stream_sid,
        )
        await self._ensure_livekit_connected()

    async def _handle_media_event(self, message: dict[str, object]) -> None:
        media = message.get("media")
        if not isinstance(media, dict):
            return
        media_payload = cast(dict[str, object], media)

        payload = media_payload.get("payload")
        if not isinstance(payload, str) or not payload:
            return

        pcm8k = mulaw_decode(base64.b64decode(payload))
        pcm16k = upsample_8k_to_16k(pcm8k)
        self._publish_to_livekit(pcm16k)
        if self._recorder is not None:
            await self._recorder.write_caller(_samples_to_pcm16le(pcm16k))

    def _publish_to_livekit(self, pcm16k: list[int]) -> None:
        if self._stopped:
            return

        if len(self._pending_frames) >= MAX_PENDING_TWILIO_FRAMES:
            self._pending_frames.popleft()
            self._dropped_frames += 1
            if self._dropped_frames == 1 or self._dropped_frames % 50 == 0:
                logger.warn(
                    "bridge.livekit.capture.backpressure",
                    callId=self._call_id,
                    pending=len(self._pending_frames),
                    dropped=self._dropped_frames,
                )

        self._pending_frames.append(pcm16k)
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_pending_frames())

    async def _drain_pending_frames(self) -> None:
        while not self._stopped and self._pending_frames:
            audio_source = self._audio_source
            if audio_source is None:
                return

            pcm16k = self._pending_frames.popleft()
            try:
                await audio_source.capture_frame(
                    rtc.AudioFrame(
                        _samples_to_pcm16le(pcm16k),
                        16_000,
                        1,
                        len(pcm16k),
                    )
                )
            except Exception as exc:
                logger.warn(
                    "bridge.livekit.capture.error",
                    callId=self._call_id,
                    error=get_error_message(exc),
                )

    async def _ensure_livekit_connected(self) -> None:
        if self._stopped or self._room is not None:
            return
        if self._call_id is None:
            raise ValueError("Missing callId in Twilio start event")
        if self._connect_task is not None:
            await self._connect_task
            return

        self._connect_task = asyncio.create_task(self._connect_livekit())
        try:
            await self._connect_task
        finally:
            self._connect_task = None

    async def _connect_livekit(self) -> None:
        assert self._call_id is not None

        room_name = f"call-{self._call_id}"
        participant_name = f"bridge-{self._call_id}"
        token = await generate_token(self._livekit_config, room_name, participant_name)
        ws_url = f"ws://{self._livekit_config.host}:{self._livekit_config.port}"

        room = rtc.Room()
        cast(Any, room).on("track_subscribed", self._on_track_subscribed)
        cast(Any, room).on("disconnected", self._on_room_disconnected)
        await room.connect(ws_url, token)

        audio_source = rtc.AudioSource(16_000, 1)
        local_track = rtc.LocalAudioTrack.create_audio_track(f"twilio-{self._call_id}", audio_source)
        publish_options = rtc.TrackPublishOptions()
        publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE
        publication = await room.local_participant.publish_track(local_track, publish_options)

        self._room = room
        self._audio_source = audio_source
        self._local_track_sid = publication.sid

        if self._pending_frames:
            self._drain_task = asyncio.create_task(self._drain_pending_frames())

        try:
            await dispatch_agent_to_room(self._livekit_config, self._call_id)
        except Exception as exc:
            logger.warn(
                "bridge.livekit.dispatch.error",
                callId=self._call_id,
                error=get_error_message(exc),
            )

        logger.info("bridge.livekit.connected", callId=self._call_id, room=room_name)

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        _publication: object,
        _participant: object,
    ) -> None:
        if not isinstance(track, rtc.RemoteAudioTrack):
            return

        task = asyncio.create_task(self._forward_livekit_track(track))
        self._track_tasks.add(task)

        def _done(done: asyncio.Task[None]) -> None:
            self._track_tasks.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None and not self._stopped:
                logger.warn(
                    "bridge.livekit.forward.error",
                    callId=self._call_id,
                    trackSid=track.sid,
                    error=get_error_message(exc),
                )

        task.add_done_callback(_done)
        logger.debug("bridge.livekit.track.subscribed", callId=self._call_id, trackSid=track.sid)

    def _on_room_disconnected(self, *_args: object) -> None:
        if not self._stopped:
            asyncio.create_task(self.stop())

    async def _forward_livekit_track(self, track: rtc.RemoteAudioTrack) -> None:
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=16_000,
            num_channels=1,
            frame_size_ms=20,
        )
        try:
            async for event in stream:
                await self._send_to_twilio(event.frame)
        finally:
            await stream.aclose()

    async def _send_to_twilio(self, frame: rtc.AudioFrame) -> None:
        if self._stopped or self._stream_sid is None or _ws_is_closed(self._twilio_ws):
            return

        frame_bytes = bytes(frame.data)
        if self._recorder is not None:
            await self._recorder.write_agent(frame_bytes)
        pcm16k = _pcm16le_to_samples(frame_bytes)
        pcm8k = downsample_16k_to_8k(pcm16k)
        payload = base64.b64encode(mulaw_encode(pcm8k)).decode("utf-8")
        await _ws_send_text(
            self._twilio_ws,
            json.dumps(
                {
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {"payload": payload},
                }
            ),
        )

    async def _stop_impl(self) -> None:
        if self._stopped:
            return

        self._stopped = True
        drain_task = self._drain_task
        self._drain_task = None
        if drain_task is not None:
            drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await drain_task

        track_tasks = list(self._track_tasks)
        self._track_tasks.clear()
        for task in track_tasks:
            task.cancel()
        await asyncio.gather(*track_tasks, return_exceptions=True)

        room = self._room
        audio_source = self._audio_source
        local_track_sid = self._local_track_sid

        self._room = None
        self._audio_source = None
        self._local_track_sid = None
        self._pending_frames.clear()

        if self._recorder is not None:
            await self._recorder.stop()
        if room is not None and local_track_sid is not None:
            with suppress(Exception):
                await room.local_participant.unpublish_track(local_track_sid)
        if audio_source is not None:
            with suppress(Exception):
                await audio_source.aclose()
        if room is not None:
            with suppress(Exception):
                await room.disconnect()

        logger.info("bridge.stopped", callId=self._call_id)


def create_audio_bridge(
    twilio_ws: TwilioWebSocket,
    *,
    livekit_config: LiveKitConfig,
    call_id: str | None = None,
    recorder: CallRecorder | None = None,
) -> AudioBridge:
    return AudioBridge(twilio_ws, livekit_config, call_id=call_id, recorder=recorder)



_TRANSCRIPT_ENTRY_RE = re.compile(
    r"^\[(?P<minutes>\d+):(?P<seconds>\d{2})\]\s+"
    r"(?P<label>Agent|Caller|Tool)(?:\s+\[(?P<modality>text|event)\])?:\s*(?P<text>.+)$"
)


def parse_transcript_entries(transcript_text: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw_line in transcript_text.split("\n"):
        line = raw_line.rstrip("\r")
        match = _TRANSCRIPT_ENTRY_RE.match(line.strip())
        if match is None:
            if current is not None:
                text = str(current.get("text", ""))
                current["text"] = text + ("\n" if not line else f"\n{line}")
            continue
        label = match.group("label")
        if label == "Tool":
            current = None
            event = _parse_transcript_tool_event(match.group("text"))
            if event is not None:
                entries.append(event)
            continue
        speaker = "agent" if label == "Agent" else "user"
        current = {
            "speaker": speaker,
            "text": match.group("text").strip(),
            "modality": match.group("modality") or "voice",
        }
        entries.append(current)
    return entries


def _parse_transcript_tool_event(text: str) -> dict[str, object] | None:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    event_type = raw.get("type")
    if event_type not in {"tool_started", "tool_completed"}:
        return None

    name = raw.get("name")
    event: dict[str, object] = {
        "type": event_type,
        "name": str(name).strip() if isinstance(name, str) and name.strip() else "tool",
    }
    args_summary = raw.get("args_summary")
    if isinstance(args_summary, str) and args_summary.strip():
        event["args_summary"] = args_summary.strip()
    duration_ms = raw.get("duration_ms")
    if isinstance(duration_ms, int | float):
        event["duration_ms"] = max(0, int(duration_ms))
    error = raw.get("error")
    if isinstance(error, bool):
        event["error"] = error
    return event


def _samples_to_pcm16le(samples: list[int]) -> bytes:
    pcm = array("h", samples)
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def _pcm16le_to_samples(data: bytes) -> list[int]:
    pcm = array("h")
    pcm.frombytes(data[: len(data) - (len(data) % 2)])
    if sys.byteorder != "little":
        pcm.byteswap()
    return list(pcm)


async def _ws_send_text(ws: TwilioWebSocket, data: str) -> None:
    if hasattr(ws, "send_str"):
        result = ws.send_str(data)
    elif hasattr(ws, "send_json"):
        result = getattr(ws, "send_json")(json.loads(data))
    elif hasattr(ws, "send"):
        result = getattr(ws, "send")(data)
    else:
        raise TypeError("Unsupported WebSocket type for Twilio bridge")

    if inspect.isawaitable(result):
        await result


def _ws_is_closed(ws: TwilioWebSocket) -> bool:
    closed = getattr(ws, "closed", False)
    return bool(closed)
