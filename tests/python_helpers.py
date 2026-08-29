"""Shared helpers for Python unit tests."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from mystic.config import clear_config_cache, silence_stdout

TEST_AGENT_CONFIG: dict[str, object] = {
    "owner": {"phone": "+15551234567"},
    "agent": {"name": "TestBot"},
    "hours": {
        "start": 9,
        "end": 17,
        "timezone": "America/New_York",
        "days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
    },
    "server": {"port": 3456},
    "tunnel": {"enabled": True},
}

TEST_PROVIDERS_CONFIG: dict[str, object] = {
    "twilio": {
        "accountSid": "test-twilio-sid",
        "authToken": "test-twilio-token",
        "phoneNumber": "+15550001234",
    },
    "livekit": {
        "host": "127.0.0.1",
        "port": 7880,
        "apiKey": "test-lk-key",
        "apiSecret": "test-lk-secret",
    },
    "stt": {
        "provider": "moonshine",
        "model": "small",
    },
    "tts": {
        "provider": "pocket",
    },
    "embedding": {"provider": "local", "model": "nomic-embed-text-v1.5", "dimensions": 256},
    "openrouter": {"apiKey": "test-openrouter-key"},
    "llm": {
        "realtime": {"provider": "openrouter", "model": "openai/gpt-5.4-mini"},
        "backend": {"provider": "openrouter"},
    },
}

TEST_INTELLIGENCE_CONFIG: dict[str, object] = {
    "extraction": {
        "facts": {"model": "test/model"},
        "commitments": {"model": "test/model"},
    },
    "judgment": {
        "scheduler": {"model": "test/model"},
        "satisfaction": {"model": "test/model"},
        "owner_call": {"model": "test/model"},
    },
    "summarization": {
        "person": {"model": "test/model"},
        "call": {"model": "test/model"},
    },
    "editing": {"model": "test/model"},
    "search": {"model": "test/model"},
    "retrieval": {
        "vectorWeight": 0.7,
        "ftsWeight": 0.3,
        "threshold": 0.0,
        "limit": 5,
    },
}

TEST_EMBEDDING_DIMENSIONS = 256

TEST_SOUL = "# Test Soul\n\nYou are a helpful test agent.\n"
TEST_IDENTITY = (
    "# Identity\n\n"
    "- **Name:** TestBot\n"
    "- **Creature:** digital assistant\n"
    "- **Vibe:** helpful and precise\n"
    "- **Emoji:** \U0001F916\n"
)


class TempAppHome:
    def __init__(self) -> None:
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.home: Path | None = None
        self._old_app_home = os.environ.get("APP_HOME")

    def __enter__(self) -> Path:
        self._tempdir = tempfile.TemporaryDirectory(prefix="mh-py-test-")
        self.home = Path(self._tempdir.name)
        os.environ["APP_HOME"] = str(self.home)
        clear_config_cache()
        silence_stdout(True)
        return self.home

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        clear_config_cache()
        silence_stdout(False)
        if self._old_app_home is None:
            os.environ.pop("APP_HOME", None)
        else:
            os.environ["APP_HOME"] = self._old_app_home
        if self._tempdir is not None:
            self._tempdir.cleanup()


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def seed_core_files(
    home: Path,
    *,
    agent: dict[str, object] | None = None,
    providers: dict[str, object] | None = None,
    intelligence: dict[str, object] | None = None,
    soul_text: str = TEST_SOUL,
    identity_text: str = TEST_IDENTITY,
) -> None:
    config_dir = home / "config"
    write_json(config_dir / "agent.json", agent or TEST_AGENT_CONFIG)
    write_json(config_dir / "providers.json", providers or TEST_PROVIDERS_CONFIG)
    write_json(config_dir / "intelligence.json", intelligence or TEST_INTELLIGENCE_CONFIG)
    (home / "SOUL.md").write_text(soul_text, encoding="utf-8")
    (home / "IDENTITY.md").write_text(identity_text, encoding="utf-8")


def make_embedding(
    values: Sequence[float],
    *,
    dimensions: int = TEST_EMBEDDING_DIMENSIONS,
) -> list[float]:
    padded = [float(value) for value in values[:dimensions]]
    if len(padded) < dimensions:
        padded.extend(0.0 for _ in range(dimensions - len(padded)))
    return padded
