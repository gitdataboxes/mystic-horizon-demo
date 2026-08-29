from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from mystic.cli import preflight_check
from mystic.config import (
    LocalEmbeddingConfig,
    LiveKitConfig,
    MoonshineSttConfig,
    PocketTtsConfig,
    ProvidersConfig,
    TwilioConfig,
    UnconfiguredSttConfig,
    UnconfiguredTtsConfig,
)
from mystic.livekit import ResolvedLiveKitBinary


def _make_providers(
    *,
    twilio: bool = False,
    tts_model: str | None = None,
) -> ProvidersConfig:
    twilio_config = (
        TwilioConfig(accountSid="AC123", authToken="tok", phoneNumber="+15551234567")
        if twilio
        else None
    )
    return ProvidersConfig(
        livekit=LiveKitConfig(host="127.0.0.1", port=7880, apiKey="k", apiSecret="s"),
        stt=MoonshineSttConfig(provider="moonshine", model="small"),
        tts=PocketTtsConfig(provider="pocket", model=tts_model),
        embedding=LocalEmbeddingConfig(
            provider="local",
            model="nomic-embed-text-v1.5",
            dimensions=256,
        ),
        twilio=twilio_config,
    )


def _healthy_mocks(tmp_path: Path | None = None):
    """Context manager stack for a fully-healthy preflight baseline."""
    from contextlib import ExitStack

    stack = ExitStack()
    if tmp_path is not None:
        binary = tmp_path / "livekit-server"
        binary.write_bytes(b"bin")
        resolved = ResolvedLiveKitBinary(binary, "1.9.12")
    else:
        resolved = None
    stack.enter_context(patch("mystic.cli.resolve_supported_livekit_binary", return_value=resolved))
    stack.enter_context(patch("mystic.cli.pocket_onnx_models_missing", return_value=[]))
    stack.enter_context(patch("mystic.cli.embedding_model_missing", return_value=[]))
    stack.enter_context(patch("mystic.cli.turn_detector_assets_missing", return_value=[]))
    stack.enter_context(patch("mystic.cli.is_python_package_available", return_value=True))
    return stack


class TestPreflightCheck:
    def test_all_deps_present(self, tmp_path: Path) -> None:
        providers = _make_providers()
        with _healthy_mocks(tmp_path):
            errors = preflight_check(providers)
        assert errors == []

    def test_missing_livekit_binary(self) -> None:
        providers = _make_providers()
        with _healthy_mocks() as stack:
            stack.enter_context(patch("mystic.cli.resolve_supported_livekit_binary", return_value=None))
            errors = preflight_check(providers)
        assert any("livekit-server" in e for e in errors)

    def test_tailscale_not_ready_with_twilio(self, tmp_path: Path) -> None:
        providers = _make_providers(twilio=True)
        with _healthy_mocks(tmp_path) as stack:
            stack.enter_context(patch("mystic.cli.check_tailscale_ready", return_value=(False, "not installed")))
            errors = preflight_check(providers)
        assert any("Tailscale not ready" in e for e in errors)

    def test_no_tailscale_error_without_twilio(self, tmp_path: Path) -> None:
        providers = _make_providers(twilio=False)
        with _healthy_mocks(tmp_path):
            errors = preflight_check(providers)
        assert not any("Tailscale not ready" in e for e in errors)

    def test_missing_livekit_binary_on_macos_recommends_homebrew(self) -> None:
        providers = _make_providers()
        with _healthy_mocks() as stack:
            stack.enter_context(patch("mystic.cli.resolve_supported_livekit_binary", return_value=None))
            stack.enter_context(patch("mystic.livekit.get_platform_system", return_value="Darwin"))
            errors = preflight_check(providers)
        assert any("brew install livekit" in e for e in errors)

    def test_invalid_livekit_binary_surfaces_version_error(self) -> None:
        providers = _make_providers()
        with _healthy_mocks() as stack:
            stack.enter_context(patch(
                "mystic.cli.resolve_supported_livekit_binary",
                side_effect=RuntimeError("Unsupported livekit-server at /usr/local/bin/livekit-server"),
            ))
            errors = preflight_check(providers)
        assert any("Unsupported livekit-server" in e for e in errors)

    def test_missing_pocket_onnx_models_surface_preflight_error(self, tmp_path: Path) -> None:
        providers = _make_providers()
        with _healthy_mocks(tmp_path) as stack:
            stack.enter_context(patch(
                "mystic.cli.pocket_onnx_models_missing",
                return_value=["onnx/flow_lm_main.onnx", "tokenizer.model"],
            ))
            errors = preflight_check(providers)
        assert any("Pocket ONNX models are missing" in e for e in errors)
        assert any("flow_lm_main.onnx" in e for e in errors)

    def test_unsupported_pocket_model_surfaces_preflight_error(self, tmp_path: Path) -> None:
        providers = _make_providers(tts_model="voice_cloning")
        with _healthy_mocks(tmp_path):
            errors = preflight_check(providers)
        assert any("unsupported tts.model='voice_cloning'" in e for e in errors)

    def test_missing_moonshine_package_surfaces_error(self, tmp_path: Path) -> None:
        providers = _make_providers()
        with _healthy_mocks(tmp_path) as stack:
            stack.enter_context(patch("mystic.cli.is_python_package_available", return_value=False))
            errors = preflight_check(providers)
        assert any("Moonshine Voice package not installed" in e for e in errors)

    def test_missing_turn_detector_package_surfaces_error(self, tmp_path: Path) -> None:
        providers = _make_providers()
        with _healthy_mocks(tmp_path) as stack:
            stack.enter_context(
                patch(
                    "mystic.cli.is_python_package_available",
                    side_effect=lambda name: name != "livekit.plugins.turn_detector",
                )
            )
            errors = preflight_check(providers)
        assert any("LiveKit turn detector package not installed" in e for e in errors)

    def test_missing_turn_detector_files_surface_error(self, tmp_path: Path) -> None:
        providers = _make_providers()
        with _healthy_mocks(tmp_path) as stack:
            stack.enter_context(
                patch(
                    "mystic.cli.turn_detector_assets_missing",
                    return_value=["languages.json", "onnx/model_q8.onnx"],
                )
            )
            errors = preflight_check(providers)
        assert any("LiveKit turn detector files are missing" in e for e in errors)
        assert any("languages.json" in e for e in errors)

    def test_missing_embedding_model_surfaces_error(self, tmp_path: Path) -> None:
        providers = _make_providers()
        with _healthy_mocks(tmp_path) as stack:
            stack.enter_context(patch(
                "mystic.cli.embedding_model_missing",
                return_value=["model.onnx"],
            ))
            errors = preflight_check(providers)
        assert any("Embedding model files missing" in e for e in errors)
        assert any("model.onnx" in e for e in errors)

    def test_unconfigured_voice_providers_skip_local_model_checks(self, tmp_path: Path) -> None:
        providers = _make_providers()
        providers.stt = UnconfiguredSttConfig()
        providers.tts = UnconfiguredTtsConfig()
        with _healthy_mocks(tmp_path):
            errors = preflight_check(providers)
        assert errors == []
