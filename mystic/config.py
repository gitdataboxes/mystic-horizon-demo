"""Flat config module — paths, config loading, identity, soul, logger, and deps."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Mapping, cast, overload

from mystic.types import Identity, JournalEntry, LogEntry, LogLevel

# ── Event emission ───────────────────────────────────────────────────────────

EventListener = Any  # Callable[[str, Mapping[str, object]], None]
_event_listeners: list[EventListener] = []


def register_event_listener(fn: EventListener) -> None:
    """Register a sync listener called for every emit_event()."""
    if fn not in _event_listeners:
        _event_listeners.append(fn)


def emit_event(event: str, data: Mapping[str, object]) -> None:
    """Broadcast an event to all registered listeners (sync, safe from any context)."""
    for fn in _event_listeners:
        fn(event, data)


# ── Paths ────────────────────────────────────────────────────────────────────

AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def get_shared_home() -> Path:
    """Return the shared root used for binaries and agent homes."""
    return Path.home() / ".mystic-horizon"


def get_home() -> Path:
    """Return the current agent home, honoring APP_HOME when set."""
    raw_home = os.environ.get("APP_HOME")
    if raw_home:
        return Path(raw_home).expanduser()
    return get_shared_home()


APP_HOME = get_home()


def is_valid_agent_name(name: str) -> bool:
    return bool(AGENT_NAME_RE.fullmatch(name))


def validate_agent_name(name: str) -> str:
    if not is_valid_agent_name(name):
        raise ValueError(f"Invalid agent name: {name}")
    return name


def resolve_agent_home(agent_name: str) -> Path:
    return get_shared_home() / validate_agent_name(agent_name)


# ── Logger ───────────────────────────────────────────────────────────────────

COLORS: dict[LogLevel, str] = {
    "debug": "\x1b[90m",
    "info": "\x1b[36m",
    "warn": "\x1b[33m",
    "error": "\x1b[31m",
}
RESET = "\x1b[0m"
DIM = "\x1b[2m"

_silenced = False
_file_lock = Lock()
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def bind_trace_id(call_id: str) -> None:
    """Bind a call UUID to the current async task's log context."""
    _trace_id_var.set(call_id)


def clear_trace_id() -> None:
    _trace_id_var.set(None)


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def silence_stdout(silent: bool = True) -> None:
    global _silenced
    _silenced = silent


def get_error_message(err: object) -> str:
    if isinstance(err, BaseException):
        return str(err)
    return str(err)


def get_log_path() -> Path:
    return get_home() / "logs" / "mystic-horizon.log"


def ensure_log_dir() -> Path:
    path = get_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def _format_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _serialize_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


class _Logger:
    def log(self, level: LogLevel, event: str, **data: object) -> None:
        trace_id = _trace_id_var.get()
        if trace_id is not None and "traceId" not in data:
            data = {"traceId": trace_id, **data}
        entry = LogEntry(
            ts=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            level=level,
            event=event,
            data=dict(data),
        )
        self._write_file(entry)
        self._write_stdout(entry)

    def debug(self, event: str, **data: object) -> None:
        self.log("debug", event, **data)

    def info(self, event: str, **data: object) -> None:
        self.log("info", event, **data)

    def warn(self, event: str, **data: object) -> None:
        self.log("warn", event, **data)

    def error(self, event: str, **data: object) -> None:
        self.log("error", event, **data)

    def _write_file(self, entry: LogEntry) -> None:
        path = ensure_log_dir()
        try:
            with _file_lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(asdict(entry), sort_keys=True, default=str) + "\n")
        except OSError:
            pass

    def _write_stdout(self, entry: LogEntry) -> None:
        if _silenced:
            return
        color = COLORS[entry.level]
        details = " ".join(
            f"{key}={_serialize_value(value)}"
            for key, value in entry.data.items()
            if value not in (None, "")
        )
        suffix = f" {DIM}{details}{RESET}" if details else ""
        print(f"{DIM}{_format_time()}{RESET} {color}{entry.event}{RESET}{suffix}")


logger = _Logger()


# ── Deps ─────────────────────────────────────────────────────────────────────

def is_python_package_available(import_name: str) -> bool:
    """Check whether a Python package is importable."""
    return importlib.util.find_spec(import_name) is not None


async def ensure_python_extra(
    import_name: str,
    pip_name: str,
    *,
    label: str | None = None,
) -> None:
    """Install a pip package if it is not already importable."""
    if is_python_package_available(import_name):
        return

    display = label or pip_name
    logger.info("deps.installing", package=display)

    result = await asyncio.to_thread(_pip_install, pip_name)
    if result != 0:
        raise RuntimeError(
            f"Failed to install {display}. "
            f"Try manually: {sys.executable} -m pip install {pip_name}"
        )

    importlib.invalidate_caches()

    if not is_python_package_available(import_name):
        raise RuntimeError(
            f"Installed {display} but it is still not importable. "
            f"Check your environment."
        )
    logger.info("deps.installed", package=display)


def _pip_install(pip_name: str) -> int:
    return subprocess.call(
        [sys.executable, "-m", "pip", "install", "-q", pip_name],
    )


# ── Identity ─────────────────────────────────────────────────────────────────

FIELD_PATTERNS = {
    "name": re.compile(r"\*\*Name:\*\*\s*(.+)"),
    "creature": re.compile(r"\*\*Creature:\*\*\s*(.+)"),
    "vibe": re.compile(r"\*\*Vibe:\*\*\s*(.+)"),
    "emoji": re.compile(r"\*\*Emoji:\*\*\s*(.+)"),
}


def get_identity_path() -> Path:
    return get_home() / "IDENTITY.md"


def identity_exists() -> bool:
    return get_identity_path().exists()


def parse_identity(raw: str) -> Identity:
    values: dict[str, str] = {}
    for field_name, pattern in FIELD_PATTERNS.items():
        match = pattern.search(raw)
        if match:
            values[field_name] = match.group(1).strip()

    return Identity(
        name=values.get("name", ""),
        creature=values.get("creature", ""),
        vibe=values.get("vibe", ""),
        emoji=values.get("emoji", ""),
    )


def format_identity(identity: Identity) -> str:
    return "\n".join(
        [
            "# Identity",
            "",
            f"- **Name:** {identity.name}",
            f"- **Creature:** {identity.creature}",
            f"- **Vibe:** {identity.vibe}",
            f"- **Emoji:** {identity.emoji}",
            "",
        ]
    )


def read_identity() -> Identity:
    return parse_identity(read_identity_raw())


def read_identity_raw() -> str:
    path = get_identity_path()
    if not path.exists():
        raise FileNotFoundError(f"IDENTITY.md not found at {path}")
    return path.read_text(encoding="utf-8")


def write_identity(
    identity: Identity,
    *,
    trigger: str = "write-identity",
    note: str = "",
) -> None:
    home = get_home()
    home.mkdir(parents=True, exist_ok=True)
    path = get_identity_path()
    if path.exists():
        _save_journal_entry(
            "identity",
            path.read_text(encoding="utf-8"),
            trigger=trigger,
            note=note,
        )
    path.write_text(format_identity(identity), encoding="utf-8")
    logger.info("identity.written", path=str(path), name=identity.name)


# ── Soul ─────────────────────────────────────────────────────────────────────

def get_soul_path() -> Path:
    return get_home() / "SOUL.md"


def soul_exists() -> bool:
    return get_soul_path().exists()


def read_soul() -> str:
    path = get_soul_path()
    if not path.exists():
        raise FileNotFoundError(f"SOUL.md not found at {path}")
    return path.read_text(encoding="utf-8")


_JOURNAL_FILE_TYPES = frozenset({"soul", "identity"})


def _normalize_journal_file_type(file_type: str) -> str:
    normalized = file_type.strip().lower()
    if normalized not in _JOURNAL_FILE_TYPES:
        raise ValueError(f"Unsupported journal file type: {file_type}")
    return normalized


def get_journal_dir(file_type: str) -> Path:
    return get_home() / "journal" / _normalize_journal_file_type(file_type)


def _save_journal_entry(file_type: str, content: str, trigger: str, note: str = "") -> None:
    journal_dir = get_journal_dir(file_type)
    journal_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time() * 1000)
    path = journal_dir / f"{timestamp}.md"
    while path.exists():
        timestamp += 1
        path = journal_dir / f"{timestamp}.md"

    cleaned_trigger = trigger.strip() or f"write-{file_type}"
    cleaned_note = " ".join(str(note).strip().splitlines())
    payload = (
        "---\n"
        f"timestamp: {timestamp}\n"
        f"trigger: {cleaned_trigger}\n"
        f"note: {cleaned_note}\n"
        "---\n\n"
        f"{content}"
    )
    path.write_text(payload, encoding="utf-8")
    logger.info("journal.entry-saved", path=str(path), fileType=file_type, trigger=cleaned_trigger)


def _parse_journal_file(path: Path, file_type: str) -> JournalEntry | None:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return None

    rest = raw[4:]
    delimiter = "\n---\n"
    marker = rest.find(delimiter)
    if marker < 0:
        return None

    frontmatter = rest[:marker]
    content = rest[marker + len(delimiter) :]
    if content.startswith("\n"):
        content = content[1:]

    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip()

    timestamp_text = fields.get("timestamp", path.stem)
    try:
        timestamp = int(timestamp_text)
    except ValueError:
        return None

    return JournalEntry(
        timestamp=timestamp,
        file_type=_normalize_journal_file_type(file_type),
        trigger=fields.get("trigger", ""),
        note=fields.get("note", ""),
        content=content,
    )


def list_journal_entries(file_type: str, *, limit: int = 20) -> list[JournalEntry]:
    if limit <= 0:
        return []

    journal_dir = get_journal_dir(file_type)
    if not journal_dir.exists():
        return []

    entries: list[JournalEntry] = []
    normalized_type = _normalize_journal_file_type(file_type)
    for path in journal_dir.glob("*.md"):
        entry = _parse_journal_file(path, normalized_type)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda entry: entry.timestamp, reverse=True)
    return entries[:limit]


def read_journal_entry(file_type: str, timestamp: int) -> JournalEntry | None:
    path = get_journal_dir(file_type) / f"{timestamp}.md"
    if not path.exists():
        return None
    return _parse_journal_file(path, file_type)


def write_soul(
    content: str,
    *,
    trigger: str = "write-soul",
    note: str = "",
) -> None:
    home = get_home()
    home.mkdir(parents=True, exist_ok=True)
    soul_path = get_soul_path()
    if soul_path.exists():
        _save_journal_entry(
            "soul",
            soul_path.read_text(encoding="utf-8"),
            trigger=trigger,
            note=note,
        )
    soul_path.write_text(content, encoding="utf-8")
    logger.info("soul.written", path=str(soul_path))


# ── Config ───────────────────────────────────────────────────────────────────

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_REALTIME_MODEL = "openai/gpt-5.5"
DEFAULT_LOCAL_EMBEDDING_MODEL = "nomic-embed-text-v1.5"
DEFAULT_LOCAL_EMBEDDING_DIMENSIONS = 256
CONFIG_FILENAMES = frozenset({"agent.json", "providers.json", "intelligence.json"})
ConfigFilename = Literal["agent.json", "providers.json", "intelligence.json"]


@dataclass(slots=True)
class AgentOwnerConfig:
    phone: str | None = None


@dataclass(slots=True)
class AgentDetailsConfig:
    name: str
    voiceId: str | None = None


@dataclass(slots=True)
class AgentHoursConfig:
    start: int
    end: int
    timezone: str
    days: list[str]


@dataclass(slots=True)
class AgentServerConfig:
    port: int
    maxActiveJobs: int | None = None


@dataclass(slots=True)
class AgentTunnelConfig:
    enabled: bool = True


@dataclass(slots=True)
class AgentRecordingConfig:
    enabled: bool = False


@dataclass(slots=True)
class AgentConfig:
    owner: AgentOwnerConfig
    agent: AgentDetailsConfig
    hours: AgentHoursConfig
    server: AgentServerConfig
    tunnel: AgentTunnelConfig
    recording: AgentRecordingConfig = field(default_factory=AgentRecordingConfig)


@dataclass(slots=True)
class TwilioConfig:
    accountSid: str
    authToken: str
    phoneNumber: str
    phoneNumberSid: str | None = None


@dataclass(slots=True)
class TwilioDraftConfig:
    accountSid: str
    authToken: str


@dataclass(slots=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    use_tls: bool = True


@dataclass(slots=True)
class LiveKitConfig:
    host: str
    port: int
    apiKey: str
    apiSecret: str


@dataclass(slots=True)
class MoonshineSttConfig:
    provider: str
    model: str


@dataclass(slots=True)
class DeepgramSttConfig:
    provider: str
    apiKey: str
    model: str | None = None


@dataclass(slots=True)
class UnconfiguredSttConfig:
    provider: str = ""


SttConfig = MoonshineSttConfig | DeepgramSttConfig | UnconfiguredSttConfig


@dataclass(slots=True)
class PocketTtsConfig:
    provider: str
    model: str | None = None
    pythonCommand: str | None = None


@dataclass(slots=True)
class InworldTtsConfig:
    provider: str
    apiKey: str
    model: str | None = None


@dataclass(slots=True)
class UnconfiguredTtsConfig:
    provider: str = ""


TtsConfig = PocketTtsConfig | InworldTtsConfig | UnconfiguredTtsConfig


@dataclass(slots=True)
class LocalEmbeddingConfig:
    provider: str
    model: str
    dimensions: int


@dataclass(slots=True)
class CalendarSubscription:
    url: str
    label: str | None = None


@dataclass(slots=True)
class CalendarHubConfig:
    provider: str
    calendar_id: str
    client_id: str | None = None
    client_secret: str | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    write_enabled: bool = True


@dataclass(slots=True)
class CalendarConfig:
    subscriptions: list[CalendarSubscription] = field(default_factory=list)
    sync_interval_minutes: int = 15
    reminder_minutes: int = 10
    hub: CalendarHubConfig | None = None


@dataclass(slots=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: int
    token_type: str = "Bearer"


@dataclass(slots=True)
class OpenRouterConfig:
    apiKey: str


@dataclass(slots=True)
class DashboardConfig:
    token: str


@dataclass(slots=True)
class LLMSlotConfig:
    provider: str
    baseURL: str | None = None
    model: str | None = None
    apiKey: str | None = None


@dataclass(slots=True)
class LLMConfig:
    realtime: LLMSlotConfig | None = None
    backend: LLMSlotConfig | None = None


@dataclass(slots=True)
class ProvidersConfig:
    livekit: LiveKitConfig
    stt: SttConfig
    tts: TtsConfig
    embedding: LocalEmbeddingConfig
    dashboard: DashboardConfig | None = None
    calendar: CalendarConfig | None = None
    twilio: TwilioConfig | None = None
    twilioDraft: TwilioDraftConfig | None = None
    smtp: SmtpConfig | None = None
    openrouter: OpenRouterConfig | None = None
    llm: LLMConfig | None = None


@dataclass(slots=True)
class SetupStatus:
    identity: bool
    soul: bool
    tailscale_installed: bool
    tailscale_reason: str
    twilio: bool

    @property
    def core_complete(self) -> bool:
        return self.identity and self.soul


@dataclass(slots=True)
class ModelRef:
    model: str


@dataclass(slots=True)
class ExtractionModels:
    facts: ModelRef
    commitments: ModelRef


@dataclass(slots=True)
class JudgmentModels:
    scheduler: ModelRef
    satisfaction: ModelRef
    owner_call: ModelRef


@dataclass(slots=True)
class SummarizationModels:
    person: ModelRef
    call: ModelRef


@dataclass(slots=True)
class RetrievalConfig:
    vectorWeight: float
    ftsWeight: float
    threshold: float
    limit: int


@dataclass(slots=True)
class IntelligenceConfig:
    extraction: ExtractionModels
    judgment: JudgmentModels
    summarization: SummarizationModels
    editing: ModelRef
    search: ModelRef
    retrieval: RetrievalConfig


@dataclass(slots=True)
class ResolvedLLMConfig:
    baseURL: str
    apiKey: str | None
    model: str


@dataclass(slots=True)
class ResolvedBackendLLMConfig:
    baseURL: str
    apiKey: str | None
    isCustom: bool


ConfigValue = AgentConfig | ProvidersConfig | IntelligenceConfig

_config_cache: dict[str, ConfigValue] = {}


def get_config_dir() -> Path:
    return get_home() / "config"


def get_recordings_dir() -> Path:
    return get_home() / "recordings"


def get_dashboard_dir() -> Path:
    return get_home() / "dashboard"


def get_dashboard_pages_dir() -> Path:
    return get_dashboard_dir() / "pages"


def get_dashboard_history_dir() -> Path:
    return get_dashboard_dir() / ".history"


def get_dashboard_style_path() -> Path:
    return get_dashboard_dir() / "style.css"


def get_dashboard_manifest_path() -> Path:
    return get_dashboard_dir() / "manifest.json"


def get_daemon_socket_path() -> Path:
    return get_home() / "mystic.sock"


def is_valid_e164(phone: str) -> bool:
    return bool(E164_RE.fullmatch(phone))


def validate_e164(phone: str) -> str:
    if not is_valid_e164(phone):
        raise ValueError(f"Invalid E.164 phone number: {phone}")
    return phone


def config_exists() -> bool:
    return (get_config_dir() / "agent.json").exists()


def clear_config_cache(filename: str | None = None) -> None:
    if filename is None:
        _config_cache.clear()
        return
    _config_cache.pop(_validate_filename(filename), None)


@overload
def load_config(filename: Literal["agent.json"]) -> AgentConfig: ...


@overload
def load_config(filename: Literal["providers.json"]) -> ProvidersConfig: ...


@overload
def load_config(filename: Literal["intelligence.json"]) -> IntelligenceConfig: ...


def load_config(filename: str) -> ConfigValue:
    validated_name = _validate_filename(filename)
    cached = _config_cache.get(validated_name)
    if cached is not None:
        return cached

    file_path = get_config_dir() / validated_name
    if not file_path.exists():
        raise FileNotFoundError(f"Config file not found: {file_path}")

    raw = json.loads(file_path.read_text(encoding="utf-8"))
    parsed = _parse_config(validated_name, raw)
    _config_cache[validated_name] = parsed
    return parsed


@overload
async def load_config_async(filename: Literal["agent.json"]) -> AgentConfig: ...


@overload
async def load_config_async(filename: Literal["providers.json"]) -> ProvidersConfig: ...


@overload
async def load_config_async(filename: Literal["intelligence.json"]) -> IntelligenceConfig: ...


async def load_config_async(filename: str) -> ConfigValue:
    validated_name = _validate_filename(filename)
    return load_config(validated_name)


@overload
def load_config_fresh(filename: Literal["agent.json"]) -> AgentConfig: ...


@overload
def load_config_fresh(filename: Literal["providers.json"]) -> ProvidersConfig: ...


@overload
def load_config_fresh(filename: Literal["intelligence.json"]) -> IntelligenceConfig: ...


def load_config_fresh(filename: str) -> ConfigValue:
    validated_name = _validate_filename(filename)
    clear_config_cache(validated_name)
    return load_config(validated_name)


def write_config(filename: str, data: ConfigValue | Mapping[str, Any]) -> None:
    validated_name = _validate_filename(filename)
    raw_data = _serialize_config(data)
    parsed = _parse_config(validated_name, raw_data)
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / validated_name).write_text(json.dumps(raw_data, indent=2) + "\n", encoding="utf-8")
    _config_cache[validated_name] = parsed


def get_agent_config() -> AgentConfig:
    return load_config("agent.json")


def get_providers_config() -> ProvidersConfig:
    return load_config("providers.json")


def get_intelligence_config() -> IntelligenceConfig:
    return load_config("intelligence.json")


def get_twilio_config() -> TwilioConfig | None:
    return get_providers_config().twilio


def get_smtp_config() -> SmtpConfig | None:
    return get_providers_config().smtp


def get_livekit_config() -> LiveKitConfig:
    return get_providers_config().livekit


def get_stt_config() -> SttConfig:
    return get_providers_config().stt


def get_tts_config() -> TtsConfig:
    return get_providers_config().tts


def get_embedding_config() -> LocalEmbeddingConfig:
    return get_providers_config().embedding


def get_embedding_dimensions() -> int:
    return get_embedding_config().dimensions


def get_calendar_config() -> CalendarConfig | None:
    return get_providers_config().calendar


def get_dashboard_config() -> DashboardConfig | None:
    return get_providers_config().dashboard


def generate_dashboard_token() -> str:
    return secrets.token_hex(32)


def ensure_dashboard_token() -> str:
    providers = get_providers_config()
    if providers.dashboard is not None and providers.dashboard.token.strip():
        return providers.dashboard.token

    payload = _serialize_config(providers)
    payload["dashboard"] = {"token": generate_dashboard_token()}
    write_config("providers.json", payload)
    refreshed = get_providers_config().dashboard
    if refreshed is None:  # pragma: no cover - defensive.
        raise RuntimeError("Dashboard token was not persisted")
    return refreshed.token


def get_calendar_hub_config() -> CalendarHubConfig | None:
    calendar = get_calendar_config()
    if calendar is None or calendar.hub is None or not calendar.hub.write_enabled:
        return None
    return calendar.hub


def get_hub_tokens() -> OAuthTokens | None:
    """Load hub OAuth tokens from disk. Returns None if missing or unparseable."""
    path = get_config_dir() / "hub_tokens.json"
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        payload = _expect_mapping(raw, "hub_tokens.json")
        return OAuthTokens(
            access_token=_expect_str(payload.get("access_token"), "hub_tokens.json.access_token"),
            refresh_token=_expect_str(payload.get("refresh_token"), "hub_tokens.json.refresh_token"),
            expires_at=_expect_int(payload.get("expires_at"), "hub_tokens.json.expires_at"),
            token_type=_expect_str(payload.get("token_type", "Bearer"), "hub_tokens.json.token_type"),
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_hub_tokens(tokens: OAuthTokens) -> None:
    """Write hub OAuth tokens to disk atomically."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "hub_tokens.json"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=config_dir,
            delete=False,
            mode="w",
            encoding="utf-8",
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(
                json.dumps(
                    {
                        "access_token": tokens.access_token,
                        "refresh_token": tokens.refresh_token,
                        "expires_at": tokens.expires_at,
                        "token_type": tokens.token_type,
                    },
                    indent=2,
                )
                + "\n"
            )
        assert temp_path is not None
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


# ── Tunnel URL ────────────────────────────────────────────────────────────────

_tunnel_url: str | None = None


def set_tunnel_url(url: str) -> None:
    global _tunnel_url
    _tunnel_url = url


def get_tunnel_url() -> str | None:
    return _tunnel_url


def get_setup_status() -> SetupStatus:
    try:
        tailscale_ready, tailscale_reason = check_tailscale_ready()
    except Exception as exc:
        tailscale_ready, tailscale_reason = False, get_error_message(exc)

    try:
        has_twilio = get_providers_config().twilio is not None
    except Exception:
        has_twilio = False

    return SetupStatus(
        identity=identity_exists(),
        soul=soul_exists(),
        tailscale_installed=tailscale_ready,
        tailscale_reason=tailscale_reason,
        twilio=has_twilio,
    )


def _resolve_llm_slot(
    slot_name: str,
    slot: LLMSlotConfig | None,
    providers: ProvidersConfig,
) -> tuple[str, str | None, bool]:
    if slot and slot.provider in {"custom", "local"}:
        if not slot.baseURL:
            raise ValueError("Custom LLM requires baseURL")
        return slot.baseURL, slot.apiKey, True

    api_key = (
        slot.apiKey if slot and slot.apiKey
        else providers.openrouter.apiKey if providers.openrouter else None
    )
    if not api_key:
        raise ValueError(f"OpenRouter API key required for {slot_name} LLM")

    base_url = slot.baseURL if slot and slot.baseURL else OPENROUTER_BASE_URL
    return base_url, api_key, False


def get_realtime_llm_config() -> ResolvedLLMConfig:
    providers = get_providers_config()
    slot = providers.llm.realtime if providers.llm else None
    base_url, api_key, is_custom = _resolve_llm_slot("realtime", slot, providers)
    if is_custom and not (slot and slot.model):
        raise ValueError("Realtime LLM slot requires model")
    model = slot.model if slot and slot.model else DEFAULT_REALTIME_MODEL
    return ResolvedLLMConfig(baseURL=base_url, apiKey=api_key, model=model)


def get_backend_llm_config() -> ResolvedBackendLLMConfig:
    providers = get_providers_config()
    slot = providers.llm.backend if providers.llm else None
    base_url, api_key, is_custom = _resolve_llm_slot("backend", slot, providers)
    return ResolvedBackendLLMConfig(baseURL=base_url, apiKey=api_key, isCustom=is_custom)


def _validate_filename(filename: str) -> ConfigFilename:
    if filename not in CONFIG_FILENAMES:
        raise ValueError(f"Unsupported config file: {filename}")
    return cast(ConfigFilename, filename)


def _parse_config(filename: str, raw: Any) -> ConfigValue:
    if filename == "agent.json":
        return _parse_agent_config(raw)
    if filename == "providers.json":
        return _parse_providers_config(raw)
    if filename == "intelligence.json":
        return _parse_intelligence_config(raw)
    raise AssertionError(f"Unexpected config file: {filename}")


def _parse_agent_config(raw: Any) -> AgentConfig:
    data = _expect_mapping(raw, "agent.json")
    owner_raw = data.get("owner")
    if owner_raw is not None:
        owner_map = _expect_mapping(owner_raw, "owner")
        unknown = set(owner_map) - {"phone"}
        if unknown:
            raise ValueError(f"Unknown keys in owner: {', '.join(sorted(unknown))}")
        owner_phone = _optional_str(owner_map.get("phone"), "owner.phone")
        if owner_phone is not None:
            owner_phone = validate_e164(owner_phone)
        owner_config = AgentOwnerConfig(phone=owner_phone)
    else:
        owner_config = AgentOwnerConfig()
    agent = _expect_mapping(data.get("agent"), "agent")
    hours = _expect_mapping(data.get("hours"), "hours")
    server = _expect_mapping(data.get("server"), "server")
    tunnel_raw = data.get("tunnel")
    recording_raw = data.get("recording")

    max_active_jobs = server.get("maxActiveJobs")
    if max_active_jobs is not None:
        max_active_jobs = _expect_int(max_active_jobs, "server.maxActiveJobs")
        if max_active_jobs <= 0:
            raise ValueError("server.maxActiveJobs must be positive")

    tunnel_enabled = True
    if tunnel_raw is not None:
        tunnel = _expect_mapping(tunnel_raw, "tunnel")
        tunnel_enabled = _expect_bool(tunnel.get("enabled", True), "tunnel.enabled")

    recording_enabled = False
    if recording_raw is not None:
        recording = _expect_mapping(recording_raw, "recording")
        recording_enabled = _expect_bool(recording.get("enabled", False), "recording.enabled")

    return AgentConfig(
        owner=owner_config,
        agent=AgentDetailsConfig(
            name=_expect_str(agent.get("name"), "agent.name"),
            voiceId=_optional_str(agent.get("voiceId"), "agent.voiceId"),
        ),
        hours=AgentHoursConfig(
            start=_expect_int(hours.get("start"), "hours.start"),
            end=_expect_int(hours.get("end"), "hours.end"),
            timezone=_expect_str(hours.get("timezone"), "hours.timezone"),
            days=_expect_str_list(hours.get("days"), "hours.days"),
        ),
        server=AgentServerConfig(
            port=_expect_int(server.get("port"), "server.port"),
            maxActiveJobs=max_active_jobs,
        ),
        tunnel=AgentTunnelConfig(enabled=tunnel_enabled),
        recording=AgentRecordingConfig(enabled=recording_enabled),
    )


def _parse_providers_config(raw: Any) -> ProvidersConfig:
    data = _expect_mapping(raw, "providers.json")
    dashboard_raw = data.get("dashboard")
    calendar_raw = data.get("calendar")
    twilio_raw = data.get("twilio")
    twilio_draft_raw = data.get("twilioDraft")
    smtp_raw = data.get("smtp")
    openrouter_raw = data.get("openrouter")
    llm_raw = data.get("llm")

    dashboard = None
    if dashboard_raw is not None:
        dashboard_map = _expect_mapping(dashboard_raw, "dashboard")
        dashboard = DashboardConfig(
            token=_expect_str(dashboard_map.get("token"), "dashboard.token"),
        )

    calendar = None
    if calendar_raw is not None:
        calendar_map = _expect_mapping(calendar_raw, "calendar")
        subscriptions_raw = calendar_map.get("subscriptions", [])
        if not isinstance(subscriptions_raw, list):
            raise ValueError("calendar.subscriptions must be an array")
        subscriptions: list[CalendarSubscription] = []
        for index, item in enumerate(cast(list[Any], subscriptions_raw)):
            item_map = _expect_mapping(item, f"calendar.subscriptions[{index}]")
            subscriptions.append(
                CalendarSubscription(
                    url=_expect_str(item_map.get("url"), f"calendar.subscriptions[{index}].url"),
                    label=_optional_str(item_map.get("label"), f"calendar.subscriptions[{index}].label"),
                )
            )

        sync_interval_raw = calendar_map.get("syncIntervalMinutes")
        sync_interval_minutes = 15 if sync_interval_raw is None else _expect_int(
            sync_interval_raw,
            "calendar.syncIntervalMinutes",
        )
        if sync_interval_minutes < 0:
            raise ValueError("calendar.syncIntervalMinutes must be non-negative")

        reminder_raw = calendar_map.get("reminderMinutes")
        reminder_minutes = 10 if reminder_raw is None else _expect_int(
            reminder_raw,
            "calendar.reminderMinutes",
        )
        if reminder_minutes < 0:
            raise ValueError("calendar.reminderMinutes must be non-negative")

        hub = None
        hub_raw = calendar_map.get("hub")
        if hub_raw is not None:
            hub_map = _expect_mapping(hub_raw, "calendar.hub")
            provider = _expect_str(hub_map.get("provider"), "calendar.hub.provider")
            if provider not in {"google", "microsoft", "caldav"}:
                raise ValueError("calendar.hub.provider must be google, microsoft, or caldav")
            hub = CalendarHubConfig(
                provider=provider,
                calendar_id=_expect_str(hub_map.get("calendarId"), "calendar.hub.calendarId"),
                client_id=_optional_str(hub_map.get("clientId"), "calendar.hub.clientId"),
                client_secret=_optional_str(hub_map.get("clientSecret"), "calendar.hub.clientSecret"),
                base_url=_optional_str(hub_map.get("baseUrl"), "calendar.hub.baseUrl"),
                username=_optional_str(hub_map.get("username"), "calendar.hub.username"),
                password=_optional_str(hub_map.get("password"), "calendar.hub.password"),
                write_enabled=_expect_bool(hub_map.get("writeEnabled", True), "calendar.hub.writeEnabled"),
            )

        calendar = CalendarConfig(
            subscriptions=subscriptions,
            sync_interval_minutes=sync_interval_minutes,
            reminder_minutes=reminder_minutes,
            hub=hub,
        )

    twilio = None
    if twilio_raw is not None:
        twilio_map = _expect_mapping(twilio_raw, "twilio")
        twilio = TwilioConfig(
            accountSid=_expect_str(twilio_map.get("accountSid"), "twilio.accountSid"),
            authToken=_expect_str(twilio_map.get("authToken"), "twilio.authToken"),
            phoneNumber=validate_e164(_expect_str(twilio_map.get("phoneNumber"), "twilio.phoneNumber")),
            phoneNumberSid=_optional_str(twilio_map.get("phoneNumberSid"), "twilio.phoneNumberSid"),
        )

    twilio_draft = None
    if twilio_draft_raw is not None:
        twilio_draft_map = _expect_mapping(twilio_draft_raw, "twilioDraft")
        twilio_draft = TwilioDraftConfig(
            accountSid=_expect_str(twilio_draft_map.get("accountSid"), "twilioDraft.accountSid"),
            authToken=_expect_str(twilio_draft_map.get("authToken"), "twilioDraft.authToken"),
        )

    smtp = None
    if smtp_raw is not None:
        smtp_map = _expect_mapping(smtp_raw, "smtp")
        smtp = SmtpConfig(
            host=_expect_str(smtp_map.get("host"), "smtp.host"),
            port=_expect_int(smtp_map.get("port"), "smtp.port"),
            username=_expect_str(smtp_map.get("username"), "smtp.username"),
            password=_expect_str(smtp_map.get("password"), "smtp.password"),
            from_address=_expect_str(smtp_map.get("fromAddress"), "smtp.fromAddress"),
            use_tls=_expect_bool(smtp_map.get("useTls", True), "smtp.useTls"),
        )

    openrouter = None
    if openrouter_raw is not None:
        openrouter_map = _expect_mapping(openrouter_raw, "openrouter")
        openrouter = OpenRouterConfig(apiKey=_expect_str(openrouter_map.get("apiKey"), "openrouter.apiKey"))

    llm = None
    if llm_raw is not None:
        llm_map = _expect_mapping(llm_raw, "llm")
        llm = LLMConfig(
            realtime=_parse_llm_slot_config(llm_map.get("realtime"), "llm.realtime"),
            backend=_parse_llm_slot_config(llm_map.get("backend"), "llm.backend"),
        )

    livekit_map = _expect_mapping(data.get("livekit"), "livekit")

    return ProvidersConfig(
        dashboard=dashboard,
        calendar=calendar,
        twilio=twilio,
        twilioDraft=twilio_draft,
        smtp=smtp,
        livekit=LiveKitConfig(
            host=_expect_str(livekit_map.get("host"), "livekit.host"),
            port=_expect_int(livekit_map.get("port"), "livekit.port"),
            apiKey=_expect_str(livekit_map.get("apiKey"), "livekit.apiKey"),
            apiSecret=_expect_str(livekit_map.get("apiSecret"), "livekit.apiSecret"),
        ),
        stt=_parse_stt_config(data.get("stt")),
        tts=_parse_tts_config(data.get("tts")),
        embedding=_parse_embedding_config(data.get("embedding")),
        openrouter=openrouter,
        llm=llm,
    )


def _parse_intelligence_config(raw: Any) -> IntelligenceConfig:
    data = _expect_mapping(raw, "intelligence.json")
    extraction = _expect_mapping(data.get("extraction"), "extraction")
    judgment = _expect_mapping(data.get("judgment"), "judgment")
    summarization = _expect_mapping(data.get("summarization"), "summarization")
    retrieval = _expect_mapping(data.get("retrieval"), "retrieval")

    limit = _expect_int(retrieval.get("limit"), "retrieval.limit")
    if limit <= 0:
        raise ValueError("retrieval.limit must be positive")

    return IntelligenceConfig(
        extraction=ExtractionModels(
            facts=_parse_model_ref(extraction.get("facts"), "extraction.facts"),
            commitments=_parse_model_ref(extraction.get("commitments"), "extraction.commitments"),
        ),
        judgment=JudgmentModels(
            scheduler=_parse_model_ref(judgment.get("scheduler"), "judgment.scheduler"),
            satisfaction=_parse_model_ref(judgment.get("satisfaction"), "judgment.satisfaction"),
            owner_call=_parse_model_ref(judgment.get("owner_call"), "judgment.owner_call"),
        ),
        summarization=SummarizationModels(
            person=_parse_model_ref(summarization.get("person"), "summarization.person"),
            call=_parse_model_ref(summarization.get("call"), "summarization.call"),
        ),
        editing=_parse_model_ref(data.get("editing"), "editing"),
        search=_parse_model_ref(data.get("search"), "search"),
        retrieval=RetrievalConfig(
            vectorWeight=_expect_float(retrieval.get("vectorWeight"), "retrieval.vectorWeight"),
            ftsWeight=_expect_float(retrieval.get("ftsWeight"), "retrieval.ftsWeight"),
            threshold=_expect_float(retrieval.get("threshold"), "retrieval.threshold"),
            limit=limit,
        ),
    )


def _parse_model_ref(raw: Any, path: str) -> ModelRef:
    data = _expect_mapping(raw, path)
    return ModelRef(model=_expect_str(data.get("model"), f"{path}.model"))


def _parse_stt_config(raw: Any) -> SttConfig:
    if raw is None:
        return UnconfiguredSttConfig()
    data = _expect_mapping(raw, "stt")
    provider = (_optional_str(data.get("provider"), "stt.provider") or "").strip().lower()
    if not provider:
        return UnconfiguredSttConfig()
    if provider == "moonshine":
        model = _expect_str(data.get("model"), "stt.model")
        if model not in {"tiny", "small", "medium"}:
            raise ValueError("stt.model must be 'tiny', 'small', or 'medium'")
        return MoonshineSttConfig(provider=provider, model=model)
    if provider == "deepgram":
        return DeepgramSttConfig(
            provider=provider,
            apiKey=_expect_str(data.get("apiKey"), "stt.apiKey"),
            model=_optional_str(data.get("model"), "stt.model"),
        )
    raise ValueError(f"Unsupported STT provider: {provider}")


def _parse_tts_config(raw: Any) -> TtsConfig:
    if raw is None:
        return UnconfiguredTtsConfig()
    data = _expect_mapping(raw, "tts")
    provider = (_optional_str(data.get("provider"), "tts.provider") or "").strip().lower()
    if not provider:
        return UnconfiguredTtsConfig()
    if provider == "pocket":
        return PocketTtsConfig(
            provider=provider,
            model=_optional_str(data.get("model"), "tts.model"),
            pythonCommand=_optional_str(data.get("pythonCommand"), "tts.pythonCommand"),
        )
    if provider == "inworld":
        return InworldTtsConfig(
            provider=provider,
            apiKey=_expect_str(data.get("apiKey"), "tts.apiKey"),
            model=_optional_str(data.get("model"), "tts.model"),
        )
    raise ValueError(f"Unsupported TTS provider: {provider}")


def _parse_embedding_config(raw: Any) -> LocalEmbeddingConfig:
    data = _expect_mapping(raw, "embedding")
    provider = _expect_str(data.get("provider"), "embedding.provider")
    if provider == "local":
        model = data.get("model", DEFAULT_LOCAL_EMBEDDING_MODEL)
        dimensions = data.get("dimensions", DEFAULT_LOCAL_EMBEDDING_DIMENSIONS)
        parsed_dimensions = _expect_int(dimensions, "embedding.dimensions")
        if parsed_dimensions <= 0:
            raise ValueError("embedding.dimensions must be positive")
        return LocalEmbeddingConfig(
            provider=provider,
            model=_expect_str(model, "embedding.model"),
            dimensions=parsed_dimensions,
        )
    raise ValueError(f"Unsupported embedding provider: {provider}")


def _parse_llm_slot_config(raw: Any, path: str) -> LLMSlotConfig | None:
    if raw is None:
        return None
    data = _expect_mapping(raw, path)
    provider = _expect_str(data.get("provider"), f"{path}.provider")
    if provider not in {"openrouter", "custom", "local"}:
        raise ValueError(f"{path}.provider must be openrouter, custom, or local")
    return LLMSlotConfig(
        provider=provider,
        baseURL=_optional_str(data.get("baseURL"), f"{path}.baseURL"),
        model=_optional_str(data.get("model"), f"{path}.model"),
        apiKey=_optional_str(data.get("apiKey"), f"{path}.apiKey"),
    )


def _serialize_config(data: ConfigValue | Mapping[str, Any]) -> dict[str, Any]:
    if is_dataclass(data):
        serialized = asdict(data)
        if isinstance(data, ProvidersConfig):
            calendar = serialized.get("calendar")
            if isinstance(calendar, dict):
                if "sync_interval_minutes" in calendar:
                    calendar["syncIntervalMinutes"] = calendar.pop("sync_interval_minutes")
                if "reminder_minutes" in calendar:
                    calendar["reminderMinutes"] = calendar.pop("reminder_minutes")
                hub = calendar.get("hub")
                if isinstance(hub, dict):
                    if "calendar_id" in hub:
                        hub["calendarId"] = hub.pop("calendar_id")
                    if "client_id" in hub:
                        hub["clientId"] = hub.pop("client_id")
                    if "client_secret" in hub:
                        hub["clientSecret"] = hub.pop("client_secret")
                    if "base_url" in hub:
                        hub["baseUrl"] = hub.pop("base_url")
                    if "write_enabled" in hub:
                        hub["writeEnabled"] = hub.pop("write_enabled")
            smtp = serialized.get("smtp")
            if isinstance(smtp, dict):
                if "from_address" in smtp:
                    smtp["fromAddress"] = smtp.pop("from_address")
                if "use_tls" in smtp:
                    smtp["useTls"] = smtp.pop("use_tls")
    else:
        serialized = dict(data)
    return serialized


def _expect_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    return dict(cast(Mapping[str, Any], value))


def _expect_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    return value


def _optional_str(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, path)


def _expect_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    return value


def _expect_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _expect_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    return float(value)


def _expect_str_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return [_expect_str(item, f"{path}[]") for item in cast(list[object], value)]


def _normalize_dashboard_file_name(name: str) -> str:
    normalized = name.strip().replace("\\", "/").lstrip("/")
    if not normalized:
        raise ValueError("Dashboard file name is required")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid dashboard file path")
    return path.as_posix()


def list_dashboard_files() -> list[str]:
    dashboard_dir = get_dashboard_dir()
    if not dashboard_dir.exists():
        return []

    files: list[str] = []
    for path in sorted(dashboard_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(dashboard_dir)
        except ValueError:  # pragma: no cover - defensive.
            continue
        if relative.parts and relative.parts[0] == ".history":
            continue
        files.append(relative.as_posix())
    return files


def read_dashboard_file(name: str) -> str:
    normalized = _normalize_dashboard_file_name(name)
    path = get_dashboard_dir() / normalized
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Dashboard file not found: {normalized}")
    return path.read_text(encoding="utf-8")


def write_dashboard_file(
    name: str,
    content: str,
    *,
    trigger: str = "design-dashboard",
    note: str = "",
) -> Path:
    normalized = _normalize_dashboard_file_name(name)
    path = get_dashboard_dir() / normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _save_dashboard_history(path, normalized, trigger=trigger, note=note)
    path.write_text(content, encoding="utf-8")
    logger.info("dashboard.file.written", path=str(path), trigger=trigger)
    return path


def _save_dashboard_history(path: Path, relative_name: str, *, trigger: str, note: str = "") -> None:
    history_dir = get_dashboard_history_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)
    stem = relative_name.replace("/", "__")
    history_path = history_dir / f"{timestamp}-{stem}"
    while history_path.exists():
        timestamp += 1
        history_path = history_dir / f"{timestamp}-{stem}"
    cleaned_trigger = trigger.strip() or "design-dashboard"
    cleaned_note = " ".join(str(note).strip().splitlines())
    payload = (
        "---\n"
        f"timestamp: {timestamp}\n"
        f"trigger: {cleaned_trigger}\n"
        f"note: {cleaned_note}\n"
        f"path: {relative_name}\n"
        "---\n\n"
        f"{path.read_text(encoding='utf-8')}"
    )
    history_path.write_text(payload, encoding="utf-8")


# Imported late so mystic.http can depend on TwilioConfig and logger without a
# circular import during module initialization.
from mystic.http import (  # noqa: E402
    DEFAULT_TIMEOUT_MS,
    AsyncHttpClient,
    HttpResponse,
    RequestTransport,
    TransportResult,
    _default_transport,
    _immediate,
    _normalize_payload,
    check_tailscale_ready,
    create_client,
    fetch_with_timeout,
    get_tailscale_hostname,
    patch_twilio_phone_webhook,
    start_tunnel,
    stop_tunnel,
)
