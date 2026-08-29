"""Benchmarks for audio codec and resampling — these run in the 20ms voice frame loop."""

from __future__ import annotations

import random

import pytest

from mystic.audio import (
    downsample_16k_to_8k,
    mulaw_decode,
    mulaw_encode,
    upsample_8k_to_16k,
)

# 20ms of audio at different sample rates
FRAME_8K = [random.randint(-32000, 32000) for _ in range(160)]    # 8kHz * 0.02s
FRAME_16K = [random.randint(-32000, 32000) for _ in range(320)]   # 16kHz * 0.02s
MULAW_FRAME = mulaw_encode(FRAME_8K)


@pytest.mark.bench
class TestMulawCodec:
    def test_encode_20ms(self, benchmark):
        benchmark(mulaw_encode, FRAME_8K)

    def test_decode_20ms(self, benchmark):
        benchmark(mulaw_decode, MULAW_FRAME)

    def test_roundtrip_20ms(self, benchmark):
        def _roundtrip():
            encoded = mulaw_encode(FRAME_8K)
            mulaw_decode(encoded)

        benchmark(_roundtrip)


@pytest.mark.bench
class TestResampling:
    def test_upsample_8k_to_16k_20ms(self, benchmark):
        benchmark(upsample_8k_to_16k, FRAME_8K)

    def test_downsample_16k_to_8k_20ms(self, benchmark):
        benchmark(downsample_16k_to_8k, FRAME_16K)


@pytest.mark.bench
class TestFullDecodePipeline:
    """Full inbound audio path: mulaw decode -> upsample to 16kHz."""

    def test_decode_and_upsample(self, benchmark):
        def _pipeline():
            pcm = mulaw_decode(MULAW_FRAME)
            upsample_8k_to_16k(pcm)

        benchmark(_pipeline)

    def test_downsample_and_encode(self, benchmark):
        """Full outbound audio path: downsample 16k -> 8k -> mulaw encode."""

        def _pipeline():
            downsampled = downsample_16k_to_8k(FRAME_16K)
            mulaw_encode(downsampled)

        benchmark(_pipeline)
