"""CLI entrypoint and all commands."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import warnings
import webbrowser
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, cast

import click

from mystic.calls import (
    DEFAULT_VOICE_ID,
    POCKET_VOICE_CATALOG,
    VOICE_CATALOG,
    buy_phone_number,
    initiate_outbound_call,
    send_sms,
    search_available_numbers,
)
from mystic.config import (
    DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    OAuthTokens,
    TwilioConfig,
    bind_trace_id,
    clear_config_cache,
    config_exists,
    ensure_dashboard_token,
    ensure_log_dir,
    ensure_python_extra,
    get_agent_config,
    get_daemon_socket_path,
    get_dashboard_config,
    get_error_message,
    get_home,
    get_log_path,
    get_identity_path,
    get_journal_dir,
    get_providers_config,
    get_setup_status,
    get_shared_home,
    get_soul_path,
    identity_exists,
    is_python_package_available,
    is_valid_e164,
    list_dashboard_files,
    list_journal_entries,
    load_config_fresh,
    logger,
    silence_stdout,
    read_dashboard_file,
    read_identity_raw,
    read_soul,
    resolve_agent_home,
    save_hub_tokens,
    soul_exists,
    validate_agent_name,
    validate_e164,
    write_config,
)
from mystic.db import (
    close_database,
    create_migration as create_migration_file,
    get_action_by_id,
    get_actions_by_status,
    get_all_active_facts_by_person,
    get_all_faq_chunks,
    get_applied_migrations,
    get_all_pending_actions,
    get_all_people,
    get_call_by_id,
    get_external_event_by_id,
    get_external_events_in_range,
    get_failed_actions,
    get_person_by_id,
    get_recent_calls,
    get_recent_calls_by_person,
    get_recent_external_events,
    get_schema_version,
    get_todays_calls,
    initialize_schema,
    insert_action,
    insert_fact,
    list_active_calls,
    now_ms,
    open_database,
    search_facts,
    upsert_person,
    write_builtin_migrations,
)
from mystic.actions import send_email
from mystic.livekit import ensure_livekit_binary, generate_livekit_keys, get_livekit_missing_message, resolve_supported_livekit_binary
from mystic.embedding import embedding_model_missing, ensure_local_model
from mystic.http import check_tailscale_ready, fetch_with_timeout
from mystic.interactions import describe_call, interaction_payload
from mystic.ink import (
    bold,
    box,
    dim,
    error_msg,
    format_phone,
    green,
    section,
    yellow,
)
from mystic.phone import CapabilityReadiness, PhoneReadiness, ensure_phone_line_ready
from mystic.runtime import Runtime, start_dev as _start_dev, start_full as _start_full, stop as _stop_runtime
from mystic.audio import start_call_recorder
from mystic.server import start_server
from mystic.types import ActionUrgency, Audience, Call, CallState, Direction, FactType
from mystic.voice import (
    ensure_pocket_onnx_models,
    pocket_onnx_models_missing,
)
from mystic.web import (
    _llm_setup_required,
    _voice_readiness,
    create_setup_app,
    run_owner_chat,
    seed_dashboard_defaults,
    set_setup_db,
    set_setup_done_event,
    set_setup_runtime,
    set_setup_server,
)


# ── runtime state helpers ──

PID_FILENAME = "mystic-horizon.pid"
RUNTIME_STATE_FILENAME = "mystic-horizon.runtime.json"
DEFAULT_SETUP_FLOW_TIMEOUT_SECONDS = 30 * 60


@dataclass(slots=True)
class RuntimeStateRecord:
    pid: int
    port: int
    tunnel_url: str
    mode: str
    started_at: int


def get_pid_path() -> Path:
    return get_home() / PID_FILENAME


def get_runtime_state_path() -> Path:
    return get_home() / RUNTIME_STATE_FILENAME


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_pid() -> int | None:
    path = get_pid_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_pid(pid: int) -> None:
    path = get_pid_path()
    path.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid_file() -> None:
    _unlink_if_exists(get_pid_path())


def read_runtime_state() -> RuntimeStateRecord | None:
    path = get_runtime_state_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _coerce_runtime_state(payload)


def write_runtime_state(record: RuntimeStateRecord) -> None:
    path = get_runtime_state_path()
    path.write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8")


def clear_runtime_state() -> None:
    _unlink_if_exists(get_runtime_state_path())


def cleanup_runtime_files() -> None:
    remove_pid_file()
    clear_runtime_state()
    _unlink_if_exists(get_daemon_socket_path())


def _coerce_runtime_state(payload: dict[str, Any]) -> RuntimeStateRecord | None:
    try:
        pid = int(payload["pid"])
        port = int(payload["port"])
        tunnel_url = str(payload.get("tunnel_url", ""))
        mode = str(payload.get("mode", ""))
        started_at = int(payload["started_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return RuntimeStateRecord(pid=pid, port=port, tunnel_url=tunnel_url, mode=mode, started_at=started_at)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return


async def _send_daemon_command(
    command: Mapping[str, object],
    *,
    home: Path | None = None,
    timeout_ms: int = 5_000,
) -> dict[str, object] | None:
    socket_path = (home or get_home()) / get_daemon_socket_path().name
    if not socket_path.exists():
        return None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)),
            timeout=timeout_ms / 1000,
        )
    except (asyncio.TimeoutError, OSError):
        _unlink_if_exists(socket_path)
        return None

    try:
        writer.write(json.dumps(command).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout_ms / 1000)
        if not raw:
            return None
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (asyncio.TimeoutError, OSError, json.JSONDecodeError):
        _unlink_if_exists(socket_path)
        return None
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


def probe_daemon(home: Path | None = None) -> dict[str, object] | None:
    return asyncio.run(_send_daemon_command({"cmd": "health"}, home=home))


# ── init command ──

SEEDS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "seeds"
FAQ_SEEDS_DIR = Path(__file__).resolve().parents[1] / "faq"
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_REALTIME_MODEL = "openai/gpt-5.5"
DEFAULT_BACKEND_MODEL = "openai/gpt-5.5"
DEFAULT_SEARCH_MODEL = "perplexity/sonar-pro"
DEFAULT_SETUP_VOICE_ID = "Olivia"
DEFAULT_SERVER_PORT = 3000
DEFAULT_LIVEKIT_PORT = 7880
DEFAULT_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
VOICE_DESCRIPTIONS: dict[str, str] = {
    "Mark": "warm, professional male",
    "Clive": "deep, authoritative male",
    "Hades": "rich, resonant male",
    "Olivia": "bright, friendly female",
    "Pippa": "clear, energetic female",
    "Orietta": "smooth, composed female",
}
TURN_DETECTOR_MODULE = "livekit.plugins.turn_detector"
TURN_DETECTOR_PIP_PACKAGE = "livekit-plugins-turn-detector"
TURN_DETECTOR_REMOTE_ENV = "LIVEKIT_REMOTE_EOT_URL"


def _pick(label: str, options: list[tuple[str, str]], default: int = 1) -> str:
    """Numbered menu. Returns the value (first element) of the selected option."""
    click.echo(f"  {label}:")
    for i, (_, description) in enumerate(options, 1):
        click.echo(f"    {dim(f'{i}.')} {description}")
    choice = click.prompt(
        "  Choose",
        default=default,
        type=click.IntRange(1, len(options)),
        show_default=True,
    )
    return options[choice - 1][0]


@dataclass(slots=True)
class UsedPorts:
    server_ports: set[int]
    livekit_ports: set[int]


@dataclass(slots=True)
class InitSelections:
    timezone: str
    selected_voice_id: str
    server_port: int
    livekit_port: int
    tts_config: dict[str, object]
    stt_config: dict[str, object]
    embedding_config: dict[str, object]
    llm_realtime: dict[str, object]
    llm_backend: dict[str, object]
    openrouter_key: str | None
    owner_phone: str | None = None


def _default_local_embedding_config() -> dict[str, object]:
    return {
        "provider": "local",
        "model": DEFAULT_LOCAL_EMBEDDING_MODEL,
        "dimensions": DEFAULT_LOCAL_EMBEDDING_DIMENSIONS,
    }


def list_agent_dirs(root: str | Path | None = None, exclude: str | None = None) -> list[str]:
    base_dir = Path(root) if root is not None else get_shared_home()
    if not base_dir.exists():
        return []
    entries: list[str] = []
    for entry in sorted(base_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        if entry.name == "bin" or entry.name == exclude:
            continue
        entries.append(entry.name)
    return entries


def discover_siblings(current_agent: str, root: str | Path | None = None) -> list[str]:
    base_dir = Path(root) if root is not None else get_shared_home()
    candidates: list[tuple[float, str]] = []
    for name in list_agent_dirs(base_dir, exclude=current_agent):
        providers_path = base_dir / name / "config" / "providers.json"
        if not providers_path.exists():
            continue
        try:
            candidates.append((providers_path.stat().st_mtime, name))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [name for _, name in candidates]


def extract_sibling_keys(sibling_name: str, root: str | Path | None = None) -> dict[str, str]:
    base_dir = Path(root) if root is not None else get_shared_home()
    providers_path = base_dir / sibling_name / "config" / "providers.json"
    try:
        payload = json.loads(providers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    result: dict[str, str] = {}
    twilio = payload.get("twilio")
    if not isinstance(twilio, dict):
        twilio = payload.get("twilioDraft")
    if isinstance(twilio, dict):
        account_sid = twilio.get("accountSid")
        auth_token = twilio.get("authToken")
        phone_number = twilio.get("phoneNumber")
        phone_number_sid = twilio.get("phoneNumberSid")
        if isinstance(account_sid, str) and account_sid:
            result["twilioSid"] = account_sid
        if isinstance(auth_token, str) and auth_token:
            result["twilioToken"] = auth_token
        if isinstance(phone_number, str) and phone_number:
            result["twilioPhone"] = phone_number
        if isinstance(phone_number_sid, str) and phone_number_sid:
            result["twilioPhoneNumberSid"] = phone_number_sid

    openrouter = payload.get("openrouter")
    if isinstance(openrouter, dict) and isinstance(openrouter.get("apiKey"), str):
        result["openrouter"] = str(openrouter["apiKey"])

    stt = payload.get("stt")
    if isinstance(stt, dict) and stt.get("provider") == "deepgram" and isinstance(stt.get("apiKey"), str):
        result["deepgram"] = str(stt["apiKey"])

    tts = payload.get("tts")
    if isinstance(tts, dict) and isinstance(tts.get("apiKey"), str):
        result["inworld"] = str(tts["apiKey"])

    smtp = payload.get("smtp")
    if isinstance(smtp, dict):
        for field, key in (
            ("host", "smtpHost"), ("port", "smtpPort"), ("username", "smtpUsername"),
            ("password", "smtpPassword"), ("fromAddress", "smtpFrom"), ("useTls", "smtpUseTls"),
        ):
            val = smtp.get(field)
            if val is not None and str(val):
                result[key] = str(val)

    return result


def discover_used_ports(current_agent: str, root: str | Path | None = None) -> UsedPorts:
    base_dir = Path(root) if root is not None else get_shared_home()
    server_ports: set[int] = set()
    livekit_ports: set[int] = set()
    for name in list_agent_dirs(base_dir, exclude=current_agent):
        agent_path = base_dir / name / "config" / "agent.json"
        providers_path = base_dir / name / "config" / "providers.json"
        try:
            if agent_path.exists():
                agent_payload = json.loads(agent_path.read_text(encoding="utf-8"))
                server_value = agent_payload.get("server", {}).get("port")
                if isinstance(server_value, int):
                    server_ports.add(server_value)
            if providers_path.exists():
                providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
                livekit_value = providers_payload.get("livekit", {}).get("port")
                if isinstance(livekit_value, int):
                    # Reserve HTTP port + RTC TCP (port+1) + RTC UDP (port+2)
                    livekit_ports.update(range(livekit_value, livekit_value + 3))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return UsedPorts(server_ports=server_ports, livekit_ports=livekit_ports)


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def allocate_port(base: int, used: set[int], *also_avoid: int, stride: int = 1) -> int:
    blocked = set(used)
    blocked.update(also_avoid)
    port = base
    while any(p in blocked or _port_in_use(p) for p in range(port, port + stride)):
        port += 1
    return port


def seed_prompt_files(home: Path) -> None:
    required = {
        "shared-context.md": home / "prompts" / "shared" / "context.md",
        "owner-briefing.md": home / "prompts" / "owner" / "briefing.md",
        "public-workflow.md": home / "prompts" / "public" / "workflow.md",
    }
    for source_name, destination in required.items():
        source = SEEDS_DIR / source_name
        if not source.exists():
            raise FileNotFoundError(f"Missing seed file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    faq_dest = home / "faq"
    faq_dest.mkdir(parents=True, exist_ok=True)
    if FAQ_SEEDS_DIR.is_dir():
        for src in sorted(FAQ_SEEDS_DIR.glob("*.md")):
            dst = faq_dest / src.name
            if not dst.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    write_builtin_migrations()



def write_config_files(home: Path, selections: InitSelections) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    for path in (
        home / "config",
        home / "logs",
        home / "prompts" / "shared",
        home / "prompts" / "owner",
        home / "prompts" / "public",
        home / "faq",
        home / "migrations",
    ):
        path.mkdir(parents=True, exist_ok=True)

    livekit_keys = generate_livekit_keys()
    agent_config: dict[str, object] = {
        "agent": {"name": "Agent", "voiceId": selections.selected_voice_id},
        "hours": {"start": 9, "end": 17, "timezone": selections.timezone, "days": DEFAULT_DAYS},
        "server": {
            "port": selections.server_port,
            "maxActiveJobs": 1 if selections.tts_config.get("provider") == "pocket" else 10,
        },
        "tunnel": {"enabled": True},
    }
    if selections.owner_phone:
        agent_config["owner"] = {"phone": selections.owner_phone}
    providers_config: dict[str, object] = {
        "livekit": {
            "host": "127.0.0.1",
            "port": selections.livekit_port,
            "apiKey": livekit_keys["apiKey"],
            "apiSecret": livekit_keys["apiSecret"],
        },
        "stt": selections.stt_config,
        "tts": selections.tts_config,
        "embedding": selections.embedding_config,
        "llm": {"realtime": selections.llm_realtime, "backend": selections.llm_backend},
    }
    if selections.openrouter_key:
        providers_config["openrouter"] = {"apiKey": selections.openrouter_key}

    backend_model = str(selections.llm_backend.get("model") or DEFAULT_BACKEND_MODEL)
    intelligence_config: dict[str, object] = {
        "extraction": {
            "facts": {"model": backend_model},
            "commitments": {"model": backend_model},
        },
        "judgment": {
            "scheduler": {"model": backend_model},
            "satisfaction": {"model": backend_model},
            "owner_call": {"model": backend_model},
        },
        "summarization": {
            "person": {"model": backend_model},
            "call": {"model": backend_model},
        },
        "editing": {"model": backend_model},
        "search": {"model": DEFAULT_SEARCH_MODEL},
        "retrieval": {"vectorWeight": 0.7, "ftsWeight": 0.3, "threshold": 0.35, "limit": 10},
    }

    write_config("agent.json", agent_config)
    write_config("providers.json", providers_config)
    write_config("intelligence.json", intelligence_config)
    return agent_config, providers_config, intelligence_config


def _voice_pipeline_configured(stt_provider: object, tts_provider: object) -> bool:
    return bool(str(stt_provider or "").strip() and str(tts_provider or "").strip())


def _turn_detector_uses_remote_inference() -> bool:
    return bool(os.environ.get(TURN_DETECTOR_REMOTE_ENV, "").strip())


def turn_detector_assets_missing() -> list[str]:
    if _turn_detector_uses_remote_inference():
        return []

    try:
        from huggingface_hub import errors, hf_hub_download

        from livekit.plugins.turn_detector.models import (
            HG_MODEL,
            MODEL_REVISIONS,
            ONNX_FILENAME,
        )
    except ModuleNotFoundError as exc:
        return [getattr(exc, "name", None) or "turn-detector dependencies"]

    revision = MODEL_REVISIONS["multilingual"]
    missing: list[str] = []
    tokenizer_files = (
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "merges.txt",
        "special_tokens_map.json",
    )

    for filename in tokenizer_files:
        try:
            hf_hub_download(
                repo_id=HG_MODEL,
                filename=filename,
                revision=revision,
                local_files_only=True,
            )
        except (errors.LocalEntryNotFoundError, OSError):
            missing.append(filename)

    try:
        hf_hub_download(
            repo_id=HG_MODEL,
            filename="languages.json",
            revision=revision,
            local_files_only=True,
        )
    except (errors.LocalEntryNotFoundError, OSError):
        missing.append("languages.json")

    try:
        hf_hub_download(
            repo_id=HG_MODEL,
            filename=ONNX_FILENAME,
            subfolder="onnx",
            revision=revision,
            local_files_only=True,
        )
    except (errors.LocalEntryNotFoundError, OSError):
        missing.append(f"onnx/{ONNX_FILENAME}")

    return missing


def _ensure_turn_detector_model_download() -> None:
    from huggingface_hub import hf_hub_download

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*PyTorch was not found.*")
        from transformers import AutoTokenizer

    from livekit.plugins.turn_detector.models import (
        HG_MODEL,
        MODEL_REVISIONS,
        ONNX_FILENAME,
    )

    revision = MODEL_REVISIONS["multilingual"]
    AutoTokenizer.from_pretrained(HG_MODEL, revision=revision)
    hf_hub_download(
        repo_id=HG_MODEL,
        filename="languages.json",
        revision=revision,
    )
    hf_hub_download(
        repo_id=HG_MODEL,
        filename=ONNX_FILENAME,
        subfolder="onnx",
        revision=revision,
    )


async def ensure_dependencies(
    selections: InitSelections,
    on_step: Callable[[str], Awaitable[None]] | None = None,
    on_detail: Callable[[str, bool], None] | None = None,
    *,
    quiet: bool = False,
) -> None:
    """Download models and install plugins required by the selected providers.

    *on_detail(message, replace_last)* receives granular progress strings safe
    to call from any thread (sync callback).
    """

    def _detail(message: str, replace: bool = False) -> None:
        if on_detail is not None:
            on_detail(message, replace)

    def _download_progress(label: str) -> Any:
        state = {"last_message": "", "finished": False}

        def callback(downloaded: int, total: int | None) -> None:
            downloaded_mb = downloaded / (1024 * 1024)
            if total and total > 0:
                total_mb = total / (1024 * 1024)
                pct = min(100, downloaded * 100 // total)
                message = f"\r  {label}... {pct}%"
                detail = f"{label} {pct}% ({downloaded_mb:.1f} / {total_mb:.1f} MB)"
            else:
                message = f"\r  {label}... {downloaded_mb:.1f} MB"
                detail = f"{label} {downloaded_mb:.1f} MB"

            if message != state["last_message"]:
                if not quiet:
                    click.echo(message, nl=False)
                _detail(detail, True)
                state["last_message"] = message

            if total and total > 0 and downloaded >= total and not state["finished"]:
                if not quiet:
                    click.echo()
                state["finished"] = True

        return callback

    # ── Voice server binary ──────────────────────────────────────────────
    if on_step is not None:
        await on_step("Checking voice server...")
    _detail("Locating LiveKit server binary...")
    try:
        await ensure_livekit_binary()
        _detail("LiveKit server ready")
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if (
        _voice_pipeline_configured(
            selections.stt_config.get("provider"),
            selections.tts_config.get("provider"),
        )
        and not _turn_detector_uses_remote_inference()
    ):
        try:
            _detail("Installing turn detector plugin...")
            await ensure_python_extra(
                TURN_DETECTOR_MODULE,
                TURN_DETECTOR_PIP_PACKAGE,
                label="LiveKit turn detector",
            )
            if not quiet:
                click.echo(dim("Preparing LiveKit turn detector model..."))
            _detail("Downloading turn detector model...")
            await asyncio.to_thread(_ensure_turn_detector_model_download)
            if not quiet:
                click.echo(dim("LiveKit turn detector model ready."))
            _detail("Turn detector ready")
        except Exception as exc:
            if not quiet:
                click.echo(yellow(
                    "LiveKit turn detector install or model download failed. "
                    "Voice will still work with basic endpointing, but turn-taking "
                    "may be less accurate. You can install it manually later:\n"
                    f"  {sys.executable} -m pip install {TURN_DETECTOR_PIP_PACKAGE}\n"
                    f"  {get_error_message(exc)}"
                ))
            _detail("Turn detector skipped (basic endpointing)")

    # ── Speech-to-text ───────────────────────────────────────────────────
    if selections.stt_config.get("provider") == "moonshine":
        try:
            if on_step is not None:
                await on_step("Downloading STT model...")
            _detail("Installing moonshine-voice package...")
            await ensure_python_extra("moonshine_voice", "moonshine-voice", label="Moonshine Voice")
            model_name = str(selections.stt_config.get("model") or "small")
            if not quiet:
                click.echo(dim(f"Preparing Moonshine Voice model ({model_name})..."))
            _detail(f"Downloading Moonshine model ({model_name})...")
            await asyncio.to_thread(_ensure_moonshine_model_download, model_name)
            if not quiet:
                click.echo(dim("Moonshine Voice model ready."))
            _detail("Moonshine Voice ready")
        except Exception as exc:
            if not quiet:
                click.echo(yellow(
                    "Moonshine Voice install or model download failed. You can install it manually later:\n"
                    f"  {sys.executable} -m pip install moonshine-voice\n"
                    f"  {get_error_message(exc)}"
                ))
    elif selections.stt_config.get("provider") == "deepgram":
        try:
            if on_step is not None:
                await on_step("Installing speech recognition...")
            _detail("Installing livekit-plugins-deepgram...")
            await ensure_python_extra(
                "livekit.plugins.deepgram",
                "livekit-plugins-deepgram",
                label="LiveKit Deepgram plugin",
            )
            _detail("Deepgram plugin ready")
        except Exception as exc:
            if not quiet:
                click.echo(yellow(
                    "Deepgram plugin install failed. You can install it manually later:\n"
                    f"  {sys.executable} -m pip install livekit-plugins-deepgram\n"
                    f"  {get_error_message(exc)}"
                ))

    # ── Text-to-speech ───────────────────────────────────────────────────
    if selections.tts_config.get("provider") == "pocket":
        try:
            if on_step is not None:
                await on_step("Downloading TTS model...")
            _detail("Downloading Pocket TTS model files...")
            await asyncio.to_thread(
                ensure_pocket_onnx_models,
                lambda relative_path: _download_progress(f"Pocket TTS {relative_path}"),
            )
            _detail("Pocket TTS ready")
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    elif selections.tts_config.get("provider") == "inworld":
        try:
            if on_step is not None:
                await on_step("Installing speech synthesis...")
            _detail("Installing livekit-plugins-inworld...")
            await ensure_python_extra(
                "livekit.plugins.inworld",
                "livekit-plugins-inworld",
                label="LiveKit Inworld plugin",
            )
            _detail("Inworld plugin ready")
        except Exception as exc:
            if not quiet:
                click.echo(yellow(
                    "Inworld plugin install failed. You can install it manually later:\n"
                    f"  {sys.executable} -m pip install livekit-plugins-inworld\n"
                    f"  {get_error_message(exc)}"
                ))

    # ── Embedding model ──────────────────────────────────────────────────
    if selections.embedding_config.get("provider") == "local":
        try:
            if on_step is not None:
                await on_step("Downloading embedding model...")
            _detail("Installing ONNX Runtime...")
            await ensure_python_extra("onnxruntime", "onnxruntime", label="ONNX Runtime")
            _detail("Installing tokenizers...")
            await ensure_python_extra("tokenizers", "tokenizers", label="HuggingFace Tokenizers")
            model_name = str(selections.embedding_config.get("model") or DEFAULT_LOCAL_EMBEDDING_MODEL)
            _detail(f"Downloading {model_name}...")
            await asyncio.to_thread(
                ensure_local_model,
                model_name,
                lambda filename: _download_progress(f"Embeddings {filename}"),
            )
            _detail("Embedding model ready")
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

def _ensure_moonshine_model_download(model: object) -> None:
    import moonshine_voice

    normalized = str(model or "small").strip().lower() or "small"
    arch_map = {
        "tiny": moonshine_voice.ModelArch.TINY_STREAMING,
        "small": moonshine_voice.ModelArch.SMALL_STREAMING,
        "medium": moonshine_voice.ModelArch.MEDIUM_STREAMING,
    }
    try:
        arch = arch_map[normalized]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported Moonshine model: {normalized}") from exc
    moonshine_voice.get_model_for_language("en", arch)


async def run_init(*, advanced: bool = False) -> None:
    home = get_home()
    current_agent = home.name

    click.echo(bold("mystic-horizon init"))

    if config_exists():
        overwrite = click.confirm("Configuration already exists. Overwrite config files?", default=False)
        if not overwrite:
            click.echo(dim("Init cancelled."))
            return

    imported = _maybe_import_sibling_keys(current_agent)
    if advanced:
        selections = _prompt_init_selections(current_agent, imported)
    else:
        selections = _prompt_quick_init(current_agent, imported)
    click.echo(bold("Downloading dependencies..."))
    await ensure_dependencies(selections)
    write_config_files(home, selections)
    seed_prompt_files(home)
    _setup_database(selections.owner_phone)

    clear_config_cache()
    _show_init_summary(selections)


async def run_connect_twilio(*, show_intro: bool = True) -> bool:
    home = get_home()
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic init' first.")

    providers_path = home / "config" / "providers.json"
    agent_path = home / "config" / "agent.json"
    try:
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
        agent_payload = json.loads(agent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Could not read config files: {exc}") from exc

    if show_intro:
        click.echo(bold("mystic-horizon connect-twilio"))

    imported = _maybe_import_sibling_keys(home.name)
    account_sid = _prompt_secret("Twilio account SID", imported.get("twilioSid"), required=True, default_is_secret=False)
    auth_token = _prompt_secret("Twilio auth token", imported.get("twilioToken"), required=True)

    imported_phone = imported.get("twilioPhone")
    imported_phone_sid = imported.get("twilioPhoneNumberSid")
    if imported_phone and click.confirm(f"Reuse Twilio number {imported_phone}?", default=True):
        phone_number = imported_phone
        phone_number_sid = imported_phone_sid
    elif click.confirm("Buy or assign a Twilio number now?", default=False):
        phone_number, phone_number_sid = await _prompt_twilio_number(account_sid=account_sid, auth_token=auth_token)
    else:
        phone_number = click.prompt("Twilio phone number (E.164)", type=str).strip()
        if not is_valid_e164(phone_number):
            raise click.ClickException(f"Invalid phone number: {phone_number}")
        phone_number_sid = click.prompt("Twilio phone SID (optional)", default="", show_default=False).strip() or None

    tailscale_ready, tailscale_reason = check_tailscale_ready()
    if not tailscale_ready:
        click.echo("Tailscale is required for Twilio connectivity.\n")
        click.echo("Install:  curl -fsSL https://tailscale.com/install.sh | sh")
        click.echo("Start:    sudo tailscale up\n")
        click.echo("Then re-run: mystic init --connect-twilio")
        if tailscale_reason:
            click.echo(dim(f"Current status: {tailscale_reason}"))
        return False

    providers_payload["twilio"] = {
        "accountSid": account_sid,
        "authToken": auth_token,
        "phoneNumber": phone_number,
    }
    if phone_number_sid:
        providers_payload["twilio"]["phoneNumberSid"] = phone_number_sid
    agent_payload["tunnel"] = {"enabled": True}

    if not agent_payload.get("owner", {}).get("phone"):
        owner_phone = click.prompt("Your phone number E.164 (for owner call detection)", default="", show_default=False).strip()
        if owner_phone and is_valid_e164(owner_phone):
            agent_payload.setdefault("owner", {})["phone"] = owner_phone

    providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")
    agent_path.write_text(json.dumps(agent_payload, indent=2) + "\n", encoding="utf-8")
    clear_config_cache()

    click.echo(green(f"Twilio connected: {format_phone(phone_number)}"))
    return True


async def run_connect_smtp(*, show_intro: bool = True) -> bool:
    home = get_home()
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic init' first.")

    providers_path = home / "config" / "providers.json"
    try:
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Could not read config files: {exc}") from exc
    if not isinstance(providers_payload, dict):
        raise click.ClickException("providers.json must contain an object.")

    if show_intro:
        click.echo(bold("mystic-horizon connect-smtp"))

    imported = _maybe_import_sibling_keys(home.name)
    host = click.prompt("SMTP host", type=str, default=imported.get("smtpHost") or "").strip()
    port = click.prompt("SMTP port", type=int, default=int(imported.get("smtpPort") or 587))
    username = click.prompt("SMTP username", type=str, default=imported.get("smtpUsername") or "").strip()
    password = _prompt_secret("SMTP password", imported.get("smtpPassword"), required=True)
    from_address = click.prompt("From email address", type=str, default=imported.get("smtpFrom") or "").strip()
    use_tls = click.confirm("Use TLS?", default=imported.get("smtpUseTls", "True").lower() != "false")

    providers_payload["smtp"] = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "fromAddress": from_address,
        "useTls": use_tls,
    }
    providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")
    clear_config_cache()
    click.echo(green(f"SMTP configured: {host}:{port} as {from_address}"))
    return True


async def run_connect_calendar(*, show_intro: bool = True) -> bool:
    home = get_home()
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic init' first.")

    providers_path = home / "config" / "providers.json"
    try:
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Could not read config files: {exc}") from exc
    if not isinstance(providers_payload, dict):
        raise click.ClickException("providers.json must contain an object.")

    if show_intro:
        click.echo(bold("mystic-horizon connect-calendar"))

    existing_subscriptions: list[dict[str, str]] = []
    sync_default = 15
    reminder_default = 10
    existing_calendar = providers_payload.get("calendar")
    if isinstance(existing_calendar, dict):
        subscriptions_raw = existing_calendar.get("subscriptions")
        if isinstance(subscriptions_raw, list):
            for item in subscriptions_raw:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                label = item.get("label")
                if not isinstance(url, str) or not url.strip():
                    continue
                entry = {"url": url.strip()}
                if isinstance(label, str) and label.strip():
                    entry["label"] = label.strip()
                existing_subscriptions.append(entry)
        sync_raw = existing_calendar.get("syncIntervalMinutes")
        if isinstance(sync_raw, int) and not isinstance(sync_raw, bool):
            sync_default = sync_raw
        reminder_raw = existing_calendar.get("reminderMinutes")
        if isinstance(reminder_raw, int) and not isinstance(reminder_raw, bool):
            reminder_default = reminder_raw

    new_subscriptions: list[dict[str, str]] = []
    while True:
        url = click.prompt(
            "Paste an ICS subscription URL (from Google Calendar, Outlook, etc.)",
            type=str,
        ).strip()
        if not url:
            raise click.ClickException("Calendar URL is required.")
        try:
            response = await fetch_with_timeout(
                url,
                timeout_ms=30_000,
                timeout_label="calendar.validate",
            )
        except Exception as exc:
            raise click.ClickException(f"Could not fetch calendar URL: {get_error_message(exc)}") from exc
        if not 200 <= response.status_code < 300:
            raise click.ClickException(f"Calendar URL returned HTTP {response.status_code}: {url}")
        if "BEGIN:VCALENDAR" not in response.text:
            raise click.ClickException("Calendar URL did not return a valid ICS feed.")

        label = click.prompt("Label for this calendar (optional)", default="", show_default=False).strip()
        entry = {"url": url}
        if label:
            entry["label"] = label
        new_subscriptions.append(entry)

        if not click.confirm("Add another?", default=False):
            break

    sync_interval_minutes = click.prompt(
        "Sync interval minutes",
        type=int,
        default=sync_default,
        show_default=True,
    )
    if sync_interval_minutes < 0:
        raise click.ClickException("Sync interval minutes must be non-negative.")

    reminder_minutes = click.prompt(
        "Reminder minutes",
        type=int,
        default=reminder_default,
        show_default=True,
    )
    if reminder_minutes < 0:
        raise click.ClickException("Reminder minutes must be non-negative.")

    merged_by_url = {entry["url"]: entry for entry in existing_subscriptions}
    for entry in new_subscriptions:
        merged_by_url[entry["url"]] = entry
    providers_payload["calendar"] = {
        "subscriptions": list(merged_by_url.values()),
        "syncIntervalMinutes": sync_interval_minutes,
        "reminderMinutes": reminder_minutes,
    }

    providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")
    clear_config_cache()

    from mystic.calendar import sync_subscription
    from mystic.config import CalendarSubscription

    db = open_database()
    synced: list[tuple[dict[str, str], int]] = []
    try:
        initialize_schema(db)
        for entry in new_subscriptions:
            synced.append(
                (
                    entry,
                    await sync_subscription(
                        db,
                        CalendarSubscription(
                            url=entry["url"],
                            label=cast(str | None, entry.get("label")),
                        ),
                    ),
                )
            )
    finally:
        close_database(db)

    if len(synced) == 1:
        entry, count = synced[0]
        click.echo(green(f"Calendar connected. {count} events synced from {entry.get('label') or entry['url']}."))
        return True

    click.echo(green("Calendar connected."))
    for entry, count in synced:
        click.echo(f"- {count} events synced from {entry.get('label') or entry['url']}.")
    return True


async def run_connect_hub_calendar(*, show_intro: bool = True) -> bool:
    home = get_home()
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic init' first.")

    providers_path = home / "config" / "providers.json"
    try:
        providers_payload = json.loads(providers_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Could not read config files: {exc}") from exc
    if not isinstance(providers_payload, dict):
        raise click.ClickException("providers.json must contain an object.")

    if show_intro:
        click.echo(bold("mystic-horizon connect-hub-calendar"))
        click.echo("Push scheduled actions to your real calendar.\n")

    provider = click.prompt(
        "Calendar provider",
        type=click.Choice(["google", "outlook", "caldav"], case_sensitive=False),
    ).strip().lower()

    if provider == "google":
        hub_config, tokens = await _connect_google_hub()
    elif provider == "outlook":
        hub_config, tokens = await _connect_outlook_hub()
    else:
        hub_config, tokens = await _connect_caldav_hub(), None

    calendar_section = providers_payload.get("calendar")
    if not isinstance(calendar_section, dict):
        calendar_section = {}
    calendar_section["hub"] = hub_config
    providers_payload["calendar"] = calendar_section

    providers_path.write_text(json.dumps(providers_payload, indent=2) + "\n", encoding="utf-8")
    clear_config_cache()

    if tokens is not None:
        save_hub_tokens(tokens)

    click.echo(green(f"{provider.title()} connected for calendar write-back."))
    return True


async def _connect_google_hub() -> tuple[dict[str, object], OAuthTokens]:
    from mystic.calendar import run_oauth_flow

    click.echo("Create a Google OAuth desktop app, enable Google Calendar API, then paste the credentials below.\n")
    client_id = click.prompt("Google OAuth client ID", type=str).strip()
    if not client_id:
        raise click.ClickException("Google client ID is required.")
    client_secret = _prompt_secret("Google OAuth client secret", None, required=True, default_is_secret=False)
    calendar_id = click.prompt("Google calendar ID", default="primary", show_default=True).strip() or "primary"
    click.echo(dim("Opening your browser for Google authorization..."))
    tokens = await run_oauth_flow(
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
        extra_params={"access_type": "offline", "prompt": "consent"},
    )
    return (
        {
            "provider": "google",
            "calendarId": calendar_id,
            "clientId": client_id,
            "clientSecret": client_secret,
            "writeEnabled": True,
        },
        tokens,
    )


async def _connect_outlook_hub() -> tuple[dict[str, object], OAuthTokens]:
    from mystic.calendar import run_oauth_flow

    click.echo("Create a Microsoft Entra app with Calendars.ReadWrite delegated access, then paste the credentials below.\n")
    client_id = click.prompt("Microsoft app client ID", type=str).strip()
    if not client_id:
        raise click.ClickException("Microsoft client ID is required.")
    client_secret = _prompt_secret("Microsoft client secret", None, required=False, default_is_secret=False)
    calendar_id = click.prompt("Microsoft calendar ID", type=str).strip()
    if not calendar_id:
        raise click.ClickException("Microsoft calendar ID is required.")
    click.echo(dim("Opening your browser for Microsoft authorization..."))
    tokens = await run_oauth_flow(
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        client_id=client_id,
        client_secret=client_secret or None,
        scopes=["Calendars.ReadWrite", "offline_access"],
    )
    return (
        {
            "provider": "microsoft",
            "calendarId": calendar_id,
            "clientId": client_id,
            "clientSecret": client_secret or None,
            "writeEnabled": True,
        },
        tokens,
    )


async def _connect_caldav_hub() -> dict[str, object]:
    base_url = click.prompt("CalDAV base URL", type=str).strip().rstrip("/")
    calendar_path = click.prompt("CalDAV calendar path", type=str).strip()
    username = click.prompt("CalDAV username", type=str).strip()
    password = _prompt_secret("CalDAV app password", None, required=True)
    if not base_url:
        raise click.ClickException("CalDAV base URL is required.")
    if not calendar_path:
        raise click.ClickException("CalDAV calendar path is required.")
    if not username:
        raise click.ClickException("CalDAV username is required.")

    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    normalized_path = calendar_path if calendar_path.startswith("/") else f"/{calendar_path}"
    response = await fetch_with_timeout(
        f"{base_url}{normalized_path}",
        method="PROPFIND",
        headers={
            "Authorization": f"Basic {auth}",
            "Depth": "0",
            "Content-Type": "application/xml",
        },
        data='<?xml version="1.0"?><propfind xmlns="DAV:"><prop><resourcetype/></prop></propfind>',
        timeout_ms=15_000,
        timeout_label="caldav-validate",
    )
    if response.status_code not in (200, 207):
        raise click.ClickException(f"CalDAV validation failed: HTTP {response.status_code}")

    return {
        "provider": "caldav",
        "calendarId": normalized_path,
        "baseUrl": base_url,
        "username": username,
        "password": password,
        "writeEnabled": True,
    }


def _prompt_init_selections(current_agent: str, imported: dict[str, str]) -> InitSelections:
    used_ports = discover_used_ports(current_agent)
    blocked = set(used_ports.server_ports | used_ports.livekit_ports)
    server_port = allocate_port(DEFAULT_SERVER_PORT, blocked)
    livekit_port = allocate_port(DEFAULT_LIVEKIT_PORT, blocked, server_port, stride=3)

    owner_phone_raw = click.prompt("Owner phone E.164 (optional, for caller detection)", default="", show_default=False).strip()
    owner_phone: str | None = None
    if owner_phone_raw:
        if not is_valid_e164(owner_phone_raw):
            raise click.ClickException(f"Invalid phone number: {owner_phone_raw}")
        owner_phone = owner_phone_raw

    timezone_default = _detect_timezone()
    timezone = click.prompt("Timezone", default=timezone_default, show_default=True).strip()

    tts_provider = click.prompt(
        "TTS provider",
        type=click.Choice(["pocket", "inworld"], case_sensitive=False),
        default="pocket",
    ).lower()
    tts_config: dict[str, object]
    if tts_provider == "inworld":
        tts_config = {
            "provider": "inworld",
            "apiKey": _prompt_secret("Inworld API key", imported.get("inworld"), required=True),
        }
    else:
        tts_config = {"provider": "pocket"}

    selected_voice_id = click.prompt("Voice ID", default=DEFAULT_VOICE_ID, show_default=True).strip()

    stt_provider = click.prompt(
        "STT provider",
        type=click.Choice(["moonshine", "deepgram"], case_sensitive=False),
        default="moonshine",
    ).lower()
    stt_config: dict[str, object]
    if stt_provider == "deepgram":
        stt_config = {
            "provider": "deepgram",
            "apiKey": _prompt_secret("Deepgram API key", imported.get("deepgram"), required=True),
        }
    else:
        moonshine_model = click.prompt(
            "Moonshine model",
            type=click.Choice(["tiny", "small", "medium"], case_sensitive=False),
            default="small",
        ).lower()
        stt_config = {"provider": "moonshine", "model": moonshine_model}

    embedding_config = _default_local_embedding_config()
    llm_realtime, llm_backend, openrouter_key = _prompt_llm_config(imported)

    click.echo(dim(f"Allocated ports: app {server_port}, livekit {livekit_port}"))
    return InitSelections(
        timezone=timezone,
        selected_voice_id=selected_voice_id or DEFAULT_VOICE_ID,
        server_port=server_port,
        livekit_port=livekit_port,
        tts_config=tts_config,
        stt_config=stt_config,
        embedding_config=embedding_config,
        llm_realtime=llm_realtime,
        llm_backend=llm_backend,
        openrouter_key=openrouter_key,
        owner_phone=owner_phone,
    )


def _prompt_quick_init(current_agent: str, imported: dict[str, str]) -> InitSelections:
    used_ports = discover_used_ports(current_agent)
    blocked = set(used_ports.server_ports | used_ports.livekit_ports)
    server_port = allocate_port(DEFAULT_SERVER_PORT, blocked)
    livekit_port = allocate_port(DEFAULT_LIVEKIT_PORT, blocked, server_port, stride=3)

    click.echo(section("API Keys"))
    openrouter_key = _prompt_secret("OpenRouter API key", imported.get("openrouter"), required=True)

    click.echo(section("Voice"))
    tts_provider = _pick(
        "Pick a TTS provider",
        [
            ("pocket", "Local Pocket TTS (Requires 8-Core CPU)"),
            ("inworld", "Cloud Inworld (Requires API Key)"),
        ],
    )
    tts_config: dict[str, object]
    voice_catalog = VOICE_CATALOG
    if tts_provider == "inworld":
        tts_config = {
            "provider": "inworld",
            "apiKey": _prompt_secret("Inworld API key", imported.get("inworld"), required=True),
        }
    else:
        tts_config = {"provider": "pocket"}
        voice_catalog = POCKET_VOICE_CATALOG

    voice_options = [
        (str(v["voice_id"]), f"{v['label']} — {VOICE_DESCRIPTIONS.get(str(v['voice_id']), '')}")
        for v in voice_catalog
    ]
    selected_voice_id = _pick("Pick a voice", voice_options)

    click.echo(dim(f"\nAllocated ports: app {server_port}, livekit {livekit_port}"))
    return InitSelections(
        timezone=_detect_timezone(),
        selected_voice_id=selected_voice_id,
        server_port=server_port,
        livekit_port=livekit_port,
        tts_config=tts_config,
        stt_config={"provider": "moonshine", "model": "small"},
        embedding_config=_default_local_embedding_config(),
        llm_realtime={"provider": "openrouter", "model": DEFAULT_REALTIME_MODEL},
        llm_backend={"provider": "openrouter", "model": DEFAULT_BACKEND_MODEL},
        openrouter_key=openrouter_key,
    )


def _prompt_embedding_config(imported: dict[str, str]) -> dict[str, object]:
    del imported
    return _default_local_embedding_config()


def _prompt_llm_config(imported: dict[str, str]) -> tuple[dict[str, object], dict[str, object], str | None]:
    openrouter_key: str | None = None
    realtime = _prompt_llm_slot("Realtime", imported, default_model=DEFAULT_REALTIME_MODEL)
    backend = _prompt_llm_slot("Backend", imported, default_model=DEFAULT_BACKEND_MODEL)
    if realtime["provider"] == "openrouter" or backend["provider"] == "openrouter":
        openrouter_key = _prompt_secret("OpenRouter API key", imported.get("openrouter"), required=True)
    return realtime, backend, openrouter_key


def _prompt_llm_slot(label: str, imported: dict[str, str], *, default_model: str) -> dict[str, object]:
    provider = click.prompt(
        f"{label} LLM provider",
        type=click.Choice(["openrouter", "custom", "local"], case_sensitive=False),
        default="openrouter",
    ).lower()
    slot: dict[str, object] = {"provider": provider}
    if provider == "openrouter":
        model = click.prompt(f"{label} model", default=default_model, show_default=True).strip()
        if model:
            slot["model"] = model
        return slot

    base_url = click.prompt(f"{label} base URL", default="http://127.0.0.1:11434/v1", show_default=True).strip()
    model = click.prompt(f"{label} model", default=default_model, show_default=True).strip()
    api_key = _prompt_secret(f"{label} API key (optional)", None, required=False)
    slot["baseURL"] = base_url.rstrip("/")
    if model:
        slot["model"] = model
    if api_key:
        slot["apiKey"] = api_key
    return slot


def _prompt_secret(
    label: str,
    imported_value: str | None,
    *,
    required: bool,
    default_is_secret: bool = True,
) -> str:
    if imported_value:
        click.echo(dim(f"  Using imported {label}."))
        return imported_value

    while True:
        click.echo(dim("  (input hidden — type and press Enter)"))
        value = click.prompt(
            label,
            default="" if not required else None,
            hide_input=True,
            show_default=not default_is_secret,
        ).strip()
        if value:
            return value
        if not required:
            return value
        click.echo(yellow(f"  {label} is required. Press Ctrl+C to abort."))


def _maybe_import_sibling_keys(current_agent: str) -> dict[str, str]:
    siblings = discover_siblings(current_agent)
    if not siblings:
        return {}
    sibling = siblings[0]
    if not click.confirm(f"Reuse service API keys from sibling agent '{sibling}'?", default=True):
        return {}
    imported = extract_sibling_keys(sibling)
    if imported:
        click.echo(dim(f"Imported keys from {sibling}."))
    return imported


def _setup_database(owner_phone: str | None = None) -> tuple[str, str]:
    from mystic.calls import LOCAL_OWNER_PHONE

    db = open_database()
    try:
        silence_stdout()
        try:
            initialize_schema(db)
        finally:
            silence_stdout(False)
        owner = upsert_person(db, owner_phone or LOCAL_OWNER_PHONE)
        bootstrap_action = insert_action(
            db,
            person_id=owner.id,
            intent="Get to know owner",
            context="Bootstrap conversation to establish identity and soul.",
            source="cli",
        )
        return owner.id, bootstrap_action.id
    finally:
        close_database(db)


async def _prompt_twilio_number(*, account_sid: str, auth_token: str) -> tuple[str, str | None]:
    twilio_config = {"accountSid": account_sid, "authToken": auth_token, "phoneNumber": "+10000000000"}
    area_code = click.prompt("US area code to search (blank for any)", default="", show_default=False).strip() or None
    available = await search_available_numbers(_coerce_twilio_config(twilio_config), area_code=area_code)
    if not available:
        raise click.ClickException("No Twilio numbers found for that search.")

    click.echo("Available numbers:")
    for index, number in enumerate(available, start=1):
        click.echo(f"  {index}. {number['phoneNumber']} ({number['friendlyName']})")
    selected = click.prompt("Choose number to buy", type=int, default=1)
    if selected < 1 or selected > len(available):
        raise click.ClickException("Invalid phone number selection")

    picked = available[selected - 1]
    purchased = await buy_phone_number(
        _coerce_twilio_config(twilio_config),
        picked["phoneNumber"],
        "https://placeholder.example.com/voice",
        "https://placeholder.example.com/status",
    )
    return purchased["phoneNumber"], purchased.get("sid")


def _coerce_twilio_config(payload: dict[str, str]) -> Any:
    return TwilioConfig(
        accountSid=payload["accountSid"],
        authToken=payload["authToken"],
        phoneNumber=payload["phoneNumber"],
    )


def _detect_timezone() -> str:
    from datetime import datetime

    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    return DEFAULT_TIMEZONE


def _show_init_summary(selections: InitSelections) -> None:
    lines = [
        f"Agent home: {get_home()}",
        f"Voice:      {selections.selected_voice_id}",
        f"Ports:      app {selections.server_port}, livekit {selections.livekit_port}",
    ]
    if selections.owner_phone:
        lines.insert(1, f"Owner:      {dim(format_phone(selections.owner_phone))}")
    click.echo(box(lines))
    click.echo(green("Initialization complete."))
    click.echo("Run 'mystic start', then open the dashboard live page to meet your agent.")


def _open_browser(url: str) -> None:
    """Open *url* in the default browser, suppressing noisy stderr from the browser process."""
    import shutil

    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    else:
        webbrowser.open(url)


def _generate_default_agent_name() -> str:
    existing = set(list_agent_dirs())
    index = 1
    while True:
        candidate = f"mystic-{index}"
        if candidate not in existing:
            return candidate
        index += 1


def _get_setup_flow_timeout_seconds() -> float | None:
    raw = os.environ.get("MH_SETUP_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_SETUP_FLOW_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(raw)
    except ValueError:
        logger.warn("setup.timeout.invalid", value=raw, defaultSeconds=DEFAULT_SETUP_FLOW_TIMEOUT_SECONDS)
        return float(DEFAULT_SETUP_FLOW_TIMEOUT_SECONDS)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def _format_setup_timeout(timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return "without a timeout"
    if timeout_seconds >= 60 and timeout_seconds % 60 == 0:
        minutes = int(timeout_seconds // 60)
        unit = "minute" if minutes == 1 else "minutes"
        return f"after {minutes} {unit}"
    seconds = int(timeout_seconds)
    unit = "second" if seconds == 1 else "seconds"
    return f"after {seconds} {unit}"


def _default_setup_selections(agent_name: str, *, port: int | None = None) -> InitSelections:
    used = discover_used_ports(agent_name)
    server_port = port or allocate_port(DEFAULT_SERVER_PORT, used.server_ports)
    livekit_port = allocate_port(DEFAULT_LIVEKIT_PORT, used.livekit_ports, server_port, stride=3)
    return InitSelections(
        timezone=_detect_timezone(),
        selected_voice_id=DEFAULT_SETUP_VOICE_ID,
        server_port=server_port,
        livekit_port=livekit_port,
        tts_config={"provider": ""},
        stt_config={"provider": ""},
        embedding_config=_default_local_embedding_config(),
        llm_realtime={"provider": "openrouter", "model": DEFAULT_REALTIME_MODEL},
        llm_backend={"provider": "openrouter", "model": DEFAULT_BACKEND_MODEL},
        openrouter_key=None,
        owner_phone=None,
    )


async def run_setup(*, agent_name: str | None = None, port: int | None = None) -> dict[str, object]:
    selected_agent = agent_name or _generate_default_agent_name()
    _apply_agent_env(selected_agent)
    home = get_home()

    if not config_exists():
        selections = _default_setup_selections(selected_agent, port=port)
        try:
            await ensure_livekit_binary()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        write_config_files(home, selections)
        seed_prompt_files(home)
        _setup_database(selections.owner_phone)

    seed_dashboard_defaults()
    token = ensure_dashboard_token()
    agent_config = get_agent_config()
    default_dashboard_url = _dashboard_base_url(agent_config.server.port)
    existing = await asyncio.to_thread(probe_daemon)
    if existing is not None:
        dashboard_url = str(existing.get("dashboard") or default_dashboard_url)
        login_url = f"{dashboard_url.rstrip('/')}/login?token={token}&next=/dashboard/setup"
        with suppress(Exception):
            _open_browser(login_url)
        result = {
            "status": "ready",
            "agent": selected_agent,
            "dashboard": dashboard_url,
            "login": login_url,
            "token": token,
            "port": existing.get("port") or agent_config.server.port,
            "pid": existing.get("pid"),
        }
        return result

    silence_stdout()
    try:
        db = open_database()
    finally:
        silence_stdout(False)
    setup_server = None
    setup_done = asyncio.Event()
    login_url = f"{default_dashboard_url.rstrip('/')}/login?token={token}&next=/dashboard/setup"
    try:
        silence_stdout()
        try:
            initialize_schema(db)
        finally:
            silence_stdout(False)
        set_setup_db(db)
        set_setup_runtime(None)
        setup_app = create_setup_app(db)
        set_setup_done_event(setup_done)
        setup_server = await start_server(setup_app, agent_config.server.port, shutdown_timeout=2.0)
        set_setup_server(setup_server)
        dashboard_url = _dashboard_base_url(setup_server.port)
        login_url = f"{dashboard_url.rstrip('/')}/login?token={token}&next=/dashboard/setup"

        with suppress(Exception):
            _open_browser(login_url)

        setup_timeout = _get_setup_flow_timeout_seconds()
        await asyncio.wait_for(setup_done.wait(), timeout=setup_timeout)

        # Close setup server to free the port, then spawn the daemon.
        # The browser stays on the setup page polling /health until the
        # daemon is fully ready — no intermediate server bounce.
        if setup_server is not None:
            await setup_server.close()
            setup_server = None

        start_result = await asyncio.to_thread(run_start)

        result: dict[str, object] = {
            "status": "ready",
            "agent": selected_agent,
            "dashboard": start_result.get("dashboard", default_dashboard_url),
            "token": token,
            "port": start_result.get("port"),
            "pid": start_result.get("pid"),
        }
        return result
    except asyncio.TimeoutError as exc:
        timeout_label = _format_setup_timeout(_get_setup_flow_timeout_seconds())
        logger.error("setup.timeout", message="Setup flow timed out", timeout=timeout_label)
        raise click.ClickException(
            f"Setup timed out {timeout_label}. "
            "If first-run model downloads are still active, retry with a larger "
            "MH_SETUP_TIMEOUT_SECONDS value or set it to 0 to disable this watchdog."
        ) from exc
    finally:
        set_setup_done_event(None)
        set_setup_server(None)
        set_setup_db(None)
        set_setup_runtime(None)
        if setup_server is not None:
            with suppress(Exception):
                await setup_server.close()
        close_database(db)


# ── start command ──

def preflight_check(providers: Any) -> list[str]:
    problems: list[str] = []

    try:
        if resolve_supported_livekit_binary() is None:
            problems.append(get_livekit_missing_message())
    except RuntimeError as exc:
        problems.append(str(exc))

    if getattr(providers.tts, "provider", "") == "pocket":
        normalized_model = str(getattr(providers.tts, "model", "") or "default").strip()
        if normalized_model not in {"", "default"}:
            problems.append(
                "Pocket ONNX only supports the bundled default model set; "
                f"unsupported tts.model={normalized_model!r}."
            )
        missing_pocket = pocket_onnx_models_missing()
        if missing_pocket:
            problems.append(
                "Pocket ONNX models are missing: "
                + ", ".join(missing_pocket)
                + ". Run 'mystic-horizon --agent <name> init' to download them."
            )

    if getattr(providers.stt, "provider", "") == "moonshine":
        if not is_python_package_available("moonshine_voice"):
            problems.append(
                "Moonshine Voice package not installed. "
                "Run 'mystic-horizon --agent <name> init' or install manually: "
                f"{sys.executable} -m pip install moonshine-voice"
            )

    if _voice_pipeline_configured(
        getattr(providers.stt, "provider", ""),
        getattr(providers.tts, "provider", ""),
    ) and not _turn_detector_uses_remote_inference():
        if not is_python_package_available(TURN_DETECTOR_MODULE):
            problems.append(
                "LiveKit turn detector package not installed. "
                "Voice will still work with basic endpointing, but turn-taking "
                "will be less accurate. Run 'mystic-horizon --agent <name> init' "
                f"or install manually: {sys.executable} -m pip install {TURN_DETECTOR_PIP_PACKAGE}"
            )
        else:
            missing_turn_detector = turn_detector_assets_missing()
            if missing_turn_detector:
                problems.append(
                    "LiveKit turn detector files are missing: "
                    + ", ".join(missing_turn_detector)
                    + ". Voice will still work with basic endpointing, but turn-taking "
                    + "will be less accurate. Run 'mystic-horizon --agent <name> init' "
                    + "or use Dashboard Prepare to download them."
                )

    missing_embed = embedding_model_missing()
    if missing_embed:
        problems.append(
            "Embedding model files missing: "
            + ", ".join(missing_embed)
            + ". Run 'mystic-horizon --agent <name> init' to download them."
        )

    if providers.twilio is not None:
        tailscale_ready, reason = check_tailscale_ready()
        if not tailscale_ready:
            problems.append(
                f"Tailscale not ready (required for Twilio tunnel): {reason}. "
                "Run 'sudo tailscale up'."
            )

    return problems


def _emit_json(payload: Mapping[str, object]) -> None:
    click.echo(json.dumps(dict(payload), indent=2, sort_keys=True))


def _dashboard_base_url(port: int, tunnel_url: str | None = None) -> str:
    base = (tunnel_url or "").strip().rstrip("/")
    if not base:
        base = f"http://localhost:{port}"
    return f"{base}/dashboard"


async def _enter_daemon_mode(rt: Runtime) -> None:
    server: asyncio.AbstractServer | None = None
    shutdown_event = asyncio.Event()
    shutting_down = False
    started_at = now_ms()

    async def shutdown(signal_name: str) -> None:
        nonlocal shutting_down, server
        if shutting_down:
            return
        shutting_down = True
        try:
            if server is not None:
                server.close()
                await server.wait_closed()
                server = None
            if rt is not None:
                await _stop_runtime(rt)
        finally:
            cleanup_runtime_files()
            shutdown_event.set()
        if signal_name:
            logger.info("daemon.stopped", signal=signal_name)

    async def handle_control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (asyncio.TimeoutError, json.JSONDecodeError):
            payload = {}

        command = payload.get("cmd")
        response: dict[str, object]
        if command == "shutdown":
            response = {"status": "stopping", "pid": os.getpid()}
            asyncio.create_task(shutdown("socket"))
        else:
            response = {
                "status": "running",
                "pid": os.getpid(),
                "port": rt.port if rt is not None else get_agent_config().server.port,
                "tunnel_url": rt.tunnel_url if rt is not None else "",
                "started_at": started_at,
                "mode": "full",
                "dashboard": _dashboard_base_url(
                    rt.port if rt is not None else get_agent_config().server.port,
                    rt.tunnel_url if rt is not None else "",
                ),
            }

        writer.write(json.dumps(response).encode("utf-8") + b"\n")
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, lambda sig=signum: asyncio.create_task(shutdown(sig.name)))

    socket_path = get_daemon_socket_path()
    _unlink_if_exists(socket_path)

    try:
        write_pid(os.getpid())
        write_runtime_state(
            RuntimeStateRecord(
                pid=os.getpid(),
                port=rt.port,
                tunnel_url=rt.tunnel_url,
                mode="full",
                started_at=started_at,
            )
        )
        server = await asyncio.start_unix_server(handle_control, path=str(socket_path))
        await shutdown_event.wait()
    finally:
        if not shutting_down:
            await shutdown("")


async def run_dev(*, skip_voice: bool = False) -> None:
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic setup' or 'mystic init' first.")

    existing = await _send_daemon_command({"cmd": "health"})
    if existing is not None:
        raise click.ClickException(
            f"Daemon already running (pid {existing.get('pid')}). Run 'mystic stop' first."
        )

    seed_dashboard_defaults()
    token = ensure_dashboard_token()
    agent_config = get_agent_config()
    port = agent_config.server.port

    # Print readiness diagnostics
    readiness = _voice_readiness()
    llm_ok = not _llm_setup_required()
    click.echo("Mystic Horizon dev server")
    click.echo("-" * 25)
    click.echo(f"  LLM realtime : {'ready' if llm_ok else 'NOT READY'}")
    click.echo(f"  STT          : {'ready' if readiness['stt_ready'] else 'NOT READY'} ({readiness['stt_provider'] or 'none'})")
    click.echo(f"  TTS          : {'ready' if readiness['tts_ready'] else 'NOT READY'} ({readiness['tts_provider'] or 'none'})")
    if skip_voice:
        click.echo("  LiveKit      : skipped (--skip-voice)")
    click.echo()
    click.echo(f"  Dashboard: http://localhost:{port}/dashboard/login?token={token}&next=/dashboard/page/home")
    click.echo()
    click.echo("Press Ctrl+C to stop.")
    click.echo()

    rt = await _start_dev(skip_voice=skip_voice)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await _stop_runtime(rt)


async def run_serve() -> None:
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic setup' or 'mystic init' first.")

    errors = preflight_check(get_providers_config())
    if errors:
        for msg in errors:
            logger.warn("preflight.warning", message=msg)

    rt = await _start_full()
    await _enter_daemon_mode(rt)


def _tail_log(n: int = 10) -> str:
    """Return last *n* lines from the daemon log, or empty string."""
    try:
        log_path = get_log_path()
        if not log_path.exists():
            return ""
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def run_start() -> dict[str, object]:
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic setup' or 'mystic init' first.")

    existing = probe_daemon()
    if existing is not None:
        return {
            "status": "already_running",
            "pid": existing.get("pid"),
            "port": existing.get("port"),
            "dashboard": existing.get("dashboard"),
        }

    seed_dashboard_defaults()
    ensure_dashboard_token()

    stale_pid = read_pid()
    if stale_pid is not None and process_is_running(stale_pid):
        os.kill(stale_pid, signal.SIGTERM)
        for _ in range(20):
            if not process_is_running(stale_pid):
                break
            time.sleep(0.1)
    cleanup_runtime_files()

    log_path = ensure_log_dir()
    agent_name = get_home().name
    command = [sys.executable, "-m", "mystic.cli", "--agent", agent_name, "_serve"]
    log_handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=log_handle,
        cwd=str(Path.cwd()),
        start_new_session=True,
    )

    deadline = time.time() + MAX_WAIT_S
    while time.time() < deadline:
        status = probe_daemon()
        if status is not None:
            log_handle.close()
            return {
                "status": "started",
                "pid": status.get("pid"),
                "port": status.get("port"),
                "dashboard": status.get("dashboard"),
            }
        if proc.poll() is not None:
            break
        time.sleep(POLL_INTERVAL_S)

    log_handle.close()
    cleanup_runtime_files()
    tail = _tail_log()
    hint = f"\nRecent log output:\n{tail}" if tail else f"\nCheck logs at: {get_log_path()}"
    raise click.ClickException(f"Daemon did not become healthy in time.{hint}")


# ── stop command ──

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 15.0


def run_stop() -> dict[str, object]:
    existing = probe_daemon()
    if existing is None:
        pid = read_pid()
        if pid is not None and process_is_running(pid):
            os.kill(pid, signal.SIGTERM)
        cleanup_runtime_files()
        return {"status": "already_stopped", "pid": pid}

    response = asyncio.run(_send_daemon_command({"cmd": "shutdown"}))
    pid = response.get("pid") if response else existing.get("pid")

    deadline = time.time() + MAX_WAIT_S
    socket_gone = False
    while time.time() < deadline:
        if not socket_gone and probe_daemon() is None:
            socket_gone = True
        # Wait for both the control socket AND the process to exit so
        # ports (HTTP, LiveKit) are fully released before returning.
        if socket_gone and not (isinstance(pid, int) and process_is_running(pid)):
            cleanup_runtime_files()
            return {"status": "stopped", "pid": pid}
        time.sleep(POLL_INTERVAL_S)

    raise click.ClickException(f"Daemon did not stop within {int(MAX_WAIT_S)}s.")


def run_health() -> tuple[dict[str, object], int]:
    if not config_exists():
        return {"status": "down", "error": "not_initialized"}, 2

    daemon = probe_daemon()
    if daemon is None:
        return {"status": "down", "pid": None}, 2

    providers = get_providers_config()
    phone_readiness: PhoneReadiness | None = None
    if providers.twilio is not None:
        try:
            phone_readiness = asyncio.run(ensure_phone_line_ready(repair=False))
        except Exception as exc:
            reason = get_error_message(exc)
            phone_readiness = PhoneReadiness(
                status="degraded",
                twilio=CapabilityReadiness("degraded", reason),
                problems=[reason],
            )
    turn_detection = "not_configured"
    if _voice_pipeline_configured(
        getattr(providers.stt, "provider", ""),
        getattr(providers.tts, "provider", ""),
    ):
        if _turn_detector_uses_remote_inference():
            turn_detection = "ok"
        elif not is_python_package_available(TURN_DETECTOR_MODULE):
            turn_detection = "degraded"
        elif turn_detector_assets_missing():
            turn_detection = "degraded"
        else:
            turn_detection = "ok"
    subsystems = {
        "db": "ok",
        "stt": "ok",
        "tts": "ok",
        "turn_detection": turn_detection,
        "livekit": "ok",
        "twilio": phone_readiness.twilio.status if phone_readiness is not None else "not_configured",
        "smtp": "ok" if providers.smtp is not None else "not_configured",
        "tailscale": phone_readiness.tailscale.status if phone_readiness is not None else "not_configured",
        "funnel": phone_readiness.funnel.status if phone_readiness is not None else "not_configured",
        "phone": phone_readiness.status if phone_readiness is not None else "not_configured",
    }
    stats = {
        "active_calls": 0,
        "calls_today": 0,
        "pending_actions": 0,
        "people": 0,
    }
    db = open_database()
    try:
        stats["active_calls"] = len(list_active_calls(db))
        stats["calls_today"] = len(get_todays_calls(db))
        stats["pending_actions"] = len(get_all_pending_actions(db))
        stats["people"] = len(get_all_people(db, 10_000))
    finally:
        close_database(db)

    degraded = any(value in {"degraded", "offline"} for value in subsystems.values())
    started_at_raw = daemon.get("started_at")
    started_at = started_at_raw if isinstance(started_at_raw, int) else now_ms()
    payload = {
        "status": "degraded" if degraded else "healthy",
        "pid": daemon.get("pid"),
        "uptime_ms": max(now_ms() - started_at, 0),
        "port": daemon.get("port"),
        "dashboard": daemon.get("dashboard"),
        "subsystems": subsystems,
        "phone": {
            "status": phone_readiness.status,
            "reason": phone_readiness.reason(),
            "public_url": phone_readiness.public_url,
            "phone_number": phone_readiness.phone_number,
        } if phone_readiness is not None else {
            "status": "not_configured",
            "reason": "Twilio not configured",
            "public_url": None,
            "phone_number": None,
        },
        "stats": stats,
    }
    return payload, 1 if degraded else 0


# ── status command ──

def run_status(*, detail: bool = False) -> dict[str, object]:
    if not config_exists():
        return {"error": "Not initialized. Run 'mystic setup' first."}

    agent_config = get_agent_config()
    pid = read_pid()
    state = read_runtime_state()
    is_running = pid is not None and process_is_running(pid)
    if pid is not None and not is_running:
        cleanup_runtime_files()

    payload: dict[str, object] = {
        "version": "0.1.0",
        "agent": agent_config.agent.name,
        "status": "running" if is_running else "stopped",
    }
    if is_running and pid is not None:
        payload["pid"] = pid
    if is_running and state is not None:
        payload["uptime_ms"] = now_ms() - state.started_at
    if agent_config.owner.phone:
        payload["owner_phone"] = agent_config.owner.phone

    db_path = get_home() / "mystic-horizon.db"
    if not db_path.exists():
        return payload

    db = open_database()
    try:
        todays_calls = get_todays_calls(db)
        if todays_calls:
            today_descriptors = [describe_call(call) for call in todays_calls]
            inbound_count = sum(1 for item in today_descriptors if item.direction == "inbound")
            outbound_count = sum(1 for item in today_descriptors if item.direction == "outbound")
            payload["today"] = {
                "total": len(todays_calls),
                "inbound": inbound_count,
                "outbound": outbound_count,
                "directions": {
                    "inbound": inbound_count,
                    "outbound": outbound_count,
                },
                "channels": {
                    channel: sum(1 for item in today_descriptors if item.channel == channel)
                    for channel in sorted({item.channel for item in today_descriptors})
                },
                "modalities": {
                    modality: sum(1 for item in today_descriptors if item.modality == modality)
                    for modality in sorted({item.modality for item in today_descriptors})
                },
            }
        active_actions = get_actions_by_status(db, "in_progress")
        pending_actions = get_all_pending_actions(db)
        failed_actions_list = get_failed_actions(db)
        payload["actions"] = {
            "in_progress": len(active_actions),
            "pending": len(pending_actions),
            "failed": len(failed_actions_list),
        }
        if detail:
            payload["extraction"] = {
                "transcribed": _count(db, "SELECT COUNT(*) FROM calls WHERE transcript IS NOT NULL"),
                "extracted": _count(
                    db,
                    "SELECT COUNT(*) FROM calls WHERE transcript IS NOT NULL "
                    "AND facts_extracted = 1 AND commitments_extracted = 1 AND summary IS NOT NULL",
                ),
                "pending": _count(
                    db,
                    "SELECT COUNT(*) FROM calls WHERE transcript IS NOT NULL "
                    "AND (facts_extracted = 0 OR commitments_extracted = 0 OR summary IS NULL) "
                    "AND extraction_retries < 5",
                ),
                "failed": _count(db, "SELECT COUNT(*) FROM calls WHERE extraction_retries >= 5"),
            }
            payload["people"] = _count(db, "SELECT COUNT(*) FROM people")
            schema_version = get_schema_version(db)
            applied_migrations = get_applied_migrations(db)
            payload["database"] = {
                "schema_version": schema_version,
                "migrations": len(applied_migrations),
                "size_bytes": db_path.stat().st_size,
            }
    finally:
        close_database(db)
    return payload


def run_status_all(*, root: str | Path | None = None) -> dict[str, object]:
    base_dir = Path(root) if root is not None else get_shared_home()
    entries: list[str] = []
    for name in list_agent_dirs(base_dir):
        if (base_dir / name / "config" / "agent.json").exists():
            entries.append(name)

    agents: list[dict[str, object]] = []
    for name in sorted(entries):
        agent_dir = base_dir / name
        running = _agent_is_running(agent_dir)
        info: dict[str, object] = {"name": name, "status": "running" if running else "stopped"}
        port = _agent_port(agent_dir)
        phone = _agent_phone(agent_dir)
        if port:
            info["port"] = port
        if phone:
            info["phone"] = phone
        agents.append(info)
    return {"agents": agents}


def run_create_migration(name: str) -> Path:
    file_path = create_migration_file(name)
    click.echo(f"Created migration: {file_path.name}")
    return file_path




def _count(db: sqlite3.Connection, sql: str) -> int:
    row = db.execute(sql).fetchone()
    if row is None:
        return 0
    value = row[0]
    return int(value) if isinstance(value, int) else int(value or 0)


def _agent_is_running(agent_dir: Path) -> bool:
    pid_path = agent_dir / "mystic-horizon.pid"
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return process_is_running(pid)


def _agent_port(agent_dir: Path) -> int | None:
    try:
        payload = json.loads((agent_dir / "config" / "agent.json").read_text(encoding="utf-8"))
        server = payload.get("server", {})
        value = server.get("port") if isinstance(server, dict) else None
        return int(value) if isinstance(value, int) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _agent_phone(agent_dir: Path) -> str | None:
    providers_path = agent_dir / "config" / "providers.json"
    try:
        payload = json.loads(providers_path.read_text(encoding="utf-8"))
        twilio = payload.get("twilio", {})
        value = twilio.get("phoneNumber") if isinstance(twilio, dict) else None
        return value if isinstance(value, str) else None
    except (OSError, json.JSONDecodeError):
        return None


# ── dial command ──

async def run_dial(phone: str, intent: str | None) -> dict[str, object]:
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic setup' or 'mystic init' first.")
    try:
        validate_e164(phone)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    providers_config = get_providers_config()
    if providers_config.twilio is None:
        raise click.ClickException("Twilio is not configured.")

    if probe_daemon() is None:
        raise click.ClickException("Server is not running. Start it with 'mystic start'.")

    agent_config = get_agent_config()
    await _ensure_server_healthy(agent_config.server.port)
    tunnel_url = _resolve_tunnel_url(agent_config)
    if not tunnel_url:
        raise click.ClickException("Could not resolve the active tunnel URL. Restart the server and try again.")

    db = open_database()
    try:
        person = upsert_person(db, phone)
        action = insert_action(db, person_id=person.id, intent=intent or "Manual outbound call", source="cli")
        call_id = await initiate_outbound_call(db, action, tunnel_url)
    finally:
        close_database(db)

    if not call_id:
        raise click.ClickException("Failed to initiate outbound call.")
    return {"status": "dialing", "call_id": call_id, "action_id": action.id, "phone": phone, "intent": action.intent}


async def _ensure_server_healthy(port: int) -> None:
    response = await fetch_with_timeout(
        f"http://127.0.0.1:{port}/health",
        timeout_ms=5_000,
        timeout_label="cli.health",
    )
    if not 200 <= response.status_code < 300:
        raise click.ClickException(f"Server health check failed with status {response.status_code}")


def _resolve_tunnel_url(agent_config: object) -> str:
    state = read_runtime_state()
    if state is not None and state.tunnel_url:
        return state.tunnel_url.rstrip("/")
    return ""


# ── chat helpers ──


async def run_chat_json(message: str | None = None) -> list[dict[str, object]]:
    if not config_exists():
        raise click.ClickException("Not initialized. Run 'mystic setup' or 'mystic init' first.")

    db = open_database()
    history: list[dict[str, object]] = []
    responses: list[dict[str, object]] = []
    try:
        if message is not None:
            response = await run_owner_chat(db, message, history=history, source="cli")
            responses.append({"response": response})
            return responses

        if sys.stdin.isatty():
            raise click.UsageError("Provide --message or pipe input on stdin.")

        for raw_line in sys.stdin:
            text = raw_line.strip()
            if not text:
                continue
            response = await run_owner_chat(db, text, history=history, source="cli")
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": response})
            responses.append({"response": response})
    finally:
        close_database(db)
    return responses


def _serialize_item(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        payload = asdict(value)  # type: ignore[arg-type]
        if isinstance(value, (Call, CallState)):
            payload.update(interaction_payload(describe_call(value)))
        return payload
    return value


def _read_json_file(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise click.ClickException(f"Config at {path} is not a JSON object.")
    return payload


def _search_faq_locally(query: str) -> list[dict[str, object]]:
    db = open_database()
    try:
        chunks = get_all_faq_chunks(db, 500)
    finally:
        close_database(db)
    lowered = query.lower()
    return [
        cast(dict[str, object], asdict(chunk))
        for chunk in chunks
        if lowered in chunk.content.lower() or lowered in (chunk.heading or "").lower()
    ]


def _parse_cli_scalar(raw: str) -> object:
    text = raw.strip()
    if text == "null":
        return None
    if text in {"true", "false"}:
        return text == "true"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return raw


def _resolve_config_path(key: str) -> tuple[str, list[str]]:
    normalized = key.strip()
    if not normalized:
        raise click.ClickException("Config key is required.")
    if normalized.startswith("agent."):
        return "agent.json", normalized.split(".")[1:]
    if normalized.startswith("providers."):
        return "providers.json", normalized.split(".")[1:]
    if normalized.startswith("intelligence."):
        return "intelligence.json", normalized.split(".")[1:]
    for filename in ("agent.json", "providers.json", "intelligence.json"):
        payload = _read_json_file(get_home() / "config" / filename)
        parts = normalized.split(".")
        current: object = payload
        found = True
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found:
            return filename, parts
    raise click.ClickException(f"Config key not found: {key}")


def _get_nested(payload: dict[str, object], parts: list[str]) -> object:
    current: object = payload
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise click.ClickException(f"Config key not found: {'.'.join(parts)}")
        current = current[part]
    return current


def _set_nested(payload: dict[str, object], parts: list[str], value: object) -> None:
    current: dict[str, object] = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = cast(dict[str, object], child)
    current[parts[-1]] = value


# ── CLI entrypoint ──

@dataclass(slots=True)
class PreparedCliArgs:
    agent_name: str | None
    click_args: list[str]


class AgentAwareGroup(click.Group):
    def main(self, *args: Any, **kwargs: Any) -> Any:
        raw_args = kwargs.pop("args", None)
        if raw_args is None:
            raw_args = list(sys.argv[1:])
        prepared = prepare_cli_args(list(raw_args))
        kwargs["args"] = prepared.click_args
        return super().main(*args, **kwargs)


def parse_agent_flag(argv: list[str]) -> tuple[str | None, list[str]]:
    agent_name: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        current = argv[index]
        if current == "--agent":
            if index + 1 >= len(argv):
                raise click.UsageError("--agent requires a name")
            agent_name = argv[index + 1].strip()
            index += 2
            continue
        if current.startswith("--agent="):
            agent_name = current.split("=", 1)[1].strip()
            index += 1
            continue
        remaining.append(current)
        index += 1
    if agent_name is not None and not agent_name:
        raise click.UsageError("--agent requires a name")
    return agent_name, remaining


def prepare_cli_args(argv: list[str]) -> PreparedCliArgs:
    explicit_agent, remaining = parse_agent_flag(argv)
    effective_agent = explicit_agent or os.environ.get("MH_AGENT", "").strip() or None
    click_args = ["--agent", explicit_agent, *remaining] if explicit_agent else remaining
    return PreparedCliArgs(agent_name=effective_agent, click_args=click_args)


@click.group(
    cls=AgentAwareGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option("--agent", metavar="NAME", envvar="MH_AGENT", help="Agent instance name (or set MH_AGENT).")
@click.pass_context
def cli(ctx: click.Context, agent: str | None) -> None:
    """Mystic Horizon local phone agent."""
    ctx.ensure_object(dict)
    ctx.obj["agent_name"] = agent.strip() if agent else None
    if agent:
        _apply_agent_env(agent)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("setup")
@click.option("--port", type=int, help="Preferred dashboard/server port.")
@click.pass_context
def setup_command(ctx: click.Context, port: int | None) -> None:
    agent_name = str(ctx.obj.get("agent_name") or "").strip() or None
    result = asyncio.run(run_setup(agent_name=agent_name, port=port))
    _emit_json(result)


@cli.command("init")
@click.option("--connect-twilio", is_flag=True, help="Add Twilio phone support to an existing agent.")
@click.option("--connect-calendar", is_flag=True, help="Add ICS calendar sync to an existing agent.")
@click.option("--connect-hub-calendar", is_flag=True, help="Connect a calendar for event write-back.")
@click.option("--connect-smtp", is_flag=True, help="Connect SMTP for outbound email.")
@click.option("--advanced", is_flag=True, help="Full init with all provider and owner options.")
@click.pass_context
def init_command(
    ctx: click.Context,
    connect_twilio: bool,
    connect_calendar: bool,
    connect_hub_calendar: bool,
    connect_smtp: bool,
    advanced: bool,
) -> None:
    _require_agent(ctx)
    selected_connectors = sum(
        bool(flag) for flag in (connect_twilio, connect_calendar, connect_hub_calendar, connect_smtp)
    )
    if selected_connectors > 1:
        raise click.UsageError(
            "Choose only one of --connect-twilio, --connect-calendar, --connect-hub-calendar, or --connect-smtp."
        )
    if connect_twilio:
        asyncio.run(run_connect_twilio())
        return
    if connect_calendar:
        asyncio.run(run_connect_calendar())
        return
    if connect_hub_calendar:
        asyncio.run(run_connect_hub_calendar())
        return
    if connect_smtp:
        asyncio.run(run_connect_smtp())
        return
    asyncio.run(run_init(advanced=advanced))


@cli.command("start")
@click.pass_context
def start_command(ctx: click.Context) -> None:
    _require_agent(ctx)
    _emit_json(run_start())


@cli.command("dev")
@click.option("--skip-voice", is_flag=True, help="Skip LiveKit startup (text chat only).")
@click.pass_context
def dev_command(ctx: click.Context, skip_voice: bool) -> None:
    _require_agent(ctx)
    asyncio.run(run_dev(skip_voice=skip_voice))


@cli.command("stop")
@click.pass_context
def stop_command(ctx: click.Context) -> None:
    _require_agent(ctx)
    _emit_json(run_stop())


@cli.command("health")
@click.pass_context
def health_command(ctx: click.Context) -> None:
    _require_agent(ctx)
    payload, exit_code = run_health()
    _emit_json(payload)
    if exit_code != 0:
        raise click.exceptions.Exit(exit_code)


@cli.command("status")
@click.option("--all", "all_agents", is_flag=True, help="List every initialized agent.")
@click.option("--detail", is_flag=True, help="Show extraction and database detail.")
@click.pass_context
def status_command(ctx: click.Context, all_agents: bool, detail: bool) -> None:
    if all_agents:
        _emit_json(run_status_all())
        return
    _require_agent(ctx)
    _emit_json(run_status(detail=detail))


@cli.command("create_migration")
@click.argument("name", nargs=-1)
@click.pass_context
def create_migration_command(ctx: click.Context, name: tuple[str, ...]) -> None:
    _require_agent(ctx)
    migration_name = " ".join(name).strip()
    if not migration_name:
        raise click.UsageError("Provide a migration name.")
    run_create_migration(migration_name)


@cli.command("dial")
@click.argument("phone")
@click.argument("intent", nargs=-1)
@click.pass_context
def dial_command(ctx: click.Context, phone: str, intent: tuple[str, ...]) -> None:
    _require_agent(ctx)
    _emit_json(asyncio.run(run_dial(phone, " ".join(intent).strip() or None)))


@cli.command("sms")
@click.argument("phone")
@click.option("--body", required=True, help="SMS body text.")
@click.pass_context
def sms_command(ctx: click.Context, phone: str, body: str) -> None:
    _require_agent(ctx)
    providers = get_providers_config()
    if providers.twilio is None:
        _emit_json({"error": "Twilio is not configured."})
        raise click.exceptions.Exit(2)
    sid = asyncio.run(send_sms(providers.twilio, phone, body))
    _emit_json({"status": "sent", "sid": sid, "phone": phone})


@cli.command("email")
@click.argument("addr")
@click.option("--subject", required=True)
@click.option("--body", required=True)
@click.pass_context
def email_command(ctx: click.Context, addr: str, subject: str, body: str) -> None:
    _require_agent(ctx)
    asyncio.run(send_email(addr, subject, body))
    _emit_json({"status": "sent", "to": addr, "subject": subject})


@cli.command("chat")
@click.option("--message", type=str, help="Single-shot owner message.")
@click.pass_context
def chat_command(ctx: click.Context, message: str | None) -> None:
    """Scriptable text chat."""
    _require_agent(ctx)
    results = asyncio.run(run_chat_json(message))
    for item in results:
        _emit_json(item)


@cli.command("_serve", hidden=True)
@click.pass_context
def serve_command(ctx: click.Context) -> None:
    _require_agent(ctx)
    asyncio.run(run_serve())


@cli.group("dashboard")
@click.pass_context
def dashboard_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@dashboard_group.command("files")
def dashboard_files_command() -> None:
    _emit_json({"files": list_dashboard_files()})


@dashboard_group.command("read")
@click.argument("filename")
def dashboard_read_command(filename: str) -> None:
    try:
        content = read_dashboard_file(filename)
    except FileNotFoundError as exc:
        _emit_json({"error": str(exc)})
        raise click.exceptions.Exit(1) from exc
    _emit_json({"filename": filename, "content": content})


@dashboard_group.command("token")
def dashboard_token_command() -> None:
    config = get_dashboard_config()
    token = config.token if config is not None else ensure_dashboard_token()
    _emit_json({"token": token})


@cli.group("people")
@click.pass_context
def people_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@people_group.command("list")
@click.option("--limit", type=int, default=50, show_default=True)
def people_list_command(limit: int) -> None:
    db = open_database()
    try:
        payload = [_serialize_item(person) for person in get_all_people(db, max(limit, 1))]
    finally:
        close_database(db)
    _emit_json({"people": payload})


@people_group.command("get")
@click.argument("person_id")
def people_get_command(person_id: str) -> None:
    db = open_database()
    try:
        person = get_person_by_id(db, person_id)
        if person is None:
            _emit_json({"error": f"Person not found: {person_id}"})
            raise click.exceptions.Exit(1)
        facts = [_serialize_item(fact) for fact in get_all_active_facts_by_person(db, person_id, 100)]
        calls = [_serialize_item(call) for call in get_recent_calls_by_person(db, person_id, 20)]
    finally:
        close_database(db)
    _emit_json({"person": _serialize_item(person), "facts": facts, "calls": calls})


@people_group.command("search")
@click.argument("query")
def people_search_command(query: str) -> None:
    db = open_database()
    try:
        from mystic.db import find_people

        payload = [_serialize_item(person) for person in find_people(db, query)]
    finally:
        close_database(db)
    _emit_json({"people": payload})


@cli.group("calls")
@click.pass_context
def calls_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@calls_group.command("list")
@click.option("--today", is_flag=True, help="Only show calls from today.")
@click.option("--limit", type=int, default=50, show_default=True)
def calls_list_command(today: bool, limit: int) -> None:
    db = open_database()
    try:
        calls = get_todays_calls(db) if today else get_recent_calls(db, max(limit, 1))
    finally:
        close_database(db)
    _emit_json({"calls": [_serialize_item(call) for call in calls[: max(limit, 1)]]})


@calls_group.command("get")
@click.argument("call_id")
def calls_get_command(call_id: str) -> None:
    db = open_database()
    try:
        call = get_call_by_id(db, call_id)
    finally:
        close_database(db)
    if call is None:
        _emit_json({"error": f"Call not found: {call_id}"})
        raise click.exceptions.Exit(1)
    _emit_json({"call": _serialize_item(call)})


@calls_group.command("active")
def calls_active_command() -> None:
    db = open_database()
    try:
        active = [_serialize_item(call) for call in list_active_calls(db)]
    finally:
        close_database(db)
    _emit_json({"calls": active})


@cli.group("actions")
@click.pass_context
def actions_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@actions_group.command("list")
@click.option("--status", type=str, default="pending", show_default=True)
def actions_list_command(status: str) -> None:
    db = open_database()
    try:
        if status == "pending":
            actions = get_all_pending_actions(db)
        else:
            actions = get_actions_by_status(db, cast(Any, status))
    finally:
        close_database(db)
    _emit_json({"actions": [_serialize_item(action) for action in actions]})


@actions_group.command("get")
@click.argument("action_id")
def actions_get_command(action_id: str) -> None:
    db = open_database()
    try:
        action = get_action_by_id(db, action_id)
    finally:
        close_database(db)
    if action is None:
        _emit_json({"error": f"Action not found: {action_id}"})
        raise click.exceptions.Exit(1)
    _emit_json({"action": _serialize_item(action)})


@actions_group.command("create")
@click.option("--person", "person_id", required=True, help="Person ID.")
@click.option("--intent", required=True, help="Action intent.")
@click.option("--due", type=int, help="Due time in epoch milliseconds.")
@click.option("--urgency", type=click.Choice(["normal", "high"]), default="normal", show_default=True)
def actions_create_command(person_id: str, intent: str, due: int | None, urgency: str) -> None:
    db = open_database()
    try:
        action = insert_action(
            db,
            person_id=person_id,
            intent=intent,
            due_at=due,
            urgency=cast(ActionUrgency, urgency),
            source="cli",
        )
    finally:
        close_database(db)
    _emit_json({"action": _serialize_item(action)})


@actions_group.command("complete")
@click.argument("action_id")
@click.option("--result", type=str, default="", help="Completion result.")
def actions_complete_command(action_id: str, result: str) -> None:
    db = open_database()
    try:
        if get_action_by_id(db, action_id) is None:
            _emit_json({"error": f"Action not found: {action_id}"})
            raise click.exceptions.Exit(1)
        from mystic.db import update_action_status

        update_action_status(db, action_id, "completed", result or "Completed from CLI")
    finally:
        close_database(db)
    _emit_json({"ok": True, "id": action_id, "status": "completed"})


@actions_group.command("cancel")
@click.argument("action_id")
@click.option("--reason", type=str, default="", help="Cancellation reason.")
def actions_cancel_command(action_id: str, reason: str) -> None:
    db = open_database()
    try:
        if get_action_by_id(db, action_id) is None:
            _emit_json({"error": f"Action not found: {action_id}"})
            raise click.exceptions.Exit(1)
        from mystic.db import update_action_status

        update_action_status(db, action_id, "cancelled", reason or "Cancelled from CLI")
    finally:
        close_database(db)
    _emit_json({"ok": True, "id": action_id, "status": "cancelled"})


@cli.group("facts")
@click.pass_context
def facts_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@facts_group.command("list")
@click.option("--person", "person_id", required=True, help="Person ID.")
def facts_list_command(person_id: str) -> None:
    db = open_database()
    try:
        facts = [_serialize_item(fact) for fact in get_all_active_facts_by_person(db, person_id, 200)]
    finally:
        close_database(db)
    _emit_json({"facts": facts})


@facts_group.command("search")
@click.argument("query")
def facts_search_command(query: str) -> None:
    db = open_database()
    try:
        facts = [_serialize_item(fact) for fact in search_facts(db, query, limit=200)]
    finally:
        close_database(db)
    _emit_json({"facts": facts})


@facts_group.command("add")
@click.option("--person", "person_id", required=True, help="Person ID.")
@click.option("--content", required=True, help="Fact content.")
@click.option(
    "--type",
    "fact_type",
    type=click.Choice(["identity", "preference", "relationship", "context"]),
    required=True,
)
def facts_add_command(person_id: str, content: str, fact_type: str) -> None:
    db = open_database()
    try:
        fact = insert_fact(
            db,
            person_id=person_id,
            type=cast(FactType, fact_type),
            content=content,
            confidence=1.0,
            source="cli",
        )
    finally:
        close_database(db)
    _emit_json({"fact": _serialize_item(fact)})


@cli.group("calendar")
@click.pass_context
def calendar_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@calendar_group.command("list")
@click.option("--from", "start_ms", type=int)
@click.option("--to", "end_ms", type=int)
def calendar_list_command(start_ms: int | None, end_ms: int | None) -> None:
    db = open_database()
    try:
        if start_ms is not None and end_ms is not None:
            events = get_external_events_in_range(db, start_ms, end_ms, limit=200)
        else:
            events = get_recent_external_events(db, 200)
    finally:
        close_database(db)
    _emit_json({"events": [_serialize_item(event) for event in events]})


@calendar_group.command("get")
@click.argument("event_id")
def calendar_get_command(event_id: str) -> None:
    db = open_database()
    try:
        event = get_external_event_by_id(db, event_id)
    finally:
        close_database(db)
    if event is None:
        _emit_json({"error": f"Calendar item not found: {event_id}"})
        raise click.exceptions.Exit(1)
    _emit_json({"event": _serialize_item(event)})


@cli.group("identity")
@click.pass_context
def identity_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@identity_group.command("show")
def identity_show_command() -> None:
    _emit_json({"content": read_identity_raw(), "path": str(get_identity_path())})


@cli.group("soul")
@click.pass_context
def soul_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@soul_group.command("show")
def soul_show_command() -> None:
    _emit_json({"content": read_soul(), "path": str(get_soul_path())})


@cli.group("journal")
@click.pass_context
def journal_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@journal_group.command("list")
@click.option("--type", "file_type", type=click.Choice(["identity", "soul"]))
@click.option("--limit", type=int, default=20, show_default=True)
def journal_list_command(file_type: str | None, limit: int) -> None:
    selected = file_type or "soul"
    entries = [
        {
            "timestamp": entry.timestamp,
            "file_type": entry.file_type,
            "trigger": entry.trigger,
            "note": entry.note,
        }
        for entry in list_journal_entries(selected, limit=max(limit, 1))
    ]
    _emit_json({"entries": entries, "path": str(get_journal_dir(selected))})


@cli.group("faq")
@click.pass_context
def faq_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@faq_group.command("list")
def faq_list_command() -> None:
    db = open_database()
    try:
        chunks = [_serialize_item(chunk) for chunk in get_all_faq_chunks(db, 200)]
    finally:
        close_database(db)
    _emit_json({"faq": chunks})


@faq_group.command("search")
@click.argument("query")
def faq_search_command(query: str) -> None:
    _emit_json({"faq": _search_faq_locally(query)})


@cli.group("config")
@click.pass_context
def config_group(ctx: click.Context) -> None:
    _require_agent(ctx)


@config_group.command("show")
def config_show_command() -> None:
    _emit_json(
        {
            "agent": _read_json_file(get_home() / "config" / "agent.json"),
            "providers": _read_json_file(get_home() / "config" / "providers.json"),
            "intelligence": _read_json_file(get_home() / "config" / "intelligence.json"),
        }
    )


@config_group.command("get")
@click.argument("key")
def config_get_command(key: str) -> None:
    filename, parts = _resolve_config_path(key)
    payload = _read_json_file(get_home() / "config" / filename)
    _emit_json({"file": filename, "key": ".".join(parts), "value": _get_nested(payload, parts)})


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set_command(key: str, value: str) -> None:
    filename, parts = _resolve_config_path(key)
    path = get_home() / "config" / filename
    payload = _read_json_file(path)
    _set_nested(payload, parts, _parse_cli_scalar(value))
    write_config(filename, payload)
    refreshed = _read_json_file(path)
    _emit_json({"file": filename, "key": ".".join(parts), "value": _get_nested(refreshed, parts)})


def _apply_agent_env(agent_name: str) -> None:
    try:
        validated = validate_agent_name(agent_name)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    os.environ["MH_AGENT"] = validated
    os.environ["APP_HOME"] = str(resolve_agent_home(validated))


def _require_agent(ctx: click.Context) -> str:
    agent_name = str(ctx.obj.get("agent_name") or "").strip()
    if not agent_name:
        raise click.UsageError("Missing --agent <name> flag (or set MH_AGENT)")
    return agent_name


if __name__ == "__main__":
    cli()
